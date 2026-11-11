from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from backend.app.database import Base
from backend.app.models import Announcement, Company, DailyMarketData, PortfolioHolding
from backend.app.database import get_db
from backend.app.main import app
from backend.app.providers import yahoo_finance
from backend.app.providers.nse import parse_announcement
from backend.app.providers import tata_power_ir
from backend.app.seed import seed_companies
from backend.app import services


def test_seed_is_repeatable_and_has_18_current_holdings():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        first = seed_companies(db)
        second = seed_companies(db)
        assert first["inserted"] == 18
        assert second["inserted"] == 0
        assert db.scalar(select(func.count(PortfolioHolding.id)).where(PortfolioHolding.is_active.is_(True))) == 18
        assert db.scalar(select(Company).where(Company.ticker == "TMPV")).yfinance_symbol == "TMPV.NS"
        assert db.scalar(select(Company).where(Company.ticker == "NIFTY50")).yfinance_symbol == "^NSEI"


def test_market_provider_excludes_incomplete_today_bar_and_calculates_returns(monkeypatch):
    index = pd.date_range("2026-11-05", periods=3, freq="D", tz="America/New_York")
    frame = pd.DataFrame({
        "Open": [100, 101, 103], "High": [101, 104, 105], "Low": [99, 100, 102],
        "Close": [100, 103, 104], "Adj Close": [99, 102, 103], "Volume": [1000, 2000, 3000],
    }, index=index)
    captured = {}

    class FakeTicker:
        def __init__(self, symbol):
            captured["symbol"] = symbol

        def history(self, **kwargs):
            captured.update(kwargs)
            return frame

    monkeypatch.setattr(yahoo_finance.yf, "Ticker", FakeTicker)
    now = datetime(2026, 11, 6, 12, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    rows = yahoo_finance.fetch_history("TATAPOWER.NS", now=now, delay_seconds=0, retries=0)
    assert captured["symbol"] == "TATAPOWER.NS"
    assert captured["auto_adjust"] is False
    assert len(rows) == 1  # only Nov 5, because Nov 6 is still in session
    assert rows[0]["trading_date"] == date(2026, 11, 5)
    assert rows[0]["daily_return_pct"] is None


def test_market_provider_derives_previous_close_and_relative_volume(monkeypatch):
    dates = pd.bdate_range("2026-10-01", periods=22, tz="America/New_York")
    frame = pd.DataFrame({
        "Open": [10.0] * 22, "High": [11.0] * 22, "Low": [9.0] * 22,
        "Close": [10.0 + i for i in range(22)], "Adj Close": [10.0 + i for i in range(22)],
        "Volume": [100] * 21 + [200],
    }, index=dates)

    class FakeTicker:
        def __init__(self, symbol): pass
        def history(self, **kwargs): return frame

    monkeypatch.setattr(yahoo_finance.yf, "Ticker", FakeTicker)
    rows = yahoo_finance.fetch_history("TEST.NS", now=datetime(2026, 11, 20, 12, tzinfo=ZoneInfo("Asia/Kolkata")), delay_seconds=0, retries=0)
    last = rows[-1]
    assert last["previous_close"] == 30.0
    assert last["daily_return_pct"] == 1 / 30 * 100
    assert last["average_volume_20"] == 100
    assert last["relative_volume"] == 2


def test_nse_announcement_parser_preserves_source_and_dedupes():
    raw = {"seq_id": "12345", "an_dt": "07-Nov-2026 18:40:29", "subject": "Financial Results", "desc": "Company informed the Exchange", "attchmntFile": "https://nsearchives.nseindia.com/corporate/test.pdf"}
    first = parse_announcement(raw, "TATAPOWER")
    second = parse_announcement(raw, "TATAPOWER")
    assert first["external_announcement_id"] == "12345"
    assert first["normalized_category"] == "Results"
    assert first["subject"] == "Financial Results"
    assert first["details"] == "Company informed the Exchange"
    assert first["dedupe_key"] == second["dedupe_key"]
    assert first["attachment_url"].startswith("https://")


def test_tata_ir_document_classification_and_pdf_hash(monkeypatch, tmp_path):
    assert tata_power_ir._is_annual_report("107AnnualReport-2025-26")
    assert tata_power_ir._is_transcript("Q1 FY27 Earning Call Transcript")
    assert tata_power_ir._is_quarterly_results("Consolidated-and-Standalone-Financial-Results", "Q1")
    pdf = b"%PDF-1.4\nFINN source fixture\n"

    class Response:
        headers = {"Content-Type": "application/pdf"}
        def raise_for_status(self): pass
        def iter_content(self, chunk_size): yield pdf

    monkeypatch.setattr(tata_power_ir, "DOC_DIR", tmp_path)
    provider = tata_power_ir.TataPowerIRProvider()
    provider.session.get = lambda *args, **kwargs: Response()
    path, digest = provider._download("https://www.tatapower.com/source/example.pdf")
    import hashlib
    assert path.exists()
    assert path.read_bytes() == pdf
    assert digest == hashlib.sha256(pdf).hexdigest()


def test_nse_sync_upserts_without_duplicates(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    class FakeNSEProvider:
        def __init__(self, delay_seconds=0): pass
        def fetch(self, ticker, start, end):
            return [parse_announcement({"seq_id": f"{ticker}-1", "an_dt": "07-Nov-2026 18:40:29", "subject": "Notice", "desc": "Source detail"}, ticker)]

    monkeypatch.setattr(services, "NSEProvider", FakeNSEProvider)
    monkeypatch.setattr(services, "_announcement_cache", lambda *args: None)
    monkeypatch.setattr(services, "_write_announcement_cache", lambda *args: None)
    with Session() as db:
        seed_companies(db)
        first = services.sync_announcements(db, end_date=date(2026, 11, 9))
        second = services.sync_announcements(db, end_date=date(2026, 11, 9))
        assert first["inserted"] == 18
        assert second["inserted"] == 0 and second["updated"] == 18
        assert db.scalar(select(func.count(Announcement.id))) == 18


def test_api_returns_persisted_market_and_filing_records():
    test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(test_engine)
    TestSession = sessionmaker(bind=test_engine, expire_on_commit=False)

    def override_db():
        with TestSession() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestSession() as db:
            seed_companies(db)
            company = db.scalar(select(Company).where(Company.ticker == "TATAPOWER"))
            db.add(DailyMarketData(company_id=company.id, trading_date=date(2026, 11, 6), close=431.25, previous_close=430.0, daily_return_pct=0.2906976744, volume=1_000_000, average_volume_20=900_000, relative_volume=1.111111, source="Yahoo fixture"))
            db.add(Announcement(company_id=company.id, exchange="NSE", external_announcement_id="case-1", dedupe_key="fixture-1", subject="Exchange notice", details="Original exchange detail", normalized_category="Unclassified", source_page_url="https://www.nseindia.com/companies-listing/corporate-filings-announcements", source="NSE"))
            db.commit()
        client = TestClient(app)
        assert client.get("/api/health").json()["database"] == "connected"
        portfolio = client.get("/api/portfolio").json()
        assert portfolio["holding_count"] == 18
        market = client.get("/api/market/TATAPOWER/summary").json()
        assert market["latest_close"] == 431.25
        assert market["latest_trading_date"] == "2026-11-06"
        filings = client.get("/api/announcements?ticker=TATAPOWER").json()["items"]
        assert len(filings) == 1
        assert filings[0]["subject"] == "Exchange notice"
        assert filings[0]["assessment_status"] == "unassessed"
    finally:
        app.dependency_overrides.clear()
        test_engine.dispose()

