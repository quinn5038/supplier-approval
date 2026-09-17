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


def test_rerun_download_expiry_then_retry_success(tmp_path, monkeypatch):
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
    assert runner.call_count == 1  # no OCR or stage2 after failed download
    download.side_effect = None
    (tmp_path / "stage2_results.json").write_text(json.dumps({"45813273": {"decision": "manual"}}))
    webapp._run_pipeline_for_one("45813273")
    assert webapp._task_status["45813273"]["status"] == "done"
    assert download.call_count == 2


def test_homepage_filter_keeps_background_state():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for real homepage JavaScript regression")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / "tests/frontend_status.cjs")], cwd=root,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
