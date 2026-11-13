from __future__ import annotations

from contextlib import asynccontextmanager
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
import logging
import os
from threading import Lock
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .database import Base, DATABASE_IDENTIFIER, PROJECT_ROOT, SessionLocal, engine, get_db, upgrade_schema
from .models import (AlertEvent, AlertPreference, Announcement, Company, DailyMarketData, DocumentExtractionRun, DocumentPage,
                     FinancialObservation, IngestionRun, PortfolioHolding, SourceDocument, ManagementStatement, CommitmentAssessment,
                     ProviderFinancialObservation, ResearchDatasetSnapshot)
from .research import PARSER_VERSION, calculate_comparison, registered_pdf_path, sha256_file
from .intelligence import (ALERT_CATEGORIES, alert_event_payload, alert_preferences_payload,
                           classify_announcement, digest_bounds, evaluate_alerts, get_alert_preferences)
from .services import discover_documents, research_company, refresh_company_research, sync_announcements, sync_market
from .providers.yahoo_research import search_listed_companies, exchange_symbol, validate_symbol
from .schemas import (DocumentPagesResponse, ExtractionStatusResponse, FinancialHeadlineResponse,
                      FinancialHistoryResponse, FinancialObservationResponse)
from .seed import seed_companies
from .management import assess_commitments, list_statements, statement_payload
from .research_assistant import answer_question
from .thesis_monitor import build_thesis_drift

logger = logging.getLogger("uvicorn.error")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("FINN starting; database=%s", DATABASE_IDENTIFIER)
    Base.metadata.create_all(bind=engine)
    upgrade_schema(engine)
    with SessionLocal() as db:
        if db.scalar(select(func.count(Company.id))) == 0:
            seed_companies(db)
    yield


app = FastAPI(title="FINN Data API", version="0.2.0", description="Evidence-first investment desk data services", lifespan=lifespan)
_refresh_launch_lock = Lock()
_cors_origins = [origin.strip() for origin in os.getenv("FINN_CORS_ORIGINS", "").split(",") if origin.strip()]
if _cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=_cors_origins, allow_credentials=False, allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Accept", "Content-Type"])


def _company_payload(company: Company) -> dict:
    return {
        "id": company.id, "name": company.name, "ticker": company.ticker,
        "exchange": company.exchange, "sector": company.sector,
        "yfinance_symbol": company.yfinance_symbol, "website": company.website,
        "investor_relations_url": company.investor_relations_url,
    }


def _market_payload(row: DailyMarketData) -> dict:
    return {
        "trading_date": row.trading_date.isoformat(), "open": row.open,
        "high": row.high, "low": row.low, "close": row.close,
        "adjusted_close": row.adjusted_close, "volume": row.volume,
        "previous_close": row.previous_close, "daily_return_pct": row.daily_return_pct,
        "average_volume_20": row.average_volume_20, "relative_volume": row.relative_volume,
        "source": row.source, "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
    }


def _announcement_payload(item: Announcement) -> dict:
    materiality = classify_announcement(item)
    return {
        "id": item.id, "ticker": item.company.ticker, "company": item.company.name,
        "exchange": item.exchange, "external_announcement_id": item.external_announcement_id,
        "subject": item.subject, "details": item.details,
        "original_category": item.original_category, "category": item.normalized_category,
        "announcement_datetime": item.announcement_datetime.isoformat() if item.announcement_datetime else None,
        "attachment_url": item.attachment_url, "source_page_url": item.source_page_url,
        "source": item.source, "ingested_at": item.ingested_at.isoformat() if item.ingested_at else None,
        "assessment_status": "unassessed",
        **materiality,
    }


def _document_payload(item: SourceDocument) -> dict:
    return {
        "id": item.id, "ticker": item.company.ticker, "company": item.company.name,
        "title": item.title, "document_type": item.document_type,
        "fiscal_year": item.fiscal_year, "quarter": item.quarter,
        "document_date": item.document_date.isoformat() if item.document_date else None,
        "landing_page_url": item.landing_page_url, "download_url": item.download_url,
        "local_file_path": item.local_file_path, "source": item.source,
        "content_type": item.content_type, "file_hash": item.file_hash,
        "discovery_status": item.discovery_status,
        "discovered_at": item.discovered_at.isoformat() if item.discovered_at else None,
    }


def _get_company(db: Session, ticker: str) -> Company:
    company = db.scalar(select(Company).where(func.upper(Company.ticker) == ticker.upper()))
    if company is None:
        raise HTTPException(status_code=404, detail={"code": "company_not_found", "message": f"Unknown ticker: {ticker.upper()}"})
    return company


def request_clock() -> datetime:
    """Single overridable wall clock; production uses current Asia/Kolkata time."""
    return datetime.now(ZoneInfo("Asia/Kolkata"))


@app.get("/api/management-statements/{ticker}")
def management_statements(ticker: str, review_status: str | None = None, category: str | None = Query(None, pattern="^(financial|operational|qualitative)$"), db: Session = Depends(get_db)) -> dict:
    _get_company(db, ticker)
    items = list_statements(db, ticker)
    if review_status:
        items = [item for item in items if item["review_status"] == review_status]
    if category:
        items = [item for item in items if item["triage_category"] == category]
    if category or review_status in {"needs_review", "candidate"}:
        items.sort(key=lambda item: ({"high": 0, "medium": 1, "low": 2}[item["review_priority"]], item["transcript_date"] or "", item["id"]))
    return {"ticker": ticker.upper(), "items": items, "count": len(items)}


