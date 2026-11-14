"""Narrative vs numbers for any company: what management promised, what was delivered.

1. Guidance extraction (fast model, one call per transcript, cached by file hash):
   the full transcript text with [[PAGE n]] markers goes in; management guidance statements
   come out as structured JSON. Speakers are identified by the model from the transcript itself
   (no hard-coded names). Every quote is then grounded: it must appear verbatim on the cited
   page (or anywhere in the transcript, with the page corrected) or it is discarded.
2. Delivered numbers are computed deterministically: machine-read results filings first,
   screener.in standardised quarterly/annual tables as the fallback baseline.
3. Assessment (strong model, one call per company, cached by input hash): each guidance item is
   judged against the delivered table only. Every number the model cites is checked against
   that table; unmatched numbers downgrade the verdict to "unverified".
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from difflib import SequenceMatcher
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import llm
from .models import (CommitmentAssessment, Company, DocumentPage, FinancialObservation, ManagementStatement,
                     NarrativeAssessment, ProviderFinancialObservation, SourceDocument)

EXTRACT_VERSION = "guidance-extract-v1"
ASSESS_VERSION = "narrative-assess-v3"
PARSER_VERSION = "llm-guidance-v1"
METRICS = ("revenue_growth", "revenue", "ebitda_margin", "ebitda", "ebitda_growth", "pat", "pat_growth", "pat_margin",
           "capex", "volume_growth", "order_book", "order_inflow", "net_debt", "dividend", "capacity", "market_share",
           "cost", "other")
VERDICT_TO_STATUS = {"delivered": "achieved", "exceeded": "exceeded", "missed": "missed", "partly": "partially_achieved",
                     "pending": "in_progress", "not_measurable": "not_comparable", "unverified": "needs_review"}

EXTRACT_SYSTEM = """You analyse Indian listed-company earnings-call transcripts for an investment firm.
Extract forward-looking GUIDANCE given by the company's own management (CEO, CFO, MD, chairman, business heads,
investor-relations head). Never extract statements by analysts, moderators or operators.
Guidance = an explicit expectation, target, plan or commitment about future results or operations
(e.g. revenue growth, margins, capex, volumes, order book, debt, capacity, launches, timelines).
Prefer quantified guidance; include clearly directional guidance on headline metrics.
Quotes must be copied VERBATIM from the transcript (a single contiguous span, at most 60 words) and must be
taken from the page indicated by the nearest preceding [[PAGE n]] marker. Return ONLY JSON."""

EXTRACT_USER = """Company: {company} ({ticker})
Transcript: {title}

Return JSON:
{{
 "call_date": "YYYY-MM-DD or null",
 "quarter_discussed": "e.g. Q2 FY26",
 "management": [{{"name": "...", "designation": "..."}}],
 "guidance": [
   {{"speaker": "...", "designation": "...", "page": <int>, "quote": "<verbatim>",
     "metric": one of {metrics},
     "metric_detail": "<what exactly is being guided, <=12 words>",
     "kind": "quantitative" | "directional",
     "target_low": <number or null>, "target_high": <number or null>,
     "unit": "%" | "crore" | "x" | "units" | "other" | null,
     "target_period": "<e.g. FY26, Q3 FY26, H2 FY26, next 2-3 years, null>",
     "direction": "increase" | "decrease" | "maintain" | null,
     "lifecycle": "new" | "reaffirmed" | "revised" | "withdrawn"}}
 ]
}}
At most 12 guidance items, most material first. If none, return an empty list.

Transcript:
{text}"""

ASSESS_SYSTEM = """You are an equity analyst holding management to account. You judge each guidance statement
ONLY against the DELIVERED table provided (company filings). Do not use outside knowledge of the company.
Rules:
- "pending" ONLY if the target period ends AFTER the latest delivered period. Indian fiscal years end on
  31 March (FY26 = Apr 2025-Mar 2026). If the target period has ended and the table has that period
  (annual rows for full-year targets, quarterly rows for quarter targets), you MUST judge it.
