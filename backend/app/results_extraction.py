"""Headline numbers from any quarterly-results filing.

An Indian results filing (SEBI LODR Reg. 33) prints three quarterly columns side by side:
this quarter, the previous quarter and the same quarter last year. So a single filing is
enough for QoQ and YoY; no history database is needed.

Pipeline (one fast-model call per filing, cached by file hash):
  1. Pick the statement page(s) by keyword scoring over indexed page text.
  2. Send the page image + text to the fast model with a fixed JSON schema.
  3. Verify mechanically instead of by a human:
       - every number the model returns must literally appear on the cited page;
       - PBT - tax (+ associates share) must reconcile to PAT in every column;
       - the three column dates must be quarter-ends 3 and 12 months apart.
  4. Store FinancialObservation rows (validated only if all checks pass, otherwise
     needs_review and surfaced as "unverified" in the UI, never hidden).
"""
from __future__ import annotations

import hashlib
import re
from calendar import monthrange
from datetime import date, datetime, timezone
from decimal import Decimal

import pymupdf
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import llm
from .models import Announcement, Company, DocumentPage, FinancialObservation, ResultsExtraction, SourceDocument
from .research import fiscal_period, quarter_start, registered_pdf_path

PROMPT_VERSION = "results-headline-v1"
METHOD = "llm_results_headline_v1"
UNIT_TO_CRORE = {
    "crore": Decimal("1"), "crores": Decimal("1"), "cr": Decimal("1"),
    "lakh": Decimal("0.01"), "lakhs": Decimal("0.01"), "lacs": Decimal("0.01"),
    "million": Decimal("0.1"), "millions": Decimal("0.1"), "mn": Decimal("0.1"),
    "thousand": Decimal("0.0001"), "thousands": Decimal("0.0001"),
    "rupees": Decimal("0.0000001"), "inr": Decimal("0.0000001"),
}
ROWS = ("revenue", "other_income", "total_income", "total_expenses", "finance_costs", "depreciation",
        "ebitda_reported", "exceptional_items", "pbt", "tax_expense", "share_of_associates_after_tax", "pat")
ROLES = ("current", "previous_quarter", "year_ago_quarter")

SYSTEM = """You read Indian listed-company quarterly financial results filings (SEBI LODR Regulation 33).
Return ONLY a JSON object. Copy numbers exactly as printed (keep the sign; numbers in brackets are negative).
Never compute, round or invent a number: if a row is not printed, use null.
The statement shows several columns. Identify exactly three QUARTER columns:
  current = the latest quarter ended, previous_quarter = the quarter immediately before it,
  year_ago_quarter = the same quarter one year earlier. Ignore half-year / nine-month / year-ended columns.
Banks/NBFCs/insurers: revenue = "Total Income" (or interest earned + other income if no total),
ebitda_reported = "Operating Profit before Provisions and Contingencies" if printed."""

USER_TEMPLATE = """Company: {company} ({ticker}). Page {page} of the filing.
Extract the {basis_hint} statement of profit and loss on this page.

JSON schema:
{{
 "statement_basis": "consolidated" | "standalone",
 "unit": "crore" | "lakh" | "million" | "thousand" | "rupees",
 "is_financial_company": true | false,
 "columns": [{{"role": "current"|"previous_quarter"|"year_ago_quarter", "header": "<column header as printed>", "period_end": "YYYY-MM-DD"}}],
 "rows": {{
   "<key>": {{"label": "<row label as printed>", "values": [<current>, <previous_quarter>, <year_ago_quarter>]}}
 }}
}}
Row keys (omit rows that are not printed): {rows}.
Definitions: revenue = Revenue from operations; total_expenses = Total expenses; pbt = Profit/(loss) before tax
(after exceptional items); tax_expense = total tax expense; share_of_associates_after_tax = share of profit of
associates/joint ventures ONLY if it is added AFTER tax; pat = Profit/(loss) for the period (before other
comprehensive income).

Page text (for exact numbers):
<<<
{text}
>>>"""


def _score_page(text: str) -> int:
    low = text.lower()
    score = 0
    for term, weight in (("revenue from operations", 3), ("profit before tax", 3), ("before tax", 1), ("tax expense", 2),
                         ("quarter ended", 3), ("three months ended", 3), ("total income", 2), ("total expenses", 2),
                         ("finance costs", 1), ("depreciation", 1), ("profit for the period", 2), ("net profit", 1),
                         ("interest earned", 2), ("operating profit before provisions", 2)):
        if term in low:
            score += weight
    if "segment" in low and "segment revenue" in low:
        score -= 3
    if "balance sheet" in low or "statement of assets and liabilities" in low:
        score -= 4
    return score


