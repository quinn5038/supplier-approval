import ast
import json
import os
import time
from datetime import date, timedelta
from unittest.mock import Mock

import pytest

import auto_approve as aa
import gen_opinion
import gen_stage2_report
import qcc_api_client
import textin_pipeline as tp
from eval import safe_eval_rule
from financial_data import (
    assess_financial,
    financial_report_presence,
    financial_report_title_check,
)
from material_policy import file_types, hydrate_iso_cache, is_public_material


@pytest.mark.parametrize("public_detail", [
    "未取得带报告期的公开财务数据", "已取得上年度公开财务数据", "旧版任意描述"])
def test_a08_missing_is_numbered_and_not_repeated_as_other_issue(public_detail):
    checklist = [
        {"id": "A03", "name": "纳税信用等级", "status": "fail",
         "detail": "缺少纳税信用等级证明"},
        {"id": "A08", "name": "资金财务状况", "status": "manual",
         "detail": public_detail},
    ]
    cache = {"1": {"supplier": {"has_financial_report": False},
                     "materials_detail": [{"fileName": "备案表.docx", "types": []}]}}
    opinion = gen_opinion.build_opinion(
        "1", {"decision": "reject", "checklist": checklist}, {}, cache)
    first_line, *rest = opinion.splitlines()
    assert "退回。" in first_line
    assert "缺少经审计的上年度财报" in first_line
    assert not any("[A08]" in line for line in rest)


def test_a08_only_missing_still_returns_with_numbered_item():
    checklist = [{"id": "A08", "name": "资金财务状况", "status": "manual",
                  "detail": "未取得带报告期的公开财务数据"}]
    cache = {"1": {"supplier": {"has_financial_report": False},
                     "materials_detail": []}}
    opinion = gen_opinion.build_opinion(
        "1", {"decision": "manual", "checklist": checklist}, {}, cache)
    assert opinion.startswith("退回。1. 缺少经审计的上年度财报")
    assert "另需整改/核实：[A08]" not in opinion


def test_a08_uploaded_can_remain_in_other_review_section():
    materials = [{"fileName": "2025年度审计报告.pdf", "types": ["financial_report"]}]
    assert financial_report_presence({"has_financial_report": False}, materials) is True
    checklist = [
        {"id": "A03", "name": "纳税信用等级", "status": "fail",
         "detail": "缺少纳税信用等级证明"},
        {"id": "A08", "name": "资金财务状况", "status": "manual",
         "detail": "已取得上年度公开财务数据，仍需核对审计意见"},
    ]
    cache = {"1": {"supplier": {"has_financial_report": False},
                     "materials_detail": materials}}
    opinion = gen_opinion.build_opinion(
        "1", {"decision": "reject", "checklist": checklist}, {}, cache)
    assert "缺少经审计的上年度财报" not in opinion.splitlines()[0]
    assert "另需整改/核实：[A08]资金财务状况" in opinion


@pytest.mark.parametrize("ids", [("C1_02", "C1_03", "C1_04"), ("D1_03", "D1_04", "D1_05")])
def test_iso_api_requires_exact_number_and_expiry_after_normalisation(ids):
    checklist = [
        {"id": cid, "name": label, "status": "pass", "detail": "OCR通过"}
        for cid, label in zip(ids, ("ISO 9001", "ISO 14001", "ISO 45001"))
    ]
    supplier_name = "合成测试有限公司"
    textin = {
        "iso9001": {"fields": {"证书编号": " qms－ 001 ", "有效期至": "2028年1月2日"}},
        "iso14001": {"fields": {"证书编号": "ems-002", "有效期至": "2028/02/03"}},
        "iso45001": {"fields": {"证书编号": "OHS-003", "有效期至": "2028.3.4"}},
    }
    for doc in textin.values():
        doc["fields"]["获证组织"] = supplier_name
        doc["checks"] = {"材料类型正确": True, "在有效期内": True}
    api = {"status": "ok", "complete": True, "records": [
        {"iso_type": "iso9001", "cert_no": "QMS-001", "end_date": "2028-01-02"},
        {"iso_type": "iso14001", "cert_no": "EMS-002", "end_date": "2028-02-03"},
        {"iso_type": "iso45001", "cert_no": "OHS-003", "end_date": "20280304"},
    ]}
    for record in api["records"]:
        record.update(certificate_status="有效", start_date="2025-01-03")

    lookup = Mock(return_value=api)
    result, issues = aa.enhance_iso_with_certificate_api(checklist, textin, supplier_name, lookup)

    lookup.assert_called_once_with(supplier_name)
    assert [row["status"] for row in result] == ["pass", "pass", "pass"]
    assert all("OCR与资质证书接口核验一致" in row["detail"] for row in result)
    assert all("接口状态=有效" in row["detail"] for row in result)
    assert issues == []


def test_c1_iso_api_guangdong_xuntong_counterexample_fails_all_three():
    checklist = [
        {"id": cid, "name": label, "status": "pass", "detail": "OCR通过"}
        for cid, label in (
            ("C1_02", "ISO 9001"),
            ("C1_03", "ISO 14001"),
            ("C1_04", "ISO 45001"),
        )
    ]
    # 脱敏后的既有结果特征：三份材料识别出有效期，但均未识别出证书编号；
    # 生产接口仅有其他资质，没有任何 ISO 证书记录。
    textin = {
        kind: {"fields": {"证书编号": None, "有效期至": "2028-08-28"}}
        for kind in ("iso9001", "iso14001", "iso45001")
    }
    api = {"status": "ok", "complete": True, "records": [], "total": 1}

    result, issues = aa.enhance_iso_with_certificate_api(checklist, textin, "合成测试有限公司", lambda _: api)

    assert [row["status"] for row in result] == ["fail", "fail", "fail"]
    assert all("资质证书接口未查询到" in row["detail"] for row in result)
    assert len(issues) == 3


def test_c1_iso_api_reports_expiry_mismatch_for_same_number():
    checklist = [{"id": "C1_02", "name": "ISO 9001",
                  "status": "pass", "detail": "OCR通过"}]
    textin = {"iso9001": {"fields": {
        "证书编号": "QMS-001", "有效期至": "2028-01-02"}}}
    api = {"status": "ok", "complete": True, "records": [{
        "iso_type": "iso9001", "cert_no": "QMS-001", "end_date": "2029-01-02"}]}

    result, issues = aa.enhance_iso_with_certificate_api(checklist, textin, "合成测试有限公司", lambda _: api)

    assert result[0]["status"] == "fail"
    assert "证书编号一致但有效期不一致" in result[0]["detail"]
    assert issues and "OCR「2028-01-02」，接口「2029-01-02」" in issues[0]


@pytest.mark.parametrize("cid", ["C1_02", "D1_03"])
@pytest.mark.parametrize("status,start,end,reason", [
    ("撤销", "2025-01-03", "2099-01-02", "证书状态为「撤销」"),
    ("暂停", "2025-01-03", "2099-01-02", "证书状态为「暂停」"),
    (None, "2025-01-03", "2099-01-02", "未明确确认"),
    ("有效", "2098-01-03", "2099-01-02", "尚未生效"),
    ("有效", "2000-01-03", "2003-01-02", "已过期"),
    ("有效", "", "2099-01-02", "未提供ISO 9001证书完整有效的起止日期"),
])
def test_iso_api_must_confirm_status_and_current_validity(cid, status, start, end, reason):
    supplier = "合成测试有限公司"
    checklist = [{"id": cid, "name": "ISO 9001", "status": "pass"}]
    ocr = {"iso9001": {"fields": {"证书编号": "SYN-001", "有效期至": end, "获证组织": supplier},
                       "checks": {"材料类型正确": True, "证书状态有效": True, "在有效期内": True}}}
    record = {"iso_type": "iso9001", "cert_no": "SYN-001", "start_date": start, "end_date": end,
              "certificate_status": status}
    records = [record]
    if status == "撤销":
        # A second record with the same number and expiry cannot mask revocation.
        records.insert(0, {**record, "certificate_status": "有效"})
    lookup = Mock(return_value={"status": "ok", "complete": True, "records": records})

    result, issues = aa.enhance_iso_with_certificate_api(checklist, ocr, supplier, lookup)

    assert result[0]["status"] == "fail"
    assert reason in result[0]["detail"] and reason in issues[0]


@pytest.mark.parametrize("cid,doc_type,label", [
    ("C1_02", "iso9001", "ISO 9001"),
    ("C1_03", "iso14001", "ISO 14001"),
    ("C1_04", "iso45001", "ISO 45001"),
])
def test_iso_api_keeps_near_expiry_renewal_as_explicit_manual_reason(
        cid, doc_type, label):
    expiry = date.today() + timedelta(days=30)
    start = date.today() - timedelta(days=365)
    number = "SYN-RENEW-001"
    supplier = "合成变压器有限公司"
    reminder = f"{label.replace(' ', '')}证书将于{expiry.isoformat()}到期（90天内，提醒续证）"
    checklist = [{"id": cid, "name": label, "status": "manual",
                  "detail": "OCR核验需人工：" + reminder}]
    ocr = {doc_type: {
        "fields": {"证书编号": number, "有效期至": expiry.isoformat(),
                   "获证组织": supplier},
        "checks": {"材料类型正确": True, "持有人一致": True,
                   "在有效期内": True, "OCR文字置信度充足": True},
        "issues": [reminder],
    }}
    lookup = Mock(return_value={"status": "ok", "complete": True, "records": [{
        "iso_type": doc_type, "cert_no": number,
        "start_date": start.isoformat(), "end_date": expiry.isoformat(),
        "certificate_status": "有效",
    }]})

    result, issues = aa.enhance_iso_with_certificate_api(
        checklist, ocr, supplier, lookup)

    assert result[0]["status"] == "manual"
    assert "OCR与资质证书接口核验一致" in result[0]["detail"]
    assert "提醒续证" in result[0]["detail"]
    assert "核验未通过" not in result[0]["detail"]
    assert issues and "请人工确认续证安排" in issues[0]


