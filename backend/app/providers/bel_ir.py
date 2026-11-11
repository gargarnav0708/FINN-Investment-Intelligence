from __future__ import annotations

import hashlib
import re
from pathlib import Path

import requests

IR_PAGE = "https://bel-india.in/investors/"
ANNUAL_REPORT_URL = "https://bel-india.in/wp-content/uploads/2025/08/Integrated-Annual-Report-2024-25.pdf"
DOC_DIR = Path(__file__).resolve().parents[3] / "data" / "documents" / "BEL"
DOC_DIR.mkdir(parents=True, exist_ok=True)


class BELIRProvider:
    """Official BEL annual-report source configuration; only issuer materials are registered."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; FINNPrototype/1.0; public-document-discovery)"})

    def discover(self, download: bool = True) -> tuple[list[dict], list[str]]:
        title = "BEL Integrated Annual Report 2024-25"
        doc = {
            "title": title, "document_type": "annual_report", "fiscal_year": "FY 2024 - 2025",
            "quarter": None, "document_date": None, "landing_page_url": IR_PAGE,
            "download_url": ANNUAL_REPORT_URL, "source": "Bharat Electronics Limited Investor Relations",
            "content_type": "application/pdf", "discovery_status": "discovered",
            "dedupe_key": hashlib.sha256(f"BEL|annual_report|{ANNUAL_REPORT_URL}".encode()).hexdigest(),
        }
        errors=[]
        if download:
            try:
                path,digest=self._download(ANNUAL_REPORT_URL)
                doc["local_file_path"]=str(path.relative_to(DOC_DIR.parents[2]))
                doc["file_hash"]=digest
                doc["discovery_status"]="downloaded"
            except Exception as exc:
                doc["discovery_status"]="link_only"
                errors.append(f"PDF download failed for {title}: {type(exc).__name__}: {exc}")
        return [doc],errors

    def _download(self,url: str) -> tuple[Path,str]:
        response=self.session.get(url,timeout=self.timeout,stream=True)
        response.raise_for_status()
        target=DOC_DIR / Path(url.split("?")[0]).name
        temp=target.with_suffix(target.suffix+".part")
        digest=hashlib.sha256();first=b"";size=0
        with temp.open("wb") as output:
            for chunk in response.iter_content(chunk_size=128*1024):
                if not chunk: continue
                size+=len(chunk)
                if size>60*1024*1024: raise ValueError("PDF exceeds 60 MB safety limit")
                if not first:first=chunk[:8]
                digest.update(chunk);output.write(chunk)
        if not first.startswith(b"%PDF"):
            temp.unlink(missing_ok=True)
            raise ValueError(f"download did not return a PDF (content type {response.headers.get('Content-Type')})")
        temp.replace(target)
        return target,digest.hexdigest()
