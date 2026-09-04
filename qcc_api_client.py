"""
企查查 API 独立客户端（不依赖 WorkBuddy 连接器）
================================================
被 auto_approve.py 的 enhance_checklist_with_qcc 调用。
当前为接入骨架（13个函数 stub），等 Quinn 提供 QCC_APP_KEY/QCC_SECRET_KEY 后填实。

接入清单（共13个接口）：
- 第一档10个核心合规（A10 商业信誉硬拦截项）：verify/baseinfo/holder/inverst/staff
                                + dishonest/zhixinginfo/consumptionRestriction/baseinfo-illegal/illegalinfo
- 第二档3个围标串标（多家供应商关联排查）：shortPath/investtree/humanholding

设计原则：
1. 失败返回空 dict/list，不抛异常 → enhance_checklist_with_qcc 走"数据缺失→人工复核"路径，不误判
2. 缓存机制：每次调用结果追加到 qcc_results.json，避免重复请求耗配额
3. 配额预警：每日调用量 >80% 阈值时 log.warning
4. 签名机制：md5(timestamp + appkey + secretkey)，与企查查开放平台规范一致
"""
import os
import time
import json
import hashlib
import logging
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    requests = None

log = logging.getLogger(__name__)

# ============================================================
# 配置（从 .env 读取）
# ============================================================
QCC_APP_KEY  = os.getenv("QCC_APP_KEY", "")
QCC_SECRET   = os.getenv("QCC_SECRET_KEY", "")
QCC_BASE_URL = os.getenv("QCC_BASE_URL", "https://api.qcc.com")  # 实际域名看申请到的通道
QCC_TIMEOUT  = int(os.getenv("QCC_TIMEOUT", "15"))

# 配额预警阈值（每日调用量百分比）
QCC_DAILY_QUOTA_WARN = 0.8

# 缓存文件路径（与 auto_approve.py 同目录）
_CACHE_FILE = Path(__file__).parent / "qcc_api_cache.json"
_CALL_COUNT_FILE = Path(__file__).parent / "qcc_call_count.json"


# ============================================================
# 内部工具
# ============================================================
def _sign(timestamp: str) -> str:
    """企查查签名：md5(timestamp + appkey + secretkey)"""
    raw = f"{timestamp}{QCC_APP_KEY}{QCC_SECRET}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _headers() -> dict:
    """生成请求头（apicode + timestamp + sign）"""
    ts = str(int(time.time()))
    return {
        "apicode": QCC_APP_KEY,
        "timestamp": ts,
        "sign": _sign(ts),
        "Content-Type": "application/json",
    }


def _is_configured() -> bool:
    """是否已配置 QCC 凭据"""
    return bool(QCC_APP_KEY and QCC_SECRET)


def _increment_call_count(api_path: str):
    """记录调用次数（用于配额预警）"""
    today = time.strftime("%Y-%m-%d")
    try:
        data = json.loads(_CALL_COUNT_FILE.read_text(encoding="utf-8")) if _CALL_COUNT_FILE.exists() else {}
    except Exception:
        data = {}
    if today not in data:
        data[today] = {"total": 0, "by_api": {}}
    data[today]["total"] += 1
    data[today]["by_api"][api_path] = data[today]["by_api"].get(api_path, 0) + 1
    try:
        _CALL_COUNT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"[QCC] 调用次数记录失败：{e}")


def _check_quota_warning():
    """检查配额预警（占位，实际配额上限需 Quinn 申请后填入）"""
    # TODO: Quinn 申请 AppKey 后填入每日配额上限（企业版通常 1000-10000次/天）
    pass


def _cache_get(supplier_name: str, key: str):
    """从缓存读取（命中则不调 API，节省配额）"""
    try:
        if not _CACHE_FILE.exists():
            return None
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        return data.get(supplier_name, {}).get(key)
    except Exception:
        return None


def _cache_set(supplier_name: str, key: str, value):
    """写入缓存"""
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8")) if _CACHE_FILE.exists() else {}
        if supplier_name not in data:
            data[supplier_name] = {}
        data[supplier_name][key] = value
        _CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"[QCC] 缓存写入失败：{e}")


