"""Orchestration for the generic (any-ticker) research and results workflows."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import llm
from .database import DATA_DIR
from .guidance import assess_narrative, extract_guidance, recent_transcripts
from .intelligence import classify_announcement
from .models import (Announcement, Company, ProviderFinancialObservation, ResearchDatasetSnapshot, ResultsExtraction,
                     SourceDocument, utcnow)
from .providers import screener
from .providers.yahoo_research import _fiscal_period
from .research import parse_documents, sha256_file
from .results_extraction import extract_results_document

log = logging.getLogger("finn.pipeline")
Progress = Callable[[str, dict], None]
MONETARY = {"revenue", "ebitda", "other_income", "finance_costs", "depreciation", "pbt", "pat", "equity_capital", "reserves",
            "borrowings", "other_liabilities", "total_liabilities", "fixed_assets", "cwip", "investments", "other_assets",
            "total_assets", "deposits", "cfo", "cfi", "cff", "net_cash_flow"}


def _noop(stage: str, detail: dict) -> None:
    log.info("%s %s", stage, detail)


def _doc_root(company: Company):
    root = DATA_DIR / "documents" / re.sub(r"[^A-Z0-9_-]", "_", company.ticker.upper())
    root.mkdir(parents=True, exist_ok=True)
    return root


def _snapshot(db: Session, company: Company, dataset: str, status: str, count: int, error: str | None = None, details: dict | None = None) -> None:
    row = db.scalar(select(ResearchDatasetSnapshot).where(ResearchDatasetSnapshot.company_id == company.id, ResearchDatasetSnapshot.dataset == dataset))
    if row is None:
        row = ResearchDatasetSnapshot(company_id=company.id, dataset=dataset, provider="screener.in", status=status, record_count=count)
        db.add(row)
    row.provider, row.status, row.record_count, row.error, row.details, row.fetched_at = "screener.in", status, count, error, details or {}, utcnow()
    db.commit()


def store_screener_rows(db: Session, company: Company, page: screener.ScreenerPage) -> int:
    stored = 0
    for row in page.rows:
        if row.metric not in MONETARY:
            continue
        fiscal_year, fiscal_quarter = _fiscal_period(row.period_end)
        existing = db.scalar(select(ProviderFinancialObservation).where(
            ProviderFinancialObservation.company_id == company.id, ProviderFinancialObservation.statement_type == row.statement_type,
            ProviderFinancialObservation.period_type == row.period_type, ProviderFinancialObservation.period_end == row.period_end,
            ProviderFinancialObservation.canonical_metric == row.metric))
        values = dict(fiscal_year=fiscal_year, fiscal_quarter=fiscal_quarter if row.period_type == "quarterly" else None,
                      reported_label=f"{row.label} ({page.basis})", reported_value=row.value, currency="INR", reported_unit="INR crore",
                      normalised_value_in_crore=row.value, provider="screener.in", source_url=page.url, fetched_at=utcnow())
        if existing is None:
            db.add(ProviderFinancialObservation(company_id=company.id, statement_type=row.statement_type, period_type=row.period_type,
                                                period_end=row.period_end, canonical_metric=row.metric, **values))
        else:
            for key, value in values.items():
                setattr(existing, key, value)
        stored += 1
    db.commit()
    return stored


def register_document(db: Session, company: Company, doc: screener.ScreenerDocument, download: bool = True) -> tuple[SourceDocument, str | None]:
    dedupe = hashlib.sha256(f"SCREENER|{company.ticker}|{doc.url}".encode()).hexdigest()
    row = db.scalar(select(SourceDocument).where(SourceDocument.dedupe_key == dedupe))
    if row is None:
        row = SourceDocument(company_id=company.id, dedupe_key=dedupe, title=doc.title, document_type=doc.document_type,
                             fiscal_year=doc.fiscal_year, quarter=None, document_date=doc.document_date,
                             landing_page_url=f"{screener.BASE}/company/{company.ticker}/", download_url=doc.url,
                             source="Company filing (linked via screener.in)", discovery_status="discovered")
        db.add(row)
        db.commit()
    error = None
    if download and not row.local_file_path:
        try:
            content = screener.download_pdf(doc.url)
            path = _doc_root(company) / (hashlib.sha256(doc.url.encode()).hexdigest()[:20] + ".pdf")
            path.write_bytes(content)
            row.local_file_path = str(path.relative_to(DATA_DIR.parent))
            row.file_hash = sha256_file(path)
            row.content_type = "application/pdf"
            row.discovery_status = "downloaded"
        except Exception as exc:
            row.discovery_status = "provider_error"
            error = f"{doc.title}: {type(exc).__name__}: {exc}"[:300]
        db.commit()
    return row, error


def sync_screener(db: Session, company: Company, transcripts: int = 4, annual_reports: int = 1, progress: Progress = _noop) -> dict:
    progress("screener", {"ticker": company.ticker, "step": "fetching company page"})
    try:
        page = screener.fetch_company(company.ticker)
    except Exception as exc:
        _snapshot(db, company, "screener", "provider_error", 0, str(exc))
        return {"status": "provider_error", "error": str(exc)}
    if page.name and company.name.upper() == company.ticker.upper():
        company.name = page.name
        db.commit()
    stored = store_screener_rows(db, company, page)
    wanted = [d for d in page.documents if d.document_type == "earnings_call_transcript"]
    wanted.sort(key=lambda d: d.document_date or date.min, reverse=True)
    reports = sorted((d for d in page.documents if d.document_type == "annual_report"), key=lambda d: d.document_date or date.min, reverse=True)
    selected = wanted[:transcripts] + reports[:annual_reports]
    errors = []
    for index, doc in enumerate(selected, 1):
        progress("documents", {"ticker": company.ticker, "step": f"downloading {doc.title}", "done": index - 1, "total": len(selected)})
        _, error = register_document(db, company, doc)
        if error:
            errors.append(error)
    progress("documents", {"ticker": company.ticker, "step": "indexing pages", "total": len(selected)})
    parsed = parse_documents(db, ticker=company.ticker)
    details = {"basis": page.basis, "financial_rows": stored, "documents_listed": len(page.documents),
               "transcripts_selected": len(wanted[:transcripts]), "annual_reports_selected": len(reports[:annual_reports]),
               "indexed": parsed.get("processed", 0), "about": page.about}
    _snapshot(db, company, "screener", "partial" if errors else "available", stored, "; ".join(errors) or None, details)
    return {"status": "partial" if errors else "success", "errors": errors, **details}


def _results_announcements(db: Session, company: Company, days: int) -> list[Announcement]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.scalars(select(Announcement).where(Announcement.company_id == company.id, Announcement.attachment_url.is_not(None))
                      .order_by(Announcement.announcement_datetime.desc())).all()
    picked = []
    for item in rows:
        stamp = item.announcement_datetime
        if stamp is not None:
            stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
            if stamp < since:
                continue
        text = f"{item.subject} {item.details or ''}".lower()
        from .intelligence import keyword_category
        nse_category = (item.original_category or "").lower()
        candidate = (classify_announcement(item)["category"] == "results" or keyword_category(item) == "results"
                     or "financial result" in text or "result" in nse_category or "outcome of board meeting" in text)
        # Board-meeting outcomes without results are harmless here: no statement page is found and no model call is made.
        if not candidate:
            continue
        if any(term in text for term in ("newspaper", "publication", "intimation of board meeting", "prior intimation", "trading window")):
            continue
        strong = (classify_announcement(item)["category"] == "results" or keyword_category(item) == "results"
                  or "financial result" in text or "result" in nse_category)
        picked.append((0 if strong else 1, item))
    # Clear results filings first (newest first), generic board-meeting outcomes after them.
    picked.sort(key=lambda pair: pair[0])
    return [item for _, item in picked]


def process_results(db: Session, company: Company, days: int = 120, limit: int = 3, progress: Progress = _noop) -> dict:
    """Download, index and machine-read recent results filings for one company."""
    from .services import _register_exchange_documents

    items = _results_announcements(db, company, days)[:limit]
    if not items:
        return {"status": "empty", "processed": 0}
    payload = [{"subject": i.subject, "details": i.details, "attachment_url": i.attachment_url, "source_page_url": i.source_page_url,
                "announcement_datetime": i.announcement_datetime} for i in items]
    registered = _register_exchange_documents(db, company, payload, force_type="quarterly_results")
    outcomes = []
    for item in items:
        document = db.scalar(select(SourceDocument).where(SourceDocument.company_id == company.id, SourceDocument.download_url == item.attachment_url))
        if document is None or not document.local_file_path:
            outcomes.append({"announcement_id": item.id, "status": "not_downloaded"})
            continue
        progress("results", {"ticker": company.ticker, "step": f"reading {item.subject[:60]}"})
        try:
            record = extract_results_document(db, document)
            outcomes.append({"announcement_id": item.id, "document_id": document.id, "status": record.status, "error": record.error})
        except Exception as exc:
            db.rollback()
            outcomes.append({"announcement_id": item.id, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
    return {"status": "success", "processed": len(outcomes), "registered": registered, "outcomes": outcomes}


def process_portfolio_results(db: Session, days: int = 3, progress: Progress = _noop) -> dict:
    from .models import PortfolioHolding

    if not llm.is_configured():
        return {"status": "skipped", "reason": "No model provider configured"}
    companies = db.scalars(select(Company).join(PortfolioHolding).where(PortfolioHolding.is_active.is_(True))).all()
    report = {}
    for company in companies:
        try:
            result = process_results(db, company, days=days, progress=progress)
            if result["status"] != "empty":
                report[company.ticker] = result
        except Exception as exc:
            db.rollback()
            report[company.ticker] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    return {"status": "success", "companies": report}


def deep_research(db: Session, ticker: str, progress: Progress = _noop, force_assessment: bool = False) -> dict:
    """Everything the Research view needs for one company, from primary sources, for any NSE ticker."""
    from .services import refresh_company_research, research_company

    ticker = ticker.strip().upper()
    company = db.scalar(select(Company).where(Company.ticker == ticker))
    report: dict = {"ticker": ticker}
    if company is None:
        progress("market", {"ticker": ticker, "step": "registering company"})
        if not re.fullmatch(r"[A-Z][A-Z0-9&-]{0,19}", ticker):
            raise ValueError("Ticker must be an NSE symbol.")
        try:
            report["register"] = research_company(db, ticker)
        except Exception as exc:
            # Market data is a nice-to-have here; primary documents can still be researched.
            db.rollback()
            report["register"] = {"status": "market_unavailable", "error": f"{type(exc).__name__}: {exc}"[:300]}
        company = db.scalar(select(Company).where(Company.ticker == ticker))
        if company is None:
            company = Company(name=ticker, ticker=ticker, exchange="NSE", sector=None, yfinance_symbol=f"{ticker}.NS")
            db.add(company)
            db.commit()
    else:
        progress("market", {"ticker": ticker, "step": "refreshing market data and filings"})
        try:
            report["provider_refresh"] = refresh_company_research(db, company)
        except Exception as exc:
            db.rollback()
            report["provider_refresh"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    report["screener"] = sync_screener(db, company, progress=progress)
    if not llm.is_configured():
        report["llm"] = {"status": "not_configured", "message": "Set FINN_LLM_API_KEY to enable results reading, guidance extraction and assessment."}
        return report
    try:
        report["results"] = process_results(db, company, days=200, limit=2, progress=progress)
    except Exception as exc:
        db.rollback()
        report["results"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    guidance = []
    transcripts = recent_transcripts(db, company)
    for index, document in enumerate(transcripts, 1):
        progress("guidance", {"ticker": ticker, "step": f"reading {document.title}", "done": index - 1, "total": len(transcripts)})
        try:
            guidance.append(extract_guidance(db, company, document))
        except Exception as exc:
            db.rollback()
            guidance.append({"document_id": document.id, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
    report["guidance"] = guidance
    from .announcement_intel import enrich_announcements
    from .company_insights import build_bull_bear, build_business_snapshot
    progress("announcements", {"ticker": ticker, "step": "classifying filings and profiling management changes"})
    try:
        report["announcement_intel"] = enrich_announcements(db, days=180, company_ids=[company.id])
    except Exception as exc:
        db.rollback()
        report["announcement_intel"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    progress("snapshot", {"ticker": ticker, "step": "reading the annual report's business and segment pages"})
    try:
        snap = build_business_snapshot(db, company)
        report["business_snapshot"] = {"status": snap.get("status"), "segments": len(snap.get("segments") or []), "model": snap.get("model")}
    except Exception as exc:
        db.rollback()
        report["business_snapshot"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    progress("assessment", {"ticker": ticker, "step": "comparing promises with delivered numbers"})
    try:
        assessment = assess_narrative(db, company, force=force_assessment)
        report["assessment"] = {"status": assessment.get("status"), "counts": assessment.get("counts"), "model": assessment.get("model"),
                                "degraded_from": assessment.get("degraded_from"), "degraded_reason": assessment.get("degraded_reason")}
    except Exception as exc:
        db.rollback()
        report["assessment"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    progress("bull_bear", {"ticker": ticker, "step": "building the bull and bear case from primary sources"})
    try:
        bb = build_bull_bear(db, company, force=force_assessment)
        report["bull_bear"] = {"status": bb.get("status"), "bull": len(bb.get("bull") or []), "bear": len(bb.get("bear") or []),
                               "dropped_ungrounded": bb.get("dropped_ungrounded"), "model": bb.get("model"), "degraded_from": bb.get("degraded_from")}
    except Exception as exc:
        db.rollback()
        report["bull_bear"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    return report


def latest_results_record(db: Session, company: Company) -> ResultsExtraction | None:
    return db.scalar(select(ResultsExtraction).where(ResultsExtraction.company_id == company.id, ResultsExtraction.status != "failed")
                     .order_by(ResultsExtraction.period_end.desc(), ResultsExtraction.id.desc()).limit(1))
