import asyncio
import json
import os
import sqlite3
from datetime import datetime

from fastapi.testclient import TestClient

import storage_cleanup
from state_store import get_state, put_state
from storage_cleanup import clean_supplier_cache, list_expired_supplier_caches
from web import app as webapp
from web.security import CSRF_COOKIE

DAY = 86400


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _old_cache(base, todo_id="old-1", name="旧供应商", now=2_000_000_000):
    fetched = datetime.fromtimestamp(now - 20 * DAY).isoformat(timespec="seconds")
    _write_json(base / "cache_v4.json", {
        todo_id: {"supplier": {"name": name}, "todo": {}, "fetched_at": fetched},
    })
    folder = base / "files_cache" / todo_id
    folder.mkdir(parents=True)
    material = folder / "material.pdf"
    material.write_bytes(b"fixture")
    os.utime(material, (now - 20 * DAY, now - 20 * DAY))
    os.utime(folder, (now - 20 * DAY, now - 20 * DAY))
    return now


def test_expired_list_uses_latest_supplier_activity(tmp_path):
    now = _old_cache(tmp_path)
    desens = tmp_path / "files_cache_desens" / "old-1"
    desens.mkdir(parents=True)
    masked = desens / "masked.png"
    masked.write_bytes(b"masked")
    os.utime(masked, (now - 15 * DAY, now - 15 * DAY))

    rows = list_expired_supplier_caches(tmp_path, now=now)
    assert [(row["todoId"], row["name"], row["ageDays"]) for row in rows] == [
        ("old-1", "旧供应商", 15),
    ]
    assert rows[0]["fileCount"] == 2
    assert rows[0]["sizeBytes"] == len(b"fixturemasked")

    os.utime(masked, (now - 2 * DAY, now - 2 * DAY))
    assert list_expired_supplier_caches(tmp_path, now=now) == []


def test_expired_list_reads_supplier_name_from_mode_specific_stage2(tmp_path):
    now = _old_cache(tmp_path, name="")
    _write_json(tmp_path / "stage2_results.offline.json", {
        "old-1": {"name": "离线结果供应商"},
    })

    rows = list_expired_supplier_caches(tmp_path, now=now)

    assert rows[0]["name"] == "离线结果供应商"


def test_clean_supplier_removes_only_related_cache(tmp_path):
    now = _old_cache(tmp_path)
    desens = tmp_path / "files_cache_desens" / "old-1"
    desens.mkdir(parents=True)
    (desens / "material.pdf.md").write_text("cached", encoding="utf-8")
    os.utime(desens / "material.pdf.md", (now - 20 * DAY, now - 20 * DAY))
    os.utime(desens, (now - 20 * DAY, now - 20 * DAY))

    for filename in (
        "textin_results.json",
        "textin_results.online.json",
        "textin_results.offline.json",
        "stage2_results.json",
        "stage2_results.online.json",
        "stage2_results.offline.json",
        "qcc_results.json",
    ):
        _write_json(tmp_path / filename, {"old-1": {"result": True}, "keep-2": {"result": True}})
    with (tmp_path / "cache_v4.json").open("r", encoding="utf-8") as stream:
        cache = json.load(stream)
    cache["keep-2"] = {"supplier": {"name": "保留供应商"},
                       "fetched_at": datetime.fromtimestamp(now).isoformat()}
    _write_json(tmp_path / "cache_v4.json", cache)
    (tmp_path / "progress_v4.jsonl").write_text(
        '{"todoId":"old-1"}\n{"todoId":"keep-2"}\nnot-json\n', encoding="utf-8")
    (tmp_path / "audit.sqlite3").write_bytes(b"audit")
    with sqlite3.connect(tmp_path / "qcc_api_cache.sqlite3") as db:
        db.execute("CREATE TABLE state(namespace TEXT, key TEXT, value TEXT, PRIMARY KEY(namespace,key))")
        db.execute("INSERT INTO state VALUES('qcc', '旧供应商/base', '{}')")
        db.execute("INSERT INTO state VALUES('qcc', '保留供应商/base', '{}')")

    result = clean_supplier_cache(tmp_path, "old-1", now=now)

    assert result["name"] == "旧供应商"
    assert not (tmp_path / "files_cache" / "old-1").exists()
    assert not (tmp_path / "files_cache_desens" / "old-1").exists()
    for filename in (
        "cache_v4.json",
        "textin_results.json",
        "textin_results.online.json",
        "textin_results.offline.json",
        "stage2_results.json",
        "stage2_results.online.json",
        "stage2_results.offline.json",
        "qcc_results.json",
    ):
        assert "old-1" not in json.loads((tmp_path / filename).read_text(encoding="utf-8"))
        assert "keep-2" in json.loads((tmp_path / filename).read_text(encoding="utf-8"))
    progress = (tmp_path / "progress_v4.jsonl").read_text(encoding="utf-8")
    assert "old-1" not in progress
    assert "keep-2" in progress and "not-json" in progress
    assert (tmp_path / "audit.sqlite3").read_bytes() == b"audit"
    with sqlite3.connect(tmp_path / "qcc_api_cache.sqlite3") as db:
        keys = [row[0] for row in db.execute("SELECT key FROM state ORDER BY key")]
    assert keys == ["保留供应商/base"]