def select_statement_pages(pages: list[tuple[int, str]]) -> list[tuple[int, str, str]]:
    """Return [(page_number, text, basis_hint)] best consolidated page first, then standalone."""
    scored = [(page_no, text, _score_page(text)) for page_no, text in pages if text and len(text) > 200]
    scored = [item for item in scored if item[2] >= 8]
    if not scored:
        return []
    def basis(text: str) -> str:
        head = text[:1500].lower()
        if "consolidated" in head:
            return "consolidated"
        if "standalone" in head:
            return "standalone"
        return "unknown"
    consolidated = sorted((s for s in scored if basis(s[1]) == "consolidated"), key=lambda s: -s[2])
    standalone = sorted((s for s in scored if basis(s[1]) != "consolidated"), key=lambda s: -s[2])
    chosen = []
    if consolidated:
        chosen.append((consolidated[0][0], consolidated[0][1], "consolidated"))
    if standalone:
        chosen.append((standalone[0][0], standalone[0][1], "standalone" if basis(standalone[0][1]) == "standalone" else "standalone (or only)"))
    return chosen


def _render_page(path, page_number: int) -> bytes | None:
    try:
        with pymupdf.open(path) as pdf:
            page = pdf[page_number - 1]
            return page.get_pixmap(dpi=110).tobytes("png")
    except Exception:
        return None


def _digits(value: str) -> str:
    return re.sub(r"[^\d.]", "", value)


def _canon(number: Decimal) -> str:
    return f"{abs(number).normalize():f}"


def _number_tokens(text: str) -> set[str]:
    tokens = set()
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        digits = raw.replace(",", "")
        tokens.add(digits)
        try:
            tokens.add(_canon(Decimal(digits)))
        except Exception:
            pass
    return tokens


