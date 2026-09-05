"""
供应商自动审批脚本（ICCEC 实战版）
基于 HAR 实录的完整接口链路，从拉取待办列表到审批操作全自动。

用法:
    pip install pyyaml requests
    # 在 .env 或环境变量中设置:
    export APP_TOKEN="你的APP_TOKEN"
    export AGENT_ID="你的agentId"
    export CODE="你的CODE"
    python auto_approve.py

断点续跑:
    每处理完一条待办就追加写入 progress_v4.jsonl；
    中断后重新运行会自动跳过已处理的 todoId（想重新全量跑就删掉该文件）。

WAF熔断:
    scpma 的华为WAF封禁实测>13分钟；超时后自动长冷却(10/15分钟)重试，
    仍失败则熔断退出（退出码2），进度已保存，稍后重跑自动续跑。
    节奏可用环境变量调：REQ_INTERVAL（默认5s，请求间隔）、
    SUPPLIER_INTERVAL（默认15s，供应商间隔）。

认证说明:
    - asca.iccec.cn 接口用 APP_TOKEN 请求头
    - scpma.iccec.cn 接口 APP_TOKEN 为空，靠 session cookie 认证
      → 需要通过 Playwright 登录后获取 cookie，或手动从浏览器复制 cookie
      → 见 README.md 中"认证获取"章节

安全:
    - Token/Cookie 走环境变量，绝不硬编码
    - 默认 dry_run=True，只模拟不真审批，确认规则无误后改 False
"""

import os
import json
import time
import logging
import sys
from datetime import datetime
from pathlib import Path
from eval import safe_eval_rule


# ============================================================
# 自动加载 .env 文件（不需要安装额外依赖）
# ============================================================
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _k, _v = _k.strip(), _v.strip()
            if _k and _k not in os.environ:
                os.environ[_k] = _v

try:
    import yaml
except ImportError:
    raise ImportError("pip install pyyaml")

try:
    import requests
except ImportError:
    raise ImportError("pip install requests")


# ============================================================
# 配置：从环境变量读取敏感信息
# ============================================================
APP_TOKEN = os.getenv("APP_TOKEN", "")           # asca 域名用
AGENT_ID  = os.getenv("AGENT_ID", "")             # 请求体里的 agentId（URL 参数 a 的值）
CODE      = os.getenv("CODE", "")                 # URL 参数 CODE 的值
# 修复：改为函数动态读取，webui 更新 os.environ 后立即生效（不再需要重启）
# 之前是模块级常量，import 时读一次锁死，导致 webui 粘贴 cookie 后仍显示过期
def _scpma_cookie() -> str:
    return os.getenv("SCPMA_COOKIE", "")

def _scpma_auth() -> str:
    return os.getenv("SCPMA_AUTH_TOKEN", "")

# 固定参数（从 HAR + cURL 实录确认）
BASE_APP_ID  = 100126
PUR_SUP_FLAG = "pur"
BUSINESS_CODE = "P07"
BUSINESS_BILL_TYPE = "P0702"   # 从"通过"cURL确认（之前HAR里是P0704=退回场景，这里是P0702=通过场景）
BUSINESS_APP_ID = "100032"
ORG_ID = int(os.getenv("ORG_ID", "103495"))

# 审批操作码（从 cURL 实录确认）
OPER_CODE_APPROVE = 2   # 通过（从 cURL 确认，之前猜的1是错的）
OPER_CODE_REJECT  = 3   # 退回（从 HAR 确认）

# 域名
ASCA_BASE = "https://asca.iccec.cn/apis/asca"
SCPMA_BASE = "https://scpma.iccec.cn/apis/scpma"

# 安全开关：True=只模拟不真审批，False=真实审批
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# WAF 防限流节奏（scpma 华为WAF对密集请求封禁IP，2026-08-31实测封禁>13分钟）
REQ_INTERVAL = int(os.getenv("REQ_INTERVAL", "5"))              # 同一供应商内 scpma 请求间隔（秒）
SUPPLIER_INTERVAL = int(os.getenv("SUPPLIER_INTERVAL", "15"))   # 供应商之间的间隔（秒）

BASE_DIR = Path(__file__).parent
RULES_FILE = BASE_DIR / "rules.yaml"
PROGRESS_FILE = BASE_DIR / "progress_v4.jsonl"   # 断点续跑进度（每处理完一条追加）
CACHE_FILE = BASE_DIR / "cache_v4.json"          # 供应商原始数据缓存（fetch时写入，stage2离线用）
QCC_FILE = BASE_DIR / "qcc_results.json"         # 企查查核验结果（agent调用MCP后写入）
STAGE2_FILE = BASE_DIR / "stage2_results.json"   # 阶段2输出（合并企查查后的清单+意见）

# 企查查核验模式：仅拉数据入缓存，不做决策（供阶段2离线重跑）
FETCH_ONLY = os.getenv("FETCH_ONLY", "").lower() == "true"
# fetch-only 时只拉指定 todoId（逗号分隔），空=全部
FETCH_IDS = {x.strip() for x in os.getenv("FETCH_IDS", "").split(",") if x.strip()}
LOG_FILE = BASE_DIR / f"approval_{datetime.now():%Y%m%d}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("supplier-approval")


# ============================================================
# HTTP 客户端
# ============================================================
# 全局 Session（keep-alive 长连接，所有请求复用，降低被WAF限流概率）
# trust_env=False：不走系统代理（换网络后残留的代理设置会导致
# ProxyError: Unable to connect to proxy —— ICCEC 是公司内网域名必须直连）
_SESSION = requests.Session()
_SESSION.trust_env = False


def _asca_headers():
    """asca.iccec.cn 的请求头（需要 APP_TOKEN + Cookie 双重认证）"""
    h = {
        "APP_TOKEN": APP_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
    }
    _c = _scpma_cookie()
    if _c:
        h["Cookie"] = _c
    return h


def _scpma_headers():
    """scpma.iccec.cn 的请求头（APP_TOKEN 为空，靠 Cookie + Authorization 认证）"""
    h = {
        "APP_TOKEN": "",
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
    }
    _c = _scpma_cookie()
    if _c:
        h["Cookie"] = _c
    _a = _scpma_auth()
    if _a:
        h["Authorization"] = _a
    return h


def _common_body(**extra):
    """每个 scpma 请求都带的公共参数"""
    base = {
        "agentId": AGENT_ID,
        "purSupFlag": PUR_SUP_FLAG,
        "baseAppId": BASE_APP_ID,
    }
    base.update(extra)
    return base


class WafBlockedError(RuntimeError):
    """scpma WAF 持续封禁（长冷却重试后仍超时），用于熔断退出"""
    pass


class SessionExpiredError(RuntimeError):
    """登录态过期（接口返回 code=111：用户信息超时或未登录）
    2026-09-02 实测：asca 和 scpma 两域同时过期，待办列表会静默显示 0 条，
    必须显式拦截，避免误判为"无待办"或"待办已关闭"空跑。
    修复：浏览器重新登录 scpma.iccec.cn → F12 复制最新 Cookie/APP_TOKEN → 更新 .env
    """
    pass


# scpma 超时后的长冷却重试梯度（秒）
# 2026-08-31 23:19 实测：WAF封禁持续>13分钟，短退避(60/90/120s)全部无效；
# 改为 10分钟/15分钟 两轮长冷却，仍超时则熔断退出（进度已保存，重跑自动续跑）
WAF_RETRY_WAITS = [600, 900]


def _post(url, headers, body, timeout=30, retry_waits=None):
    """统一的 POST 请求，带超时熔断（scpma 的 WAF 限流时会超时）
    - 全局 Session keep-alive 长连接（减少TLS握手）
    - scpma 超时 → 长冷却重试 600s/900s，仍超时 → 抛 WafBlockedError（主流程熔断退出）
    - asca 超时 → 短退避 15s/30s 后正常抛出
    - 非超时错误（连接拒绝等）不重试，直接抛出
    """
    is_scpma = "scpma.iccec.cn" in url
    if retry_waits is None:
        retry_waits = WAF_RETRY_WAITS if is_scpma else [15, 30]
    last_exc = None
    attempt = 0
    while True:
        try:
            resp = _SESSION.post(url, headers=headers, json=body, timeout=timeout)
            resp.raise_for_status()
            try:
                result = resp.json()
            except Exception:
                return {"_raw": resp.text}
            # 统一拦截登录态过期：code=111 说明 Cookie/APP_TOKEN 失效
            # （不拦截会被上层误读成"0条待办"或"待办已关闭"）
            if isinstance(result, dict) and str(result.get("code")) == "111":
                raise SessionExpiredError(
                    f"{url.split('/')[2]}/{url.split('/')[-1]} 返回 code=111"
                    f"（用户信息超时或未登录）—— .env 里的登录凭据已过期"
                )
            return result
        except requests.exceptions.Timeout as e:
            last_exc = e
            if attempt < len(retry_waits):
                wait = retry_waits[attempt]
                log.warning(f"[超时重试] {url.split('/')[-1]} 第{attempt+1}次失败，"
                            f"冷却{wait}s后重试（WAF限流）")
                time.sleep(wait)
                attempt += 1
            elif is_scpma:
                raise WafBlockedError(
                    f"scpma 持续超时（已长冷却重试{len(retry_waits)}轮），判定WAF长封禁"
                ) from e
            else:
                raise last_exc
        except requests.exceptions.RequestException:
            raise   # 非超时错误不重试，直接抛出


# ============================================================
# 核心 API（基于 HAR 实录）
# ============================================================
def fetch_pending_todos(page_no=1, page_size=50):
    """
    ③ 查询待审批列表
    POST https://asca.iccec.cn/apis/asca/todo/home/list
    返回: data.rows[] 待办列表，data.recordsTotal 总数
    每条含: id(=todoId), businessBillId, procInstId, actInstId, taskId,
            applyUnitName(申请单位), businessBillName(审批类型), businessBillType, title
    """
    url = f"{ASCA_BASE}/todo/home/list"
    body = {
        "pageNo": page_no,
        "pageSize": page_size,
        "status": 0,            # 0=待办
        "businessCode": "",
        "businessName": "",
        "agentId": "100005",   # 固定值
    }
    result = _post(url, _asca_headers(), body)
    data = result.get("data", {})
    rows = data.get("rows", []) if isinstance(data, dict) else []
    total = data.get("recordsTotal", 0) if isinstance(data, dict) else 0
    log.info(f"待办列表: 共 {total} 条，本次拉取 {len(rows)} 条")
    return result


def get_approval_request(todo_id):
    """
    ④ 获取审批请求详情
    POST /scpma/approve/getReq
    返回: businessBillId, procInstId, actInstId, taskId 等
    """
    url = f"{SCPMA_BASE}/approve/getReq"
    body = _common_body(todoId=str(todo_id))
    return _post(url, _scpma_headers(), body)


