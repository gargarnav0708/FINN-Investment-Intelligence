"""Business snapshot and bull vs bear, both from primary documents only.

* Business snapshot (fast model): reads the annual report's business-overview / MD&A / segment pages,
  returns what the company does and its segments with revenue. Each segment revenue number must
  appear on its cited page; otherwise the number is dropped and only the segment name is kept.
* Bull vs bear (strong model, one call per company): reads the most relevant pages of the annual report
  and the last four concall transcripts plus the delivered-numbers table. Every point must carry a
  verbatim quote from a cited page; points whose quote is not found are dropped.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import llm
from .guidance import _ground, _table_text, delivered_table, recent_transcripts
from .models import Company, CompanyInsight, DocumentPage, SourceDocument
from .results_extraction import _canon, _number_tokens

SNAPSHOT_VERSION = "business-snapshot-v1"
BULLBEAR_VERSION = "bull-bear-v1"

SNAPSHOT_SYSTEM = """You summarise an Indian listed company's business from its own annual report.
Use ONLY the pages given. Return ONLY JSON."""
SNAPSHOT_USER = """Company: {company} ({ticker}). Annual report: {title}.

Return:
{{"description": "<what the company does, 2 sentences, max 50 words>",
  "fiscal_year": "<e.g. FY26>",
  "segments": [{{"name": "<segment as reported>", "what": "<max 12 words>",
                "revenue": <segment revenue as printed, number, or null>, "unit": "crore"|"lakh"|"million"|null,
                "page": <page number where that revenue figure is printed, or null>}}],
  "mix_note": "<one line on revenue concentration or mix shift, or null>"}}
Up to 8 reportable segments. Copy revenue numbers exactly as printed (consolidated if both are given).

Pages:
{text}"""

BULLBEAR_SYSTEM = """You are a buy-side analyst writing a tight bull and bear case for an Indian listed company.
Use ONLY the primary-source excerpts and the delivered-numbers table provided: no outside knowledge, no news,
no broker views. Each point must be specific (numbers where possible) and backed by a quote copied
VERBATIM from one excerpt (max 40 words), with that excerpt's doc id and page. Return ONLY JSON."""
BULLBEAR_USER = """Company: {company} ({ticker})

DELIVERED NUMBERS (INR crore; _pct = percent; growth = YoY):
{table}

EXCERPTS:
{excerpts}

Return {{"bull": [{{"point": "<max 22 words>", "quote": "<verbatim>", "doc": <doc id>, "page": <page>}}],
         "bear": [ ...same... ],
         "verdict": "<one sentence on what decides between the two cases>"}}
Exactly 3 bull and 3 bear points, the strongest first."""

UNIT_TO_CRORE = {"crore": 1.0, "lakh": 0.01, "million": 0.1}
BULL_TERMS = ("growth", "grew", "expand", "demand", "order book", "pipeline", "market share", "margin improvement", "record", "capacity",
              "guidance", "outlook", "new product", "export", "operating leverage", "free cash", "deleverag")
BEAR_TERMS = ("risk", "challenge", "pressure", "decline", "competition", "headwind", "slowdown", "debt", "borrowing", "working capital",
              "receivable", "contingent", "regulatory", "delay", "impairment", "write", "volatil", "concentration", "litigation")


def _upsert(db: Session, company: Company, kind: str, status: str, payload: dict, model: str | None, input_hash: str | None) -> dict:
    row = db.scalar(select(CompanyInsight).where(CompanyInsight.company_id == company.id, CompanyInsight.kind == kind))
    if row is None:
        row = CompanyInsight(company_id=company.id, kind=kind, status=status)
        db.add(row)
    row.status, row.payload, row.model, row.input_hash = status, payload, model, input_hash
    db.commit()
    return {"status": status, **payload, "model": model}


def latest_annual_report(db: Session, company: Company) -> SourceDocument | None:
    docs = db.scalars(select(SourceDocument).where(SourceDocument.company_id == company.id, SourceDocument.document_type == "annual_report",
                                                   SourceDocument.local_file_path.is_not(None))).all()
    docs = [d for d in docs if db.scalar(select(DocumentPage.id).where(DocumentPage.document_id == d.id).limit(1))]
    docs.sort(key=lambda d: (d.document_date or datetime.min.date(), d.id), reverse=True)
    return docs[0] if docs else None


