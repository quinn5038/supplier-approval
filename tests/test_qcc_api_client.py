from unittest.mock import Mock

import auto_approve as aa
import qcc_api_client as qcc


def _response(payload, status_code=200):
    response = Mock()
    response.ok = status_code < 400
    response.status_code = status_code
    response.json.return_value = payload
    return response


def _configure(monkeypatch, tmp_path):
    monkeypatch.setattr(qcc, "QCC_APP_KEY", "A" * 32)
    monkeypatch.setattr(qcc, "QCC_SECRET", "S" * 32)
    monkeypatch.setattr(qcc, "QCC_REQUEST_INTERVAL", 0)
    monkeypatch.setattr(qcc, "_CACHE_DB", tmp_path / "qcc.sqlite3")
    monkeypatch.setattr(qcc, "_CALL_COUNT_FILE", tmp_path / "calls.json")


def test_local_env_uses_last_duplicate_but_preserves_explicit_environment(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text(
        "QCC_APP_KEY=\nQCC_APP_KEY=filled-in-file\nQCC_SECRET_KEY=file-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(qcc, "BASE_DIR", tmp_path)
    monkeypatch.delenv("QCC_APP_KEY", raising=False)
    monkeypatch.setenv("QCC_SECRET_KEY", "explicit-secret")

    qcc._load_local_env()

    assert qcc.os.environ["QCC_APP_KEY"] == "filled-in-file"
    assert qcc.os.environ["QCC_SECRET_KEY"] == "explicit-secret"


def test_a10_no_result_is_complete_clear_and_uses_production_paths(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append((url, headers, json, timeout))
        return _response({"status": "200100001", "msg": "查询无结果", "result": None})

    monkeypatch.setattr(qcc.requests, "post", fake_post)
    result = qcc.fetch_a10_checks("测试企业", "913000000000000000")

    assert result["categories"]["dishonest"]["status"] == "clear"
    assert result["categories"]["serious_illegal"]["status"] == "clear"
    assert result["complete_for_a10"] is False
    assert len(calls) == 4
    assert calls[0][0].endswith("/webapi/jr/dishonest/2.0")
    assert calls[1][0].endswith("/webapi/jr/dishonest/2.0")
    assert calls[2][0].endswith("/webapi/mr/illegalinfo/2.0")
    assert calls[3][0].endswith("/webapi/mr/illegalinfo/2.0")
    assert [call[2]["keyword"] for call in calls] == [
        "913000000000000000", "测试企业", "913000000000000000", "测试企业"]
    assert all(call[1]["User-Agent"].startswith("Mozilla/5.0") for call in calls)
    assert all(call[1]["Accept"] == "application/json,text/plain,*/*" for call in calls)


def test_a10_paginates_and_classifies_current_vs_resolved(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)

    def fake_post(url, headers, json, timeout):
        del headers, timeout
        if "dishonest" in url:
            if json["pageNum"] == 1:
                items = [{"performance": "全部已履行"} for _ in range(20)]
            else:
                items = [{"performance": "部分未履行", "unperformPart": "尚未履行"}]
            return _response({"status": 200, "result": {"total": 21, "items": items}})
        return _response({"status": "200", "result": {"total": 2, "items": [
            {"putDate": 1700000000000, "removeDate": None},
            {"putDate": 1600000000000, "removeDate": 1700000000000},
        ]}})

    monkeypatch.setattr(qcc.requests, "post", fake_post)
    result = qcc.fetch_a10_checks("测试企业")

    dishonest = result["categories"]["dishonest"]
    illegal = result["categories"]["serious_illegal"]
    assert dishonest == {"status": "risk", "complete": True, "total": 21,
                          "risk_count": 1, "resolved_count": 20, "unknown_count": 0}
    assert illegal == {"status": "risk", "complete": True, "total": 2,
                       "risk_count": 1, "removed_count": 1, "unknown_count": 0}


def test_a10_business_error_is_unavailable_and_not_cached(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    calls = 0

    def fake_post(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _response({"status": "10010001", "msg": "鉴权失败", "result": None})

    monkeypatch.setattr(qcc.requests, "post", fake_post)
    first = qcc.fetch_a10_checks("测试企业")
    second = qcc.fetch_a10_checks("测试企业")

    assert first["categories"]["dishonest"]["status"] == "unavailable"
    assert first["categories"]["serious_illegal"]["status"] == "unavailable"
    assert second["categories"]["dishonest"]["status"] == "unavailable"
    assert calls == 4


def test_a10_two_clear_categories_still_leave_overall_manual():
    checklist = [{"id": "A10", "name": "商业信誉", "status": "pending", "detail": ""}]
    summary = {
        "a10_checks": {
            "categories": {
                "dishonest": {"status": "clear", "total": 0, "resolved_count": 0},
                "serious_illegal": {"status": "clear", "total": 1, "removed_count": 1},
            },
            "pending_categories": ["被执行人", "限制消费令", "经营异常"],
            "complete_for_a10": False,
        }
    }

    result, issues = aa.enhance_checklist_with_qcc(checklist, {}, summary)

    assert result[0]["status"] == "manual"
    assert "失信被执行人：未查到记录" in result[0]["detail"]
    assert "严重违法：1条历史记录均已移出" in result[0]["detail"]
    assert "被执行人、限制消费令、经营异常尚未接入" in result[0]["detail"]
    assert issues == ["A10 商业信誉：被执行人、限制消费令、经营异常尚未接入"]


def test_a10_current_risks_are_shown_but_not_auto_rejected():
    checklist = [{"id": "A10", "name": "商业信誉", "status": "pending", "detail": ""}]
    summary = {
        "a10_checks": {
            "categories": {
                "dishonest": {"status": "risk", "risk_count": 2},
                "serious_illegal": {"status": "risk", "risk_count": 1},
            },
            "pending_categories": [],
            "complete_for_a10": False,
        }
    }

    result, issues = aa.enhance_checklist_with_qcc(checklist, {}, summary)

    assert result[0]["status"] == "manual"
    assert "发现2条当前未履行风险" in result[0]["detail"]
    assert "发现1条尚未移出记录" in result[0]["detail"]
    assert len(issues) == 1
    assert "当前未履行风险" in issues[0]
    assert "尚未移出记录" in issues[0]