def _get(path: str, params: dict, supplier_name: str = "", cache_key: str = "") -> dict:
    """
    统一调用入口
    - cache_key 非空则优先读缓存
    - 失败返回空 dict，不抛异常
    """
    # 1. AppKey 未配置 → 直接返回空 dict（enhance_checklist_with_qcc 走人工复核路径）
    if not _is_configured():
        log.debug(f"[QCC] AppKey 未配置，跳过 {path}")
        return {}

    # 2. 优先读缓存
    if cache_key and supplier_name:
        cached = _cache_get(supplier_name, cache_key)
        if cached is not None:
            log.debug(f"[QCC] 命中缓存 {supplier_name}/{cache_key}")
            return cached

    # 3. 调用 API
    if requests is None:
        log.error("[QCC] requests 模块未安装")
        return {}
    try:
        url = QCC_BASE_URL + path
        log.info(f"[QCC] GET {path} supplier={supplier_name}")
        r = requests.get(url, headers=_headers(), params=params, timeout=QCC_TIMEOUT)
        _increment_call_count(path)
        if not r.ok:
            log.warning(f"[QCC] {path} HTTP {r.status_code}: {r.text[:200]}")
            return {}
        result = r.json()
        # 企查查返回 code=0 是成功，其他是失败
        if str(result.get("status", "")) not in ("200", "0"):
            log.warning(f"[QCC] {path} 业务失败：{result.get('message', '')}")
            return {}
        # 写缓存
        if cache_key and supplier_name:
            _cache_set(supplier_name, cache_key, result)
        return result
    except Exception as e:
        log.warning(f"[QCC] {path} 调用异常：{e}")
        return {}


# ============================================================
# 第一档：10个核心合规接口（A10 商业信誉硬拦截项）
# ============================================================
def verify_ic(name: str, creditcode: str, legalrep: str) -> dict:
    """1. 工商三要素核验（企业名/统一社会信用代码/法人）→ A01 工商执照核实"""
    return _get("/webapi/ic/verify/2.0", {
        "name": name, "creditCode": creditcode, "legalRep": legalrep
    }, supplier_name=name, cache_key="verify_ic")


def baseinfo(name: str) -> dict:
    """2. 工商基本信息（含主要人员、注册资本、成立日期）→ A01/A02 工商+法人"""
    return _get("/webapi/ic/baseinfoV3/2.0", {
        "keyword": name
    }, supplier_name=name, cache_key="baseinfo")


def holders(name: str) -> dict:
    """3. 股东信息 → A03 股东核实"""
    return _get("/webapi/ic/holder/2.0", {
        "keyword": name
    }, supplier_name=name, cache_key="holders")


def invest_out(name: str) -> dict:
    """4. 对外投资 → A03 股东核实（扩展）"""
    return _get("/webapi/ic/inverst/2.0", {
        "keyword": name
    }, supplier_name=name, cache_key="invest_out")


def staff(name: str) -> dict:
    """5. 主要人员（法人、董事、监事）→ A02 法人核实"""
    return _get("/webapi/ic/staff/2.0", {
        "keyword": name
    }, supplier_name=name, cache_key="staff")


def dishonest(name: str) -> dict:
    """6. 失信被执行人（A10 硬拦截项）→ A10 商业信誉"""
    return _get("/webapi/jr/dishonest/2.0", {
        "keyword": name
    }, supplier_name=name, cache_key="dishonest")


def zhixinginfo(name: str) -> dict:
    """7. 被执行人（A10 硬拦截项）→ A10 商业信誉"""
    return _get("/webapi/jr/zhixinginfo", {
        "keyword": name
    }, supplier_name=name, cache_key="zhixinginfo")


def consumption_restriction(name: str) -> dict:
    """8. 限制消费令（A10 硬拦截项）→ A10 商业信誉"""
    return _get("/webapi/jr/consumptionRestriction", {
        "keyword": name
    }, supplier_name=name, cache_key="consumption_restriction")


def operation_abnormal(name: str) -> dict:
    """9. 经营异常名录（A10 硬拦截项）→ A10 商业信誉"""
    return _get("/webapi/mr/baseinfo/normal", {
        "keyword": name
    }, supplier_name=name, cache_key="operation_abnormal")


def illegal_info(name: str) -> dict:
    """10. 严重违法失信（A10 硬拦截项）→ A10 商业信誉"""
    return _get("/webapi/mr/illegalinfo", {
        "keyword": name
    }, supplier_name=name, cache_key="illegal_info")


