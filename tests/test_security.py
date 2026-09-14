import json
import sqlite3
import time
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import auto_approve as aa
import desensitize as ds
import textin_pipeline as tp
from approval_guard import ApprovalIdentity, verify_fresh_task
from material_policy import aggregate_materials, file_types, is_public_material
from state_store import claim_operation, record_event
from web import security
from web.app import app


@pytest.mark.parametrize("types,name", [
    (["financial_report"], "report.pdf"),
    (["legal_person_id"], "person.pdf"),
    (["tax_credit", "financial_report"], "tax.pdf"),
    (["tax_credit"], "财务报表及纳税证明.pdf"),
    (["iso9001"], "身份证与9001.pdf"),
    ([], "unknown.pdf"), (["authorization"], "授权书.pdf"),
])
def test_sensitive_policy(types, name):
    assert not is_public_material(name, types)


@pytest.mark.parametrize("field", ["typeName", "desc"])
def test_sensitive_metadata_overrides_public_type(field):
    cache = {"1": {"materials_detail": [{"fileName": "public.pdf", "types": ["tax_credit"],
                                          field: "含财务报表"}]}}
    assert not is_public_material("1/public.pdf", file_types("1/public.pdf", cache))


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(tp, "BASE", tmp_path)
    monkeypatch.setattr(tp, "FILES_DIR", tmp_path / "files_cache")
    monkeypatch.setattr(tp, "FILES_DESENS_DIR", tmp_path / "files_cache_desens")
    monkeypatch.setattr(tp, "RESULTS_FILE", tmp_path / "results.json")
    monkeypatch.setattr(ds, "__file__", str(tmp_path / "desensitize.py"))
    monkeypatch.setattr(ds, "desensitize_id_cards_with_paddle", lambda *args: {})
    monkeypatch.setattr(tp.time, "sleep", lambda *args: None)
    def setup(files):
        raw = tp.FILES_DIR / "1"
        raw.mkdir(parents=True, exist_ok=True)
        details = []
        for name, types in files:
            (raw / name).write_bytes(b"synthetic fixture")
            details.append({"fileName": name, "types": types})
        (tmp_path / "cache_v4.json").write_text(json.dumps({"1": {"materials_detail": details}}), encoding="utf-8")
    return setup


def test_pipeline_never_sends_sensitive(pipeline, monkeypatch):
    pipeline([("财报.pdf", ["financial_report"]), ("id.pdf", ["legal_person_id"]),
              ("财务与纳税.pdf", ["tax_credit"]), ("unknown.pdf", ["unknown"])])
    send = Mock(side_effect=AssertionError("must not send"))
    monkeypatch.setattr(tp, "parse_file_textin", send)
    tp.run_parse("1")
    send.assert_not_called()
    result = json.loads(tp.RESULTS_FILE.read_text(encoding="utf-8"))["1"]
    assert all(r.get("issues") for r in result.values())
    assert not list(tp.FILES_DESENS_DIR.rglob("*.pdf"))


def test_transport_gate_precedes_request(pipeline, monkeypatch):
    pipeline([("a.pdf", ["financial_report"])])
    staged = tp.FILES_DESENS_DIR / "1" / "a.pdf"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"secret")
    send = Mock()
    monkeypatch.setattr(tp.requests, "post", send)
    with pytest.raises(PermissionError):
        tp.parse_file_textin(staged, "financial_report")
    with pytest.raises(PermissionError):
        tp.parse_file_textin(tp.FILES_DIR / "1/a.pdf", "business_license")
    send.assert_not_called()


def test_desens_failure_stops_network(pipeline, monkeypatch):
    pipeline([("license.pdf", ["business_license"])])
    monkeypatch.setattr(ds, "desensitize_dir", Mock(side_effect=OSError("failure")))
    send = Mock()
    monkeypatch.setattr(tp, "parse_file_textin", send)
    with pytest.raises(RuntimeError, match="脱敏失败"):
        tp.run_parse("1")
    send.assert_not_called()


def test_multiple_materials_and_hash_refresh(pipeline, monkeypatch):
    pipeline([("one.pdf", ["iso9001"]), ("two.pdf", ["iso9001"])])
    send = Mock(side_effect=[("first", []), ("second", []), ("changed", [])])
    monkeypatch.setattr(tp, "parse_file_textin", send)
    monkeypatch.setattr(tp, "extract", lambda kind, text, *args: {"checks": {"valid": text != "first"}, "fields": {}, "issues": []})
    tp.run_parse("1")
    result = json.loads(tp.RESULTS_FILE.read_text(encoding="utf-8"))["1"]["iso9001"]
    assert len(result["documents"]) == 2
    assert False in result["checks"].values()
    tp.run_parse("1")
    assert send.call_count == 2
    (tp.FILES_DIR / "1/one.pdf").write_bytes(b"modified file")
    tp.run_parse("1")
    assert send.call_count == 3


def test_offline_extract_never_calls_api(pipeline, monkeypatch):
    pipeline([("one.pdf", ["iso9001"])])
    send = Mock()
    monkeypatch.setattr(tp, "parse_file_textin", send)
    tp.run_parse("1", offline=True)
    send.assert_not_called()


def test_removed_material_clears_result(pipeline, monkeypatch):
    pipeline([])
    tp.RESULTS_FILE.write_text('{"1":{"iso9001":{"checks":{"ok":true}}}}')
    tp.run_parse("1")
    assert json.loads(tp.RESULTS_FILE.read_text())["1"] == {}