def query_todo_detail(todo_id):
    """
    ④-2 获取待办消息详情
    POST /scpma/approve/queryTodomsgDetailById
    """
    url = f"{SCPMA_BASE}/approve/queryTodomsgDetailById"
    body = _common_body(id=str(todo_id))
    return _post(url, _scpma_headers(), body)


def get_approval_buttons(todo_id, bill_id, proc_inst_id, act_inst_id, act_inst_name,
                          work_item_id, current_oper_user_id, current_oper_user_dept_id):
    """
    ⑩ 获取审批按钮（返回可用的操作：通过/退回等）
    POST /scpma/approve/get/button
    """
    url = f"{SCPMA_BASE}/approve/get/button"
    body = _common_body(
        todoId=str(todo_id),
        businessAppId=BUSINESS_APP_ID,
        businessBillId=str(bill_id),
        businessCode=BUSINESS_CODE,
        businessBillTypeCode=BUSINESS_BILL_TYPE,
        procInstId=str(proc_inst_id),
        actInstId=str(act_inst_id),
        actInstName=act_inst_name,
        workItemId=str(work_item_id),
        workItemStatus=0,
        currentOperUserId=current_oper_user_id,
        currentOperUserDeptId=current_oper_user_dept_id,
    )
    return _post(url, _scpma_headers(), body)


def query_supplier_base_info(sup_info_apply_id, business_bill_type="P0702"):
    """
    ⑤ 查询供应商注册基本信息
    POST /scpma/smc/qrySupRegistBaseInfo
    
    参数（从 cURL + Response 实录确认）:
      supInfoApplyId: 从 queryTodomsgDetailById 返回的 businessBillId
      tempOrFormalFlag: P0704(合作意向)=1, P0701(注册)/P0702(变更)=0
      businessBillType: P0701/P0702/P0704
      dataSourceFlag: 1
    
    返回字段（从 Response 确认）:
      supShort: 公司简称
      legalContactName: 法人
      creditCode: 统一社会信用代码（境内注册/变更有值，合作意向可能为空）
      officeAddr: 办公地址
      homeAddr: 家庭地址
      isOverseas: 0=境内, 1=境外
      countryName: 国家
      freeordisStateName: 状态（正常/冻结）
      isInIndy: 是否在黑名单(0=否)
      regCapl: 注册资本（万元）
      factCapl: 实缴资本（万元）
      foundTime: 成立日期
      moneyTypeName: 货币类型
      supTypeName: 供应商类型（制造商/贸易商等）
      suptypeQualName: 供应商等级（一般供应商等）
      tycInfo: 天眼查数据（JSON字符串，境外/合作意向可能为空）
      recommendIndyName: 推荐产业
      busiScope: 经营范围
    """
    url = f"{SCPMA_BASE}/smc/qrySupRegistBaseInfo"
    # P0704(合作意向)=1, P0701(注册)/P0702(变更)=0
    temp_flag = 1 if business_bill_type == "P0704" else 0
    body = _common_body(
        supInfoApplyId=str(sup_info_apply_id),
        tempOrFormalFlag=temp_flag,
        dataSourceFlag=1,
        businessBillType=business_bill_type,
    )
    return _post(url, _scpma_headers(), body)


def query_supplier_qualification(sup_info_apply_id, business_bill_type="P0702"):
    """
    ⑥ 查询资质信息
    POST /scpma/smc/qrySupQualification
    参数同 qrySupRegistBaseInfo: supInfoApplyId + tempOrFormalFlag + businessBillType
    """
    url = f"{SCPMA_BASE}/smc/qrySupQualification"
    temp_flag = 1 if business_bill_type == "P0704" else 0
    body = _common_body(
        supInfoApplyId=str(sup_info_apply_id),
        tempOrFormalFlag=temp_flag,
        dataSourceFlag=1,
        businessBillType=business_bill_type,
    )
    return _post(url, _scpma_headers(), body)


def query_qualification_files(sup_info_apply_id, business_bill_type="P0702"):
    """
    ⑥-2 查询资质文件列表
    POST /scpma/smc/qrySupplierQualificationFile
    
    ⚠️ 重要：这个接口的参数名是 supinfoId（不是 supInfoApplyId）！
    用 supInfoApplyId 会返回"获取用户信息失败"
    
    返回: data.supFilesBOList[] 上传文件列表
    每条含:
      - fileName: 文件名（如"2023新营业执照清晰.png"）
      - fileinfoTypeName: 附件名称/类型（如"营业执照副本图片"、"质量管理体系认证证书"）
      - fileDesc: 附件说明（可能为空）
      - fileSumm: 附件摘要（可能为空）
      - uploadId: 用于文件预览/下载
      - fileinfoType: 类型代码（00=营业执照, 01=法人身份证, 02=税务登记证等）
    """
    url = f"{SCPMA_BASE}/smc/qrySupplierQualificationFile"
    temp_flag = 1 if business_bill_type == "P0704" else 0
    body = _common_body(
        supinfoId=str(sup_info_apply_id),
        businessBillType=business_bill_type,
        tempOrFormalFlag=temp_flag,
        dataSourceFlag=1,
    )
    return _post(url, _scpma_headers(), body)


def query_finance_info(sup_info_apply_id, business_bill_type="P0702"):
    """
    ⑦ 查询财务信息
    POST /scpma/smc/qrySupFinanceInfo
    """
    url = f"{SCPMA_BASE}/smc/qrySupFinanceInfo"
    temp_flag = 1 if business_bill_type == "P0704" else 0
    body = _common_body(
        supInfoApplyId=str(sup_info_apply_id),
        businessBillType=business_bill_type,
        tempOrFormalFlag=temp_flag,
    )
    return _post(url, _scpma_headers(), body)


def query_audit_detail(supinfo_id, sup_audit_id):
    """
    ⑧ 查询审核详情
    POST /scpma/smc/qrySupAuditDetail
    """
    url = f"{SCPMA_BASE}/smc/qrySupAuditDetail"
    body = _common_body(
        supinfoId=str(supinfo_id),
        supAuditId=str(sup_audit_id),
        businessBillType=BUSINESS_BILL_TYPE,
    )
    return _post(url, _scpma_headers(), body)


def query_file_preview(upload_id):
    """
    ⑨ 文件预览/下载
    POST /scpma/oss/filePreview
    传入 uploadId，返回预览 URL（图片/PDF）
    """
    url = f"{SCPMA_BASE}/oss/filePreview"
    body = _common_body(uploadId=str(upload_id))
    return _post(url, _scpma_headers(), body)


def query_selectable_persons(proc_inst_id, act_inst_id, task_id, bill_id,
                              current_oper_user_id, current_oper_user_dept_id):
    """
    ⑪ 查询可选审批人（下一环节）
    POST /scpma/mtc/querySelectablePerson
    """
    url = f"{SCPMA_BASE}/mtc/querySelectablePerson"
    body = _common_body(
        procInstId=str(proc_inst_id),
        actInstId=str(act_inst_id),
        taskId=str(task_id),
        businessBillId=str(bill_id),
        businessCode=BUSINESS_CODE,
        businessBillType=BUSINESS_BILL_TYPE,
        currentOperUserId=current_oper_user_id,
        currentOperUserDeptId=current_oper_user_dept_id,
        orgId=str(ORG_ID),
        appId=BUSINESS_APP_ID,
        selectTypeCode="1",
    )
    return _post(url, _scpma_headers(), body)


def execute_approval(proc_inst_id, act_inst_id, act_inst_name, task_id, bill_id,
                      opinion, oper_code, business_para_bo=None, participant_list=None):
    """
    ⑫ 执行审批操作
    POST /scpma/mtc/approvalOperate

    oper_code（从 cURL 实录确认）:
        2 = 通过（cURL 确认）
        3 = 退回（HAR 确认）

    business_para_bo: 供应商类型信息（从查询接口获取，含 suptypeQualId 等）
    """
    url = f"{SCPMA_BASE}/mtc/approvalOperate"
    body = _common_body(
        procInstId=str(proc_inst_id),
        appointLists=[],
        actInstId=str(act_inst_id),
        actInstName=act_inst_name,
        taskId=str(task_id),
        opinion=opinion,
        remark=opinion,
        operCode=oper_code,
        participantList=participant_list or [],
        businessBillId=str(bill_id),
        businessCode=BUSINESS_CODE,
        businessBillType=BUSINESS_BILL_TYPE,
        businessAppId=BUSINESS_APP_ID,
        orgId=ORG_ID,
        bizState=0,
        isApplyUser=0,   # 从 cURL 确认（之前猜的1是错的）
        businessParaBO=business_para_bo or {},
    )

    if DRY_RUN:
        log.info(f"[DRY-RUN] 模拟审批 operCode={oper_code} opinion={opinion[:50]}")
        return {"_dry_run": True, "oper_code": oper_code}

    return _post(url, _scpma_headers(), body)


