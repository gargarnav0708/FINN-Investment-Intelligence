from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import Base, get_db
from backend.app.main import app
from backend.app.management import extract_candidates, assess_commitments
from backend.app.models import (Company, DocumentPage, ManagementStatement, SourceDocument,
                                CommitmentAssessment)
from backend.app.seed import seed_companies


def test_management_candidates_are_source_exact_repeat_safe_and_review_gated():
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
            doc = SourceDocument(company_id=company.id, dedupe_key="transcript-one", title="Q1 FY27 Earnings Call Transcript",
                document_type="earnings_call_transcript", fiscal_year="FY 2026 - 2027", quarter="Q1",
                document_date=date(2026, 7, 28), landing_page_url="https://example.test/transcript", source="Tata Power IR")
            db.add(doc); db.flush()
            text = "The Tata Power Company Limited\nJuly 28, 2026\n\nPraveer Sinha: Now this year, we will set up 2,500 megawatt to 2,700 megawatt. This target is subject to grid connectivity.\n\nAnalyst One: Can you confirm the outlook?\n\nPraveer Sinha: We expect the project to become operational by 2030."
            db.add(DocumentPage(document_id=doc.id, page_number=4, extracted_text=text, extraction_status="extracted", page_classification="earnings_call_transcript", table_count=0, parser_version="fixture", content_hash="hash-one")); db.commit()
            first = extract_candidates(db)
            again = extract_candidates(db)
            assert first["inserted"] == 2
            assert again["inserted"] == 0
            rows = db.scalars(select(ManagementStatement).order_by(ManagementStatement.id)).all()
            assert len(rows) == 2
            assert all(row.exact_quote in text for row in rows)
            capacity = next(row for row in rows if row.metric_name == "capacity")
            assert (capacity.target_low, capacity.target_high, capacity.unit) == (2500, 2700, "megawatt")
            assert all(row.review_status == "needs_review" for row in rows)
            result = assess_commitments(db)
            assert result["assessed"] == 0
        client = TestClient(app)
        candidate = client.get("/api/management-statements/TATAPOWER").json()["items"][0]
        assert candidate["review_status"] == "needs_review"
        assert candidate["exact_quote"] in candidate["surrounding_context"]
        review = client.post(f"/api/management-statements/{candidate['id']}/review", json={"action": "approved", "reviewer": "Analyst A"})
        assert review.status_code == 200
        assert review.json()["review_status"] == "approved"
        assert client.get("/api/commitments/TATAPOWER").json()["count"] == 1
        assert client.post("/api/commitments/TATAPOWER/assess").json()["assessed"] == 1
        summary = client.get("/api/commitments/TATAPOWER/summary").json()
        assert summary["assessed_count"] == 1
        assert summary["awaiting_outcome_count"] == 1
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

