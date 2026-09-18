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
from material_policy import file_types, hydrate_iso_cache, is_public_material


@pytest.mark.parametrize("labels,carrier", [("承运商", True), ("运输商/承运商", True),
    ("经销商/承运商", False), ("服务商/代理商/承运商", False), ("服务商", False)])
def test_carrier_e2_replaces_a07_only_for_pure_carriers(labels, carrier):
    rules, _ = aa.determine_supplier_rules({"sup_type": labels}, aa.load_rules())
    ids = {r["id"] for r in rules}
    assert ("E2" in ids) is carrier
    assert ("A07" in ids) is not carrier


@pytest.mark.parametrize("filename", ["过往业绩证明.pdf", "运输合同.jpg", "物流服务合同.pdf", "履约证明.docx", "2025年业绩清单.pdf"])
def test_e2_filename_presence_always_manual(filename):
    rules, _ = aa.determine_supplier_rules({"sup_type": "承运商"}, aa.load_rules())
    checklist, _, missing, _ = aa.build_checklist({}, [r for r in rules if r["id"] == "E2"],
        [{"fileName": filename, "types": []}])
    assert not missing
    assert checklist[0]["status"] == "manual"
    assert filename in checklist[0]["detail"]
    html = gen_stage2_report.render_supplier("1", {"decision": "manual", "checklist": checklist}, {}, {})
    assert "已提交（仅文件名核查）" in html
    assert "转人工核查" in html


@pytest.mark.parametrize("materials", [[], [{"fileName": "材料.pdf", "typeName": "过往业绩证明", "desc": "运输合同"}],
    [{"fileName": "无需业绩证明声明.pdf"}], [{"fileName": "运输合同模板.pdf"}]])
def test_e2_missing_is_numbered_in_return_opinion(materials):
    rules, _ = aa.determine_supplier_rules({"sup_type": "承运商"}, aa.load_rules())
    checklist, _, missing, _ = aa.build_checklist({}, [r for r in rules if r["id"] == "E2"], materials)
    assert missing and checklist[0]["status"] == "fail"
    opinion = gen_opinion.build_opinion("1", {"decision": "reject", "checklist": checklist}, {}, {})
    assert "退回。1. 缺少过往业绩证明" in opinion
    html = gen_stage2_report.render_supplier("1", {"decision": "reject", "checklist": checklist}, {}, {})
    assert "过往业绩证明 未上传" in html


@pytest.mark.parametrize("name,position", [
    ("环境管理.png", "环境管理体系认证证书图片"),
    ("环境管理.png", " 环境管理体系认证证书 图片 "),
    ("环境管理.png", "其他文件"),
    ("certificate.png", "环境管理体系认证证书扫描件"),
])
def test_iso14001_alias_classification(name, position):
    result = aa.classify_uploaded_materials([{"fileName": name, "fileinfoTypeName": position}])
    assert "ISO14001" in result["certifications"]


def test_iso_legacy_cache_and_sensitive_veto():
    cache = {"1": {"supplier": {"certifications": []}, "materials_detail": [
        {"fileName": "环境管理.png", "typeName": "环境管理体系认证证书图片", "types": []}]}}
    hydrate_iso_cache(cache)
    assert cache["1"]["supplier"]["certifications"] == ["ISO14001"]
    assert file_types("1/10_环境管理.png", cache) == {"iso14001"}
    cache["1"]["materials_detail"][0]["desc"] = "含身份证"
    types = file_types("1/10_环境管理.png", cache)
    assert not is_public_material("1/10_环境管理.png", types)


def test_wrong_iso_candidate_is_not_approved():
    result = tp.extract("iso14001", "营业执照\n有效期至：2099年06月04日")
    assert result["checks"]["材料类型正确"] is None
    assert result["issues"]


def test_production_expiry_is_visible_after_holder_issue_and_in_return_list():
    certificate = tp.extract("production_license",
        "特种设备生产许可证\n单位名称：另一家有限公司\n有效期至：2026年05月16日",
        {"full_name": "合成科技有限公司"})
    assert certificate["checks"]["在有效期内"] is False
    checklist, _ = aa.enhance_checklist_with_textin(
        [{"id": "C1_05", "name": "生产许可证", "status": "pending"}], {},
        {"production_license": certificate})
    assert checklist[0]["status"] == "fail"
    s2 = {"decision": "reject", "name": "合成科技有限公司", "checklist": checklist}
    textin = {"1": {"production_license": certificate}}
    html = gen_stage2_report.render_supplier("1", s2, textin, {})
    assert "已过期" in html
    assert "2026-05-16" in html
    opinion = gen_opinion.build_opinion("1", s2, textin, {})
    first_line = opinion.splitlines()[0]
    assert first_line.startswith("退回。1.")
    assert "C1_05" in first_line and "已过期（2026-05-16）" in first_line


