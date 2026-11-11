from __future__ import annotations

import hashlib
import time
from datetime import date, datetime, timezone
from urllib.parse import urljoin

import requests

NSE_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
NSE_API = "https://www.nseindia.com/api/corporate-announcements"
NSE_ARCHIVE = "https://nsearchives.nseindia.com/"


def normalize_category(subject: str, details: str | None = None) -> str:
    text = f"{subject} {details or ''}".lower()
    rules = [
        ("Results", ("financial results", "quarterly results", "audited results", "unaudited results")),
        ("Management change", ("resignation", "appointment of", "cessation of", "change in director", "key managerial personnel")),
        ("M&A", ("acquisition", "merger", "amalgamation", "scheme of arrangement")),
        ("Fundraising", ("fund raising", "fundraising", "qip", "preferential issue", "rights issue", "non-convertible debenture")),
        ("Capital allocation", ("capex", "capital expenditure", "buyback", "dividend", "investment plan")),
        ("Investor meeting", ("analyst", "investor meeting", "concall", "conference call", "investor presentation")),
        ("Shareholding", ("shareholding pattern", "acquisition of shares", "encumbrance", "pledge")),
        ("Regulatory/compliance", ("regulation", "compliance", "secretarial", "reconciliation of share capital", "corporate governance")),
    ]
    for category, terms in rules:
        if any(term in text for term in terms):
            return category
    return "Unclassified"


def parse_announcement(item: dict, ticker: str, fetched_at: datetime | None = None) -> dict:
    subject = _clean(item.get("subject") or item.get("desc") or "")
    details = _clean(item.get("desc") or item.get("attchmntText") or "")
    raw_dt = item.get("an_dt") or item.get("sort_date") or ""
    parsed_dt = None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y"):
        try:
            parsed_dt = datetime.strptime(raw_dt, fmt)
            break
        except (TypeError, ValueError):
            pass
    attachment = item.get("attchmntFile")
    if attachment:
        attachment = urljoin(NSE_ARCHIVE, str(attachment))
    external_id = str(item.get("seq_id") or item.get("id") or "") or None
    source_page = NSE_PAGE
    if external_id:
        dedupe_material = f"NSE:{external_id}"
    else:
        dedupe_material = "|".join(("NSE", ticker, raw_dt, subject, attachment or ""))
    return {
        "ticker": ticker,
        "exchange": "NSE",
        "external_announcement_id": external_id,
        "dedupe_key": hashlib.sha256(dedupe_material.encode()).hexdigest(),
        "subject": subject or "(subject missing in exchange response)",
        "details": details or None,
        "original_category": _clean(item.get("category") or item.get("announcementType") or "") or None,
        "normalized_category": normalize_category(subject, details),
        "announcement_datetime": parsed_dt,
        "attachment_url": attachment,
        "source_page_url": source_page,
        "source": "NSE",
        "raw_metadata": item,
        "ingested_at": fetched_at or datetime.now(timezone.utc),
    }


class NSEProvider:
    def __init__(self, delay_seconds: float = 0.35, timeout: int = 20):
        self.delay_seconds = delay_seconds
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": NSE_PAGE,
            "Origin": "https://www.nseindia.com",
        })
        self.ready = False

    def fetch(self, ticker: str, start: date, end: date) -> list[dict]:
        if not self.ready:
            self._get(NSE_PAGE)
            self.ready = True
        time.sleep(self.delay_seconds)
        params = {
            "index": "equities",
            "from_date": start.strftime("%d-%m-%Y"),
            "to_date": end.strftime("%d-%m-%Y"),
            "symbol": ticker,
        }
        response = self._get(NSE_API, params=params)
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError(f"NSE response for {ticker} was not a list")
        fetched_at = datetime.now(timezone.utc)
        return [parse_announcement(item, ticker, fetched_at) for item in payload]

    def _get(self, url: str, **kwargs):
        last_error = None
        for attempt in range(3):
            try:
                response = self.session.get(url, timeout=self.timeout, **kwargs)
                if response.status_code == 429 and attempt < 2:
                    wait_for = response.headers.get("Retry-After", "1")
                    try:
                        time.sleep(min(3.0, max(0.5, float(wait_for))))
                    except ValueError:
                        time.sleep(1.0)
                    continue
                if response.status_code in {500, 502, 503, 504} and attempt < 2:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in {401, 403, 404} or attempt >= 2:
                    raise
                time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"NSE request failed after bounded retries: {last_error}") from last_error


def _clean(value) -> str:
    return " ".join(str(value or "").split())
