"""Ship a pre-built database with the deployment, so the demo works even if live sources block cloud IPs.

`python -m backend.app.cli snapshot` writes data/snapshot/finn.db.gz (+ optional documents tarball).
On startup, if the configured SQLite file does not exist (fresh container), it is restored from the snapshot.
"""
from __future__ import annotations

import gzip
import shutil
import sqlite3
import tarfile
from pathlib import Path

from sqlalchemy.engine import make_url

from .database import DATA_DIR, DATABASE_URL

SNAPSHOT_DIR = DATA_DIR.parent / "snapshot"
DB_SNAPSHOT = SNAPSHOT_DIR / "finn.db.gz"
DOC_SNAPSHOT = SNAPSHOT_DIR / "documents.tar.gz"


def _sqlite_path() -> Path | None:
    url = make_url(DATABASE_URL)
    if not url.drivername.startswith("sqlite") or url.database in (None, ":memory:"):
        return None
    return Path(url.database)


def write_snapshot(include_pdfs: bool = False) -> dict:
    source = _sqlite_path()
    if source is None or not source.exists():
        return {"status": "failed", "error": "No SQLite database to snapshot"}
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT_DIR / "finn.db.tmp"
    with sqlite3.connect(source) as src, sqlite3.connect(tmp) as dst:
        src.backup(dst)
        dst.execute("DELETE FROM llm_calls WHERE status != 'success'")
        dst.commit()
        dst.execute("VACUUM")
    with tmp.open("rb") as raw, gzip.open(DB_SNAPSHOT, "wb", compresslevel=9) as out:
        shutil.copyfileobj(raw, out)
    tmp.unlink()
    result = {"status": "success", "database": str(DB_SNAPSHOT), "bytes": DB_SNAPSHOT.stat().st_size}
    if include_pdfs:
        docs = DATA_DIR / "documents"
        with tarfile.open(DOC_SNAPSHOT, "w:gz") as tar:
            tar.add(docs, arcname="documents")
        result["documents"] = str(DOC_SNAPSHOT)
        result["documents_bytes"] = DOC_SNAPSHOT.stat().st_size
    return result


def restore_snapshot_if_needed() -> dict | None:
    target = _sqlite_path()
    if target is None or target.exists() or not DB_SNAPSHOT.exists():
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(DB_SNAPSHOT, "rb") as packed, target.open("wb") as out:
        shutil.copyfileobj(packed, out)
    restored = {"database": str(target)}
    if DOC_SNAPSHOT.exists() and not (DATA_DIR / "documents").exists():
        with tarfile.open(DOC_SNAPSHOT, "r:gz") as tar:
            tar.extractall(DATA_DIR, filter="data")
        restored["documents"] = str(DATA_DIR / "documents")
    return restored