# ============================================================
# 规则引擎（支持多类别准入标准）
# ============================================================
def load_rules():
    with open(RULES_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ============================================================
# 材料分类引擎（基于资质文件列表）
# ============================================================
# 业务确认的附件名称 → 材料类型映射
# 供应商上传的文件按 fileinfoTypeName 分类，但有时会传到"错误"位置
# 所以需要 fileinfoTypeName + fileName + fileDesc 三结合判断
FILEINFO_TYPE_MAP = {
    "营业执照副本图片": {"business_license"},
    "营业执照": {"business_license"},
    "法人代表身份证复印件": {"legal_person_id"},
    "法人代表身份证": {"legal_person_id"},
    "法人身份证正反面": {"legal_person_id"},
    "税务登记证图片": {"tax_cert"},
    "税务登记证图片（含增值税一般纳税人红章页面）": {"tax_cert"},
    "质量管理体系认证证书": {"iso9001"},
    "环境管理体系认证证书": {"iso14001"},
    "职业健康安全管理体系认证证书": {"iso45001"},
    "健康安全管理体系认证证书": {"iso45001"},
    "财务报表": {"financial_report"},
    "审计报告": {"financial_report"},
    "财务审计报告": {"financial_report"},
    "特殊行业资质证书图片": set(),  # 需进一步从文件名/说明判断
    "其他": set(),  # 需进一步从文件名/说明判断
    "开户许可证图片": {"bank_permit"},
    "其他图片": set(),
    "专利证书图片": {"product_cert"},
    "产品认证证书图片": {"product_cert"},
    "检验检测报告图片": {"product_cert"},
    "检验检测机构资质认定证书": {"inspection_cert"},
    "资质认定证书": {"inspection_cert"},
    "CMA资质证书": {"inspection_cert"},
}

# 文件名/附件说明关键词 → 材料类型（用于"传到错误位置"的情况）
# 按优先级排列，先匹配的优先
KEYWORD_MAP = [
    # 检验检测资质（最高优先级，避免被"检测报告"误判到 product_cert）
    ("检验检测机构资质", {"inspection_cert"}),
    ("检验检测资质", {"inspection_cert"}),
    ("资质认定证书", {"inspection_cert"}),
    ("资质认定", {"inspection_cert"}),
    ("CMA", {"inspection_cert"}),
    ("cma", {"inspection_cert"}),
    ("CNAS", {"inspection_cert"}),
    ("cnas", {"inspection_cert"}),
    ("实验室认可", {"inspection_cert"}),
    ("计量认证", {"inspection_cert"}),
    ("检定校准", {"inspection_cert"}),
    # ISO三体系（精确匹配优先）
    ("9001", {"iso9001"}),
    ("ISO9001", {"iso9001"}),
    ("ISO 9001", {"iso9001"}),
    ("质量管理体系", {"iso9001"}),
    ("14001", {"iso14001"}),
    ("ISO14001", {"iso14001"}),
    ("ISO 14001", {"iso14001"}),
    ("环境管理体系", {"iso14001"}),
    ("45001", {"iso45001"}),
    ("ISO45001", {"iso45001"}),
    ("ISO 45001", {"iso45001"}),
    ("职业健康安全", {"iso45001"}),
    ("健康安全管理体系", {"iso45001"}),
    # 纳税信用等级
    ("纳税信用", {"tax_credit"}),
    ("纳税缴费信用", {"tax_credit"}),
    ("信用评价", {"tax_credit"}),
    ("信用等级", {"tax_credit"}),
    ("A级纳税人", {"tax_credit"}),
    ("B级纳税人", {"tax_credit"}),
    ("C级纳税人", {"tax_credit"}),
    ("纳税证明", {"tax_cert"}),
    # 财务
    ("财报", {"financial_report"}),
    ("审计", {"financial_report"}),
    ("资产负债表", {"financial_report"}),
    ("利润表", {"financial_report"}),
    ("现金流量表", {"financial_report"}),
    # 售后
    ("售后", {"after_sales_cert"}),
    ("五星", {"after_sales_cert"}),
    ("服务认证", {"after_sales_cert"}),
    ("声明函", {"after_sales_cert"}),
    ("说明函", {"after_sales_cert"}),
    # 授权/代理
    ("授权书", {"authorization"}),
    ("代理协议", {"authorization"}),
    ("委托书", {"authorization"}),
    ("销售授权", {"authorization"}),
    # 生产许可
    ("生产许可", {"production_license"}),
    ("强制认证", {"production_license"}),
    ("3C认证", {"production_license"}),
    ("3c认证", {"production_license"}),
    # 产品认证
    ("检测报告", {"product_cert"}),
    ("检验报告", {"product_cert"}),
    ("产品认证", {"product_cert"}),
    ("CE认证", {"product_cert"}),
    ("UL认证", {"product_cert"}),
    # 营业执照
    ("营业执照", {"business_license"}),
    # 法人身份证
    ("法人", {"legal_person_id"}),
    ("身份证", {"legal_person_id"}),
]


# 经营范围关键词 → 是否需要生产许可/强制认证证明（业务确认用关键词匹配，无需兜底）
# 3C强制认证产品目录（电线电缆/开关/灯具/电机/电池/充电器/家电/玩具/安全玻璃/汽车零部件等）
CCC_SCOPE_KEYWORDS = [
    "电线", "电缆", "开关", "灯具", "照明", "电机", "电池", "充电器",
    "家用电器", "家电", "玩具", "安全玻璃", "汽车零部件", "汽车配件",
    "低压电器", "插头", "插座", "电焊机",
]
# 工业产品生产许可证目录（食品/化工/农药/危化品/水泥/压力容器/电梯/起重机械等）
LICENSE_SCOPE_KEYWORDS = [
    "食品", "食品生产", "化工", "化学原料", "农药", "危险化学品", "危化",
    "水泥", "压力容器", "电梯", "起重机械", "起重设备", "制冷设备",
    "燃气器具", "化肥", "饲料", "化妆品",
]


def needs_production_license_by_scope(busi_scope):
    """
    根据经营范围判断是否需要生产许可证/强制认证证明（C1_05）
    返回: (是否需要, 需要的证明类型描述)
    """
    scope = str(busi_scope or "")
    for kw in CCC_SCOPE_KEYWORDS:
        if kw in scope:
            return True, "3C强制认证证书（经营范围涉及强制认证产品）"
    for kw in LICENSE_SCOPE_KEYWORDS:
        if kw in scope:
            return True, "生产许可证（经营范围涉及强制许可产品）"
    return False, ""


def classify_uploaded_materials(file_list):
    """
    从资质文件列表分类材料类型
    
    参数: file_list = qrySupplierQualificationFile 返回的 supFilesBOList
    返回: dict {
        'materials': set of material types found,
        'certifications': list of ISO certs found,  # ['ISO9001', 'ISO14001', 'ISO45001']
        'details': list of (fileName, fileinfoTypeName, fileDesc, classified_types)  # 供调试
    }
    """
    materials = set()
    certifications = []
    details = []
    
    for f in file_list:
        if not isinstance(f, dict):
            continue
        
        file_name = str(f.get("fileName", ""))
        type_name = str(f.get("fileinfoTypeName", ""))
        file_desc = str(f.get("fileDesc", ""))
        file_summ = str(f.get("fileSumm", ""))
        
        # 合并所有文字用于关键词搜索
        all_text = f"{type_name} {file_name} {file_desc} {file_summ}"
        
        classified = set()
        
        # 1. 先用 fileinfoTypeName 精确匹配
        if type_name in FILEINFO_TYPE_MAP:
            classified |= FILEINFO_TYPE_MAP[type_name]
        
        # 2. 再用关键词在 all_text 中搜索（补充"传到错误位置"的情况）
        for keyword, types in KEYWORD_MAP:
            if keyword.lower() in all_text.lower():
                classified |= types
        
        # 3. 特殊行业资质证书图片 → 需从文件名/说明判断具体是什么
        if type_name == "特殊行业资质证书图片" or type_name == "其他图片" or type_name == "其他":
            # 已经在上面关键词匹配中处理了
            pass
        
        materials |= classified
        details.append((file_name, type_name, file_desc, classified))
    
    # 从材料类型提取 ISO 认证列表
    if "iso9001" in materials:
        certifications.append("ISO9001")
    if "iso14001" in materials:
        certifications.append("ISO14001")
    if "iso45001" in materials:
        certifications.append("ISO45001")
    
    return {
        "materials": materials,
        "certifications": certifications,
        "details": details,
    }


def _build_skip_result(tid, entry, decision_reason):
    """9/5 新增：分流供应商（国外/集团）也生成"不适用"结果，避免列表 31 家有点击报 no_result"""
    supplier = entry.get("supplier", {})
    todo = entry.get("todo", {})
    name = supplier.get("name") or todo.get("applyUnitName") or tid
    return {
        "todoId": tid,
        "name": name,
        "billName": todo.get("billName") or "",
        "type_desc": "不适用",
        "decision": "skip",
        "opinion": f"不适用。{decision_reason}。",
        "checklist": [],
        "qcc_issues": [],
        "textin_issues": [],
        "materials_detail": entry.get("materials_detail", []),
        "certifications": entry.get("certifications", []),
        "stage2_at": datetime.now().isoformat(timespec="seconds"),
    }


def _ensure_inspection_fields(supplier, entry=None):
    """补全 is_inspection / has_inspection_cert 字段（兼容旧 cache）。
    9/3 新增 A13 检验检测资质，旧 cache 无此字段时按规则重新判定。"""
    if supplier.get("is_inspection") is None or "is_inspection" not in supplier:
        name = supplier.get("name", "")
        scope = supplier.get("busi_scope", "") or ""
        kw = ("检验检测", "检测服务", "检验服务", "检定", "校准",
              "实验室", "测试中心", "检验机构", "检测机构")
        supplier["is_inspection"] = (
            any(k in name for k in ("检验", "检测", "检定", "校准", "实验室"))
            or any(k in scope for k in kw))
    if "has_inspection_cert" not in supplier:
        cm = supplier.get("classified_materials") or set()
        if not cm and entry:
            # 从 materials_detail 重新提取 types 集合
            cm = set()
            for d in entry.get("materials_detail", []):
                for t in (d.get("types") or []):
                    cm.add(t)
        supplier["has_inspection_cert"] = "inspection_cert" in cm


def build_checklist(supplier, rules_list):
    """
    业务 8/31确认的统一检查流程：
    全部检查项跑完（齐全性+准确性），统一收集问题，最后一次性生成意见。
    不做"缺一项就立即退回"。

    返回: (checklist, auto_failed_rules, missing_rules, verify_items)
      checklist: [{'id','name','status','detail'}] status: pass/fail/pending/skip
      auto_failed_rules: auto硬性规则失败（如注册资本不足）
      missing_rules: 材料缺失的规则dict列表
      verify_items: 待核验描述列表（材料齐全但准确性未核验/需人工）
    """
    checklist = []
    auto_failed_rules = []
    missing_rules = []
    verify_items = []

    for rule in rules_list:
        ct = rule.get("check_type", "")
        rid = rule.get("id", "")
        name = rule.get("name", "")
        if ct in ("skip", ""):
            continue

        entry = {"id": rid, "name": name, "status": "pass", "detail": ""}

        if ct == "auto":
            ok, _ = evaluate_rule(rule, supplier)
            if ok:
                if rid == "C1_01":
                    entry["detail"] = f"注册资本{supplier.get('registered_capital', 0):g}万元，达到500万元要求"
                else:
                    entry["detail"] = "通过"
            else:
                entry["status"] = "fail"
                if rid == "C1_01":
                    entry["detail"] = f"注册资本{supplier.get('registered_capital', 0):g}万元，未达到500万元要求"
                else:
                    entry["detail"] = "未达标"
                auto_failed_rules.append(rule)
            checklist.append(entry)

        elif ct == "material":
            # 条件性规则：不需要检查时标记跳过（不进核验清单、不退回）
            if rid == "A07" and not supplier.get("is_manufacturer", False):
                entry["status"] = "skip"
                entry["detail"] = "贸易商不需要售后服务材料"
                checklist.append(entry)
                continue
            if rid == "A13" and not supplier.get("is_inspection", False):
                entry["status"] = "skip"
                entry["detail"] = "非检验检测类供应商，不需要检验检测资质"
                checklist.append(entry)
                continue
            if rid == "C1_05" and not supplier.get("needs_production_license", False):
                entry["status"] = "skip"
                entry["detail"] = "经营范围不涉及强制许可/认证产品"
                checklist.append(entry)
                continue

            ok, _ = evaluate_rule(rule, supplier)
            if not ok:
                entry["status"] = "fail"
                rid_name = f"{rid} {name}"
                req = rule.get("requirement") or MATERIAL_REQUIREMENTS.get(rid_name, rid_name)
                if rid == "C1_05":
                    why = supplier.get("production_license_reason", "")
                    entry["detail"] = f"缺少{req}" + (f"【{why}】" if why else "")
                else:
                    entry["detail"] = f"缺少{req}"
                missing_rules.append(rule)
            else:
                if rule.get("verify"):
                    # 材料已有，但准确性待核验（TextIn/企查查接入后自动核验）
                    entry["status"] = "pending"
                    entry["detail"] = f"材料已上传；待核验：{rule['verify']}"
                    verify_items.append(rule["verify"])
                else:
                    entry["detail"] = "材料已上传"
            checklist.append(entry)

        elif ct == "qichacha":
            ok, _ = evaluate_rule(rule, supplier)
            if ok:
                entry["detail"] = "天眼查初判通过（无处罚/失信记录）"
            elif supplier.get("tyc_checked", False):
                # 天眼查有数据且显示有问题 → 转人工（核对表：是则转人工）
                entry["status"] = "fail"
                entry["detail"] = "天眼查显示存在处罚/失信记录，需人工复核"
                verify_items.append(f"{name}：天眼查显示存在处罚/失信记录，需人工复核")
            else:
                # 天眼查无数据（合作意向/境外常见）→ 待核验
                entry["status"] = "pending"
                v = rule.get("verify", name)
                entry["detail"] = f"天眼查无数据；待核验：{v}"
                verify_items.append(v)
            checklist.append(entry)

    return checklist, auto_failed_rules, missing_rules, verify_items


def evaluate_rule(rule, supplier):
    """评估单条规则，返回 (通过?, 不通过原因)"""
    expr = rule.get("rule", "False")
    try:
        ok = safe_eval_rule(expr, supplier)
    except Exception:
        ok = False
    return ok, rule.get("name", "")


def check_auto_rules(supplier, rules_list):
    """
    只检查 check_type=auto 的规则（skip/qichacha/material 都不算）
    返回: (auto全通过?, 失败的规则dict列表)
    """
    failed = []
    for rule in rules_list:
        if rule.get("check_type") != "auto":
            continue
        ok, name = evaluate_rule(rule, supplier)
        if not ok:
            failed.append(rule)
    return len(failed) == 0, failed


# ============================================================
# 审批意见生成（v3 两阶段：齐全性→准确性）
# 业务要求：缺材料→提醒补充；材料有误→提醒什么样的材料才是正确的
# ============================================================
# 每个material审查项对应的"正确材料"描述（缺/传错时都提醒这个）
MATERIAL_REQUIREMENTS = {
    "A01 法律主体资格": "营业执照副本（须持有统一社会信用代码，且执照信息与基本信息栏的供应商名称、注册资本、法人一致）",
    "A02 法人身份证明": "法人身份证正反面（或护照首页，须在有效期内）",
    "A03 纳税信用等级": "国家税务总局网站或信用中国下载的纳税信用等级证明（须为C级及以上）",
    "A07 售后服务": "正规机构出具的售后服务五星认证或厂家出具的售后服务证明函",
    "A08 资金财务状况": "上年度经审计的财务报表（2026年须提交2025年财报）",
    "C1_02 ISO 9001": "ISO 9001质量管理体系认证证书（须为该公司认证且在有效期内）",
    "C1_03 ISO 14001": "ISO 14001环境管理体系认证证书（须为该公司认证且在有效期内）",
    "C1_04 ISO 45001": "ISO 45001职业健康安全管理体系认证证书（须为该公司认证且在有效期内）",
    "C1_05 生产许可证": "生产许可证或强制认证证明（须在有效期内）",
    "D1_03 ISO 9001": "ISO 9001质量管理体系认证证书（可上传代理厂家的认证，须在有效期内）",
    "D1_04 ISO 14001": "ISO 14001环境管理体系认证证书（须为该贸易公司自己的认证且在有效期内）",
    "D1_05 ISO 45001": "ISO 45001职业健康安全管理体系认证证书（须为该贸易公司自己的认证且在有效期内）",
    "D1_06 授权资质": "产品生产企业的代理协议或产品销售授权资质（授权一方须为该贸易公司且在有效期内）",
}


def generate_opinion_v4(supplier, auto_failed_rules, material_failed_rules, verify_items,
                        type_desc=""):
    """
    生成审批意见（v4统一汇总逻辑，业务 8/31确认）

    全部检查跑完后统一调用：
    - 有问题（auto不达标）或缺材料 → 一条退回意见，统一列出全部需补充/更正项
      （先写"退回。"再列所有需要补充的文件/修改的文件）
    - 材料齐全但有未核验项 → 转人工，列出核验点
    - 全部通过 → 建议同意（初期不自动通过，需人工核实）

    参数:
        auto_failed_rules: auto规则失败列表（规则dict，含fail_action）
        material_failed_rules: 材料缺失列表（规则dict）
        verify_items: 准确性核验点描述列表
    返回: (意见文本, 决策reject/manual/recommend)
    """
    sname = supplier.get("name", "")

    # ---- 统一汇总：全部检查完毕后一次性生成意见 ----
    # 优先级：存在问题(auto失败) + 缺材料 → 合并成一条退回意见
    #        无缺失但有未核验项 → 转人工
    #        全部通过 → 建议同意（初期需人工核实，不自动通过）

    has_problems = bool(auto_failed_rules)
    has_missing = bool(material_failed_rules)

    if has_problems or has_missing:
        opinion_parts = ["退回。"]
        idx = 1

        # 存在问题（auto硬性指标，如注册资本不足）
        if auto_failed_rules:
            problem_lines = []
            for rule in auto_failed_rules:
                rid = rule.get("id", "")
                if rid == "C1_01":
                    cap = supplier.get("registered_capital", 0)
                    problem_lines.append(f"注册资本{cap:g}万元，未达到500万元要求")
                else:
                    problem_lines.append(f"{rid} {rule.get('name','')}未达标")
            opinion_parts.append("存在问题：")
            for line in problem_lines:
                opinion_parts.append(f"  {idx}. {line}")
                idx += 1

        # 需补充的资质文件（材料缺失，统一列出全部）
        if material_failed_rules:
            # 按核对表审核顺序排列
            sorted_rules = sorted(material_failed_rules, key=lambda r: r.get("order", 99))

            iso_missing = [r for r in sorted_rules if "ISO" in r.get("name", "")]
            other_missing = [r for r in sorted_rules if "ISO" not in r.get("name", "")]

            opinion_parts.append("请补充资质文件：")

            # ISO三认证合并成一条
            if iso_missing:
                iso_names = []
                for r in iso_missing:
                    n = r.get("name", "")
                    for code in ("9001", "14001", "45001"):
                        if code in n:
                            iso_names.append(f"ISO {code}")
                if iso_names:
                    desc = "、".join(dict.fromkeys(iso_names)) + " 认证证书"
                    if type_desc and "贸易" in type_desc:
                        desc += "（ISO 9001可上传代理厂家的认证，14001/45001须为贵司自己的认证；均须在有效期内）"
                    else:
                        desc += "（须为贵司认证且在有效期内）"
                    opinion_parts.append(f"  {idx}. {desc}")
                    idx += 1

            for rule in other_missing:
                rid_name = f"{rule.get('id','')} {rule.get('name','')}"
                desc = rule.get("requirement") or MATERIAL_REQUIREMENTS.get(rid_name, rid_name)
                if rule.get("id") == "C1_05":
                    why = supplier.get("production_license_reason", "")
                    if why:
                        desc = f"{desc}【{why}】"
                opinion_parts.append(f"  {idx}. {desc}")
                idx += 1

        opinion = "\n".join(opinion_parts)
        return opinion, "reject"

    # ---- 材料齐全但有未核验项 → 转人工 ----
    if verify_items:
        seen = set()
        unique_verifies = []
        for v in verify_items:
            if v not in seen:
                unique_verifies.append(v)
                seen.add(v)
        opinion_parts = ["转人工复核。材料齐全，以下核验点需人工/后续自动核验："]
        for i, v in enumerate(unique_verifies, 1):
            opinion_parts.append(f"  {i}. {v}")
        opinion = "\n".join(opinion_parts)
        return opinion, "manual"

    # ---- 全部通过 → 建议同意（业务确认：初期不自动通过，需人工核实）----
    opinion = ("建议同意。该供应商材料齐全、各项核验通过。"
               "（初期设置：需人工核实后再执行通过操作）")
    return opinion, "recommend"


def is_special_category(supplier):
    """
    判断供应商是否属于第三部分集团独有类别
    返回: (是否特殊类别, 类别名称)
    """
    sup_type = supplier.get("sup_type", "")
    name_lower = sup_type.lower()
    
    special_keywords = {
        "平台": "平台类",
        "租赁": "租赁商",
        "云服务": "云服务商",
        "软件服务": "软件服务商",
        "运输": "运输服务商",
        "承运": "运输服务商",
        "差旅": "差旅服务商",
        "审计": "审计/评估机构",
        "评估": "审计/评估机构",
        "保险": "保险机构",
        "投行": "投资银行类",
        "投资银行": "投资银行类",
    }
    
    for keyword, category in special_keywords.items():
        if keyword in sup_type:
            return True, category
    
    return False, None


def determine_supplier_rules(supplier, cfg):
    """
    根据供应商类型确定适用的规则集
    核对表v3：国外供应商已在B01分流（不会走到这）；港澳台视同境内流程
    返回: (适用规则列表, 类型描述)
    """
    part1 = cfg.get("part_1_basic", {})
    part2 = cfg.get("part_2_by_type", {})

    # 港澳台视同境内（is_foreign=False 用境内规则）；纯境外规则集仅B01且已分流
    is_foreign = supplier.get("is_foreign", False)

    # 第一部分：基本要求
    if is_foreign:
        basic_rules = part1.get("overseas", [])
        region_label = "境外"
    else:
        basic_rules = part1.get("domestic", [])
        region_label = "境内" if not supplier.get("is_overseas") else "港澳台"

    # 第二部分：按类型选查
    sup_type_name = supplier.get("sup_type_name", "")

    type_rules = []
    type_label = ""

    if "厂家" in sup_type_name or "生产商" in sup_type_name or "制造商" in sup_type_name:
        if is_foreign:
            type_rules = part2.get("overseas_manufacturer", [])
            type_label = "境外厂家"
        else:
            type_rules = part2.get("domestic_manufacturer", [])
            type_label = "境内厂家"
    elif "贸易" in sup_type_name or "经销" in sup_type_name or "代理" in sup_type_name:
        if is_foreign:
            type_rules = part2.get("overseas_trader", [])
            type_label = "境外贸易商"
        else:
            type_rules = part2.get("domestic_trader", [])
            type_label = "境内贸易商"

    all_rules = basic_rules + type_rules
    return all_rules, f"{region_label}{'·' + type_label if type_label else ''}"


# ============================================================
# 主流程
# ============================================================
def _print_result(sname, todo_id, type_desc, mat_cls, certifications, opinion, decision,
                  supplier=None, checklist=None):
    """统一输出审批结果（核查清单 + 审批意见）"""
    print(f"\n{'='*60}")
    print(f"供应商: {sname} | todoId: {todo_id} | 类型: {type_desc}")
    decision_str = {
        "approve": "自动通过",
        "recommend": "建议同意（待人工核实）",
        "reject": "退回",
        "manual": "转人工",
    }.get(decision, decision)
    print(f"决策: {decision_str}")
    if supplier:
        print(f"注册资本: {supplier.get('registered_capital', 0)}万元 | "
              f"法人: {supplier.get('legal_person', '')} | 经营范围许可证: "
              f"{'需要' if supplier.get('needs_production_license') else '不需要'}")
    # 核查清单（哪项通过/不通过/待核，以及原因）
    if checklist:
        icons = {"pass": "[✓]", "fail": "[✗]", "pending": "[待核]", "skip": "[跳过]"}
        print(f"--- 核查清单 ---")
        for c in checklist:
            icon = icons.get(c.get("status"), "[?]")
            print(f"  {icon} {c.get('id','')} {c.get('name','')} — {c.get('detail','')}")
    if mat_cls and mat_cls.get("details"):
        print(f"--- 已上传材料 ({len(mat_cls.get('details', []))} 类) ---")
        for fname, tname, fdesc, ctypes in mat_cls["details"]:
            ctype_str = ", ".join(ctypes) if ctypes else "未分类"
            desc_str = f" | 说明: {fdesc}" if fdesc else ""
            print(f"  [{ctype_str}] {tname} → {fname}{desc_str}")
    if certifications:
        print(f"--- 认证: {', '.join(certifications)} ---")
    print(f"{'='*60}")
    print(f"审批意见：")
    print(opinion)
    print(f"{'='*60}\n")


# ============================================================
# 断点续跑进度（progress_v4.jsonl，处理完一条追加一条）
# ============================================================
def _load_processed():
    """读取已处理进度: {todoId: 记录dict}"""
    processed = {}
    if PROGRESS_FILE.exists():
        for line in PROGRESS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("todoId"):
                    processed[rec["todoId"]] = rec
            except Exception:
                pass
    return processed


def _record_progress(todo_id, name, decision):
    """追加一条已处理记录（异常的不记录，下次续跑自动重试）"""
    rec = {
        "todoId": todo_id,
        "name": name,
        "decision": decision,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    with open(PROGRESS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ============================================================
# 供应商数据缓存（fetch时写入；阶段2离线重决策不碰scpma，绕开WAF）
# ============================================================
def _load_cache():
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"缓存文件损坏，忽略: {e}")
    return {}


def _save_cache(cache):
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def _cache_supplier(cache, todo_id, todo_meta, supplier, mat_cls):
    """缓存组装好的供应商数据（set转list以便JSON序列化）"""
    sup = dict(supplier)
    cm = sup.get("classified_materials")
    if isinstance(cm, set):
        sup["classified_materials"] = sorted(cm)
    details = [
        {"fileName": d[0], "typeName": d[1], "desc": d[2],
         "types": sorted(d[3]) if isinstance(d[3], set) else list(d[3])}
        for d in mat_cls.get("details", [])
    ]
    cache[todo_id] = {
        "todo": todo_meta,
        "supplier": sup,
        "materials_detail": details,
        "certifications": mat_cls.get("certifications", []),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def run():
    cfg = load_rules()
    log.info("=== 供应商自动审批开始（中港采购发〔2025〕161号标准）===")
    log.info(f"DRY_RUN={DRY_RUN}")

    # 1. 拉取待办列表
    try:
        todo_result = fetch_pending_todos()
    except SessionExpiredError as e:
        log.error(f"[登录过期] {e}")
        log.error("修复步骤：浏览器打开 scpma.iccec.cn 并确认已登录 → F12 → Network → "
                  "任选一个请求 → 复制请求头里的 Cookie 和 APP_TOKEN → 更新 .env 中的 "
                  "SCPMA_COOKIE / APP_TOKEN → 重新运行")
        sys.exit(3)   # 登录过期专用退出码，便于自动化任务识别并提醒更新 cookie
    todo_list = todo_result.get("data", {}).get("rows", [])
    if not isinstance(todo_list, list):
        log.error(f"待办列表格式异常: {json.dumps(todo_result, ensure_ascii=False)[:500]}")
        return

    log.info(f"待审批供应商 {len(todo_list)} 条")

    # 断点续跑：跳过 progress_v4.jsonl 里已处理的 todoId
    processed = _load_processed()
    if processed:
        log.info(f"断点续跑: 进度文件已有 {len(processed)} 条记录，本次自动跳过")

    rejected, reviewed, errored, skipped = 0, 0, 0, 0
    waf_blocked = False
    session_expired = False
    cache = _load_cache()
    if FETCH_ONLY:
        log.info(f"FETCH_ONLY 模式：只拉数据入缓存，不做决策"
                 f"{'（指定 ' + str(len(FETCH_IDS)) + ' 家）' if FETCH_IDS else '（全部）'}")
    elif cache:
        log.info(f"数据缓存: 已有 {len(cache)} 家（阶段2可用）")

    for item in todo_list:
        # asca返回的ID字段是 id，不是 todoId；businessBillId/procInstId/actInstId/taskId 也在列表里
        todo_id = item.get("id") or item.get("todoId")
        bill_id = item.get("businessBillId", "")
        proc_inst_id = item.get("procInstId", "")
        act_inst_id = item.get("actInstId", "")
        act_inst_name = item.get("actInstName", "")
        task_id = item.get("taskId", "")
        bill_name = item.get("businessBillName", "")  # 审批类型：合作意向/信息变更等
        apply_unit = item.get("applyUnitName", "") or item.get("applyUserName", "")
        
        if not todo_id:
            log.warning(f"跳过无 id 的项")
            continue

        # 断点续跑：已处理的直接跳过（不耗请求）
        # fetch-only 模式不受进度限制（缓存需要数据，即使已决策过）
        if not FETCH_ONLY and todo_id in processed:
            prev = processed[todo_id]
            log.info(f"[续跑跳过] #{todo_id} {apply_unit}"
                     f"（{prev.get('ts', '')} 已决策: {prev.get('decision', '')}）")
            skipped += 1
            continue

        # fetch-only 指定名单过滤
        if FETCH_ONLY and FETCH_IDS and todo_id not in FETCH_IDS:
            continue
        # fetch-only 且已有缓存（且未指定强制刷新）→ 跳过
        if FETCH_ONLY and todo_id in cache:
            log.info(f"[已缓存跳过] #{todo_id} {apply_unit}")
            skipped += 1
            continue

        log.info(f"--- 待办 #{todo_id}: {apply_unit} ({bill_name}) ---")

        # 供应商之间留足间隔，避免 scpma 的 WAF 限流（实测需要≥15s）
        time.sleep(SUPPLIER_INTERVAL)

        try:
            # 2. 获取待办详情（从这里拿到真正的 supInfoApplyId = businessBillId）
            bill_type = item.get("businessBillType", "")  # P0702=变更, P0704=合作意向, P0701=注册(推测)
            bill_name = item.get("businessBillName", "")
            
            todo_detail = query_todo_detail(todo_id)
            td_data = todo_detail.get("data", {})
            # queryTodomsgDetailById 返回的 businessBillId 才是真正的 supInfoApplyId
            sup_info_apply_id = td_data.get("businessBillId", "")
            # 补充审批流程字段
            proc_inst_id = td_data.get("procInstId", "") or proc_inst_id
            act_inst_id = td_data.get("actInstId", "") or act_inst_id
            act_inst_name = td_data.get("actInstName", "") or act_inst_name
            task_id = td_data.get("taskId", "") or task_id

            if not sup_info_apply_id:
                log.warning(f"[跳过] todoId={todo_id} 无法获取 supInfoApplyId")
                continue

            # 3. 查询供应商基本信息（三种审批类型都查）
            # 业务确认：三种审核流程一样，都能看到基本信息
            # 参数规律：P0704(合作意向)=tempOrFormalFlag1, P0701/P0702=tempOrFormalFlag0
            time.sleep(REQ_INTERVAL)   # scpma 请求间隔，防WAF限流
            base_info = query_supplier_base_info(sup_info_apply_id, bill_type)
            base_bo = base_info.get("data", {}).get("supRegistBaseInfoBO", {}) or {}
            tyc_raw = base_bo.get("tycInfo", "{}")
            try:
                tyc = json.loads(tyc_raw) if tyc_raw and tyc_raw != "" else {}
            except Exception:
                tyc = {}

            # 资质文件列表（⚠️ 用 supinfoId 参数，返回 supFilesBOList）
            time.sleep(REQ_INTERVAL)   # scpma 请求间隔，防WAF限流
            qual_files = query_qualification_files(sup_info_apply_id, bill_type)
            file_list = qual_files.get("data", {}).get("supFilesBOList", [])
            if not isinstance(file_list, list):
                file_list = []

            # 材料分类（附件名称+文件名+附件说明 三结合）
            mat_cls = classify_uploaded_materials(file_list)
            classified_materials = mat_cls["materials"]

            # 4. 组装 supplier 字典（用真实字段名 + 材料分类结果）
            reg_capl_str = base_bo.get("regCapl", "0")
            found_time = base_bo.get("foundTime", "")

            established_years = 0
            if found_time:
                try:
                    found_year = int(found_time[:4])
                    established_years = datetime.now().year - found_year
                except Exception:
                    pass

            # 判断境内/境外/国外（核对表v3：港澳台视同境内流程，国外供应商走B01分流）
            is_overseas = base_bo.get("isOverseas", 0) == 1
            country = str(base_bo.get("countryName", "") or "")
            hmt_keywords = ("香港", "澳门", "台湾", "Hong Kong", "Macao", "Taiwan", "港澳台")
            is_hmt = is_overseas and any(k in country for k in hmt_keywords)
            is_foreign = is_overseas and not is_hmt   # 国外供应商（非港澳台）
            region = country if is_overseas else "中国"

            # 从材料分类提取 ISO 认证
            certifications = mat_cls["certifications"]

            # 天眼查数据提取
            tyc_violations = tyc.get("penaltyCount", 0) or 0
            tyc_litigation = tyc.get("litigationCount", 0) or 0
            tyc_tax_rating = tyc.get("taxCreditRating", "")
            tyc_dishonesty = tyc.get("dishonestyCount", 0) or 0
            tyc_checked = bool(tyc)   # tycInfo 是否有数据

            # 是否合作意向（P0704=合作，已入库的供应商申请合作）
            is_cooperation = (bill_type == "P0704")

            # 是否生产厂家/制造商（A07售后服务仅厂家需要查）
            sup_type_name = base_bo.get("supTypeName", "") or ""
            is_manufacturer = any(k in sup_type_name for k in ("厂家", "生产商", "制造商", "生产"))

            # 经营范围判断是否需要生产许可证/强制认证（C1_05条件检查）
            needs_license, license_reason = needs_production_license_by_scope(base_bo.get("busiScope", ""))
            if not is_manufacturer:
                # 贸易商不需要生产许可证（业务确认：一般只有生产商、厂家才需要）
                needs_license = False
                license_reason = ""

            # 财报指标：有财报材料时暂用占位值（阶段2核验点会提示人工核验，TextIn接入后自动解析）
            has_financial_report = "financial_report" in classified_materials

            supplier = {
                "name": base_bo.get("supShort", "") or tyc.get("supName", ""),
                # 信用代码：优先base_bo.creditCode，其次tycInfo，再次orgCode
                "social_credit_code": base_bo.get("creditCode", "") or tyc.get("creditCode", "") or base_bo.get("orgCode", ""),
                "credit_code": base_bo.get("creditCode", "") or tyc.get("creditCode", "") or base_bo.get("orgCode", ""),
                "registered_capital": float(reg_capl_str) if reg_capl_str else 0,
                "actual_capital": float(base_bo.get("factCapl", "0") or "0") if base_bo.get("factCapl") else 0,
                "legal_person": base_bo.get("legalContactName", ""),
                "found_time": found_time,
                "established_years": established_years,
                "company_type": base_bo.get("companyTypeName", ""),
                "sup_type": base_bo.get("supTypeName", ""),
                "sup_type_name": sup_type_name,
                "state": base_bo.get("freeordisStateName", ""),
                "in_blacklist": base_bo.get("isInIndy", 0) != 0,
                "money_type": base_bo.get("moneyTypeName", ""),
                "busi_scope": base_bo.get("busiScope", ""),
                # 经营场所：用 officeAddr（从Response确认）
                "business_address": base_bo.get("officeAddr", "") or base_bo.get("homeAddr", ""),
                "is_overseas": is_overseas,
                "is_hmt": is_hmt,
                "is_foreign": is_foreign,
                "is_cooperation": is_cooperation,
                "is_manufacturer": is_manufacturer,
                "region": region,
                "country_name": base_bo.get("countryName", ""),
                # ISO认证（从资质文件分类提取）
                "certifications": certifications,
                # 材料分类结果（齐全性检查用）
                "has_business_license": "business_license" in classified_materials,
                "has_legal_id_material": "legal_person_id" in classified_materials,
                "has_production_license": "production_license" in classified_materials,
                "has_authorization": "authorization" in classified_materials,
                "has_after_sales_cert": "after_sales_cert" in classified_materials,
                "has_tax_cert_material": "tax_cert" in classified_materials,
                "tax_credit_rating_ok": "tax_credit" in classified_materials,
                "has_financial_report": has_financial_report,
                "has_inspection_cert": "inspection_cert" in classified_materials,
                # 2026-09-04 保密合规改造：财报敏感不发 AI，由企查查公查数据判定
                # A08 规则改 check_type=qichacha，qcc_finance_ok 由 enhance_checklist_with_qcc 阶段写入
                "qcc_finance_ok": False,
                # 生产许可证条件检查
                "needs_production_license": needs_license,
                "production_license_reason": license_reason,
                # 财报指标（占位，阶段2核验点提醒人工，TextIn接入后自动解析）
                "asset_liability_ratio": None,
                "operating_cash_flow": None,
                "current_ratio": None,
                # 材料详情
                "qualification_files": [f.get("uploadId") for f in file_list if isinstance(f, dict)],
                "classified_materials": classified_materials,
                # 天眼查数据（A10商业信誉初判用；合作意向/境外可能为空）
                "tyc_checked": tyc_checked,
                "tax_credit_rating": tyc_tax_rating,
                "has_violation_3y": tyc_violations > 0,
                "is_dishonest": tyc_dishonesty > 0,
                "has_major_litigation_with_ccc": tyc_litigation > 0 and "中交" in str(tyc),
                # 其他
                "suptype_qual_id": base_bo.get("suptypeQualId", "1"),
                "suptype_qual_name": base_bo.get("suptypeQualName", "一般供应商"),
                "ccc_supplier_level": base_bo.get("suptypeQualName", ""),
                # 流程相关
                "todo_id": todo_id,
                "supinfo_id": sup_info_apply_id,
                "bill_id": bill_id,
                "bill_type": bill_type,
            }

            sname = supplier["name"] or todo_id

            # ---- 数据缓存（阶段2离线重决策用，不碰scpma）----
            _cache_supplier(cache, todo_id, {
                "todoId": todo_id, "billType": bill_type, "billName": bill_name,
                "applyUnitName": apply_unit, "businessBillId": sup_info_apply_id,
                "procInstId": proc_inst_id, "actInstId": act_inst_id,
                "actInstName": act_inst_name, "taskId": task_id,
            }, supplier, mat_cls)
            if FETCH_ONLY:
                log.info(f"[已缓存] #{todo_id} {sname}（共 {len(cache)} 家）")
                continue

            # ============================================================
            # 决策流程 v4（统一汇总：全部检查跑完 → 一次性生成意见）
            # ============================================================

            # ---- 分流1：国外供应商（B01跳过项，分流由代码处理）----
            if is_foreign:
                # 数据冲突保护（2026-09-01发现）：isOverseas=1 但国家为"China/中国/空"
                # 属于系统数据自相矛盾（可能是国内公司误走境外渠道注册），
                # 自动退回话术可能不适用 → 转人工核实
                if (not country) or country in ("China", "中国", "CHINA"):
                    opinion = ("转人工复核。系统数据异常：该供应商标记为境外注册，"
                               "但国家信息为中国（或为空），请人工核实其注册渠道与主体信息后，"
                               "再决定是否按国外或国内标准审查。")
                    decision = "manual"
                elif bill_type == "P0701":
                    # 注册审批 + 国外供应商 → 自动退回（固定话术，核对表原文）
                    opinion = "退回。国外供应商请通过中交海外采购专区进行注册申请。"
                    decision = "reject"
                else:
                    # 非注册审批 + 国外供应商 → 转人工
                    opinion = f"转人工复核。国外供应商（审查类型：{bill_name or bill_type}），需人工审核。"
                    decision = "manual"
                _print_result(sname, todo_id, f"国外供应商({country})", mat_cls, certifications, opinion, decision)
                log.info(f"决策: {decision} | {sname}")
                if decision == "reject":
                    if not DRY_RUN:
                        execute_approval(
                            proc_inst_id, act_inst_id, act_inst_name,
                            task_id, bill_id, opinion,
                            oper_code=OPER_CODE_REJECT,
                        )
                    rejected += 1
                else:
                    reviewed += 1
                _record_progress(todo_id, sname, decision)
                continue

            # ---- 分流2：港澳台供应商 → 转人工（业务 8/31确认）----
            if is_hmt:
                opinion = "转人工复核。港澳台地区供应商需人工审批。"
                decision = "manual"
                _print_result(sname, todo_id, f"港澳台({country})", mat_cls, certifications, opinion, decision)
                log.info(f"决策: {decision} | {sname}")
                reviewed += 1
                _record_progress(todo_id, sname, decision)
                continue

            # ---- 分流3：集团独有类别（第三部分，全部转人工）----
            is_special, special_category = is_special_category(supplier)
            if is_special:
                opinion = f"转人工复核。该供应商属于集团独有类别（{special_category}），审查标准较复杂，需人工审核。"
                decision = "manual"
                _print_result(sname, todo_id, special_category, mat_cls, certifications, opinion, decision)
                log.info(f"决策: {decision} | {sname}")
                reviewed += 1
                _record_progress(todo_id, sname, decision)
                continue

            # ---- 确定适用规则集 ----
            applicable_rules, type_desc = determine_supplier_rules(supplier, cfg)
            log.info(f"[审批] {sname} | 类型: {type_desc} | 适用 {len(applicable_rules)} 条规则")

            # ---- 全部检查统一跑完（齐全性+准确性），收集所有问题 ----
            checklist, auto_failed_rules, missing_rules, verify_items = build_checklist(
                supplier, applicable_rules)

            # ---- 统一生成审批意见（v4：不缺一项就退，全部查完再出意见）----
            opinion, decision = generate_opinion_v4(
                supplier, auto_failed_rules, missing_rules, verify_items,
                type_desc=type_desc,
            )

            # ---- 输出：核查清单 + 审批意见 ----
            _print_result(sname, todo_id, type_desc, mat_cls, certifications, opinion, decision,
                          supplier=supplier, checklist=checklist)
            log.info(f"决策: {decision} | {sname}")
            log.info(f"审批意见:\n{opinion}")
            if checklist:
                log.info("核查清单:\n" + "\n".join(
                    f"  [{c['status']}] {c['id']} {c['name']} — {c['detail']}" for c in checklist))

            # ---- 执行审批操作（初期：只执行退回；通过/转人工均不自动执行）----
            # 业务 8/31确认：初期不设置自动通过项，全达标也需人工核实
            if decision == "recommend":
                # 建议同意 → 输出意见供人工核实，不执行任何操作
                log.info(f"[建议同意·待人工核实] {sname} (todoId={todo_id})")
                reviewed += 1
            elif decision == "reject":
                # 退回操作（DRY_RUN模式下只输出意见不执行）
                if not DRY_RUN:
                    execute_approval(
                        proc_inst_id, act_inst_id, act_inst_name,
                        task_id, bill_id, opinion,
                        oper_code=OPER_CODE_REJECT,
                    )
                log.info(f"[退回] {sname} (todoId={todo_id})")
                rejected += 1
            else:
                # 转人工，不执行审批操作
                reviewed += 1

            _record_progress(todo_id, sname, decision)

        except SessionExpiredError as e:
            log.error(f"[登录过期] {e}")
            log.error("处理中途登录态失效，本条未完成（异常不记进度，续跑会自动重试）。"
                      "请更新 .env 中的 SCPMA_COOKIE / APP_TOKEN 后重新运行。")
            session_expired = True
            break
        except WafBlockedError:
            log.error("[熔断] scpma WAF 持续封禁，中止本次运行。"
                      "进度已保存，重新运行将自动从断点续跑。")
            waf_blocked = True
            break
        except Exception as e:
            log.error(f"[异常] todoId={todo_id}: {e}")
            errored += 1

    # 保存数据缓存（fetch-only 与正常跑批都保存）
    if cache:
        _save_cache(cache)

    if FETCH_ONLY:
        log.info(f"=== fetch-only 结束: 本次新缓存 {len(cache) - skipped if skipped else len(cache)} 家，"
                 f"缓存总计 {len(cache)} 家（{CACHE_FILE.name}）===")
        # 2026-09-04 修复：fetch-only 模式登录过期时必须以退出码 3 结束，
        # 否则 webui 误判为成功 → 继续跑 OCR/stage2 → 最终报"无结果"却不说原因
        if session_expired:
            sys.exit(3)
        if waf_blocked:
            sys.exit(2)
        return

    total_done = len(processed) + rejected + reviewed
    log.info(f"=== 结束: 退回 {rejected} | 转人工/待人工核实 {reviewed} | "
             f"异常 {errored} | 续跑跳过 {skipped} ===")
    log.info(f"累计已处理 {total_done}/{len(todo_list)} 条"
             f"（进度文件: {PROGRESS_FILE.name}，删除可重新全量跑）")
    log.info(f"日志文件: {LOG_FILE}")
    if waf_blocked:
        sys.exit(2)
    if session_expired:
        sys.exit(3)   # 登录过期专用退出码，便于自动化任务识别并提醒更新 cookie


# ============================================================
# 阶段2：企查查核验结果合并（离线，不碰scpma）
# ============================================================
# 中交系统关联关键词（股东/实控人名称匹配 → 需回避的关联交易风险）
ZHONGJIAO_KEYWORDS = (
    "中交", "中国交通建设", "CCCC", "中港", "中国港湾", "CHEC",
    "振华重工", "ZPMC", "一航局", "二航局", "三航局", "四航局",
    "一公局", "二公局", "三公局", "四公局", "公规院", "水规院",
    "疏浚", "航道局",
)

# 财务指标阈值（业务 9/1确认：资产负债率≤65%、流动比率≥100%、现金流>0，不符转人工）
# 数据来源：供应商上传的经审计财报（TextIn解析），不用企查查财务数据（普遍过期）
FIN_RATIO_LIMITS = {"资产负债率": 65.0, "流动比率": 100.0}


def _parse_capital_wan(text):
    """企查查注册资本文本 → 万元数值。'2000万元'→2000.0, '500万美元'→None(币种不符)"""
    if not text:
        return None
    t = str(text).replace(",", "").strip()
    if "万美元" in t or "万港" in t or "万欧元" in t or "万日元" in t:
        return None
    import re as _re
    m = _re.search(r"([\d.]+)\s*万", t)
    if m:
        return float(m.group(1))
    m = _re.search(r"^([\d.]+)$", t)
    if m:
        return float(m.group(1))
    return None


def _find_metric(obj, keyword):
    """递归在企查查财务数据里找含关键词的字段值"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if keyword in str(k) and isinstance(v, (int, float)):
                return v
            if isinstance(v, str):
                try:
                    fv = float(v.replace("%", "").replace(",", ""))
                    if keyword in str(k):
                        return fv
                except ValueError:
                    pass
            r = _find_metric(v, keyword)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for it in obj:
            r = _find_metric(it, keyword)
            if r is not None:
                return r
    return None


def enhance_checklist_with_qcc(checklist, supplier, qcc):
    """
    企查查核验结果合并进核查清单（阶段2）。
    保守原则（业务 8/31确认）：企查查发现的问题一律转人工核实，不直接退回。

    qcc 结构（qcc_results.json 每条）:
      reg_info: 工商登记信息dict（企业名称/统一社会信用代码/法定代表人/注册资本/登记状态...）
      accuracy: {"核验结果是否一致": "一致"/"不一致"}
      shareholders: [{"股东名称","持股比例",...}]
      actual_controller: {"实际控制人信息": [{"实际控制人名称","直接持股比例",...}]}
      financial: 财务数据dict 或 {"搜索结果": "未发现任何记录"}

    更新 A01(执照核验)/A02(股权穿透)/A10(商业信誉) 三项，
    A08 财务指标不用企查查（年报数据过期，业务 9/1确认），由 TextIn 解析上传财报（阶段3）。
    返回 (checklist, qcc_issues) — qcc_issues 为需人工核实的问题列表
    """
    reg = qcc.get("reg_info") or {}
    acc = qcc.get("accuracy") or {}
    shareholders = qcc.get("shareholders") or []
    controller = qcc.get("actual_controller") or {}

    qcc_issues = []      # 需人工核实的问题（进意见）

    for c in checklist:
        cid = c.get("id")

        # ---- A01 法律主体资格：执照真伪 + 基本信息一致性 ----
        if cid == "A01" and reg:
            issues = []
            if acc:
                if acc.get("核验结果是否一致") != "一致":
                    issues.append("企业名称/统一社会信用代码与工商登记不一致")
            sys_legal = supplier.get("legal_person", "")
            qcc_legal = reg.get("法定代表人", "")
            if sys_legal and qcc_legal and sys_legal != qcc_legal:
                issues.append(f"法人不一致（审批系统:{sys_legal} / 工商登记:{qcc_legal}）")
            qcc_cap = _parse_capital_wan(reg.get("注册资本", ""))
            sys_cap = supplier.get("registered_capital", 0) or 0
            if qcc_cap and sys_cap and abs(qcc_cap - sys_cap) / max(sys_cap, 1) > 0.01:
                issues.append(f"注册资本不一致（审批系统:{sys_cap:g}万 / 工商登记:{qcc_cap:g}万）")
            status = reg.get("登记状态", "")
            if status and not any(k in status for k in ("存续", "在业", "开业")):
                issues.append(f"工商登记状态异常：「{status}」")
            if issues:
                c["status"] = "fail"
                c["detail"] = "企查查核验发现问题：" + "；".join(issues)
                qcc_issues.append("法律主体资格：" + "；".join(issues))
            else:
                c["status"] = "pass"
                c["detail"] = (f"企查查核验通过：名称/信用代码一致，法定代表人{qcc_legal}，"
                               f"注册资本{reg.get('注册资本', '')}，登记状态「{status}」")

        # ---- A02 法人身份证明：股权穿透查中交关联 ----
        elif cid == "A02" and (reg or shareholders):
            zj_hits = []
            for sh in shareholders:
                nm = str(sh.get("股东名称", ""))
                if any(kw in nm for kw in ZHONGJIAO_KEYWORDS):
                    zj_hits.append(f"股东「{nm}」（持股{sh.get('持股比例', '?')}）")
            ctrl_list = controller.get("实际控制人信息") or []
            ctrl_name = str(ctrl_list[0].get("实际控制人名称", "")) if ctrl_list else ""
            if ctrl_name and any(kw in ctrl_name for kw in ZHONGJIAO_KEYWORDS):
                zj_hits.append(f"实际控制人「{ctrl_name}」")
            if zj_hits:
                c["status"] = "fail"
                c["detail"] = "企查查股权穿透发现中交系统关联：" + "、".join(zj_hits)
                qcc_issues.append("股权穿透发现中交关联：" + "、".join(zj_hits))
            else:
                sh_desc = "、".join(
                    f"{sh.get('股东名称', '')}({sh.get('持股比例', '')})"
                    for sh in shareholders[:4])
                ctrl_desc = f"，实际控制人{ctrl_name}" if ctrl_name else ""
                c["status"] = "partial"
                c["detail"] = (f"企查查股权穿透：未发现中交系统关联"
                               f"（股东：{sh_desc}{ctrl_desc}）；"
                               f"身份证有效期仍需人工核验")

        # ---- A08 资金财务状况（2026-09-04 保密合规改造）----
        # 改走企查查财务数据（公开披露），不再依赖 TextIn 解析供应商上传的财报（敏感数据）
        elif cid == "A08":
            financial = qcc.get("financial") or {}
            # 适配企查查多种数据形态：
            #   {"搜索结果": "未发现任何记录"} → 非上市公司常见
            #   {"财务数据信息": [{"报告期": ..., "指标详情": {"分析数据": {"偿还能力": {资产负债率, 流动比率, ...}}}}]}
            #   {"资产负债率": ..., "流动比率": ..., "经营性现金流": ...} → 扁平指标
            no_data = (
                not financial
                or "搜索结果" in financial
                or "财务数据信息" not in financial
            )
            if no_data:
                c["status"] = "manual"
                c["detail"] = ("企查查未查到上年度财报数据（非上市公司常见，公开披露数据有限），"
                               "已按保密合规要求不再 OCR 解析上传财报；转人工要求供应商补交经审计财报")
                qcc_issues.append("A08 财报：企查查无数据，转人工要求补交")
            else:
                # 提取嵌套指标——取最新报告期（财务数据信息[0]）
                info_list = financial.get("财务数据信息") or []
                if not info_list:
                    c["status"] = "manual"
                    c["detail"] = "企查查财报数据为空，转人工要求供应商补交"
                    qcc_issues.append("A08 财报：企查查数据为空，转人工要求补交")
                else:
                    # 取最新报告期（按报告期字符串倒序）
                    try:
                        latest = max(info_list, key=lambda x: x.get("报告期", ""))
                    except Exception:
                        latest = info_list[0]
                    indicators = (
                        latest.get("指标详情", {})
                        .get("分析数据", {})
                    )
                    # 三项指标
                    debt_ratio = indicators.get("资产负债率") or indicators.get("负债率")
                    liq_ratio = indicators.get("流动比率")
                    ocf = indicators.get("经营性现金流") or indicators.get("经营活动现金流净额")

                    # 保护：若三项指标全是空（企查查披露等级"指标稀少"），按无数据转人工
                    if (not debt_ratio or debt_ratio == "") and (not liq_ratio or liq_ratio == "") and (not ocf or ocf == ""):
                        c["status"] = "manual"
                        c["detail"] = (f"企查查财报披露不完整（{latest.get('报告期', '?')}，"
                                       f"披露等级：{latest.get('披露等级', '稀少')}），"
                                       "转人工要求供应商补交经审计财报")
                        qcc_issues.append(f"A08 财报：企查查数据披露稀少（{latest.get('报告期', '?')}），转人工要求补交")
                    else:
                        # 计算指标
                        ratio_issues = []
                        if debt_ratio not in (None, ""):
                            try:
                                dr = float(str(debt_ratio).rstrip("%"))
                                if dr > 65:
                                    ratio_issues.append(f"资产负债率{dr:.1f}%超阈值65%")
                            except (ValueError, TypeError):
                                pass
                        if liq_ratio not in (None, ""):
                            try:
                                lr = float(liq_ratio)
                                if lr < 100:
                                    ratio_issues.append(f"流动比率{lr:.1f}%不足100%")
                            except (ValueError, TypeError):
                                pass
                        if ocf not in (None, ""):
                            try:
                                ocf_val = float(str(ocf).replace(",", ""))
                                if ocf_val <= 0:
                                    ratio_issues.append(f"经营性现金流{ocf_val:g}为负或零")
                            except (ValueError, TypeError):
                                pass

                        if ratio_issues:
                            c["status"] = "manual"
                            c["detail"] = f"企查查财报数据（{latest.get('报告期', '?')}）指标不符：" + "；".join(ratio_issues)
                            qcc_issues.append("A08 财报：" + "；".join(ratio_issues))
                        else:
                            c["status"] = "pass"
                            c["detail"] = (
                                f"企查查财报指标核算通过（{latest.get('报告期', '?')}）："
                                f"资产负债率{debt_ratio or 'N/A'}、"
                                f"流动比率{liq_ratio or 'N/A'}、"
                                f"经营性现金流{ocf or 'N/A'}"
                            )
                            c["qcc_finance_ok"] = True

        # ---- A10 商业信誉：企查查登记状态辅助天眼查 ----
        elif cid == "A10" and reg:
            status = reg.get("登记状态", "")
            if status and not any(k in status for k in ("存续", "在业", "开业")):
                c["status"] = "fail"
                c["detail"] = f"企查查工商登记状态异常：「{status}」，需人工复核"
                qcc_issues.append(f"商业信誉：工商登记状态「{status}」")
            elif c["status"] == "pending":
                # 天眼查无数据 + 企查查登记状态正常 → 部分通过
                c["status"] = "partial"
                c["detail"] = (f"企查查登记状态「{status}」正常；"
                               f"失信/被执行/惩戒名单记录仍需人工核验")
            elif c["status"] == "pass":
                c["detail"] += f"；企查查登记状态「{status}」正常"

    return checklist, qcc_issues


# ============================================================
# TextIn OCR 解析结果 → 核查清单增强（业务 9/2 接入）
# ============================================================
# doc_type → checklist 项名关键词（按 name 含任意关键词匹配）
_TEXTIN_DOC_TYPE_KEYWORDS = {
    "business_license":      ["营业执照", "法律主体资格"],
    "legal_person_id":       ["法人身份"],
    "tax_credit":            ["纳税信用"],
    "production_license":    ["生产许可", "强制认证"],
    "after_sales_cert":      ["售后服务"],
    "after_sales_statement": ["售后服务"],
    "financial_report":      ["审计财报", "资金财务"],
    "iso9001":               ["ISO 9001", "ISO9001"],
    "iso14001":              ["ISO 14001", "ISO14001"],
    "iso45001":              ["ISO 45001", "ISO45001"],
}


def _find_checklist_item(checklist, keywords):
    """在 checklist 里找 name 含任意关键词的项（返回第一个匹配）"""
    for c in checklist:
        nm = c.get("name", "") or ""
        if any(k in nm for k in keywords):
            return c
    return None


def enhance_checklist_with_textin(checklist, supplier, textin_for_todo):
    """
    TextIn OCR 解析结果合并进核查清单（阶段2 增强）。
    textin_for_todo 形如 {"business_license": {fields,checks,issues}, "iso9001": {...}, ...}
    返回 (checklist, textin_issues) — textin_issues 为 OCR 发现的问题汇总
    """
    textin_issues = []

    for doc_type, kws in _TEXTIN_DOC_TYPE_KEYWORDS.items():
        r = textin_for_todo.get(doc_type)
        if not r:
            continue
        c = _find_checklist_item(checklist, kws)
        if not c:
            continue

        checks = r.get("checks") or {}
        issues = r.get("issues") or []
        fields = r.get("fields") or {}

        # 通用：有 issues → fail；全 pass → pass；混合 → partial；无 checks → pending
        true_keys = [k for k, v in checks.items() if v is True]
        false_keys = [k for k, v in checks.items() if v is False]
        none_keys = [k for k, v in checks.items() if v is None]

        if issues or false_keys:
            # 取最具体的 issue 描述（限制长度）
            descs = issues if issues else [f"{k}={v}" for k, v in checks.items() if v is False]
            short = "；".join(descs[:3])
            if len(descs) > 3:
                short += f"（另有{len(descs)-3}项）"
            c["status"] = "fail"
            c["detail"] = f"OCR核验发现问题：{short}"
            textin_issues.append(f"{c.get('name', doc_type)}：{short}")
        elif true_keys and not none_keys:
            # 全 pass：标 pass，描述抽取出的关键字段
            sample = []
            for k in list(fields.keys())[:4]:
                v = fields[k]
                if v is None: continue
                s = str(v)
                if len(s) > 30: s = s[:30] + "…"
                sample.append(f"{k}={s}")
            c["status"] = "pass"
            c["detail"] = "OCR核验通过：" + "，".join(sample)
        elif true_keys and none_keys:
            # 有 pass 有 需人工 → partial
            c["status"] = "partial"
            c["detail"] = (f"OCR核验：自动确认{len(true_keys)}项，"
                           f"{len(none_keys)}项需人工核验（{'; '.join(none_keys)}）")
        # else: 保持原 status（pending 等）

    return checklist, textin_issues


def run_stage2():
    """阶段2：读缓存+企查查结果+TextIn OCR → 离线重决策（不碰scpma，无WAF风险）"""
    cfg = load_rules()
    cache = _load_cache()
    if not cache:
        log.error(f"无缓存数据（{CACHE_FILE.name}），请先跑批或 FETCH_ONLY=true 拉取")
        sys.exit(1)
    qcc = {}
    if QCC_FILE.exists():
        try:
            qcc = json.loads(QCC_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"企查查结果文件损坏，按无结果处理: {e}")
    textin = {}
    textin_path = BASE_DIR / "textin_results.json"
    if textin_path.exists():
        try:
            textin = json.loads(textin_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"TextIn 结果文件损坏，按无结果处理: {e}")
    log.info(f"=== 阶段2开始: 缓存 {len(cache)} 家，企查查结果 {len(qcc)} 条，"
             f"TextIn OCR 结果 {len(textin)} 家 ===")

    results = {}
    n_qcc = 0
    for tid, entry in cache.items():
        supplier = entry.get("supplier", {})
        todo = entry.get("todo", {})

        # 补全 is_inspection / has_inspection_cert（旧 cache 没有，新版新增）
        _ensure_inspection_fields(supplier, entry)

        # 分流供应商（国外/港澳台/集团独有）不需要企查查增强
        if supplier.get("is_foreign") or supplier.get("is_hmt"):
            # 9/5 修复：即使分流也生成"不适用"结果，让所有 31 家都能看到决策
            results[tid] = _build_skip_result(tid, entry, decision_reason="境外供应商不适用中国大陆合规审查")
            continue
        is_special, _ = is_special_category(supplier)
        if is_special:
            results[tid] = _build_skip_result(tid, entry, decision_reason="集团内部供应商（股东含中交系统关键词），按内部流程处理")
            continue

        # 匹配企查查结果（优先信用代码，其次企业名）
        key = supplier.get("social_credit_code") or ""
        q = qcc.get(key) or qcc.get(supplier.get("name", "")) \
            or qcc.get(todo.get("applyUnitName", ""))
        # 2026-09-05 修复（Quinn 反馈）：无企查查数据不再整体跳过——
        # 以空数据继续跑规则引擎，材料齐全性检查照常，企查查相关核验项
        # 自然落入"待人工核验"。这样每家供应商都有审批结果（决策），
        # 而不是 no_result。
        if not q:
            q = {}
            log.info(f"[阶段2] {todo.get('applyUnitName') or supplier.get('name')}"
                     f" 无企查查数据，仅按材料+规则出结果（企查查项转人工）")

        applicable_rules, type_desc = determine_supplier_rules(supplier, cfg)
        checklist, auto_failed, missing, verify_items = build_checklist(
            supplier, applicable_rules)

        # 企查查增强（材料缺失规则不受影响，只增强核验项）
        checklist, qcc_issues = enhance_checklist_with_qcc(checklist, supplier, q)

        # TextIn OCR 增强（核验供应商上传的资质材料）
        textin_issues = []
        if tid in textin:
            checklist, textin_issues = enhance_checklist_with_textin(
                checklist, supplier, textin[tid])

        # 重建 verify_items（完全基于增强后的清单，旧的作废——
        # 已增强为 pass 的不再贡献核验点，partial 只留残差）
        verify_items = []
        seen_v = set()
        for c in checklist:
            st = c.get("status")
            v = None
            if st == "pending":
                d = c.get("detail", "")
                v = d.split("；待核验：", 1)[1] if "；待核验：" in d else d
            elif st == "partial":
                d = c.get("detail", "")
                if "；" in d:
                    v = d.split("；", 1)[1]
            elif st == "fail" and c.get("id") in ("A01", "A02", "A08", "A10"):
                v = c.get("detail", "")
            if v and v not in seen_v:
                verify_items.append(v)
                seen_v.add(v)

        opinion, decision = generate_opinion_v4(
            supplier, auto_failed, missing, verify_items, type_desc=type_desc)

        # reject（缺材料）时，企查查/TextIn 发现的问题附加在意见后
        extra_parts = []
        if decision == "reject" and qcc_issues:
            extra_parts.append("另经企查查核验发现（请一并核实）：\n" + "\n".join(
                f"  {i}. {x}" for i, x in enumerate(qcc_issues, 1)))
        if textin_issues:
            extra_parts.append("另经资质文件OCR核验发现：\n" + "\n".join(
                f"  {i}. {x}" for i, x in enumerate(textin_issues, 1)))
        if extra_parts:
            opinion += "\n\n" + "\n\n".join(extra_parts)

        full_name = todo.get("applyUnitName") or supplier.get("name", "")
        results[tid] = {
            "todoId": tid,
            "name": full_name,
            "billName": todo.get("billName", ""),
            "type_desc": type_desc,
            "decision": decision,
            "opinion": opinion,
            "checklist": checklist,
            "qcc_issues": qcc_issues,
            "textin_issues": textin_issues,
            "materials_detail": entry.get("materials_detail", []),
            "certifications": entry.get("certifications", []),
            "stage2_at": datetime.now().isoformat(timespec="seconds"),
        }
        n_qcc += 1
        d_cn = {"reject": "退回", "manual": "转人工", "recommend": "建议同意"}.get(decision, decision)
        n_textin_extra = f"；OCR{len(textin_issues)}项" if textin_issues else ""
        log.info(f"[阶段2] {full_name} | {d_cn}"
                 f"{'（企查查发现' + str(len(qcc_issues)) + '项问题）' if qcc_issues else ''}"
                 f"{n_textin_extra}")

    STAGE2_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    n_rej = sum(1 for r in results.values() if r["decision"] == "reject")
    n_man = sum(1 for r in results.values() if r["decision"] == "manual")
    n_rec = sum(1 for r in results.values() if r["decision"] == "recommend")
    n_issue_qcc = sum(len(r["qcc_issues"]) for r in results.values())
    n_issue_textin = sum(len(r["textin_issues"]) for r in results.values())
    log.info(f"=== 阶段2结束: 企查查增强 {n_qcc} 家 | 退回 {n_rej} | 转人工 {n_man} | "
             f"建议同意 {n_rec} | 企查查问题 {n_issue_qcc} 项 | "
             f"OCR问题 {n_issue_textin} 项 ===")
    log.info(f"结果文件: {STAGE2_FILE}")


if __name__ == "__main__":
    if "--stage2" in sys.argv:
        run_stage2()
    else:
        run()
