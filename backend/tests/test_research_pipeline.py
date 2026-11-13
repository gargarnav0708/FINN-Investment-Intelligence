from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import Base, get_db
from backend.app.main import _results_context, app
from backend.app.models import Announcement, Company, FinancialObservation, SourceDocument
from backend.app.intelligence import classify_announcement
from backend.app.research import (
    calculate_comparison,
    classify_page,
    extract_financial_candidates,
    fiscal_period,
    parse_amounts,
    period_end_from_metadata,
)
from backend.app.seed import seed_companies


def test_fragmented_pdf_numbers_and_negative_values_are_parsed():
    values = parse_amounts("Net Profit/(Loss) (9-10) 431 .10 520.09 1,008.61 951 .1 9 1,745.65")
    assert values == [Decimal("9"), Decimal("-10"), Decimal("431.10"), Decimal("520.09"), Decimal("1008.61"), Decimal("951.19"), Decimal("1745.65")]
    assert parse_amounts("(123.45) - 9.00")[0] == Decimal("-123.45")
    assert parse_amounts("- 9.00") == [None, Decimal("9.00")]
    assert parse_amounts("1.042.83")[0] == Decimal("1042.83")


def test_fiscal_period_uses_statement_date_not_filename():
    assert period_end_from_metadata("FY 2026 - 2027", "Q1") == date(2026, 6, 30)
    assert fiscal_period(date(2025, 9, 30)) == ("FY 2025 - 2026", "Q2")
    assert fiscal_period(date(2026, 3, 31)) == ("FY 2025 - 2026", "Q4")


def test_metric_extraction_keeps_basis_and_header_periods_separate():
    text = """STATEMENT OF CONSOLIDATED FINANCIAL RESULTS FOR THE QUARTER ENDED 30TH JUNE, 2026
Quarter ended
Particulars 30-Jun-26 31-Mar-26 30-Jun-25 31-Mar-26
(₹ crore)
Revenue from Operations 19,051.26 14,900.20 18,035.07 62,428.59
Profit/(Loss) Before Tax (7+8) 1,823.34 1,797.00 1,619.46 6,635.99
Net Profit/(Loss) for the Period (9-10) 1,400.86 1,415.52 1,262.32 5,117.56
STATEMENT OF STANDALONE FINANCIAL RESULTS FOR THE QUARTER ENDED 30TH JUNE, 2026
Quarter ended
Particulars 30-Jun-26 31-Mar-26 30-Jun-25 31-Mar-26
(₹ crore)
Revenue from Operations 5,000.00 4,900.00 4,800.00 20,000.00
Profit/(Loss) Before Tax 100.00 90.00 80.00 400.00
Net Profit/(Loss) for the Period 70.00 60.00 50.00 280.00"""
    document = SimpleNamespace(document_type="quarterly_results", fiscal_year="FY 2026 - 2027", quarter="Q1")
    consolidated_text, standalone_text = text.split("STATEMENT OF STANDALONE", 1)
    rows = extract_financial_candidates(document, [(8, consolidated_text), (16, "STATEMENT OF STANDALONE" + standalone_text)])
    assert len(rows) == 18
    consolidated = [row for row in rows if row["statement_basis"] == "consolidated" and row["period_end"] == date(2026, 6, 30)]
    standalone = [row for row in rows if row["statement_basis"] == "standalone" and row["period_end"] == date(2026, 6, 30)]
    assert {row["canonical_metric"]: row["normalised_value_in_crore"] for row in consolidated} == {
        "revenue": Decimal("19051.26"), "pbt": Decimal("1823.34"), "pat": Decimal("1400.86")
    }
    assert {row["canonical_metric"]: row["normalised_value_in_crore"] for row in standalone}["pat"] == Decimal("70.00")
    assert all(row["extraction_status"] == "needs_review" for row in rows)
    assert all(row["period_type"] == "quarterly" for row in rows)
    assert next(row for row in consolidated if row["canonical_metric"] == "revenue")["reported_value_raw"] == "19,051.26"
    assert classify_page("STATEMENT OF STANDALONE FINANCIAL RESULTS", "quarterly_results") == "financial_results_statement"


def test_comparisons_require_validated_matching_basis_and_nonnegative_base():
    current = SimpleNamespace(statement_basis="consolidated", currency="INR", period_type="quarterly", extraction_status="validated", normalised_value_in_crore=Decimal("120"))
    previous = SimpleNamespace(statement_basis="consolidated", currency="INR", period_type="quarterly", extraction_status="validated", normalised_value_in_crore=Decimal("100"))
    result = calculate_comparison(current, previous, "qoq")
    assert result["absolute_change_crore"] == 20.0
    assert result["percentage_change"] == 20.0
    previous.statement_basis = "standalone"
    assert calculate_comparison(current, previous, "qoq")["status"] == "unavailable"


