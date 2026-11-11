from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.database import Base
from backend.app.main import research_coverage
from backend.app.models import Company, DailyMarketData, SourceDocument


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def test_coverage_distinguishes_unconfigured_from_connected_market():
    engine = _db()
    with Session(engine) as db:
        company = Company(name="Example", ticker="EXM", exchange="NSE", yfinance_symbol="EXM.NS")
        db.add(company); db.flush()
        result = research_coverage("EXM", db)
        assert result["states"]["annual_reports"] == "not_configured"
        assert result["states"]["validated_financial_observations"] == "not_configured"
        db.add(DailyMarketData(company_id=company.id, trading_date=date(2026, 1, 2), close=10, source="test")); db.commit()
        result = research_coverage("EXM", db)
        assert result["states"]["eod_market_data"] == "available"
    engine.dispose()


def test_coverage_reports_registered_and_provider_error_documents():
    engine = _db()
    with Session(engine) as db:
        company = Company(name="Infosys", ticker="INFY", exchange="NSE", yfinance_symbol="INFY.NS")
        db.add(company); db.flush()
        common = dict(company_id=company.id, fiscal_year="FY 2024 - 2025", quarter=None,
                      document_date=None, landing_page_url="https://example.com", download_url="https://example.com/a.pdf",
                      source="test", dedupe_key="a")
        db.add(SourceDocument(title="Annual", document_type="annual_report", discovery_status="downloaded", **common)); db.commit()
        result = research_coverage("INFY", db)
        assert result["states"]["annual_reports"] == "documents_registered"
        db.query(SourceDocument).filter_by(dedupe_key="a").update({"discovery_status": "provider_error", "dedupe_key": "b"}); db.commit()
        result = research_coverage("INFY", db)
        assert result["states"]["annual_reports"] == "provider_error"
    engine.dispose()
