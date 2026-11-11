from datetime import date, datetime
from pydantic import BaseModel, ConfigDict


class ComparisonResponse(BaseModel):
    status: str
    reason: str | None = None
    kind: str | None = None
    absolute_change_crore: float | None = None
    percentage_change: float | None = None


class FinancialObservationResponse(BaseModel):
    id: int
    metric: str
    value: float
    reported_value_raw: str
    reported_unit: str
    normalised_value_in_crore: float | None
    currency: str
    fiscal_year: str
    fiscal_quarter: str
    period_type: str
    period_start: date
    period_end: date
    statement_basis: str
    extraction_status: str
    reviewer: str | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None
    document_id: int
    document_title: str
    document_type: str
    page_number: int
    row_label: str
    column_header: str
    source_quote: str
    local_pdf_url: str


class FinancialMetricResponse(BaseModel):
    metric: str
    status: str
    message: str | None = None
    observation: FinancialObservationResponse | None = None
    qoq: ComparisonResponse
    yoy: ComparisonResponse


class FinancialHeadlineResponse(BaseModel):
    ticker: str
    company: str
    status: str
    statement_basis: str
    period_type: str
    fiscal_year: str | None = None
    fiscal_quarter: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    currency: str = "INR"
    unit: str = "INR crore"
    metrics: list[FinancialMetricResponse]


class FinancialHistoryItemResponse(BaseModel):
    observation: FinancialObservationResponse
    conflict: bool = False
    conflict_values_crore: list[float] = []
    qoq: ComparisonResponse
    yoy: ComparisonResponse


class FinancialHistoryResponse(BaseModel):
    ticker: str
    metric: str
    statement_basis: str
    count: int
    data: list[FinancialHistoryItemResponse]


class DocumentPageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    document_id: int
    page_number: int
    extracted_text: str
    extraction_status: str
    page_classification: str
    table_count: int
    parser_version: str
    content_hash: str
    created_at: datetime


class DocumentPagesResponse(BaseModel):
    document_id: int
    document_title: str
    total_pages: int
    count: int
    pages: list[DocumentPageResponse]


class ExtractionStatusResponse(BaseModel):
    documents_total: int
    documents_processed: int
    pages_processed: int
    financial_observations: int
    validated_observations: int
    observations_needing_review: int
    documents_requiring_ocr: int
    failed_documents: int
    parser_version: str

