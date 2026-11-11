from __future__ import annotations

import hashlib
from pathlib import Path

import requests

IR_PAGE = "https://www.infosys.com/investors/"
DOC_DIR = Path(__file__).resolve().parents[3] / "data" / "documents" / "INFY"
DOC_DIR.mkdir(parents=True, exist_ok=True)


class InfosysIRProvider:
    """Small official-source registry for Infosys annual and quarterly statements."""
    urls = [
        ("Infosys Integrated Annual Report FY 2024-25", "annual_report", "FY 2024 - 2025", None, "https://www.infosys.com/investors/reports-filings/annual-report/annual/documents/infosys-ar-25.pdf"),
        ("Infosys Q4 FY26 Financial Results", "quarterly_results", "FY 2025 - 2026", "Q4", "https://www.infosys.com/investors/reports-filings/quarterly-results/2025-2026/q4/documents/q4-and-12m-fy26-financial-results-auditorsreports.pdf"),
    ]

    def discover(self, download: bool = True):
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 FINN research prototype"
        documents, errors = [], []
        for title, kind, year, quarter, url in self.urls:
            doc = {"title": title, "document_type": kind, "fiscal_year": year, "quarter": quarter,
                   "document_date": None, "landing_page_url": IR_PAGE, "download_url": url,
                   "local_file_path": None, "source": "Infosys Investor Relations", "content_type": "application/pdf",
                   "discovery_status": "discovered", "dedupe_key": hashlib.sha256(f"INFY|{kind}|{url}".encode()).hexdigest()}
            if download:
                try:
                    response = session.get(url, timeout=25)
                    response.raise_for_status()
                    if not response.content.startswith(b"%PDF"):
                        raise ValueError("official URL did not return a PDF")
                    target = DOC_DIR / ("infosys-ar-fy25.pdf" if kind == "annual_report" else "q4-fy26-financial-results.pdf")
                    target.write_bytes(response.content)
                    doc["local_file_path"] = str(target.relative_to(DOC_DIR.parents[2]))
                    doc["file_hash"] = hashlib.sha256(response.content).hexdigest()
                    doc["discovery_status"] = "downloaded"
                except Exception as exc:
                    doc["discovery_status"] = "provider_error"
                    errors.append(f"{title}: {type(exc).__name__}: {exc}")
            documents.append(doc)
        return documents, errors
