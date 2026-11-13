"""Offline tests for the model router, results extraction, screener.in parsing and narrative vs numbers."""
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app import guidance, llm, results_extraction
from backend.app.database import Base
from backend.app.models import (Announcement, CommitmentAssessment, Company, DocumentPage, FinancialObservation, LLMCall,
                                ManagementStatement, ProviderFinancialObservation, ResultsExtraction, SourceDocument)
from backend.app.pipeline import store_screener_rows
from backend.app.providers import screener

FIXTURES = Path(__file__).parent / "fixtures"

RESULTS_PAGE = """Statement of Consolidated Unaudited Financial Results for the quarter ended 30 September 2026
(Rs. in crore)
Particulars                        Quarter ended   Quarter ended   Quarter ended   Half year ended
                                   30.09.2026      30.06.2026      30.09.2025      30.09.2026
1 Revenue from operations          16,100.40       14,200.10       14,855.00       30,300.50
2 Other income                     14.20           10.10           8.00            24.30
3 Total income                     16,114.60       14,210.20       14,863.00       30,324.80
4 Expenses
  Total expenses                   15,521.60       13,726.10       14,371.00       29,247.70
  Finance costs                    39.00           41.00           42.00           80.00
  Depreciation and amortisation    80.00           75.00           66.00           155.00
5 Profit before tax                593.00          484.10          492.00          1,077.10
6 Tax expense                      148.00          122.10          80.00           270.10
7 Profit for the period            445.00          362.00          412.00          807.00
"""

GOOD_LLM = {
    "statement_basis": "consolidated", "unit": "crore", "is_financial_company": False,
    "columns": [
        {"role": "current", "header": "Quarter ended 30.09.2026", "period_end": "2026-09-30"},
        {"role": "previous_quarter", "header": "Quarter ended 30.06.2026", "period_end": "2026-06-30"},
        {"role": "year_ago_quarter", "header": "Quarter ended 30.09.2025", "period_end": "2025-09-30"},
    ],
    "rows": {
        "revenue": {"label": "Revenue from operations", "values": ["16,100.40", "14,200.10", "14,855.00"]},
        "other_income": {"label": "Other income", "values": [14.2, 10.1, 8.0]},
        "total_expenses": {"label": "Total expenses", "values": [15521.6, 13726.1, 14371.0]},
        "finance_costs": {"label": "Finance costs", "values": [39, 41, 42]},
        "depreciation": {"label": "Depreciation and amortisation", "values": [80, 75, 66]},
        "pbt": {"label": "Profit before tax", "values": [593, 484.1, 492]},
        "tax_expense": {"label": "Tax expense", "values": [148, 122.1, 80]},
        "pat": {"label": "Profit for the period", "values": [445, 362, 412]},
    },
}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("FINN_LLM_API_KEY", "test-key")
    with Session(engine) as session:
        yield session


def _company(db, ticker="DIXON"):
    company = Company(name="Dixon Technologies", ticker=ticker, exchange="NSE", yfinance_symbol=f"{ticker}.NS")
    db.add(company); db.commit()
    return company


def _results_document(db, company, text=RESULTS_PAGE):
    doc = SourceDocument(company_id=company.id, dedupe_key="d1", title="Outcome of Board Meeting - Financial Results", document_type="quarterly_results",
                         landing_page_url="https://www.nseindia.com", download_url="https://nsearchives.nseindia.com/r.pdf", source="NSE exchange filing")
    db.add(doc); db.flush()
    db.add(DocumentPage(document_id=doc.id, page_number=1, extracted_text="Cover letter to the exchanges. Please find enclosed results.", parser_version="t", content_hash="a"))
    db.add(DocumentPage(document_id=doc.id, page_number=2, extracted_text=text, parser_version="t", content_hash="b"))
    db.commit()
    return doc


def _fake_llm(monkeypatch, replies):
    calls = []

    def fake(db, **kwargs):
        calls.append(kwargs)
        reply = replies[kwargs["purpose"]]
        return (reply(kwargs) if callable(reply) else reply), {"model": "fake-" + kwargs["tier"], "tier": kwargs["tier"], "cached": False}

    monkeypatch.setattr(llm, "complete_json", fake)
    return calls


# --- router ---------------------------------------------------------------------------------

