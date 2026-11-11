from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
load_dotenv(PROJECT_ROOT / ".env", override=False)
DEFAULT_DATABASE_URL = f"sqlite:///{(DATA_DIR / 'finn.db').resolve()}"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
_database_url = make_url(DATABASE_URL)
if _database_url.drivername.startswith("sqlite") and _database_url.database not in (None, ":memory:"):
    _sqlite_path = Path(_database_url.database)
    if not _sqlite_path.is_absolute():
        _sqlite_path = (PROJECT_ROOT / _sqlite_path).resolve()
        _database_url = _database_url.set(database=str(_sqlite_path))
        DATABASE_URL = _database_url.render_as_string(hide_password=False)
DATABASE_IDENTIFIER = (
    str(Path(_database_url.database).resolve())
    if _database_url.drivername.startswith("sqlite") and _database_url.database not in (None, ":memory:")
    else f"{_database_url.drivername}:{_database_url.database or 'memory'}"
)
CONNECT_ARGS = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}


class Base(DeclarativeBase):
    pass


engine = create_engine(DATABASE_URL, connect_args=CONNECT_ARGS, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def upgrade_schema(target_engine=engine) -> None:
    """Apply additive schema changes that create_all cannot make on an existing DB."""
    # New feature tables are created explicitly here as a migration-safe step;
    # the existing Day 1–3 tables and data are left untouched.
    from .models import AlertEvent, AlertPreference, CommitmentAssessment, ManagementStatement

    AlertPreference.__table__.create(target_engine, checkfirst=True)
    AlertEvent.__table__.create(target_engine, checkfirst=True)
    ManagementStatement.__table__.create(target_engine, checkfirst=True)
    CommitmentAssessment.__table__.create(target_engine, checkfirst=True)
    inspector = inspect(target_engine)
    if "financial_observations" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("financial_observations")}
        additions = {
            "reviewer": "VARCHAR(160)",
            "reviewed_at": "DATETIME",
            "review_note": "TEXT",
        }
        with target_engine.begin() as connection:
            for name, sql_type in additions.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE financial_observations ADD COLUMN {name} {sql_type}"))
                    columns.add(name)
    if "document_pages" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("document_pages")}
        if "page_classification" not in columns:
            with target_engine.begin() as connection:
                connection.execute(text("ALTER TABLE document_pages ADD COLUMN page_classification VARCHAR(80) NOT NULL DEFAULT 'document_page'"))
