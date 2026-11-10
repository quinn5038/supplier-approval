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
    assert result["categories"]["operation_abnormal"]["status"] == "clear"
    assert result["categories"]["bad_conduct"]["status"] == "clear"
    assert result["complete_for_a10"] is False
    assert len(calls) == 8
    assert sum(call[0].endswith("/webapi/jr/dishonest/2.0") for call in calls) == 2
    assert sum(call[0].endswith("/webapi/mr/illegalinfo/2.0") for call in calls) == 2
    assert sum(call[0].endswith("/webapi/mr/baseinfo/normal") for call in calls) == 2
    assert sum(call[0].endswith("/webapi/bq/badConduct") for call in calls) == 2
    assert [call[2]["keyword"] for call in calls] == [
        value for _ in range(4) for value in ("913000000000000000", "测试企业")]
    assert all(call[1]["User-Agent"].startswith("Mozilla/5.0") for call in calls)
    assert all(call[1]["Accept"] == "application/json,text/plain,*/*" for call in calls)


def test_a10_success_with_null_result_is_complete_clear(monkeypatch, tmp_path):
    """经营异常等接口会用 status=200/result=null 表示成功且无记录。"""
    _configure(monkeypatch, tmp_path)

    monkeypatch.setattr(
        qcc.requests,
        "post",
        lambda *args, **kwargs: _response(
            {"status": "200", "msg": "success", "result": None}),
    )

    result = qcc.fetch_a10_checks("测试企业", "913000000000000000")

    assert all(
        category["status"] == "clear"
        for category in result["categories"].values()
    )
    assert all(
        category["complete"] is True
        for category in result["categories"].values()
    )


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
        if "badConduct" in url:
            return _response({"status": "200", "result": [
                {"publishValidityPeriod": "2999-01-01"},
                {"publishValidityPeriod": "2000-01-01"},
            ], "total": 2})
        return _response({"status": "200", "result": {"total": 2, "items": [
            {"putDate": 1700000000000, "removeDate": None},
            {"putDate": 1600000000000, "removeDate": 1700000000000},
        ]}})

    monkeypatch.setattr(qcc.requests, "post", fake_post)
    result = qcc.fetch_a10_checks("测试企业")

    dishonest = result["categories"]["dishonest"]
    illegal = result["categories"]["serious_illegal"]
    abnormal = result["categories"]["operation_abnormal"]
    bad_conduct = result["categories"]["bad_conduct"]
    assert dishonest == {"status": "risk", "complete": True, "total": 21,
                          "risk_count": 1, "resolved_count": 20, "unknown_count": 0}
    assert illegal == {"status": "risk", "complete": True, "total": 2,
                       "risk_count": 1, "removed_count": 1, "unknown_count": 0}
    assert abnormal == illegal
    assert bad_conduct == {"status": "risk", "complete": True, "total": 2,
                           "risk_count": 1, "historical_count": 1, "unknown_count": 0}


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
    assert calls == 8


def test_a10_four_clear_categories_still_leave_overall_manual():
    checklist = [{"id": "A10", "name": "商业信誉", "status": "pending", "detail": ""}]
    summary = {
        "a10_checks": {
            "categories": {
                "dishonest": {"status": "clear", "total": 0, "resolved_count": 0},
                "serious_illegal": {"status": "clear", "total": 1, "removed_count": 1},
                "operation_abnormal": {"status": "clear", "total": 0, "removed_count": 0},
                "bad_conduct": {"status": "clear", "total": 1, "historical_count": 1},
            },
            "pending_categories": ["被执行人", "限制消费令"],
            "complete_for_a10": False,
        }
    }

    result, issues = aa.enhance_checklist_with_qcc(checklist, {}, summary)

    assert result[0]["status"] == "manual"
    assert "失信被执行人：未查到记录" in result[0]["detail"]
    assert "严重违法：1条历史记录均已移出" in result[0]["detail"]
    assert "经营异常：未查到记录" in result[0]["detail"]
    assert "建筑资质不良行为：1条历史记录均已过有效期" in result[0]["detail"]
    assert "尚未接入" not in result[0]["detail"]
    assert issues == []


def test_a10_current_risks_are_shown_but_not_auto_rejected():
    checklist = [{"id": "A10", "name": "商业信誉", "status": "pending", "detail": ""}]
    summary = {
        "a10_checks": {
            "categories": {
                "dishonest": {"status": "risk", "risk_count": 2},
                "serious_illegal": {"status": "risk", "risk_count": 1},
                "operation_abnormal": {"status": "risk", "risk_count": 3},
                "bad_conduct": {"status": "risk", "risk_count": 4},
            },
            "pending_categories": [],
            "complete_for_a10": False,
        }
    }

    result, issues = aa.enhance_checklist_with_qcc(checklist, {}, summary)

    assert result[0]["status"] == "manual"
    assert "发现2条当前未履行风险" in result[0]["detail"]
    assert "发现1条尚未移出记录" in result[0]["detail"]
    assert "经营异常：发现3条尚未移出记录" in result[0]["detail"]
    assert "建筑资质不良行为：发现4条有效期内记录" in result[0]["detail"]
    assert len(issues) == 1
    assert "当前未履行风险" in issues[0]
    assert "尚未移出记录" in issues[0]


