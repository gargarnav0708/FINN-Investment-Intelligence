from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    ticker: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), default="NSE", nullable=False)
    sector: Mapped[str | None] = mapped_column(String(100))
    yfinance_symbol: Mapped[str] = mapped_column(String(40), nullable=False)
    website: Mapped[str | None] = mapped_column(String(500))
    investor_relations_url: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    holding: Mapped[PortfolioHolding | None] = relationship(back_populates="company", uselist=False)
    market_data: Mapped[list[DailyMarketData]] = relationship(back_populates="company", cascade="all, delete-orphan")
    announcements: Mapped[list[Announcement]] = relationship(back_populates="company", cascade="all, delete-orphan")
    documents: Mapped[list[SourceDocument]] = relationship(back_populates="company", cascade="all, delete-orphan")
    financial_observations: Mapped[list[FinancialObservation]] = relationship(back_populates="company", cascade="all, delete-orphan")


class PortfolioHolding(Base):
    __tablename__ = "portfolio_holdings"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    company: Mapped[Company] = relationship(back_populates="holding")


class DailyMarketData(Base):
    __tablename__ = "daily_market_data"
    __table_args__ = (UniqueConstraint("company_id", "trading_date", name="uq_market_company_day"), Index("ix_market_ticker_day", "company_id", "trading_date"))
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    adjusted_close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[int | None] = mapped_column(Integer)
    previous_close: Mapped[float | None] = mapped_column(Float)
    daily_return_pct: Mapped[float | None] = mapped_column(Float)
    average_volume_20: Mapped[float | None] = mapped_column(Float)
    relative_volume: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(80), default="Yahoo Finance via yfinance")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    company: Mapped[Company] = relationship(back_populates="market_data")


