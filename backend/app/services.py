from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import DATA_DIR
from .models import Announcement, Company, DailyMarketData, IngestionRun, SourceDocument, utcnow
from .providers.nse import NSEProvider
from .providers.tata_power_ir import TataPowerIRProvider
from .providers.bel_ir import BELIRProvider
from .providers.infosys_ir import InfosysIRProvider
from .providers.yahoo_finance import fetch_history
from .intelligence import evaluate_alerts
from .seed import seed_companies

CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _begin_run(db: Session, provider: str) -> IngestionRun:
    run = IngestionRun(provider=provider, started_at=utcnow(), status="running")
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _end_run(db: Session, run: IngestionRun, attempted: int, inserted: int, updated: int, errors: list[str]) -> None:
    run.completed_at = utcnow()
    run.records_attempted = attempted
    run.records_inserted = inserted
    run.records_updated = updated
    run.records_failed = len(errors)
    run.status = "failed" if errors and not (inserted or updated) else "partial" if errors else "success"
    run.error_summary = "\n".join(errors)[:12000] or None
    db.commit()


def sync_market(db: Session, months: int = 6) -> dict:
    seed_companies(db)
    run = _begin_run(db, "yahoo_finance")
    companies = list(db.scalars(select(Company).where(Company.holding.has(is_active=True)).order_by(Company.ticker)))
    attempted = inserted = updated = 0
    errors: list[str] = []
    reports = []
    delay = float(os.getenv("YAHOO_REQUEST_DELAY_SECONDS", "0.4"))
    for index, company in enumerate(companies):
        attempted += 1
        try:
            rows = fetch_history(company.yfinance_symbol, months=months, retries=2, delay_seconds=delay)
            if not rows:
                errors.append(f"{company.ticker}: provider returned no completed daily sessions")
                reports.append({"ticker": company.ticker, "status": "empty", "sessions": 0, "latest_date": None})
                continue
            for row in rows:
                existing = db.scalar(select(DailyMarketData).where(DailyMarketData.company_id == company.id, DailyMarketData.trading_date == row["trading_date"]))
                if existing is None:
                    db.add(DailyMarketData(company_id=company.id, fetched_at=utcnow(), **row))
                    inserted += 1
                else:
                    for key, value in row.items():
                        setattr(existing, key, value)
                    existing.fetched_at = utcnow()
                    updated += 1
            db.commit()
            reports.append({"ticker": company.ticker, "symbol": company.yfinance_symbol, "status": "success", "sessions": len(rows), "latest_date": rows[-1]["trading_date"].isoformat()})
        except Exception as exc:
            db.rollback()
            message = f"{company.ticker}: {type(exc).__name__}: {exc}"
            errors.append(message)
            reports.append({"ticker": company.ticker, "symbol": company.yfinance_symbol, "status": "failed", "sessions": 0, "latest_date": None, "error": message})
        if index < len(companies) - 1:
            time.sleep(delay)
    benchmark_report = None
    benchmark_company = db.scalar(select(Company).where(Company.ticker == "NIFTY50"))
    if benchmark_company is not None:
        try:
            rows = fetch_history(benchmark_company.yfinance_symbol, months=months, retries=1, delay_seconds=delay)
            for row in rows:
                existing = db.scalar(select(DailyMarketData).where(DailyMarketData.company_id == benchmark_company.id, DailyMarketData.trading_date == row["trading_date"]))
                if existing is None:
                    db.add(DailyMarketData(company_id=benchmark_company.id, fetched_at=utcnow(), **row))
                    inserted += 1
                else:
                    for key, value in row.items():
                        setattr(existing, key, value)
                    existing.fetched_at = utcnow()
                    updated += 1
            db.commit()
            benchmark_report = {"ticker": "NIFTY50", "symbol": "^NSEI", "status": "success" if rows else "empty", "sessions": len(rows), "latest_date": rows[-1]["trading_date"].isoformat() if rows else None}
        except Exception as exc:
            db.rollback()
            benchmark_report = {"ticker": "NIFTY50", "symbol": "^NSEI", "status": "optional_failed", "sessions": 0, "error": f"{type(exc).__name__}: {exc}"}
    _end_run(db, run, attempted, inserted, updated, errors)
    return {"provider": run.provider, "status": run.status, "attempted": attempted, "inserted": inserted, "updated": updated, "failed": len(errors), "ticker_report": reports, "benchmark": benchmark_report, "errors": errors}