@pytest.mark.parametrize("rule_group,ids", [
    ("domestic_manufacturer", ("C1_02", "C1_03", "C1_04")),
    ("domestic_trader", ("D1_03", "D1_04", "D1_05")),
])
@pytest.mark.parametrize("certifications", [[], ["ISO9001"], ["ISO14001"], ["ISO9001", "ISO14001", "ISO45001"]])
def test_stage2_queries_certificate_api_only_for_uploaded_iso(tmp_path, monkeypatch,
                                                            certifications, rule_group, ids):
    rules = aa.load_rules()["part_2_by_type"][rule_group]
    iso_rules = [rule for rule in rules if rule["id"] in ids]
    supplier = {"name": "合成制造有限公司", "certifications": certifications}
    materials = [{"fileName": kind + ".pdf", "types": [kind.lower()]} for kind in certifications]
    cache = {"1": {"supplier": supplier, "materials_detail": materials}}
    ocr = {"1": {"iso9001": {
        "fields": {"证书编号": "QMS-001", "有效期至": "2099-01-02", "获证组织": supplier["name"]},
        "checks": {"材料类型正确": True, "持有人一致": True, "在有效期内": True},
    }}}
    records = [{
        "iso_type": "iso9001", "cert_no": "QMS-001", "end_date": "2099-01-02",
    }, {"iso_type": "iso14001", "cert_no": "EMS-001", "end_date": "2099-01-02"}]
    for record in records:
        record.update(certificate_status="有效", start_date="2025-01-03")
    if certifications == ["ISO9001"]:
        ocr["1"]["iso9001"]["fields"]["证书状态"] = "暂停"
        ocr["1"]["iso9001"]["checks"]["证书状态有效"] = False
        ocr["1"]["iso9001"]["issues"] = ["ISO9001证书状态为「暂停」"]
    if len(certifications) == 3:
        # ISO9001 reports an expired date and a noisy holder containing the supplier name.
        ocr["1"]["iso9001"]["fields"].update(有效期至="2025-04-28", 获证组织="QMS" + supplier["name"])
        ocr["1"]["iso9001"]["checks"]["在有效期内"] = False
        ocr["1"]["iso9001"]["issues"] = ["ISO9001证书已过期（2025-04-28）"]
        for kind in ("iso14001", "iso45001"):
            ocr["1"][kind] = {"fields": {"有效期至": "2099-01-02", "获证组织": supplier["name"]},
                              "checks": {"材料类型正确": True, "持有人一致": True, "在有效期内": True}}
        records = []
    api = Mock(return_value={"status": "ok", "complete": True, "records": records})
    stage2_path = tmp_path / "stage2_results.offline.json"
    (tmp_path / "textin_results.offline.json").write_text(json.dumps(ocr), encoding="utf-8")
    monkeypatch.setenv("OCR_MODE", "offline")
    monkeypatch.setattr(aa, "BASE_DIR", tmp_path)
    monkeypatch.setattr(aa, "QCC_FILE", tmp_path / "qcc_results.json")
    monkeypatch.setattr(aa, "STAGE2_FILE", stage2_path)
    monkeypatch.setattr(aa, "FETCH_IDS", {"1"})
    monkeypatch.setattr(aa, "_load_cache", lambda: cache)
    monkeypatch.setattr(aa, "determine_supplier_rules", lambda *_: (iso_rules, "境内厂家"))
    monkeypatch.setattr(qcc_api_client, "fetch_a10_checks", Mock(return_value={}))
    monkeypatch.setattr(qcc_api_client, "fetch_tax_credit", Mock(return_value={}))
    monkeypatch.setattr(qcc_api_client, "fetch_qualification_certificates", api)

    aa.run_stage2()

    result = json.loads(stage2_path.read_text(encoding="utf-8"))["1"]
    opinion = gen_opinion.build_opinion("1", result, ocr, cache)
    rows = {row["id"]: row for row in result["checklist"]}
    if certifications == ["ISO9001"]:
        api.assert_called_once_with(supplier["name"])
        assert rows[ids[0]]["status"] == "pass"
        assert "缺少ISO14001、ISO45001" in opinion
        assert "未取得OCR识别结果" not in opinion
    elif certifications == ["ISO14001"]:
        api.assert_called_once_with(supplier["name"])
        assert "未取得OCR识别结果" in rows[ids[1]]["detail"]
        assert "缺少ISO9001、ISO45001" in opinion
    elif len(certifications) == 3:
        api.assert_called_once_with(supplier["name"])
        html = gen_stage2_report.render_supplier("1", result, ocr, cache)
        assert all(row["status"] == "fail" for row in rows.values())
        for code in ("9001", "14001", "45001"):
            message = f"资质证书接口未查询到ISO {code}证书记录"
            assert message in html and message in opinion
        assert "已过期" not in html and "已过期" not in opinion and "已过期" not in result["opinion"]
    else:
        api.assert_not_called()
        assert result["decision"] == "reject"
        assert all(row["detail"].startswith("缺少") for row in rows.values())
        assert "缺少ISO9001、ISO14001、ISO45001" in opinion
        assert "未取得OCR识别结果" not in opinion


def test_stage2_skips_tax_credit_api_and_interface_text_when_a03_missing(
        tmp_path, monkeypatch):
    tax_rule = {
        "id": "A03", "name": "纳税信用等级", "check_type": "material",
        "rule": "supplier.get('tax_credit_rating_ok', False)",
        "requirement": "纳税信用等级证明（须为正规文件，C级及以上）",
        "verify": "核对来源、年度及等级",
    }
    supplier = {"name": "合成缺件有限公司", "tax_credit_rating_ok": False}
    cache = {"1": {"supplier": supplier, "todo": {}, "materials_detail": []}}
    stage2_path = tmp_path / "stage2_results.offline.json"
    (tmp_path / "qcc_results.json").write_text(json.dumps({supplier["name"]: {
        "tax_credit": {"status": "clear", "latest_year": 2025, "latest_grade": "A"},
    }}), encoding="utf-8")
    monkeypatch.setenv("OCR_MODE", "offline")
    monkeypatch.setattr(aa, "BASE_DIR", tmp_path)
    monkeypatch.setattr(aa, "QCC_FILE", tmp_path / "qcc_results.json")
    monkeypatch.setattr(aa, "STAGE2_FILE", stage2_path)
    monkeypatch.setattr(aa, "FETCH_IDS", {"1"})
    monkeypatch.setattr(aa, "_load_cache", lambda: cache)
    monkeypatch.setattr(aa, "determine_supplier_rules", lambda *_: ([tax_rule], "境内供应商"))
    monkeypatch.setattr(qcc_api_client, "fetch_a10_checks", Mock(return_value={}))
    tax_api = Mock(return_value={"status": "clear", "latest_year": 2025,
                                 "latest_grade": "A"})
    monkeypatch.setattr(qcc_api_client, "fetch_tax_credit", tax_api)

    aa.run_stage2()

    tax_api.assert_not_called()
    result = json.loads(stage2_path.read_text(encoding="utf-8"))["1"]
    a03 = next(row for row in result["checklist"] if row["id"] == "A03")
    assert a03["status"] == "fail"
    assert "缺少纳税信用等级证明" in a03["detail"]
    assert "企业信息接口" not in a03["detail"]
    assert result["qcc_issues"] == []
    opinion = gen_opinion.build_opinion("1", result, {}, cache)
    assert "缺少纳税信用等级证明" in opinion
    assert "企业信息接口" not in opinion


def test_d1_03_agent_iso_checks_the_certificate_holder_and_requires_authorization_review():
    supplier, manufacturer = "合成贸易有限公司", "合成生产有限公司"
    checklist = [{"id": "D1_03", "name": "ISO 9001", "status": "fail"}]
    ocr = {"iso9001": {
        "fields": {"证书编号": "SYN-QMS-001", "有效期至": "2099-01-02", "获证组织": manufacturer},
        "checks": {"材料类型正确": True, "持有人一致": False, "在有效期内": True},
    }}
    lookup = Mock(return_value={"status": "ok", "complete": True, "records": [
        {"iso_type": "iso9001", "cert_no": "SYN-QMS-001", "end_date": "2099-01-02",
         "start_date": "2025-01-03", "certificate_status": "有效"},
    ]})

    result, _ = aa.enhance_iso_with_certificate_api(checklist, ocr, supplier, lookup)

    lookup.assert_called_once_with(manufacturer)
    assert result[0]["status"] == "manual"
    assert "人工核对厂家与贸易商的授权关系" in result[0]["detail"]


