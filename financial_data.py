"""Normalize public financial evidence without treating it as an audited report."""
import re
from datetime import date

_FINANCIAL_TITLE_PATTERN = re.compile(
    r"财务报表|财务报告|财报|审[计记]报告|资产负债表|利润表|现金流量表")
_TITLE_YEAR_PATTERN = re.compile(r"(?<!\d)(20\d{2})(?!\d)")


def _financial_material_metadata(item):
    """Return ``(is_financial, title_text)`` using upload metadata only."""
    if isinstance(item, dict):
        name = item.get("fileName") or ""
        position = item.get("typeName") or item.get("fileinfoTypeName") or ""
        types = item.get("types") or []
        if isinstance(types, str):
            types = [types]
        title_text = " ".join(str(item.get(key) or "") for key in (
            "fileName", "title", "fileTitle", "fileDesc", "desc", "fileSumm", "remark"))
    else:
        name = item[0] if len(item) > 0 else ""
        position = item[1] if len(item) > 1 else ""
        types = item[3] if len(item) > 3 else []
        title_text = str(name or "")
    is_financial = ("financial_report" in types or bool(
        _FINANCIAL_TITLE_PATTERN.search(str(name) + " " + str(position))))
    return is_financial, title_text


def financial_report_title_check(supplier, materials_detail=None, *, current_year=None):
    """Check whether an uploaded financial-report title names the prior year.

    ``matched`` means at least one financial-report title explicitly contains the
    required prior year. ``mismatch`` means report material exists and its title
    contains year numbers, but none is the required year. A title without any
    four-digit year remains ``unknown`` and must be reviewed manually.
    """
    required_year = int(current_year or date.today().year) - 1
    if materials_detail is None:
        submitted = financial_report_presence(supplier)
        status = "missing" if submitted is False else "unknown"
        return {"status": status,
                "submitted": submitted, "required_year": required_year, "title_years": []}

    submitted = False
    title_years = set()
    for item in materials_detail:
        is_financial, title_text = _financial_material_metadata(item)
        if not is_financial:
            continue
        submitted = True
        title_years.update(int(value) for value in _TITLE_YEAR_PATTERN.findall(title_text))

    if not submitted:
        status = "missing"
    elif required_year in title_years:
        status = "matched"
    elif title_years:
        status = "mismatch"
    else:
        status = "unknown"
    return {"status": status, "submitted": submitted, "required_year": required_year,
            "title_years": sorted(title_years)}


def financial_report_presence(supplier, materials_detail=None):
    """Return whether a financial report was uploaded; ``None`` means unknown."""
    if materials_detail is not None:
        for item in materials_detail:
            is_financial, _ = _financial_material_metadata(item)
            if is_financial:
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
