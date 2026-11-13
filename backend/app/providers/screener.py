"""screener.in adapter: primary-document links and standardised financial tables for any NSE ticker.

screener.in is used for two things only:
  * as a link directory to the company's own documents (annual reports and concall transcripts,
    which it links on BSE / the company's IR site) - the documents themselves are primary;
  * its standardised quarterly / annual tables (built from exchange filings) as the "delivered"
    baseline when a results filing has not been machine-read.
No screener commentary, AI summaries or ratios are used.
"""
from __future__ import annotations

import re
import time
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

BASE = "https://www.screener.in"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}
DOC_HEADERS = {**HEADERS, "Accept": "application/pdf,*/*;q=0.8", "Referer": BASE + "/"}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

ROW_MAP = {
    "quarters": {
        "sales": "revenue", "revenue": "revenue", "operating profit": "ebitda", "financing profit": "ebitda",
        "opm %": "ebitda_margin_pct", "financing margin %": "ebitda_margin_pct", "other income": "other_income",
        "interest": "finance_costs", "depreciation": "depreciation", "profit before tax": "pbt",
        "tax %": "tax_rate_pct", "net profit": "pat", "eps in rs": "eps",
    },
    "profit-loss": {
        "sales": "revenue", "revenue": "revenue", "operating profit": "ebitda", "financing profit": "ebitda",
        "opm %": "ebitda_margin_pct", "financing margin %": "ebitda_margin_pct", "other income": "other_income",
        "interest": "finance_costs", "depreciation": "depreciation", "profit before tax": "pbt",
        "tax %": "tax_rate_pct", "net profit": "pat", "eps in rs": "eps", "dividend payout %": "dividend_payout_pct",
    },
    "balance-sheet": {
        "equity capital": "equity_capital", "reserves": "reserves", "borrowings": "borrowings",
        "other liabilities": "other_liabilities", "total liabilities": "total_liabilities",
        "fixed assets": "fixed_assets", "cwip": "cwip", "investments": "investments",
        "other assets": "other_assets", "total assets": "total_assets", "deposits": "deposits",
    },
    "cash-flow": {
        "cash from operating activity": "cfo", "cash from investing activity": "cfi",
        "cash from financing activity": "cff", "net cash flow": "net_cash_flow",
    },
}
STATEMENT_TYPE = {"quarters": "income", "profit-loss": "income", "balance-sheet": "balance_sheet", "cash-flow": "cash_flow"}


@dataclass
class ScreenerDocument:
    document_type: str  # annual_report | earnings_call_transcript | investor_presentation
    title: str
    url: str
    period_label: str
    document_date: date | None
    fiscal_year: str | None = None


@dataclass
class ScreenerRow:
    section: str
    statement_type: str
    period_type: str
    period_end: date
    metric: str
    label: str
    value: Decimal


@dataclass
class ScreenerPage:
    ticker: str
    url: str
    basis: str
    name: str | None = None
    about: str | None = None
    documents: list[ScreenerDocument] = field(default_factory=list)
    rows: list[ScreenerRow] = field(default_factory=list)


def _month_end(label: str) -> date | None:
    match = re.search(r"([A-Za-z]{3})[a-z]*\s+(\d{4})", label)
    if not match or match.group(1).lower() not in MONTHS:
        return None
    month, year = MONTHS[match.group(1).lower()], int(match.group(2))
    return date(year, month, monthrange(year, month)[1])


def _fy_label(period_end: date) -> str:
    start = period_end.year if period_end.month >= 4 else period_end.year - 1
    return f"FY {start} - {start + 1}"


def _number(text: str) -> Decimal | None:
    cleaned = text.replace(",", "").replace("%", "").replace("\xa0", " ").strip()
    if not cleaned or cleaned in {"-", "—"}:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _section(soup: BeautifulSoup, *ids_or_titles: str) -> Tag | None:
    for key in ids_or_titles:
        node = soup.find(id=key)
        if node:
            return node
    for heading in soup.find_all(["h2", "h3"]):
        text = heading.get_text(" ", strip=True).lower()
        if any(key.replace("-", " ") in text for key in ids_or_titles):
            return heading.find_parent(["section", "div"]) or heading.parent
    return None