@pytest.mark.parametrize("label", [
    "证书注册号：QMS-2028-001",
    "证书号：QMS-2028-001",
    "证     书     号：QMS-2028-001",
    "证　书　号：QMS-2028-001",
    "证\n书\n号\nQMS-2028-001",
    "Certificate No.: QMS-2028-001",
    "Registration Number: QMS-2028-001",
])
def test_iso_ocr_extracts_common_certificate_number_labels(label):
    text = ("质量管理体系认证证书\n兹证明\n合成制造有限公司\n"
            f"{label}\n有效期至：2028年12月31日")

    result = tp.extract("iso9001", text, {"full_name": "合成制造有限公司"})

    assert result["fields"]["证书编号"] == "QMS-2028-001"


@pytest.mark.parametrize("doc_type,title,number", [
    ("iso9001", "质量管理体系认证证书", "SYN-QMS-001"),
    ("iso14001", "环境管理体系认证证书", "SYN-EMS-001"),
    ("iso45001", "职业健康安全管理体系认证证书", "SYN-OHS-001"),
])
def test_all_three_iso_types_extract_widely_spaced_certificate_number(
        doc_type, title, number):
    text = (f"{title}\n兹证明\n合成变压器有限公司\n"
            f"证     书     号：{number}\n有效期至：2028年12月31日")

    result = tp.extract(doc_type, text, {"full_name": "合成变压器有限公司"})

    assert result["fields"]["证书编号"] == number


@pytest.mark.parametrize("separator", [".", "．", "。"])
def test_iso_number_dot_separator_keeps_grouped_digits(separator):
    text = ("质量管理体系认证证书\n"
            f"证书编号{separator}44 100 2588 0053 有效期起始日2025-10-28\n"
            "有效期至2028-10-27")
    result = tp.extract("iso9001", text)
    assert result["fields"]["证书编号"] == "44 100 2588 0053"


def test_iso_number_does_not_consume_following_english_line():
    number = tp._extract_iso_certificate_number(
        "CertificateRegistration No.44 100 2588 0053\nISO 9001:2015")
    assert number == "44 100 2588 0053"


@pytest.mark.parametrize("good_score,bad_score,expected", [
    (.987, .962, "11425S212926R0S"),
    (.96, .99, None),
    (.987, .986, None),
])
def test_iso_repeated_number_requires_stronger_independent_evidence(
        good_score, bad_score, expected):
    text = ("职业健康安全管理体系认证证书\n"
            "证书号：411425S212926R0S\n"
            "CertificateNo.:11425S212926R0S\n有效期至2028-01-01")
    detail = [
        {"text": "证书号：411425S212926R0S", "confidence": bad_score},
        {"text": "CertificateNo.:11425S212926R0S", "confidence": good_score},
    ]
    result = tp.extract("iso45001", text, detail=detail)
    assert result["fields"]["证书编号"] == expected
    if expected is None:
        assert any("证书编号存在多个不一致" in issue for issue in result["issues"])


def test_iso_single_number_does_not_lose_leading_digit():
    assert tp._extract_iso_certificate_number(
        "证书号：411425S212926R0S") == "411425S212926R0S"


@pytest.mark.parametrize("name,status,years", [
    ("2025年度审计报告.pdf", "matched", [2025]),
    ("2024年财报.pdf", "mismatch", [2024]),
    ("经审计财务报告.pdf", "unknown", []),
])
def test_a08_title_year_check(name, status, years):
    result = financial_report_title_check({}, [{
        "fileName": name, "types": ["financial_report"],
    }], current_year=2026)
    assert result == {"status": status, "submitted": True,
                      "required_year": 2025, "title_years": years}


def test_a08_old_title_year_is_returned_as_missing_prior_year_report():
    rule = next(r for r in aa.load_rules()["part_1_basic"]["domestic"]
                if r["id"] == "A08")
    materials = [{"fileName": "2024年财报.pdf", "types": ["financial_report"]}]
    supplier = {}
    checklist, _, missing, _ = aa.build_checklist(supplier, [rule], materials)

    assert checklist[0]["status"] == "fail"
    assert "2024年" in checklist[0]["detail"]
    assert "2025年" in checklist[0]["detail"]
    assert missing == [rule]

    cache = {"1": {"supplier": supplier, "materials_detail": materials}}
    opinion = gen_opinion.build_opinion(
        "1", {"decision": "reject", "checklist": checklist}, {}, cache)
    first_line, *rest = opinion.splitlines()
    assert "退回。1. 缺少经审计的上年度财报" in first_line
    assert not any("[A08]" in line for line in rest)

    stage2_opinion, decision = aa.generate_opinion_v4(
        supplier, [], [rule], [], type_desc="境内")
    assert decision == "reject"
    assert stage2_opinion.count("缺少经审计的上年度财报") == 1


def test_a08_any_prior_year_title_satisfies_title_check():
    result = financial_report_title_check({}, [
        {"fileName": "2024年比较财务数据.pdf", "types": ["financial_report"]},
        {"fileName": "2025年度审计报告.pdf", "types": ["financial_report"]},
    ], current_year=2026)
    assert result["status"] == "matched"


def test_a08_uses_display_title_year_when_filename_is_generic():
    result = financial_report_title_check({}, [{
        "fileName": "财务附件.pdf", "fileTitle": "2024年财报",
        "types": ["financial_report"],
    }], current_year=2026)
    assert result["status"] == "mismatch"
    assert result["title_years"] == [2024]


def test_a08_unknown_upload_state_is_not_declared_missing():
    assert financial_report_presence({}) is None
    checklist = [{"id": "A08", "name": "资金财务状况", "status": "manual",
                  "detail": "上传状态待确认"}]
    opinion = gen_opinion.build_opinion(
        "1", {"decision": "manual", "checklist": checklist}, {}, {})
    assert "缺少经审计的上年度财报" not in opinion


def test_a08_missing_is_rule_failure_even_when_public_data_exists():
    supplier = {"has_financial_report": True}
    rule = next(r for r in aa.load_rules()["part_1_basic"]["domestic"]
                if r["id"] == "A08")
    checklist, _, missing, _ = aa.build_checklist(supplier, [rule], [])
    assert checklist[0]["status"] == "fail"
    assert missing == [rule]
    assert supplier["has_financial_report"] is False
    checklist, _ = aa.enhance_checklist_with_qcc(
        checklist, supplier, {"financial": {"报告期": str(date.today().year - 1)}})
    assert checklist[0]["status"] == "fail"
    assert "缺少经审计的上年度财报" in checklist[0]["detail"]


def test_business_license_inline_spaced_address_does_not_join_legal_person():
    text = ("统一社会信用代码 91370700MA3QM7Y36H\n"
            "名 称 山东优派斯装配式建筑有限公司\n"
            "注册资本 伍仟万元整\n"
            "法定代表人 徐沈腾 住 所 潍坊市滨海开发区央子街道珠江西一街00967号\n"
            "经营范围 建筑材料制造\n登记机关 潍坊市市场监督管理局")
    supplier = {"social_credit_code": "91370700MA3QM7Y36H",
                "full_name": "山东优派斯装配式建筑有限公司", "legal_person": "徐沈腾",
                "registered_capital": 5000, "busi_scope": "建筑材料制造"}
    result = tp.extract("business_license", text, supplier)
    assert result["fields"]["名称"] == "山东优派斯装配式建筑有限公司"
    assert result["fields"]["法定代表人"] == "徐沈腾"
    assert result["fields"]["住所"] == "潍坊市滨海开发区央子街道珠江西一街00967号"
    assert result["checks"]["法人一致"] is True
    assert not any("法人" in issue and "不一致" in issue for issue in result["issues"])


def test_business_license_cross_column_labels_do_not_join_legal_person():
    text = (
        "经营范围 法定代表人陈太江 类 名 91320411MABW059K0W 统一社会信用代码\n"
        "型其他有限责任公司 称台荣建材（常州）有限公司\n"
        "登记机关 2022年08月12日 500万元整"
    )
    result = tp.extract_business_license(text, {
        "full_name": "台荣建材（常州）有限公司",
        "legal_person": "陈太江",
        "social_credit_code": "91320411MABW059K0W",
    })

    assert result["fields"]["法定代表人"] == "陈太江"
    assert result["checks"]["法人一致"] is True
    assert not any("法人" in issue and "不一致" in issue for issue in result["issues"])


def test_sideways_business_license_label_noise_recovers_seen_system_legal_person():
    text = (
        "法定代表人 类 名 91370214599038194P 统一社会信用代码\n"
        "称 金属产品、机械设备及配件 研发、 韩巧玲 有限责任公司（自然人独资）\n"
        "青岛海隆升智能装备有限公司\n"
    )
    result = tp.extract_business_license(text, {
        "legal_person": "韩巧玲",
        "social_credit_code": "91370214599038194P",
    })

    assert result["fields"]["法定代表人"] == "韩巧玲"
    assert result["checks"]["法人一致"] is True
    assert not any("执照法人类名" in issue for issue in result["issues"])


