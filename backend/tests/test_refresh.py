from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import main as main_module
from backend.app.database import Base, get_db
from backend.app.main import app
from backend.app.models import IngestionRun


def test_refresh_endpoint_reuses_running_job_and_exposes_status(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db():
        with Session() as db:
            yield db

    monkeypatch.setattr(main_module, "_run_refresh_job", lambda _job_id: None)
    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        first = client.post("/api/refresh")
        assert first.status_code == 202
        body = first.json()
        assert body["status"] == "running"
        second = client.post("/api/refresh").json()
        assert second["already_running"] is True
        assert second["job_id"] == body["job_id"]
        status = client.get(f"/api/refresh/{body['job_id']}").json()
        assert status["status"] == "running"
        with Session() as db:
            job = db.get(IngestionRun, body["job_id"])
            job.status = "partial"
            job.metadata_json = {"market": {"status": "success"}, "announcements": {"status": "failed"}}
            db.commit()
        status = client.get(f"/api/refresh/{body['job_id']}").json()
        assert status["status"] == "partial"
        assert status["details"]["market"]["status"] == "success"
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

