"""中交企业信息接口客户端。

当前生产接入仅启用 A10 的两个已授权类别：失信被执行人、严重违法。
原始响应只保存在本机、被 ``.gitignore`` 排除的 SQLite 缓存中；审批结果
仅保留类别状态和数量，不落案件编号、证件号码、人员姓名等明细。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
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
}


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


def _fetch_category(category: str, keyword: str, identity: str,
                    query_variant: str = "primary") -> dict:
    config = A10_CATEGORY_REGISTRY[category]
    if not _is_configured():
        return {"status": "not_configured", "items": [], "total": None, "complete": False}
    if not keyword:
        return {"status": "invalid_input", "items": [], "total": None, "complete": False}

    items: list[dict] = []
    total: int | None = None
    for page in range(1, QCC_MAX_PAGES + 1):
        response = _post_page(
            config["path"],
            {"keyword": keyword, "pageNum": page, "pageSize": 20},
            identity,
            f"a10:{category}:v1:{query_variant}:page:{page}",
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
        if not isinstance(result, dict):
            return {"status": "error", "items": items, "total": total,
                    "complete": False, "error": "invalid_result_shape"}
        page_items = result.get("items") or []
        if not isinstance(page_items, list):
            return {"status": "error", "items": items, "total": total,
                    "complete": False, "error": "invalid_items_shape"}
        items.extend(item for item in page_items if isinstance(item, dict))
        try:
            total = int(result.get("total", len(items)))
        except (TypeError, ValueError):
            total = len(items)
        if len(items) >= total or not page_items:
            return {"status": "ok", "items": items, "total": total, "complete": True}
    return {"status": "ok", "items": items, "total": total, "complete": False,
            "error": "page_limit"}


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


def fetch_a10_checks(name: str, credit_code: str = "") -> dict:
    """查询已授权 A10 类别，返回不含案件明细的结构化摘要。"""
    keyword = credit_code.strip() or name.strip()
    # 缓存继续按供应商名分组，以便现有“清理单家供应商缓存”同步删除。
    identity = name.strip() or credit_code.strip()
    def _fetch_with_name_fallback(category: str) -> dict:
        primary = _fetch_category(
            category, keyword, identity, "credit" if credit_code.strip() else "name")
        # 企业名称是文档示例的标准入参。信用代码优先查询无记录时再用名称复查，
        # 避免接口只按名称建索引而产生假阴性；两种查询各自缓存，不混用。
        if (credit_code.strip() and name.strip() and primary.get("status") == "ok"
                and int(primary.get("total") or 0) == 0):
            fallback = _fetch_category(category, name.strip(), identity, "name")
            if fallback.get("status") == "ok":
                return fallback
        return primary

    dishonest = _assess_dishonest(_fetch_with_name_fallback("dishonest"))
    serious = _assess_serious_illegal(_fetch_with_name_fallback("serious_illegal"))
    return {
        "version": 1,
        "queried_categories": list(A10_CATEGORY_REGISTRY),
        "pending_categories": ["被执行人", "限制消费令", "经营异常"],
        "categories": {"dishonest": dishonest, "serious_illegal": serious},
        "complete_for_a10": False,
    }


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
    print("已启用类别：失信被执行人、严重违法")