- For a target whose period is still running, you may still say "partly" or "missed" when the year-to-date
  quarters make the target clearly unreachable or clearly on track; otherwise "pending".
- "not_measurable" if the table has no metric that measures the guidance.
- "delivered" / "exceeded" / "missed" / "partly" only with explicit numbers from the table.
- Every number you cite must be copied from the table (same metric key and period).
Be terse and specific. Return ONLY JSON."""

ASSESS_USER = """Company: {company} ({ticker}). Today: {today}. Latest delivered quarter: {latest}. Latest delivered full year: {latest_year}.

GUIDANCE (id, said on, speaker, quote, parsed target):
{guidance}

DELIVERED (INR crore unless the key ends in _pct; growth keys are YoY %):
{table}

Return one item for EVERY guidance id listed above. JSON:
{{
 "items": [{{"id": <guidance id>, "verdict": "delivered"|"exceeded"|"missed"|"partly"|"pending"|"not_measurable",
            "actual": "<what actually happened, <=20 words, with numbers>",
            "explanation": "<why this verdict, <=30 words>",
            "numbers_used": [{{"metric": "<table key>", "period": "<table period label>", "value": <number>}}]}}],
 "scorecard": {{"credibility": "high"|"mixed"|"low"|"insufficient_evidence",
               "headline": "<one sentence verdict on management's track record>",
               "pattern": "<one sentence on any pattern, e.g. margins repeatedly over-promised; or null>",
               "watch_next": "<the single guidance item most worth tracking next quarter, <=20 words>"}}
}}"""


def _norm(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-")
    return " ".join(text.split()).lower()


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9%]", "", _norm(text))


def _ground(quote: str, page_hint: int | None, pages: dict[int, str]) -> tuple[int | None, str | None]:
    """Return (page, quote) if the quote is in the transcript, else (None, None).

    PDF text breaks sentences with line ends, hyphenation and running page headers, so matching is
    done on letters and digits only; a quote is accepted if >= 90% of it appears on one page in long
    (12+ character) in-order runs within a short stretch of text. Paraphrases and inventions fail.
    """
    target = _compact(quote)
    if len(target) < 20:
        return None, None
    order = ([page_hint] if page_hint in pages else []) + [p for p in sorted(pages) if p != page_hint]
    compact_pages = {p: _compact(pages[p]) for p in order}
    for page in order:
        if target in compact_pages[page]:
            return page, quote.strip()
    for page in order:
        body = compact_pages[page]
        blocks = [b for b in SequenceMatcher(None, body, target, autojunk=False).get_matching_blocks() if b.size >= 12]
        if not blocks:
            continue
        covered = sum(b.size for b in blocks)
        span = (blocks[-1].a + blocks[-1].size) - blocks[0].a
        # >= 90% of the quote in long in-order runs, within a short stretch of the page
        # (tolerates hyphenation, page headers/footers and a dropped filler word; not paraphrase).
        if covered >= 0.9 * len(target) and span <= 1.3 * len(target) + 40:
            return page, quote.strip()
    return None, None


def _parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def recent_transcripts(db: Session, company: Company, limit: int = 4) -> list[SourceDocument]:
    docs = db.scalars(select(SourceDocument).where(SourceDocument.company_id == company.id,
                                                   SourceDocument.document_type == "earnings_call_transcript",
                                                   SourceDocument.local_file_path.is_not(None))).all()
    docs.sort(key=lambda d: (d.document_date or date.min, d.id), reverse=True)
    return docs[:limit]


def extract_guidance(db: Session, company: Company, document: SourceDocument) -> dict:
    pages = {p.page_number: p.extracted_text for p in db.scalars(select(DocumentPage).where(DocumentPage.document_id == document.id)).all()}
    if not pages:
        return {"document_id": document.id, "status": "not_indexed", "inserted": 0}
    text = "\n".join(f"[[PAGE {n}]]\n{pages[n]}" for n in sorted(pages))[:400_000]
    result, meta = llm.complete_json(db, tier="fast", purpose="guidance_extract", prompt_version=EXTRACT_VERSION,
                                     system=EXTRACT_SYSTEM, max_tokens=6000,
                                     user=EXTRACT_USER.format(company=company.name, ticker=company.ticker, title=document.title,
                                                              metrics=list(METRICS), text=text))
    call_date = _parse_date(result.get("call_date")) or document.document_date
    if call_date and (document.document_date is None or abs((call_date - document.document_date).days) < 62):
        document.document_date = call_date
    inserted = dropped = 0
    for item in result.get("guidance") or []:
        if not isinstance(item, dict) or not item.get("quote"):
            continue
        try:
            hint = int(item.get("page")) if item.get("page") is not None else None
        except (TypeError, ValueError):
            hint = None
        page, quote = _ground(str(item["quote"]), hint, pages)
        if page is None:
            dropped += 1
            continue
        speaker = str(item.get("speaker") or "Management").strip()[:180]
        key = hashlib.sha256(f"{document.id}|{page}|{_norm(quote)}".encode()).hexdigest()
        row = db.scalar(select(ManagementStatement).where(ManagementStatement.evidence_key == key))
        metric = item.get("metric") if item.get("metric") in METRICS else "other"
        lifecycle = item.get("lifecycle") if item.get("lifecycle") in {"new", "reaffirmed", "revised", "withdrawn"} else "new"
        statement_class = {"reaffirmed": "reaffirmation", "revised": "revision", "withdrawn": "withdrawal"}.get(
            lifecycle, "quantitative_guidance" if item.get("kind") == "quantitative" else "qualitative_forward_looking")
        values = dict(
            company_id=company.id, document_id=document.id, document_type=document.document_type,
            transcript_date=call_date, speaker=speaker, designation=(str(item.get("designation") or "") or None),
            page_number=page, exact_quote=quote, surrounding_context=pages[page][:4000],
            statement_class=statement_class, lifecycle="original" if lifecycle == "new" else lifecycle,
            topic=str(item.get("metric_detail") or metric.replace("_", " ")).strip()[:180] or "Guidance",
            metric_name=metric, metric_definition=str(item.get("metric_detail") or "")[:500] or None,
            target_low=_float(item.get("target_low")), target_high=_float(item.get("target_high") if item.get("target_high") is not None else item.get("target_low")),
            unit=(str(item.get("unit")) if item.get("unit") else None), currency="INR" if item.get("unit") == "crore" else None,
            target_period=(str(item.get("target_period")) if item.get("target_period") else None),
            baseline_period=str(result.get("quarter_discussed") or "")[:100] or None,
            assumptions=(f"direction: {item['direction']}" if item.get("direction") else None),
            extraction_status="grounded",
        )
        if row is None:
            row = ManagementStatement(evidence_key=key, parser_version=PARSER_VERSION, review_status="approved",
                                      reviewer="finn-auto (quote verified verbatim)", reviewed_at=datetime.now(timezone.utc), **values)
            db.add(row)
            inserted += 1
        elif row.parser_version == PARSER_VERSION and (row.reviewer or "").startswith("finn-auto"):
            for k, v in values.items():
                setattr(row, k, v)
    db.commit()
    return {"document_id": document.id, "title": document.title, "status": "success", "inserted": inserted,
            "dropped_ungrounded": dropped, "model": meta.get("model"), "cached": meta.get("cached")}


def _float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _quarter_label(end: date) -> str:
    fy = end.year + (1 if end.month >= 4 else 0)
    q = ((end.month - 4) % 12) // 3 + 1
    return f"Q{q} FY{str(fy)[-2:]}"


def delivered_table(db: Session, company: Company) -> dict:
    """Deterministic delivered numbers: filings first, screener.in standardised tables as fallback."""
    quarterly: dict[date, dict] = defaultdict(dict)
    sources: dict[date, str] = {}
    filed = db.scalars(select(FinancialObservation).where(
        FinancialObservation.company_id == company.id, FinancialObservation.period_type == "quarterly",
        FinancialObservation.extraction_status == "validated", FinancialObservation.statement_basis == "consolidated")).all()
    if not filed:
        filed = db.scalars(select(FinancialObservation).where(
            FinancialObservation.company_id == company.id, FinancialObservation.period_type == "quarterly",
            FinancialObservation.extraction_status == "validated")).all()
    for row in filed:
        if row.canonical_metric in {"revenue", "ebitda", "pbt", "pat"}:
            quarterly[row.period_end][row.canonical_metric] = float(row.normalised_value_in_crore)
            sources[row.period_end] = "results filing"
    provider = db.scalars(select(ProviderFinancialObservation).where(
        ProviderFinancialObservation.company_id == company.id, ProviderFinancialObservation.provider == "screener.in")).all()
    annual: dict[date, dict] = defaultdict(dict)
    for row in provider:
        target = quarterly if row.period_type == "quarterly" else annual
        if row.period_type == "quarterly" and row.canonical_metric in quarterly.get(row.period_end, {}):
            continue
        target[row.period_end].setdefault(row.canonical_metric, float(row.reported_value))
        if row.period_type == "quarterly":
            sources.setdefault(row.period_end, "screener.in standardised")

    def enrich(table: dict[date, dict], lag: int) -> list[dict]:
        out = []
        for end in sorted(table):
            values = dict(table[end])
            if values.get("revenue"):
                if values.get("ebitda") is not None:
                    values["ebitda_margin_pct"] = round(values["ebitda"] / values["revenue"] * 100, 2)
                if values.get("pat") is not None:
                    values["pat_margin_pct"] = round(values["pat"] / values["revenue"] * 100, 2)
            prior_end = date(end.year - 1, end.month, end.day if end.month != 2 else 28)
            prior = table.get(prior_end)
            if prior:
                for metric in ("revenue", "ebitda", "pat"):
                    if values.get(metric) is not None and prior.get(metric):
                        if prior[metric] > 0:
                            values[f"{metric}_growth_pct"] = round((values[metric] / prior[metric] - 1) * 100, 2)
            values = {k: round(v, 2) if isinstance(v, float) else v for k, v in values.items()}
            out.append({"period_end": end.isoformat(), "period": _quarter_label(end) if lag == 3 else f"FY{str(end.year)[-2:]}",
                        "source": sources.get(end, "screener.in standardised"), **values})
        return out

    q_rows = enrich(quarterly, 3)[-10:]
    a_rows = enrich(annual, 12)[-5:]
    return {"quarterly": q_rows, "annual": a_rows, "latest_quarter": q_rows[-1]["period"] if q_rows else None}


def _table_text(table: dict) -> str:
    keys = ("revenue", "revenue_growth_pct", "ebitda", "ebitda_margin_pct", "ebitda_growth_pct", "pat", "pat_margin_pct",
            "pat_growth_pct", "borrowings", "cfo")
    lines = []
    for section in ("quarterly", "annual"):
        for row in table[section]:
            parts = [f"{k}={row[k]}" for k in keys if row.get(k) is not None]
            lines.append(f"{row['period']} [{section}, {row['source']}]: " + ", ".join(parts))
    return "\n".join(lines) or "(no delivered numbers available)"


def _check_numbers(numbers: list, table: dict) -> bool:
    index = {}
    for section in ("quarterly", "annual"):
        for row in table[section]:
            for key, value in row.items():
                if isinstance(value, (int, float)):
                    index[(row["period"].replace(" ", "").upper(), key)] = float(value)
    if not numbers:
        return False
    for item in numbers:
        try:
            key = (str(item["period"]).replace(" ", "").upper(), str(item["metric"]))
            actual = index.get(key)
            if actual is None or abs(actual - float(item["value"])) > max(abs(actual) * 0.01, 0.06):
                return False
        except (KeyError, TypeError, ValueError):
            return False
    return True


def assess_narrative(db: Session, company: Company, force: bool = False) -> dict:
    statements = db.scalars(select(ManagementStatement).where(ManagementStatement.company_id == company.id,
                                                              ManagementStatement.review_status == "approved")
                            .order_by(ManagementStatement.transcript_date.desc(), ManagementStatement.id)).all()
    table = delivered_table(db, company)
    # Most decision-relevant first: quantified targets, then most recent.
    statements.sort(key=lambda s: (s.target_low is None, -(s.transcript_date.toordinal() if s.transcript_date else 0), s.id))
    budget = llm.input_budget("strong")
    statements = statements[:40]
    chunk_size = 40
    if budget:
        # Small-budget provider (Groq free): judge statements in batches so every one gets a verdict.
        fixed = llm.estimate_tokens(ASSESS_SYSTEM + ASSESS_USER + _table_text(table)) + 200
        chunk_size = max(4, min(8, (budget - fixed) // 110))
    record = db.scalar(select(NarrativeAssessment).where(NarrativeAssessment.company_id == company.id))
    if not statements:
        payload = {"status": "no_guidance", "message": "No grounded guidance statements are indexed for this company yet.", "delivered": table}
        if record is None:
            record = NarrativeAssessment(company_id=company.id, status="no_guidance", payload=payload)
            db.add(record)
        else:
            record.status, record.payload = "no_guidance", payload
        db.commit()
        return payload
    guidance_text = "\n".join(
        f"[{s.id}] said {s.transcript_date or 'n/a'} ({s.baseline_period or 'call'}), {s.speaker}{' - ' + s.designation if s.designation else ''}: "
        f"\"{s.exact_quote}\" | metric={s.metric_name}; target={s.target_low}-{s.target_high} {s.unit or ''}; period={s.target_period or 'unspecified'}; {s.assumptions or ''}"
        for s in statements)
    table_text = _table_text(table)
    input_hash = hashlib.sha256((ASSESS_VERSION + guidance_text + table_text).encode()).hexdigest()  # prompt changes invalidate it
    if record and record.input_hash == input_hash and record.status == "available" and not force:
        return record.payload
    lines = guidance_text.split("\n")
    chunks = [lines[i:i + chunk_size] for i in range(0, len(lines), chunk_size)]
    result, meta, items = {}, {}, []
    for chunk in chunks:
        result, meta = llm.complete_json(db, tier="strong", purpose="narrative_assess", prompt_version=ASSESS_VERSION,
                                         system=ASSESS_SYSTEM, max_tokens=12000,
                                         user=ASSESS_USER.format(company=company.name, ticker=company.ticker,
                                                                 latest=table.get("latest_quarter") or "unknown",
                                                                 latest_year=(table["annual"][-1]["period"] if table.get("annual") else "unknown"),
                                                                 today=date.today().isoformat(),
                                                                 guidance="\n".join(chunk), table=table_text))
        items.extend(result.get("items") or [])
    scorecard = result.get("scorecard") or {}
    if len(chunks) > 1:
        # One more small call so the scorecard reflects every verdict, not just the last batch.
        summary = "\n".join(f"[{i.get('id')}] {i.get('verdict')}: {str(i.get('actual') or '')[:120]}" for i in items)
        try:
            sc, _ = llm.complete_json(db, tier="strong", purpose="narrative_scorecard", prompt_version=ASSESS_VERSION, max_tokens=1200,
                                      system="You summarise how reliably a company's management delivered on its guidance. Return ONLY JSON.",
                                      user=f"Company: {company.name}. Verdicts on management guidance (id, verdict, what happened):\n{summary}\n\n"
                                           'Return {"scorecard": {"credibility": "high"|"mixed"|"low"|"insufficient_evidence", "headline": "<one sentence>", '
                                           '"pattern": "<one sentence or null>", "watch_next": "<max 20 words>"}}')
            scorecard = sc.get("scorecard") or scorecard
        except llm.LLMUnavailable:
            pass
    result = {"items": items, "scorecard": scorecard}
    by_id = {s.id: s for s in statements}
    verdicts = []
    for item in result.get("items") or []:
        try:
            statement = by_id.get(int(item.get("id")))
        except (TypeError, ValueError):
            statement = None
        if statement is None:
            continue
        verdict = item.get("verdict") if item.get("verdict") in VERDICT_TO_STATUS else "not_measurable"
        numbers = item.get("numbers_used") or []
        checked = _check_numbers(numbers, table)
        if verdict in {"delivered", "exceeded", "missed", "partly"} and not checked:
            verdict = "unverified"
        explanation = str(item.get("explanation") or "").strip()
        actual = str(item.get("actual") or "").strip()
        assessment = db.scalar(select(CommitmentAssessment).where(CommitmentAssessment.statement_id == statement.id))
        if assessment is None:
            assessment = CommitmentAssessment(statement_id=statement.id, status="", explanation="")
            db.add(assessment)
        assessment.status = VERDICT_TO_STATUS[verdict]
        assessment.explanation = (f"{actual} — {explanation}" if actual else explanation)[:4000] or "No explanation returned."
        assessment.formula = json.dumps(numbers)[:4000] if numbers else None
        assessment.actual_value = None
        assessment.actual_unit = None
        assessment.assessed_at = datetime.now(timezone.utc)
        verdicts.append({"statement_id": statement.id, "verdict": verdict, "numbers_checked": checked, "actual": actual, "explanation": explanation, "numbers_used": numbers})
    counts = defaultdict(int)
    for v in verdicts:
        counts[v["verdict"]] += 1
    payload = {"status": "available", "scorecard": result.get("scorecard") or {}, "counts": dict(counts),
               "verdicts": verdicts, "delivered": table, "model": meta.get("model"), "degraded_from": meta.get("degraded_from"), "degraded_reason": meta.get("degraded_reason"),
               "assessed_at": datetime.now(timezone.utc).isoformat(), "guidance_count": len(statements)}
    if record is None:
        record = NarrativeAssessment(company_id=company.id, status="available", payload=payload)
        db.add(record)
    record.status, record.payload, record.model, record.input_hash = "available", payload, meta.get("model"), input_hash
    db.commit()
    return payload


def narrative_payload(db: Session, company: Company) -> dict:
    """Grouped by call: what was said, and what followed. Used by the Commitments view."""
    record = db.scalar(select(NarrativeAssessment).where(NarrativeAssessment.company_id == company.id))
    statements = db.scalars(select(ManagementStatement).where(ManagementStatement.company_id == company.id,
                                                              ManagementStatement.parser_version == PARSER_VERSION)
                            .order_by(ManagementStatement.transcript_date.desc(), ManagementStatement.page_number)).all()
    verdict_by_id = {v["statement_id"]: v for v in (record.payload.get("verdicts", []) if record else [])}
    calls: dict[str, dict] = {}
    for s in statements:
        key = f"{s.document_id}"
        call = calls.setdefault(key, {"document_id": s.document_id, "title": s.document.title,
                                      "call_date": s.transcript_date.isoformat() if s.transcript_date else None,
                                      "quarter": s.baseline_period, "items": []})
        v = verdict_by_id.get(s.id, {})
        call["items"].append({
            "id": s.id, "speaker": s.speaker, "designation": s.designation, "quote": s.exact_quote, "page": s.page_number,
            "metric": s.metric_name, "detail": s.metric_definition, "target_low": s.target_low, "target_high": s.target_high,
            "unit": s.unit, "target_period": s.target_period, "lifecycle": s.lifecycle,
            "source_url": f"/api/documents/{s.document_id}/file#page={s.page_number}",
            "verdict": v.get("verdict"), "actual": v.get("actual"), "explanation": v.get("explanation"),
            "numbers_checked": v.get("numbers_checked"), "numbers_used": v.get("numbers_used"),
        })
    ordered = sorted(calls.values(), key=lambda c: c["call_date"] or "", reverse=True)
    return {
        "ticker": company.ticker, "status": record.status if record else ("pending_assessment" if statements else "no_guidance"),
        "scorecard": (record.payload.get("scorecard") if record else None) or {},
        "counts": (record.payload.get("counts") if record else None) or {},
        "model": record.model if record else None,
        "assessed_at": record.payload.get("assessed_at") if record else None,
        "delivered": (record.payload.get("delivered") if record else None) or delivered_table(db, company),
        "calls": ordered, "llm": llm.status(),
    }
