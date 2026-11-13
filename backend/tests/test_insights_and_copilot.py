"""Offline tests: taxonomy, summaries, management profiles, business snapshot, bull/bear, copilot, scheduler."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app import announcement_intel, company_insights, copilot, llm
from backend.app.database import Base
from backend.app.intelligence import classify_announcement
from backend.app.models import (Announcement, AnnouncementInsight, Company, CompanyInsight, DocumentPage, PortfolioHolding,
                                SourceDocument)
from backend.app.scheduler import due


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("FINN_LLM_API_KEY", "test-key")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    with Session(engine) as session:
        yield session


def fake_llm(monkeypatch, replies):
    calls = []

    def fake(db, **kw):
        calls.append(kw)
        r = replies[kw["purpose"]]
        return (r(kw) if callable(r) else r), {"model": "fake-" + kw["tier"], "tier": kw["tier"], "cached": False}

    monkeypatch.setattr(llm, "complete_json", fake)
    return calls


def company(db, ticker="TITAN", name="Titan Company Limited", holding=True):
    c = Company(name=name, ticker=ticker, exchange="NSE", yfinance_symbol=f"{ticker}.NS")
    db.add(c); db.flush()
    if holding:
        db.add(PortfolioHolding(company_id=c.id, is_active=True))
    db.commit()
    return c


def announcement(db, c, subject, details=None, hours=2, key=None):
    a = Announcement(company_id=c.id, exchange="NSE", dedupe_key=key or subject[:60], subject=subject, details=details,
                     announcement_datetime=datetime.now(timezone.utc) - timedelta(hours=hours), source_page_url="https://www.nseindia.com", source="NSE")
    db.add(a); db.commit()
    return a


# --- taxonomy -------------------------------------------------------------------------------

@pytest.mark.parametrize("subject,details,category,materiality", [
    ("Disclosure under Regulation 31 - creation of pledge", None, "pledging", "high"),
    ("Disclosure under SEBI (Prohibition of Insider Trading) Regulation 7(2)", "acquisition of shares by promoter", "insider_trading", "medium"),
    ("Appointment of Statutory Auditor", None, "unclassified", "routine"),
    ("Resignation of Statutory Auditor", None, "management_change", "high"),
    ("Credit Rating", "CRISIL downgrade to AA-", "credit_rating", "high"),
    ("Newspaper Publication", "publication of financial results", "results", "routine"),
    ("Board approves capex of Rs 1,200 crore for a new plant", None, "capex", "high"),
    ("Closure of Trading Window", None, "unclassified", "routine"),
    ("Analysts/Institutional Investor Meet/Con. Call Updates", None, "investor_meetings", "routine"),
])
def test_keyword_taxonomy(subject, details, category, materiality):
    result = classify_announcement(Announcement(subject=subject, details=details))
    assert (result["category"], result["materiality"]) == (category, materiality)


def test_batch_classification_with_summaries_and_fallback(db, monkeypatch):
    c = company(db)
    a1 = announcement(db, c, "Bagging/Receiving of orders/contracts", "Order worth Rs 450 crore from Indian Railways", key="a1")
    a2 = announcement(db, c, "Shareholders meeting", "Notice of postal ballot", key="a2")
    a3 = announcement(db, c, "Something odd", "text", key="a3")
    fake_llm(monkeypatch, {"announcement_classify": {"items": [
        {"id": a1.id, "category": "press_release", "materiality": "high", "summary": "Rs 450 cr order from Indian Railways (~3% of FY26 revenue).", "reason": "Sizeable order"},
        {"id": a2.id, "category": "postal_ballot", "materiality": "routine", "summary": "should be dropped"},
        {"id": a3.id, "category": "made_up_category", "materiality": "high", "summary": "x"},
    ]}})
    report = announcement_intel.enrich_announcements(db, days=7, profiles=False)
    assert report["classified"] == 3
    db.expire_all()
    i1, i2, i3 = (db.get(Announcement, a.id).insight for a in (a1, a2, a3))
    assert i1.method == "llm" and i1.summary.startswith("Rs 450 cr") and i1.materiality == "high"
    assert i2.summary is None  # routine items get no summary
    assert i3.method == "keyword"  # invalid model category -> keyword fallback
    c1 = classify_announcement(db.get(Announcement, a1.id))
    assert c1["category_label"] == "Press release" and c1["summary"]


def test_management_profile_grounded_and_enriched(db, monkeypatch):
    c = company(db)
    details = ("Appointment of Mr. Ashok Sonthalia as Chief Financial Officer w.e.f. 1 December 2026. Brief profile: "
               "Mr. Sonthalia is a Chartered Accountant with over 30 years of experience in finance, most recently CFO of Tata Consumer Products.")
    a = announcement(db, c, "Change in Key Managerial Personnel", details)
    db.add(AnnouncementInsight(announcement_id=a.id, category="management_change", materiality="high", method="llm")); db.commit()
    fake_llm(monkeypatch, {
        "mgmt_profile": {"people": [
            {"name": "Ashok Sonthalia", "role": "Chief Financial Officer", "change": "appointed", "effective_date": "2026-12-01",
             "profile": "Mr. Sonthalia is a Chartered Accountant with over 30 years of experience in finance, most recently CFO of Tata Consumer Products.", "page": 0},
            {"name": "Imaginary Person", "role": "Director", "change": "appointed", "profile": "Invented."},
        ]},
        "mgmt_background": {"background": "Former CFO at Tata Consumer Products; earlier finance roles at Hindustan Unilever.", "used": [0]},
    })
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    monkeypatch.setattr(announcement_intel, "web_search", lambda q, limit=5: [
        {"title": "Ashok Sonthalia - CFO - LinkedIn", "url": "https://www.linkedin.com/in/example", "snippet": "Ashok Sonthalia, CFO, Tata Consumer Products"},
        {"title": "Unrelated", "url": "https://x.example", "snippet": "someone else"}])
    announcement_intel.enrich_announcements(db, days=7, profiles=True)
    db.expire_all()
    people = db.get(Announcement, a.id).insight.people
    assert [p["name"] for p in people] == ["Ashok Sonthalia"]  # invented person not in filing -> dropped
    assert people[0]["profile"].startswith("Mr. Sonthalia") and "Tata Consumer" in people[0]["background"]
    assert people[0]["background_sources"][0]["url"].startswith("https://www.linkedin.com")


# --- company insights ------------------------------------------------------------------------

AR_P12 = """Business overview and operating segments. Segment revenue (Rs crore) FY26:
Jewellery 52,446.10 ; Watches and Wearables 4,512.30 ; Eyecare 812.40 ; Others 1,203.00"""
AR_P40 = """Management discussion and analysis. Our business: the company designs and retails jewellery, watches and eyewear.
Segment performance, segment revenue growth, our business segments and products across customers in India."""


def _annual_report(db, c):
    doc = SourceDocument(company_id=c.id, dedupe_key="ar", title="Annual Report FY2026", document_type="annual_report", document_date=date(2026, 3, 31),
                         landing_page_url="https://www.screener.in", local_file_path="data/documents/TITAN/ar.pdf", source="test")
    db.add(doc); db.flush()
    for n, text in ((12, AR_P12), (40, AR_P40), (41, "Statutory reports. " * 40)):
        db.add(DocumentPage(document_id=doc.id, page_number=n, extracted_text=text, parser_version="t", content_hash=str(n)))
    db.commit()
    return doc


def test_business_snapshot_keeps_only_verified_segment_numbers(db, monkeypatch):
    c = company(db)
    _annual_report(db, c)
    fake_llm(monkeypatch, {"business_snapshot": {"description": "Titan designs and retails jewellery, watches and eyewear in India.", "fiscal_year": "FY26",
        "segments": [{"name": "Jewellery", "what": "Tanishq, Mia, Zoya", "revenue": "52,446.10", "unit": "crore", "page": 12},
                     {"name": "Watches and Wearables", "revenue": 4512.3, "unit": "crore", "page": 12},
                     {"name": "Eyecare", "revenue": 999.0, "unit": "crore", "page": 12}]}})
    snap = company_insights.build_business_snapshot(db, c)
    segs = {s["name"]: s for s in snap["segments"]}
    assert segs["Jewellery"]["revenue_crore"] == 52446.10 and segs["Jewellery"]["page"] == 12
    assert segs["Eyecare"]["revenue_crore"] is None  # 999 is not on the page
    assert segs["Jewellery"]["share_pct"] == pytest.approx(52446.10 / (52446.10 + 4512.30) * 100, abs=0.1)
    assert 12 in snap["pages_read"]


def test_bull_bear_drops_ungrounded_points(db, monkeypatch):
    c = company(db)
    doc = _annual_report(db, c)
    for n, text in ((43, "Risks: gold price volatility and competition from organised and unorganised jewellers could pressure margins and demand. " * 4),
                    (44, "Growth: we expanded retail capacity, adding 120 stores, and demand for studded jewellery grew strongly, driving market share gains. " * 4)):
        db.add(DocumentPage(document_id=doc.id, page_number=n, extracted_text=text, parser_version="t", content_hash=str(n)))
    db.commit()
    point = lambda q, p: {"point": "x", "quote": q, "doc": doc.id, "page": p}
    fake_llm(monkeypatch, {"bull_bear": {
        "bull": [point("we expanded retail capacity, adding 120 stores", 44), point("demand for studded jewellery grew strongly", 44), point("we will double profits", 44)],
        "bear": [point("gold price volatility and competition from organised and unorganised jewellers", 43), point("could pressure margins and demand", 43)],
        "verdict": "Store productivity decides."}})
    bb = company_insights.build_bull_bear(db, c)
    assert len(bb["bull"]) == 2 and len(bb["bear"]) == 2 and bb["dropped_ungrounded"] == 1
    assert bb["status"] == "available" and bb["bull"][0]["source_url"].endswith("#page=44")
    stored = db.scalar(select(CompanyInsight).where(CompanyInsight.kind == "bull_bear"))
    assert stored.status == "available"


# --- copilot & scheduler ---------------------------------------------------------------------

def test_copilot_routes_company_questions_and_cites_portfolio_items(db, monkeypatch):
    titan = company(db)
    company(db, "TATAPOWER", "The Tata Power Company Limited")
    assert copilot.resolve_company(db, "how did titan do on margins?", None).ticker == "TITAN"
    assert copilot.resolve_company(db, "what changed at TATAPOWER", None).ticker == "TATAPOWER"
    assert copilot.resolve_company(db, "anything material today?", None) is None
    a = announcement(db, titan, "Financial Results", "Q2 results")
    db.add(AnnouncementInsight(announcement_id=a.id, category="results", materiality="high", summary="Q2 revenue +21% YoY; margin 11.2%.", method="llm")); db.commit()
    fake_llm(monkeypatch, {"copilot": {"answer": f"Titan reported Q2: revenue +21% YoY [A{a.id}] and a made-up item [A999].", "used": [f"A{a.id}", "A999"]}})
    out = copilot.ask(db, "What were the biggest developments this week?")
    assert out["scope"] == "portfolio" and [c["ref"] for c in out["citations"]] == [f"A{a.id}"]
    assert "[A999]" not in out["answer"]


def test_scheduler_due_only_on_weekdays_after_close():
    from zoneinfo import ZoneInfo
    from datetime import time as dtime
    ist = ZoneInfo("Asia/Kolkata")
    run_at = dtime(16, 45)
    assert due(datetime(2026, 11, 13, 17, 0, tzinfo=ist), None, run_at)           # Friday after close
    assert not due(datetime(2026, 11, 13, 15, 0, tzinfo=ist), None, run_at)       # before close
    assert not due(datetime(2026, 11, 14, 17, 0, tzinfo=ist), None, run_at)       # Saturday
    assert not due(datetime(2026, 11, 13, 17, 0, tzinfo=ist), date(2026, 11, 13), run_at)  # already ran today