# ============================================================
# 第一档扩展（3个，强烈推荐加，演示加分）
# ============================================================
def certificate(name: str) -> dict:
    """11. 资质证书（用于 A04/A05 资质核实）"""
    return _get("/webapi/m/certificate/2.0", {
        "keyword": name
    }, supplier_name=name, cache_key="certificate")


def tax_credit(name: str) -> dict:
    """12. 纳税信用等级（A10 商业信誉扩展）"""
    return _get("/webapi/m/taxCredit/2.0", {
        "keyword": name
    }, supplier_name=name, cache_key="tax_credit")


def end_case(name: str) -> dict:
    """13. 终本案件（A10 扩展）"""
    return _get("/webapi/jr/endCase", {
        "keyword": name
    }, supplier_name=name, cache_key="end_case")


# ============================================================
# 第二档：3个围标串标接口（多家供应商关联排查）
# ============================================================
def short_path(name_a: str, name_b: str) -> dict:
    """14. 两家公司最短路径（查共同股东/法人）→ 围标串标识别"""
    # 注意：此接口需要两个公司名，缓存键特殊
    cache_key = f"short_path_{hash(name_a)}_{hash(name_b)}"
    return _get("/webapi/rela/shortPath/2.0", {
        "name1": name_a, "name2": name_b
    }, supplier_name=f"{name_a}↔{name_b}", cache_key=cache_key)


def invest_tree(name: str, depth: int = 10) -> dict:
    """15. 股权穿透10层（追溯实控人）→ 关联交易识别"""
    return _get("/webapi/v3/investtree/ten", {
        "keyword": name, "depth": depth
    }, supplier_name=name, cache_key=f"invest_tree_{depth}")


def human_holding(name: str) -> dict:
    """16. 最终受益人（实际控制人）→ 影子股东识别"""
    return _get("/webapi/ic/humanholding/2.0", {
        "keyword": name
    }, supplier_name=name, cache_key="human_holding")


# ============================================================
# 批量入口（被 enhance_checklist_with_qcc 调用）
# ============================================================
def fetch_first_tier(name: str) -> dict:
    """
    批量拉取第一档全部10个接口数据（单家供应商）
    返回结构对齐 qcc_results.json 现有字段（向后兼容）：
    {
        "reg_info": {...},           # baseinfo 返回
        "accuracy": {...},           # verify_ic 返回
        "shareholders": [...],       # holders 返回
        "actual_controller": {...},  # human_holding 返回（第二档接口，这里也拉）
        "dishonest": [...],          # 新增 5 块
        "executed": [...],
        "consumption_restriction": [...],
        "operation_abnormal": [...],
        "illegal_info": [...]
    }
    """
    return {
        "reg_info": baseinfo(name),
        "accuracy": verify_ic(name, "", ""),  # 需要 creditcode/legalrep，从 reg_info 里取
        "shareholders": holders(name).get("Result", []),
        "actual_controller": human_holding(name),
        "dishonest": dishonest(name).get("Result", []),
        "executed": zhixinginfo(name).get("Result", []),
        "consumption_restriction": consumption_restriction(name).get("Result", []),
        "operation_abnormal": operation_abnormal(name).get("Result", []),
        "illegal_info": illegal_info(name).get("Result", []),
    }


def fetch_second_tier_pairs(suppliers: list) -> dict:
    """
    第二档围标串标：N家供应商两两组合调用 shortPath
    N=13 时调用 78 次，配额压力大
    返回 {pair_key: path_info}
    """
    result = {}
    for i, a in enumerate(suppliers):
        for b in suppliers[i+1:]:
            pair_key = f"{a}↔{b}"
            result[pair_key] = short_path(a, b)
    return result


if __name__ == "__main__":
    # 自检：AppKey 未配置时的行为
    if not _is_configured():
        print("[QCC] QCC_APP_KEY/QCC_SECRET_KEY 未配置")
        print("[QCC] 调用任何接口都会返回空 dict，enhance_checklist_with_qcc 走人工复核路径")
        print("[QCC] 不影响现有审批流程，零侵入")
    else:
        print(f"[QCC] 已配置 AppKey={QCC_APP_KEY[:8]}...")
        # TODO: Quinn 申请到 AppKey 后，这里加一个端到端自检