@app.get("/api/management-statements/item/{statement_id}")
def management_statement_detail(statement_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(ManagementStatement, statement_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Management statement not found")
    return statement_payload(db, item)


@app.post("/api/management-statements/{statement_id}/review")
def review_management_statement(statement_id: int, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    item = db.get(ManagementStatement, statement_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Management statement not found")
    action = payload.get("action")
    reviewer = str(payload.get("reviewer", "")).strip()
    if action not in {"approved", "rejected", "needs_review"} or not reviewer:
        raise HTTPException(status_code=422, detail="Provide action approved/rejected/needs_review and reviewer")
    allowed = {"statement_class", "topic", "metric_name", "metric_definition", "target_low", "target_high", "unit", "currency", "target_period", "baseline_period", "assumptions", "lifecycle", "related_statement_id"}
    for key, value in payload.get("corrections", {}).items():
        if key not in allowed:
            raise HTTPException(status_code=422, detail=f"Field cannot be corrected: {key}")
        setattr(item, key, value)
    item.review_status = action
    item.reviewer = reviewer[:160]
    item.reviewed_at = datetime.now(timezone.utc)
    item.review_note = str(payload.get("note", ""))[:4000] or None
    db.commit(); db.refresh(item)
    return statement_payload(db, item)


@app.get("/api/commitments/{ticker}/summary")
def commitment_summary(ticker: str, db: Session = Depends(get_db)) -> dict:
    _get_company(db, ticker)
    items = list_statements(db, ticker)
    approved = [item for item in items if item["review_status"] == "approved"]
    assessed = [item for item in approved if item["assessment"] is not None]
    return {"ticker": ticker.upper(), "candidate_count": len(items), "approved_count": len(approved),
            "needs_review_count": sum(item["review_status"] in {"needs_review", "candidate"} for item in items),
            "rejected_count": sum(item["review_status"] == "rejected" for item in items),
            "awaiting_outcome_count": sum(item["assessment"] and item["assessment"]["status"] in {"awaiting_outcome", "in_progress"} for item in approved),
            "assessed_count": len(assessed), "items": approved}


@app.get("/api/commitments/{ticker}")
def commitments(ticker: str, db: Session = Depends(get_db)) -> dict:
    _get_company(db, ticker)
    items = list_statements(db, ticker, approved_only=True)
    return {"ticker": ticker.upper(), "items": items, "count": len(items)}


@app.post("/api/commitments/{ticker}/assess")
def assess_company_commitments(ticker: str, db: Session = Depends(get_db)) -> dict:
    return assess_commitments(db, ticker)


@app.get("/api/company-profile/{ticker}")
def company_profile(ticker: str, db: Session = Depends(get_db)) -> dict:
    company = _get_company(db, ticker)
    docs = db.scalars(select(SourceDocument).where(SourceDocument.company_id == company.id, SourceDocument.document_type == "annual_report").order_by(SourceDocument.fiscal_year.desc())).all()
    if not docs:
        cached = db.scalar(select(ResearchDatasetSnapshot).where(ResearchDatasetSnapshot.company_id == company.id, ResearchDatasetSnapshot.dataset == "profile"))
        profile = (cached.details or {}) if cached and cached.status in {"available", "partially_available"} else {}
        if profile.get("business"):
            return {"ticker": company.ticker, "status": "provider_available", "message": "Yahoo Finance business description; not an original company filing.", "facts": [{"text": profile["business"], "provider": profile.get("provider"), "retrieved_at": cached.fetched_at.isoformat() if cached.fetched_at else None}], "segments": [], "provider": profile.get("provider"), "website": profile.get("website")}
        return {"ticker": company.ticker, "status": "unavailable", "message": "No indexed official profile is available. Provider profile coverage will be checked on demand.", "facts": [], "segments": []}
    document = docs[0]
    pages = {p.page_number: p for p in db.scalars(select(DocumentPage).where(DocumentPage.document_id == document.id, DocumentPage.page_number.in_([5, 6, 7]))).all()}
    facts = []
    page5 = pages.get(5)
    if page5:
        text5 = " ".join(page5.extracted_text.split())
        match = re.search(r"Tata Power operates across the power value chain.*?solar cells and modules\.", text5, re.I)
        if match:
            facts.append({"text": match.group(0), "document_id": document.id, "document_title": document.title, "page_number": 5, "source_url": f"/api/documents/{document.id}/file#page=5"})
    segment_names = ["Renewables", "Transmission and Distribution", "Thermal and Hydro", "New-age Energy Solutions"]
    page6 = pages.get(6)
    segments = []
    if page6:
        normalized = " ".join(page6.extracted_text.split()).lower()
        segments = [{"name": name, "document_id": document.id, "document_title": document.title, "page_number": 6, "source_url": f"/api/documents/{document.id}/file#page=6"} for name in segment_names if name.lower() in normalized]
    if not facts:
        cached = db.scalar(select(ResearchDatasetSnapshot).where(ResearchDatasetSnapshot.company_id == company.id, ResearchDatasetSnapshot.dataset == "profile"))
        profile = (cached.details or {}) if cached and cached.status in {"available", "partially_available"} else {}
        if profile.get("business"):
            return {"ticker": company.ticker, "status": "provider_available", "message": "Yahoo Finance business description; not an original company filing.", "facts": [{"text": profile["business"], "provider": profile.get("provider"), "retrieved_at": cached.fetched_at.isoformat() if cached.fetched_at else None}], "segments": [], "provider": profile.get("provider"), "website": profile.get("website")}
    return {"ticker": company.ticker, "status": "available" if facts else "unavailable", "message": None if facts else "The annual report is indexed, but a business profile passage was not extracted.", "facts": facts, "segments": segments, "document_id": document.id, "document_title": document.title}


@app.get("/api/research-coverage/{ticker}")
def research_coverage(ticker: str, db: Session = Depends(get_db)) -> dict:
    """Report persisted, issuer-specific source coverage without treating missing data as an error."""
    company = _get_company(db, ticker)
    docs = db.scalars(select(SourceDocument).where(SourceDocument.company_id == company.id)).all()
    obs = db.scalars(select(FinancialObservation).where(FinancialObservation.company_id == company.id)).all()
    pages_by_doc = {d.id: db.scalar(select(func.count(DocumentPage.id)).where(DocumentPage.document_id == d.id)) or 0 for d in docs}
    validated = [o for o in obs if o.extraction_status == "validated"]
    review = [o for o in obs if o.extraction_status == "needs_review"]
    provider_configured = company.ticker.split(":")[-1] in {"TATAPOWER", "BEL", "INFY"}

    def source_state(items, configured=True):
        if items:
            failed_docs = any(d.discovery_status == "provider_error" for d in items)
            if failed_docs and any(d.local_file_path for d in items): return "partially_available"
            if failed_docs: return "provider_error"
            item_obs = [o for o in obs if o.document_id in {d.id for d in items}]
            if any(pages_by_doc.get(d.id, 0) for d in items):
                if any(o.extraction_status == "validated" for o in item_obs): return "available"
                if any(o.extraction_status == "needs_review" for o in item_obs): return "needs_review"
                return "indexed_not_extracted"
            if any(d.local_file_path for d in items): return "documents_registered"
            return "documents_registered"
        return "not_connected" if configured else "not_configured"

    annual = [d for d in docs if d.document_type == "annual_report"]
    quarterly = [d for d in docs if d.document_type == "quarterly_results"]
    transcripts = [d for d in docs if d.document_type == "earnings_call_transcript"]
    latest_runs = db.scalars(select(IngestionRun).order_by(IngestionRun.started_at.desc()).limit(50)).all()
    def provider_status(provider):
        run = next((r for r in latest_runs if r.provider == provider), None)
        return run.status if run else ("not_connected" if provider else "not_configured")
    market_count = db.scalar(select(func.count(DailyMarketData.id)).where(DailyMarketData.company_id == company.id)) or 0
    ann_count = db.scalar(select(func.count(Announcement.id)).where(Announcement.company_id == company.id)) or 0
    snapshots = {item.dataset: item for item in db.scalars(select(ResearchDatasetSnapshot).where(ResearchDatasetSnapshot.company_id == company.id)).all()}
    market = "available" if market_count else ("provider_error" if snapshots.get("market") and snapshots["market"].status == "provider_error" else "not_connected")
    announcements = "available" if ann_count else (snapshots["announcements"].status if snapshots.get("announcements") else ("provider_error" if provider_status("nse_announcements") == "failed" else "not_connected"))
    profile_result = company_profile(company.ticker, db)
    profile = {"available": "available", "provider_available": "partially_available"}.get(profile_result.get("status"))
    if profile is None:
        profile = "provider_error" if snapshots.get("profile") and snapshots["profile"].status == "provider_error" else "indexed_not_extracted" if annual and any(pages_by_doc.get(d.id, 0) for d in annual) else "not_connected" if provider_configured else "not_configured"
    valid_mgmt = db.scalar(select(func.count(ManagementStatement.id)).where(ManagementStatement.company_id == company.id, ManagementStatement.review_status == "approved")) or 0
    mgmt_count = db.scalar(select(func.count(ManagementStatement.id)).where(ManagementStatement.company_id == company.id)) or 0
    categories = {
        "eod_market_data": market, "exchange_announcements": announcements,
        "official_business_profile": profile, "annual_reports": source_state(annual, provider_configured),
        "quarterly_results": source_state(quarterly, provider_configured),
        "earnings_call_transcripts": source_state(transcripts, company.ticker == "TATAPOWER"),
        "indexed_document_pages": "available" if sum(pages_by_doc.values()) else ("documents_registered" if docs else "not_connected" if provider_configured else "not_configured"),
        "validated_financial_observations": "available" if validated else "needs_review" if review else "indexed_not_extracted" if any(pages_by_doc.values()) else "not_connected" if provider_configured else "not_configured",
        "provider_financial_statements": snapshots["financials"].status if snapshots.get("financials") else "not_connected",
        "management_statements_and_commitments": "available" if valid_mgmt else "needs_review" if mgmt_count else "not_connected" if transcripts else "not_configured",
    }
    return {"ticker": company.ticker, "company": company.name, "states": categories,
            "counts": {"market_observations": market_count, "announcements": ann_count, "annual_reports": len(annual), "quarterly_results": len(quarterly), "transcripts": len(transcripts), "indexed_pages": sum(pages_by_doc.values()), "financial_observations": len(obs), "validated_financial_observations": len(validated), "financial_observations_needing_review": len(review), "provider_financial_observations": db.scalar(select(func.count(ProviderFinancialObservation.id)).where(ProviderFinancialObservation.company_id == company.id)) or 0, "management_statements": mgmt_count, "approved_management_statements": valid_mgmt},
            "providers": {"market": snapshots["market"].status if snapshots.get("market") else provider_status("yahoo_finance"), "announcements": snapshots["announcements"].status if snapshots.get("announcements") else provider_status("nse_announcements"), "documents": provider_status({"TATAPOWER":"tata_power_ir", "BEL":"bel_ir", "INFY":"infosys_ir"}.get(company.ticker.split(":")[-1], "")) if provider_configured else "not_configured"}}


@app.get("/api/health")
def health(db: Session = Depends(get_db)) -> dict:
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected", "database_identifier": DATABASE_IDENTIFIER, "checked_at": datetime.now(timezone.utc).isoformat()}
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail={"code": "database_unavailable", "message": "Database is unavailable"}) from exc


@app.get("/api/data-status")
def data_status(db: Session = Depends(get_db)) -> dict:
    providers = ["yahoo_finance", "nse_announcements", "tata_power_ir"]
    source_status = {}
    for provider in providers:
        run = db.scalar(select(IngestionRun).where(IngestionRun.provider == provider).order_by(IngestionRun.started_at.desc()).limit(1))
        source_status[provider] = None if run is None else {
            "status": run.status, "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "records_attempted": run.records_attempted, "records_inserted": run.records_inserted,
            "records_updated": run.records_updated, "records_failed": run.records_failed,
            "error_summary": run.error_summary,
        }
    latest_market = db.scalar(select(func.max(DailyMarketData.trading_date)).where(DailyMarketData.company_id.in_(select(PortfolioHolding.company_id).where(PortfolioHolding.is_active.is_(True)))))
    latest_research_market = db.scalar(select(func.max(DailyMarketData.trading_date)).where(DailyMarketData.company_id.in_(select(Company.id).where(~Company.holding.has(is_active=True), Company.ticker != "NIFTY50"))))
    latest_market_observation = db.scalar(select(func.max(DailyMarketData.trading_date)))
    docs = db.scalar(select(func.count(SourceDocument.id))) or 0
    companies_count = db.scalar(select(func.count(Company.id))) or 0
    market_count = db.scalar(select(func.count(DailyMarketData.id))) or 0
    portfolio_count = db.scalar(select(func.count(PortfolioHolding.id)).where(PortfolioHolding.is_active.is_(True))) or 0
    research_count = db.scalar(select(func.count(Company.id)).where(~Company.holding.has(is_active=True), Company.ticker != "NIFTY50")) or 0
    announcements_count = db.scalar(select(func.count(Announcement.id))) or 0
    last_announcement = db.scalar(select(func.max(Announcement.ingested_at)))
    financial_count = db.scalar(select(func.count(FinancialObservation.id))) or 0
    last_financial = db.scalar(select(func.max(FinancialObservation.created_at)))
    latest_refresh = db.scalar(select(IngestionRun).where(IngestionRun.provider == "data_refresh").order_by(IngestionRun.started_at.desc()).limit(1))
    today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    freshness = {
        "market_stale": latest_market is None or (today_ist - latest_market).days > 5,
        "announcements_stale": last_announcement is None or (datetime.now(timezone.utc) - (last_announcement.replace(tzinfo=timezone.utc) if last_announcement.tzinfo is None else last_announcement)).total_seconds() > 48 * 3600,
    }
    return {
        "mode": "live", "database_status": "connected", "database_identifier": DATABASE_IDENTIFIER,
        "company_count": companies_count, "market_observation_count": market_count,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "portfolio_company_count": portfolio_count, "research_universe_company_count": research_count,
        "latest_market_date": latest_market.isoformat() if latest_market else None,
        "latest_research_market_date": latest_research_market.isoformat() if latest_research_market else None,
        "latest_market_observation_date": latest_market_observation.isoformat() if latest_market_observation else None,
        "latest_announcement_ingestion": last_announcement.isoformat() if last_announcement else None,
        "latest_financial_extraction": last_financial.isoformat() if last_financial else None,
        "announcement_count": announcements_count, "document_count": docs,
        "financial_observation_count": financial_count,
        "latest_document_sync": source_status.get("tata_power_ir"),
        "latest_refresh": {"id": latest_refresh.id, "status": latest_refresh.status, "started_at": latest_refresh.started_at.isoformat() if latest_refresh.started_at else None, "completed_at": latest_refresh.completed_at.isoformat() if latest_refresh.completed_at else None, "details": latest_refresh.metadata_json} if latest_refresh else None,
        "freshness": freshness, "sources": source_status,
    }


def _run_refresh_job(job_id: int) -> None:
    with SessionLocal() as db:
        job = db.get(IngestionRun, job_id)
        if job is None or job.status != "running":
            return
        result = {}
        errors = []
        for name, task in (("market", lambda: sync_market(db)), ("announcements", lambda: sync_announcements(db, lookback_days=30)), ("documents", lambda: discover_documents(db, "TATAPOWER", download=True))):
            try:
                result[name] = task()
                if name in {"market", "announcements"} and result[name].get("status") in {"success", "partial"}:
                    result["alerts"] = evaluate_alerts(db)
                if result[name].get("status") not in {"success", "ok"}:
                    errors.append(f"{name}: {result[name].get('status')} {result[name].get('errors', result[name].get('error_summary', ''))}")
                job.metadata_json = dict(result)
                db.commit()
            except Exception as exc:
                db.rollback()
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                result[name] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
                job.metadata_json = dict(result)
                db.commit()
        job.metadata_json = result
        job.error_summary = "\n".join(errors)[:12000] or None
        job.completed_at = datetime.now(timezone.utc)
        job.status = "partial" if errors else "success"
        job.records_attempted = sum(int(item.get("attempted", item.get("records", 0)) or 0) for item in result.values())
        job.records_failed = sum(int(item.get("failed", 0) or 0) for item in result.values())
        db.commit()


@app.post("/api/refresh", status_code=202)
def start_data_refresh(background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> dict:
    with _refresh_launch_lock:
        active = db.scalar(select(IngestionRun).where(IngestionRun.provider == "data_refresh", IngestionRun.status == "running").order_by(IngestionRun.started_at.desc()).limit(1))
        if active:
            return {"job_id": active.id, "status": active.status, "started_at": active.started_at.isoformat(), "already_running": True}
        job = IngestionRun(provider="data_refresh", started_at=datetime.now(timezone.utc), status="running", metadata_json={})
        db.add(job); db.commit(); db.refresh(job)
        background_tasks.add_task(_run_refresh_job, job.id)
        return {"job_id": job.id, "status": job.status, "started_at": job.started_at.isoformat(), "already_running": False}


@app.get("/api/refresh/{job_id}")
def data_refresh_status(job_id: int, db: Session = Depends(get_db)) -> dict:
    job = db.get(IngestionRun, job_id)
    if job is None or job.provider != "data_refresh":
        raise HTTPException(status_code=404, detail={"code": "refresh_not_found", "message": "Refresh job not found"})
    return {"job_id": job.id, "status": job.status, "started_at": job.started_at.isoformat() if job.started_at else None, "completed_at": job.completed_at.isoformat() if job.completed_at else None, "details": job.metadata_json or {}, "error_summary": job.error_summary}


@app.get("/api/companies")
def companies(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(Company).join(PortfolioHolding).where(PortfolioHolding.is_active.is_(True)).order_by(Company.ticker)).all()
    return [_company_payload(item) for item in rows]


@app.get("/api/companies/{ticker}")
def company_detail(ticker: str, db: Session = Depends(get_db)) -> dict:
    company = _get_company(db, ticker)
    payload = _company_payload(company)
    payload["market_summary"] = _market_summary(db, company)
    payload["document_count"] = db.scalar(select(func.count(SourceDocument.id)).where(SourceDocument.company_id == company.id)) or 0
    return payload


@app.get("/api/portfolio")
def portfolio(db: Session = Depends(get_db)) -> dict:
    companies = db.scalars(select(Company).join(PortfolioHolding).where(PortfolioHolding.is_active.is_(True)).order_by(Company.ticker)).all()
    return {"holding_count": len(companies), "holdings": [_company_payload(company) for company in companies]}


def _market_summary(db: Session, company: Company) -> dict:
    rows = db.scalars(select(DailyMarketData).where(DailyMarketData.company_id == company.id).order_by(DailyMarketData.trading_date.desc()).limit(21)).all()
    if not rows:
        return {"ticker": company.ticker, "status": "unavailable", "message": "No market observations are stored yet."}
    latest = rows[0]
    return {
        "ticker": company.ticker, "status": "available", "latest_close": latest.close,
        "latest_trading_date": latest.trading_date.isoformat(), "previous_close": latest.previous_close,
        "daily_return_pct": latest.daily_return_pct, "volume": latest.volume,
        "average_volume_20": latest.average_volume_20, "relative_volume": latest.relative_volume,
        "source": latest.source, "fetched_at": latest.fetched_at.isoformat() if latest.fetched_at else None,
    }


@app.get("/api/market/{ticker}")
def market_history(
    ticker: str,
    period: str = Query("6mo", pattern="^(1mo|3mo|6mo|1y|max)$"),
    start_date: date | None = None,
    end_date: date | None = None,
    db: Session = Depends(get_db),
) -> dict:
    company = _get_company(db, ticker)
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=422, detail={"code": "invalid_date_range", "message": "start_date must be on or before end_date"})
    if end_date is None:
        end_date = date.today()
    if start_date is None and period != "max":
        days = {"1mo": 31, "3mo": 93, "6mo": 186, "1y": 366}[period]
        start_date = end_date - timedelta(days=days)
    query = select(DailyMarketData).where(DailyMarketData.company_id == company.id)
    if start_date:
        query = query.where(DailyMarketData.trading_date >= start_date)
    if end_date:
        query = query.where(DailyMarketData.trading_date <= end_date)
    rows = db.scalars(query.order_by(DailyMarketData.trading_date)).all()
    return {"ticker": company.ticker, "symbol": company.yfinance_symbol, "count": len(rows), "period": period, "source": "Yahoo Finance via yfinance", "data": [_market_payload(row) for row in rows]}


@app.get("/api/market/{ticker}/summary")
def market_summary(ticker: str, db: Session = Depends(get_db)) -> dict:
    return _market_summary(db, _get_company(db, ticker))


@app.get("/api/announcements")
def announcements(
    ticker: str | None = None,
    lookback_days: int = Query(30, ge=1, le=3650),
    exchange: str | None = None,
    category: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=100000),
    db: Session = Depends(get_db),
) -> dict:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=lookback_days)
    query = select(Announcement).join(Company).where((Announcement.announcement_datetime >= cutoff) | (Announcement.announcement_datetime.is_(None)))
    if ticker:
        query = query.where(func.upper(Company.ticker) == ticker.upper())
    if exchange:
        query = query.where(func.upper(Announcement.exchange) == exchange.upper())
    if category:
        query = query.where(Announcement.normalized_category == category)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(query.order_by(Announcement.announcement_datetime.desc().nullslast(), Announcement.id.desc()).offset(offset).limit(limit)).all()
    return {"count": len(rows), "total": total, "limit": limit, "offset": offset, "lookback_days": lookback_days, "items": [_announcement_payload(row) for row in rows]}


def _results_context(db: Session, item: Announcement, assessment: dict) -> dict | None:
    if assessment["category"] != "results" or item.announcement_datetime is None:
        return None
    announcement_day = item.announcement_datetime.date()
    observations = db.scalars(select(FinancialObservation).where(
        FinancialObservation.company_id == item.company_id,
        FinancialObservation.statement_basis == "consolidated",
        FinancialObservation.period_type == "quarterly",
        FinancialObservation.canonical_metric.in_(["revenue", "pbt", "pat"]),
        FinancialObservation.extraction_status == "validated",
        FinancialObservation.period_end <= announcement_day,
    ).order_by(FinancialObservation.period_end.desc(), FinancialObservation.id.desc())).all()
    ends = sorted({o.period_end for o in observations if (announcement_day - o.period_end).days <= 90}, reverse=True)
    if not ends:
        return {"status": "extraction_unavailable", "message": "Results filing detected. Financial extraction is not yet available."}
    period_end = ends[0]
    company = db.get(Company, item.company_id)
    values = {}
    for metric in ("revenue", "pbt", "pat"):
        row = next((o for o in observations if o.period_end == period_end and o.canonical_metric == metric), None)
        if row is None:
            continue
        values[metric] = {
            "value_in_crore": float(row.normalised_value_in_crore),
            "fiscal_year": row.fiscal_year, "fiscal_quarter": row.fiscal_quarter,
            "period_end": row.period_end.isoformat(), "observation_id": row.id,
            "page_number": row.page_number, "document_id": row.document_id,
            "evidence_url": f"/api/financial-observations/{row.id}",
            "qoq": _quarter_comparison(db, row, company, 0),
            "yoy": _quarter_comparison(db, row, company, 1),
        }
    return {"status": "available" if values else "extraction_unavailable", "statement_basis": "consolidated", "unit": "INR crore", "period_end": period_end.isoformat(), "metrics": values, "research_url": f"/#research?ticker={company.ticker}"}


@app.get("/api/digest")
def portfolio_digest(
    window: str = Query("24h", pattern="^(24h|7d|30d)$"),
    from_date: date | None = None,
    to_date: date | None = None,
    priority: str | None = Query(None, pattern="^(high|medium|routine|unclassified)$"),
    category: str | None = None,
    limit: int = Query(500, ge=1, le=1000),
    now: datetime = Depends(request_clock),
    db: Session = Depends(get_db),
) -> dict:
    try:
        start, end = digest_bounds(window, now=now, from_date=from_date, to_date=to_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    rows = db.scalars(select(Announcement).join(Company).join(PortfolioHolding).where(PortfolioHolding.is_active.is_(True))).all()
    undated = 0
    selected = []
    for item in rows:
        stamp = item.announcement_datetime
        if stamp is None:
            undated += 1
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        else:
            stamp = stamp.astimezone(ZoneInfo("Asia/Kolkata"))
        if start <= stamp < end:
            assessment = classify_announcement(item)
            if priority and assessment["materiality"] != priority: continue
            if category and assessment["category"] != category: continue
            selected.append((stamp, item, assessment))
    selected.sort(key=lambda x: x[0], reverse=True)
    total = len(selected)
    counts = {level: sum(1 for _, _, a in selected if a["materiality"] == level) for level in ("high", "medium", "routine", "unclassified")}
    selected = selected[:limit]
    items = []
    for stamp, item, assessment in selected:
        payload = _announcement_payload(item)
        payload["announcement_datetime"] = stamp.isoformat()
        payload["results_context"] = _results_context(db, item, assessment)
        items.append(payload)
    return {
        "window": window, "from": start.isoformat(), "to_exclusive": end.isoformat(),
        "timezone": "Asia/Kolkata", "count": len(items), "total": total,
        "counts": counts, "undated_portfolio_announcements": undated,
        "items": items,
    }


@app.get("/api/alerts/settings")
def read_alert_settings(db: Session = Depends(get_db)) -> dict:
    return alert_preferences_payload(get_alert_preferences(db))


@app.patch("/api/alerts/settings")
def update_alert_settings(payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    prefs = get_alert_preferences(db)
    categories = payload.get("announcement_categories", prefs.announcement_categories or {})
    if not isinstance(categories, dict) or any(k not in ALERT_CATEGORIES or not isinstance(v, bool) for k, v in categories.items()):
        raise HTTPException(status_code=422, detail="announcement_categories must map supported category names to booleans")
    try:
        up = float(payload.get("positive_price_move_pct", prefs.positive_price_move_pct))
        down = float(payload.get("negative_price_move_pct", prefs.negative_price_move_pct))
        volume = float(payload.get("unusual_volume_multiple", prefs.unusual_volume_multiple))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Alert thresholds must be numeric") from exc
    if not 0 < up <= 50 or not 0 < down <= 50 or not 1 <= volume <= 20:
        raise HTTPException(status_code=422, detail="Price thresholds must be 0–50%; volume multiple must be 1–20×")
    prefs.announcement_categories = {key: bool(categories.get(key, True)) for key in ALERT_CATEGORIES}
    prefs.positive_price_move_pct = up; prefs.negative_price_move_pct = down; prefs.unusual_volume_multiple = volume
    db.commit(); db.refresh(prefs)
    return alert_preferences_payload(prefs)


@app.get("/api/alerts")
def alert_history(limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(AlertEvent).order_by(AlertEvent.triggered_at.desc(), AlertEvent.id.desc()).limit(limit)).all()
    return {"count": len(rows), "items": [alert_event_payload(row) for row in rows]}


@app.post("/api/alerts/evaluate")
def run_alert_evaluation(now: datetime = Depends(request_clock), db: Session = Depends(get_db)) -> dict:
    return evaluate_alerts(db, now=now)


@app.get("/api/research-universe")
def research_universe(db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(Company).where(~Company.holding.has(is_active=True), Company.ticker != "NIFTY50").order_by(Company.ticker)).all()
    return {"count": len(rows), "items": [{**_company_payload(c), "portfolio_member": False, "market_summary": _market_summary(db, c), "document_count": db.scalar(select(func.count(SourceDocument.id)).where(SourceDocument.company_id == c.id)) or 0} for c in rows]}


@app.get("/api/company-search")
def company_search(q: str = Query(..., min_length=2, max_length=100)) -> dict:
    try:
        return {"query": q, "items": search_listed_companies(q)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"code": "search_provider_error", "message": f"Company search is unavailable ({type(exc).__name__})."}) from exc


@app.post("/api/research-company")
def register_research_company(payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    exchange = str(payload.get("exchange", "")).upper()
    ticker = str(payload.get("ticker", "")).strip().upper()
    name = str(payload.get("name", "")).strip()
    try:
        symbol = exchange_symbol(exchange, ticker)
        identity = validate_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "ticker_unavailable", "message": str(exc)}) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"code": "provider_unavailable", "message": f"Yahoo Finance validation failed ({type(exc).__name__})."}) from exc
    resolved_name = identity.get("name") or name
    if name and name.casefold() != ticker.casefold() and resolved_name and name.casefold() not in resolved_name.casefold() and resolved_name.casefold() not in name.casefold():
        raise HTTPException(status_code=422, detail={"code": "identity_mismatch", "message": "The provider name does not match the selected search result."})
    storage_ticker = ticker if exchange == "NSE" else f"BSE:{ticker}"
    company = db.scalar(select(Company).where(Company.ticker == storage_ticker)) or db.scalar(select(Company).where(Company.yfinance_symbol == symbol))
    if company is None:
        company = Company(name=resolved_name or ticker, ticker=storage_ticker, exchange=exchange, sector=identity.get("sector"), yfinance_symbol=symbol, website=identity.get("website"))
        db.add(company); db.commit(); db.refresh(company)
    elif company.yfinance_symbol != symbol:
        raise HTTPException(status_code=409, detail={"code": "symbol_conflict", "message": "This exchange symbol is already registered to a different provider symbol."})
    refresh = refresh_company_research(db, company)
    return {**_company_payload(company), "provider_identity": resolved_name, "refresh": refresh}