def test_tax_credit_parses_top_level_list_and_enhances_a03(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)

    def fake_post(url, headers, json, timeout):
        del url, headers, json, timeout
        return _response({"status": 200, "result": [
            {"year": str(qcc.date.today().year - 2), "grade": "B"},
            {"year": str(qcc.date.today().year - 1), "grade": "A"},
        ], "total": 2, "pageNum": 1, "pageSize": 20})

    monkeypatch.setattr(qcc.requests, "post", fake_post)
    tax = qcc.fetch_tax_credit("测试企业")
    assert tax["status"] == "clear"
    assert tax["latest_grade"] == "A"

    checklist = [{"id": "A03", "name": "纳税信用等级", "status": "pending", "detail": ""}]
    result, issues = aa.enhance_checklist_with_qcc(checklist, {}, {"tax_credit": tax})
    assert result[0]["status"] == "pass"
    assert "纳税信用A级" in result[0]["detail"]
    assert issues == []


def test_tax_credit_does_not_replace_missing_required_document():
    checklist = [{"id": "A03", "name": "纳税信用等级", "status": "fail",
                  "detail": "缺少纳税信用等级证明"}]
    tax = {"status": "clear", "latest_year": 2025, "latest_grade": "A"}
    result, _ = aa.enhance_checklist_with_qcc(checklist, {}, {"tax_credit": tax})
    assert result[0]["status"] == "fail"
    assert "缺少纳税信用等级证明" in result[0]["detail"]
    assert "企业信息接口" in result[0]["detail"]


def test_qualification_certificate_client_maps_iso_records(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)

    def fake_post(url, headers, json, timeout):
        del headers, timeout
        assert url.endswith("/webapi/m/certificate/2.0")
        assert json == {
            "name": "测试制造有限公司",
            "certificateName": "",
            "pageNum": 1,
            "pageSize": 20,
        }
        return _response({"status": 200, "result": [
            {"certNo": "QMS-001", "endDate": "2028-01-02",
             "certificateName": "质量管理体系认证证书"},
            {"certNo": "EMS-002", "endDate": "2028-02-03",
             "certificateType": "环境管理体系认证"},
            {"certNo": "OHS-003", "endDate": "2028-03-04",
             "certificateName": "管理体系认证", "detail": [
                 {"title": "认证标准", "content": "ISO 45001:2018"},
             ]},
            {"certNo": "OTHER-1", "endDate": "2029-01-01",
             "certificateName": "食品经营许可证"},
        ], "total": 4})

    monkeypatch.setattr(qcc.requests, "post", fake_post)
    result = qcc.fetch_qualification_certificates("测试制造有限公司")

    assert result["status"] == "ok"
    assert result["complete"] is True
    assert result["total"] == 4
    assert result["records"] == [
        {"iso_type": "iso9001", "cert_no": "QMS-001", "end_date": "2028-01-02"},
        {"iso_type": "iso14001", "cert_no": "EMS-002", "end_date": "2028-02-03"},
        {"iso_type": "iso45001", "cert_no": "OHS-003", "end_date": "2028-03-04"},
    ]


def test_relationship_uses_supplier_and_cccc_credit_codes(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    calls = []

    def fake_get(url, headers, params, timeout):
        calls.append((url, headers, params, timeout))
        return _response({"status": 200, "result": {
            "companyNames": params["companyNames"],
            "correlationships": [{"path": "测试路径"}],
        }})

    monkeypatch.setattr(qcc.requests, "get", fake_get)
    relationship = qcc.fetch_relationship_check("测试企业", "913000000000000000")
    assert relationship["status"] == "risk"
    assert relationship["relation_count"] == 1
    assert calls[0][2]["companyNames"] == (
        "913000000000000000," + qcc.CCCC_GROUP_CREDIT_CODE)
    assert set(calls[0][2]["rels"].split(",")) == set(qcc.RELATIONSHIP_TYPES)

    checklist = [{"id": "A14", "name": "中交集团关联关系",
                  "status": "pending", "detail": ""}]
    result, issues = aa.enhance_checklist_with_qcc(
        checklist, {}, {"relationship_check": relationship})
    assert result[0]["status"] == "manual"
    assert "1条关联路径" in result[0]["detail"]
    assert issues and issues[0].startswith("A14")


def test_relationship_permission_error_stays_manual(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(qcc.requests, "get", lambda *args, **kwargs: _response(
        {"status": "10010004", "msg": "无访问权限"}, status_code=401))
    relationship = qcc.fetch_relationship_check("测试企业")
    assert relationship["status"] == "unavailable"
    checklist = [{"id": "A14", "name": "中交集团关联关系",
                  "status": "pending", "detail": ""}]
    result, _ = aa.enhance_checklist_with_qcc(
        checklist, {}, {"relationship_check": relationship})
    assert result[0]["status"] == "manual"
    assert "查询未完成" in result[0]["detail"]