def test_sideways_business_license_recovers_wrapped_name_after_residence_noise():
    text = (
        "名称 测试电气有限公司\n"
        "法定代表人强 住\n"
        "测试姓名\n"
        "经营范围 电气设备制造\n"
    )
    result = tp.extract_business_license(text, {
        "full_name": "测试电气有限公司",
        "legal_person": "测试姓名",
    })

    assert result["fields"]["法定代表人"] == "测试姓名"
    assert result["checks"]["法人一致"] is True
    assert not any("法人" in issue and "不一致" in issue
                   for issue in result["issues"])


def test_business_license_label_noise_without_seen_person_stays_missing():
    result = tp.extract_business_license(
        "法定代表人 类 名 91370214599038194P 统一社会信用代码",
        {"legal_person": "韩巧玲"},
    )

    assert result["fields"]["法定代表人"] is None
    assert "法人一致" not in result["checks"]


def test_business_license_long_dash_ocr_noise_does_not_join_legal_person():
    text = (
        "经营范围 法定代表人马卫东— 米\n"
        "名 91370211MA3RNQ0M3R 统一社会信用代码\n"
        "型有限责任公司（自然人投资或控股) 青岛亿丰通商贸有限公司 称"
    )
    result = tp.extract_business_license(text, {
        "full_name": "青岛国承科技发展有限公司",
        "legal_person": "马卫东",
        "social_credit_code": "91370212MACCHYX058",
    })

    assert result["fields"]["法定代表人"] == "马卫东"
    assert result["checks"]["法人一致"] is True
    assert not any("法人" in issue and "不一致" in issue for issue in result["issues"])


def test_business_license_reversed_residence_label_does_not_join_legal_person():
    text = (
        "统一社会信用代码 91441900055339351C\n"
        "名称 东莞市旭佳家具实业有限公司\n"
        "法定代表人 陈庆中 所东莞市塘厦镇莆沙南路3号C栋\n"
        "住\n经营范围 家具制造"
    )
    result = tp.extract_business_license(text, {
        "full_name": "东莞市旭佳家具实业有限公司",
        "legal_person": "陈庆中",
        "social_credit_code": "91441900055339351C",
    })

    assert result["fields"]["法定代表人"] == "陈庆中"
    assert result["checks"]["法人一致"] is True
    assert not any("法人" in issue and "不一致" in issue for issue in result["issues"])


def test_business_license_reverse_vertical_name_label_detects_mismatch():
    """离线 OCR 双栏展平为“公司名称 称”时仍应提取执照主体并触发 A01。"""
    text = ("法定代表人 马卫东\n"
            "统一社会信用代码 91370211MA3RNQ0M3R\n"
            "型 有限责任公司（自然人投资或控股） 青岛亿丰通商贸有限公司 称\n"
            "注册资本 壹佰万元整\n"
            "经营范围 汽车销售；互联网销售")
    supplier = {
        "social_credit_code": "91370212MACCHYX058",
        "full_name": "青岛国承科技发展有限公司",
        "legal_person": "马卫东",
        "registered_capital": 100,
        "busi_scope": "技术服务；汽车销售",
    }

    result = tp.extract("business_license", text, supplier)

    assert result["fields"]["名称"] == "青岛亿丰通商贸有限公司"
    assert result["checks"]["名称一致"] is False
    assert "执照名称「青岛亿丰通商贸有限公司」与系统「青岛国承科技发展有限公司」不一致" in result["issues"]
    assert aa._resolve_supplier_name(supplier, {"title": supplier["full_name"] + "/1"},
                                     {"business_license": result}) == supplier["full_name"]


def test_business_license_name_recovers_application_name_from_rotated_type_noise():
    expected = "上海智翀环保科技有限公司"
    text = (
        "统一社会信用代码 91110000SYNTHETIC1\n"
        "名称 型有限责任公司\n"
        "住所 上海市测试路1号 上海智翀环保科技有限公司\n"
        "经营范围 环保技术服务"
    )
    result = tp.extract("business_license", text, {"full_name": expected})
    assert result["fields"]["名称"] == expected
    assert result["checks"]["名称一致"] is True


def test_partnership_business_license_name_stops_before_contribution_amount():
    expected = "杭州汇鑫联合会计师事务所（普通合伙）"
    text = (
        "名称 " + expected + " 出 资 额伍拾万元整\n"
        "成立日期 2020年01月01日\n经营范围 审计服务"
    )
    result = tp.extract("business_license", text, {"full_name": expected})
    assert result["fields"]["名称"] == expected
    assert "伍拾万元" not in result["fields"]["名称"]
    assert result["checks"]["名称一致"] is True


@pytest.mark.parametrize(("application_name", "bad_ocr_name"), [
    ("上海智翀环保科技有限公司", "型有限责任公司"),
    ("杭州汇鑫联合会计师事务所（普通合伙）",
     "杭州汇鑫联合会计师事务所（普通合伙） 出 资 额伍拾万元整"),
])
def test_report_supplier_name_prefers_application_name(application_name, bad_ocr_name):
    resolved = aa._resolve_supplier_name(
        {}, {"title": application_name + "/申请编号"},
        {"business_license": {"fields": {"名称": bad_ocr_name}}},
    )
    assert resolved == application_name


def test_material_name_check_uses_todo_title_not_apply_unit_contract():
    supplier = {"name": "合成简称", "social_credit_code": "91110000123456789X"}
    todo = {
        "title": "合成自动化信息技术有限公司/2000200100000000000",
        "applyUnitName": "海外港口工程EPC机电专业分包合同",
    }
    resolved = tp._supplier_context_for_material_checks(supplier, todo)

    assert resolved["full_name"] == "合成自动化信息技术有限公司"
    without_title = tp._supplier_context_for_material_checks(
        supplier, {"applyUnitName": todo["applyUnitName"]})
    assert "full_name" not in without_title
    result = tp.extract(
        "business_license",
        "统一社会信用代码 91110000123456789X\n名称 合成自动化信息技术有限公司",
        resolved,
    )
    assert result["checks"]["名称一致"] is True
    assert not any("名称" in issue and "不一致" in issue for issue in result["issues"])


def test_business_license_lishang_spaced_address_boundary():
    text = ("统一社会信用代码 91130408MA0GATNL3F\n"
            "名 称 邯郸市利尚金属制品有限公司 注册资本 壹仟伍佰万元整\n"
            "法定代表人 吴艺濮 住 所河北省邯郸市永年区临洺关镇东洺阳村南\n"
            "经营范围 金属制品制造\n登记机关 邯郸市市场监督管理局")
    supplier = {"social_credit_code": "91130408MA0GATNL3F",
                "full_name": "邯郸市利尚金属制品有限公司", "legal_person": "吴艺濮",
                "registered_capital": 1500, "busi_scope": "金属制品制造"}
    result = tp.extract("business_license", text, supplier)
    assert result["fields"]["法定代表人"] == "吴艺濮"
    assert result["fields"]["住所"] == "河北省邯郸市永年区临洺关镇东洺阳村南"
    assert result["checks"]["法人一致"] is True


@pytest.mark.parametrize("placeholder", ["********", "＊＊＊＊＊＊＊＊"])
def test_business_license_asterisk_term_is_unbounded(placeholder):
    text = ("| 营业期限 | **2006-07-26 至 " + placeholder + "** |\n"
            "| 名称 | 正信光电科技股份有限公司 |")
    result = tp.extract("business_license", text, {})
    assert result["checks"]["营业期限长期或有效"] is True
    assert not any("营业期限已到期" in issue for issue in result["issues"])


@pytest.mark.parametrize("doc_type", ["iso9001", "iso14001", "iso45001"])
def test_iso_registration_valid_date_allows_ocr_spacing(doc_type):
    result = tp.extract(
        doc_type,
        "管理体系认证证书\n注册有效日期：2028 年 01 月 07 日",
        {},
    )
    assert result["fields"]["有效期至"] == "2028-01-07"
    assert result["checks"]["在有效期内"] is True
    assert not any("未识别到有效期" in issue for issue in result["issues"])


@pytest.mark.parametrize("doc_type,title", [
    ("iso9001", "质量管理体系认证证书"),
    ("iso14001", "环境管理体系认证证书"),
])
def test_iso_seal_split_date_range_does_not_use_issue_or_fake_day(doc_type, title):
    text = (f"{title}\n合成重工科技有限公司\n"
            "发证日期：2024年12月31日 有效期：2024年12月 2027年12月30日\n31 日至")
    result = tp.extract(doc_type, text, {"full_name": "合成重工科技有限公司"})

    assert result["fields"]["有效期至"] == "2027-12-30"
    assert result["checks"]["在有效期内"] is True


def test_three_system_pdf_scopes_type_and_expiry_to_each_certificate():
    text = (
        "质量管理体系认证合格证书\n注册号：SYN-QMS-001\n兹证明\n"
        "合成石化设备有限公司\nGB/T19001-2016/1S09001:2015\n"
        "有效期至：2027年06月24日\n\n"
        "Environmental Management System Certificate\n"
        "环境管理体系认证合格证书\n注册号：SYN-EMS-001\n兹证明\n"
        "合成石化设备有限公司\nGB/T24001-2016/1S014001:2015\n"
        "有效期至：2027年07月29日\n\n"
        "职业健康安全管理体系认证合格证书\n注册号：SYN-OHS-001\n兹证明\n"
        "合成石化设备有限公司\nGB/T45001-2020/1S045001:2018\n"
        "有效期至：2027年08月30日\n"
    )
    supplier = {"full_name": "合成石化设备有限公司"}

    expected = {
        "iso9001": ("2027-06-24", "SYN-QMS-001"),
        "iso14001": ("2027-07-29", "SYN-EMS-001"),
        "iso45001": ("2027-08-30", "SYN-OHS-001"),
    }
    for doc_type, (expiry, number) in expected.items():
        result = tp.extract(doc_type, text, supplier)
        assert result["fields"]["有效期至"] == expiry
        assert result["fields"]["证书编号"] == number
        assert result["checks"]["材料类型正确"] is True
        assert result["checks"]["持有人一致"] is True
        assert result["checks"]["在有效期内"] is True
        assert not result["issues"]