@pytest.mark.parametrize("labels,expected", [
    ("经销商/承运商", "D1_03"),
    ("服务商/代理商/承运商", "D1_03"),
    ("服务商/承运商", "A01"),
    ("承运商", "E1"), ("运输商/承运商", "E1"),
])
def test_carrier_routing(labels, expected):
    supplier = {"sup_type": labels, "busi_scope": "销售生产"}
    assert aa.is_special_category(supplier)[0] is False
    rules, _ = aa.determine_supplier_rules(supplier, aa.load_rules())
    ids = {r["id"] for r in rules}
    assert expected in ids
    assert "A01" in ids
    if "服务商" in labels and "代理商" not in labels:
        assert "D1_03" not in ids
    assert ("E1" in ids) is aa._pure_carrier(supplier)


@pytest.mark.parametrize("text,status", [
    ("道路运输经营许可证\n有效期至：2099年06月04日", "pass"),
    ("无船承运业务经营资格登记证\n有效期至：2099年06月04日", "pass"),
    ("道路运输经营许可证\n有效期至：2020年06月04日", "fail"),
    ("道路运输经营许可证", "manual"),
    ("道路运输经营许可证\n发证日期：2026年06月04日", "manual"),
    ("道路运输经营许可证\n有效期：2098年06月04日至2099年06月04日", "manual"),
    ("营业执照\n有效期至：2099年06月04日", "manual"),
])
def test_e1_ocr(text, status):
    rules, _ = aa.determine_supplier_rules({"sup_type": "承运商"}, aa.load_rules())
    checklist, _, missing, _ = aa.build_checklist({}, [r for r in rules if r["id"] == "E1"])
    assert not missing
    assert checklist[0]["status"] == "manual"
    result = tp.extract("transport_license", text)
    checklist, _ = aa.enhance_checklist_with_textin(checklist, {}, {"transport_license": result})
    assert checklist[0]["status"] == status


def test_e1_does_not_borrow_iso_expiry():
    result = tp.extract("transport_license", "", detail=[
        {"page_id": 1, "text": "道路运输经营许可证"},
        {"page_id": 2, "text": "ISO9001 有效期至：2099年06月04日"},
    ])
    assert result["checks"]["在有效期内"] is None


def test_e1_legacy_date_only_cannot_pass():
    checklist = [{"id": "E1", "name": "运输承运资质", "status": "manual"}]
    checklist, _ = aa.enhance_checklist_with_textin(checklist, {}, {
        "transport_license": {"checks": {"在有效期内": True}}})
    assert checklist[0]["status"] == "manual"


@pytest.mark.parametrize("capital,expected", [(499.99, False), (500, True), (500.01, True)])
def test_capital_boundary(capital, expected):
    assert safe_eval_rule("supplier.get('registered_capital', 0) >= 500", {"registered_capital": capital}) is expected


@pytest.mark.parametrize("scope,system", [
    ("**经营范围**\n\n说明：\n\n一般项目：国际货物运输代理；\n国内集装箱货物运输代理。\n注册资本：500万元",
     "一般项目：国际货物运输代理；国内集装箱货物运输代理。"),
    ("经营范围：技术服务；软件开发\n登记机关：市场监督管理局", "软件开发；技术服务"),
    ("经营范围：一般项目：软件开发；道路货物运输（不含危险、货物）\n成立日期：2020年01月01日",
     "一般项目:软件 开发;道路货物运输(不含危险货物)"),
    ("经营范围：软件开发（除依法须经批准的项目外，凭营业执照依法自主开展经营活动）", "软件开发"),
])
def test_scope_multiline_and_presentation(scope, system):
    result = tp.extract("business_license", scope, {"busi_scope": system})
    assert result["checks"]["经营范围一致"] is True
    assert "说明：" not in (result["fields"]["经营范围"] or "")
    assert "登记机关" not in (result["fields"]["经营范围"] or "")


