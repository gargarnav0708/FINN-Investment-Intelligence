from __future__ import annotations

import hashlib
import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

IR_PAGE = "https://www.tatapower.com/investor-resource-center/analyst-call-transcript-tab"
BASE = "https://www.tatapower.com"
DOC_DIR = Path(__file__).resolve().parents[3] / "data" / "documents" / "TATAPOWER"
DOC_DIR.mkdir(parents=True, exist_ok=True)


class TataPowerIRProvider:
    def __init__(self, timeout: int = 25):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; FINNPrototype/1.0; public-document-discovery)", "Accept": "text/html,application/json,*/*"})

    def discover(self, download: bool = True) -> tuple[list[dict], list[str]]:
        page = self.session.get(IR_PAGE, timeout=self.timeout)
        page.raise_for_status()
        soup = BeautifulSoup(page.content, "html.parser")
        panel_ids = {
            "annual_report": "annual-reports-tabpanel-0",
            "transcript": "analyst-call-transcript-tabpanel-5",
            "quarterly_results": "quarterly-reports-tabpanel-1",
        }
        docs: list[dict] = []
        errors: list[str] = []
        for kind, panel_id in panel_ids.items():
            panel = soup.find(id=panel_id)
            resource = panel.select_one("input.currentResource") if panel else None
            node = resource.get("data-current-node") if resource else None
            if not node:
                errors.append(f"Could not resolve Tata Power {kind} resource node from the official page")
                continue
            try:
                endpoint = f"{BASE}{node}.tablesearch.json?d=des"
                response = self.session.get(endpoint, headers={"Referer": IR_PAGE}, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                records = data.get("results", [])
                if not isinstance(records, list):
                    raise ValueError("resource response did not include a results list")
                for record in records:
                    title = _clean(record.get("pageTitle"))
                    relative = record.get("pagePath")
                    if not title or not relative:
                        continue
                    download_url = urljoin(BASE, relative)
                    doc_type = {"annual_report": "annual_report", "transcript": "earnings_call_transcript", "quarterly_results": "quarterly_results"}[kind]
                    if kind == "annual_report" and not _is_annual_report(title):
                        continue
                    if kind == "transcript" and not _is_transcript(title):
                        continue
                    if kind == "quarterly_results" and not _is_quarterly_results(title, record.get("quarter")):
                        continue
                    fiscal_year = _clean(record.get("year")) or None
                    quarter = _clean(record.get("quarter")) or None
                    doc = {
                        "title": title,
                        "document_type": doc_type,
                        "fiscal_year": fiscal_year,
                        "quarter": quarter,
                        "document_date": _date_from_title(title),
                        "landing_page_url": IR_PAGE,
                        "download_url": download_url,
                        "source": "Tata Power Investor Resource Center",
                        "content_type": "application/pdf" if download_url.lower().endswith(".pdf") else None,
                        "discovery_status": "discovered",
                    }
                    docs.append(doc)
            except Exception as exc:
                errors.append(f"Tata Power {kind} lookup failed: {type(exc).__name__}: {exc}")
            time.sleep(0.25)
        # Keep one latest annual report and the four most recent transcripts/results.
        # Keep three completed reports locally so annual comparisons can be built
        # from explicit audited-year statements rather than summed quarterlies.
        annuals = sorted((d for d in docs if d["document_type"] == "annual_report"), key=lambda d: (d["fiscal_year"] or "", d["title"]), reverse=True)[:3]
        transcripts = sorted((d for d in docs if d["document_type"] == "earnings_call_transcript"), key=_quarter_sort, reverse=True)[:4]
        quarters = sorted((d for d in docs if d["document_type"] == "quarterly_results"), key=_quarter_sort, reverse=True)[:4]
        chosen = annuals + transcripts + quarters
        for doc in chosen:
            doc["dedupe_key"] = hashlib.sha256(f"TATAPOWER|{doc['document_type']}|{doc['download_url']}".encode()).hexdigest()
            if download and doc["download_url"].lower().split("?")[0].endswith(".pdf"):
                try:
                    local_path, file_hash = self._download(doc["download_url"])
                    doc["local_file_path"] = str(local_path.relative_to(DOC_DIR.parents[2]))
                    doc["file_hash"] = file_hash
                    doc["discovery_status"] = "downloaded"
                except Exception as exc:
                    doc["discovery_status"] = "link_only"
                    errors.append(f"PDF download failed for {doc['title']}: {type(exc).__name__}: {exc}")
        return chosen, errors

    def _download(self, url: str) -> tuple[Path, str]:
        response = self.session.get(url, headers={"Referer": IR_PAGE}, timeout=self.timeout, stream=True)
        response.raise_for_status()
        digest = hashlib.sha256()
        safe_name = Path(url.split("?")[0]).name
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", safe_name)
        target = DOC_DIR / safe_name
        if target.exists() and target.stat().st_size > 4:
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if target.read_bytes()[:4] == b"%PDF":
                return target, digest
        temp = target.with_suffix(target.suffix + ".part")
        first = b""
        size = 0
        with temp.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 128):
                if not chunk:
                    continue
                size += len(chunk)
                if size > 60 * 1024 * 1024:
                    raise ValueError("PDF exceeds 60 MB safety limit")
                if not first:
                    first = chunk[:8]
                digest.update(chunk)
                output.write(chunk)
        if not first.startswith(b"%PDF"):
            temp.unlink(missing_ok=True)
            raise ValueError(f"download did not return a PDF (content type {response.headers.get('Content-Type')})")
        temp.replace(target)
        return target, digest.hexdigest()


def _quarter_sort(doc: dict) -> tuple[int, int]:
    year_match = re.search(r"(?:FY\s*)?(20\d{2})\s*[-/]?\s*(20\d{2})?", doc.get("fiscal_year") or doc.get("title") or "", re.I)
    year = int(year_match.group(1)) if year_match else 0
    q_match = re.search(r"Q([1-4])", (doc.get("quarter") or "") + " " + (doc.get("title") or ""), re.I)
    quarter = int(q_match.group(1)) if q_match else 0
    return year, quarter


def _is_annual_report(title: str) -> bool:
    return "annualreport" in title.lower().replace(" ", "") or "annual report" in title.lower()


def _is_transcript(title: str) -> bool:
    return "transcript" in title.lower() and ("earning" in title.lower() or "analyst" in title.lower() or "call" in title.lower())


def _is_quarterly_results(title: str, quarter: str | None = None) -> bool:
    text = title.lower()
    return "financial" in text and "results" in text and str(quarter or "").upper() in {"Q1", "Q2", "Q3", "Q4"}


def _date_from_title(title: str) -> date | None:
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?[- ](?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[- ,]+(20\d{2})", title, re.I)
    if not m:
        return None
    # Publication dates are not consistently exposed in this metadata feed.
    return None


def _clean(value) -> str:
    return " ".join(str(value or "").split())
