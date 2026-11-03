import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import auto_approve as aa
import textin_pipeline as tp
from web import app as webapp


def test_running_task_is_not_resubmitted_and_error_is_exposed(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setattr(webapp, "STATE_DB", tmp_path / "state.sqlite3")
    monkeypatch.setattr(webapp, "_task_status", {})
    executor = Mock()
    monkeypatch.setattr(webapp, "_pipeline_executor", executor)
    asyncio.run(webapp.api_start_approve("1"))
    state = dict(webapp._task_status["1"])
    for _ in range(2):
        assert asyncio.run(webapp.api_status("1")) == state
        assert asyncio.run(webapp.api_status_all())["1"]["status"] == "running"
    assert asyncio.run(webapp.api_start_approve("1")).status_code == 409
    assert executor.submit.call_count == 1
    assert webapp._task_status["1"] == state
    webapp._task_status["1"].update(status="error", error="登录过期")
    assert asyncio.run(webapp.api_status_all())["1"]["error"] == "登录过期"
    asyncio.run(webapp.api_start_approve("1"))
    assert executor.submit.call_count == 2


def test_download_session_expiry_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(tp, "BASE", tmp_path)
    (tmp_path / "cache_v4.json").write_text(json.dumps({"1": {"supplier": {"supinfo_id": "id"}}}))
    monkeypatch.setattr(aa, "query_qualification_files", Mock(side_effect=aa.SessionExpiredError("expired")))
    with pytest.raises(aa.SessionExpiredError):
        tp.download_supplier_files("1")


def _download_fixture(tmp_path, monkeypatch, response):
    monkeypatch.setattr(tp, "BASE", tmp_path)
    monkeypatch.setattr(tp, "FILES_DIR", tmp_path / "files")
    (tmp_path / "cache_v4.json").write_text(json.dumps({
        "1": {"supplier": {"supinfo_id": "synthetic", "bill_type": "P0702"}},
    }), encoding="utf-8")
    monkeypatch.setattr(aa, "query_qualification_files", Mock(return_value={
        "code": 0, "data": {"supFilesBOList": [{
            "fileUrl": "synthetic-encrypted-value", "fileName": "营业执照.jpg",
            "fileSize": 100, "uploadId": "synthetic-upload",
        }]},
    }))
    monkeypatch.setattr(tp.requests, "get", Mock(return_value=response))
    sleep = Mock()
    monkeypatch.setattr(tp.time, "sleep", sleep)
    return sleep


def test_download_http_200_code_111_fails_fast_as_session_expired(tmp_path, monkeypatch):
    response = Mock(status_code=200, content=b'{"code":"111"}',
                    headers={"Content-Type": "text/plain;charset=UTF-8"})
    response.json.return_value = {"code": "111", "message": "synthetic expired"}
    sleep = _download_fixture(tmp_path, monkeypatch, response)

    with pytest.raises(aa.SessionExpiredError, match="Cookie 和 Authorization"):
        tp.download_supplier_files("1", delay=0)

    tp.requests.get.assert_called_once()
    sleep.assert_not_called()
    response.close.assert_called_once()


def test_download_transient_http_error_uses_short_retry(tmp_path, monkeypatch):
    unavailable = Mock(status_code=503, content=b"temporary",
                       headers={"Content-Type": "text/plain"})
    unavailable.json.side_effect = ValueError
    success = Mock(status_code=200, content=b"x" * 100,
                   headers={"Content-Type": "image/jpeg"})
    _download_fixture(tmp_path, monkeypatch, unavailable)
    tp.requests.get.side_effect = [unavailable, success]

    tp.download_supplier_files("1", delay=0)

    assert tp.requests.get.call_count == 2
    assert tp.time.sleep.call_args_list[0].args == (5,)
    assert (tmp_path / "files/1/synthetic-upload_营业执照.jpg").read_bytes() == b"x" * 100


def test_report_loader_keeps_online_and_offline_results_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "BASE_DIR", tmp_path)
    (tmp_path / "cache_v4.json").write_text("{}", encoding="utf-8")
    (tmp_path / "stage2_results.online.json").write_text(
        json.dumps({"1": {"decision": "recommend"}}), encoding="utf-8")
    (tmp_path / "stage2_results.offline.json").write_text(
        json.dumps({"1": {"decision": "manual"}}), encoding="utf-8")
    (tmp_path / "textin_results.online.json").write_text('{"1": {}}', encoding="utf-8")
    (tmp_path / "textin_results.offline.json").write_text('{"1": {}}', encoding="utf-8")

    monkeypatch.setenv("OCR_MODE", "online")
    assert webapp._load_stage2_for_todo("1")[0]["decision"] == "recommend"
    monkeypatch.setenv("OCR_MODE", "offline")
    assert webapp._load_stage2_for_todo("1")[0]["decision"] == "manual"