def _as_decimal(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip()
    if not text or text in {"-", "—", "–", "NA", "N.A."}:
        return None
    negative = text.startswith("(") and text.endswith(")") or text.startswith("-")
    digits = _digits(text)
    if not digits:
        return None
    number = Decimal(digits)
    return -number if negative else number


def _quarter_end(value: str | None) -> date | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return date(parsed.year, parsed.month, monthrange(parsed.year, parsed.month)[1])


def _months_between(later: date, earlier: date) -> int:
    return (later.year - earlier.year) * 12 + later.month - earlier.month


def verify(result: dict, page_text: str) -> tuple[list[dict], dict]:
    """Run mechanical checks. Returns (checks, normalised) where normalised has Decimal values."""
    checks: list[dict] = []
    unit = str(result.get("unit") or "").strip().lower()
    factor = UNIT_TO_CRORE.get(unit)
    checks.append({"check": "unit_recognised", "passed": factor is not None, "detail": f"Unit '{unit or 'missing'}'"})

    columns = {c.get("role"): c for c in result.get("columns") or [] if isinstance(c, dict)}
    ends = {role: _quarter_end(columns.get(role, {}).get("period_end")) for role in ROLES}
    dates_ok = all(ends.values()) and _months_between(ends["current"], ends["previous_quarter"]) == 3 and _months_between(ends["current"], ends["year_ago_quarter"]) == 12 and ends["current"].month in (3, 6, 9, 12)
    checks.append({"check": "quarter_columns", "passed": bool(dates_ok), "detail": ", ".join(f"{r}={ends[r]}" for r in ROLES)})

    rows = result.get("rows") or {}
    values: dict[str, list[Decimal | None]] = {}
    raw: dict[str, list[str | None]] = {}
    for key in ROWS:
        row = rows.get(key)
        if not isinstance(row, dict):
            continue
        items = list(row.get("values") or [])[:3]
        items += [None] * (3 - len(items))
        values[key] = [_as_decimal(v) for v in items]
        raw[key] = [None if v is None else str(v) for v in items]

    tokens = _number_tokens(page_text)
    missing = []
    for key in ("revenue", "pbt", "tax_expense", "pat", "finance_costs", "depreciation", "total_expenses", "ebitda_reported"):
        for role, number in zip(ROLES, values.get(key, [])):
            if number is None:
                continue
            if _canon(number) not in tokens and f"{abs(number):.2f}" not in tokens:
                missing.append(f"{key}/{role}={number}")
    checks.append({"check": "numbers_on_page", "passed": not missing and bool(values.get("revenue")) and bool(values.get("pat")),
                   "detail": "All extracted numbers appear on the cited page." if not missing else "Not found on page: " + ", ".join(missing[:6])})

    recon = []
    for index, role in enumerate(ROLES):
        pbt = (values.get("pbt") or [None] * 3)[index]
        tax = (values.get("tax_expense") or [None] * 3)[index]
        pat = (values.get("pat") or [None] * 3)[index]
        assoc = (values.get("share_of_associates_after_tax") or [None] * 3)[index] or Decimal(0)
        if pbt is None or tax is None or pat is None:
            recon.append((role, False, "PBT, tax or PAT missing"))
            continue
        implied = pbt - tax + assoc
        tolerance = max(abs(pat) * Decimal("0.015"), Decimal("0.6"))
        recon.append((role, abs(implied - pat) <= tolerance, f"{pbt} - {tax}{' + ' + str(assoc) if assoc else ''} = {implied} vs PAT {pat}"))
    checks.append({"check": "pbt_minus_tax_equals_pat", "passed": all(ok for _, ok, _ in recon),
                   "detail": "; ".join(f"{role}: {msg}" for role, _, msg in recon)})
    return checks, {"factor": factor, "ends": ends, "values": values, "raw": raw, "columns": columns, "unit": unit}


def derive_ebitda(values: dict[str, list[Decimal | None]], index: int) -> tuple[Decimal | None, str]:
    reported = (values.get("ebitda_reported") or [None] * 3)[index]
    if reported is not None:
        return reported, "Reported in the filing"
    revenue = (values.get("revenue") or [None] * 3)[index]
    expenses = (values.get("total_expenses") or [None] * 3)[index]
    finance = (values.get("finance_costs") or [None] * 3)[index]
    dep = (values.get("depreciation") or [None] * 3)[index]
    if None not in (revenue, expenses, finance, dep):
        return revenue - expenses + finance + dep, "Revenue from operations - total expenses + finance costs + depreciation & amortisation (excludes other income)"
    pbt = (values.get("pbt") or [None] * 3)[index]
    other = (values.get("other_income") or [None] * 3)[index]
    exceptional = (values.get("exceptional_items") or [None] * 3)[index] or Decimal(0)
    if None not in (pbt, finance, dep, other):
        return pbt + finance + dep - other - exceptional, "PBT + finance costs + depreciation - other income - exceptional items"
    return None, "Not derivable from the printed rows"


def _row_quote(page_text: str, label: str | None) -> str:
    if label:
        for line in page_text.splitlines():
            if label.lower()[:28] in line.lower():
                return " ".join(line.split())[:500]
    return (label or "")[:500]


def extract_results_document(db: Session, document: SourceDocument, force: bool = False) -> ResultsExtraction:
    existing = db.scalar(select(ResultsExtraction).where(ResultsExtraction.document_id == document.id))
    if existing and not force and existing.status in {"verified", "unverified"}:
        return existing
    company = db.get(Company, document.company_id)
    pages = db.scalars(select(DocumentPage).where(DocumentPage.document_id == document.id).order_by(DocumentPage.page_number)).all()
    record = existing or ResultsExtraction(company_id=company.id, document_id=document.id, status="failed")
    if existing is None:
        db.add(record)
    candidates = select_statement_pages([(p.page_number, p.extracted_text) for p in pages])
    try:
        path = registered_pdf_path(document)
    except Exception:
        path = None
    scanned = False
    if not candidates:
        image_pages = [p.page_number for p in pages if len((p.extracted_text or "").strip()) < 200][:4]
        if not image_pages or path is None:
            record.status, record.error = "failed", "No results statement page was found in the indexed text (cover letter only, or scanned PDF without a stored file)."
            db.commit()
            return record
        # Scanned filing: let the model read the page images. Numbers cannot be grounded in text,
        # so the result can be at best "unverified" (the reconciliation check still runs).
        candidates = [(n, "", "consolidated (scanned image; read from the picture)") for n in image_pages]
        scanned = True

    best = None
    for page_number, text, basis_hint in candidates:
        image = _render_page(path, page_number) if path else None
        try:
            result, meta = llm.complete_json(
                db, tier="fast", purpose="results_headline", prompt_version=PROMPT_VERSION, system=SYSTEM,
                user=USER_TEMPLATE.format(company=company.name, ticker=company.ticker, page=page_number, basis_hint=basis_hint,
                                          rows=", ".join(ROWS), text=text[:12000]),
                images=[image] if image else None, max_tokens=2500)
        except llm.LLMUnavailable as exc:
            record.status, record.error = "failed", str(exc)
            db.commit()
            return record
        checks, norm = verify(result, text)
        if scanned:
            for check in checks:
                if check["check"] == "numbers_on_page":
                    check["detail"] = "Scanned filing: numbers were read from the page image and could not be matched to page text."
        passed = all(c["passed"] for c in checks)
        attempt = (passed, page_number, text, result, meta, checks, norm)
        if best is None or sum(c["passed"] for c in checks) > sum(c["passed"] for c in best[5]):
            best = attempt
        if passed:
            break  # consolidated page verified; no need to read the standalone one

    passed, page_number, text, result, meta, checks, norm = best
    basis = "consolidated" if str(result.get("statement_basis", "")).lower().startswith("consol") else "standalone"
    record.status = "verified" if passed else "unverified"
    record.statement_basis = basis
    record.reported_unit = norm["unit"] or None
    record.period_end = norm["ends"].get("current")
    record.page_number = page_number
    record.checks = checks
    record.model = meta.get("model")
    record.error = None

    payload: dict = {"columns": {}, "metrics": {}, "is_financial_company": bool(result.get("is_financial_company"))}
    for role in ROLES:
        payload["columns"][role] = {"header": norm["columns"].get(role, {}).get("header"), "period_end": norm["ends"][role].isoformat() if norm["ends"][role] else None}
    factor = norm["factor"] or Decimal(0)
    for metric in ("revenue", "ebitda", "pbt", "pat"):
        series = []
        for index, role in enumerate(ROLES):
            if metric == "ebitda":
                value, formula = derive_ebitda(norm["values"], index)
                label = (result.get("rows", {}).get("ebitda_reported") or {}).get("label") or "EBITDA (derived)"
                raw_value = (norm["raw"].get("ebitda_reported") or [None] * 3)[index] or (None if value is None else f"{value}")
            else:
                value = (norm["values"].get(metric) or [None] * 3)[index]
                formula = "As printed"
                label = (result.get("rows", {}).get(metric) or {}).get("label") or metric.upper()
                raw_value = (norm["raw"].get(metric) or [None] * 3)[index]
            series.append({"role": role, "value": None if value is None else float(value),
                           "value_in_crore": None if value is None or not factor else float(value * factor),
                           "raw": raw_value, "label": label, "formula": formula})
        payload["metrics"][metric] = series
    record.payload = payload
    db.flush()
    _write_observations(db, company, document, record, text, norm, result, basis, passed)
    db.commit()
    return record


def _write_observations(db: Session, company: Company, document: SourceDocument, record: ResultsExtraction, page_text: str,
                        norm: dict, result: dict, basis: str, passed: bool) -> None:
    factor = norm["factor"]
    if factor is None:
        return
    status = "validated" if passed else "needs_review"
    note = "Auto-verified: " + "; ".join(c["check"] for c in record.checks) if passed else "Unverified: " + "; ".join(f"{c['check']} failed ({c['detail']})" for c in record.checks if not c["passed"])
    for index, role in enumerate(ROLES):
        period_end = norm["ends"].get(role)
        if period_end is None:
            continue
        fiscal_year, fiscal_quarter = fiscal_period(period_end)
        header = norm["columns"].get(role, {}).get("header") or role
        for metric, series in record.payload["metrics"].items():
            entry = series[index]
            if entry["value"] is None:
                continue
            value = Decimal(str(entry["value"]))
            item = {"canonical_metric": metric, "statement_basis": basis, "period_end": period_end}
            key = hashlib.sha256(f"{company.id}|{document.id}|{METHOD}|{metric}|{basis}|{period_end}".encode()).hexdigest()
            row = db.scalar(select(FinancialObservation).where(FinancialObservation.evidence_key == key))
            if row is None:
                row = db.scalar(select(FinancialObservation).where(
                    FinancialObservation.company_id == company.id, FinancialObservation.document_id == document.id,
                    FinancialObservation.canonical_metric == metric, FinancialObservation.statement_basis == basis,
                    FinancialObservation.period_end == period_end))
            if row is not None and row.reviewer and row.reviewer != "finn-auto-check":
                continue  # never overwrite a human review decision
            if row is None:
                row = FinancialObservation(evidence_key=key, company_id=company.id, document_id=document.id, **item,
                                           period_start=quarter_start(period_end), period_type="quarterly",
                                           reported_label="", reported_value_raw="", numeric_value=value, reported_unit="",
                                           normalised_value_in_crore=value, fiscal_year=fiscal_year, fiscal_quarter=fiscal_quarter,
                                           extraction_method=METHOD, page_number=record.page_number, source_quote="",
                                           table_title="", row_label="", column_header="", parser_version=PROMPT_VERSION)
                db.add(row)
            row.evidence_key = key
            row.reported_label = entry["label"]
            row.reported_value_raw = str(entry["raw"] or entry["value"])[:100]
            row.numeric_value = value
            row.reported_unit = norm["unit"]
            row.normalised_value_in_crore = value * factor
            row.fiscal_year, row.fiscal_quarter = fiscal_year, fiscal_quarter
            row.period_start, row.period_type = quarter_start(period_end), "quarterly"
            row.extraction_method, row.parser_version = METHOD, PROMPT_VERSION
            row.page_number = record.page_number
            row.source_quote = _row_quote(page_text, entry["label"]) if metric != "ebitda" or entry["formula"] == "Reported in the filing" else entry["formula"]
            row.table_title = f"{basis.title()} statement of profit and loss"
            row.row_label = entry["label"]
            row.column_header = header[:300]
            row.extraction_status = status
            row.reviewer = "finn-auto-check" if passed else None
            row.reviewed_at = datetime.now(timezone.utc) if passed else None
            row.review_note = (note + (f" | EBITDA: {entry['formula']}" if metric == "ebitda" else ""))[:4000]
    db.flush()


def headline_payload(db: Session, record: ResultsExtraction) -> dict:
    """Shape a ResultsExtraction like the digest's results_context so the UI renders it directly."""
    if record.status == "failed":
        return {"status": "extraction_unavailable", "message": f"Results filing detected; headline extraction failed: {record.error or 'unknown error'}"}
    document = record.document
    columns = record.payload.get("columns", {})
    fiscal_year = fiscal_quarter = None
    if record.period_end:
        fiscal_year, fiscal_quarter = fiscal_period(record.period_end)
    metrics = {}
    for metric, series in (record.payload.get("metrics") or {}).items():
        current, previous, year_ago = (series + [{}] * 3)[:3]
        if current.get("value_in_crore") is None:
            continue
        def compare(prior: dict) -> dict:
            if prior.get("value_in_crore") is None:
                return {"status": "unavailable", "reason": "Column not printed"}
            base = prior["value_in_crore"]
            if base <= 0:
                return {"status": "unavailable", "reason": "Comparison base is zero or negative", "previous_value_in_crore": base}
            return {"status": "available", "percentage_change": (current["value_in_crore"] / base - 1) * 100, "previous_value_in_crore": base,
                    "absolute_change_crore": current["value_in_crore"] - base}
        metrics[metric] = {
            "value_in_crore": current["value_in_crore"], "reported_value_raw": current.get("raw"), "reported_unit": record.reported_unit,
            "fiscal_year": fiscal_year, "fiscal_quarter": fiscal_quarter, "period_end": record.period_end.isoformat() if record.period_end else None,
            "page_number": record.page_number, "document_id": record.document_id, "document_title": document.title if document else None,
            "source_url": f"/api/documents/{record.document_id}/file#page={record.page_number}",
            "formula": current.get("formula"), "qoq": compare(previous), "yoy": compare(year_ago),
        }
    return {
        "status": "available" if metrics else "extraction_unavailable",
        "verification": record.status, "checks": record.checks, "model": record.model,
        "statement_basis": record.statement_basis, "unit": "INR crore",
        "period_end": record.period_end.isoformat() if record.period_end else None,
        "fiscal_year": fiscal_year, "fiscal_quarter": fiscal_quarter, "columns": columns,
        "is_financial_company": record.payload.get("is_financial_company", False),
        "metrics": metrics, "message": None if metrics else "No headline rows were extracted.",
    }


def results_document_for_announcement(db: Session, item: Announcement) -> SourceDocument | None:
    if not item.attachment_url:
        return None
    return db.scalar(select(SourceDocument).where(SourceDocument.company_id == item.company_id, SourceDocument.download_url == item.attachment_url))


def results_context_for_announcement(db: Session, item: Announcement) -> dict | None:
    document = results_document_for_announcement(db, item)
    if document is None:
        return None
    record = db.scalar(select(ResultsExtraction).where(ResultsExtraction.document_id == document.id))
    if record is None:
        return None
    return headline_payload(db, record)