def parse_documents(soup: BeautifulSoup) -> list[ScreenerDocument]:
    docs: list[ScreenerDocument] = []
    seen: set[str] = set()

    annual = soup.select_one("div.annual-reports") or _documents_block(soup, "annual reports")
    if annual:
        for link in annual.find_all("a", href=True):
            title = " ".join(link.get_text(" ", strip=True).split())
            year = re.search(r"(?:Financial\s+Year|Annual\s+Report|FY)\s*(?:20\d{2}\s*[-–]\s*)?(20\d{2}|\d{2})\b", title, re.I)
            if not year:
                continue
            token = year.group(1)
            end_year = int(token) if len(token) == 4 else 2000 + int(token)
            url = urljoin(BASE, link["href"])
            if url in seen:
                continue
            seen.add(url)
            docs.append(ScreenerDocument("annual_report", f"Annual Report FY{end_year}", url, f"FY{end_year}", date(end_year, 3, 31), f"FY {end_year - 1} - {end_year}"))

    concalls = soup.select_one("div.concalls") or _documents_block(soup, "concalls")
    if concalls:
        for item in concalls.find_all("li"):
            label_node = item.find("div")
            label = " ".join((label_node.get_text(" ", strip=True) if label_node else item.get_text(" ", strip=True)).split())
            call_month = _month_end(label)
            for link in item.find_all("a", href=True):
                text = link.get_text(" ", strip=True).lower()
                title_attr = (link.get("title") or "").lower()
                if "transcript" in text or "transcript" in title_attr:
                    doc_type = "earnings_call_transcript"
                elif text in {"ppt", "presentation"}:
                    doc_type = "investor_presentation"
                else:
                    continue
                url = urljoin(BASE, link["href"])
                if url in seen:
                    continue
                seen.add(url)
                nice = label or "Concall"
                docs.append(ScreenerDocument(doc_type, f"{'Earnings call transcript' if doc_type == 'earnings_call_transcript' else 'Investor presentation'} {nice}",
                                             url, nice, call_month, _fy_label(call_month) if call_month else None))
    return docs


def _documents_block(soup: BeautifulSoup, title: str) -> Tag | None:
    for heading in soup.find_all(["h3", "h4"]):
        if title in heading.get_text(" ", strip=True).lower():
            return heading.find_parent("div")
    return None


def parse_tables(soup: BeautifulSoup) -> list[ScreenerRow]:
    rows: list[ScreenerRow] = []
    for section_id, mapping in ROW_MAP.items():
        section = _section(soup, section_id)
        if not section:
            continue
        table = section.find("table")
        if not table:
            continue
        header_cells = table.find("thead").find_all("th") if table.find("thead") else table.find("tr").find_all(["th", "td"])
        periods = [_month_end(cell.get_text(" ", strip=True)) for cell in header_cells[1:]]
        body = table.find("tbody") or table
        for tr in body.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 2:
                continue
            label = " ".join(cells[0].get_text(" ", strip=True).replace("+", " ").split())
            key = label.lower().strip()
            metric = mapping.get(key)
            if metric is None:
                continue
            for period_end, cell in zip(periods, cells[1:]):
                value = _number(cell.get_text(" ", strip=True))
                if period_end is None or value is None:
                    continue
                rows.append(ScreenerRow(section_id, STATEMENT_TYPE[section_id], "quarterly" if section_id == "quarters" else "annual",
                                        period_end, metric, label, value))
    return rows


def parse_company_page(html: str, ticker: str, url: str) -> ScreenerPage:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.find("h1")
    basis = "consolidated" if "/consolidated" in url else "standalone"
    about = None
    about_node = soup.select_one("div.about p") or soup.select_one("div.company-profile p")
    if about_node:
        about = " ".join(about_node.get_text(" ", strip=True).split())
    return ScreenerPage(ticker=ticker.upper(), url=url, basis=basis, name=title.get_text(" ", strip=True) if title else None,
                        about=about, documents=parse_documents(soup), rows=parse_tables(soup))


def fetch_company(ticker: str, session: requests.Session | None = None, timeout: int = 25) -> ScreenerPage:
    """Fetch the consolidated page, falling back to standalone when no consolidated accounts exist."""
    http = session or requests.Session()
    last_error = None
    for suffix in ("consolidated/", ""):
        url = f"{BASE}/company/{ticker.upper()}/{suffix}"
        try:
            response = http.get(url, headers=HEADERS, timeout=timeout)
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        if response.status_code == 404:
            last_error = "screener.in has no page for this ticker"
            continue
        if response.status_code >= 400:
            last_error = f"HTTP {response.status_code}"
            time.sleep(1.5)
            continue
        page = parse_company_page(response.text, ticker, response.url)
        if suffix and not any(r.section == "quarters" for r in page.rows):
            continue  # consolidated view empty for standalone-only companies
        page.basis = "consolidated" if suffix else "standalone"
        return page
    raise RuntimeError(f"screener.in unavailable for {ticker.upper()}: {last_error}")


def download_pdf(url: str, timeout: int = 60, max_bytes: int = 60_000_000) -> bytes:
    response = requests.get(url, headers=DOC_HEADERS, timeout=timeout, stream=True, allow_redirects=True)
    response.raise_for_status()
    chunks, size = [], 0
    for chunk in response.iter_content(1 << 16):
        size += len(chunk)
        if size > max_bytes:
            raise ValueError("Document exceeds the download size limit")
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content.startswith(b"%PDF"):
        raise ValueError("Linked document is not a PDF")
    return content