def _announcement_cache(ticker: str, start: date, end: date, ttl_minutes: int = 15) -> list[dict] | None:
    path = CACHE_DIR / "nse" / f"{ticker}_{start:%Y%m%d}_{end:%Y%m%d}.json"
    if not path.exists() or time.time() - path.stat().st_mtime > ttl_minutes * 60:
        return None
    try:
        rows = json.loads(path.read_text())
        for row in rows:
            value = row.get("announcement_datetime")
            if value:
                row["announcement_datetime"] = datetime.fromisoformat(value)
            value = row.get("ingested_at")
            if value:
                row["ingested_at"] = datetime.fromisoformat(value)
        return rows
    except (OSError, json.JSONDecodeError):
        return None


def _write_announcement_cache(ticker: str, start: date, end: date, payload: list[dict]) -> None:
    path = CACHE_DIR / "nse"
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{ticker}_{start:%Y%m%d}_{end:%Y%m%d}.json").write_text(json.dumps(payload, ensure_ascii=False, default=str))


def sync_announcements(db: Session, lookback_days: int = 30, end_date: date | None = None) -> dict:
    seed_companies(db)
    run = _begin_run(db, "nse_announcements")
    companies = list(db.scalars(select(Company).where(Company.holding.has(is_active=True)).order_by(Company.ticker)))
    end = end_date or date.today()
    start = end - timedelta(days=lookback_days)
    provider = NSEProvider(delay_seconds=float(os.getenv("NSE_REQUEST_DELAY_SECONDS", "0.35")))
    attempted = inserted = updated = 0
    errors: list[str] = []
    ticker_reports = []
    for company in companies:
        attempted += 1
        try:
            rows = _announcement_cache(company.ticker, start, end)
            cached = rows is not None
            if rows is None:
                rows = provider.fetch(company.ticker, start, end)
                _write_announcement_cache(company.ticker, start, end, rows)
            ticker_inserted = ticker_updated = 0
            for row in rows:
                existing = db.scalar(select(Announcement).where(Announcement.dedupe_key == row["dedupe_key"]))
                company_id = company.id
                values = {k: v for k, v in row.items() if k not in {"ticker"}}
                if existing is None:
                    db.add(Announcement(company_id=company_id, **values))
                    inserted += 1
                    ticker_inserted += 1
                else:
                    existing.company_id = company_id
                    for key, value in values.items():
                        setattr(existing, key, value)
                    updated += 1
                    ticker_updated += 1
            db.commit()
            ticker_reports.append({"ticker": company.ticker, "status": "success", "records": len(rows), "inserted": ticker_inserted, "updated": ticker_updated, "cache": cached})
        except Exception as exc:
            db.rollback()
            message = f"{company.ticker}: {type(exc).__name__}: {exc}"
            errors.append(message)
            ticker_reports.append({"ticker": company.ticker, "status": "failed", "records": 0, "error": message})
    _end_run(db, run, attempted, inserted, updated, errors)
    return {"provider": run.provider, "status": run.status, "lookback_days": lookback_days, "attempted": attempted, "inserted": inserted, "updated": updated, "failed": len(errors), "ticker_report": ticker_reports, "errors": errors}


def discover_documents(db: Session, ticker: str = "TATAPOWER", download: bool = True) -> dict:
    seed_companies(db)
    company = db.scalar(select(Company).where(Company.ticker == ticker.upper()))
    if company is None:
        return {"provider": "tata_power_ir", "status": "failed", "ticker": ticker.upper(), "records": 0, "errors": ["Company not found; seed the portfolio first"]}
    if ticker.upper() == "BEL":
        company.website = "https://bel-india.in/"
        company.investor_relations_url = "https://bel-india.in/investors/"
    if ticker.upper() == "INFY":
        company.website = "https://www.infosys.com/"
        company.investor_relations_url = "https://www.infosys.com/investors/"
    providers = {"TATAPOWER": ("tata_power_ir", TataPowerIRProvider), "BEL": ("bel_ir", BELIRProvider), "INFY": ("infosys_ir", InfosysIRProvider)}
    provider_name, provider_class = providers.get(ticker.upper(), (f"{ticker.lower()}_ir", None))
    run = _begin_run(db, provider_name)
    attempted = inserted = updated = 0
    errors = []
    try:
        if provider_class is None:
            documents, errors = [], [f"Official investor-relations discovery is not configured for {ticker.upper()}"]
        else:
            documents, errors = provider_class().discover(download=download)
        attempted = len(documents)
        for doc in documents:
            existing = db.scalar(select(SourceDocument).where(SourceDocument.dedupe_key == doc["dedupe_key"]))
            if existing is None:
                db.add(SourceDocument(company_id=company.id, **doc))
                inserted += 1
            else:
                existing.company_id = company.id
                for key, value in doc.items():
                    setattr(existing, key, value)
                existing.discovered_at = utcnow()
                updated += 1
        db.commit()
    except Exception as exc:
        db.rollback()
        errors.append(f"{type(exc).__name__}: {exc}")
    _end_run(db, run, attempted, inserted, updated, errors)
    return {"provider": run.provider, "ticker": ticker.upper(), "status": run.status, "attempted": attempted, "inserted": inserted, "updated": updated, "failed": len(errors), "documents": [{"title": d.title, "document_type": d.document_type, "fiscal_year": d.fiscal_year, "quarter": d.quarter, "url": d.download_url, "status": d.discovery_status, "file_hash": d.file_hash} for d in db.scalars(select(SourceDocument).where(SourceDocument.company_id == company.id).order_by(SourceDocument.document_type, SourceDocument.fiscal_year.desc(), SourceDocument.quarter.desc()))], "errors": errors}


