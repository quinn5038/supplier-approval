"""Fail-closed external OCR policy, shared by staging and transport."""
import hashlib
import json
import re
import unicodedata
from pathlib import Path

# 售后认证/证明函不含身份证、财报、银行账号等受限信息，按 A07 规则允许
# TextIn 核验。文件名或元数据一旦命中 SENSITIVE_NAME，仍由下方否决。
PUBLIC_TYPES = frozenset({"business_license", "iso9001", "iso14001", "iso45001",
                          "tax_credit", "tax_cert", "production_license",
                          "after_sales_cert", "after_sales_statement", "transport_license"})
SENSITIVE_NAME = re.compile(r"身份证|证件|财务|财报|审[计记]|资产负债|利润表|现金流|银行|账号|保密|涉密|id.?card", re.I)


def iso_candidate_types(filename, type_name=""):
    name = unicodedata.normalize("NFKC", str(filename or ""))
    position = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(type_name or "")))
    position = re.sub(r"(?:图片|扫描件|复印件)$", "", position)
    positions = {"质量管理体系认证证书": "iso9001", "环境管理体系认证证书": "iso14001",
                 "职业健康安全管理体系认证证书": "iso45001", "健康安全管理体系认证证书": "iso45001"}
    codes = {"iso" + code for code in ("9001", "14001", "45001") if code in name}
    if codes:
        return codes
    result = {positions[position]} if position in positions else set()
    stem = re.sub(r"^\d+_", "", Path(name).stem)
    if stem in {"环境管理", "环境管理体系", "环境管理体系证书", "环境管理体系认证证书"}:
        result.add("iso14001")
    return result


def hydrate_iso_cache(cache):
    """Upgrade legacy ISO routing hints without declaring OCR verification passed."""
    for entry in cache.values():
        found = set()
        for item in entry.get("materials_detail", []):
            value = item.get("types") or []
            types = set([value] if isinstance(value, str) else value)
            types.update(iso_candidate_types(item.get("fileName"), item.get("typeName")))
            item["types"] = sorted(types)
            found.update(types & {"iso9001", "iso14001", "iso45001"})
        supplier = entry.get("supplier", {})
        supplier["classified_materials"] = sorted(set(supplier.get("classified_materials") or []) | found)
        certs = set(supplier.get("certifications") or []) | {v.upper() for v in found}
        supplier["certifications"] = sorted(certs)
        entry["certifications"] = sorted(set(entry.get("certifications") or []) | certs)
    return cache


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
            types.update(iso_candidate_types(item.get("fileName"), item.get("typeName")))
            description = " ".join(str(item.get(k) or "") for k in ("fileName", "typeName", "desc"))
            # Existing caches predate E1; recognise certificate routing hints without
            # dropping other types or the sensitive veto. OCR still validates content.
            if re.search(r"道路运输|无船承运", description):
                types.add("transport_license")
            if SENSITIVE_NAME.search(description):
                types.add("sensitive")
    return types


def is_public_material(path, types):
    return bool(types) and set(types) <= PUBLIC_TYPES and not SENSITIVE_NAME.search(Path(path).name)


def load_cache(base):
    path = Path(base) / "cache_v4.json"
    return hydrate_iso_cache(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else {}


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
