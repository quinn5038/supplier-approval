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


def test_production_license_rejects_business_license_plus_iso_date():
    detail = [
        {"page_id": 1, "text": "营业执照 名称 合成铁路器材有限公司"},
        {"page_id": 2, "text": "质量管理体系认证证书 ISO 9001 有效期至：2099年06月04日"},
    ]
    extracted = tp.extract(
        "production_license", "营业执照\n质量管理体系认证证书\n有效期至：2099年06月04日",
        {"full_name": "合成铁路器材有限公司"}, detail)
    assert extracted["checks"]["材料类型正确"] is False
    checklist, _ = aa.enhance_checklist_with_textin(
        [{"id": "C1_05", "name": "生产许可证", "status": "pending", "detail": "待核验"}],
        {}, {"production_license": extracted})
    assert checklist[0]["status"] == "fail"
    assert "ISO 9001" in checklist[0]["detail"]


@pytest.mark.parametrize("certificate", [
    "全国工业产品生产许可证 许可证编号：XK00-001-12345",
    "中国国家强制性产品认证证书 CCC认证 证书编号：2026999999999999",
])
def test_real_production_or_ccc_certificate_can_pass(certificate):
    text = f"{certificate} 持证单位 合成铁路器材有限公司 有效期至：2099年12月31日"
    extracted = tp.extract(
        "production_license", text, {"full_name": "合成铁路器材有限公司"},
        [{"page_id": 1, "text": text}])
    assert extracted["checks"] == {
        "材料类型正确": True, "持证主体一致": True, "在有效期内": True}
    checklist, _ = aa.enhance_checklist_with_textin(
        [{"id": "C1_05", "name": "生产许可证", "status": "pending", "detail": "待核验"}],
        {}, {"production_license": extracted})
    assert checklist[0]["status"] == "pass"


def test_expired_production_license_fails():
    text = ("全国工业产品生产许可证 持证单位 合成铁路器材有限公司 "
            "有效期至：2020年01月01日")
    extracted = tp.extract(
        "production_license", text, {"full_name": "合成铁路器材有限公司"},
        [{"page_id": 1, "text": text}])
    assert extracted["checks"]["材料类型正确"] is True
    assert extracted["checks"]["在有效期内"] is False


def test_legacy_production_license_cache_cannot_pass_on_date_alone():
    legacy = {"fields": {"有效期至": "2099-12-31"},
              "checks": {"在有效期内": True}, "issues": []}
    checklist, _ = aa.enhance_checklist_with_textin(
        [{"id": "C1_05", "name": "生产许可证", "status": "pending", "detail": "待核验"}],
        {}, {"production_license": legacy})
    assert checklist[0]["status"] == "manual"
    assert checklist[0]["status"] != "pass"


def test_after_sales_statement_uses_recent_signature_date():
    from datetime import timedelta
    recent = (date.today() - timedelta(days=20)).strftime("%Y年%m月%d日")
    extracted = tp.extract(
        "after_sales_cert",
        f"售后服务证明函\n我公司提供售后服务保障。\n日期：{recent}")
    assert extracted["checks"]["材料类型正确"] is True
    assert extracted["checks"]["落款时间在3个月内"] is True


def test_after_sales_certificate_uses_expiry_date():
    extracted = tp.extract(
        "after_sales_cert",
        "商品售后服务评价体系认证 售后服务认证证书 五星级 有效期至：2099年12月31日")
    assert extracted["checks"]["材料类型正确"] is True
    assert extracted["checks"]["在有效期内"] is True


def test_unrelated_file_cannot_pass_as_after_sales_material():
    extracted = tp.extract("after_sales_cert", "营业执照 注册资本1000万元")
    assert extracted["checks"]["材料类型正确"] is False


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