def test_web_cleanup_rejects_while_pipeline_is_running(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "BASE_DIR", tmp_path)
    monkeypatch.setattr(webapp, "_task_status", {"another": {"status": "running"}})
    response = asyncio.run(webapp.api_clean_supplier_cache("old-1"))
    assert response.status_code == 409


def test_web_selected_cleanup_reports_partial_failure_and_refreshes_inventory(tmp_path, monkeypatch):
    for todo_id in ("remove-1", "remove-2", "locked", "keep"):
        folder = tmp_path / "files_cache" / todo_id
        folder.mkdir(parents=True)
        (folder / "material.pdf").write_bytes(b"fixture")
    reports = {tid: {"name": tid} for tid in ("remove-1", "remove-2", "locked", "keep", "report-only")}
    for mode in ("online", "offline"):
        _write_json(tmp_path / f"stage2_results.{mode}.json", reports)
    monkeypatch.setattr(webapp, "BASE_DIR", tmp_path)
    monkeypatch.setattr(webapp, "STATE_DB", tmp_path / "web_state.sqlite3")
    monkeypatch.setattr(webapp, "_task_status", {"remove-1": {"status": "done"}})
    monkeypatch.setenv("WEB_AUTH_MODE", "local")
    rmtree = storage_cleanup.shutil.rmtree

    def remove_tree(path):
        if path.name == "locked":
            raise PermissionError("fixture in use")
        rmtree(path)

    with TestClient(webapp.app, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        assert client.get("/api/cache-cleanup").json()["items"] == []
        headers = {"Origin": "http://localhost", "X-CSRF-Token": client.cookies[CSRF_COOKIE]}
        payload = {"mode": "selected", "todoIds": ["remove-1", "locked", "remove-2", "remove-1"]}
        assert client.post("/api/cache-cleanup", json=payload).status_code == 403
        unsafe = {"mode": "selected", "todoIds": ["remove-1", "../keep"]}
        assert client.post("/api/cache-cleanup", json=unsafe, headers=headers).status_code == 400
        assert (tmp_path / "files_cache" / "remove-1").exists()

        with monkeypatch.context() as scoped:
            scoped.setattr(storage_cleanup.shutil, "rmtree", remove_tree)
            response = client.post("/api/cache-cleanup", json=payload, headers=headers)
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is False
        assert [row["todoId"] for row in result["deleted"]] == ["remove-1", "remove-2"]
        assert [row["todoId"] for row in result["failed"]] == ["locked"]
        assert result["freedBytes"] == 2 * len(b"fixture")
        refreshed = client.get("/api/cache-cleanup?include_recent=true").json()["items"]
        assert {row["todoId"] for row in refreshed} == {"locked", "keep", "report-only"}
        assert "remove-1" not in webapp._task_status

        webapp._task_status["busy"] = {"status": "running"}
        assert client.post("/api/cache-cleanup", json={"mode": "all"}, headers=headers).status_code == 409
        assert (tmp_path / "files_cache" / "keep").exists()

        webapp._task_status.clear()
        qcc_db = tmp_path / "qcc_api_cache.sqlite3"
        put_state(qcc_db, "qcc", "orphan/path", {})
        put_state(qcc_db, "other", "keep", {})
        _write_json(tmp_path / "qcc_results.json", {"external-company-key": {}})
        _write_json(tmp_path / "qcc_call_count.json", {"total": 1})
        response = client.post("/api/cache-cleanup", json={"mode": "all"}, headers=headers)
        assert response.status_code == 200
        assert {row["todoId"] for row in response.json()["deleted"]} == {"locked", "keep", "report-only"}
        assert client.get("/api/cache-cleanup?include_recent=true").json()["items"] == []
        assert get_state(qcc_db, "qcc") == {}
        assert get_state(qcc_db, "other") == {"keep": {}}
        assert json.loads((tmp_path / "qcc_results.json").read_text(encoding="utf-8")) == {}
        assert (tmp_path / "qcc_call_count.json").exists()
        assert (tmp_path / "audit.sqlite3").exists()


def test_homepage_has_cache_cleanup_states():
    html = (webapp.WEB_DIR / "templates" / "index.html").read_text(encoding="utf-8")
    css = (webapp.WEB_DIR / "static" / "style.css").read_text(encoding="utf-8")
    assert "清理供应商缓存" in html
    assert "一键清理全部" in html
    assert "删除所选" in html
    assert "cache-select-all" in html
    assert "此操作不可撤销" in html
    assert "await loadSupplierCaches()" in html
    assert "WebUI 后端尚未加载缓存清理功能" in html
    assert "nameById[item.todoId] || item.name" in html
    assert ".cache-row .btn { width: 86px; white-space: nowrap; }" in css