def _snapshot_pages(pages: list[DocumentPage], limit_chars: int = 45000) -> list[DocumentPage]:
    def score(page: DocumentPage) -> int:
        low = page.extracted_text.lower()
        s = 0
        for term, w in (("segment", 3), ("business overview", 4), ("our business", 3), ("management discussion", 3), ("revenue from operations", 2),
                        ("segment revenue", 5), ("operating segments", 5), ("business segments", 4), ("product", 1), ("customers", 1), ("at a glance", 3)):
            s += w * min(low.count(term), 3)
        return s
    ranked = sorted((p for p in pages if len(p.extracted_text) > 120), key=lambda p: -score(p))
    chosen, total = [], 0
    for page in ranked[:16]:
        if score(page) <= 2 or total + len(page.extracted_text) > limit_chars:
            continue
        chosen.append(page)
        total += len(page.extracted_text)
    return sorted(chosen, key=lambda p: p.page_number)


def build_business_snapshot(db: Session, company: Company, force: bool = False) -> dict:
    report = latest_annual_report(db, company)
    if report is None:
        return _upsert(db, company, "business_snapshot", "unavailable", {"message": "No indexed annual report for this company yet."}, None, None)
    pages = db.scalars(select(DocumentPage).where(DocumentPage.document_id == report.id)).all()
    chosen = _snapshot_pages(pages)
    if not chosen:
        return _upsert(db, company, "business_snapshot", "unavailable", {"message": "No business or segment pages were found in the annual report text."}, None, None)
    text = "\n".join(f"[[PAGE {p.page_number}]]\n{p.extracted_text}" for p in chosen)
    input_hash = hashlib.sha256((SNAPSHOT_VERSION + text).encode()).hexdigest()
    existing = db.scalar(select(CompanyInsight).where(CompanyInsight.company_id == company.id, CompanyInsight.kind == "business_snapshot"))
    if existing and existing.input_hash == input_hash and existing.status == "available" and not force:
        return {"status": existing.status, **existing.payload, "model": existing.model}
    reply, meta = llm.complete_json(db, tier="fast", purpose="business_snapshot", prompt_version=SNAPSHOT_VERSION, system=SNAPSHOT_SYSTEM,
                                    max_tokens=2500, user=SNAPSHOT_USER.format(company=company.name, ticker=company.ticker, title=report.title, text=text))
    by_page = {p.page_number: p.extracted_text for p in pages}
    segments = []
    for seg in reply.get("segments") or []:
        name = " ".join(str(seg.get("name") or "").split())[:80]
        if not name:
            continue
        revenue, page, verified = seg.get("revenue"), seg.get("page"), False
        try:
            page = int(page) if page is not None else None
            value = float(str(revenue).replace(",", "")) if revenue not in (None, "") else None
        except (TypeError, ValueError):
            page, value = None, None
        if value is not None and page in by_page:
            from decimal import Decimal
            verified = _canon(Decimal(str(value))) in _number_tokens(by_page[page]) or f"{value:,.2f}".replace(",", "") in _number_tokens(by_page[page])
        factor = UNIT_TO_CRORE.get(str(seg.get("unit") or "crore").lower(), 1.0)
        segments.append({"name": name, "what": str(seg.get("what") or "")[:120] or None,
                         "revenue_crore": round(value * factor, 2) if verified else None, "page": page if verified else None,
                         "source_url": f"/api/documents/{report.id}/file#page={page}" if verified else None})
    total = sum(s["revenue_crore"] for s in segments if s["revenue_crore"] and s["revenue_crore"] > 0)
    for s in segments:
        s["share_pct"] = round(s["revenue_crore"] / total * 100, 1) if total and s["revenue_crore"] and s["revenue_crore"] > 0 else None
    cited_pages = [p.page_number for p in chosen[:6]]
    payload = {"description": str(reply.get("description") or "")[:500], "fiscal_year": reply.get("fiscal_year"), "segments": segments,
               "mix_note": reply.get("mix_note"), "document_id": report.id, "document_title": report.title, "pages_read": [p.page_number for p in chosen],
               "description_source_url": f"/api/documents/{report.id}/file#page={cited_pages[0]}" if cited_pages else None,
               "generated_at": datetime.now(timezone.utc).isoformat()}
    return _upsert(db, company, "business_snapshot", "available", payload, meta.get("model"), input_hash)


