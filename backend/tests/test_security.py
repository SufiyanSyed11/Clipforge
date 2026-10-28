"""Local-only API guard, secrets never exposed, file-serving confined to the data folder."""

from __future__ import annotations

from fastapi.testclient import TestClient

from clipforge.api.app import create_app
from tests.helpers import api_client


def test_writes_need_the_clipforge_header():
    bare = TestClient(create_app(), base_url="http://127.0.0.1")
    r = bare.post("/api/jobs", json={"kind": "ping", "payload": {}})
    assert r.status_code == 403  # a cross-site form post can't add this header
    assert bare.get("/api/health").status_code == 200  # reads are fine
    assert api_client().post("/api/jobs", json={"kind": "ping", "payload": {}}).status_code == 201


def test_dns_rebinding_host_is_refused():
    evil = TestClient(
        create_app(), base_url="http://evil.example.com", headers={"X-ClipForge": "1"}
    )
    assert evil.get("/api/health").status_code == 400
    for host in ("http://localhost", "http://127.0.0.1:8000"):
        assert TestClient(create_app(), base_url=host).get("/api/health").status_code == 200


def test_cors_preflight_only_for_the_web_app():
    c = TestClient(create_app(), base_url="http://127.0.0.1")
    ok = c.options(
        "/api/jobs",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-clipforge,content-type",
        },
    )
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:3000"
    bad = c.options(
        "/api/jobs",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-clipforge",
        },
    )
    assert "access-control-allow-origin" not in bad.headers


def test_keys_never_leave_the_server(monkeypatch):
    from clipforge.settings import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-123")
    monkeypatch.setenv("PEXELS_API_KEY", "pexels-secret-456")
    get_settings.cache_clear()
    c = api_client()
    for path in ("/api/llm/status", "/api/settings", "/api/health", "/api/learning"):
        body = c.get(path).text
        assert "secret" not in body, path
    assert c.get("/api/llm/status").json()["stock"]["pexels"] is True


def test_oauth_callback_escapes_its_input():
    r = api_client().get("/api/learning/youtube/callback", params={"error": "<script>x()</script>"})
    assert r.status_code == 400 and "<script>" not in r.text and "&lt;script&gt;" in r.text


def test_files_served_only_from_inside_the_project(tmp_path):
    from clipforge.db import Clip, Project, session_scope

    secret = tmp_path / "secret.mp4"
    secret.write_bytes(b"not yours")
    with session_scope() as s:
        p = Project(name="x", source_type="upload", source="x", rights_confirmed=True)
        s.add(p)
        s.flush()
        c = Clip(project_id=p.id, rank=1, plan={}, render_path=str(secret), export_path=str(secret))
        s.add(c)
        s.flush()
        cid = c.id
    client = api_client()
    assert client.get(f"/api/clips/{cid}/video").status_code == 404
    assert client.get(f"/api/clips/{cid}/export").status_code == 404


def test_upload_size_limit(tmp_path, monkeypatch):
    import io

    from clipforge.settings import get_settings

    monkeypatch.setenv("MAX_UPLOAD_GB", str(1 / 1024**3 * 1000))  # 1000 bytes
    get_settings.cache_clear()
    r = api_client().post(
        "/api/projects",
        data={"rights_confirmed": "true"},
        files={"file": ("ep.mp4", io.BytesIO(b"x" * 5000), "video/mp4")},
    )
    assert r.status_code == 413
    assert api_client().get("/api/projects").json() == []
