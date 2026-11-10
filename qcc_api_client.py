"""中交企业信息接口客户端。

当前生产流程接入税务评级、失信被执行人、严重违法、经营异常、建筑资质不良行为
和资质证书查询。资质证书结果用于 C1_02-C1_04 的证书编号、有效期严格交叉核验。
关联关系查询客户端已预留，但 A14 当前未启用，也不会由审批流水线调用。
原始响应只保存在本机、被 ``.gitignore`` 排除的 SQLite 缓存中；审批结果
仅保留类别状态和数量，不落案件编号、证件号码、人员姓名等明细。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

log = logging.getLogger(__name__)
BASE_DIR = Path(__file__).parent


def _load_local_env() -> None:
    """允许独立运行本模块；不覆盖进程显式设置的环境变量。"""
    path = BASE_DIR / ".env"
    if not path.exists():
        return
    explicit_env_keys = set(os.environ)
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in explicit_env_keys:
            os.environ[key] = value


_load_local_env()

QCC_APP_KEY = os.getenv("QCC_APP_KEY", "")
QCC_SECRET = os.getenv("QCC_SECRET_KEY", "")
QCC_BASE_URL = os.getenv("QCC_BASE_URL", "https://ai-dataapi.ccccltd.cn").rstrip("/")
QCC_TIMEOUT = float(os.getenv("QCC_TIMEOUT", "15"))
QCC_CACHE_TTL = int(os.getenv("QCC_CACHE_TTL", "86400"))
QCC_REQUEST_INTERVAL = float(os.getenv("QCC_REQUEST_INTERVAL", "0.5"))
QCC_MAX_PAGES = max(1, int(os.getenv("QCC_MAX_PAGES", "100")))

_CACHE_DB = BASE_DIR / "qcc_api_cache.sqlite3"
_CALL_COUNT_FILE = BASE_DIR / "qcc_call_count.json"
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0

SUCCESS_STATUS = "200"
NO_RESULT_STATUS = "200100001"

A10_CATEGORY_REGISTRY = {
    "dishonest": {
        "label": "失信被执行人",
        "path": "/webapi/jr/dishonest/2.0",
    },
    "serious_illegal": {
        "label": "严重违法",
        "path": "/webapi/mr/illegalinfo/2.0",
    },
    "operation_abnormal": {
        "label": "经营异常",
        "path": "/webapi/mr/baseinfo/normal",
    },
    "bad_conduct": {
        "label": "建筑资质不良行为",
        "path": "/webapi/bq/badConduct",
    },
}

TAX_CREDIT_PATH = "/webapi/tax/information/taxCredit"
QUALIFICATION_CERTIFICATE_PATH = "/webapi/m/certificate/2.0"
RELATIONSHIP_PATH = "/webapi/relationship/warning/character"
CCCC_GROUP_NAME = "中国交通建设集团有限公司"
CCCC_GROUP_CREDIT_CODE = "91110000710933809D"
RELATIONSHIP_TYPES = (
    "LEGAL", "EMPLOY", "INVEST", "AC", "HISLEGAL", "HISEMPLOY",
    "HISINVEST", "BRANCH",
)


def _sign(timestamp: str) -> str:
    raw = f"{QCC_APP_KEY}{timestamp}{QCC_SECRET}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def _headers() -> dict[str, str]:
    timestamp = str(int(time.time()))
    return {
        "AppId": QCC_APP_KEY,
        "Timespan": timestamp,
        "Token": _sign(timestamp),
        "Content-Type": "application/json",
        # 生产网关会拒绝 requests 默认 UA；这两个头已通过获授权的低频连通测试。
        "Accept": "application/json,text/plain,*/*",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/130.0.0.0 Safari/537.36"),
    }


def _is_configured() -> bool:
    return bool(QCC_APP_KEY and QCC_SECRET)


def _cache_get(identity: str, key: str) -> dict | None:
    from state_store import get_state

    entry = get_state(_CACHE_DB, "qcc").get(f"{identity}/{key}")
    if not isinstance(entry, dict):
        return None
    if time.time() - float(entry.get("at", 0)) > QCC_CACHE_TTL:
        return None
    value = entry.get("value")
    return value if isinstance(value, dict) else None


def _cache_set(identity: str, key: str, value: dict) -> None:
    from state_store import put_state

    put_state(_CACHE_DB, "qcc", f"{identity}/{key}", {"at": time.time(), "value": value})


def _increment_call_count(path: str) -> None:
    today = time.strftime("%Y-%m-%d")
    try:
        data = json.loads(_CALL_COUNT_FILE.read_text(encoding="utf-8")) \
            if _CALL_COUNT_FILE.exists() else {}
    except (OSError, ValueError, TypeError):
        data = {}
    row = data.setdefault(today, {"total": 0, "by_api": {}})
    row["total"] += 1
    row["by_api"][path] = row["by_api"].get(path, 0) + 1
    try:
        _CALL_COUNT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("[企业信息] 调用计数写入失败：%s", exc)


def _throttled_post(path: str, body: dict) -> dict:
    """串行、低频请求；仅对超时/连接错误/5xx重试，最多两次。"""
    global _LAST_REQUEST_AT
    if requests is None:
        return {"_client_error": "requests_not_installed"}

    url = QCC_BASE_URL + path
    for attempt in range(3):
        try:
            with _REQUEST_LOCK:
                delay = QCC_REQUEST_INTERVAL - (time.monotonic() - _LAST_REQUEST_AT)
                if delay > 0:
                    time.sleep(delay)
                response = requests.post(url, headers=_headers(), json=body, timeout=QCC_TIMEOUT)
                _LAST_REQUEST_AT = time.monotonic()
                _increment_call_count(path)
            if response.status_code >= 500 and attempt < 2:
                continue
            if not response.ok:
                # 不记录响应正文，避免网关回显请求或敏感信息进入日志。
                log.warning("[企业信息] %s HTTP %s", path, response.status_code)
                return {"_client_error": f"http_{response.status_code}"}
            payload = response.json()
            return payload if isinstance(payload, dict) else {"_client_error": "invalid_json_shape"}
        except Exception as exc:
            retryable = requests is not None and isinstance(
                exc, (requests.Timeout, requests.ConnectionError))
            if retryable and attempt < 2:
                continue
            log.warning("[企业信息] %s 调用失败：%s", path, type(exc).__name__)
            return {"_client_error": type(exc).__name__}
    return {"_client_error": "retry_exhausted"}


def _throttled_get(path: str, params: dict) -> dict:
    """GET 版串行低频请求，重试和日志策略与 POST 一致。"""
    global _LAST_REQUEST_AT
    if requests is None:
        return {"_client_error": "requests_not_installed"}

    url = QCC_BASE_URL + path
    for attempt in range(3):
        try:
            with _REQUEST_LOCK:
                delay = QCC_REQUEST_INTERVAL - (time.monotonic() - _LAST_REQUEST_AT)
                if delay > 0:
                    time.sleep(delay)
                response = requests.get(
                    url, headers=_headers(), params=params, timeout=QCC_TIMEOUT)
                _LAST_REQUEST_AT = time.monotonic()
                _increment_call_count(path)
            if response.status_code >= 500 and attempt < 2:
                continue
            if not response.ok:
                log.warning("[企业信息] %s HTTP %s", path, response.status_code)
                return {"_client_error": f"http_{response.status_code}"}
            payload = response.json()
            return payload if isinstance(payload, dict) else {"_client_error": "invalid_json_shape"}
        except Exception as exc:
            retryable = requests is not None and isinstance(
                exc, (requests.Timeout, requests.ConnectionError))
            if retryable and attempt < 2:
                continue
            log.warning("[企业信息] %s 调用失败：%s", path, type(exc).__name__)
            return {"_client_error": type(exc).__name__}
    return {"_client_error": "retry_exhausted"}


def _post_page(path: str, body: dict, identity: str, cache_key: str) -> dict:
    cached = _cache_get(identity, cache_key)
    if cached is not None:
        return cached
    response = _throttled_post(path, body)
    status = str(response.get("status", ""))
    # 成功和“有效请求无结果”都缓存；鉴权、配额、限流失败不缓存。
    if status in {SUCCESS_STATUS, NO_RESULT_STATUS}:
        _cache_set(identity, cache_key, response)
    elif not response.get("_client_error"):
        log.warning("[企业信息] %s 业务状态=%s", path, status or "missing")
    return response


def _fetch_paged(path: str, cache_scope: str, keyword: str, identity: str,
                 query_variant: str = "primary") -> dict:
    if not _is_configured():
        return {"status": "not_configured", "items": [], "total": None, "complete": False}
    if not keyword:
        return {"status": "invalid_input", "items": [], "total": None, "complete": False}

    items: list[dict] = []
    total: int | None = None
    for page in range(1, QCC_MAX_PAGES + 1):
        response = _post_page(
            path,
            {"keyword": keyword, "pageNum": page, "pageSize": 20},
            identity,
            f"{cache_scope}:v2:{query_variant}:page:{page}",
        )
        if response.get("_client_error"):
            return {"status": "error", "items": items, "total": total,
                    "complete": False, "error": response["_client_error"]}
        business_status = str(response.get("status", ""))
        if business_status == NO_RESULT_STATUS:
            return {"status": "ok", "items": items, "total": len(items), "complete": True}
        if business_status != SUCCESS_STATUS:
            return {"status": "error", "items": items, "total": total,
                    "complete": False, "error": f"business_{business_status or 'missing'}"}
        result = response.get("result")
        # 部分生产接口用 status=200/result=null 表示查询成功但没有记录，
        # 不能把这种合法空结果误判为接口查询失败。
        if result is None:
            return {"status": "ok", "items": items, "total": len(items),
                    "complete": True}
        if isinstance(result, list):
            page_items = result
            raw_total = response.get("total", len(items) + len(page_items))
        elif isinstance(result, dict):
            page_items = result.get("items") or []
            raw_total = result.get("total", len(items) + len(page_items))
        else:
            return {"status": "error", "items": items, "total": total,
                    "complete": False, "error": "invalid_result_shape"}
        if not isinstance(page_items, list):
            return {"status": "error", "items": items, "total": total,
                    "complete": False, "error": "invalid_items_shape"}
        items.extend(item for item in page_items if isinstance(item, dict))
        try:
            total = int(raw_total)
        except (TypeError, ValueError):
            total = len(items)
        if len(items) >= total or not page_items:
            return {"status": "ok", "items": items, "total": total, "complete": True}
    return {"status": "ok", "items": items, "total": total, "complete": False,
            "error": "page_limit"}


def _fetch_category(category: str, keyword: str, identity: str,
                    query_variant: str = "primary") -> dict:
    config = A10_CATEGORY_REGISTRY[category]
    return _fetch_paged(
        config["path"], f"a10:{category}", keyword, identity, query_variant)


def _text(value: Any) -> str:
    return "".join(str(value or "").split()).lower()


def _assess_dishonest(result: dict) -> dict:
    if result.get("status") != "ok":
        return {"status": "unavailable", "complete": False, "total": result.get("total")}
    risk = resolved = unknown = 0
    for item in result.get("items", []):
        performance = _text(item.get("performance"))
        unperformed = _text(item.get("unperformPart"))
        if unperformed or "未履行" in performance or "部分履行" in performance:
            risk += 1
        elif "已履行" in performance or "履行完毕" in performance:
            resolved += 1
        else:
            unknown += 1
    status = "risk" if risk else ("unknown" if unknown or not result.get("complete") else "clear")
    return {"status": status, "complete": bool(result.get("complete")),
            "total": int(result.get("total") or 0), "risk_count": risk,
            "resolved_count": resolved, "unknown_count": unknown}


def _has_valid_removal_date(value: Any) -> bool:
    if value in (None, "", 0, "0"):
        return False
    if isinstance(value, (int, float)):
        return value > 0
    text = _text(value)
    return text not in {"", "-", "--", "无", "null", "none"}


def _assess_serious_illegal(result: dict) -> dict:
    if result.get("status") != "ok":
        return {"status": "unavailable", "complete": False, "total": result.get("total")}
    risk = removed = unknown = 0
    for item in result.get("items", []):
        if _has_valid_removal_date(item.get("removeDate")):
            removed += 1
        elif any(key in item for key in ("removeDate", "putDate", "putReason", "putDepartment")):
            risk += 1
        else:
            unknown += 1
    status = "risk" if risk else ("unknown" if unknown or not result.get("complete") else "clear")
    return {"status": status, "complete": bool(result.get("complete")),
            "total": int(result.get("total") or 0), "risk_count": risk,
            "removed_count": removed, "unknown_count": unknown}


def _parse_public_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _assess_bad_conduct(result: dict) -> dict:
    if result.get("status") != "ok":
        return {"status": "unavailable", "complete": False, "total": result.get("total")}
    current = historical = unknown = 0
    today = date.today()
    for item in result.get("items", []):
        valid_until = _parse_public_date(item.get("publishValidityPeriod"))
        if valid_until is None:
            unknown += 1
        elif valid_until >= today:
            current += 1
        else:
            historical += 1
    status = "risk" if current else (
        "unknown" if unknown or not result.get("complete") else "clear")
    return {"status": status, "complete": bool(result.get("complete")),
            "total": int(result.get("total") or 0), "risk_count": current,
            "historical_count": historical, "unknown_count": unknown}


def _fetch_with_name_fallback(category: str, name: str, credit_code: str,
                              identity: str) -> dict:
    keyword = credit_code.strip() or name.strip()
    primary = _fetch_category(
        category, keyword, identity, "credit" if credit_code.strip() else "name")
    if (credit_code.strip() and name.strip() and primary.get("status") == "ok"
            and int(primary.get("total") or 0) == 0):
        fallback = _fetch_category(category, name.strip(), identity, "name")
        if fallback.get("status") == "ok":
            return fallback
    return primary


def fetch_a10_checks(name: str, credit_code: str = "") -> dict:
    """查询已授权 A10 类别，返回不含案件明细的结构化摘要。"""
    # 缓存继续按供应商名分组，以便现有“清理单家供应商缓存”同步删除。
    identity = name.strip() or credit_code.strip()
    dishonest = _assess_dishonest(
        _fetch_with_name_fallback("dishonest", name, credit_code, identity))
    serious = _assess_serious_illegal(
        _fetch_with_name_fallback("serious_illegal", name, credit_code, identity))
    abnormal = _assess_serious_illegal(
        _fetch_with_name_fallback("operation_abnormal", name, credit_code, identity))
    bad_conduct = _assess_bad_conduct(
        _fetch_with_name_fallback("bad_conduct", name, credit_code, identity))
    return {
        "version": 2,
        "queried_categories": list(A10_CATEGORY_REGISTRY),
        "pending_categories": ["被执行人", "限制消费令"],
        "categories": {
            "dishonest": dishonest,
            "serious_illegal": serious,
            "operation_abnormal": abnormal,
            "bad_conduct": bad_conduct,
        },
        "complete_for_a10": False,
    }


def fetch_tax_credit(name: str, credit_code: str = "") -> dict:
    """查询税务评级，只返回最新年度/等级摘要，不保存纳税人识别号到报告。"""
    identity = name.strip() or credit_code.strip()

    def fetch(keyword: str, variant: str) -> dict:
        return _fetch_paged(
            TAX_CREDIT_PATH, "tax_credit", keyword, identity, variant)

    keyword = credit_code.strip() or name.strip()
    raw = fetch(keyword, "credit" if credit_code.strip() else "name")
    if (credit_code.strip() and name.strip() and raw.get("status") == "ok"
            and int(raw.get("total") or 0) == 0):
        fallback = fetch(name.strip(), "name")
        if fallback.get("status") == "ok":
            raw = fallback
    if raw.get("status") != "ok":
        return {"status": "unavailable", "complete": False}

    records = []
    for item in raw.get("items", []):
        try:
            year = int(str(item.get("year", "")).strip())
        except ValueError:
            continue
        grade = str(item.get("grade", "")).strip().upper().replace("级", "")
        records.append((year, grade))
    if not records:
        return {"status": "no_data", "complete": bool(raw.get("complete")), "total": 0}
    latest_year, latest_grade = max(records, key=lambda row: row[0])
    required_year = date.today().year - 1
    if latest_year < required_year:
        status = "unknown"
    elif latest_grade == "D":
        status = "risk"
    elif latest_grade not in {"A", "B", "C"}:
        status = "unknown"
    else:
        status = "clear"
    return {"status": status, "complete": bool(raw.get("complete")),
            "latest_year": latest_year, "latest_grade": latest_grade,
            "required_year": required_year, "total": int(raw.get("total") or len(records))}


def _certificate_iso_type(item: dict) -> str | None:
    """Map a certificate record to an ISO type without scanning issuer scope text."""
    evidence = [item.get("certificateName"), item.get("certificateType")]
    for detail in item.get("detail") or []:
        if not isinstance(detail, dict):
            continue
        title = "".join(str(detail.get("title") or "").split())
        if any(marker in title for marker in ("认证标准", "认证项目", "证书名称", "证书类型")):
            evidence.append(detail.get("content"))
    text = unicodedata.normalize("NFKC", " ".join(str(v or "") for v in evidence))
    compact = re.sub(r"\s+", "", text).upper()
    if "ISO9001" in compact or "质量管理体系" in compact or "GB/T19001" in compact:
        return "iso9001"
    if "ISO14001" in compact or "环境管理体系" in compact or "GB/T24001" in compact:
        return "iso14001"
    if ("ISO45001" in compact or "职业健康安全管理体系" in compact
            or "GB/T45001" in compact):
        return "iso45001"
    return None


def fetch_qualification_certificates(name: str) -> dict:
    """Fetch public certificate summaries used for C1_02-C1_04 cross-checks."""
    identity = name.strip()
    if not _is_configured():
        return {"status": "unavailable", "complete": False, "records": []}
    if not identity:
        return {"status": "unavailable", "complete": False, "records": []}

    items: list[dict] = []
    total: int | None = None
    for page in range(1, QCC_MAX_PAGES + 1):
        response = _post_page(
            QUALIFICATION_CERTIFICATE_PATH,
            {"name": identity, "certificateName": "", "pageNum": page, "pageSize": 20},
            identity,
            f"qualification_certificates:v1:page:{page}",
        )
        if response.get("_client_error"):
            return {"status": "unavailable", "complete": False, "records": []}
        business_status = str(response.get("status", ""))
        if business_status == NO_RESULT_STATUS:
            total = len(items)
            break
        if business_status != SUCCESS_STATUS:
            return {"status": "unavailable", "complete": False, "records": []}
        result = response.get("result")
        if result is None:
            total = len(items)
            break
        if not isinstance(result, list):
            return {"status": "unavailable", "complete": False, "records": []}
        page_items = [item for item in result if isinstance(item, dict)]
        items.extend(page_items)
        try:
            total = int(response.get("total", len(items)))
        except (TypeError, ValueError):
            total = len(items)
        if len(items) >= total or not page_items:
            break
    else:
        return {"status": "unavailable", "complete": False, "records": []}

    # Only fields needed for deterministic matching leave the client. Full raw
    # responses remain in the local ignored cache and are never written to reports.
    records = []
    for item in items:
        iso_type = _certificate_iso_type(item)
        if iso_type:
            records.append({
                "iso_type": iso_type,
                "cert_no": str(item.get("certNo") or ""),
                "end_date": str(item.get("endDate") or ""),
            })
    return {"status": "ok", "complete": True, "records": records,
            "total": int(total or 0)}


def fetch_relationship_check(name: str, credit_code: str = "") -> dict:
    """查询供应商与中国交通建设集团有限公司的全部文档支持关联类型。"""
    identity = name.strip() or credit_code.strip()
    supplier_key = credit_code.strip() or name.strip()
    if not _is_configured():
        return {"status": "unavailable", "complete": False, "relation_count": None}
    if not supplier_key:
        return {"status": "unavailable", "complete": False, "relation_count": None}
    params = {
        "companyNames": f"{supplier_key},{CCCC_GROUP_CREDIT_CODE}",
        "rels": ",".join(RELATIONSHIP_TYPES),
        "delInvalid": "0",
    }
    cache_key = "relationship:cccc:v1"
    response = _cache_get(identity, cache_key)
    if response is None:
        response = _throttled_get(RELATIONSHIP_PATH, params)
        if str(response.get("status", "")) in {SUCCESS_STATUS, NO_RESULT_STATUS}:
            _cache_set(identity, cache_key, response)
    if response.get("_client_error"):
        return {"status": "unavailable", "complete": False, "relation_count": None}
    business_status = str(response.get("status", ""))
    if business_status == NO_RESULT_STATUS:
        return {"status": "clear", "complete": True, "relation_count": 0}
    if business_status != SUCCESS_STATUS:
        return {"status": "unavailable", "complete": False, "relation_count": None}
    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("correlationships", []), list):
        return {"status": "unavailable", "complete": False, "relation_count": None}
    relation_count = len([row for row in result.get("correlationships", [])
                          if isinstance(row, dict)])
    return {"status": "risk" if relation_count else "clear", "complete": True,
            "relation_count": relation_count, "target": CCCC_GROUP_NAME}


# 向后兼容旧调用方；新流程不会触发未获授权接口。
def dishonest_person(keyword: str, human_name: str = "") -> dict:
    del human_name
    return _post_page(A10_CATEGORY_REGISTRY["dishonest"]["path"],
                      {"keyword": keyword, "pageNum": 1, "pageSize": 20},
                      keyword, "legacy:dishonest:page:1")


def serious_illegal(keyword: str) -> dict:
    return _post_page(A10_CATEGORY_REGISTRY["serious_illegal"]["path"],
                      {"keyword": keyword, "pageNum": 1, "pageSize": 20},
                      keyword, "legacy:serious_illegal:page:1")


if __name__ == "__main__":
    print("企业信息接口凭据：" + ("已配置" if _is_configured() else "未配置"))
    print("已启用类别：税务评级、失信被执行人、严重违法、经营异常、建筑资质不良行为、资质证书")
    print("预留未启用：关联关系（A14）")
