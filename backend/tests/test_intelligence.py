from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import services
from backend.app.database import Base, get_db
from backend.app.intelligence import classify_announcement, evaluate_alerts
from backend.app.main import app, request_clock
from backend.app.models import AlertEvent, AlertPreference, Announcement, Company, DailyMarketData, PortfolioHolding
from backend.app.providers.nse import parse_announcement
from backend.app.seed import seed_companies

IST = ZoneInfo("Asia/Kolkata")


def _db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, Session


def test_materiality_rules_explain_results_orders_and_routine_disclosures():
    result = Announcement(subject="Unaudited Financial Results", original_category="Financial Results")
    order = Announcement(subject="Bagging/Receiving of orders/contracts")
    routine = Announcement(subject="Shareholding Pattern", normalized_category="Shareholding")
    assert classify_announcement(result)["materiality"] == "high"
    assert classify_announcement(result)["category"] == "results"
    assert "financial picture" in classify_announcement(result)["materiality_reason"]
    assert classify_announcement(order)["category"] == "orders"
    assert classify_announcement(routine)["materiality"] == "routine"


def test_digest_time_window_uses_ist_and_excludes_undated_without_substitution():
    engine, Session = _db_session()

    def override_db():
        with Session() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with Session() as db:
            seed_companies(db)
            company = db.scalar(select(Company).where(Company.ticker == "TATAPOWER"))
            now = datetime(2026, 11, 9, 12, 0, tzinfo=IST)
            rows = [
                parse_announcement({"seq_id": "recent", "an_dt": "09-Nov-2026 10:00:00", "subject": "Financial Results"}, company.ticker),
                parse_announcement({"seq_id": "older", "an_dt": "01-Nov-2026 10:00:00", "subject": "Shareholding Pattern"}, company.ticker),
                parse_announcement({"seq_id": "undated", "subject": "General update"}, company.ticker),
            ]
            for row in rows:
                db.add(Announcement(company_id=company.id, **{k: v for k, v in row.items() if k != "ticker"}))
            db.commit()
        app.dependency_overrides[request_clock] = lambda: now
        client = TestClient(app)
        r = client.get("/api/digest?window=24h")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == body["total"] == 1
        assert body["counts"]["high"] == 1
        assert body["undated_portfolio_announcements"] == 1
        assert body["items"][0]["subject"] == "Financial Results"
        week = client.get("/api/digest?window=7d").json()
        assert week["count"] == 1
        month = client.get("/api/digest?window=30d").json()
        assert month["count"] == 2
        custom = client.get("/api/digest?from_date=2026-11-01&to_date=2026-11-09").json()
        assert custom["count"] == 2
        assert custom["timezone"] == "Asia/Kolkata"
        invalid = client.get("/api/digest?from_date=2026-11-10&to_date=2026-11-09")
        assert invalid.status_code == 422
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_alert_evaluation_persists_preferences_and_deduplicates_events():
    engine, Session = _db_session()
    try:
        with Session() as db:
            seed_companies(db)
            company = db.scalar(select(Company).where(Company.ticker == "TATAPOWER"))
            db.add(AlertPreference(id=1, announcement_categories={"results": True}, positive_price_move_pct=5, negative_price_move_pct=5, unusual_volume_multiple=2))
            announcement = Announcement(
                company_id=company.id, exchange="NSE", external_announcement_id="recent-result",
                dedupe_key="recent-result-key", subject="Financial Results", normalized_category="Results",
                announcement_datetime=datetime(2026, 11, 9, 10, tzinfo=IST).replace(tzinfo=None),
                source_page_url="https://nseindia.com/filings", source="NSE",
            )
            market = DailyMarketData(company_id=company.id, trading_date=date(2026, 11, 6), close=110, previous_close=100,
                                     daily_return_pct=10, volume=300, average_volume_20=100, relative_volume=3, source="test")
            db.add_all([announcement, market]); db.commit()
            now = datetime(2026, 11, 9, 12, tzinfo=IST)
            first = evaluate_alerts(db, now=now)
            second = evaluate_alerts(db, now=now)
            assert first["events_created"] == 3
            assert second["events_created"] == 0
            assert db.scalar(select(func.count(AlertEvent.id))) == 3
            assert db.get(AlertPreference, 1).positive_price_move_pct == 5
    finally:
        engine.dispose()