def test_iso_confidence_accepts_qualified_certificate_title():
    text = ("环境管理体系认证合格证书\n兹证明\n合成石化设备有限公司\n"
            "有效期至：2027年07月29日")
    result = tp.extract("iso14001", text, {"full_name": "合成石化设备有限公司"})
    detail = [
        {"text": "环境管理体系认证合格证书", "confidence": .99},
        {"text": "合成石化设备有限公司", "confidence": .99},
        {"text": "有效期至：2027年07月29日", "confidence": .99},
    ]

    tp._apply_ocr_confidence_review("iso14001", result, detail)

    assert result["checks"]["OCR文字置信度充足"] is True
    assert not any("置信度" in issue for issue in result["issues"])


@pytest.mark.parametrize(("doc_type", "title", "standard"), [
    ("iso9001", "质量管理体系认证证书", "GB/T19001-2016 idt ISO9001:2015"),
    ("iso14001", "环境管理体系认证证书", "GB/T24001-2016 idt ISO14001:2015"),
    ("iso45001", "职业健康安全管理体系认证证书", "GB/T45001-2020 idt ISO45001:2018"),
])
def test_iso_decorative_low_confidence_does_not_force_manual_review(
        doc_type, title, standard):
    text = (f"{title}\n成都伯斯曼实业有限公司\n{standard}\n"
            "有效期至：2028年07月18日")
    result = tp.extract(doc_type, text, {"full_name": "成都伯斯曼实业有限公司"})
    detail = [
        {"text": title, "confidence": .99},
        {"text": "成都伯斯曼实业有限公司", "confidence": .99},
        {"text": standard, "confidence": .98},
        {"text": "有效期至：2028年07月18日", "confidence": .99},
        {"text": "IAF", "confidence": .74},
        {"text": "（贴花）", "confidence": .77},
    ]

    tp._apply_ocr_confidence_review(doc_type, result, detail)

    assert result["checks"]["OCR文字置信度充足"] is True
    assert not any("置信度" in issue for issue in result["issues"])


def test_iso_low_confidence_holder_still_requires_manual_review():
    text = ("质量管理体系认证证书\n成都伯斯曼实业有限公司\n"
            "GB/T19001-2016 idt ISO9001:2015\n有效期至：2028年07月18日")
    result = tp.extract("iso9001", text, {"full_name": "成都伯斯曼实业有限公司"})
    detail = [
        {"text": "质量管理体系认证证书", "confidence": .99},
        {"text": "成都伯斯曼实业有限公司", "confidence": .82},
        {"text": "GB/T19001-2016 idt ISO9001:2015", "confidence": .98},
        {"text": "有效期至：2028年07月18日", "confidence": .99},
    ]

    tp._apply_ocr_confidence_review("iso9001", result, detail)

    assert result["checks"]["OCR文字置信度充足"] is None
    assert "ISO关键字段获证组织文字置信度较低（0.82），转人工核验" in result["issues"]


def test_iso_missing_confidence_detail_still_requires_manual_review():
    result = tp.extract(
        "iso9001",
        "质量管理体系认证证书\n成都伯斯曼实业有限公司\n有效期至：2028年07月18日",
        {"full_name": "成都伯斯曼实业有限公司"},
    )

    tp._apply_ocr_confidence_review("iso9001", result, None)

    assert result["checks"]["OCR文字置信度充足"] is None
    assert "OCR置信度明细缺失，转人工核验" in result["issues"]


def test_business_license_incomplete_date_range_does_not_expire_on_start_date():
    result = tp.extract("business_license", "营业期限：2006-07-26 至", {})
    assert result["checks"].get("营业期限长期或有效") is not False
    assert not any("营业期限已到期" in issue for issue in result["issues"])


@pytest.mark.parametrize("term,expected,expiry", [
    ("2015-01-01 至 2099-12-31", True, None),
    ("2006-07-26 至 2020-07-25", False, "2020-07-25"),
    ("2099-12-31", True, None),
])
def test_business_license_normal_terms_keep_existing_expiry_behavior(term, expected, expiry):
    result = tp.extract("business_license", "营业期限：" + term, {})
    assert result["checks"]["营业期限长期或有效"] is expected
    if expiry:
        assert any(expiry in issue for issue in result["issues"])
    else:
        assert not any("营业期限已到期" in issue for issue in result["issues"])


def test_business_scope_removes_inline_address_and_trailing_registration_label():
    ocr = ("经营范围 一般项目：白蚁防治、灭鼠及其它害虫防治的技术服务，"
           "农作物病虫害防治服 住 所 重庆市渝中区嘉陵东村45号2-3 "
           "务，林业有害生物防治服务，林业产品销售、机械零件、零部侏销售 "
           "登记机关 -->")
    system = ("白蚁防治、灭鼠及其它害虫防治的技术服务，"
              "农作物病虫害防治服务，林业有害生物防治服务，林业产品销售、"
              "机械零件、零部件销售")
    result = tp.extract("business_license", ocr, {"busi_scope": system})
    scope = result["fields"]["经营范围"]
    assert "住所" not in scope
    assert "重庆市渝中区嘉陵东村45号2-3" not in scope
    assert "登记机关" not in scope
    assert result["checks"]["经营范围一致"] is True
    assert 98 <= float(result["fields"]["经营范围相似度"].rstrip("%")) < 100


def test_business_scope_stops_at_inline_registration_and_annual_report_footer():
    scope = "技术开发；技术服务；软件开发"
    ocr = (
        "经营范围：" + scope
        + "登记机关猫市场主体应当于每年1月1日至6月30日通过"
        "国家市场监督管理总局监制国家企业信用信息公示系统网址、"
        "http、wwwgsxtgovcn国家企业信息公示系统报送公示年度报告"
    )
    result = tp.extract("business_license", ocr, {"busi_scope": scope})
    assert result["fields"]["经营范围"] == scope
    assert result["checks"]["经营范围一致"] is True
    assert "经营范围差异" not in result["fields"]


def test_business_scope_stops_at_annual_report_footer_when_field_label_is_missing():
    scope = "技术开发；技术服务；软件开发"
    ocr = ("经营范围：" + scope
           + "市场主体应当于每年1月1日至6月30日通过国家企业信用信息公示系统"
           "报送公示年度报告")
    result = tp.extract("business_license", ocr, {"busi_scope": scope})
    assert result["fields"]["经营范围"] == scope
    assert result["checks"]["经营范围一致"] is True


def test_business_scope_stops_before_inline_fields_from_sideways_license():
    ocr = (
        "经营范围 法定代表人 类 名 91310116MA1J8WBR88 统一社会信用代码 "
        "型有限责任公司 名称 测试环保科技有限公司 营业执照 材料销售 "
        "成立日期2016年10月21日 注册资本人民币3000万元整"
    )
    result = tp.extract("business_license", ocr, {
        "busi_scope": "环保技术服务；材料销售；橡胶制品销售",
    })
    assert result["fields"]["经营范围"] is None
    assert result["checks"]["经营范围一致"] is None
    assert any("经营范围提取不完整" in issue for issue in result["issues"])
    assert not any("经营范围与系统基本信息栏填写不一致" in issue for issue in result["issues"])


def test_business_scope_stops_at_stamp_corrupted_registration_footer():
    scope = (
        "服务：审查企业会计报表，验证企业资本，办理企业合并、分立、清算事宜中的"
        "审计业务，会计咨询，税务咨询。（依法须经批准的项目，经相关部门批准后方可开展经营活动）"
    )
    ocr = (
        "经营范围 " + scope
        + "\n业\n拱 记 机 关 市 理\n2023年08月28日\n"
        "国家企业信用信息公示系统网址http://www.gsxt.gov.cn"
    )
    result = tp.extract("business_license", ocr, {"busi_scope": scope})
    assert result["fields"]["经营范围"] == scope
    assert result["checks"]["经营范围一致"] is True
    assert "公示系统网址" not in result["fields"]["经营范围"]


def test_business_scope_keeps_publicity_system_inside_new_style_scope_notice():
    scope = "软件开发（具体经营项目请登录国家企业信用信息公示系统查询）"
    result = tp.extract("business_license", "经营范围：" + scope,
                        {"busi_scope": "软件开发"})
    assert "国家企业信用信息公示系统" in result["fields"]["经营范围"]
    assert result["checks"]["经营范围一致"] is None
    assert any("概括式" in issue for issue in result["issues"])


