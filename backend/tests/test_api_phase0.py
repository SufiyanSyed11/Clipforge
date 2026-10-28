from __future__ import annotations

from fastapi.testclient import TestClient

from clipforge.api.app import create_app
from clipforge.jobs.worker import run_one


def client() -> TestClient:
    return TestClient(create_app())


def test_health():
    r = client().get("/api/health")
    assert r.status_code == 200 and r.json()["ok"]
    assert "ping" in r.json()["job_kinds"]


def test_ping_job_end_to_end_over_http_and_sse():
    c = client()
    r = c.post("/api/jobs", json={"kind": "ping", "payload": {"steps": 2, "delay": 0}})
    assert r.status_code == 201
    job_id = r.json()["id"]
    run_one("api-test-worker")
    with c.stream("GET", f"/api/jobs/{job_id}/events") as resp:
        body = "".join(resp.iter_text())
    assert "ping step 2/2" in body
    assert "event: end" in body
    job = c.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "done"
    assert job["qa"][0]["checks"][0]["passed"] is True


def test_pipeline_jobs_cannot_be_created_directly():
    r = client().post("/api/jobs", json={"kind": "process_project", "payload": {}})
    assert r.status_code == 422


def test_settings_defaults_and_hard_max():
    c = client()
    cfg = c.get("/api/settings").json()
    assert cfg["brand_tag"] == "#DoubleCoverage"
    assert cfg["frame_rate_mode"] == "repeat"
    assert cfg["profanity_mode"] == "prefer_alternative_then_bleep"
    assert "women" in cfg["broll_banned_content"]
    assert cfg["style"]["max_clip_seconds"] == 30
    cfg["style"]["max_clip_seconds"] = 45
    assert c.put("/api/settings", json=cfg).status_code == 422
    cfg["style"]["max_clip_seconds"] = 28
    cfg["broll_banned_content"] = ["logos"]
    assert c.put("/api/settings", json=cfg).status_code == 200
    again = c.get("/api/settings").json()
    assert again["style"]["max_clip_seconds"] == 28
    assert again["broll_banned_content"] == ["logos"]
