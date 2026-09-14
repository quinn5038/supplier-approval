"""Synthetic offline examples. No platform accounts, files or network calls."""
from auto_approve import build_checklist, generate_opinion_v4


def cases():
    examples = {}
    rules = [{"id": "DEMO_CAPITAL", "name": "演示注册资本", "check_type": "auto",
              "rule": "supplier.get('registered_capital', 0) >= 500"},
             {"id": "DEMO_LICENSE", "name": "演示营业执照", "check_type": "material",
              "rule": "supplier.get('has_business_license', False)",
              "requirement": "演示营业执照", "verify": "人工核验演示信息"}]
    for tid, capital, license_present in [("demo-pass", 600, True), ("demo-reject", 100, False), ("demo-manual", 600, True)]:
        supplier = {"name": "虚构供应商 " + tid, "registered_capital": capital,
                    "has_business_license": license_present}
        checklist, failed, missing, verify = build_checklist(supplier, rules)
        if tid == "demo-pass":
            for check in checklist:
                check.update(status="pass", detail="合成测试证据通过（不代表真实资质）")
            verify = []
        opinion, decision = generate_opinion_v4(supplier, failed, missing, verify)
        examples[tid] = {"todoId": tid, "name": supplier["name"], "billName": "离线演示",
                         "type_desc": "合成样例，仅演示两项规则", "decision": decision,
                         "opinion": opinion, "checklist": checklist, "qcc_issues": [], "textin_issues": []}
    return examples