def sync_all(db: Session, lookback_days: int = 30, download_docs: bool = True, market_months: int = 6) -> dict:
    market = sync_market(db, months=market_months)
    announcements = sync_announcements(db, lookback_days=lookback_days)
    documents = discover_documents(db, "TATAPOWER", download=download_docs)
    alerts = evaluate_alerts(db) if market.get("status") in {"success", "partial"} or announcements.get("status") in {"success", "partial"} else {"status":"skipped","created":0}
    return {"market": market, "announcements": announcements, "documents": documents, "alerts": alerts}


def research_company(db: Session, ticker: str, months: int = 6, announcement_days: int = 30) -> dict:
    """Fetch EOD data and filings without adding the symbol to the portfolio."""
    import re
    from .providers.nse import NSEProvider

    ticker = ticker.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9&-]{0,19}", ticker) or ticker == "NIFTY50":
        raise ValueError("Ticker must be a valid NSE equity symbol.")
    company = db.scalar(select(Company).where(Company.ticker == ticker))
    symbol = company.yfinance_symbol if company else f"{ticker}.NS"
    rows = fetch_history(symbol, months=months, retries=1, delay_seconds=0.1)
    if not rows:
        raise ValueError(f"No completed EOD market data was returned for {ticker}; it was not added to the research universe.")
    if company is None:
        company = Company(name=ticker, ticker=ticker, exchange="NSE", sector=None, yfinance_symbol=symbol)
        db.add(company); db.commit(); db.refresh(company)
    inserted = updated = 0
    for row in rows:
        existing = db.scalar(select(DailyMarketData).where(DailyMarketData.company_id == company.id, DailyMarketData.trading_date == row["trading_date"]))
        if existing is None:
            db.add(DailyMarketData(company_id=company.id, fetched_at=utcnow(), **row)); inserted += 1
        else:
            for key, value in row.items(): setattr(existing, key, value)
            existing.fetched_at = utcnow(); updated += 1
    db.commit()
    end = date.today(); start = end - timedelta(days=announcement_days)
    ann_result = {"status": "unavailable", "records": 0, "error": None}
    try:
        announcements = _announcement_cache(ticker, start, end)
        cached = announcements is not None
        if announcements is None:
            announcements = NSEProvider(delay_seconds=0.15).fetch(ticker, start, end)
            _write_announcement_cache(ticker, start, end, announcements)
        inserted_ann = 0
        for item in announcements:
            values = {k: v for k, v in item.items() if k != "ticker"}
            existing = db.scalar(select(Announcement).where(Announcement.dedupe_key == item["dedupe_key"]))
            if existing is None:
                db.add(Announcement(company_id=company.id, **values)); inserted_ann += 1
            else:
                existing.company_id = company.id
                for key, value in values.items(): setattr(existing, key, value)
        db.commit()
        ann_result = {"status": "success", "records": len(announcements), "inserted": inserted_ann, "cached": cached, "error": None}
    except Exception as exc:
        db.rollback()
        ann_result = {"status": "unavailable", "records": 0, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ticker": company.ticker, "company": company.name, "exchange": "NSE",
        "portfolio_member": bool(company.holding and company.holding.is_active),
        "market": {"status": "success", "sessions": len(rows), "inserted": inserted, "updated": updated, "latest_date": rows[-1]["trading_date"].isoformat(), "provider": "Yahoo Finance via yfinance"},
        "announcements": ann_result,
        "source_coverage": "primary company documents are not configured for this issuer",
    }
