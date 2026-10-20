import os

import pytest
from fastapi.testclient import TestClient

import auto_approve
from web.app import app
from web.security import CSRF_COOKIE


@pytest.fixture
def local_client(monkeypatch, tmp_path):
    from web import app as web_app
    monkeypatch.setattr(web_app, "BASE_DIR", tmp_path)
    monkeypatch.setenv("OCR_MODE", "online")
    monkeypatch.setenv("WEB_AUTH_MODE", "local")
    monkeypatch.delenv("WEB_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("SCPMA_COOKIE", "")
    monkeypatch.setenv("SCPMA_AUTH_TOKEN", "")
    for name in ("APP_TOKEN", "AGENT_ID"):
        monkeypatch.setenv(name, "")
        monkeypatch.setattr(auto_approve, name, "")
    monkeypatch.setenv("CODE", "")
    # Never let tests inspect or remove the real user's saved credential file.
    monkeypatch.setenv("SUPPLIER_CREDENTIAL_FILE", str(tmp_path / "credentials.bin"))
    monkeypatch.setattr(web_app, "_task_status", {})
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        yield client


def test_local_entry_memory_credentials(local_client, monkeypatch):
    from pathlib import Path
    assert local_client.get("/").status_code == 200
    assert local_client.get("/api/todos").status_code == 401
    headers = {"origin": "http://localhost", "x-csrf-token": local_client.cookies.get(CSRF_COOKIE)}
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


def test_active_ocr_edition_is_visible_on_main_pages(local_client):
    home = local_client.get("/")
    detail = local_client.get("/approve/synthetic")
    preflight = local_client.get("/preflight")
    for response in (home, detail, preflight):
        assert response.status_code == 200
        assert "在线 OCR 版" in response.text
        assert "ocr-edition-badge" in response.text


def test_unified_csrf_cookie_is_namespaced(local_client):
    response = local_client.get("/")
    assert response.status_code == 200
    assert local_client.cookies.get(CSRF_COOKIE)
    assert local_client.cookies.get("supplier_offline_csrf") is None
    assert "supplier_csrf=" in response.text


def test_ocr_mode_switch_updates_all_pages(local_client):
    local_client.get("/")
    headers = {"origin": "http://localhost",
               "x-csrf-token": local_client.cookies.get(CSRF_COOKIE)}
    response = local_client.post("/api/ocr-mode", data={"mode": "offline"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["mode"] == "offline"
    for path in ("/", "/approve/synthetic", "/preflight"):
        page = local_client.get(path)
        assert "离线 OCR 版" in page.text


def test_ocr_mode_cannot_switch_during_approval(local_client, monkeypatch):
    from web import app as web_app
    local_client.get("/")
    headers = {"origin": "http://localhost",
               "x-csrf-token": local_client.cookies.get(CSRF_COOKIE)}
    monkeypatch.setattr(web_app, "_task_status", {"1": {"status": "running", "ocr_mode": "online"}})
    response = local_client.post("/api/ocr-mode", data={"mode": "offline"}, headers=headers)
    assert response.status_code == 409


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
                                 headers={"origin": "http://localhost", "x-csrf-token": local_client.cookies.get(CSRF_COOKIE)})
    assert response.status_code == 409
    assert os.environ["SCPMA_COOKIE"] == ""


def test_credentials_can_be_dpapi_saved_and_cleared(local_client, monkeypatch, tmp_path):
    from web import app as web_app
    target = tmp_path / "platform_credentials.bin"
    monkeypatch.setenv("SUPPLIER_CREDENTIAL_FILE", str(target))
    local_client.get("/")
    headers = {"origin": "http://localhost",
               "x-csrf-token": local_client.cookies.get("supplier_csrf")}
    values = {"cookie": "session=encrypted-synthetic",
              "auth_token": "Bearer encrypted-synthetic", "remember": "true"}

    response = local_client.post("/api/cookie", data=values, headers=headers)

    assert response.status_code == 200
    assert response.json()["remembered"] is True
    assert target.is_file()
    assert b"encrypted-synthetic" not in target.read_bytes()
    assert web_app.load_credentials()["SCPMA_COOKIE"] == values["cookie"]
    monkeypatch.setenv("SCPMA_COOKIE", "")
    monkeypatch.setenv("SCPMA_AUTH_TOKEN", "")
    web_app._apply_platform_credentials(web_app.load_credentials())
    assert os.environ["SCPMA_COOKIE"] == values["cookie"]
    assert os.environ["SCPMA_AUTH_TOKEN"] == values["auth_token"]
    assert local_client.get("/api/credentials/status").json() == {"saved": True}

    cleared = local_client.delete("/api/credentials", headers=headers)
    assert cleared.status_code == 200
    assert not target.exists()
