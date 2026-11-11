from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.database import Base
from backend.app.models import Company, FinancialObservation, SourceDocument
from backend.app.thesis_monitor import build_thesis_drift


def _observation(company, document, metric, value, end):
    year = f"FY {end.year - 1} - {end.year}"
    return FinancialObservation(
        evidence_key=f"{company.ticker}-{metric}-{end.isoformat()}", company_id=company.id,
        document_id=document.id, canonical_metric=metric, reported_label=metric,
        reported_value_raw=str(value), numeric_value=Decimal(str(value)), currency="INR",
        reported_unit="INR crore", normalised_value_in_crore=Decimal(str(value)),
        period_start=date(end.year - 1, 4, 1), period_end=end, fiscal_year=year, fiscal_quarter="FY",
        period_type="annual", statement_basis="consolidated", extraction_method="test",
        extraction_status="validated", page_number=12, source_quote=f"{metric}: {value}",
        table_title="Consolidated statement", row_label=metric, column_header=year,
        parser_version="test",
    )


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def test_thesis_drift_uses_validated_period_matched_observations_and_source_pages():
    engine = _db()
    with Session(engine) as db:
        company = Company(name="Example Power", ticker="EXM", exchange="NSE", yfinance_symbol="EXM.NS")
        db.add(company); db.flush()
        observations = []
        for end, factor in ((date(2025, 3, 31), 1), (date(2026, 3, 31), 1.1)):
            year = f"FY {end.year - 1} - {end.year}"
            doc = SourceDocument(company_id=company.id, dedupe_key=f"annual-{end.year}", title=f"Annual report {year}",
                document_type="annual_report", fiscal_year=year, landing_page_url="https://example.com/report",
                source="test")
            db.add(doc); db.flush()
            values = {"revenue": 1000 * factor, "pat": 100 * factor, "pbt": 130 * factor,
                "cash_from_operations": 150 * factor, "capital_expenditure_ppe": -30 * factor,
                "borrowings_noncurrent": 200 * factor, "borrowings_current": 50 * factor,
                "cash_and_cash_equivalents": 40 * factor, "total_equity": 500 * factor,
                "current_assets": 400 * factor, "current_liabilities": 200 * factor}
            observations.extend(_observation(company, doc, metric, value, end) for metric, value in values.items())
        db.add_all(observations); db.commit()

        result = build_thesis_drift(db, company, periods=3)
        groups = {group["code"]: group for group in result["signal_groups"]}
        earnings = {signal["code"]: signal for signal in groups["earnings_quality"]["signals"]}
        cash = {signal["code"]: signal for signal in groups["cash_conversion"]["signals"]}
        balance = {signal["code"]: signal for signal in groups["balance_sheet"]["signals"]}

        assert earnings["revenue_pat_growth"]["status"] == "no_material_change"
        assert earnings["pat_margin"]["current_value"] == 10.0
        assert earnings["pat_margin"]["supporting_observations"]
        assert cash["free_cash_flow"]["current_value"] == 132.0
        assert cash["free_cash_flow"]["status"] == "improving"
        assert balance["net_debt"]["current_value"] == 231.0
        assert balance["debt_equity"]["current_value"] == 0.5
        assert groups["working_capital"]["coverage_state"] == "insufficient_data"
        assert all(item["document_id"] and item["page_number"] == 12 for item in balance["net_debt"]["supporting_observations"])
        other = Company(name="Other Issuer", ticker="OTH", exchange="NSE", yfinance_symbol="OTH.NS")
        db.add(other); db.commit()
        isolated = build_thesis_drift(db, other)
        assert isolated["coverage"]["validated_observation_count"] == 0
        assert isolated["coverage"]["available_groups"] == 0
    engine.dispose()


def test_thesis_drift_does_not_promote_missing_company_facts():
    engine = _db()
    with Session(engine) as db:
        company = Company(name="No Data", ticker="EMPTY", exchange="NSE", yfinance_symbol="EMPTY.NS")
        db.add(company); db.commit()
        result = build_thesis_drift(db, company)
        assert result["coverage"]["coverage_state"] == "insufficient_data"
        assert result["coverage"]["available_groups"] == 0
        assert all(group["coverage_state"] == "insufficient_data" for group in result["signal_groups"])
    engine.dispose()
