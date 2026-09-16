"""Fail-closed external OCR policy, shared by staging and transport."""
import hashlib
import json
import re
from pathlib import Path

# 售后认证/证明函不含身份证、财报、银行账号等受限信息，按 A07 规则允许
# TextIn 核验。文件名或元数据一旦命中 SENSITIVE_NAME，仍由下方否决。
PUBLIC_TYPES = frozenset({"business_license", "iso9001", "iso14001", "iso45001",
                          "tax_credit", "tax_cert", "production_license",
                          "after_sales_cert", "after_sales_statement"})
SENSITIVE_NAME = re.compile(r"身份证|证件|财务|财报|审[计记]|资产负债|利润表|现金流|银行|账号|保密|涉密|id.?card", re.I)


def file_types(path, cache):
    """Use all matching metadata, with a filename veto for combined attachments."""
    path = Path(path)
    bare = re.sub(r"^\d+_", "", path.name)
    entries = cache.get(path.parent.name, {}).get("materials_detail", [])
    types = set()
    for item in entries:
        if item.get("fileName") in (path.name, bare):
            value = item.get("types") or []
            types.update([value] if isinstance(value, str) else value)
            description = " ".join(str(item.get(k) or "") for k in ("fileName", "typeName", "desc"))
            if SENSITIVE_NAME.search(description):
                types.add("sensitive")
    return types


def is_public_material(path, types):
    return bool(types) and set(types) <= PUBLIC_TYPES and not SENSITIVE_NAME.search(Path(path).name)


def load_cache(base):
    path = Path(base) / "cache_v4.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def aggregate_materials(documents):
    """All attachments must pass; failures and unknowns cannot be overwritten."""
    if len(documents) == 1:
        return {**documents[0], "documents": documents}
    checks, fields, issues = {}, {}, []
    for i, document in enumerate(documents, 1):
        prefix = f"附件{i}({document.get('file', '')})"
        checks.update({f"{prefix} {k}": v for k, v in (document.get("checks") or {}).items()})
        fields.update({f"{prefix} {k}": v for k, v in (document.get("fields") or {}).items()})
        issues.extend(f"{prefix}：{v}" for v in document.get("issues", []))
        if document.get("error"):
            issues.append(f"{prefix}：识别失败，需人工核验")
        if not document.get("checks"):
            checks[f"{prefix} 核验完成"] = None
    return {"documents": documents, "checks": checks, "fields": fields,
            "issues": issues, "file": documents[0].get("file"), "policy": "all"}