@pytest.mark.parametrize("doc_type,title,standard", [
    ("iso9001", "质量管理体系认证证书", "ISO9001:2015"),
    ("iso14001", "环境管理体系认证证书", "ISO14001:2015"),
    ("iso45001", "职业健康安全管理体系认证证书", "ISO45001:2018"),
])
def test_iso_holder_prefers_company_after_declaration_over_supervision_footer(
        doc_type, title, standard):
    text = (f"{title}\n注册号：43125Q31487R0S\n兹证明\n"
            "邯郸市利尚金属制品有限公司\n"
            f"统一社会信用代码：91130408MA0GATNL3F\n{standard}\n"
            "证书有效期：2025年01月17日至2028年01月16日\n"
            "本证书在行政许可范围内有效，获证组织须按规定接受年度监督。")
    result = tp.extract(doc_type, text, {"full_name": "邯郸市利尚金属制品有限公司"})
    assert result["fields"]["获证组织"] == "邯郸市利尚金属制品有限公司"
    assert result["fields"]["获证组织_来源"] == "兹证明"
    assert result["checks"]["持有人一致"] is True


def test_iso_supervision_footer_alone_is_not_holder():
    result = tp.extract("iso9001",
                        "质量管理体系认证证书\n获证组织须按规定接受年度监督。\n"
                        "有效期至：2028年01月16日",
                        {"full_name": "邯郸市利尚金属制品有限公司"})
    assert result["fields"]["获证组织"] is None
    assert "持有人一致" not in result["checks"]


@pytest.mark.parametrize("doc_type,title,english_title,system_name", [
    ("iso9001", "质量管理体系认证证书", "QMS CERTIFICATE OF REGISTRATION", "质量管理体系"),
    ("iso14001", "环境管理体系认证证书", "EMS CERTIFICATE OF REGISTRATION", "环境管理体系"),
    ("iso45001", "职业健康安全管理体系认证证书", "OHSMS CERTIFICATE OF REGISTRATION",
     "职业健康安全管理体系"),
])
def test_iso_declaration_separates_certification_body_from_holder(
        doc_type, title, english_title, system_name):
    text = ("华信技术检验有限公司\nVOUCHING TECHNICAL INSPECTION LTD\n"
            f"{title}\n{english_title}\n"
            f"我公司认定下列组织的{system_name}\n"
            "辽宁昊特电器有限公司\n统一社会信用代码91210804MA7HDNRN9N\n"
            "生效日期：2026-01-11 终止日期：2029-01-10")
    result = tp.extract(doc_type, text, {"full_name": "辽宁昊特电器有限公司"})
    assert result["fields"]["获证组织"] == "辽宁昊特电器有限公司"
    assert result["fields"]["获证组织_来源"] == "我公司认定下列组织"
    assert result["fields"]["认证机构"] == "华信技术检验有限公司"
    assert result["checks"]["持有人一致"] is True
    assert not any("持有人" in issue and "不一致" in issue for issue in result["issues"])


def test_iso45001_split_title_keeps_header_body_out_of_holder():
    text = (
        "合成联合认证中心\n"
        "职业健康安全管理体系\n"
        "认证证书\n"
        "注册号：06525S01483R0M\n"
        "合成勘察设计有限公司\n"
        "统一社会信用代码：91110000SYNTHETIC\n"
        "GB/T45001-2020/ISO45001:2018\n"
        "证书有效期：2025年11月28日至2028年11月27日"
    )

    result = tp.extract(
        "iso45001", text, {"full_name": "合成勘察设计有限公司"})

    assert result["fields"]["获证组织"] == "合成勘察设计有限公司"
    assert result["fields"]["获证组织_来源"] == "证书版面推断（无标签）"
    assert result["fields"]["认证机构"] == "合成联合认证中心"
    assert result["checks"]["持有人一致"] is True


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


def test_e2_recognises_freight_agency_contract_in_performance_upload_field():
    material = {
        "fileName": "委托代理合同-鲁商-华畅  双签.pdf",
        "typeName": "合作业绩证明文件",
        "desc": "",
        "types": [],
    }
    rules, _ = aa.determine_supplier_rules({"sup_type": "承运商"}, aa.load_rules())
    checklist, _, missing, _ = aa.build_checklist(
        {}, [r for r in rules if r["id"] == "E2"], [material])
    assert not missing
    assert checklist[0]["status"] == "manual"
    assert checklist[0]["files"] == [material["fileName"]]
    assert "已提交疑似过往业绩证明" in checklist[0]["detail"]
    html = gen_stage2_report.render_supplier(
        "1", {"decision": "manual", "checklist": checklist}, {}, {})
    assert "已提交（仅文件名核查）" in html
    assert "材料未上传" not in html


def test_e2_performance_field_does_not_accept_unrelated_misplaced_file():
    material = {
        "fileName": "售后服务承诺书.pdf",
        "typeName": "合作业绩证明文件",
        "types": [],
    }
    rules, _ = aa.determine_supplier_rules({"sup_type": "承运商"}, aa.load_rules())
    checklist, _, missing, _ = aa.build_checklist(
        {}, [r for r in rules if r["id"] == "E2"], [material])
    assert missing
    assert checklist[0]["status"] == "fail"
    assert checklist[0]["files"] == []


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


def test_tax_level_proof_in_other_files_satisfies_a03_presence():
    material = {
        "fileName": "纳税等级证明.pdf",
        "fileinfoTypeName": "其他文件",
        "fileDesc": "",
    }
    classified = aa.classify_uploaded_materials([material])
    types = classified["details"][0][3]
    assert "tax_credit" in types
    assert "tax_cert" not in types

    supplier = {"tax_credit_rating_ok": "tax_credit" in classified["materials"]}
    applicable, _ = aa.determine_supplier_rules({"sup_type": "经销商"}, aa.load_rules())
    rules = [rule for rule in applicable if rule["id"] == "A03"]
    checklist, _, missing, _ = aa.build_checklist(supplier, rules, [material])
    assert not missing
    assert checklist[0]["status"] == "pending"
    assert "材料已上传" in checklist[0]["detail"]


@pytest.mark.parametrize("filename", ["AAA级信用等级证书.pdf", "企业信用等级证明.pdf"])
def test_general_credit_rating_in_other_files_is_not_tax_credit(filename):
    result = aa.classify_uploaded_materials([
        {"fileName": filename, "fileinfoTypeName": "其他文件", "fileDesc": ""},
    ])
    assert "tax_credit" not in result["materials"]


@pytest.mark.parametrize("filename,position,expected", [
    ("衡水安达机械设备有限公司－质量管理体系认证证书-带CNAS标.png",
     "质量管理体系认证证书", "iso9001"),
    ("衡水安达机械设备有限公司－环境管理体系认证证书-带CNAS标.png",
     "环境管理体系认证证书图片", "iso14001"),
    ("衡水安达机械设备有限公司－职业健康安全管理体系认证证书-带CNAS标.jpg",
     "健康安全管理体系认证证书图片", "iso45001"),
])
def test_iso_cnas_mark_is_not_misclassified_as_inspection_certificate(
        filename, position, expected):
    result = aa.classify_uploaded_materials(
        [{"fileName": filename, "fileinfoTypeName": position}])
    types = result["details"][0][3]
    assert expected in types
    assert "inspection_cert" not in types


def test_iso_cnas_certificates_render_their_shared_expiry_date():
    supplier = {"full_name": "衡水安达机械设备有限公司"}
    definitions = [
        ("C1_02", "iso9001", "质量管理体系认证证书"),
        ("C1_03", "iso14001", "环境管理体系认证证书"),
        ("C1_04", "iso45001", "职业健康安全管理体系认证证书"),
    ]
    textin, checklist, materials = {}, [], []
    for rule_id, doc_type, title in definitions:
        textin[doc_type] = tp.extract(
            doc_type,
            f"{title}\n兹证明\n衡水安达机械设备有限公司\n"
            "初次获证日期：2025年07月28日\n签发日期：2025年07月28日\n"
            "有效日期：2028年07月27日",
            supplier,
        )
        assert textin[doc_type]["fields"]["有效期至"] == "2028-07-27"
        assert textin[doc_type]["checks"]["在有效期内"] is True
        checklist.append({"id": rule_id, "name": title, "status": "pass", "detail": "OCR核验通过"})
        materials.append({"fileName": f"{title}-带CNAS标.png", "types": [doc_type]})

    html = gen_stage2_report.render_supplier(
        "1", {"decision": "recommend", "checklist": checklist},
        {"1": textin}, {"1": {"supplier": supplier, "materials_detail": materials}})
    assert html.count("有效期至 2028-07-27，在有效期内") == 3
    assert "未识别到有效期" not in html


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
    supplier = {"sup_type": labels, "busi_scope": ""}
    assert aa.is_special_category(supplier)[0] is False
    rules, _ = aa.determine_supplier_rules(supplier, aa.load_rules())
    ids = {r["id"] for r in rules}
    assert expected in ids
    assert "A01" in ids
    if "服务商" in labels and "代理商" not in labels:
        assert "D1_03" not in ids
    assert ("E1" in ids) is aa._pure_carrier(supplier)