def test_report_loader_rejects_stage2_entry_without_same_mode_ocr(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "BASE_DIR", tmp_path)
    monkeypatch.setattr(webapp, "_task_status", {})
    (tmp_path / "cache_v4.json").write_text("{}", encoding="utf-8")
    (tmp_path / "stage2_results.online.json").write_text(
        json.dumps({"1": {"decision": "recommend"}, "2": {"decision": "manual"}}),
        encoding="utf-8")
    (tmp_path / "textin_results.online.json").write_text(
        json.dumps({"1": {}}), encoding="utf-8")

    monkeypatch.setenv("OCR_MODE", "online")
    assert webapp._has_valid_result("1") is True
    assert webapp._has_valid_result("2") is False
    assert webapp._load_stage2_for_todo("2")[0] == {}


def test_legacy_task_state_belongs_to_online_only(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "BASE_DIR", tmp_path)
    monkeypatch.setattr(webapp, "_task_status", {"1": {"status": "done", "step": "完成"}})

    monkeypatch.setenv("OCR_MODE", "online")
    assert "1" in asyncio.run(webapp.api_status_all())
    monkeypatch.setenv("OCR_MODE", "offline")
    assert "1" not in asyncio.run(webapp.api_status_all())
    assert asyncio.run(webapp.api_status("1"))["status"] == "idle"


def test_rerun_download_expiry_then_retry_success(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_MODE", "online")
    monkeypatch.setattr(webapp, "BASE_DIR", tmp_path)
    monkeypatch.setattr(webapp, "STATE_DB", tmp_path / "state.sqlite3")
    monkeypatch.setattr(webapp, "_task_status", {})
    runner = Mock(return_value=Mock(returncode=0, stderr=""))
    monkeypatch.setattr(webapp.subprocess, "run", runner)
    download = Mock(side_effect=aa.SessionExpiredError("expired"))
    monkeypatch.setattr(tp, "download_supplier_files", download)
    webapp._run_pipeline_for_one("45813273")
    assert webapp._task_status["45813273"]["step"] == "cookie_expired"
    assert webapp._task_status["45813273"]["status"] == "error"
    assert runner.call_count == 2  # runtime probe + fetch; no OCR or stage2 after failed download
    download.side_effect = None
    (tmp_path / "stage2_results.json").write_text(json.dumps({"45813273": {"decision": "manual"}}))
    webapp._run_pipeline_for_one("45813273")
    assert webapp._task_status["45813273"]["status"] == "done"
    assert download.call_count == 2


def test_broken_worker_python_reports_repair_instruction(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "BASE_DIR", tmp_path)
    monkeypatch.setattr(webapp, "STATE_DB", tmp_path / "state.sqlite3")
    monkeypatch.setattr(webapp, "_task_status", {})
    broken = Mock(returncode=1, stderr="No module named 'encodings'")
    runner = Mock(return_value=broken)
    monkeypatch.setattr(webapp.subprocess, "run", runner)

    webapp._run_pipeline_for_one("123")

    state = webapp._task_status["123"]
    assert state["step"] == "python_runtime_invalid"
    assert state["status"] == "error"
    assert "start_webui.bat" in state["error"]
    assert runner.call_count == 1
    child_env = runner.call_args.kwargs["env"]
    assert "PYTHONHOME" not in child_env
    assert "PYTHONPATH" not in child_env


def test_homepage_filter_keeps_background_state():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for real homepage JavaScript regression")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / "tests/frontend_status.cjs")], cwd=root,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
