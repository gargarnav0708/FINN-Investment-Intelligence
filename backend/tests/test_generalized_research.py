from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.database import Base
from backend.app.models import Company, ProviderFinancialObservation, ResearchDatasetSnapshot
from backend.app.providers import yahoo_research
from backend.app import services
from backend.app import main as main_module
from backend.app.database import get_db
from fastapi.testclient import TestClient


def test_exchange_symbol_and_listed_company_search(monkeypatch):
    assert yahoo_research.exchange_symbol("NSE", "INFY") == "INFY.NS"
    assert yahoo_research.exchange_symbol("BSE", "500325") == "500325.BO"

    class Search:
        def __init__(self, query, **kwargs):
            self.quotes = [
                {"quoteType": "EQUITY", "exchange": "NSI", "symbol": "INFY.NS", "shortname": "Infosys Limited"},
                {"quoteType": "EQUITY", "exchange": "BSE", "symbol": "500325.BO", "longname": "Reliance Industries"},
                {"quoteType": "ETF", "exchange": "NSI", "symbol": "NIFTYBEES.NS", "shortname": "ETF"},
                {"quoteType": "EQUITY", "exchange": "NYQ", "symbol": "IBM", "shortname": "IBM"},
            ]

    monkeypatch.setattr(yahoo_research.yf, "Search", Search)
    assert [row["yfinance_symbol"] for row in yahoo_research.search_listed_companies("infosys")] == ["INFY.NS", "500325.BO"]


@pytest.mark.parametrize("symbol,name,sector", [
    ("INFY.NS", "Infosys Limited", "Technology"),
    ("BEL.NS", "Bharat Electronics Limited", "Industrials"),
    ("HDFCBANK.NS", "HDFC Bank Limited", "Financial Services"),
])
def test_financial_statement_mapping_keeps_provider_reported_rows_separate(monkeypatch, symbol, name, sector):
    income = pd.DataFrame({pd.Timestamp("2026-03-31"): [25000000000, 2500000000, 1800000000]}, index=["Total Revenue", "Operating Income", "Net Income"])
    empty = pd.DataFrame()

    class Ticker:
        def __init__(self, ticker_symbol): pass
        def get_info(self): return {"longName": name, "financialCurrency": "INR", "sector": sector}
        financials = income
        quarterly_financials = empty
        balance_sheet = empty
        quarterly_balance_sheet = empty
        cashflow = empty
        quarterly_cashflow = empty

    monkeypatch.setattr(yahoo_research.yf, "Ticker", Ticker)
    profile, rows = yahoo_research.fetch_financial_statements(symbol)
    assert profile["name"] == name and profile["sector"] == sector
    assert {row["canonical_metric"] for row in rows} == {"revenue", "operating_profit", "pat"}
    assert all(row["normalised_value_in_crore"] == row["reported_value"] / Decimal(10_000_000) for row in rows)
    assert all(row["period_type"] == "annual" for row in rows)


def test_on_demand_refresh_persists_and_reuses_yahoo_data(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    calls = {"market": 0, "financials": 0}
    monkeypatch.setattr(services, "_announcement_cache", lambda *args, **kwargs: [])
    monkeypatch.setattr(services, "fetch_history", lambda *args, **kwargs: calls.__setitem__("market", calls["market"] + 1) or [{"trading_date": date(2026, 11, 10), "close": 101.0, "source": "Yahoo Finance via yfinance"}])
    monkeypatch.setattr(services, "fetch_financial_statements", lambda symbol: (calls.__setitem__("financials", calls["financials"] + 1) or ({"name": "Infosys Limited", "sector": "Technology", "business": "A provider sourced company description.", "currency": "INR"}, [{"statement_type": "income", "period_type": "annual", "period_end": date(2026, 3, 31), "fiscal_year": "FY 2025 - 2026", "canonical_metric": "revenue", "reported_label": "Total Revenue", "reported_value": Decimal("1000000000"), "currency": "INR", "reported_unit": "INR as reported by provider", "normalised_value_in_crore": Decimal("100"), "provider": "Yahoo Finance via yfinance", "source_url": "https://finance.yahoo.com/quote/INFY.NS/financials/"}])))
    with Session(engine) as db:
        company = Company(name="INFY", ticker="INFY", exchange="NSE", yfinance_symbol="INFY.NS")
        db.add(company); db.commit(); db.refresh(company)
        first = services.refresh_company_research(db, company)
        second = services.refresh_company_research(db, company)
        rows = db.scalars(select(ProviderFinancialObservation).where(ProviderFinancialObservation.company_id == company.id)).all()
        status = db.scalar(select(ResearchDatasetSnapshot).where(ResearchDatasetSnapshot.company_id == company.id, ResearchDatasetSnapshot.dataset == "financials"))
        assert first["status"] == "partial"
        assert second["status"] == "cached"
        assert calls == {"market": 1, "financials": 1}
        assert len(rows) == 1 and rows[0].normalised_value_in_crore == Decimal("100")
        assert rows[0].provider == "Yahoo Finance via yfinance"
        assert status.status == "available" and status.record_count == 1
    engine.dispose()


def test_exchange_search_registration_validates_identity_without_live_provider(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    def override_db():
        with Session(engine) as db:
            yield db
    app = main_module.app
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(main_module, "validate_symbol", lambda symbol: {"name": "Example Limited", "sector": "Industrials", "website": "https://example.test", "symbol": symbol})
    monkeypatch.setattr(main_module, "refresh_company_research", lambda db, company: {"status": "success", "ticker": company.ticker})
    try:
        response = TestClient(app).post("/api/research-company", json={"ticker": "EXM", "exchange": "BSE", "name": "Example Limited"})
        assert response.status_code == 200
        body = response.json()
        assert body["ticker"] == "BSE:EXM"
        assert body["yfinance_symbol"] == "EXM.BO"
        assert body["provider_identity"] == "Example Limited"
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_provider_financial_api_exposes_attribution_and_no_filing_page_claim():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        company = Company(name="Example Limited", ticker="NSE:EXM", exchange="NSE", yfinance_symbol="EXM.NS")
        db.add(company); db.flush()
        db.add(ProviderFinancialObservation(company_id=company.id, statement_type="income", period_type="annual", period_end=date(2026, 3, 31), fiscal_year="FY 2025 - 2026", canonical_metric="revenue", reported_label="Total Revenue", reported_value=Decimal("1000000000"), currency="INR", reported_unit="INR as reported by provider", normalised_value_in_crore=Decimal("100"), provider="Yahoo Finance via yfinance", source_url="https://finance.yahoo.com/quote/EXM.NS/financials/"))
        db.commit()
        result = main_module.provider_financials("NSE:EXM", "income", "annual", db)
        assert result["provider"] == "Yahoo Finance via yfinance"
        assert result["items"][0]["normalised_value_in_crore"] == 100
        assert "page_number" not in result["items"][0]
        assert "not guaranteed" in result["basis"]
    engine.dispose()