def _excerpt_pool(db: Session, company: Company, max_pages: int = 22, per_page: int = 3200) -> tuple[list[dict], dict[int, dict[int, str]]]:
    docs = recent_transcripts(db, company)
    report = latest_annual_report(db, company)
    if report:
        docs = docs + [report]
    pool, texts = [], {}
    for doc in docs:
        pages = db.scalars(select(DocumentPage).where(DocumentPage.document_id == doc.id)).all()
        texts[doc.id] = {p.page_number: p.extracted_text for p in pages}
        for page in pages:
            low = page.extracted_text.lower()
            if len(low) < 400:
                continue
            bull = sum(low.count(t) for t in BULL_TERMS)
            bear = sum(low.count(t) for t in BEAR_TERMS)
            numeric = len(re.findall(r"\d+(?:\.\d+)?\s*(?:%|crore|cr\b|bn|gw|mw)", low))
            pool.append({"doc": doc.id, "title": doc.title, "page": page.page_number, "bull": bull, "bear": bear, "numeric": numeric,
                         "text": page.extracted_text[:per_page], "transcript": doc.document_type == "earnings_call_transcript"})
    bull_ranked = sorted(pool, key=lambda x: -(x["bull"] * 2 + x["numeric"] + (3 if x["transcript"] else 0)))
    bear_ranked = sorted(pool, key=lambda x: -(x["bear"] * 2 + x["numeric"] + (3 if x["transcript"] else 0)))
    picked, seen = [], set()
    for a, b in zip(bull_ranked, bear_ranked):
        for item in (a, b):
            key = (item["doc"], item["page"])
            if key not in seen and len(picked) < max_pages:
                seen.add(key)
                picked.append(item)
    return picked, texts


def build_bull_bear(db: Session, company: Company, force: bool = False) -> dict:
    excerpts, texts = _excerpt_pool(db, company)
    if len(excerpts) < 3:
        return _upsert(db, company, "bull_bear", "unavailable", {"message": "Not enough indexed annual-report or transcript pages for a grounded bull/bear case."}, None, None)
    table = delivered_table(db, company)
    excerpt_text = "\n\n".join(f"<<doc {e['doc']} | {e['title']} | page {e['page']}>>\n{e['text']}" for e in excerpts)
    input_hash = hashlib.sha256((BULLBEAR_VERSION + excerpt_text + _table_text(table)).encode()).hexdigest()
    existing = db.scalar(select(CompanyInsight).where(CompanyInsight.company_id == company.id, CompanyInsight.kind == "bull_bear"))
    if existing and existing.input_hash == input_hash and existing.status in {"available", "partial"} and not force:
        return {"status": existing.status, **existing.payload, "model": existing.model}
    reply, meta = llm.complete_json(db, tier="strong", purpose="bull_bear", prompt_version=BULLBEAR_VERSION, system=BULLBEAR_SYSTEM, max_tokens=8000,
                                    user=BULLBEAR_USER.format(company=company.name, ticker=company.ticker, table=_table_text(table), excerpts=excerpt_text))
    titles = {e["doc"]: e["title"] for e in excerpts}
    out, dropped = {"bull": [], "bear": []}, 0
    for side in ("bull", "bear"):
        for item in (reply.get(side) or [])[:4]:
            try:
                doc = int(item.get("doc"))
                hint = int(item.get("page")) if item.get("page") is not None else None
            except (TypeError, ValueError):
                dropped += 1
                continue
            page, quote = _ground(str(item.get("quote") or ""), hint, texts.get(doc, {}))
            if page is None:
                dropped += 1
                continue
            out[side].append({"point": str(item.get("point") or "")[:220], "quote": quote[:400], "document_id": doc,
                              "document_title": titles.get(doc), "page": page, "source_url": f"/api/documents/{doc}/file#page={page}"})
        out[side] = out[side][:3]
    status = "available" if len(out["bull"]) >= 2 and len(out["bear"]) >= 2 else "partial"
    payload = {**out, "verdict": reply.get("verdict"), "dropped_ungrounded": dropped, "pages_read": len(excerpts),
               "degraded_from": meta.get("degraded_from"), "generated_at": datetime.now(timezone.utc).isoformat()}
    return _upsert(db, company, "bull_bear", status, payload, meta.get("model"), input_hash)


def insights_payload(db: Session, company: Company) -> dict:
    rows = {r.kind: r for r in db.scalars(select(CompanyInsight).where(CompanyInsight.company_id == company.id)).all()}
    def view(kind: str) -> dict:
        row = rows.get(kind)
        return {"status": "not_run"} if row is None else {"status": row.status, **row.payload, "model": row.model}
    return {"ticker": company.ticker, "business_snapshot": view("business_snapshot"), "bull_bear": view("bull_bear"), "llm": llm.status()}
