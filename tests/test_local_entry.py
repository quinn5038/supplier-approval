import os

import pytest
from fastapi.testclient import TestClient

import auto_approve
from web.app import app


@pytest.fixture
def local_client(monkeypatch):
    from web import app as web_app
    monkeypatch.setenv("WEB_AUTH_MODE", "local")
    monkeypatch.delenv("WEB_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("SCPMA_COOKIE", "")
    monkeypatch.setenv("SCPMA_AUTH_TOKEN", "")
    for name in ("APP_TOKEN", "AGENT_ID"):
        monkeypatch.setenv(name, "")
        monkeypatch.setattr(auto_approve, name, "")
    monkeypatch.setenv("CODE", "")
    monkeypatch.setattr(web_app, "_task_status", {})
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        yield client


def test_local_entry_memory_credentials(local_client, monkeypatch):
    from pathlib import Path
    assert local_client.get("/").status_code == 200
    assert local_client.get("/api/todos").status_code == 401
    headers = {"origin": "http://localhost", "x-csrf-token": local_client.cookies.get("supplier_csrf")}
    def forbid_write(*args, **kwargs):
        raise AssertionError("Credentials must never be written to disk")
    monkeypatch.setattr(Path, "write_text", forbid_write)
    monkeypatch.setattr(Path, "write_bytes", forbid_write)
    data = {"cookie": "session=synthetic", "auth_token": "Bearer synthetic",
            "app_token": "app-synthetic", "agent_id": "agent-synthetic", "code": "code-synthetic"}
    assert local_client.post("/api/cookie", data=data).status_code == 403
    assert local_client.post("/api/cookie", data=data, headers={**headers, "origin": "https://evil.test"}).status_code == 403
    result = local_client.post("/api/cookie", data=data, headers=headers)
    assert result.status_code == 200
    assert "synthetic" not in result.text
    assert auto_approve._scpma_headers()["Authorization"] == "Bearer synthetic"
    assert auto_approve._asca_headers()["APP_TOKEN"] == "app-synthetic"
    assert auto_approve.AGENT_ID == "agent-synthetic"
    assert os.environ["CODE"] == ""
    assert os.environ.copy()["SCPMA_COOKIE"] == "session=synthetic"
    monkeypatch.setattr(auto_approve, "fetch_pending_todos", lambda: {"data": {"rows": [], "recordsTotal": 0}})
    assert local_client.get("/api/todos").status_code == 200


def test_local_entry_blocks_remote_and_cross_site(local_client):
    assert local_client.get("/", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert local_client.get("/", headers={"host": "evil.test"}).status_code == 400
    with TestClient(app, base_url="http://localhost", client=("192.168.1.20", 12345)) as remote:
        assert remote.get("/").status_code == 403


def test_credentials_not_switched_during_processing(local_client, monkeypatch):
    from web import app as web_app
    local_client.get("/")
    monkeypatch.setattr(web_app, "_task_status", {"1": {"status": "running"}})
    response = local_client.post("/api/cookie", data={"cookie": "a=b", "auth_token": "token"},
                                 headers={"origin": "http://localhost", "x-csrf-token": local_client.cookies.get("supplier_csrf")})
    assert response.status_code == 409
    assert os.environ["SCPMA_COOKIE"] == ""