def test_alert_settings_api_applies_category_and_asymmetric_price_thresholds():
    engine, Session = _db_session()

    def override_db():
        with Session() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with Session() as db:
            seed_companies(db)
            up = db.scalar(select(Company).where(Company.ticker == "TATAPOWER"))
            down = db.scalar(select(Company).where(Company.ticker == "ICICIBANK"))
            db.add_all([
                DailyMarketData(company_id=up.id, trading_date=date(2026, 11, 6), close=110, previous_close=100, daily_return_pct=6, volume=100, average_volume_20=100, relative_volume=1, source="fixture"),
                DailyMarketData(company_id=down.id, trading_date=date(2026, 11, 6), close=94, previous_close=100, daily_return_pct=-6, volume=100, average_volume_20=100, relative_volume=1, source="fixture"),
                Announcement(company_id=up.id, exchange="NSE", dedupe_key="disabled-order", subject="Order received", normalized_category="Orders", announcement_datetime=datetime(2026, 11, 9, 10, tzinfo=IST).replace(tzinfo=None), source_page_url="https://nse.test", source="NSE"),
                Announcement(company_id=down.id, exchange="NSE", dedupe_key="enabled-result", subject="Financial Results", normalized_category="Results", announcement_datetime=datetime(2026, 11, 9, 10, tzinfo=IST).replace(tzinfo=None), source_page_url="https://nse.test", source="NSE"),
            ]); db.commit()
        app.dependency_overrides[request_clock] = lambda: datetime(2026, 11, 9, 12, tzinfo=IST)
        client = TestClient(app)
        update = client.patch("/api/alerts/settings", json={"announcement_categories":{"results":True,"orders":False},"positive_price_move_pct":5,"negative_price_move_pct":4,"unusual_volume_multiple":2})
        assert update.status_code == 200
        prefs = update.json()
        assert prefs["announcement_categories"]["results"] is True
        assert prefs["announcement_categories"]["orders"] is False
        assert prefs["positive_price_move_pct"] == 5
        assert prefs["negative_price_move_pct"] == 4
        first = client.post("/api/alerts/evaluate").json()
        types = {item["rule_type"] for item in first["events"]}
        assert types == {"announcement", "price_up", "price_down"}
        assert len(first["events"]) == 3
        history = client.get("/api/alerts").json()
        assert history["count"] == 3
        assert client.post("/api/alerts/evaluate").json()["events_created"] == 0
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_ad_hoc_research_sync_does_not_create_portfolio_holding(monkeypatch):
    engine, Session = _db_session()

    class FakeNSE:
        def __init__(self, delay_seconds=0): pass
        def fetch(self, ticker, start, end): return []

    monkeypatch.setattr(services, "fetch_history", lambda symbol, **kwargs: [{
        "trading_date": date(2026, 11, 6), "open": 10, "high": 11, "low": 9, "close": 10,
        "adjusted_close": 10, "volume": 100, "previous_close": 9.5, "daily_return_pct": 5.26,
        "average_volume_20": None, "relative_volume": None, "source": "Yahoo test",
    }])
    monkeypatch.setattr(services, "NSEProvider", FakeNSE)
    monkeypatch.setattr(services, "_announcement_cache", lambda *args: None)
    monkeypatch.setattr(services, "_write_announcement_cache", lambda *args: None)
    try:
        with Session() as db:
            seed_companies(db)
            result = services.research_company(db, "HINDUNILVR", months=1, announcement_days=1)
            company = db.scalar(select(Company).where(Company.ticker == "HINDUNILVR"))
            assert result["market"]["status"] == "success"
            assert result["portfolio_member"] is False
            assert company.holding is None
            assert db.scalar(select(func.count(DailyMarketData.id)).where(DailyMarketData.company_id == company.id)) == 1
            assert services.research_company(db, "HINDUNILVR")["market"]["inserted"] == 0
            assert db.scalar(select(func.count(Company.id)).where(Company.ticker == "HINDUNILVR")) == 1
    finally:
        engine.dispose()