class ProviderFinancialObservation(Base):
    """Structured financial data from a market-data provider, kept separate from filing evidence."""
    __tablename__ = "provider_financial_observations"
    __table_args__ = (UniqueConstraint("company_id", "statement_type", "period_type", "period_end", "canonical_metric", name="uq_provider_financial_period_metric"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    statement_type: Mapped[str] = mapped_column(String(24), nullable=False)
    period_type: Mapped[str] = mapped_column(String(16), nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    fiscal_year: Mapped[str] = mapped_column(String(24), nullable=False)
    fiscal_quarter: Mapped[str | None] = mapped_column(String(8))
    canonical_metric: Mapped[str] = mapped_column(String(48), nullable=False)
    reported_label: Mapped[str] = mapped_column(Text, nullable=False)
    reported_value: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(8))
    reported_unit: Mapped[str] = mapped_column(String(40), default="provider reported", nullable=False)
    normalised_value_in_crore: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    provider: Mapped[str] = mapped_column(String(60), default="Yahoo Finance via yfinance", nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ResearchDatasetSnapshot(Base):
    __tablename__ = "research_dataset_snapshots"
    __table_args__ = (UniqueConstraint("company_id", "dataset", name="uq_research_dataset_company"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    dataset: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class Announcement(Base):
    __tablename__ = "announcements"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_announcement_dedupe"), Index("ix_announcement_date", "announcement_datetime"), Index("ix_announcement_company_date", "company_id", "announcement_datetime"))
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    external_announcement_id: Mapped[str | None] = mapped_column(String(100))
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    original_category: Mapped[str | None] = mapped_column(String(160))
    normalized_category: Mapped[str] = mapped_column(String(64), default="Unclassified")
    announcement_datetime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attachment_url: Mapped[str | None] = mapped_column(String(1000))
    source_page_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    company: Mapped[Company] = relationship(back_populates="announcements")


class SourceDocument(Base):
    __tablename__ = "source_documents"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_document_dedupe"), Index("ix_document_company_type", "company_id", "document_type"))
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    document_type: Mapped[str] = mapped_column(String(40), nullable=False)
    fiscal_year: Mapped[str | None] = mapped_column(String(20))
    quarter: Mapped[str | None] = mapped_column(String(20))
    document_date: Mapped[date | None] = mapped_column(Date)
    landing_page_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    download_url: Mapped[str | None] = mapped_column(String(1000))
    local_file_path: Mapped[str | None] = mapped_column(String(1000))
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(100))
    file_hash: Mapped[str | None] = mapped_column(String(64))
    discovery_status: Mapped[str] = mapped_column(String(40), default="discovered")
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    company: Mapped[Company] = relationship(back_populates="documents")
    pages: Mapped[list[DocumentPage]] = relationship(back_populates="document", cascade="all, delete-orphan")
    extraction_runs: Mapped[list[DocumentExtractionRun]] = relationship(back_populates="document", cascade="all, delete-orphan")
    financial_observations: Mapped[list[FinancialObservation]] = relationship(back_populates="document", cascade="all, delete-orphan")


class DocumentPage(Base):
    __tablename__ = "document_pages"
    __table_args__ = (UniqueConstraint("document_id", "page_number", name="uq_document_page_number"), Index("ix_document_page_document", "document_id", "page_number"))
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("source_documents.id", ondelete="CASCADE"), nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    extracted_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    extraction_status: Mapped[str] = mapped_column(String(32), default="extracted", nullable=False)
    page_classification: Mapped[str] = mapped_column(String(80), default="document_page", nullable=False)
    table_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    parser_version: Mapped[str] = mapped_column(String(40), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    document: Mapped[SourceDocument] = relationship(back_populates="pages")


class DocumentExtractionRun(Base):
    __tablename__ = "document_extraction_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("source_documents.id", ondelete="CASCADE"), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="running", nullable=False)
    pages_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tables_detected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metrics_extracted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metrics_needing_review: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errors: Mapped[str | None] = mapped_column(Text)
    parser_version: Mapped[str] = mapped_column(String(40), nullable=False)
    document: Mapped[SourceDocument] = relationship(back_populates="extraction_runs")


class FinancialObservation(Base):
    __tablename__ = "financial_observations"
    __table_args__ = (
        UniqueConstraint("evidence_key", name="uq_financial_observation_evidence"),
        Index("ix_financial_observation_company_period", "company_id", "period_end", "statement_basis"),
        Index("ix_financial_observation_metric", "canonical_metric", "statement_basis", "period_end"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    evidence_key: Mapped[str] = mapped_column(String(64), nullable=False)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    document_id: Mapped[int] = mapped_column(ForeignKey("source_documents.id", ondelete="CASCADE"), nullable=False)
    canonical_metric: Mapped[str] = mapped_column(String(40), nullable=False)
    reported_label: Mapped[str] = mapped_column(Text, nullable=False)
    reported_value_raw: Mapped[str] = mapped_column(String(100), nullable=False)
    numeric_value: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="INR", nullable=False)
    reported_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    normalised_value_in_crore: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    fiscal_year: Mapped[str] = mapped_column(String(20), nullable=False)
    fiscal_quarter: Mapped[str] = mapped_column(String(8), nullable=False)
    period_type: Mapped[str] = mapped_column(String(20), default="quarterly", nullable=False)
    statement_basis: Mapped[str] = mapped_column(String(20), nullable=False)
    extraction_method: Mapped[str] = mapped_column(String(40), nullable=False)
    extraction_status: Mapped[str] = mapped_column(String(24), default="needs_review", nullable=False)
    reviewer: Mapped[str | None] = mapped_column(String(160))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_quote: Mapped[str] = mapped_column(Text, nullable=False)
    table_title: Mapped[str] = mapped_column(Text, nullable=False)
    row_label: Mapped[str] = mapped_column(Text, nullable=False)
    column_header: Mapped[str] = mapped_column(Text, nullable=False)
    parser_version: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    company: Mapped[Company] = relationship(back_populates="financial_observations")
    document: Mapped[SourceDocument] = relationship(back_populates="financial_observations")


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(100), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="running")
    records_attempted: Mapped[int] = mapped_column(Integer, default=0)
    records_inserted: Mapped[int] = mapped_column(Integer, default=0)
    records_updated: Mapped[int] = mapped_column(Integer, default=0)
    records_failed: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)


class AlertPreference(Base):
    __tablename__ = "alert_preferences"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    announcement_categories: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    positive_price_move_pct: Mapped[float] = mapped_column(Float, default=5.0, nullable=False)
    negative_price_move_pct: Mapped[float] = mapped_column(Float, default=5.0, nullable=False)
    unusual_volume_multiple: Mapped[float] = mapped_column(Float, default=2.0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AlertEvent(Base):
    __tablename__ = "alert_events"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_alert_event_dedupe"),
        Index("ix_alert_event_company_date", "company_id", "triggered_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    announcement_id: Mapped[int | None] = mapped_column(ForeignKey("announcements.id", ondelete="SET NULL"))
    market_data_id: Mapped[int | None] = mapped_column(ForeignKey("daily_market_data.id", ondelete="SET NULL"))
    rule_type: Mapped[str] = mapped_column(String(40), nullable=False)
    category: Mapped[str | None] = mapped_column(String(80))
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    company: Mapped[Company] = relationship(foreign_keys=[company_id])
    announcement: Mapped[Announcement | None] = relationship(foreign_keys=[announcement_id])
    market_data: Mapped[DailyMarketData | None] = relationship(foreign_keys=[market_data_id])


class ManagementStatement(Base):
    __tablename__ = "management_statements"
    __table_args__ = (UniqueConstraint("evidence_key", name="uq_management_statement_evidence"), Index("ix_management_statement_company_status", "company_id", "review_status"), Index("ix_management_statement_document_page", "document_id", "page_number"))
    id: Mapped[int] = mapped_column(primary_key=True)
    evidence_key: Mapped[str] = mapped_column(String(64), nullable=False)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    document_id: Mapped[int] = mapped_column(ForeignKey("source_documents.id", ondelete="CASCADE"), nullable=False)
    document_type: Mapped[str] = mapped_column(String(40), nullable=False)
    transcript_date: Mapped[date | None] = mapped_column(Date)
    speaker: Mapped[str] = mapped_column(String(180), nullable=False)
    designation: Mapped[str | None] = mapped_column(String(180))
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    exact_quote: Mapped[str] = mapped_column(Text, nullable=False)
    surrounding_context: Mapped[str] = mapped_column(Text, nullable=False)
    statement_class: Mapped[str] = mapped_column(String(48), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(24), default="original", nullable=False)
    topic: Mapped[str] = mapped_column(String(180), nullable=False)
    metric_name: Mapped[str | None] = mapped_column(String(100))
    metric_definition: Mapped[str | None] = mapped_column(Text)
    target_low: Mapped[float | None] = mapped_column(Float)
    target_high: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(12))
    unit: Mapped[str | None] = mapped_column(String(40))
    target_period: Mapped[str | None] = mapped_column(String(100))
    baseline_period: Mapped[str | None] = mapped_column(String(100))
    assumptions: Mapped[str | None] = mapped_column(Text)
    extraction_status: Mapped[str] = mapped_column(String(24), default="candidate", nullable=False)
    review_status: Mapped[str] = mapped_column(String(24), default="needs_review", nullable=False)
    reviewer: Mapped[str | None] = mapped_column(String(160))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)
    parser_version: Mapped[str] = mapped_column(String(40), nullable=False)
    related_statement_id: Mapped[int | None] = mapped_column(ForeignKey("management_statements.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    company: Mapped[Company] = relationship(foreign_keys=[company_id])
    document: Mapped[SourceDocument] = relationship(foreign_keys=[document_id])


class CommitmentAssessment(Base):
    __tablename__ = "commitment_assessments"
    __table_args__ = (UniqueConstraint("statement_id", name="uq_commitment_assessment_statement"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey("management_statements.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    formula: Mapped[str | None] = mapped_column(Text)
    actual_value: Mapped[float | None] = mapped_column(Float)
    actual_unit: Mapped[str | None] = mapped_column(String(40))
    financial_observation_id: Mapped[int | None] = mapped_column(ForeignKey("financial_observations.id", ondelete="SET NULL"))
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    statement: Mapped[ManagementStatement] = relationship()
    financial_observation: Mapped[FinancialObservation | None] = relationship()