def test_service_supplier_scope_supplements_trader_rules_without_dropping_service():
    supplier = {
        "sup_type": "服务商",
        "busi_scope": ("工程设计、工程咨询、项目管理；设备设计、设备采购、设备成套、"
                       "系统集成服务；设备和材料进出口；销售机械电器设备"),
    }
    rules, type_desc = aa.determine_supplier_rules(supplier, aa.load_rules())
    ids = {rule["id"] for rule in rules}
    assert "D1_03" in ids
    assert "经销商" in type_desc
    assert "服务商" in type_desc
    assert "经营范围补充识别" in type_desc
    inferred = gen_stage2_report._infer_supplier_type(supplier, type_desc)
    assert "贸易商/经销商" in inferred
    assert "服务商" in inferred


def test_manufacturer_scope_with_sales_deduplicates_iso_rules():
    supplier = {
        "sup_type": "其他",
        "registered_capital": 800,
        "busi_scope": "机械设备生产、加工、销售；货物进出口",
    }

    rules, type_desc = aa.determine_supplier_rules(supplier, aa.load_rules())
    ids = [rule["id"] for rule in rules]

    assert "C1_01" in ids
    assert {"C1_02", "C1_03", "C1_04"}.issubset(ids)
    assert not {"D1_03", "D1_04", "D1_05"}.intersection(ids)
    assert "D1_06" in ids
    assert ids.count("C1_01") == 1
    assert "厂家" in type_desc


def test_longi_manufacturing_and_sales_scope_has_only_one_iso_rule_set():
    supplier = {
        "name": "合成光伏科技有限公司",
        "sup_type": "其他服务商",
        "busi_scope": "光伏设备及元器件制造；电池制造；电子产品销售；货物进出口",
    }

    rules, type_desc = aa.determine_supplier_rules(supplier, aa.load_rules())
    ids = [rule["id"] for rule in rules]

    assert {"C1_02", "C1_03", "C1_04"}.issubset(ids)
    assert not {"D1_03", "D1_04", "D1_05"}.intersection(ids)
    assert len(ids) == len(set(ids))
    assert "厂家" in type_desc


def test_legacy_manufacturer_flag_keeps_capital_rule_without_type_text():
    rules, type_desc = aa.determine_supplier_rules(
        {"is_manufacturer": True, "registered_capital": 500}, aa.load_rules())

    assert "C1_01" in {rule["id"] for rule in rules}
    assert "厂家" in type_desc


def test_pure_trader_does_not_gain_manufacturer_capital_rule():
    rules, _ = aa.determine_supplier_rules({
        "sup_type": "经销商",
        "busi_scope": "机械设备销售、批发、进出口",
    }, aa.load_rules())

    assert "C1_01" not in {rule["id"] for rule in rules}


def test_capital_rule_and_report_show_explicit_500_wan_threshold():
    rule = next(rule for rule in aa.load_rules()["part_2_by_type"]["domestic_manufacturer"]
                if rule["id"] == "C1_01")
    supplier = {"registered_capital": 500}
    checklist, failed, _, _ = aa.build_checklist(supplier, [rule])

    assert not failed
    assert "符合不小于500万元要求" in checklist[0]["detail"]
    html = gen_stage2_report.render_supplier(
        "1", {"decision": "recommend", "checklist": checklist}, {},
        {"1": {"supplier": supplier, "materials_detail": []}})
    assert "注册资本不小于500万元" in html
    assert "注册资本500万元，符合不小于500万元要求" in html


@pytest.mark.parametrize("label", ["服务商", "代理商", "其他", ""])
def test_freight_forwarder_scope_overrides_generic_label_to_carrier(label):
    supplier = {
        "sup_type": label,
        "busi_scope": "国际货运代理，仓储服务；无船承运。",
    }
    rules, type_desc = aa.determine_supplier_rules(supplier, aa.load_rules())
    ids = {rule["id"] for rule in rules}
    assert "运输/承运商（经营范围识别）" in type_desc
    assert {"A01", "E1", "E2"}.issubset(ids)
    assert "A07" not in ids
    assert "D1_03" not in ids


@pytest.mark.parametrize("scope", [
    "国际货物运输代理；国内集装箱货物运输代理",
    "道路货物运输；普通货运",
    "网络货运；仓储服务",
])
def test_common_freight_scope_phrases_are_classified_as_carrier(scope):
    rules, type_desc = aa.determine_supplier_rules(
        {"sup_type": "服务商", "busi_scope": scope}, aa.load_rules())
    ids = {rule["id"] for rule in rules}
    assert "运输/承运商" in type_desc
    assert {"E1", "E2"}.issubset(ids)


@pytest.mark.parametrize("name,scope", [
    ("广州港物流有限公司",
     "国内集装箱货物运输代理；海上国际货物运输代理；国内贸易代理；食品销售；无船承运业务"),
    ("上海邑航国际物流有限公司",
     "海上、航空、公路国际货物运输代理；道路货物运输代理；从事货物及技术的进出口业务"),
])
def test_logistics_service_label_uses_carrier_rules_despite_incidental_trade(name, scope):
    supplier = {"name": name, "sup_type": "物流服务商", "busi_scope": scope}
    rules, type_desc = aa.determine_supplier_rules(supplier, aa.load_rules())
    ids = {rule["id"] for rule in rules}

    assert aa._pure_carrier(supplier) is True
    assert "运输/承运商" in type_desc
    assert {"A01", "E1", "E2"}.issubset(ids)
    assert "A07" not in ids
    assert "D1_03" not in ids
    assert gen_stage2_report._infer_supplier_type(supplier, type_desc) == "运输/承运商"


def test_logistics_and_warehouse_labels_are_carrier_not_dealer_service():
    supplier = {
        "name": "江苏亚东朗升国际物流有限公司无锡分公司",
        "sup_type": "仓储服务商、物流服务商",
        "busi_scope": (
            "承办海运、陆运、空运国际运输代理业务，包括：揽货、托运、订舱、"
            "仓储、中转、集装箱拼装拆箱、结算运杂费、报验及运输咨询业务。"
            "一般项目：无船承运业务"
        ),
    }
    rules, type_desc = aa.determine_supplier_rules(supplier, aa.load_rules())
    ids = {rule["id"] for rule in rules}

    assert aa._pure_carrier(supplier) is True
    assert aa._scope_roles(supplier["busi_scope"]) == (True, False)
    assert "运输/承运商" in type_desc
    assert {"A01", "E1", "E2"}.issubset(ids)
    assert "A07" not in ids
    assert "D1_03" not in ids
    assert gen_stage2_report._infer_supplier_type(supplier, type_desc) == "运输/承运商"


def test_warehouse_label_alone_does_not_trigger_carrier_rules():
    supplier = {"sup_type": "仓储服务商", "busi_scope": "仓储服务；装卸搬运"}
    rules, type_desc = aa.determine_supplier_rules(supplier, aa.load_rules())
    ids = {rule["id"] for rule in rules}

    assert aa._pure_carrier(supplier) is False
    assert "运输/承运商" not in type_desc
    assert "E1" not in ids


@pytest.mark.parametrize("label,scope", [
    ("服务商", "招标代理；软件开发；技术服务"),
    ("代理商", "产品销售代理；货物进出口"),
    ("服务商", "工程设计；设备采购；销售机械设备"),
])
def test_non_freight_agency_and_trade_are_not_classified_as_carrier(label, scope):
    rules, type_desc = aa.determine_supplier_rules(
        {"sup_type": label, "busi_scope": scope}, aa.load_rules())
    ids = {rule["id"] for rule in rules}
    assert "运输/承运商" not in type_desc
    assert "E1" not in ids
    assert "E2" not in ids


def test_carrier_scope_does_not_override_explicit_non_carrier_or_combined_label():
    for label in ("经销商", "经销商/承运商", "服务商/代理商/承运商", "物流服务商/经销商"):
        rules, type_desc = aa.determine_supplier_rules(
            {"sup_type": label, "busi_scope": "国际货运代理；无船承运"}, aa.load_rules())
        ids = {rule["id"] for rule in rules}
        assert "运输/承运商" not in type_desc
        assert "E1" not in ids
        assert "D1_03" in ids


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
    materials = [{"fileName": "道路运输经营许可证.pdf", "types": ["transport_license"]}]
    checklist, _, missing, _ = aa.build_checklist(
        {}, [r for r in rules if r["id"] == "E1"], materials)
    assert not missing
    assert checklist[0]["status"] == "manual"
    result = tp.extract("transport_license", text)
    checklist, _ = aa.enhance_checklist_with_textin(checklist, {}, {"transport_license": result})
    assert checklist[0]["status"] == status


def test_e1_missing_material_is_fail_and_report_says_not_uploaded():
    supplier = {"sup_type": "仓储服务商、物流服务商"}
    rules, type_desc = aa.determine_supplier_rules(supplier, aa.load_rules())
    checklist, _, missing, _ = aa.build_checklist(
        supplier, [r for r in rules if r["id"] == "E1"], [])

    assert "运输/承运商" in type_desc
    assert checklist == [{
        "id": "E1", "name": "运输承运资质", "status": "fail",
        "detail": "缺少道路运输许可证或无船承运证明", "files": [],
    }]
    assert [rule["id"] for rule in missing] == ["E1"]

    stage2 = {"name": "合成物流公司", "decision": "reject", "checklist": checklist}
    cache = {"1": {"supplier": supplier, "materials_detail": []}}
    rendered = gen_stage2_report.render_supplier("1", stage2, {}, cache)
    assert "材料未上传" in rendered
    assert "上传异常" not in rendered
    assert "缺少道路运输许可证或无船承运证明" in gen_opinion.build_opinion(
        "1", stage2, {}, cache)


