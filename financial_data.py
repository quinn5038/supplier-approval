"""Normalize public financial evidence without treating it as an audited report."""
import re
from datetime import date


def financial_report_presence(supplier, materials_detail=None):
    """Return whether a financial report was uploaded; ``None`` means unknown."""
    if materials_detail is not None:
        for item in materials_detail:
            if isinstance(item, dict):
                name = item.get("fileName") or ""
                position = item.get("typeName") or ""
                types = item.get("types") or []
            else:
                name, position, types = item[0], item[1], item[3]
            if not name:
                continue
            if "financial_report" in types or re.search(
                    r"财务报表|财务报告|财报|审[计记]报告|资产负债表|利润表|现金流量表",
                    str(name) + " " + str(position)):
                return True
        return False
    value = supplier.get("has_financial_report")
    return value if isinstance(value, bool) else None


def assess_financial(data, submitted=None):
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
    if submitted is False:
        detail = ("缺少经审计的上年度财报；请补交后转人工核查年度、三表完整性及审计意见；"
                  "公开财务数据不能替代应上传的经审计财报。")
    elif submitted is None:
        detail = "财报上传状态未确认；" + detail
    return {"source": "QCC", "periods": periods, "required_year": required,
            "confidence": "manual_review", "submitted": submitted, "detail": detail}
