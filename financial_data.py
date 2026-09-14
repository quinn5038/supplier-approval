"""Normalize public financial evidence without treating it as an audited report."""
from datetime import date


def assess_financial(data):
    records = []

    def visit(value):
        if isinstance(value, dict):
            if any(k in value for k in ("报告期", "报告年度", "ReportDate")):
                records.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(data)
    periods = sorted({str(r.get("报告期") or r.get("报告年度") or r.get("ReportDate")) for r in records})
    required = str(date.today().year - 1)
    detail = "公开财务数据不能证明上传财报已经审计；转人工核验上年度经审计财报。"
    if not records:
        detail = "未取得带报告期的公开财务数据；" + detail
    elif not any(p.startswith(required) for p in periods):
        detail = "公开数据非上年度，报告期：" + "、".join(periods) + "；" + detail
    else:
        detail = "已取得上年度公开财务数据，仍需核对三表完整性及审计意见；" + detail
    return {"source": "QCC", "periods": periods, "required_year": required,
            "confidence": "manual_review", "detail": detail}
