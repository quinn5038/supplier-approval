"""
企业信息接口 独立客户端（独立于集成环境，可脱离连接器插件运行）
==================================================
被 auto_approve.py 的 enhance_checklist_with_qcc 调用。
当前为接入骨架（13个函数 stub），等获取 QCC_APP_KEY/QCC_SECRET_KEY 后填实。

接口实际归属（2026-09-04 接口文档扫描确认）：
- 域名：https://ai-dataapi.ccccltd.cn  ← CCC=中国交建，港湾是中交子公司
- 这不是企查查公开 API，是中交系内部接口
- 鉴权方式：AppId + Timespan（Unix秒）+ Token=MD5(AppId+Timespan+secretKey).upper()
- 响应码：200 成功 / 300000 无数据 / 300002 账号失效 / 300006 余额不足 / 300011 此IP无权限

接入清单（共13个接口，按接入排期：先做第一档+第二档围标串标留后续版本）：
- 第一档10个核心合规（A10 商业信誉硬拦截项）：
    工商: ic/verify/2.0, ic/baseinfoV3/2.0
    股东法人: ic/holder/2.0, ic/inverst/2.0, ic/staff/2.0
    司法风险: jr/dishonest/2.0 (人员), jr/consumptionRestriction/2.0, jr/endCase, jr/bankruptcy/2.0
    经营违法: hi/abnormal/2.0, mr/illegalinfo (税收违法 mr/taxContravention/2.0)
- 第一档扩展（财务三表，对应保密改造后 A08 财报合规）：
    cb/ic/balanceSheet/2.0, cb/ic/incomeStatement/2.0, cb/ic/cashFlow/2.0
- 第二档3个围标串标（先不做）：rela/shortPath/2.0, v3/investtree/ten, ic/humanholding/2.0

设计原则：
1. 失败返回空 dict/list，不抛异常 → enhance_checklist_with_qcc 走"数据缺失→人工复核"路径，不误判
2. 缓存机制：每次调用结果追加到 qcc_api_cache.json，避免重复请求耗配额
3. 配额预警：每日调用量 >80% 阈值时 log.warning
4. 签名机制：MD5(AppId + Timespan + secretKey).upper()，与接口文档一致
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
# 接口实际域名（中国交建系接口，非企查查公开 API）
QCC_BASE_URL = os.getenv("QCC_BASE_URL", "https://ai-dataapi.ccccltd.cn")
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
    """中交系接口签名：MD5(AppId + Timespan + secretKey).upper()

    注意：与企查查公开 API 不同，是中交系内部约定。
    secretKey 由系统提供，不参与 HTTP 传输（仅用于 token 计算）。
    """
    raw = f"{QCC_APP_KEY}{timestamp}{QCC_SECRET}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def _headers() -> dict:
    """生成请求头（AppId + Timespan + Token）

    按接口文档 9/4 扫描：三个 header 字段名是 AppId/Timespan/Token，
    不是企查查公开 API 的 apicode/timestamp/sign。
    """
    ts = str(int(time.time()))
    return {
        "AppId": QCC_APP_KEY,
        "Timespan": ts,
        "Token": _sign(ts),
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
    """检查配额预警（占位，实际配额上限需申请后填入）"""
    # TODO: 申请 AppKey 后填入每日配额上限（企业版通常 1000-10000次/天）
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


def _post(path: str, body: dict, supplier_name: str = "", cache_key: str = "") -> dict:
    """
    统一 POST 调用入口（接口文档全是 POST 方法）
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
        log.info(f"[QCC] POST {path} supplier={supplier_name}")
        r = requests.post(url, headers=_headers(), json=body, timeout=QCC_TIMEOUT)
        _increment_call_count(path)
        if not r.ok:
            log.warning(f"[QCC] {path} HTTP {r.status_code}: {r.text[:200]}")
            return {}
        result = r.json()
        # 中交系接口响应：status=200 成功，其他业务失败码（300000/300001/...）
        if str(result.get("status", "")) != "200":
            log.warning(f"[QCC] {path} 业务失败 status={result.get('status')} msg={result.get('msg', '')}")
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
# 按接口文档 9/4 扫描的真实路径调整
# ============================================================
def verify_ic(name: str, creditcode: str, legalrep: str) -> dict:
    """1. 工商三要素核验（企业名/统一社会信用代码/法人）→ A01 工商执照核实"""
    return _post("/webapi/ic/verify/2.0", {
        "name": name, "creditCode": creditcode, "legalRep": legalrep
    }, supplier_name=name, cache_key="verify_ic")


def baseinfo(keyword: str) -> dict:
    """2. 工商基本信息（含主要人员、注册资本、成立日期）→ A01/A02"""
    return _post("/webapi/cb/cb/ic/2.0", {
        "keyword": keyword
    }, supplier_name=keyword, cache_key="baseinfo")


def holders(keyword: str) -> dict:
    """3. 股东信息（工商快照里拿）→ A03 股东核实"""
    # 工商快照接口已含股东及出资信息（接口文档）
    return _post("/webapi/ic/snapshot", {
        "keyword": keyword
    }, supplier_name=keyword, cache_key="holders")


def staff(keyword: str) -> dict:
    """4. 主要人员（法人、董事、监事）→ A02 法人核实"""
    # 主要人员通常在 baseinfo 返回里
    return baseinfo(keyword)