def test_e1_stale_ocr_cannot_override_missing_material():
    checklist = [{
        "id": "E1", "name": "运输承运资质", "status": "fail",
        "detail": "缺少道路运输许可证或无船承运证明", "files": [],
    }]
    result = tp.extract(
        "transport_license", "道路运输经营许可证\n有效期至：2099年06月04日")
    checklist, _ = aa.enhance_checklist_with_textin(
        checklist, {}, {"transport_license": result})
    assert checklist[0]["status"] == "fail"
    assert checklist[0]["detail"] == "缺少道路运输许可证或无船承运证明"


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


def test_scope_similarity_at_least_93_percent_is_treated_as_basically_equal():
    result = tp.extract(
        "business_license",
        "经营范围：软件开发技术服务信息咨询货物运输代理",
        {"busi_scope": "软件开发技术服务信息咨询货物运输代办"},
    )
    assert float(result["fields"]["经营范围相似度"].rstrip("%")) >= 93
    assert result["checks"]["经营范围一致"] is True
    assert "经营范围差异" not in result["fields"]


def test_scope_similarity_below_93_percent_shows_text_difference():
    result = tp.extract(
        "business_license",
        "经营范围：软件开发技术服务信息咨询货物运输代理",
        {"busi_scope": "软件开发技术服务信息咨询货物运输服务"},
    )
    assert float(result["fields"]["经营范围相似度"].rstrip("%")) < 93
    assert result["checks"]["经营范围一致"] is None
    assert "仅系统含" in result["fields"]["经营范围差异"]
    assert "仅执照OCR含" in result["fields"]["经营范围差异"]


@pytest.mark.parametrize("ocr,system", [
    ("经营范围\n说明：\n注册资本：500万元", "软件开发；技术服务"),
    ("经营范围：软件开发", ""),
    ("经营范围：软件开发", "软件开发；技术服务；道路货物运输；建筑工程施工"),
    ("经营范围：软件开发；技术服务；信息咨询；货物运输", "软件开发；技术服务；信息咨询；货物运输；货物进出口"),
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


def test_stage2_fetch_ids_only_updates_current_mode_ocr_results(tmp_path, monkeypatch):
    cache = {
        "1": {"supplier": {"name": "合成港澳台公司一", "is_hmt": True}, "todo": {}},
        "2": {"supplier": {"name": "合成港澳台公司二", "is_hmt": True}, "todo": {}},
    }
    textin_path = tmp_path / "textin_results.online.json"
    textin_path.write_text(json.dumps({"1": {}}), encoding="utf-8")
    stage2_path = tmp_path / "stage2_results.online.json"
    stage2_path.write_text(json.dumps({
        "1": {"decision": "manual"},
        "2": {"decision": "manual"},
    }), encoding="utf-8")
    monkeypatch.setenv("OCR_MODE", "online")
    monkeypatch.setattr(aa, "_load_cache", lambda: cache)
    monkeypatch.setattr(aa, "BASE_DIR", tmp_path)
    monkeypatch.setattr(aa, "QCC_FILE", tmp_path / "none.json")
    monkeypatch.setattr(aa, "STAGE2_FILE", stage2_path)
    monkeypatch.setattr(aa, "FETCH_IDS", {"1"})

    aa.run_stage2()

    results = json.loads(stage2_path.read_text(encoding="utf-8"))
    assert set(results) == {"1"}
    assert results["1"]["ocr_mode"] == "online"


def test_missing_after_sales_opinion_uses_concise_requirement():
    rule = next(
        item
        for item in aa.load_rules()["part_1_basic"]["domestic"]
        if item.get("id") == "A07"
    )
    assert rule["requirement"] == "售后服务五星认证或售后服务承诺函"

    stage2 = {
        "decision": "reject",
        "checklist": [{
            "id": "A07",
            "name": "售后服务",
            "status": "fail",
            "detail": "缺少售后服务五星认证或厂家出具的售后服务证明函；贸易商/经销商也需提交",
        }],
    }
    cache = {"1": {"supplier": {}, "materials_detail": []}}

    opinion = gen_opinion.build_opinion("1", stage2, {}, cache)

    assert "缺少售后服务五星认证或售后服务承诺函" in opinion
    assert "售后服务证明函" not in opinion
    assert "厂家出具" not in opinion
    assert "贸易商/经销商也需提交" not in opinion


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
    assert extracted["fields"]["材料类型"] == "售后服务承诺函"
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
    monkeypatch.setattr(tp, "BASE", tmp_path)
    folder = tmp_path / "files_cache" / "1"
    folder.mkdir(parents=True)
    file = folder / "synthetic.pdf"
    file.write_bytes(b"fixture")
    old = time.time() - 10 * 86400
    os.utime(file, (old, old))
    os.utime(folder, (old, old))
    tp.clean_files_cache(days=7, do_delete=True)
    assert not folder.exists()


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


def test_post_rechecks_single_code_111_before_expiring_session(monkeypatch):
    class Response:
        def __init__(self, payload):
            self.payload = payload
            self.text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    post = Mock(side_effect=[Response({"code": 111}), Response({"code": 0, "data": {}})])
    monkeypatch.setattr(aa._SESSION, "post", post)
    monkeypatch.setattr(aa.time, "sleep", Mock())

    result = aa._post("https://asca.iccec.cn/apis/asca/todo/home/list", {}, {},
                      retry_waits=[], auth_retry_waits=[0])

    assert result["code"] == 0
    assert post.call_count == 2


def test_pending_todos_retries_without_stale_app_token(monkeypatch):
    monkeypatch.setattr(aa, "APP_TOKEN", "stale-synthetic")
    monkeypatch.setenv("SCPMA_COOKIE", "session=synthetic")
    monkeypatch.setenv("SCPMA_AUTH_TOKEN", "Bearer synthetic")
    post = Mock(side_effect=[
        aa.SessionExpiredError("synthetic conflict"),
        {"code": 0, "data": {"rows": [], "recordsTotal": 0}},
    ])
    monkeypatch.setattr(aa, "_post", post)

    result = aa.fetch_pending_todos()

    assert result["code"] == 0
    assert post.call_count == 2
    first_headers = post.call_args_list[0].args[1]
    fallback_headers = post.call_args_list[1].args[1]
    assert first_headers["APP_TOKEN"] == "stale-synthetic"
    assert first_headers["Authorization"] == "Bearer synthetic"
    assert "APP_TOKEN" not in fallback_headers
    assert fallback_headers["Authorization"] == "Bearer synthetic"


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


def test_cooperation_application_reject_uses_change_request_prefix():
    stage2 = {
        "decision": "reject",
        "checklist": [{
            "id": "A03",
            "name": "纳税信用等级",
            "status": "fail",
            "detail": "缺少纳税信用等级证明",
        }],
    }
    cache = {"1": {
        "todo": {"billType": "P0704", "billName": "合作意向申请审批"},
        "supplier": {},
    }}

    opinion = gen_opinion.build_opinion("1", stage2, {}, cache)

    assert opinion.startswith(
        "退回。请提交变更申请至中国港湾工程有限责任公司（中港总部），并补充："
        "1. 缺少纳税信用等级证明")


def test_non_cooperation_reject_keeps_standard_prefix():
    stage2 = {
        "decision": "reject",
        "checklist": [{
            "id": "A03",
            "name": "纳税信用等级",
            "status": "fail",
            "detail": "缺少纳税信用等级证明",
        }],
    }
    cache = {"1": {
        "todo": {"billType": "P0702", "billName": "信息变更审批"},
        "supplier": {},
    }}

    opinion = gen_opinion.build_opinion("1", stage2, {}, cache)

    assert opinion.startswith("退回。1. 缺少纳税信用等级证明")
    assert "请提交变更申请至中国港湾工程有限责任公司" not in opinion


def test_ocr_missing_fields_does_not_claim_submitted_business_license_is_missing():
    stage2 = {"name": "合成钢铁供应链有限公司", "decision": "reject", "checklist": [
        {"id": "A01", "name": "法律主体资格", "status": "fail",
         "detail": "OCR核验发现问题：营业执照识别不完整，缺少字段：名称、法定代表人"},
        {"id": "A03", "name": "纳税信用等级", "status": "fail",
         "detail": "缺少纳税信用等级证明（须为正规文件，C级及以上）"},
    ]}
    cache = {"1": {
        "supplier": {"has_business_license": True},
        "materials_detail": [
            {"fileName": "合成营业执照.png", "types": ["business_license"]},
        ],
    }}

    opinion = gen_opinion.build_opinion("1", stage2, {}, cache)

    assert "缺少营业执照副本" not in opinion
    assert "缺少纳税信用等级证明" in opinion
    assert "[A01]法律主体资格：OCR核验发现问题" in opinion


def test_demo_three_decisions():
    from demo import cases
    results = cases()
    assert {r["decision"] for r in results.values()} == {"recommend", "reject", "manual"}
    for tid, result in results.items():
        assert gen_stage2_report.render_supplier(tid, result, {}, {})