@app.post("/api/research/{ticker}/refresh")
def refresh_research_endpoint(ticker: str, db: Session = Depends(get_db)) -> dict:
    return refresh_company_research(db, _get_company(db, ticker))


@app.get("/api/provider-financials/{ticker}")
def provider_financials(ticker: str, statement_type: str | None = Query(None, pattern="^(income|balance_sheet|cash_flow)$"), period_type: str | None = Query(None, pattern="^(annual|quarterly)$"), db: Session = Depends(get_db)) -> dict:
    company = _get_company(db, ticker)
    query = select(ProviderFinancialObservation).where(ProviderFinancialObservation.company_id == company.id)
    if statement_type:
        query = query.where(ProviderFinancialObservation.statement_type == statement_type)
    if period_type:
        query = query.where(ProviderFinancialObservation.period_type == period_type)
    rows = db.scalars(query.order_by(ProviderFinancialObservation.period_end, ProviderFinancialObservation.id)).all()
    snapshots = db.scalars(select(ResearchDatasetSnapshot).where(ResearchDatasetSnapshot.company_id == company.id)).all()
    statuses = {row.dataset: {"status": row.status, "fetched_at": row.fetched_at.isoformat(), "error": row.error} for row in snapshots}
    return {"ticker": company.ticker, "company": company.name, "provider": "Yahoo Finance via yfinance", "basis": "provider reported; consolidated or standalone basis is not guaranteed", "data_status": statuses, "items": [{"statement_type": row.statement_type, "period_type": row.period_type, "period_end": row.period_end.isoformat(), "fiscal_year": row.fiscal_year, "fiscal_quarter": row.fiscal_quarter, "canonical_metric": row.canonical_metric, "reported_label": row.reported_label, "reported_value": float(row.reported_value), "currency": row.currency, "reported_unit": row.reported_unit, "normalised_value_in_crore": float(row.normalised_value_in_crore) if row.normalised_value_in_crore is not None else None, "provider": row.provider, "source_url": row.source_url, "fetched_at": row.fetched_at.isoformat()} for row in rows]}