def test_financial_api_returns_period_basis_and_evidence_reference():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db():
        with Session() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with Session() as db:
            seed_companies(db)
            company = db.scalar(select(Company).where(Company.ticker == "TATAPOWER"))
            source = SourceDocument(company_id=company.id, dedupe_key="test-doc", title="Official results", document_type="quarterly_results", fiscal_year="FY 2026 - 2027", quarter="Q1", landing_page_url="https://example.com", source="fixture")
            db.add(source)
            db.flush()
            db.add(FinancialObservation(
                evidence_key="fixture-observation", company_id=company.id, document_id=source.id,
                canonical_metric="revenue", reported_label="Revenue from Operations", reported_value_raw="100.00",
                numeric_value=Decimal("100"), currency="INR", reported_unit="INR crore",
                normalised_value_in_crore=Decimal("100"), period_start=date(2026, 4, 1), period_end=date(2026, 6, 30),
                fiscal_year="FY 2026 - 2027", fiscal_quarter="Q1", period_type="quarterly", statement_basis="consolidated",
                extraction_method="test_fixture", extraction_status="validated", page_number=8, source_quote="Revenue from Operations 100.00",
                table_title="Consolidated results", row_label="Revenue from Operations", column_header="Quarter ended · 30 Jun 2026", parser_version="test",
            ))
            db.add(FinancialObservation(
                evidence_key="fixture-prior-observation", company_id=company.id, document_id=source.id,
                canonical_metric="revenue", reported_label="Revenue from Operations", reported_value_raw="90.00",
                numeric_value=Decimal("90"), currency="INR", reported_unit="INR crore",
                normalised_value_in_crore=Decimal("90"), period_start=date(2026, 1, 1), period_end=date(2026, 3, 31),
                fiscal_year="FY 2025 - 2026", fiscal_quarter="Q4", period_type="quarterly", statement_basis="consolidated",
                extraction_method="test_fixture", extraction_status="validated", page_number=8, source_quote="Revenue from Operations 90.00",
                table_title="Consolidated results", row_label="Revenue from Operations", column_header="Quarter ended · 31 Mar 2026", parser_version="test",
            ))
            db.commit()
        client = TestClient(app)
        headline = client.get("/api/financials/TATAPOWER/headline").json()
        assert headline["status"] == "available"
        assert headline["fiscal_quarter"] == "Q1"
        revenue = next(metric for metric in headline["metrics"] if metric["metric"] == "revenue")
        assert revenue["observation"]["page_number"] == 8
        assert revenue["observation"]["normalised_value_in_crore"] == 100
        assert revenue["observation"]["local_pdf_url"] == "/api/documents/1/file#page=8"
        assert revenue["qoq"]["status"] == "available"
        assert revenue["qoq"]["percentage_change"] == 100 / 90 * 10
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_results_context_includes_four_validated_metrics_and_period_comparisons():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with Session() as db:
            seed_companies(db)
            company = db.scalar(select(Company).where(Company.ticker == "TATAPOWER"))
            source = SourceDocument(company_id=company.id, dedupe_key="results-context-doc", title="Q2 results source", document_type="quarterly_results", fiscal_year="FY 2026 - 2027", quarter="Q2", landing_page_url="https://example.com/results", source="fixture")
            db.add(source)
            db.flush()
            periods = [
                (date(2026, 7, 1), date(2026, 9, 30), "FY 2026 - 2027", "Q2", 120),
                (date(2026, 4, 1), date(2026, 6, 30), "FY 2026 - 2027", "Q1", 100),
                (date(2025, 7, 1), date(2025, 9, 30), "FY 2025 - 2026", "Q2", 80),
            ]
            for metric_index, metric in enumerate(("revenue", "ebitda", "pbt", "pat")):
                for start, end, fiscal_year, quarter, base in periods:
                    value = base + metric_index * 10
                    db.add(FinancialObservation(
                        evidence_key=f"results-{metric}-{end.isoformat()}", company_id=company.id, document_id=source.id,
                        canonical_metric=metric, reported_label=metric.upper(), reported_value_raw=str(value),
                        numeric_value=Decimal(value), currency="INR", reported_unit="INR crore",
                        normalised_value_in_crore=Decimal(value), period_start=start, period_end=end,
                        fiscal_year=fiscal_year, fiscal_quarter=quarter, period_type="quarterly", statement_basis="consolidated",
                        extraction_method="fixture", extraction_status="validated", page_number=7 + metric_index,
                        source_quote=f"{metric.upper()} {value}", table_title="Consolidated results", row_label=metric.upper(),
                        column_header=f"Quarter ended {end.isoformat()}", parser_version="test",
                    ))
            announcement = Announcement(company_id=company.id, exchange="NSE", external_announcement_id="results-context", dedupe_key="results-context-announcement", subject="Quarterly Financial Results", original_category="Financial Results", announcement_datetime=datetime(2026, 11, 2), source_page_url="https://example.com/results", source="fixture")
            db.add(announcement)
            db.commit()
            context = _results_context(db, announcement, classify_announcement(announcement))
            assert context["status"] == "available"
            assert context["statement_basis"] == "consolidated"
            assert set(context["metrics"]) == {"revenue", "ebitda", "pbt", "pat"}
            revenue = context["metrics"]["revenue"]
            assert revenue["value_in_crore"] == 120
            assert revenue["qoq"]["previous_value_in_crore"] == 100
            assert revenue["qoq"]["percentage_change"] == 20
            assert revenue["yoy"]["previous_value_in_crore"] == 80
            assert revenue["yoy"]["percentage_change"] == 50
            assert revenue["document_title"] == "Q2 results source"
            assert revenue["page_number"] == 7
    finally:
        engine.dispose()
