from __future__ import annotations

from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient

from backend.app import main


def test_job_routes_and_idempotency(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setenv("BROLL_PROVIDER", "demo")
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    monkeypatch.setattr(main, "process_job", lambda *args: None)

    with TestClient(main.app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        assert health.headers["x-request-id"]

        openapi = client.get("/openapi.json").json()
        assert openapi["openapi"] == "3.1.0"
        assert "requestBody" in openapi["paths"]["/api/v1/broll/jobs"]["post"]

        request = {
            "script": "A seed becomes a forest.",
            "aspect_ratios": ["16:9"],
            "captions_enabled": True,
        }
        created = client.post(
            "/api/v1/broll/jobs",
            json=request,
            headers={"Idempotency-Key": "assessment-request-1"},
        )
        assert created.status_code == 202
        assert created.json()["status"] == "queued"
        job_id = created.json()["job_id"]

        repeated = client.post(
            "/api/v1/broll/jobs",
            json=request,
            headers={"Idempotency-Key": "assessment-request-1"},
        )
        assert repeated.status_code == 202
        assert repeated.json()["job_id"] == job_id

        changed = client.post(
            "/api/v1/broll/jobs",
            json={**request, "script": "A different story."},
            headers={"Idempotency-Key": "assessment-request-1"},
        )
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == "conflict"

        status = client.get(f"/api/v1/jobs/{job_id}")
        assert status.status_code == 200
        assert status.json()["progress"] == 0
        assert client.get(f"/api/v1/jobs/{job_id}/result").status_code == 409
        assert client.get(f"/api/v1/jobs/{job_id}/timeline").status_code == 409
        assert client.get("/api/v1/jobs/not-a-job").status_code == 404

        ready = client.get("/ready")
        assert ready.status_code in {200, 503}
        assert {"database", "ffmpeg", "ffprobe", "footage_provider"} <= set(ready.json()["checks"])
        assert ready.json()["checks"]["footage_provider"] == {
            "selected": "demo",
            "active": "demo",
            "credentials": "not_required",
        }


def test_job_creation_rejects_missing_content(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setattr(main, "process_job", lambda *args: None)
    with TestClient(main.app) as client:
        response = client.post("/api/v1/broll/jobs", json={"aspect_ratios": ["16:9"]})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_repeated_script_requests_create_independent_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setattr(main, "process_job", lambda *args: None)
    payload = {"script": "A satellite orbits Earth.", "aspect_ratios": ["16:9"]}
    with TestClient(main.app) as client:
        first = client.post("/api/v1/broll/jobs", json=payload)
        second = client.post("/api/v1/broll/jobs", json=payload)
    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] != second.json()["job_id"]


def test_api_rejects_multiple_output_ratios_for_single_video_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setattr(main, "process_job", lambda *args: None)
    with TestClient(main.app) as client:
        response = client.post(
            "/api/v1/broll/jobs",
            json={"script": "A satellite orbits Earth.", "aspect_ratios": ["16:9", "9:16"]},
        )
    assert response.status_code == 422
    assert "at most 1 item" in str(response.json()["error"]["details"])


def test_multipart_audio_upload_is_accepted_and_temporary_file_is_cleaned(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "jobs.sqlite3"))
    finished = Event()

    def fake_processor(_job_id, _data, uploaded_audio):
        assert uploaded_audio
        Path(uploaded_audio).unlink(missing_ok=True)
        finished.set()

    monkeypatch.setattr(main, "process_job", fake_processor)
    with TestClient(main.app) as client:
        response = client.post(
            "/api/v1/broll/jobs",
            files={"audio": ("voice.mp3", b"test-audio-bytes", "audio/mpeg")},
            data={
                "scenes": '[{"text":"A manually described scene."}]',
                "aspect_ratios": '["16:9"]',
                "captions_enabled": "false",
            },
        )
        assert response.status_code == 202
        assert response.json()["status"] == "queued"
        assert finished.wait(timeout=3)
