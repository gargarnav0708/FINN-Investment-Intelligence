from __future__ import annotations

import hashlib
import io
import json
import re
from contextlib import redirect_stdout
from calendar import monthrange
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pymupdf
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .database import PROJECT_ROOT
from .models import Company, DocumentExtractionRun, DocumentPage, FinancialObservation, SourceDocument

PARSER_VERSION = "pymupdf-page-tables-v11"
PDF_ROOT = (PROJECT_ROOT / "data" / "documents").resolve()
DATE_RE = re.compile(r"\b(\d{1,2})[-\s]+([A-Za-z]{3,9})[-,\s]+(\d{2,4})\b")
AMOUNT_RE = re.compile(r"\(?\s*-?\d[\d,]*(?:\.\d+)?\s*\)?|(?<!\w)[—–-](?!\w)")
MONTHS = {name.lower(): index for index, name in enumerate(("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1)}
MONTHS.update({name.lower()[:3]: index for index, name in enumerate(("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1)})
MONTHS["seo"] = 9

METRIC_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("revenue", re.compile(r"\brevenue\s+from\s+operations\b", re.I)),
    ("ebitda", re.compile(r"\b(?:operating\s+)?ebitda\b|earnings before interest,? tax,? depreciation and amorti[sz]ation", re.I)),
    ("pbt", re.compile(r"\bprofi[ltu]\s*/?\s*\(?loss\)?\s+before\s+tax\b|\bprofi[ltu] before tax\b", re.I)),
    ("pat", re.compile(r"\bnet\s+profit\s*/?\s*\(?loss\)?\s+for\s+the\s+period\b|\bprofit\s+for\s+the\s+period\b", re.I)),
    ("pat_attributable_to_owners", re.compile(r"\bowners of the company\b|\bequity holders of the company\b", re.I)),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def registered_pdf_path(document: SourceDocument) -> Path:
    raw = Path(document.local_file_path or "")
    path = raw.resolve() if raw.is_absolute() else (PROJECT_ROOT / raw).resolve()
    if path != PDF_ROOT and PDF_ROOT not in path.parents:
        raise ValueError("registered file path is outside the document store")
    return path


def period_end_from_metadata(fiscal_year: str | None, quarter: str | None) -> date | None:
    if not fiscal_year or not quarter:
        return None
    match = re.search(r"(20\d{2})\s*-\s*(20\d{2})", fiscal_year)
    qmatch = re.fullmatch(r"Q([1-4])", quarter.upper())
    if not match or not qmatch:
        return None
    quarter_number = int(qmatch.group(1))
    year = int(match.group(1)) + (1 if quarter_number == 4 else 0)
    month = {1: 6, 2: 9, 3: 12, 4: 3}[quarter_number]
    return date(year, month, monthrange(year, month)[1])


def quarter_start(period_end: date) -> date:
    month = ((period_end.month - 1) // 3) * 3 + 1
    return date(period_end.year, month, 1)


def fiscal_period(period_end: date) -> tuple[str, str]:
    start_year = period_end.year if period_end.month >= 4 else period_end.year - 1
    quarter = ((period_end.month - 4) % 12) // 3 + 1
    return f"FY {start_year} - {start_year + 1}", f"Q{quarter}"


def parse_amount_tokens(line: str) -> list[tuple[Decimal | None, str]]:
    line = re.sub(r"(?<!\S)(\d{1,3})\.(\d{3})\.(\d{2})(?!\S)", r"\1,\2.\3", line)
    compact = re.sub(r"(?<=\d),\s+(?=\d)", ",", line)
    compact = re.sub(r"(?<=\d)\s{1,2}(?=\.\d{1,2}(?:\s|$))", "", compact)
    compact = re.sub(r"(\d+\.\d{1,2})\s+(\d)(?=\s+\d[\d,]*\.\d)", r"\1\2", compact)
    values: list[tuple[Decimal | None, str]] = []
    for match in AMOUNT_RE.finditer(compact):
        raw = match.group(0).strip().replace(" ", "")
        if raw in {"-", "—", "–"}:
            values.append((None, raw))
            continue
        negative = raw.startswith("(") and raw.endswith(")")
        reported_raw = raw
        raw = raw.strip("() ").replace(",", "")
        try:
            number = Decimal(raw)
            values.append((-number if negative else number, reported_raw))
        except InvalidOperation:
            continue
    return values


def parse_amounts(line: str) -> list[Decimal | None]:
    return [value for value, _ in parse_amount_tokens(line)]


def _normalise_unit(header: str) -> tuple[str | None, Decimal | None]:
    text = header.lower().replace("₹", "inr").replace("~", "inr")
    if "crore" in text or "cr." in text or "cr)" in text:
        return "INR crore", Decimal("1")
    if "lakh" in text:
        return "INR lakh", Decimal("0.01")
    if "million" in text:
        return "INR million", Decimal("10")
    if re.search(r"\binr\b|\brs\.?\b|\brupees?\b", text):
        return "INR", Decimal("0.0000001")
    return None, None


def _dates_in(text: str) -> list[date]:
    result = []
    for day, month, year in DATE_RE.findall(text):
        month_num = MONTHS.get(month.lower(), MONTHS.get(month.lower()[:3]))
        if not month_num:
            continue
        y = int(year)
        if y < 100:
            y += 2000 if y < 70 else 1900
        try:
            result.append(date(y, month_num, int(day)))
        except ValueError:
            continue
    return result


def _statement_pages(page_rows: list[tuple[int, str]]) -> dict[str, tuple[int, str, str]]:
    found: dict[str, tuple[int, str, str]] = {}
    for page_number, text in page_rows:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        joined = " ".join(lines[:15])
        title_match = re.search(r"STATEMENT OF (?:AUDITED\s+)?(CONSOLIDATED|STANDALONE).*?FINANCIAL RESULTS", joined, re.I)
        if not title_match:
            continue
        basis = title_match.group(1).lower()
        title = re.search(r"STATEMENT OF .*?FINANCIAL RESULTS[^\n]*", text, re.I)
        table_title = title.group(0).strip() if title else f"{basis.title()} financial results"
        found.setdefault(basis, (page_number, text, table_title))
    return found


def classify_page(text: str, document_type: str) -> str:
    lower = text.lower()
    if re.search(r"statement of (?:consolidated|standalone).*financial results", lower):
        return "financial_results_statement"
    if "balance sheet" in lower:
        return "balance_sheet"
    if "statement of profit and loss" in lower or "statement of profit or loss" in lower:
        return "profit_and_loss"
    if "cash flow statement" in lower or "statement of cash flows" in lower:
        return "cash_flow_statement"
    if "independent auditor" in lower or "auditor's report" in lower:
        return "auditor_report"
    if "management discussion and analysis" in lower or "management discussion & analysis" in lower:
        return "management_discussion"
    if "directors' report" in lower or "board's report" in lower:
        return "directors_report"
    if "notes to the financial statements" in lower or "notes forming part of" in lower:
        return "financial_statement_notes"
    if document_type == "earnings_call_transcript":
        return "earnings_call_transcript"
    if document_type == "annual_report":
        return "annual_report_section"
    return "document_page"


def extract_financial_candidates(document: SourceDocument, page_rows: list[tuple[int, str]]) -> list[dict]:
    if getattr(getattr(document, "company", None), "ticker", None) == "INFY" and document.document_type == "quarterly_results":
        # Infosys FY26 result pack uses a separate 3-column consolidated summary table (PDF p.25).
        # Its ordering is Q4 FY26, FY26, Q4 FY25; keep each period and basis explicit.
        candidates = []
        target = next(((number, text) for number, text in page_rows if "Profit before tax (1)" in text and "Revenue from operations" in text and "178,650" in text), None)
        if not target:
            return []
        page_number, text = target
        periods = [(date(2026, 3, 31), "Q4", "Quarter ended 31 Mar 2026", "quarterly"),
                   (date(2026, 3, 31), "FY", "Year ended 31 Mar 2026", "annual"),
                   (date(2025, 3, 31), "Q4", "Quarter ended 31 Mar 2025", "quarterly")]
        for metric, pattern in (("revenue", r"Revenue from operations"), ("pbt", r"Profit before tax \(1\)"), ("pat", r"Profit for the period \(1\)")):
            line = next((x.strip() for x in text.splitlines() if re.match(pattern, x.strip(), re.I)), None)
            if not line: continue
            value_text = re.sub(r"\(\d+\)", "", line)
            value_text = re.sub(pattern, "", value_text, count=1, flags=re.I)
            values = [v for v, raw in parse_amount_tokens(value_text) if v is not None]
            if len(values) < 3: continue
            label = line.split()[0] if metric == "revenue" else ("Profit before tax" if metric == "pbt" else "Profit for the period")
            for index, (end, quarter, column, period_type) in enumerate(periods):
                candidates.append({"canonical_metric": metric, "reported_label": label,
                    "reported_value_raw": f"{values[index]:,.0f}", "numeric_value": values[index],
                    "currency": "INR", "reported_unit": "INR crore", "normalised_value_in_crore": values[index],
                    "period_start": date(end.year, 1, 1) if quarter == "Q4" else date(2025, 4, 1),
                    "period_end": end, "fiscal_year": "FY 2025 - 2026" if end.year == 2026 else "FY 2024 - 2025",
                    "fiscal_quarter": quarter, "period_type": period_type, "statement_basis": "consolidated",
                    "extraction_method": "infosys_fy26_result_summary", "extraction_status": "needs_review",
                    "page_number": page_number, "source_quote": line, "table_title": "Infosys consolidated financial results (₹ crore)",
                    "row_label": label, "column_header": column, "parser_version": PARSER_VERSION})
        return candidates
    # Select the current nine-doc set by registered reporting metadata; the older Q3 FY25
    # result remains parseable/indexable but is outside the requested four-quarter engine.
    if document.document_type == "annual_report":
        return extract_annual_table_candidates(document, page_rows)
    if document.document_type != "quarterly_results":
        return []
    period_end_expected = period_end_from_metadata(document.fiscal_year, document.quarter)
    if period_end_expected is None or document.fiscal_year not in {"FY 2025 - 2026", "FY 2026 - 2027"}:
        return []

    statements = _statement_pages(page_rows)
    candidates: list[dict] = []
    for basis, (page_number, text, table_title) in statements.items():
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        title_pos = next((i for i, line in enumerate(lines) if "STATEMENT OF" in line.upper() and "FINANCIAL RESULTS" in line.upper()), 0)
        revenue_pos = next((i for i, line in enumerate(lines) if re.search(r"revenue\s+from\s+operations", line, re.I)), len(lines))
        header_text = " ".join(lines[title_pos:revenue_pos])
        header_dates = _dates_in(header_text)
        column_header = "Quarter ended · " + " · ".join(d.strftime("%d %b %Y") for d in header_dates)
        column_count = len(header_dates)
        reported_unit, multiplier = _normalise_unit(header_text)
        if not column_count:
            column_count = 0

        for metric, pattern in METRIC_RULES:
            if metric == "ebitda":
                # Only explicitly named EBITDA rows qualify; never derive it here.
                matching = [line for line in lines if pattern.search(line) and not re.search(r"before interest.*tax", line, re.I)]
            else:
                matching = [line for line in lines if pattern.search(line)]
            if metric == "pbt":
                matching = [line for line in matching if not re.search(r"before exceptional|before regulatory|before share of", line, re.I)]
            if metric == "pat":
                matching = [line for line in matching if not re.search(r"attributable|owners of|non-controlling", line, re.I)]
            if metric == "pat_attributable_to_owners":
                heading_index = next((i for i, row in enumerate(lines) if re.search(r"profit\s*/?\s*\(?loss\)?\s+for the period attributable to", row, re.I)), None)
                if heading_index is None:
                    matching = []
                else:
                    matching = []
                    for row in lines[heading_index + 1:]:
                        if re.search(r"other comprehensive|paid-up equity", row, re.I):
                            break
                        if pattern.search(row):
                            matching = [row]
                            break
            # The first row in the statement is the primary line item; later narrative/table repeats are ignored.
            line = matching[0] if matching else None
            if not line:
                continue
            amount_tokens = parse_amount_tokens(line)
            if column_count and len(amount_tokens) >= column_count:
                quarter_values = amount_tokens[-column_count:][:3]
            elif amount_tokens:
                quarter_values = amount_tokens[-1:]
            else:
                continue
            if metric == "ebitda":
                unit, unit_multiplier = _normalise_unit(header_text)
            else:
                unit, unit_multiplier = reported_unit, multiplier
            number_start = re.search(pattern, line)
            label = line[:number_start.end()].strip(" :") if number_start else line
            for column_index, (quarter_value, raw_value) in enumerate(quarter_values):
                if quarter_value is None:
                    continue
                reported_period_end = header_dates[column_index] if column_index < len(header_dates) else period_end_expected
                fiscal_year, fiscal_quarter = fiscal_period(reported_period_end)
                if fiscal_year not in {"FY 2024 - 2025", "FY 2025 - 2026", "FY 2026 - 2027"}:
                    continue
                actual_column_header = f"Quarter ended · {reported_period_end.strftime('%d %b %Y')}"
                candidates.append({
                    "canonical_metric": metric,
                    "reported_label": label,
                    "reported_value_raw": raw_value,
                    "numeric_value": quarter_value,
                    "currency": "INR",
                    "reported_unit": unit or "unknown",
                    "normalised_value_in_crore": quarter_value * unit_multiplier if unit_multiplier is not None else quarter_value,
                    "period_start": quarter_start(reported_period_end),
                    "period_end": reported_period_end,
                    "fiscal_year": fiscal_year,
                    "fiscal_quarter": fiscal_quarter,
                    "period_type": "quarterly",
                    "statement_basis": basis,
                    "extraction_method": "pymupdf_table_row",
                    "extraction_status": "needs_review",
                    "page_number": page_number,
                    "source_quote": line,
                    "table_title": table_title,
                    "row_label": label,
                    "column_header": actual_column_header,
                    "parser_version": PARSER_VERSION,
                    "unit_known": unit_multiplier is not None,
                    "header_period_matches": reported_period_end == period_end_expected if column_index == 0 else True,
                })
    return candidates


def extract_annual_table_candidates(document: SourceDocument, page_rows: list[tuple[int, str]]) -> list[dict]:
    """Read audited annual statement tables with PyMuPDF's cell extraction."""
    match = re.search(r"(20\d{2})\s*-\s*(20\d{2})", document.fiscal_year or "")
    if not match:
        return []
    start_year, end_year = map(int, match.groups())
    period_start, period_end = date(start_year, 4, 1), date(end_year, 3, 31)
    fiscal_year = f"FY {start_year} - {end_year}"
    text_by_page = dict(page_rows)
    candidates: list[dict] = []
    path = registered_pdf_path(document)
    pdf = pymupdf.open(path)

    def as_value(raw: str | None):
        if not raw:
            return None
        parsed = parse_amount_tokens(raw)
        return next(((value, token) for value, token in reversed(parsed) if value is not None), None)

    def append(metric: str, basis: str, page_number: int, label: str, row: list[str | None], period_label: str):
        if len(row) < 2:
            return
        amount = next((parsed for cell in row[1:] if (parsed := as_value(cell)) is not None), None)
        if amount is None:
            return
        value, raw = amount
        quote = " | ".join(" ".join(str(cell or "").split()) for cell in row if cell)
        candidates.append({
            "canonical_metric": metric, "reported_label": label,
            "reported_value_raw": raw, "numeric_value": value, "currency": "INR",
            "reported_unit": "INR crore", "normalised_value_in_crore": value,
            "period_start": period_start, "period_end": period_end, "fiscal_year": fiscal_year,
            "fiscal_quarter": "FY", "period_type": "annual", "statement_basis": basis,
            "extraction_method": "pymupdf_annual_table", "extraction_status": "needs_review",
            "page_number": page_number, "source_quote": quote,
            "table_title": f"{basis.title()} annual financial statements · FY {start_year}-{end_year}",
            "row_label": label, "column_header": period_label, "parser_version": PARSER_VERSION,
        })

    for zero_index, page in enumerate(pdf):
        page_number = zero_index + 1
        page_text = text_by_page.get(page_number, "")
        head = "\n".join(page_text.splitlines()[:8]).lower()
        for basis in ("consolidated", "standalone"):
            title = basis.title()
            is_pnl = f"{basis} statement of profit and loss" in head and "for the year ended" in page_text.lower()
            is_balance = f"{basis} balance sheet" in "\n".join(page_text.splitlines()[:12]).lower() and (
                "as at march 31" in page_text.lower() or "borrowings" in page_text.lower() or "total current liabilities" in page_text.lower()
            )
            is_cash = f"{basis} cash flow statement" in head and "for the year ended" in page_text.lower()
            if not (is_pnl or is_balance or is_cash):
                continue
            try:
                tables = page.find_tables().tables
            except Exception:
                continue
            for table in tables:
                try:
                    table_rows = table.extract()
                except Exception:
                    continue
                if is_pnl and re.search(r"\bRevenue\s+from\s+Operations\b", " ".join(str(r[0] or "") for r in table_rows), re.I):
                    rules = (
                        ("revenue", re.compile(r"\bRevenue\s+from\s+Operations\b", re.I), ()),
                        ("pbt", re.compile(r"\bProfit\s*/?\s*\(?Loss\)?\s+Before\s+Tax\b", re.I), ("before movement", "before exceptional", "before share of", "before regulatory")),
                        ("pat", re.compile(r"\b(?:Net\s+)?Profit\s*/?\s*\(?Loss\)?\s+for\s+the\s+(?:year|period)\b", re.I), ("attributable", "comprehensive")),
                    )
                    for metric, pattern, excludes in rules:
                        for row in table_rows:
                            label = " ".join(str(row[0] or "").split()) if row else ""
                            if pattern.search(label) and not any(re.search(term, label, re.I) for term in excludes):
                                append(metric, basis, page_number, label, row, f"Year ended 31 Mar {end_year}")
                                break
                if is_balance:
                    section = ""
                    for row in table_rows:
                        label = " ".join(str(row[0] or "").split()) if row else ""
                        low = label.lower()
                        if "non-current liabilities" in low and not low.startswith("total"):
                            section = "noncurrent"
                        elif "current liabilities" in low and not low.startswith("total"):
                            section = "current"
                        elif "current assets" in low and not low.startswith("total"):
                            section = "current_assets"
                        metrics = []
                        if re.fullmatch(r"TOTAL\s+ASSETS", label, re.I): metrics = ["total_assets"]
                        elif re.fullmatch(r"TOTAL\s+EQUITY", label, re.I): metrics = ["total_equity"]
                        elif re.search(r"\bBorrowings\b", label, re.I) and "lease" not in low and section in {"noncurrent", "current"}:
                            metrics = [f"borrowings_{section}"]
                        elif re.search(r"\bLease Liabilities\b", label, re.I) and section in {"noncurrent", "current"}:
                            metrics = [f"lease_liabilities_{section}"]
                        elif re.search(r"\bCash and Cash Equivalents\b", label, re.I) and "total" not in low:
                            metrics = ["cash_and_cash_equivalents"]
                        elif re.fullmatch(r"Total Current Assets", label, re.I): metrics = ["current_assets"]
                        elif re.fullmatch(r"Total Current Liabilities", label, re.I): metrics = ["current_liabilities"]
                        for metric in metrics:
                            append(metric, basis, page_number, label, row, f"As at 31 Mar {end_year}")
                if is_cash:
                    rules = (
                        ("cash_from_operations", re.compile(r"\bNet Cash Flows?\s+(?:generated\s+)?from\s*/?\s*\(?used in\)?\s*Operating Activities\b", re.I)),
                        ("cash_from_investing", re.compile(r"\bNet Cash Flows?\s+(?:generated\s+)?from\s*/?\s*\(?used in\)?\s*Investing Activities\b", re.I)),
                        ("cash_from_financing", re.compile(r"\bNet Cash Flows?\s+(?:generated\s+)?from\s*/?\s*\(?used in\)?\s*Financing Activities\b", re.I)),
                        ("capital_expenditure_ppe", re.compile(r"\b(?:Capital Expenditure on Property, Plant and Equipment|Purchase of Capital Assets)\b", re.I)),
                    )
                    for metric, pattern in rules:
                        for row in table_rows:
                            label = " ".join(str(row[0] or "").split()) if row else ""
                            if pattern.search(label):
                                append(metric, basis, page_number, label, row, f"Year ended 31 Mar {end_year}")
                                break
    return candidates


def extract_annual_statement_candidates(document: SourceDocument, page_rows: list[tuple[int, str]]) -> list[dict]:
    """Extract annual-statement row candidates from the report's audited statements.

    Only the year named by the registered annual report is extracted. Comparative
    columns are deliberately not copied from another year's filing, and all rows
    remain needs_review until a person checks the cited source page.
    """
    match = re.search(r"(20\d{2})\s*-\s*(20\d{2})", document.fiscal_year or "")
    if not match:
        return []
    start_year, end_year = map(int, match.groups())
    period_start, period_end = date(start_year, 4, 1), date(end_year, 3, 31)
    fiscal_year = f"FY {start_year} - {end_year}"
    candidates: list[dict] = []

    def statement_pages(basis: str, statement: str) -> list[tuple[int, str]]:
        title = re.compile(rf"(?is)^.*?\b{basis}\s+{statement}\b", re.I)
        return [(number, text) for number, text in page_rows
                if title.search("\n".join(text.splitlines()[:5]))
                and re.search(r"(?i)(?:as at|for the year ended)\s+march\s+31", text)]

    def matching_row(pages: list[tuple[int, str]], pattern: re.Pattern[str], excludes: tuple[str, ...] = ()):
        found = []
        for page_number, text in pages:
            for line in text.splitlines():
                cleaned = " ".join(line.split())
                if not pattern.search(cleaned) or any(re.search(term, cleaned, re.I) for term in excludes):
                    continue
                amounts = [(value, raw) for value, raw in parse_amount_tokens(cleaned) if value is not None]
                if amounts:
                    found.append((page_number, cleaned, amounts[-2:]))
        return found

    def add_candidate(metric: str, basis: str, page_number: int, line: str, values: list[tuple[Decimal, str]], table_title: str, period_label: str, unit_text: str):
        if not values:
            return
        value, raw = values[0]
        unit, multiplier = _normalise_unit(unit_text)
        candidates.append({
            "canonical_metric": metric, "reported_label": line.split(str(raw))[0].strip(" :.") or line,
            "reported_value_raw": raw, "numeric_value": value, "currency": "INR",
            "reported_unit": unit or "unknown", "normalised_value_in_crore": value * (multiplier or Decimal("1")),
            "period_start": period_start, "period_end": period_end, "fiscal_year": fiscal_year,
            "fiscal_quarter": "FY", "period_type": "annual", "statement_basis": basis,
            "extraction_method": "annual_statement_row", "extraction_status": "needs_review",
            "page_number": page_number, "source_quote": line, "table_title": table_title,
            "row_label": line, "column_header": period_label, "parser_version": PARSER_VERSION,
        })

    for basis in ("Consolidated", "Standalone"):
        lower_basis = basis.lower()
        pnl_pages = statement_pages(basis, r"Statement of Profit and Loss")
        balance_pages = statement_pages(basis, r"Balance Sheet")
        cash_pages = statement_pages(basis, r"Cash Flow Statement")
        page_title = f"{basis} annual financial statements · FY {start_year}-{end_year}"
        for metric, pattern, excludes in (
            ("revenue", re.compile(r"\bRevenue\s+from\s+Operations\b", re.I), ()),
            ("pbt", re.compile(r"\bProfit\s*/?\s*\(?Loss\)?\s+Before\s+Tax\b", re.I), ("before movement", "before exceptional", "before share of", "before regulatory")),
            ("pat", re.compile(r"\b(?:Net\s+)?Profit\s*/?\s*\(?Loss\)?\s+for\s+the\s+(?:year|period)\b", re.I), ("attributable", "comprehensive")),
        ):
            rows = matching_row(pnl_pages, pattern, excludes)
            if metric == "pbt" and rows:
                # The primary, final reported PBT line is the one that does not
                # exclude associate or regulatory results.
                rows = rows[-1:]
            for page_number, line, values in rows[-1:]:
                add_candidate(metric, lower_basis, page_number, line, values, page_title,
                              f"Year ended 31 Mar {end_year}", "₹ crore")

        balance_rules = (
            ("total_assets", re.compile(r"^TOTAL\s+ASSETS\b", re.I)),
            ("total_equity", re.compile(r"^Total\s+Equity\s*$", re.I)),
            ("borrowings_noncurrent", re.compile(r"\bBorrowings\b", re.I)),
            ("borrowings_current", re.compile(r"\bBorrowings\b", re.I)),
            ("lease_liabilities_noncurrent", re.compile(r"\bLease Liabilities\b", re.I)),
            ("lease_liabilities_current", re.compile(r"\bLease Liabilities\b", re.I)),
            ("cash_and_cash_equivalents", re.compile(r"\bCash and Cash Equivalents\b", re.I)),
            ("current_assets", re.compile(r"^Total Current Assets\b", re.I)),
            ("current_liabilities", re.compile(r"^Total Current Liabilities\b", re.I)),
        )
        used_rows: set[tuple[int, str]] = set()
        for metric, pattern in balance_rules:
            rows = matching_row(balance_pages, pattern)
            if metric.startswith("borrowings_"):
                wanted = "non-current liabilities" if metric.endswith("noncurrent") else "current liabilities"
                rows = [r for r in rows if wanted in r[1].lower()]
                if metric.endswith("noncurrent"):
                    rows = [r for r in rows if "lease" not in r[1].lower()]
                elif metric == "borrowings_current":
                    rows = [r for r in rows if "lease" not in r[1].lower()]
            elif metric.startswith("lease_liabilities_"):
                wanted = "non-current liabilities" if metric.endswith("noncurrent") else "current liabilities"
                rows = [r for r in rows if wanted in r[1].lower() and "lease" in r[1].lower()]
            elif metric == "cash_and_cash_equivalents":
                rows = [r for r in rows if "cash and cash equivalents" in r[1].lower() and "total" not in r[1].lower()]
            if rows:
                page_number, line, values = rows[-1]
                if (page_number, line) not in used_rows:
                    add_candidate(metric, lower_basis, page_number, line, values, page_title,
                                  f"As at 31 Mar {end_year}", "₹ crore")
                    used_rows.add((page_number, line))

        cash_rules = (
            ("cash_from_operations", re.compile(r"\bNet Cash Flow from\s*/?\s*\(?used in\)?\s*Operating Activities\b", re.I)),
            ("cash_from_investing", re.compile(r"\bNet Cash Flow from\s*/?\s*\(?used in\)?\s*Investing Activities\b", re.I)),
            ("cash_from_financing", re.compile(r"\bNet Cash Flow from\s*/?\s*\(?used in\)?\s*Financing Activities\b", re.I)),
            ("capital_expenditure_ppe", re.compile(r"\bCapital Expenditure on Property, Plant and Equipment\b", re.I)),
        )
        for metric, pattern in cash_rules:
            rows = matching_row(cash_pages, pattern)
            if rows:
                page_number, line, values = rows[-1]
                add_candidate(metric, lower_basis, page_number, line, values, page_title,
                              f"Year ended 31 Mar {end_year}", "₹ crore")

    return candidates


def _evidence_key(company_id: int, document_id: int, item: dict) -> str:
    basis = "|".join(str(item[k]) for k in ("canonical_metric", "statement_basis", "period_end", "row_label", "column_header"))
    return hashlib.sha256(f"{company_id}|{document_id}|{basis}".encode()).hexdigest()


def parse_documents(db: Session, ticker: str | None = None, force: bool = False) -> dict:
    query = select(SourceDocument).join(Company).where(SourceDocument.local_file_path.is_not(None))
    if ticker:
        query = query.where(func.upper(Company.ticker) == ticker.upper())
    documents = db.scalars(query.order_by(Company.ticker, SourceDocument.id)).all()
    result = {"status": "success", "documents_discovered": len(documents), "processed": 0, "skipped": 0, "failed": 0, "pages_processed": 0, "tables_detected": 0, "metrics_extracted": 0, "errors": []}
    for document in documents:
        run = DocumentExtractionRun(document_id=document.id, parser_version=PARSER_VERSION)
        db.add(run)
        db.flush()
        try:
            path = registered_pdf_path(document)
            if not path.is_file() or path.stat().st_size <= 0:
                raise ValueError("local PDF is missing or empty")
            if path.read_bytes()[:4] != b"%PDF":
                raise ValueError("local file does not have a PDF signature")
            digest = sha256_file(path)
            if not document.file_hash or digest != document.file_hash:
                raise ValueError("PDF SHA-256 does not match the registered document hash")
            prior_run = db.scalar(select(DocumentExtractionRun).where(
                DocumentExtractionRun.document_id == document.id,
                DocumentExtractionRun.parser_version == PARSER_VERSION,
                DocumentExtractionRun.status == "success",
            ).order_by(DocumentExtractionRun.completed_at.desc()).limit(1))
            existing_pages = db.scalar(select(func.count(DocumentPage.id)).where(DocumentPage.document_id == document.id)) or 0
            if not force and prior_run and existing_pages == prior_run.pages_processed:
                run.status = "skipped"
                run.completed_at = datetime.now(timezone.utc)
                result["skipped"] += 1
                continue

            stored_pages = db.scalars(select(DocumentPage).where(DocumentPage.document_id == document.id).order_by(DocumentPage.page_number)).all()
            page_rows: list[tuple[int, str]] = []
            tables_total = 0
            if stored_pages and not force:
                # Reuse hash-verified page text for a financial-rule update; PDF parsing is skipped.
                page_rows = [(page.page_number, page.extracted_text) for page in stored_pages]
                tables_total = sum(page.table_count for page in stored_pages)
                for page in stored_pages:
                    page.page_classification = classify_page(page.extracted_text, document.document_type)
                    page.parser_version = PARSER_VERSION
                page_count = len(stored_pages)
            else:
                pdf = pymupdf.open(path)
                if pdf.is_encrypted:
                    raise ValueError("encrypted PDFs are not supported")
                for index, page in enumerate(pdf):
                    page_number = index + 1
                    content = page.get_text("text", sort=True).strip()
                    try:
                        with redirect_stdout(io.StringIO()):
                            table_count = len(page.find_tables().tables)
                    except Exception:
                        table_count = 0
                    tables_total += table_count
                    status = "needs_ocr" if len(content) < 40 else "extracted"
                    page_rows.append((page_number, content))
                    page_record = db.scalar(select(DocumentPage).where(DocumentPage.document_id == document.id, DocumentPage.page_number == page_number))
                    if page_record is None:
                        page_record = DocumentPage(document_id=document.id, page_number=page_number)
                        db.add(page_record)
                    page_record.extracted_text = content
                    page_record.extraction_status = status
                    page_record.page_classification = classify_page(content, document.document_type)
                    page_record.table_count = table_count
                    page_record.parser_version = PARSER_VERSION
                    page_record.content_hash = hashlib.sha256(content.encode()).hexdigest()
                page_count = len(pdf)
            candidates = extract_financial_candidates(document, page_rows)
            company = db.get(Company, document.company_id)
            for item in candidates:
                evidence_key = _evidence_key(company.id, document.id, item)
                observation = db.scalar(select(FinancialObservation).where(FinancialObservation.evidence_key == evidence_key))
                if observation is None:
                    observation = db.scalar(select(FinancialObservation).where(
                        FinancialObservation.company_id == company.id,
                        FinancialObservation.document_id == document.id,
                        FinancialObservation.canonical_metric == item["canonical_metric"],
                        FinancialObservation.statement_basis == item["statement_basis"],
                        FinancialObservation.period_end == item["period_end"],
                    ))
                if observation is None:
                    observation = FinancialObservation(evidence_key=evidence_key, company_id=company.id, document_id=document.id, **{k: v for k, v in item.items() if k in FinancialObservation.__table__.columns.keys()})
                    db.add(observation)
                else:
                    prior_review = (observation.extraction_status, observation.reviewer, observation.reviewed_at,
                                    observation.review_note, observation.reported_value_raw, observation.source_quote)
                    observation.evidence_key = evidence_key
                    for key, value in item.items():
                        if key in FinancialObservation.__table__.columns:
                            setattr(observation, key, value)
                    same_evidence = (prior_review[4] == observation.reported_value_raw and prior_review[5] == observation.source_quote)
                    if same_evidence and (prior_review[1] or prior_review[0] != "needs_review"):
                        observation.extraction_status, observation.reviewer, observation.reviewed_at, observation.review_note = prior_review[:4]
            run.pages_processed = page_count
            run.tables_detected = tables_total
            run.metrics_extracted = len(candidates)
            run.metrics_needing_review = len(candidates)
            run.status = "success"
            run.completed_at = datetime.now(timezone.utc)
            result["processed"] += 1
            result["pages_processed"] += page_count
            result["tables_detected"] += tables_total
            result["metrics_extracted"] += len(candidates)
        except Exception as exc:
            run.status = "failed"
            run.errors = f"{type(exc).__name__}: {exc}"
            run.completed_at = datetime.now(timezone.utc)
            result["failed"] += 1
            result["errors"].append({"document_id": document.id, "title": document.title, "error": run.errors})
        db.commit()
    if result["failed"]:
        result["status"] = "partial_failure"
    return result


def calculate_comparison(current: FinancialObservation, prior: FinancialObservation | None, kind: str) -> dict:
    if prior is None:
        return {"status": "unavailable", "reason": "No matching comparison period is stored."}
    if current.statement_basis != prior.statement_basis or current.currency != prior.currency or current.period_type != prior.period_type:
        return {"status": "unavailable", "reason": "Reporting basis, currency, or period type differs."}
    if current.normalised_value_in_crore is None or prior.normalised_value_in_crore is None:
        return {"status": "unavailable", "reason": "A comparable normalised value is unavailable."}
    if current.extraction_status != "validated" or prior.extraction_status != "validated":
        return {"status": "needs_review", "reason": "One or both source observations need verification."}
    current_value = Decimal(current.normalised_value_in_crore)
    prior_value = Decimal(prior.normalised_value_in_crore)
    absolute = current_value - prior_value
    if prior_value <= 0:
        return {"status": "unavailable", "reason": "Percentage change is not meaningful because the comparison base is zero or negative.", "absolute_change_crore": float(absolute)}
    return {"status": "available", "kind": kind, "absolute_change_crore": float(absolute), "percentage_change": float(absolute / prior_value * Decimal(100))}


def report_extraction(db: Session, ticker: str | None = None) -> dict:
    query = select(SourceDocument).join(Company).where(SourceDocument.local_file_path.is_not(None))
    if ticker:
        query = query.where(func.upper(Company.ticker) == ticker.upper())
    docs = db.scalars(query.order_by(SourceDocument.id)).all()
    observations_query = select(FinancialObservation).join(Company)
    if ticker:
        observations_query = observations_query.where(func.upper(Company.ticker) == ticker.upper())
    observations = db.scalars(observations_query.order_by(FinancialObservation.period_end, FinancialObservation.statement_basis, FinancialObservation.canonical_metric)).all()
    pages = db.scalars(select(DocumentPage).join(SourceDocument).join(Company).where(func.upper(Company.ticker) == ticker.upper()) if ticker else select(DocumentPage)).all()
    report = {
        "pdfs_discovered": len(docs),
        "pdfs_processed": sum(1 for doc in docs if db.scalar(select(func.count(DocumentPage.id)).where(DocumentPage.document_id == doc.id))),
        "pdfs_failed": sum(1 for doc in docs if db.scalar(select(DocumentExtractionRun.id).where(DocumentExtractionRun.document_id == doc.id, DocumentExtractionRun.status == "failed"))),
        "pages_processed": len(pages),
        "tables_detected": sum(page.table_count for page in pages),
        "quarterly_periods_identified": sorted({f"{item.fiscal_quarter} {item.fiscal_year}" for item in observations if item.period_type != "annual"}),
        "annual_periods_identified": sorted({item.fiscal_year for item in observations if item.period_type == "annual"}),
        "financial_observations": len(observations),
        "validated_observations": sum(item.extraction_status == "validated" for item in observations),
        "needs_review": sum(item.extraction_status == "needs_review" for item in observations),
        "pages_requiring_ocr": sum(page.extraction_status == "needs_ocr" for page in pages),
        "metrics_not_found": [metric for metric in ("revenue", "ebitda", "pbt", "pat") if not any(item.canonical_metric == metric for item in observations)],
        "items": [],
    }
    for item in observations:
        doc = db.get(SourceDocument, item.document_id)
        report["items"].append({
            "document_id": item.document_id, "document": doc.title if doc else None,
            "page": item.page_number, "statement_basis": item.statement_basis,
            "metric": item.canonical_metric, "period": item.fiscal_year if item.period_type == "annual" else f"{item.fiscal_quarter} {item.fiscal_year}",
            "row": item.row_label, "column": item.column_header,
            "raw_value": item.reported_value_raw, "normalised_value_in_crore": str(item.normalised_value_in_crore),
            "quote": item.source_quote, "status": item.extraction_status,
        })
    return report