def test_audit_and_duplicate_claim(tmp_path):
    db_path = tmp_path / "audit.sqlite3"
    record_event(db_path, "test", {"sha256": "123"})
    record_event(db_path, "test", {"sha256": "456"})
    with sqlite3.connect(db_path) as db:
        rows = db.execute("SELECT previous,hash FROM events ORDER BY seq").fetchall()
        assert rows[1][0] == rows[0][1]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM events")
    claim_operation(db_path, "task")
    with pytest.raises(ValueError):
        claim_operation(db_path, "task")


@pytest.mark.parametrize("bill_type", ["P0701", "P0702", "P0704"])
def test_approval_payload(bill_type, monkeypatch):
    monkeypatch.setattr(aa, "DRY_RUN", True)
    result = aa.execute_approval("p", "a", "step", "t", "b", "review", 3,
                                 business_bill_type=bill_type, todo_id="1")
    assert result["payload"]["businessBillType"] == bill_type
    assert result["payload"]["businessBillId"] == "b"


@pytest.mark.parametrize("change", [{"businessBillId": "other"}, {"taskId": "other"},
                                    {"businessBillType": "P0701"}, {"workItemStatus": 1}, {"id": "other"}])
def test_changed_task_blocked(change):
    identity = ApprovalIdentity(todo_id="1", bill_id="b", task_id="t", proc_inst_id="p", act_inst_id="a", business_bill_type="P0702", oper_code=3)
    data = dict(id="1", businessBillId="b", taskId="t", procInstId="p", actInstId="a", businessBillType="P0702", workItemStatus=0)
    data.update(change)
    with pytest.raises(ValueError):
        verify_fresh_task(identity, data)


def test_live_write_requires_confirmation(monkeypatch):
    monkeypatch.setattr(aa, "DRY_RUN", False)
    monkeypatch.setenv("ENABLE_LIVE_APPROVAL", "true")
    send = Mock()
    monkeypatch.setattr(aa, "_post", send)
    with pytest.raises(PermissionError):
        aa.execute_approval("p", "a", "step", "t", "b", "opinion", 3, business_bill_type="P0702", todo_id="1")
    send.assert_not_called()


def test_confirmed_write_checks_buttons_and_is_idempotent(tmp_path, monkeypatch):
    args = ("p", "a", "step", "t", "b", "synthetic opinion", 3)
    kwargs = {"business_bill_type": "P0702", "todo_id": "1"}
    monkeypatch.setattr(aa, "DRY_RUN", True)
    preview = aa.execute_approval(*args, **kwargs)
    monkeypatch.setattr(aa, "DRY_RUN", False)
    monkeypatch.setattr(aa, "BASE_DIR", tmp_path)
    monkeypatch.setenv("ENABLE_LIVE_APPROVAL", "true")
    fresh = dict(id="1", businessBillId="b", taskId="t", procInstId="p", actInstId="a", businessBillType="P0702", workItemStatus=0,
                 workItemId="w", currentOperUserId="user", currentOperUserDeptId="dept")
    monkeypatch.setattr(aa, "query_todo_detail", lambda *a: {"data": fresh})
    buttons = Mock(return_value={"data": []})
    monkeypatch.setattr(aa, "get_approval_buttons", buttons)
    send = Mock(return_value={"code": 200})
    monkeypatch.setattr(aa, "_post", send)
    kwargs["confirmed_digest"] = preview["confirmation_digest"]
    with pytest.raises(ValueError, match="明确允许"):
        aa.execute_approval(*args, **kwargs)
    send.assert_not_called()
    buttons.return_value = {"data": [{"operCode": 3}]}
    aa.execute_approval(*args, **kwargs)
    assert send.call_args.kwargs["retry_waits"] == []
    with pytest.raises(ValueError, match="已尝试"):
        aa.execute_approval(*args, **kwargs)
    assert send.call_count == 1


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_MODE", "password")
    monkeypatch.setenv("WEB_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("WEB_VIEWER_PASSWORD", "view-password")
    security._sessions.clear()
    security._attempts.clear()
    with TestClient(app, base_url="http://localhost") as client:
        yield client


def login(client, user="admin", password="test-password"):
    return client.post("/login", data={"username": user, "password": password}, headers={"origin": "http://localhost"}, follow_redirects=False)


@pytest.mark.parametrize("path", ["/api/todos", "/api/status/1", "/api/report/1", "/api/desens_image/1", "/openapi.json"])
def test_anonymous_cannot_read(client, path):
    assert client.get(path, follow_redirects=False).status_code in (401, 303)


def test_login_csrf_roles_timeout(client):
    assert login(client).status_code == 303
    assert client.get("/").status_code == 200
    assert client.post("/api/approve/1").status_code == 403
    assert client.post("/api/cookie", data={"cookie": "secret"}, headers={"origin": "http://localhost", "x-csrf-token": client.cookies.get("supplier_csrf")}).status_code == 400
    security._sessions[client.cookies.get("supplier_session")]["expires"] = time.monotonic() - 1
    assert client.get("/api/status/1").status_code == 401
    assert login(client, "viewer", "view-password").status_code == 303
    assert client.post("/api/approve/1", headers={"origin": "http://localhost", "x-csrf-token": client.cookies.get("supplier_csrf")}).status_code == 403


def test_bruteforce_and_host(client):
    for _ in range(5):
        assert login(client, password="bad").status_code == 401
    assert login(client).status_code == 429
    assert client.get("/health", headers={"host": "attacker.example"}).status_code == 400


def test_aggregate_error_never_passes():
    result = aggregate_materials([{"file": "a", "error": "failed"}, {"file": "b", "checks": {"valid": True}}])
    assert result["issues"]
    assert None in result["checks"].values()