@app.post("/api/research-universe/{ticker}")
def add_research_company(ticker: str, db: Session = Depends(get_db)) -> dict:
    try:
        result = research_company(db, ticker)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "ticker_unavailable", "message": str(exc)}) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"code": "provider_unavailable", "message": f"Could not retrieve research data for {ticker.upper()}: {type(exc).__name__}"}) from exc
    company = db.scalar(select(Company).where(Company.ticker == ticker.upper()))
    if company:
        result["financial_research"] = refresh_company_research(db, company)
    return result


@app.get("/api/announcements/{announcement_id}")
def announcement_detail(announcement_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(Announcement, announcement_id)
    if item is None:
        raise HTTPException(status_code=404, detail={"code": "announcement_not_found", "message": "Announcement not found"})
    return _announcement_payload(item)


@app.get("/api/documents")
def documents(
    ticker: str | None = None,
    document_type: str | None = None,
    fiscal_year: str | None = None,
    quarter: str | None = None,
    db: Session = Depends(get_db),
) -> dict:
    query = select(SourceDocument).join(Company)
    if ticker:
        query = query.where(func.upper(Company.ticker) == ticker.upper())
    if document_type:
        query = query.where(SourceDocument.document_type == document_type)
    if fiscal_year:
        query = query.where(SourceDocument.fiscal_year == fiscal_year)
    if quarter:
        query = query.where(SourceDocument.quarter == quarter)
    rows = db.scalars(query.order_by(SourceDocument.fiscal_year.desc(), SourceDocument.quarter.desc(), SourceDocument.id.desc())).all()
    return {"count": len(rows), "items": [_document_payload(row) for row in rows]}


@app.get("/api/documents/{document_id}")
def document_detail(document_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(SourceDocument, document_id)
    if item is None:
        raise HTTPException(status_code=404, detail={"code": "document_not_found", "message": "Document not found"})
    return _document_payload(item)


def _financial_rows(db: Session, company: Company, metric: str, basis: str, period_type: str) -> list[FinancialObservation]:
    rows = db.scalars(select(FinancialObservation).where(
        FinancialObservation.company_id == company.id,
        FinancialObservation.canonical_metric == metric,
        FinancialObservation.statement_basis == basis,
        FinancialObservation.period_type == period_type,
    ).order_by(FinancialObservation.period_end, FinancialObservation.id)).all()
    grouped: dict[date, list[FinancialObservation]] = {}
    for row in rows:
        grouped.setdefault(row.period_end, []).append(row)
    selected = []
    for period_end, items in grouped.items():
        if period_type == "annual":
            primary = [row for row in items if row.fiscal_year == row.document.fiscal_year and row.document.document_type == "annual_report"]
        else:
            primary = [row for row in items if row.fiscal_year == row.document.fiscal_year and row.fiscal_quarter == row.document.quarter]
        candidates = primary or items
        validated = [row for row in candidates if row.extraction_status == "validated"]
        eligible = [row for row in candidates if row.extraction_status != "rejected"]
        selected.append((validated or eligible or candidates)[-1])
    return selected


def _financial_observation_payload(item: FinancialObservation) -> dict:
    document = item.document
    return {
        "id": item.id, "metric": item.canonical_metric,
        "value": float(item.numeric_value), "reported_value_raw": item.reported_value_raw,
        "reported_unit": item.reported_unit,
        "normalised_value_in_crore": float(item.normalised_value_in_crore) if item.normalised_value_in_crore is not None else None,
        "currency": item.currency, "fiscal_year": item.fiscal_year, "fiscal_quarter": item.fiscal_quarter,
        "period_type": item.period_type, "period_start": item.period_start, "period_end": item.period_end,
        "statement_basis": item.statement_basis, "extraction_status": item.extraction_status,
        "reviewer": item.reviewer, "reviewed_at": item.reviewed_at, "review_note": item.review_note,
        "document_id": item.document_id, "document_title": document.title,
        "document_type": document.document_type, "page_number": item.page_number,
        "row_label": item.row_label, "column_header": item.column_header,
        "source_quote": item.source_quote, "local_pdf_url": f"/api/documents/{document.id}/file#page={item.page_number}",
    }


def _comparison_for(db: Session, current: FinancialObservation, company: Company, metric: str, delta_years: int) -> dict:
    target_year = current.period_end.year - delta_years
    try:
        target_end = current.period_end.replace(year=target_year)
    except ValueError:
        target_end = current.period_end.replace(year=target_year, day=28)
    prior_rows = db.scalars(select(FinancialObservation).where(
        FinancialObservation.company_id == company.id,
        FinancialObservation.canonical_metric == metric,
        FinancialObservation.statement_basis == current.statement_basis,
        FinancialObservation.period_type == current.period_type,
        FinancialObservation.period_end == target_end,
    ).order_by(FinancialObservation.id.desc())).all()
    prior = next((row for row in prior_rows if row.extraction_status == "validated"), None)
    return calculate_comparison(current, prior, "qoq" if delta_years == 0 else "yoy") if delta_years else {"status": "unavailable", "reason": "Quarter-over-quarter comparison requires the immediately prior quarter."}


def _quarter_comparison(db: Session, current: FinancialObservation, company: Company, years: int) -> dict:
    if years == 0:
        if current.period_type == "annual":
            return {"status": "unavailable", "reason": "Quarter-over-quarter comparison does not apply to a full fiscal-year result."}
        prior_end = current.period_end
        month = prior_end.month - 3
        year = prior_end.year
        if month <= 0:
            month += 12
            year -= 1
        target_end = date(year, month, monthrange(year, month)[1])
        kind = "qoq"
    else:
        try:
            target_end = current.period_end.replace(year=current.period_end.year - 1)
        except ValueError:
            target_end = current.period_end.replace(year=current.period_end.year - 1, day=28)
        kind = "yoy"
    prior_rows = db.scalars(select(FinancialObservation).where(
        FinancialObservation.company_id == company.id,
        FinancialObservation.canonical_metric == current.canonical_metric,
        FinancialObservation.statement_basis == current.statement_basis,
        FinancialObservation.period_type == current.period_type,
        FinancialObservation.period_end == target_end,
    ).order_by(FinancialObservation.id.desc())).all()
    prior = next((row for row in prior_rows if row.extraction_status == "validated"), None)
    return calculate_comparison(current, prior, kind)


@app.get("/api/financials/{ticker}/headline", response_model=FinancialHeadlineResponse)
def financial_headline(
    ticker: str,
    statement_basis: str = Query("consolidated", pattern="^(consolidated|standalone)$"),
    period_type: str = Query("quarterly", pattern="^(quarterly|annual)$"),
    fiscal_year: str | None = None,
    fiscal_quarter: str | None = Query(None, pattern="^Q[1-4]$"),
    db: Session = Depends(get_db),
):
    company = _get_company(db, ticker)
    query = select(FinancialObservation).where(
        FinancialObservation.company_id == company.id,
        FinancialObservation.statement_basis == statement_basis,
        FinancialObservation.period_type == period_type,
    )
    if fiscal_year:
        query = query.where(FinancialObservation.fiscal_year == fiscal_year)
    if fiscal_quarter:
        query = query.where(FinancialObservation.fiscal_quarter == fiscal_quarter)
    all_rows = db.scalars(query.order_by(FinancialObservation.period_end.desc())).all()
    latest_end = all_rows[0].period_end if all_rows else None
    metrics = []
    for metric in ("revenue", "ebitda", "pbt", "pat"):
        candidates = [row for row in all_rows if row.canonical_metric == metric and row.period_end == latest_end]
        primary = [row for row in candidates if row.fiscal_year == row.document.fiscal_year and ((period_type == "annual" and row.document.document_type == "annual_report") or (period_type == "quarterly" and row.fiscal_quarter == row.document.quarter))]
        eligible = primary or candidates
        validated_candidates = [row for row in eligible if row.extraction_status == "validated"]
        non_rejected = [row for row in eligible if row.extraction_status != "rejected"]
        observation = (validated_candidates or non_rejected or eligible)[0] if eligible else None
        if observation is None:
            metrics.append({"metric": metric, "status": "not_separately_disclosed" if metric == "ebitda" else "unavailable", "message": "Not separately disclosed in the selected filing." if metric == "ebitda" else "No observation was extracted for this period.", "observation": None, "qoq": {"status": "unavailable", "reason": "No current observation is available."}, "yoy": {"status": "unavailable", "reason": "No current observation is available."}})
        else:
            metrics.append({"metric": metric, "status": observation.extraction_status, "message": None, "observation": _financial_observation_payload(observation), "qoq": _quarter_comparison(db, observation, company, 0), "yoy": _quarter_comparison(db, observation, company, 1)})
    return {
        "ticker": company.ticker, "company": company.name,
        "status": "available" if any(m["status"] == "validated" for m in metrics) else ("needs_review" if all_rows else "unavailable"),
        "statement_basis": statement_basis, "period_type": period_type,
        "fiscal_year": all_rows[0].fiscal_year if all_rows else fiscal_year,
        "fiscal_quarter": all_rows[0].fiscal_quarter if all_rows else fiscal_quarter,
        "period_start": all_rows[0].period_start if all_rows else None,
        "period_end": latest_end, "currency": "INR", "unit": "INR crore", "metrics": metrics,
    }


@app.get("/api/financials/{ticker}/history", response_model=FinancialHistoryResponse)
def financial_history(
    ticker: str,
    metric: str = Query("revenue", min_length=1, max_length=60, pattern="^[a-z][a-z0-9_]*$"),
    statement_basis: str = Query("consolidated", pattern="^(consolidated|standalone)$"),
    period_type: str = Query("quarterly", pattern="^(quarterly|annual)$"),
    periods: int = Query(8, ge=1, le=40),
    db: Session = Depends(get_db),
) -> dict:
    company = _get_company(db, ticker)
    selected = _financial_rows(db, company, metric, statement_basis, period_type)[-periods:]
    data = []
    all_periods = _financial_rows(db, company, metric, statement_basis, period_type)
    for item in selected:
        peers = [row for row in db.scalars(select(FinancialObservation).where(
            FinancialObservation.company_id == company.id,
            FinancialObservation.canonical_metric == metric,
            FinancialObservation.statement_basis == statement_basis,
            FinancialObservation.period_type == period_type,
            FinancialObservation.period_end == item.period_end,
        )).all()]
        values = sorted({float(row.normalised_value_in_crore) for row in peers})
        conflict = len(values) > 1
        data.append({
            "observation": _financial_observation_payload(item), "conflict": conflict,
            "conflict_values_crore": values if conflict else [],
            "qoq": _quarter_comparison(db, item, company, 0), "yoy": _quarter_comparison(db, item, company, 1),
        })
    return {"ticker": company.ticker, "metric": metric, "statement_basis": statement_basis, "count": len(data), "data": data}


@app.get("/api/financials/{ticker}/analysis")
def financial_analysis(
    ticker: str,
    statement_basis: str = Query("consolidated", pattern="^(consolidated|standalone)$"),
    period_type: str = Query("annual", pattern="^(quarterly|annual)$"),
    db: Session = Depends(get_db),
) -> dict:
    company = _get_company(db, ticker)
    metric_names = ("revenue", "pbt", "pat", "total_assets", "total_equity", "borrowings_noncurrent",
                    "borrowings_current", "lease_liabilities_noncurrent", "lease_liabilities_current",
                    "cash_and_cash_equivalents", "current_assets", "current_liabilities", "cash_from_operations",
                    "cash_from_investing", "cash_from_financing", "capital_expenditure_ppe")
    series = {metric: _financial_rows(db, company, metric, statement_basis, period_type) for metric in metric_names}
    by_metric_period = {metric: {row.period_end: row for row in rows} for metric, rows in series.items()}
    dates = sorted({row.period_end for rows in series.values() for row in rows})
    derived = []
    for period_end in dates:
        def get(metric: str):
            row = by_metric_period.get(metric, {}).get(period_end)
            return row if row is not None and row.extraction_status == "validated" else None

        def ratio(key: str, label: str, formula: str, inputs: list[FinancialObservation | None], compute, unit: str = "x"):
            if not all(inputs):
                derived.append({"period_end": period_end, "metric": key, "label": label, "status": "unavailable",
                                "formula": formula, "value": None,
                                "reason": "One or more input observations are missing or not source-validated.", "inputs": [
                                    _financial_observation_payload(item) if item else None for item in inputs]})
                return
            result = compute([float(item.normalised_value_in_crore) for item in inputs if item is not None])
            if result is None:
                derived.append({"period_end": period_end, "metric": key, "label": label, "status": "unavailable",
                                "formula": formula, "value": None, "reason": "The denominator is zero or unsuitable for this ratio.",
                                "inputs": [_financial_observation_payload(item) for item in inputs if item]})
                return
            derived.append({"period_end": period_end, "metric": key, "label": label, "status": "available",
                            "formula": formula, "value": result, "unit": unit,
                            "inputs": [_financial_observation_payload(item) for item in inputs if item]})

        noncurrent, current = get("borrowings_noncurrent"), get("borrowings_current")
        cash, equity = get("cash_and_cash_equivalents"), get("total_equity")
        if noncurrent and current and cash:
            net_debt = float(noncurrent.normalised_value_in_crore + current.normalised_value_in_crore - cash.normalised_value_in_crore)
            derived.append({"period_end": period_end, "metric": "net_debt", "label": "Net debt (lease liabilities excluded)",
                            "status": "available", "formula": "non-current borrowings + current borrowings - cash and cash equivalents; lease liabilities excluded",
                            "value": net_debt, "unit": "INR crore", "inputs": [_financial_observation_payload(x) for x in (noncurrent,current,cash)]})
        else:
            derived.append({"period_end": period_end, "metric": "net_debt", "label": "Net debt (lease liabilities excluded)",
                            "status": "unavailable", "formula": "non-current borrowings + current borrowings - cash and cash equivalents; lease liabilities excluded",
                            "value": None, "reason": "A required input is missing or not source-validated.",
                            "inputs": [_financial_observation_payload(x) if x else None for x in (noncurrent,current,cash)]})
        borrowings_inputs = [noncurrent, current]
        ratio("debt_equity", "Borrowings / equity", "(non-current borrowings + current borrowings) / reported equity", borrowings_inputs + [equity],
              lambda v: (v[0]+v[1])/v[2] if v[2] > 0 else None)
        ratio("current_ratio", "Current ratio", "current assets / current liabilities",
              [get("current_assets"),get("current_liabilities")], lambda v: v[0]/v[1] if v[1] > 0 else None)
        ocf, capex, pat = get("cash_from_operations"), get("capital_expenditure_ppe"), get("pat")
        ratio("free_cash_flow", "Free cash flow", "operating cash flow + reported capex cash-flow amount (capex is shown with its original negative sign)",
              [ocf,capex], lambda v: v[0]+v[1], unit="INR crore")
        ratio("operating_cash_to_pat", "Operating cash flow / PAT", "operating cash flow / reported PAT",
              [ocf,pat], lambda v: v[0]/v[1] if v[1] > 0 else None)

    signals = []
    for metric, label in (("revenue", "Revenue from Operations"), ("pbt", "Profit before tax"), ("pat", "Profit after tax"), ("cash_from_operations", "Operating cash flow")):
        rows = [row for row in series[metric] if row.extraction_status == "validated"]
        if len(rows) >= 2:
            changes = []
            for prior, current in zip(rows, rows[1:]):
                change = float(current.normalised_value_in_crore / prior.normalised_value_in_crore - 1) * 100 if prior.normalised_value_in_crore > 0 else None
                if change is not None:
                    changes.append(f"{prior.fiscal_year} to {current.fiscal_year}: {change:+.1f}%")
            if changes:
                signals.append({"text": f"{label} annual changes — " + "; ".join(changes) + ".",
                                "inputs": [_financial_observation_payload(row) for row in rows]})
    net_debt_rows = [item for item in derived if item["metric"] == "net_debt" and item["status"] == "available"]
    if len(net_debt_rows) >= 2:
        first, last = net_debt_rows[0], net_debt_rows[-1]
        delta = last["value"] - first["value"]
        signals.append({"text": f"Lease-excluded net debt {('increased' if delta >= 0 else 'decreased')} by INR {abs(delta):,.2f} crore between FY {first['period_end'].year} and FY {last['period_end'].year}.",
                        "inputs": first["inputs"] + last["inputs"]})
    return {"ticker": company.ticker, "company": company.name, "statement_basis": statement_basis,
            "period_type": period_type, "as_of": datetime.now(timezone.utc).isoformat(),
            "series": {metric: [_financial_observation_payload(row) for row in rows] for metric, rows in series.items()},
            "derived": derived, "computed_signals": signals}


@app.get("/api/research/{ticker}/thesis-drift")
def thesis_drift(
    ticker: str,
    statement_basis: str = Query("consolidated", pattern="^(consolidated|standalone)$"),
    period_type: str = Query("annual", pattern="^(quarterly|annual)$"),
    periods: int = Query(3, ge=2, le=12),
    db: Session = Depends(get_db),
) -> dict:
    """Return deterministic, validated-only thesis drift and source evidence."""
    company = _get_company(db, ticker)
    return build_thesis_drift(db, company, statement_basis, period_type, periods)


@app.post("/api/research/{ticker}/ask")
def research_assistant(ticker: str, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    question = str(payload.get("question", ""))
    if len(question) > 500:
        raise HTTPException(status_code=422, detail="Question must be 500 characters or fewer")
    result = answer_question(db, ticker, question)
    if result["status"] == "unavailable":
        raise HTTPException(status_code=404, detail=result["answer"])
    return {"ticker": ticker.upper(), "question": question, **result}


@app.get("/api/financial-observations/{observation_id}", response_model=FinancialObservationResponse)
def financial_observation_detail(observation_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(FinancialObservation, observation_id)
    if item is None:
        raise HTTPException(status_code=404, detail={"code": "observation_not_found", "message": "Financial observation not found"})
    return _financial_observation_payload(item)


@app.post("/api/financial-observations/{observation_id}/review")
def review_financial_observation(observation_id: int, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    item = db.get(FinancialObservation, observation_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Financial observation not found")
    status = payload.get("status")
    reviewer = str(payload.get("reviewer", "")).strip()
    note = str(payload.get("note", "")).strip()
    if status not in {"validated", "needs_review", "rejected"} or not reviewer or not note:
        raise HTTPException(status_code=422, detail="Provide status, reviewer, and source-review note")
    item.extraction_status = status
    item.reviewer = reviewer
    item.reviewed_at = datetime.now(timezone.utc)
    item.review_note = note
    db.commit()
    return {"id": item.id, "ticker": item.company.ticker, "metric": item.canonical_metric,
            "period_end": item.period_end, "statement_basis": item.statement_basis,
            "extraction_status": item.extraction_status, "reviewer": item.reviewer,
            "reviewed_at": item.reviewed_at, "review_note": item.review_note}


@app.get("/api/documents/{document_id}/pages", response_model=DocumentPagesResponse)
def document_pages(
    document_id: int,
    page: int | None = Query(None, ge=1),
    start_page: int | None = Query(None, ge=1),
    end_page: int | None = Query(None, ge=1),
    db: Session = Depends(get_db),
):
    document = db.get(SourceDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail={"code": "document_not_found", "message": "Document not found"})
    if page is not None:
        start_page = end_page = page
    if start_page and end_page and start_page > end_page:
        raise HTTPException(status_code=422, detail={"code": "invalid_page_range", "message": "start_page must be before end_page"})
    query = select(DocumentPage).where(DocumentPage.document_id == document_id)
    if start_page:
        query = query.where(DocumentPage.page_number >= start_page)
    if end_page:
        query = query.where(DocumentPage.page_number <= end_page)
    pages = db.scalars(query.order_by(DocumentPage.page_number)).all()
    total = db.scalar(select(func.count(DocumentPage.id)).where(DocumentPage.document_id == document_id)) or 0
    return {"document_id": document_id, "document_title": document.title, "total_pages": total, "count": len(pages), "pages": pages}


@app.get("/api/documents/{document_id}/file", include_in_schema=False)
def registered_pdf(document_id: int, db: Session = Depends(get_db)):
    document = db.get(SourceDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    try:
        path = registered_pdf_path(document)
        if not path.is_file() or path.stat().st_size == 0 or not document.file_hash or sha256_file(path) != document.file_hash:
            raise ValueError("PDF is missing or does not match its registered hash")
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Verified source PDF is unavailable") from exc
    return FileResponse(path, media_type="application/pdf", filename=path.name, content_disposition_type="inline")


@app.get("/api/extraction-status", response_model=ExtractionStatusResponse)
def extraction_status(db: Session = Depends(get_db)) -> dict:
    document_ids = db.scalars(select(SourceDocument.id).where(SourceDocument.local_file_path.is_not(None))).all()
    processed_ids = db.scalars(select(DocumentPage.document_id).distinct()).all()
    needs_ocr_ids = db.scalars(select(DocumentPage.document_id).where(DocumentPage.extraction_status == "needs_ocr").distinct()).all()
    failed_ids = db.scalars(select(DocumentExtractionRun.document_id).where(DocumentExtractionRun.status == "failed").distinct()).all()
    return {
        "documents_total": len(document_ids), "documents_processed": len(set(processed_ids)),
        "pages_processed": db.scalar(select(func.count(DocumentPage.id))) or 0,
        "financial_observations": db.scalar(select(func.count(FinancialObservation.id))) or 0,
        "validated_observations": db.scalar(select(func.count(FinancialObservation.id)).where(FinancialObservation.extraction_status == "validated")) or 0,
        "observations_needing_review": db.scalar(select(func.count(FinancialObservation.id)).where(FinancialObservation.extraction_status == "needs_review")) or 0,
        "documents_requiring_ocr": len(set(needs_ocr_ids)), "failed_documents": len(set(failed_ids)),
        "parser_version": PARSER_VERSION,
    }


@app.get("/", include_in_schema=False)
def root():
    return FileResponse(PROJECT_ROOT / "index.html")


@app.get("/styles.css", include_in_schema=False)
def styles():
    return FileResponse(PROJECT_ROOT / "styles.css", media_type="text/css")


@app.get("/app.js", include_in_schema=False)
def frontend_js():
    return FileResponse(PROJECT_ROOT / "app.js", media_type="application/javascript")