def dishonest_person(keyword: str, human_name: str = "") -> dict:
    """5. 失信被执行人（人员）→ A10 硬拦截项"""
    body = {"name": keyword, "pageNum": 1, "pageSize": 20}
    if human_name:
        body["humanName"] = human_name
    return _post("/webapi/v4/human/dishonest", body,
                 supplier_name=keyword, cache_key="dishonest")


def consumption_restriction(keyword: str) -> dict:
    """6. 限制消费令 → A10 硬拦截项"""
    return _post("/webapi/jr/consumptionRestriction/2.0", {
        "keyword": keyword, "pageNum": 1, "pageSize": 20
    }, supplier_name=keyword, cache_key="consumption_restriction")


def end_case(keyword: str) -> dict:
    """7. 终本案件（执行案件终结本次程序）→ A10 扩展"""
    # 终本案件在接口清单里，需单独查文档确认路径
    return _post("/webapi/jr/endCase", {
        "keyword": keyword, "pageNum": 1, "pageSize": 20
    }, supplier_name=keyword, cache_key="end_case")


def bankruptcy(keyword: str) -> dict:
    """8. 破产重整 → A10 严重违法"""
    return _post("/webapi/jr/bankruptcy/2.0", {
        "keyword": keyword, "pageNum": 1, "pageSize": 20
    }, supplier_name=keyword, cache_key="bankruptcy")


def operation_abnormal(keyword: str) -> dict:
    """9. 经营异常名录 → A10 硬拦截项"""
    return _post("/webapi/hi/abnormal/2.0", {
        "keyword": keyword, "pageNum": 1, "pageSize": 20
    }, supplier_name=keyword, cache_key="operation_abnormal")


def tax_contravention(keyword: str) -> dict:
    """10. 税收违法 → A10 严重违法"""
    return _post("/webapi/mr/taxContravention/2.0", {
        "keyword": keyword, "pageNum": 1, "pageSize": 20
    }, supplier_name=keyword, cache_key="tax_contravention")


# ============================================================
# 第一档扩展（财务三表，对应保密改造后 A08 财报合规）
# 9/4 保密改造：A08 财报改走企查查公查数据，无数据转人工
# ============================================================
def balance_sheet(keyword: str) -> dict:
    """11. 资产负债表 → A08 财报（资产负债率计算）"""
    return _post("/webapi/cb/cb/ic/balanceSheet/2.0", {
        "keyword": keyword
    }, supplier_name=keyword, cache_key="balance_sheet")


def income_statement(keyword: str) -> dict:
    """12. 利润表 → A08 财报（辅助判断经营情况）"""
    return _post("/webapi/cb/cb/ic/incomeStatement/2.0", {
        "keyword": keyword
    }, supplier_name=keyword, cache_key="income_statement")


def cash_flow(keyword: str) -> dict:
    """13. 现金流量表 → A08 财报（经营性现金流判断）"""
    return _post("/webapi/cb/cb/ic/cashFlow/2.0", {
        "keyword": keyword
    }, supplier_name=keyword, cache_key="cash_flow")


# ============================================================
# 批量入口（被 enhance_checklist_with_qcc 调用）
# ============================================================
def fetch_first_tier(name: str) -> dict:
    """
    批量拉取第一档全部接口数据（单家供应商）
    返回结构对齐 qcc_results.json 现有字段（向后兼容）：
    {
        "reg_info": {...},           # baseinfo 返回
        "accuracy": {...},           # verify_ic 返回
        "shareholders": [...],       # holders 返回
        "actual_controller": {...},  # human_holding 返回（暂未对接）
        "dishonest": [...],          # 新增 5 块（合规拦截）
        "executed": [...],
        "consumption_restriction": [...],
        "operation_abnormal": [...],
        "illegal_info": [...],       # 实际是 tax_contravention
        "financial": {...}           # 财报三表（A08 财报合规）
    }
    """
    return {
        "reg_info": baseinfo(name),
        "accuracy": verify_ic(name, "", ""),
        "shareholders": holders(name).get("result", []),
        "actual_controller": {},  # 围标串标接口，先不做
        "dishonest": dishonest_person(name).get("result", []),
        "executed": [],  # 历史被执行人，需单查
        "consumption_restriction": consumption_restriction(name).get("result", []),
        "operation_abnormal": operation_abnormal(name).get("result", []),
        "illegal_info": tax_contravention(name).get("result", []),
        "financial": {
            "balance_sheet": balance_sheet(name),
            "income_statement": income_statement(name),
            "cash_flow": cash_flow(name),
        },
    }


if __name__ == "__main__":
    # 自检：AppKey 未配置时的行为
    if not _is_configured():
        print("[QCC] QCC_APP_KEY/QCC_SECRET_KEY 未配置")
        print("[QCC] 调用任何接口都会返回空 dict，enhance_checklist_with_qcc 走人工复核路径")
        print("[QCC] 不影响现有审批流程，零侵入")
        print(f"[QCC] 接口域名: {QCC_BASE_URL}")
        print("[QCC] 鉴权方式: MD5(AppId + Timespan + secretKey).upper()")
    else:
        print(f"[QCC] 已配置 AppKey={QCC_APP_KEY[:8]}...")
        # TODO: 申请到 AppKey 后，这里加一个端到端自检