def test_router_caches_by_input_and_records_usage(db, monkeypatch):
    sent = []

    def fake_post(config, messages, max_tokens, timeout):
        sent.append(config.model)
        return {"ok": True}, {"prompt_tokens": 100, "completion_tokens": 10}

    monkeypatch.setattr(llm, "_post", fake_post)
    first, meta1 = llm.complete_json(db, tier="fast", purpose="t", prompt_version="v1", system="s", user="u")
    second, meta2 = llm.complete_json(db, tier="fast", purpose="t", prompt_version="v1", system="s", user="u")
    assert first == second == {"ok": True}
    assert meta1["cached"] is False and meta2["cached"] is True
    assert sent == ["gemini-3.5-flash-lite"]
    summary = llm.usage_summary(db)
    assert summary["total_calls"] == 1 and summary["total_cache_hits"] == 1 and summary["input_tokens"] == 100


def test_strong_tier_degrades_to_fast_when_quota_exhausted(db, monkeypatch):
    def fake_post(config, messages, max_tokens, timeout):
        if config.tier == "strong":
            raise llm.LLMUnavailable("HTTP 429")
        return {"ok": "fast"}, {}

    monkeypatch.setattr(llm, "_post", fake_post)
    result, meta = llm.complete_json(db, tier="strong", purpose="t", prompt_version="v1", system="s", user="u2")
    assert result == {"ok": "fast"} and meta["degraded_from"] == "gemini-3.8-flash"


