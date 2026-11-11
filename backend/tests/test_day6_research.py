from datetime import date
from decimal import Decimal
import json
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import Base
from backend.app.llm_research import answer_with_provider
from backend.app.models import Company, DocumentPage, FinancialObservation, SourceDocument
from backend.app.research_assistant import answer_question
from backend.app.seed import seed_companies


def _db():
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine,sessionmaker(bind=engine,expire_on_commit=False)


def _observation(company, document, metric, value, end, *, status="validated", annual=True):
    return FinancialObservation(
        evidence_key=f"{metric}-{end}-{status}-{value}",company_id=company.id,document_id=document.id,
        canonical_metric=metric,reported_label=metric,reported_value_raw=str(value),numeric_value=Decimal(str(value)),
        currency="INR",reported_unit="INR crore",normalised_value_in_crore=Decimal(str(value)),
        period_start=date(end.year-1,4,1),period_end=end,fiscal_year=f"FY {end.year-1} - {end.year}",
        fiscal_quarter="FY" if annual else "Q4",period_type="annual" if annual else "quarterly",
        statement_basis="consolidated",extraction_method="test",extraction_status=status,page_number=12,
        source_quote=f"{metric} {value}",table_title="Official consolidated statements",row_label=metric,
        column_header=f"Year ended 31 Mar {end.year}",parser_version="test")


def test_deterministic_annual_revenue_answer_cites_only_validated_company_rows():
    engine,Session=_db()
    with Session() as db:
        seed_companies(db)
        company=db.scalar(select(Company).where(Company.ticker=="TATAPOWER"))
        doc=SourceDocument(company_id=company.id,dedupe_key="annual-source",title="Official annual report",document_type="annual_report",fiscal_year="FY 2024 - 2025",landing_page_url="https://example.com",source="test")
        db.add(doc);db.flush()
        db.add_all([_observation(company,doc,"revenue",100,date(2024,3,31)),_observation(company,doc,"revenue",120,date(2025,3,31)),_observation(company,doc,"revenue",150,date(2026,3,31),status="needs_review")]);db.commit()
        answer=answer_question(db,"TATAPOWER","How has revenue changed over the last three financial years?")
        assert answer["status"]=="available"
        assert "+20.0%" in answer["answer"]
        assert len(answer["citations"])==2
        assert all(c["title"]=="Official annual report" for c in answer["citations"])
    engine.dispose()


def test_retrieval_llm_citations_are_checked_against_selected_company_pages(monkeypatch):
    engine,Session=_db()
    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,n):
            content={"answer":"The transcript states a growth outlook.","citations":[{"document_id":self.document_id,"page":4,"quote":self.citation_quote}]}
            return json.dumps({"choices":[{"message":{"content":json.dumps(content)}}]}).encode()
    with Session() as db:
        seed_companies(db)
        company=db.scalar(select(Company).where(Company.ticker=="TATAPOWER"))
        doc=SourceDocument(company_id=company.id,dedupe_key="transcript-source",title="Official transcript",document_type="transcript",fiscal_year="FY 2025 - 2026",landing_page_url="https://example.com",local_file_path="transcript.pdf",source="test")
        db.add(doc);db.flush();page=DocumentPage(document_id=doc.id,page_number=4,extracted_text="We expect steady growth this year.",extraction_status="extracted",page_classification="transcript",parser_version="test",content_hash="test-hash");db.add(page);db.commit()
        FakeResponse.document_id=doc.id
        FakeResponse.citation_quote="We expect steady growth this year."
        monkeypatch.setenv("FINN_LLM_BASE_URL","https://llm.example/v1");monkeypatch.setenv("FINN_LLM_MODEL","test-model");monkeypatch.setenv("FINN_LLM_API_KEY","test-key")
        monkeypatch.setattr("backend.app.llm_research.urlopen",lambda request,timeout:FakeResponse())
        result=answer_with_provider(db,company,"What growth outlook did management discuss?")
        assert result["status"]=="available"
        assert result["citations"][0]["page"]==4
        assert result["citations"][0]["url"]==f"/api/documents/{doc.id}/file#page=4"
        FakeResponse.citation_quote="The company will double profits next quarter."
        fabricated=answer_with_provider(db,company,"What growth outlook did management discuss?")
        assert fabricated["status"]=="abstained"
        assert not fabricated["citations"]
    engine.dispose()


def test_unvalidated_financial_input_is_never_used_in_deterministic_answers():
    engine,Session=_db()
    with Session() as db:
        seed_companies(db)
        company=db.scalar(select(Company).where(Company.ticker=="TATAPOWER"))
        doc=SourceDocument(company_id=company.id,dedupe_key="source",title="Annual report",document_type="annual_report",fiscal_year="FY 2024 - 2025",landing_page_url="https://example.com",source="test")
        db.add(doc);db.flush()
        db.add(_observation(company,doc,"cash_from_operations",100,date(2025,3,31)))
        db.add(_observation(company,doc,"pat",80,date(2025,3,31),status="needs_review"))
        db.commit()
        result=answer_question(db,"TATAPOWER","Is operating cash flow keeping pace with PAT?")
        assert result["status"]=="abstained"
        assert not result["citations"]
    engine.dispose()