def test_scope_no_500_character_truncation():
    scope = "；".join(f"业务编号{i}的技术服务" for i in range(100))
    result = tp.extract("business_license", "经营范围：" + scope + "\n注册资本：500万元", {"busi_scope": scope})
    assert result["fields"]["经营范围"] == scope
    assert result["checks"]["经营范围一致"] is True


@pytest.mark.parametrize("ocr,system", [
    ("经营范围\n说明：\n注册资本：500万元", "软件开发；技术服务"),
    ("经营范围：软件开发", ""),
    ("经营范围：软件开发", "软件开发；技术服务；道路货物运输；建筑工程施工"),
    ("经营范围：软件开发；技术服务；信息咨询；货物运输", "软件开发；技术服务；信息咨询；货物运输；货物进出口"),
    ("经营范围：道路货物运输（不含危险货物）", "道路货物运输（含危险货物）"),
])
def test_scope_unknown_and_substantive_differences_are_manual(ocr, system):
    result = tp.extract("business_license", ocr, {"busi_scope": system})
    assert result["checks"]["经营范围一致"] is None
    assert any("经营范围" in issue for issue in result["issues"])


def test_scope_clearly_different_is_false():
    result = tp.extract("business_license", "经营范围：软件开发；技术服务", {"busi_scope": "道路运输；建筑施工"})
    assert result["checks"]["经营范围一致"] is False


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


def test_report_lists_every_passed_check_in_green():
    stage2 = {"name": "合成公司", "decision": "recommend", "checklist": [
        {"id": "A03", "name": "纳税信用等级", "status": "pass", "detail": "通过"}]}
    textin = {"1": {"tax_credit": {
        "fields": {"纳税信用级别": "A", "评价年度": "2025"},
        "checks": {"C级及以上": True, "评价年度=2025": True}, "issues": []}}}
    cache = {"1": {"supplier": {}, "materials_detail": [
        {"fileName": "纳税证明.pdf", "types": ["tax_credit"]}]}}
    rendered = gen_stage2_report.render_supplier("1", stage2, textin, cache)
    assert "核验通过：C级及以上、评价年度=2025" in rendered
    assert "class='ok-mini'" in rendered


def test_report_renders_aggregated_business_license_checks():
    stage2 = {"name": "北京昌佳泵业有限公司", "decision": "recommend", "checklist": [
        {"id": "A01", "name": "法律主体资格", "status": "pass", "detail": "OCR核验通过"}]}
    textin = {"1": {"business_license": {
        "fields": {"附件1(license-a.jpg) 名称": "北京昌佳泵业有限公司"},
        "checks": {
            "附件1(license-a.jpg) 信用代码一致": True,
            "附件1(license-a.jpg) 名称一致": True,
            "附件2(license-b.jpg) 信用代码一致": True,
            "附件2(license-b.jpg) 名称一致": True,
        }, "issues": []}}}
    cache = {"1": {"supplier": {}, "materials_detail": [
        {"fileName": "license-a.jpg", "types": ["business_license"]},
        {"fileName": "license-b.jpg", "types": ["business_license"]}]}}
    rendered = gen_stage2_report.render_supplier("1", stage2, textin, cache)
    assert "无核验数据" not in rendered
    assert "附件1 信用代码一致、名称一致；附件2 信用代码一致、名称一致" in rendered
    assert rendered.count("附件1 ") == 1
    assert rendered.count("附件2 ") == 1


def test_final_opinion_contains_every_missing_material():
    stage2 = {"name": "合成公司", "decision": "reject", "checklist": [
        {"id": "A03", "name": "纳税信用等级", "status": "fail",
         "detail": "缺少纳税信用等级证明（须为国家税务总局网站或信用中国下载的正规文件，C级及以上）"},
        {"id": "X99", "name": "新增资质", "status": "fail",
         "detail": "缺少新增专项资质证明"},
    ]}
    opinion = gen_opinion.build_opinion("1", stage2, {}, {"1": {"supplier": {}}})
    assert opinion.startswith("退回。1. ")
    assert "请补充资质文件：" not in opinion
    assert "缺少纳税信用等级证明" in opinion
    assert "缺少新增专项资质证明" in opinion
    assert opinion.index("缺少纳税信用等级证明") < opinion.index("缺少新增专项资质证明")


def test_demo_three_decisions():
    from demo import cases
    results = cases()
    assert {r["decision"] for r in results.values()} == {"recommend", "reject", "manual"}
    for tid, result in results.items():
        assert gen_stage2_report.render_supplier(tid, result, {}, {})