def test_router_disabled_without_key(db, monkeypatch):
    monkeypatch.delenv("FINN_LLM_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert llm.is_configured() is False
    with pytest.raises(llm.LLMUnavailable):
        llm.complete_json(db, tier="fast", purpose="t", prompt_version="v1", system="s", user="u")


def test_parse_json_tolerates_fences():
    assert llm.parse_json_text('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.parse_json_text('prefix {"a": [1,2]} suffix') == {"a": [1, 2]}


# --- results extraction ---------------------------------------------------------------------

def test_statement_page_selection_skips_cover_letter():
    pages = [(1, "Cover letter to the exchanges. " * 20), (2, RESULTS_PAGE)]
    chosen = results_extraction.select_statement_pages(pages)
    assert chosen and chosen[0][0] == 2 and chosen[0][2] == "consolidated"


def test_results_extraction_verifies_and_writes_validated_observations(db, monkeypatch):
    company = _company(db)
    doc = _results_document(db, company)
    _fake_llm(monkeypatch, {"results_headline": GOOD_LLM})
    record = results_extraction.extract_results_document(db, doc)
    assert record.status == "verified", record.checks
    assert record.period_end == date(2026, 9, 30) and record.page_number == 2
    payload = results_extraction.headline_payload(db, record)
    revenue = payload["metrics"]["revenue"]
    assert revenue["value_in_crore"] == pytest.approx(16100.40)
    assert revenue["qoq"]["percentage_change"] == pytest.approx((16100.40 / 14200.10 - 1) * 100)
    assert revenue["yoy"]["percentage_change"] == pytest.approx((16100.40 / 14855.0 - 1) * 100)
    # EBITDA derived = revenue - total expenses + finance costs + D&A
    assert payload["metrics"]["ebitda"]["value_in_crore"] == pytest.approx(16100.40 - 15521.60 + 39 + 80)
    assert payload["fiscal_quarter"] == "Q2" and payload["verification"] == "verified"
    rows = db.scalars(select(FinancialObservation).where(FinancialObservation.document_id == doc.id)).all()
    assert len(rows) == 12 and all(r.extraction_status == "validated" and r.reviewer == "finn-auto-check" for r in rows)
    assert {r.period_end for r in rows} == {date(2026, 9, 30), date(2026, 6, 30), date(2025, 9, 30)}


def test_hallucinated_number_is_marked_unverified_not_hidden(db, monkeypatch):
    company = _company(db)
    doc = _results_document(db, company)
    bad = {**GOOD_LLM, "rows": {**GOOD_LLM["rows"], "revenue": {"label": "Revenue from operations", "values": [16999.0, 14200.10, 14855.0]}}}
    _fake_llm(monkeypatch, {"results_headline": bad})
    record = results_extraction.extract_results_document(db, doc)
    assert record.status == "unverified"
    failed = [c["check"] for c in record.checks if not c["passed"]]
    assert "numbers_on_page" in failed
    payload = results_extraction.headline_payload(db, record)
    assert payload["status"] == "available" and payload["verification"] == "unverified"
    assert all(r.extraction_status == "needs_review" for r in db.scalars(select(FinancialObservation)).all())


def test_reconciliation_failure_is_caught(db, monkeypatch):
    company = _company(db)
    doc = _results_document(db, company)
    bad = {**GOOD_LLM, "rows": {**GOOD_LLM["rows"], "tax_expense": {"label": "Tax expense", "values": [80, 122.1, 80]}}}
    _fake_llm(monkeypatch, {"results_headline": bad})
    record = results_extraction.extract_results_document(db, doc)
    assert record.status == "unverified"
    assert [c for c in record.checks if c["check"] == "pbt_minus_tax_equals_pat"][0]["passed"] is False


def test_lakh_units_are_normalised_to_crore(db, monkeypatch):
    company = _company(db)
    doc = _results_document(db, company)
    _fake_llm(monkeypatch, {"results_headline": {**GOOD_LLM, "unit": "lakh"}})
    record = results_extraction.extract_results_document(db, doc)
    assert results_extraction.headline_payload(db, record)["metrics"]["pat"]["value_in_crore"] == pytest.approx(4.45)


def test_digest_uses_machine_read_results(db, monkeypatch):
    company = _company(db)
    doc = _results_document(db, company)
    _fake_llm(monkeypatch, {"results_headline": GOOD_LLM})
    results_extraction.extract_results_document(db, doc)
    ann = Announcement(company_id=company.id, exchange="NSE", dedupe_key="a1", subject="Outcome of Board Meeting", details="approved the unaudited financial results",
                       announcement_datetime=datetime(2026, 11, 5, 15, tzinfo=timezone.utc), attachment_url=doc.download_url,
                       source_page_url="https://www.nseindia.com", source="NSE")
    db.add(ann); db.commit()
    context = results_extraction.results_context_for_announcement(db, ann)
    assert context["metrics"]["pat"]["yoy"]["status"] == "available"


# --- screener.in ----------------------------------------------------------------------------

def test_screener_page_parsing():
    html = (FIXTURES / "screener_company.html").read_text()
    page = screener.parse_company_page(html, "DIXON", "https://www.screener.in/company/DIXON/consolidated/")
    assert page.basis == "consolidated" and page.name.startswith("Dixon")
    transcripts = [d for d in page.documents if d.document_type == "earnings_call_transcript"]
    reports = [d for d in page.documents if d.document_type == "annual_report"]
    assert [d.document_date for d in transcripts] == [date(2026, 10, 31), date(2026, 7, 31)]
    assert reports[0].fiscal_year == "FY 2025 - 2026" and reports[0].url.endswith("1001.pdf")
    assert [r.document_date.year for r in reports] == [2026, 2025, 2024]  # "Annual Report 2024" label format too
    assert not any(d.url.endswith(".mp3") for d in page.documents)
    sales = [r for r in page.rows if r.section == "quarters" and r.metric == "revenue"]
    assert len(sales) == 6 and sales[-1].period_end == date(2026, 9, 30) and sales[-1].value == Decimal("16100")
    assert not any(r.period_end is None for r in page.rows)  # TTM column skipped
    assert any(r.metric == "cfo" and r.value == Decimal("1420") for r in page.rows)


def test_screener_rows_override_yahoo_rows(db):
    company = _company(db)
    db.add(ProviderFinancialObservation(company_id=company.id, statement_type="income", period_type="quarterly", period_end=date(2026, 9, 30),
                                        fiscal_year="FY 2026 - 2027", canonical_metric="revenue", reported_label="Total Revenue",
                                        reported_value=Decimal("1"), provider="Yahoo Finance via yfinance"))
    db.commit()
    page = screener.parse_company_page((FIXTURES / "screener_company.html").read_text(), "DIXON", "https://www.screener.in/company/DIXON/consolidated/")
    assert store_screener_rows(db, company, page) > 20
    row = db.scalar(select(ProviderFinancialObservation).where(ProviderFinancialObservation.period_end == date(2026, 9, 30), ProviderFinancialObservation.canonical_metric == "revenue", ProviderFinancialObservation.period_type == "quarterly"))
    assert row.provider == "screener.in" and row.reported_value == Decimal("16100")


# --- narrative vs numbers -------------------------------------------------------------------

TRANSCRIPT_P3 = """Atul Lall: Thank you. For the full year FY27 we expect revenue growth of 30% to 35% and
our EBITDA margin should remain around 4% as mobile volumes scale up.
Moderator: The next question is from the line of an analyst.
Analyst: Will margins improve to 6% next year?"""
TRANSCRIPT_P4 = """Saurabh Gupta: Our capex for this year will be about Rs 1,200 crore, largely for the display fab."""


def _transcript(db, company):
    doc = SourceDocument(company_id=company.id, dedupe_key="t1", title="Earnings call transcript Oct 2026", document_type="earnings_call_transcript",
                         document_date=date(2026, 10, 31), landing_page_url="https://www.screener.in", download_url="https://www.bseindia.com/t.pdf",
                         local_file_path="data/documents/DIXON/t.pdf", source="Company filing (linked via screener.in)")
    db.add(doc); db.flush()
    db.add(DocumentPage(document_id=doc.id, page_number=3, extracted_text=TRANSCRIPT_P3, parser_version="t", content_hash="c"))
    db.add(DocumentPage(document_id=doc.id, page_number=4, extracted_text=TRANSCRIPT_P4, parser_version="t", content_hash="d"))
    db.commit()
    return doc


def test_guidance_quotes_must_be_verbatim(db, monkeypatch):
    company = _company(db)
    doc = _transcript(db, company)
    reply = {"call_date": "2026-10-28", "quarter_discussed": "Q2 FY27", "guidance": [
        {"speaker": "Atul Lall", "designation": "Vice Chairman & MD", "page": 3, "quote": "For the full year FY27 we expect revenue growth of 30% to 35%",
         "metric": "revenue_growth", "kind": "quantitative", "target_low": 30, "target_high": 35, "unit": "%", "target_period": "FY27", "lifecycle": "new"},
        # right quote, wrong page: grounded to page 4
        {"speaker": "Saurabh Gupta", "designation": "CFO", "page": 3, "quote": "Our capex for this year will be about Rs 1,200 crore",
         "metric": "capex", "kind": "quantitative", "target_low": 1200, "unit": "crore", "target_period": "FY27", "lifecycle": "new"},
        # paraphrase, not in transcript: dropped
        {"speaker": "Atul Lall", "page": 3, "quote": "We will double our margins to 8 percent within two years", "metric": "ebitda_margin"},
    ]}
    calls = _fake_llm(monkeypatch, {"guidance_extract": reply})
    result = guidance.extract_guidance(db, company, doc)
    assert result["inserted"] == 2 and result["dropped_ungrounded"] == 1
    assert "[[PAGE 3]]" in calls[0]["user"] and calls[0]["tier"] == "fast"
    capex = db.scalar(select(ManagementStatement).where(ManagementStatement.metric_name == "capex"))
    assert capex.page_number == 4 and capex.review_status == "approved" and capex.transcript_date == date(2026, 10, 28)
    assert "Atul" not in guidance.EXTRACT_SYSTEM  # no hard-coded speakers


def test_narrative_assessment_checks_cited_numbers(db, monkeypatch):
    company = _company(db)
    doc = _transcript(db, company)
    page = screener.parse_company_page((FIXTURES / "screener_company.html").read_text(), "DIXON", "https://www.screener.in/company/DIXON/consolidated/")
    store_screener_rows(db, company, page)
    _fake_llm(monkeypatch, {"guidance_extract": {"guidance": [
        {"speaker": "Atul Lall", "page": 3, "quote": "For the full year FY27 we expect revenue growth of 30% to 35%", "metric": "revenue_growth", "target_low": 30, "target_high": 35, "unit": "%", "target_period": "FY27"},
        {"speaker": "Atul Lall", "page": 3, "quote": "our EBITDA margin should remain around 4% as mobile volumes scale up", "metric": "ebitda_margin", "target_low": 4, "unit": "%", "target_period": "FY27"},
    ]}})
    guidance.extract_guidance(db, company, doc)
    table = guidance.delivered_table(db, company)
    q2 = [r for r in table["quarterly"] if r["period"] == "Q2 FY27"][0]
    assert q2["revenue_growth_pct"] == pytest.approx(round((16100 / 14855 - 1) * 100, 2))
    assert q2["ebitda_margin_pct"] == pytest.approx(round(698 / 16100 * 100, 2))
    ids = [s.id for s in db.scalars(select(ManagementStatement).order_by(ManagementStatement.id)).all()]

    def assess(kwargs):
        assert kwargs["tier"] == "strong" and "Q2 FY27" in kwargs["user"]
        return {"items": [
            {"id": ids[0], "verdict": "pending", "actual": "H1 growth tracking ~8%", "explanation": "FY27 not finished", "numbers_used": []},
            {"id": ids[1], "verdict": "delivered", "actual": "Q2 FY27 margin 4.34%", "explanation": "In line",
             "numbers_used": [{"metric": "ebitda_margin_pct", "period": "Q2 FY27", "value": q2["ebitda_margin_pct"]}]},
        ], "scorecard": {"credibility": "mixed", "headline": "Margins as guided; growth still pending."}}

    _fake_llm(monkeypatch, {"narrative_assess": assess})
    payload = guidance.assess_narrative(db, company)
    assert payload["counts"] == {"pending": 1, "delivered": 1}
    statuses = {a.statement_id: a.status for a in db.scalars(select(CommitmentAssessment)).all()}
    assert statuses[ids[1]] == "achieved" and statuses[ids[0]] == "in_progress"

    def wrong_numbers(kwargs):
        return {"items": [{"id": ids[1], "verdict": "missed", "actual": "x", "explanation": "y",
                           "numbers_used": [{"metric": "ebitda_margin_pct", "period": "Q2 FY27", "value": 2.1}]}], "scorecard": {}}

    _fake_llm(monkeypatch, {"narrative_assess": wrong_numbers})
    payload = guidance.assess_narrative(db, company, force=True)
    assert payload["verdicts"][0]["verdict"] == "unverified" and payload["verdicts"][0]["numbers_checked"] is False
    view = guidance.narrative_payload(db, company)
    assert view["calls"][0]["items"][0]["quote"] and view["delivered"]["latest_quarter"] == "Q2 FY27"


def test_400_drops_optional_parameters_and_retries(db, monkeypatch):
    seen = []

    class Resp:
        def __init__(self, code, body):
            self.status_code, self._body, self.text, self.headers = code, body, str(body), {}
        def json(self):
            return self._body

    def fake_post(url, json, headers, timeout):
        seen.append(set(json))
        if "reasoning_effort" in json or "response_format" in json:
            return Resp(400, {"error": {"message": "Request contains an invalid argument."}})
        return Resp(200, {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {}})

    monkeypatch.setattr(llm.requests, "post", fake_post)
    monkeypatch.setattr(llm, "_pace", lambda config: None)
    result, _ = llm.complete_json(db, tier="fast", purpose="t", prompt_version="v1", system="s", user="retry")
    assert result == {"ok": True}
    assert "reasoning_effort" in seen[0] and "response_format" not in seen[-1]


def test_overloaded_model_falls_back_to_next_model(db, monkeypatch):
    tried = []

    def fake_post(config, messages, max_tokens, timeout):
        tried.append(config.model)
        if config.model == "gemini-3.5-flash-lite":
            raise llm.LLMUnavailable("Model provider unavailable after retries (HTTP 503: high demand)")
        return {"ok": config.model}, {}

    monkeypatch.setattr(llm, "_post", fake_post)
    result, meta = llm.complete_json(db, tier="fast", purpose="t", prompt_version="v1", system="s", user="busy")
    assert tried == ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    assert meta["model"] == "gemini-3.1-flash-lite" and result == {"ok": "gemini-3.1-flash-lite"}
    # cached under the primary key: the retry is free
    again, meta2 = llm.complete_json(db, tier="fast", purpose="t", prompt_version="v1", system="s", user="busy")
    assert meta2["cached"] is True and len(tried) == 2


def test_strong_failure_reason_is_reported_when_degrading(db, monkeypatch):
    def fake_post(config, messages, max_tokens, timeout):
        if config.tier == "strong":
            raise llm.LLMUnavailable("HTTP 429: quota exceeded")
        return {"ok": 1}, {}

    monkeypatch.setattr(llm, "_post", fake_post)
    _, meta = llm.complete_json(db, tier="strong", purpose="t", prompt_version="v1", system="s", user="quota")
    assert meta["degraded_from"] == "gemini-3.8-flash" and "429" in meta["degraded_reason"]
