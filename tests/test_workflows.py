import ast
import json
import os
import time
from datetime import date
from unittest.mock import Mock

import pytest

import auto_approve as aa
import gen_opinion
import gen_stage2_report
import textin_pipeline as tp
from eval import safe_eval_rule
from financial_data import assess_financial


@pytest.mark.parametrize("capital,expected", [(499.99, False), (500, True), (500.01, True)])
def test_capital_boundary(capital, expected):
    assert safe_eval_rule("supplier.get('registered_capital', 0) >= 500", {"registered_capital": capital}) is expected


@pytest.mark.parametrize("expr", ["__import__('os').system('bad')", "().__class__.__bases__", "supplier.clear()", "open('x')", "supplier['missing']", "1/0"])
def test_rule_escape_rejected(expr):
    assert safe_eval_rule(expr, {}) is False


def test_rules_unique_and_parseable():
    rules = []
    def visit(value):
        if isinstance(value, dict):
            if "id" in value:
                rules.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(aa.load_rules())
    assert len(rules) == len({r["id"] for r in rules})
    for rule in rules:
        if rule.get("rule"):
            ast.parse(rule["rule"], mode="eval")


def test_hmt_stage2_and_opinion(tmp_path, monkeypatch):
    cache = {"1": {"supplier": {"name": "合成港澳台公司", "is_hmt": True}, "todo": {}}}
    monkeypatch.setattr(aa, "_load_cache", lambda: cache)
    monkeypatch.setattr(aa, "BASE_DIR", tmp_path)
    monkeypatch.setattr(aa, "QCC_FILE", tmp_path / "none.json")
    monkeypatch.setattr(aa, "STAGE2_FILE", tmp_path / "stage2.json")
    aa.run_stage2()
    result = json.loads(aa.STAGE2_FILE.read_text(encoding="utf-8"))["1"]
    assert result["decision"] == "manual"
    assert "转人工" in gen_opinion.build_opinion("1", result, {}, cache)
    assert "同意" not in gen_opinion.build_opinion("1", result, {}, cache)


def test_empty_checklist_never_recommends():
    assert "同意" not in gen_opinion.build_opinion("1", {"decision": "manual"}, {}, {})


@pytest.mark.parametrize("data", [{}, {"资产负债率": 0}, {"财务数据信息": [{"报告期": "2020", "资产负债率": 10}]},
    {"balance_sheet": {"result": [{"报告期": str(date.today().year - 1), "资产负债率": 0}]}, "cash_flow": {"result": []}}])
def test_financial_contract_never_false_pass(data):
    result = assess_financial(data)
    assert result["confidence"] == "manual_review"
    checklist, _ = aa.enhance_checklist_with_qcc([{"id": "A08", "status": "pending"}], {}, {"financial": data})
    assert checklist[0]["status"] == "manual"


def test_qcc_error_response_not_passed():
    checklist, _ = aa.enhance_checklist_with_qcc([{"id": "A01", "status": "pending"}], {}, {"reg_info": {"status": 200, "result": {}}})
    assert checklist[0]["status"] == "manual"


def test_ocr_does_not_erase_public_conflict():
    checklist = [{"id": "A01", "name": "法律主体资格", "status": "fail", "detail": "企查查法人冲突"}]
    result, _ = aa.enhance_checklist_with_textin(checklist, {}, {"business_license": {"checks": {"名称一致": True}}})
    assert result[0]["status"] == "fail"
    assert "企查查法人冲突" in result[0]["detail"]


def test_clean_delete_reports_size(tmp_path, monkeypatch):
    monkeypatch.setattr(tp, "FILES_DIR", tmp_path)
    folder = tmp_path / "1"
    folder.mkdir()
    file = folder / "synthetic.pdf"
    file.write_bytes(b"fixture")
    old = time.time() - 10 * 86400
    os.utime(file, (old, old))
    tp.clean_files_cache(days=7, do_delete=True)
    assert not file.exists()


def test_fetch_only_refreshes_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(aa, "FETCH_ONLY", True)
    monkeypatch.setattr(aa, "FETCH_IDS", {"1"})
    monkeypatch.setattr(aa, "_load_processed", lambda: {})
    monkeypatch.setattr(aa, "_load_cache", lambda: {"1": {"old": True}})
    monkeypatch.setattr(aa, "_save_cache", lambda data: None)
    monkeypatch.setattr(aa.time, "sleep", lambda *args: None)
    monkeypatch.setattr(aa, "fetch_pending_todos", lambda: {"data": {"rows": [{"id": "1", "businessBillType": "P0702"}]}})
    detail = Mock(return_value={"data": {"businessBillId": "b"}})
    monkeypatch.setattr(aa, "query_todo_detail", detail)
    monkeypatch.setattr(aa, "query_supplier_base_info", lambda *a: {"data": {"supRegistBaseInfoBO": {}}})
    monkeypatch.setattr(aa, "query_qualification_files", lambda *a: {"data": {"supFilesBOList": []}})
    aa.run()
    detail.assert_called_once_with("1")


def test_report_escapes_attributes():
    text = gen_stage2_report.esc("<script>'\"")
    assert "<" not in text and '"' not in text and "'" not in text


def test_demo_three_decisions():
    from demo import cases
    results = cases()
    assert {r["decision"] for r in results.values()} == {"recommend", "reject", "manual"}
    for tid, result in results.items():
        assert gen_stage2_report.render_supplier(tid, result, {}, {})
