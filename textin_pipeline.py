# -*- coding: utf-8 -*-
"""
TextIn xParse 材料解析管道（阶段3：材料内容核验）

流程:
    files_cache/<todoId>/  下的资质文件（图片/PDF/Word）
      → TextIn xParse API 解析成 Markdown（直连模式，凭据在 .env）
      → 按材料类型抽取字段（执照/身份证/纳税/财报/ISO/授权）
      → textin_results.json（todoId → 各材料字段+核验结论）
      → auto_approve.py --stage2 合并进核查清单

文件来源（三选一）:
    A. 文件直链API下载（待补：需浏览器录HAR拿到真实下载接口）
    B. 手动下载放入 files_cache/<todoId>/（过渡期测试用）
    C. TextIn 支持 URL 直传（拿到直链后可省下载步骤）

凭据（.env）:
    TEXTIN_APP_ID=xxx        # textin.com 工作台-账号设置-开发者信息
    TEXTIN_SECRET_CODE=xxx
    （新注册送100页；加官方福利官再送1000页；新客套餐9.9元/1000页）

用量参考: 一家供应商关键材料约5-10页，37家全量约200-400页/轮。

用法:
    python textin_pipeline.py scan                 # 扫描 files_cache/ 并分类
    python textin_pipeline.py parse [todoId]       # 调TextIn解析→抽取→写textin_results.json
    python textin_pipeline.py extract [todoId]     # 仅从已缓存的 .md 抽取（不调API）
    python textin_pipeline.py test                 # 离线自测（mock材料）
"""
import json
import os
import re
import sys
import time
import logging
from datetime import datetime, date
from pathlib import Path

# 9/5 修复：之前 run_parse 里用了 log.info 但模块没定义 log，导致 OCR 集成脱敏后崩溃
log = logging.getLogger("textin-pipeline")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

import requests

BASE = Path(__file__).parent
FILES_DIR = BASE / "files_cache"
FILES_DESENS_DIR = BASE / "files_cache_desens"  # 2026-09-04 保密合规：脱敏后给 OCR 用的中间目录
RESULTS_FILE = BASE / "textin_results.json"
TEXTIN_API = "https://api.textin.com/ai/service/v1/pdf_to_markdown"
# 注：新版 xParse 端点 /ai/service/v1 实测返回 400「缺少必要参数或参数值不正确」，
# 旧版 pdf_to_markdown 端点实测可用（9/2 验证），故用旧版。

# ---------- .env 加载 ----------
_env_file = BASE / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _k.strip() and _k.strip() not in os.environ:
                os.environ[_k.strip()] = _v.strip()

TEXTIN_APP_ID = os.getenv("TEXTIN_APP_ID", "")
TEXTIN_SECRET_CODE = os.getenv("TEXTIN_SECRET_CODE", "")

# 需要解析的材料类型 → 对应核查清单项
DOC_TYPES = [
    "business_license", "legal_person_id", "tax_credit", "tax_cert",
    "financial_report", "iso9001", "iso14001", "iso45001",
    "production_license", "authorization", "after_sales_cert",
    "after_sales_statement",
]

# 文件名关键词 → 材料类型（files_cache 手动放置时分类用）
# 2026-09-04 保密合规改造：
# - legal_person_id / financial_report 的 extract_* 函数已废弃（直接返回空 dict）
# - 上传身份证/财报时走 desensitize_dir 脱敏（人像页打码+财报签字/银行账号打码）
# - 合规边界：
#   * "公开披露信息"（营业执照、ISO、税务、企查查可查的财报）可发给 AI 处理
#   * "未公开披露信息"（身份证正反面+身份证号、财报签字/银行账号）禁止发给 AI
# - 警告：脱敏是**降低风险不是消除风险**——
#   * 国徽页保留（含签发机关+有效期），可能被认定为辅助信息泄露
#   * 身份证 OCR 字段（姓名+身份证号）在脱敏图上 100% 不可见，但若 PDF/扫描件分辨率
#     异常高，可能保留指纹/边角信息
# - 因此 9/5 与保密专员共识：身份证 OCR 在脱敏前提下仍**不启用**，避免触线
FILENAME_KEYWORDS = [
    (("营业执照", "执照"), {"business_license"}),
    (("身份证",), {"legal_person_id"}),                  # 保留分类，OCR 禁用
    # 审计/审记（错别字）优先于纳税：供应商常把审计报告和纳税申报混在一起命名
    (("财务", "审计", "审记", "财报"), {"financial_report"}),  # 保留分类，OCR 禁用
    (("纳税", "信用等级", "信用评价"), {"tax_credit"}),
    (("质量管理体系", "9001"), {"iso9001"}),
    (("环境", "14001"), {"iso14001"}),
    (("职业健康", "职业安全", "45001"), {"iso45001"}),
    (("生产许可", "强制认证", "3C"), {"production_license"}),
    (("授权", "代理", "经销"), {"authorization"}),
    (("售后", "承诺"), {"after_sales_statement"}),   # 自拟承诺书：落款3个月内
    (("售后",), {"after_sales_cert"}),               # 机构认证证书：看过期
]

FILE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".pdf",
             ".doc", ".docx", ".xls", ".xlsx"}


# ============================================================
# TextIn API 直连解析
# ============================================================
def parse_file_textin(path):
    """调 TextIn xParse 解析单个文件 → 返回 (markdown, detail)（失败抛异常）

    9/7 改造：markdown_details=1 以拿到字段坐标 detail（含 position + 纯文本 text）。
    - markdown：正文（身份证会返回 [DESENSITIZED] + HTML 注释敏感信息）
    - detail：结构化字段列表，text 是纯文本（无 HTML 注释包裹），供身份证提取姓名/有效期
    """
    if not TEXTIN_APP_ID or not TEXTIN_SECRET_CODE:
        raise RuntimeError("未配置 TEXTIN_APP_ID / TEXTIN_SECRET_CODE（.env），"
                           "无法直连TextIn解析")
    path = Path(path)
    with open(path, "rb") as f:
        data = f.read()
    headers = {
        "x-ti-app-id": TEXTIN_APP_ID,
        "x-ti-secret-code": TEXTIN_SECRET_CODE,
        "Content-Type": "application/octet-stream",
    }
    params = {
        "parse_mode": "auto",       # 引擎自动选择（图片=scan，电子档=parse）
        "apply_document_tree": 0,   # 不需要标题树
        "table_flavor": "md",       # 表格按md输出（财报数字在表格里）
        "get_image": "none",
        "markdown_details": 1,      # 9/7：拿 detail 坐标，身份证需要定位姓名/有效期
    }
    resp = requests.post(TEXTIN_API, headers=headers, params=params,
                         data=data, timeout=120,
                         proxies={"http": None, "https": None})  # 不走系统代理（防换网络后 ProxyError）
    resp.raise_for_status()
    result = resp.json()
    if result.get("code") not in (200, "200"):
        raise RuntimeError(f"TextIn返回异常: code={result.get('code')} "
                           f"msg={result.get('msg') or result.get('message')}")
    # 兼容多种返回结构
    r = result.get("result") or result.get("data") or {}
    md = (r.get("markdown") or r.get("markdown_text") or r.get("text")
          or result.get("markdown") or "")
    if not md:
        # 有的版本放在 content 列表里
        content = r.get("content") or []
        if isinstance(content, list):
            md = "\n".join(str(x.get("text", x) if isinstance(x, dict) else x)
                           for x in content)
    detail = r.get("detail") or []
    if not isinstance(detail, list):
        detail = []
    return md, detail


def _sanitize_detail(doc_type, detail):
    """缓存 detail 前字段级脱敏：身份证只保留「姓名」+「有效期限」，
    其余敏感字段（性别/民族/出生/住址/身份证号/签发机关）一律丢弃，不落盘。
    其他材料类型原样返回 detail。
    """
    if doc_type != "legal_person_id" or not detail:
        return detail
    text = "\n".join(str(d.get("text", "")) for d in detail if isinstance(d, dict))
    kept = []
    m = re.search(r"姓\s*名\s*[:：]?\s*([\u4e00-\u9fa5·]{2,15})", text)
    if m:
        kept.append("姓名 " + m.group(1))
    m = re.search(r"有效期限\s*([^\s\n，,；;]+)", text)
    if m:
        kept.append("有效期限 " + m.group(1))
    return [{"text": " ".join(kept)}] if kept else []


# ============================================================
# 字段抽取（纯函数，可离线测试）
# ============================================================
_DATE = r"(\d{4})[年.\-/](\d{1,2})[月.\-/](\d{1,2})"

# 通用证书：检测"有效期至/失效日期/发证日期"等明显标签（无标签→不取最后日期兜底）
# 拆成两段避免 Python 3.13 parser 对多行中文 raw string 拼接的边界 bug
_RE_VALID_DATE_RANGE = re.compile(
    r"有效期.{0,40}?[-—~至].{0,10}?(?:" + _DATE + r"|长期|永久|无固定期限)")
_RE_EXPIRE_DATE = re.compile(r"失效[日日期]*\s*[:：]?\s*" + _DATE)
_RE_ISSUE_DATE = re.compile(r"发证[日日期]*\s*[:：]?\s*" + _DATE)


def _norm(s):
    """去空格（竖排/间隔排版）"""
    return re.sub(r"\s+", "", str(s or ""))


def _pre(text):
    """预处理：删表格分隔线、竖线换空格、去加粗星号/标题井号——保留换行结构
    （TextIn 返回的 markdown 里字段通常按行/表格排列，行结构是字段边界的依据）

    注（9/7）：TextIn 对"证照类"文档（营业执照/身份证）把结构化内容放在
    HTML 注释里，正文可能是空或占位符。因此这里**不过滤注释**（否则营业执照
    也提取不到）。身份证的敏感字段隔离改在 extract_legal_person_id（用 detail
    纯文本只取姓名+有效期）+ _sanitize_detail（缓存时脱敏）两层处理。
    """
    text = str(text or "")
    text = re.sub(r"^\s*\|[\s:\-|]+\|?\s*$", "", text, flags=re.M)
    text = text.replace("|", " ").replace("**", "")
    # 竖排执照常见"名\n称"式断行标签：把 1-3 个汉字的短行与下一行合并
    text = re.sub(r"(?m)^([\u4e00-\u9fa5]{1,3})[ \t]*\n+[ \t]*(?=[\u4e00-\u9fa5])", r"\1", text)
    return re.sub(r"^#{1,6}\s*", "", text, flags=re.M)


# 已知字段标签（值遇到这些词就停，防止把下一字段吞进来）
_FIELD_STOP = (r"统一社会信用代码|名称|类型|住所|法定代表人|注册资本|成立日期|"
               r"营业期限|经营范围|证书编号|获证组织|认证状态|有效期至|有效期限|"
               r"授权方|被授权方|授权期限|纳税人名称|纳税人识别号")


_CN_NUM = {"零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "贰": 2, "两": 2,
           "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5,
           "六": 6, "陆": 6, "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9}
_CN_UNIT = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}


def _cn2num(s):
    """中文数字→数值（分段解析，正确支持亿/万组合）：
    '贰仟'→2000、'壹佰贰拾'→120、'壹亿陆仟陆佰伍拾伍万'→166550000"""
    result, section, num = 0, 0, 0
    for ch in s:
        if ch in _CN_NUM:
            num = _CN_NUM[ch]
        elif ch in _CN_UNIT:
            if num == 0:
                num = 1
            section += num * _CN_UNIT[ch]
            num = 0
        elif ch == "万":
            section += num
            result += section * 10000
            section = 0
            num = 0
        elif ch == "亿":
            section += num
            result += section * 100000000
            section = 0
            num = 0
        else:
            return None
    return result + section + num


def _parse_capital(t):
    """注册资本 → 万元数值。兼容多种排版：
    '800万元' / '人民币1100万元' / 中文大写'人民币元 壹亿陆仟陆佰伍拾伍万元整'"""
    # 阿拉伯数字（允许"注册资本"与金额间夹"人民币/人民币元"字样）
    m = re.search(r"注册资本\s*[:：]?\s*(?:人民币(?:元)?)?\s*([\d,.，]+)\s*万", t)
    if m:
        try:
            return float(m.group(1).replace(",", "").replace("，", ""))
        except ValueError:
            return None
    # 中文大写（完整金额含亿/万，_cn2num 转"元"后 /10000 得"万元"）
    m = re.search(r"注册资本\s*[:：]?\s*(?:人民币(?:元)?)?\s*"
                  r"([零〇一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟]+)", t)
    if m:
        v = _cn2num(m.group(1))
        if v:
            return float(v) / 10000
    return None


def _grab(text, labels, pat):
    """取'标签: 值'——值到换行/冒号/下一已知标签为止（懒匹配+前瞻断言）。
    标签前不能紧跟其他汉字（防'纳税人名称'误中'名称'、'被授权方'误中'授权方'）"""
    tail = rf"(?=\s*[:：]?\s*(?:{_FIELD_STOP}|\n|$))"
    for lab in labels:
        m = re.search(rf"(?<![\u4e00-\u9fa5]){re.escape(lab)}"
                      rf"\s*[:：]?\s*({pat})" + tail, text)
        if m:
            return m.group(1).strip(" 　。，,；;、")
    return None


def _to_date(m):
    y, mth, d = int(m[0]), int(m[1]), int(m[2])
    try:
        return date(y, mth, d)
    except ValueError:
        return None


def _find_dates(text):
    """找所有日期（返回date列表）"""
    return [d for d in (_to_date(m) for m in re.findall(_DATE, text)) if d]


def _valid_until(text):
    """抽取'有效期至/失效日期'类字段 → (date或None, 是否长期)"""
    if re.search(r"长期|永久|无固定期限", _norm(text)):
        return None, True
    m = re.search(r"(有效期[至到]|失效[日日期]*|到期[日日期]*)[:：]?\s*" + _DATE, text)
    if m:
        d = _to_date(m.groups()[-3:])
        if d:
            return d, False
    # 兜底：文本里最晚的日期（证书类常只印一个截止日）
    dates = _find_dates(text)
    return (max(dates), False) if dates else (None, False)


def extract_business_license(text, supplier):
    """营业执照 → 字段+与系统信息比对（按行结构抽取，兼容md表格/加粗排版）"""
    t = _pre(text)
    fields = {}
    m = re.search(r"统一社会信用代码\s*[:：]?\s*([0-9A-Za-z]{15,18})", t)
    fields["统一社会信用代码"] = m.group(1).upper() if m else None
    fields["名称"] = _grab(t, ["名称"], r"[^\n:：]{2,60}?")
    fields["法定代表人"] = _grab(t, ["法定代表人"], r"[^\n:：]{2,15}?")
    m = re.search(r"注册资本\s*[:：]?\s*([\d,.，]+)\s*万", t)
    fields["注册资本_万"] = _parse_capital(t)

    checks, issues = {}, []
    if not any(fields.values()):
        issues.append("营业执照未能识别关键字段（需人工核验扫描件清晰度）")
    f_code = _norm(fields["统一社会信用代码"] or "").upper()
    sys_code = _norm(supplier.get("social_credit_code", "")).upper()
    if f_code and sys_code:
        checks["信用代码一致"] = f_code == sys_code
        if not checks["信用代码一致"]:
            issues.append(f"执照信用代码{f_code}与系统{sys_code}不一致")
    f_name = _norm(fields["名称"] or "")
    sys_name = _norm(supplier.get("full_name") or supplier.get("name", ""))
    if f_name and sys_name and len(sys_name) > 4:
        checks["名称一致"] = f_name in sys_name or sys_name in f_name
        if not checks["名称一致"]:
            issues.append(f"执照名称「{f_name}」与系统「{sys_name}」不一致")
    f_legal = _norm(fields["法定代表人"] or "")
    sys_legal = _norm(supplier.get("legal_person", ""))
    if f_legal and sys_legal:
        checks["法人一致"] = f_legal == sys_legal
        if not checks["法人一致"]:
            issues.append(f"执照法人{f_legal}与系统{sys_legal}不一致")
    sys_cap = supplier.get("registered_capital", 0) or 0
    if fields["注册资本_万"] and sys_cap:
        checks["注册资本一致"] = abs(fields["注册资本_万"] - sys_cap) / max(sys_cap, 1) <= 0.01
        if not checks["注册资本一致"]:
            issues.append(f"执照注册资本{fields['注册资本_万']:g}万与系统{sys_cap:g}万不一致")
    # 营业期限：只认明确标注的"营业期限"字段——成立日期不是到期日；
    # 新版执照常省略该字段，省略时不判定（执照有效性以信用代码可查为准）
    mm = re.search(r"营业期限\s*[:：]?\s*([^\n]{1,60})", t)
    if mm:
        seg = mm.group(1)
        if re.search(r"长期|永久|无固定期限", seg):
            checks["营业期限长期或有效"] = True
        else:
            ds = _find_dates(seg)
            if ds:
                exp = max(ds)
                checks["营业期限长期或有效"] = exp >= date.today()
                if not checks["营业期限长期或有效"]:
                    issues.append(f"营业执照营业期限已到期（{exp}）")
    # 经营范围：执照 ↔ 系统基本信息栏比对（9/2 新增审核要点）
    m = re.search(r"经营范围\s*[:：]?\s*([^\n]{2,500})", t)
    fields["经营范围"] = m.group(1).strip() if m else None
    f_scope = _norm(fields["经营范围"] or "")
    sys_scope = _norm(supplier.get("busi_scope") or "")
    if f_scope and sys_scope:
        if "具体经营项目" in f_scope and ("公示系统" in f_scope or "gsxt" in f_scope.lower()):
            # 新版执照只印概括式范围+提示查公示系统，无法逐项机器比对
            checks["经营范围一致"] = None
            issues.append("执照为概括式经营范围（新版执照样式），"
                          "无法与系统填写逐项比对，需人工核验")
        else:
            from difflib import SequenceMatcher
            ratio = SequenceMatcher(None, f_scope, sys_scope).ratio()
            if ratio >= 0.9:
                checks["经营范围一致"] = True
            elif ratio >= 0.7:
                # 9/8 降级：高度相似但有差异（0.7~0.9，常见是系统多/少了
                # 「依法须经批准的项目…」这类标准结尾提示语）不再判 fail（退回），
                # 改为 checks=None → enhance_checklist_with_textin 走 manual（转人工复核）。
                checks["经营范围一致"] = None
                issues.append(f"执照经营范围与系统填写高度相似但有差异"
                              f"（相似度{ratio:.0%}），需人工确认")
            else:
                checks["经营范围一致"] = False
                issues.append(f"执照经营范围与系统基本信息栏填写不一致"
                              f"（相似度{ratio:.0%}）")
    # 9/7 修复：关键字段（名称/法定代表人/注册资本/经营范围）任一缺失 → 识别不完整，转人工。
    # 此前只在「全部字段都空」时报 issue，导致「只识别到信用代码」这种部分识别
    # 被 enhance_checklist_with_textin 误判为 pass（A01 要求名称/注册资本/法人/经营范围全一致，
    # 只核到信用代码一项远远不够）。
    _missing = [label for key, label in (
        ("名称", "名称"), ("法定代表人", "法定代表人"),
        ("注册资本_万", "注册资本"), ("经营范围", "经营范围"))
        if not fields.get(key)]
    if _missing:
        issues.append("营业执照识别不完整，缺少字段：" + "、".join(_missing)
                      + "（需人工核验扫描件清晰度，或改用企查查比对）")
    return {"fields": fields, "checks": checks, "issues": issues}


def extract_legal_person_id(text, supplier, detail=None):
    """法人身份证 → 核验「姓名」+「身份证有效期」（2026-09-07 重写）

    保密合规（9/7）：身份证 OCR 前本地脱敏，仅保留姓名 + 有效期限两项可见；
    核验也仅针对这两项。其余敏感字段（性别/民族/出生/住址/身份证号/签发机关）
    一律不提取、不落盘。

    detail 为 TextIn markdown_details=1 返回的结构化字段（text 是纯文本，
    不含 HTML 注释）。优先从 detail 提取（正文 markdown 里敏感信息在注释中，
    已被 _pre 过滤）。

    正反面完整性：姓名（正面）与有效期限（背面）任一缺失 → 报错提示补传。
    """
    # 拼接 detail 里的纯文本（TextIn 把正面/背面分成多个 paragraph）
    detail_text = ""
    if detail:
        detail_text = "\n".join(
            str(d.get("text", "")) for d in detail if isinstance(d, dict))
    # 正文（_pre 已过滤注释，正文仅剩 [DESENSITIZED]，无法提取字段，仅兜底）
    t = _pre(text)
    combined = detail_text + "\n" + t
    n = _norm(combined)

    fields = {}
    issues = []

    # ---- 姓名（正面）----
    m = re.search(r"姓\s*名\s*[:：]?\s*([\u4e00-\u9fa5·]{2,15}?)"
                  r"(?=\s*(?:性别|民族|出生|住址|公民|号码|签发|$))", combined)
    fields["姓名"] = m.group(1) if m else None

    # ---- 有效期限（背面）----
    # 格式：有效期限 2018.12.26-长期 / 有效期限 2022.10.12-2025.10.11
    exp, longterm = None, False
    if re.search(r"有效期限", n) and re.search(r"长期|永久|无固定期限", n):
        longterm = True
    else:
        # 严格匹配 "有效期限 起始日-长期/结束日" 或 "有效期至 YYYY-MM-DD"
        m = re.search(r"有效[期限]*[^\d]*" + _DATE + r"\s*[-—~至\s]*" +
                      r"(长期|永久|无固定期限|" + _DATE + r")", combined)
        if m:
            gs = [g for g in m.groups() if g]
            end_str = gs[3] if len(gs) >= 4 else None
            if end_str in ("长期", "永久", "无固定期限"):
                longterm = True
            elif end_str:
                em = re.match(_DATE, end_str)
                exp = _to_date(em.groups()) if em else None
    if not longterm and exp is None:
        m = re.search(r"有效期[至止]?\s*[:：]?\s*" + _DATE, n)
        if m:
            exp = _to_date(m.groups())

    checks = {}

    # ---- 正反面完整性检测（9/7 修复：区分「真缺面」与「识别失败」）----
    # 姓名=正面，有效期限=背面
    name_ok = bool(fields["姓名"])
    expiry_ok = bool(longterm or exp)
    if name_ok and expiry_ok:
        checks["正反面齐全"] = True
    elif not name_ok and not expiry_ok:
        # 两面都识别不到 → 可能是真缺、或文件根本不是身份证/严重不清晰，标 fail
        checks["正反面齐全"] = False
        issues.append("身份证正反面均未能识别，请确认是否上传了完整的身份证正反面")
    else:
        # 只识别到一面 → 另一面可能缺、也可能被打码/不清晰导致识别失败，转人工核验
        # （不武断判「缺少XX面请重新上传」，避免脱敏打码/OCR识别率低导致的误判）
        checks["正反面齐全"] = None
        if not name_ok:
            issues.append("身份证正面（姓名）未能识别，需人工核验是否缺面或图片不清晰")
        if not expiry_ok:
            issues.append("身份证背面（有效期限）未能识别，需人工核验是否缺面或图片不清晰")

    # ---- 姓名一致性核验 ----
    sys_legal = _norm(supplier.get("legal_person", ""))
    if fields["姓名"] and sys_legal:
        checks["姓名与法人一致"] = fields["姓名"] == sys_legal
        if not checks["姓名与法人一致"]:
            issues.append(f"身份证姓名「{fields['姓名']}」与系统法人「{sys_legal}」不一致")

    # ---- 有效期核验 ----
    today = date.today()
    if longterm:
        checks["在有效期内"] = True
        fields["有效期至"] = "长期"
    elif exp:
        checks["在有效期内"] = exp >= today
        fields["有效期至"] = str(exp)
        if not checks["在有效期内"]:
            issues.append(f"身份证已过期（{exp}）")
    else:
        fields["有效期至"] = None

    return {"fields": fields, "checks": checks, "issues": issues}


def build_idcard_from_paddle(cards, supplier=None):
    """用 PaddleOCR 的 cards 构造 legal_person_id 结果（2026-09-09 方案B）。

    idcard_masker.py（PaddleOCR）打码时已识别出正反面字段，cards 形如：
      [{"card_index":1,"side":"front","name":"陈永泉","valid_until":null,"status":"已脱敏"},
       {"card_index":2,"side":"back","name":null,"valid_until":"2007.02.24-2027.02.24",...}]

    本函数把 cards 映射成与 extract_legal_person_id 相同的返回结构，
    替代「TextIn 读打码图 → 提取」链路（打码图只剩值没标签，TextIn 提取不到）。
    """
    name = None
    valid_until = None
    for c in cards or []:
        if not isinstance(c, dict):
            continue
        if c.get("side") == "front" and c.get("name"):
            name = c["name"]
        elif c.get("side") == "back" and c.get("valid_until"):
            valid_until = c["valid_until"]

    fields = {"姓名": name, "有效期至": None}
    checks = {}
    issues = []

    # 正反面完整性（name=正面，valid_until=背面）
    name_ok = bool(name)
    expiry_ok = bool(valid_until)
    if name_ok and expiry_ok:
        checks["正反面齐全"] = True
    elif not name_ok and not expiry_ok:
        checks["正反面齐全"] = False
        issues.append("身份证正反面均未能识别，请确认是否上传了完整的身份证正反面")
    else:
        checks["正反面齐全"] = None
        if not name_ok:
            issues.append("身份证正面（姓名）未能识别，需人工核验是否缺面或图片不清晰")
        if not expiry_ok:
            issues.append("身份证背面（有效期限）未能识别，需人工核验是否缺面或图片不清晰")

    # 姓名一致性
    sys_legal = _norm((supplier or {}).get("legal_person", ""))
    if name and sys_legal:
        checks["姓名与法人一致"] = name == sys_legal
        if not checks["姓名与法人一致"]:
            issues.append(f"身份证姓名「{name}」与系统法人「{sys_legal}」不一致")

    # 有效期核验（valid_until 格式：2007.02.24-2027.02.24 或 长期）
    today = date.today()
    if valid_until:
        if "长期" in valid_until or "永久" in valid_until:
            checks["在有效期内"] = True
            fields["有效期至"] = "长期"
        else:
            # valid_until 格式：起始日-结束日（如 2007.02.24-2027.02.24），取结束日期（最后一个）
            dates = re.findall(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", valid_until)
            if dates:
                y, mo, d = dates[-1]  # 结束日期（有效期至）
                exp = date(int(y), int(mo), int(d))
                checks["在有效期内"] = exp >= today
                fields["有效期至"] = str(exp)
                if not checks["在有效期内"]:
                    issues.append(f"身份证已过期（{exp}）")

    return {"fields": fields, "checks": checks, "issues": issues}


def extract_tax_credit(text):
    """纳税信用等级证明 → 等级+年度+来源正规性"""
    n = _norm(_pre(text))
    fields = {}
    m = re.search(r"年度评价结果[:：]?\s*([A-D])", n) or \
        re.search(r"纳税信用(?:级别|等级)[:：]?\s*([A-D])", n) or \
        re.search(r"级别[:：]?\s*([A-D])\s*级?", n) or \
        re.search(r"[（(]\s*([A-D])\s*级\s*[)）]", n)
    fields["纳税信用级别"] = m.group(1) if m else None
    m = re.search(r"(20\d{2})\s*年度", n) or \
        re.search(r"年度\s*[:：]?\s*(20\d{2})", n)
    fields["评价年度"] = m.group(1) if m else None

    checks, issues = {}, []
    lvl = fields["纳税信用级别"]
    if lvl:
        checks["C级及以上"] = lvl in ("A", "B", "C")
        if not checks["C级及以上"]:
            issues.append(f"纳税信用等级为{lvl}级，低于C级要求")
    else:
        issues.append("未能识别纳税信用等级（请人工核实文件内容）")
    # 年度合规判定（161 号标准：需为上年度纳税信用评价；2026 年审 → 2025 年度）
    # 纳税信用评价通常在次年4月发布，因此上年度评价=今年-1
    required_year = date.today().year - 1
    if fields.get("评价年度"):
        if fields["评价年度"] != str(required_year):
            issues.append(f"上传为{fields['评价年度']}年度纳税信用评价，"
                          f"按161号标准应为{required_year}年度（上年度）")
            checks[f"评价年度={required_year}"] = False
        else:
            checks[f"评价年度={required_year}"] = True
    src_ok = bool(re.search(r"国家税务总局|税务局|纳税服务平台|信用中国|电子税务局", n))
    fields["来源标记"] = "有税务机关字样" if src_ok else "未见税务机关字样"
    if not src_ok:
        issues.append("文件未见税务机关落款/来源（需人工确认是否国税总局网站下载）")
    return {"fields": fields, "checks": checks, "issues": issues}


def _num(s):
    """'1,234.56'→1234.56；带括号/负号处理"""
    s = str(s).strip().replace(",", "").replace("，", "")
    neg = s.startswith("-") or s.startswith("（-") or "(" in s and s.endswith(")")
    s = re.sub(r"[^\d.]", "", s)
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def extract_financial_report(text):
    """[已废弃 2026-09-04] 财报 → 资产负债率≤65% / 流动比率≥100% / 经营现金流>0

    按保密合规要求：未公开披露的财报数据不能发给 AI 处理。
    A08 上年度审计财报改走企查查财务数据接口（公开披露）：
      - 资产负债表 → 资产负债率
      - 利润表 → 经营情况辅助判断
      - 现金流量表 → 经营性现金流
    企查查无数据（非上市公司常见）→ 转人工要求供应商补交。

    函数保留为兼容旧调用，实际被 extract() 调用时直接返回 None。
    详见 docs/保密合规改造方案.md
    """
    log.warning("[DEPRECATED] extract_financial_report 已废弃——按保密合规要求 "
                "A08 财报改走企查查（公查信息），不发 AI 处理")
    return {"fields": {}, "checks": {}, "issues": [], "_deprecated": True}
    fields, checks, issues = {}, {}, []

    def find_item(label, alt=None):
        """在文本中找 '项目 数字' 模式（取第一个匹配）"""
        for lab in (label, alt or ""):
            if not lab:
                continue
            m = re.search(re.escape(_norm(lab)) + r"[^\d\-（(]{0,12}([\d,\-（）()\.]+)",
                          n)
            if m:
                v = _num(m.group(1))
                if v is not None:
                    return v
        return None

    # ① 优先取报表直接印出的比率
    m = re.search(r"资产负债率[^\d%]{0,8}([\d.]+)\s*%?", n)
    if m:
        fields["资产负债率%"] = float(m.group(1).replace(",", ""))
    m = re.search(r"流动比率[^\d%]{0,8}([\d.]+)\s*%?", n)
    if m:
        fields["流动比率%"] = float(m.group(1).replace(",", ""))

    # ② 没有现成比率 → 用科目计算
    if "资产负债率%" not in fields:
        liab = find_item("负债合计", "负债总计")
        asset = find_item("资产合计", "资产总计")
        if liab is not None and asset and asset != 0:
            fields["资产负债率%"] = round(liab / asset * 100, 2)
            fields["资产负债率_计算依据"] = f"负债合计{liab:g}/资产合计{asset:g}"
    if "流动比率%" not in fields:
        ca = find_item("流动资产合计", "流动资产总计")
        cl = find_item("流动负债合计", "流动负债总计")
        if ca is not None and cl is not None and cl != 0:
            fields["流动比率%"] = round(ca / cl * 100, 2)
            fields["流动比率_计算依据"] = f"流动资产{ca:g}/流动负债{cl:g}"

    # ③ 经营现金流
    ocf = find_item("经营活动产生的现金流量净额", "经营活动现金流量净额")
    if ocf is None:
        m = re.search(r"经营活动[^\d\-（(]{0,20}净额[^\d\-（(]{0,10}([\d,\-（）()\.]+)", n)
        ocf = _num(m.group(1)) if m else None
    if ocf is not None:
        fields["经营现金流净额"] = ocf

    # 年度
    m = re.search(r"(20\d{2})\s*年度", n)
    if m:
        fields["报告年度"] = m.group(1)
    # 年度合规判定（161 号标准要求上年度财报）
    # 当年审核 = 当前年份-1 即"上年度"；2026年审 → 应为 2025 年度财报
    required_year = date.today().year - 1
    if fields.get("报告年度"):
        if fields["报告年度"] != str(required_year):
            issues.append(f"上传为{fields['报告年度']}年度财报，"
                          f"按161号标准应为{required_year}年度（上年度）财报")
            checks[f"报告年度={required_year}"] = False
        else:
            checks[f"报告年度={required_year}"] = True

    # 判定（阈值：资产负债率≤65%、流动比率≥100%、现金流>0，不符转人工）
    if "资产负债率%" in fields:
        checks["资产负债率≤65%"] = fields["资产负债率%"] <= 65.0
        if not checks["资产负债率≤65%"]:
            issues.append(f"资产负债率{fields['资产负债率%']}%，超过65%")
    if "流动比率%" in fields:
        checks["流动比率≥100%"] = fields["流动比率%"] >= 100.0
        if not checks["流动比率≥100%"]:
            issues.append(f"流动比率{fields['流动比率%']}%，低于100%")
    if "经营现金流净额" in fields:
        checks["经营现金流>0"] = fields["经营现金流净额"] > 0
        if not checks["经营现金流>0"]:
            issues.append(f"经营性现金流净额为{fields['经营现金流净额']:g}（应为正）")
    if not checks:
        if re.search(r"纳税申报表|纳税申报\s*A?\s*类", n):
            issues.append("上传材料为《企业所得税年度纳税申报表》，非经审计财务报告——"
                          "无法核算资产负债率/流动比率/现金流，需要求供应商补交经审计财报")
        else:
            issues.append("未能从财报中识别关键指标（需人工核验）")
    return {"fields": fields, "checks": checks, "issues": issues}


def extract_iso_cert(text, supplier, iso_code):
    """ISO证书 → 持有人比对+效期（按行结构抽取，兼容md表格/加粗排版）"""
    t = _pre(text)
    fields = {}
    fields["获证组织"] = _grab(t, ["获证组织", "认证委托人", "受审核方", "组织名称",
                                   "证书持有者"], r"[^\n:：]{2,60}?")
    # 9/9 修复：ISO 证书底部常印「获证组织必须定期接受监督审核并经审核合格后，
    # 方可保持证书有效性」这类固定提示语，_grab 会把句首的「获证组织」误当字段标签、
    # 把整句提示语当值，导致持有人被错误显示成提示语。校验：值含提示语特征词则判无效。
    if fields["获证组织"] and re.search(
            r"必须|定期接受监督|监督审核|方可保持|有效性|经审核合格", fields["获证组织"]):
        fields["获证组织"] = None
    if not fields["获证组织"]:
        # 优先：ISO 证书标准格式「兹证明：XXX公司」——获证组织的权威来源
        m = re.search(r"兹证明\s*[:：]?\s*([^\n:：]{4,40}?(?:公司|集团|中心|厂))", t)
        if m:
            fields["获证组织"] = m.group(1).strip(" 　。，,；;、")
            fields["获证组织_来源"] = "兹证明"
        else:
            # 兜底：取标题后第一行独立的公司名（认证机构名通常在证书底部，取首个可避开）
            m = re.search(r"(?m)^([\u4e00-\u9fa5（）()A-Za-z0-9]{4,40}"
                          r"(?:公司|集团|中心|厂))\s*$", t)
            if m:
                fields["获证组织"] = m.group(1)
                fields["获证组织_来源"] = "证书版面推断（无标签）"
    m = re.search(r"证书编号\s*[:：]?\s*([A-Za-z0-9\-]{6,30})", t)
    fields["证书编号"] = m.group(1) if m else None
    exp, longterm = _valid_until(t)
    fields["有效期至"] = str(exp) if exp else ("长期" if longterm else None)
    m = re.search(r"认证?状态\s*[:：]?\s*(有效|暂停|撤销|失效)", t)
    fields["证书状态"] = m.group(1) if m else None

    checks, issues = {}, []
    org = _norm(fields["获证组织"] or "")
    sys_name = _norm(supplier.get("full_name") or supplier.get("name", ""))
    if org and sys_name and len(sys_name) > 4:
        checks["持有人一致"] = org in sys_name or sys_name in org
        if not checks["持有人一致"]:
            issues.append(f"ISO{iso_code}证书持有人「{org}」与供应商「{sys_name}」"
                          f"不一致（贸易商代理厂家认证时需人工确认授权链）")
    st = fields["证书状态"]
    if st and st != "有效":
        checks["证书状态有效"] = False
        issues.append(f"ISO{iso_code}证书状态为「{st}」")
    elif st:
        checks["证书状态有效"] = True
    today = date.today()
    if longterm:
        checks["在有效期内"] = True
    elif exp:
        days = (exp - today).days
        checks["在有效期内"] = days >= 0
        if days < 0:
            issues.append(f"ISO{iso_code}证书已过期（{exp}）")
        elif days < 90:
            issues.append(f"ISO{iso_code}证书将于{exp}到期（90天内，提醒续证）")
    else:
        issues.append(f"ISO{iso_code}证书未识别到有效期（需人工核验）")
    return {"fields": fields, "checks": checks, "issues": issues}


def extract_authorization(text, supplier):
    """授权书/代理协议 → 授权方+被授权方+效期（按行结构抽取，兼容md表格/加粗排版）"""
    t = _pre(text)
    fields = {}
    fields["授权方"] = _grab(t, ["授权方", "许可方", "甲方", "授权单位"],
                             r"[^\n:：]{2,60}?")
    fields["被授权方"] = _grab(t, ["被授权方", "被许可方", "乙方", "受权单位"],
                               r"[^\n:：]{2,60}?")
    exp, longterm = _valid_until(t)
    fields["有效期至"] = str(exp) if exp else ("长期" if longterm else None)

    checks, issues = {}, []
    sys_name = _norm(supplier.get("full_name") or supplier.get("name", ""))
    grantee = _norm(fields["被授权方"] or "")
    if grantee and sys_name and len(sys_name) > 4:
        checks["被授权方为本公司"] = grantee in sys_name or sys_name in grantee
        if not checks["被授权方为本公司"]:
            issues.append(f"授权书被授权方「{grantee}」与供应商「{sys_name}」不一致")
    today = date.today()
    if longterm:
        checks["在有效期内"] = True
    elif exp:
        checks["在有效期内"] = exp >= today
        if exp < today:
            issues.append(f"授权已过期（{exp}）")
    else:
        issues.append("授权书未识别到有效期（需人工核验）")
    return {"fields": fields, "checks": checks, "issues": issues}


def extract_generic_cert(text):
    '''通用证书（生产许可证/机构认证等）→ 效期
    若无明确有效期/失效日期标签，不取最后日期兜底（避免误报开票资料/执照等非证书材料）'''
    t = _pre(text)
    has_label = bool(
        _RE_VALID_DATE_RANGE.search(t)
        or _RE_EXPIRE_DATE.search(t)
        or _RE_ISSUE_DATE.search(t)
    )
    if not has_label:
        return {"fields": {"有效期至": None, "备注": "未识别到有效期标签"},
                "checks": {"在有效期内": None},
                "issues": ["未识别到有效期标签（材料可能为开票资料/其他证明，"
                          "或 OCR 未能识别，需人工核验）"]}
    exp, longterm = _valid_until(t)
    fields = {"有效期至": str(exp) if exp else ("长期" if longterm else None)}
    checks, issues = {}, []
    today = date.today()
    if longterm:
        checks["在有效期内"] = True
    elif exp:
        checks["在有效期内"] = exp >= today
        if exp < today:
            issues.append(f"证书已过期（{exp}）")
    else:
        issues.append("未识别到有效期（需人工核验）")
    return {"fields": fields, "checks": checks, "issues": issues}


def extract_self_statement(text):
    """供应商自拟文件（售后服务承诺书等）→ 落款时间在3个月内即有效
    （9/2 确认：自拟文件不看过期概念，只看落款新鲜度）"""
    t = _pre(text)
    fields, checks, issues = {}, {}, []
    # 落款日期：优先取最后一处"日期：xxx"标注，兜底取全文最后一个日期
    sign_date = None
    ms = re.findall(r"日期\s*[:：]?\s*([^\n]{4,40})", t)
    for seg in reversed(ms):
        ds = _find_dates(seg)
        if ds:
            sign_date = ds[-1]
            break
    if sign_date is None:
        ds = _find_dates(t)
        if ds:
            sign_date = ds[-1]
    fields["落款日期"] = str(sign_date) if sign_date else None
    today = date.today()
    if sign_date:
        days = (today - sign_date).days
        if days < 0:
            checks["落款时间在3个月内"] = False
            issues.append(f"落款日期{sign_date}晚于今天（日期异常，需人工核验）")
        elif days <= 92:
            checks["落款时间在3个月内"] = True
        else:
            checks["落款时间在3个月内"] = False
            issues.append(f"落款日期{sign_date}距今已{days}天（超过3个月），"
                          f"要求供应商重新出具")
    else:
        checks["落款时间在3个月内"] = None
        issues.append("未识别到落款日期（需人工核验）")
    return {"fields": fields, "checks": checks, "issues": issues}


# 材料类型 → 抽取器
def extract(doc_type, text, supplier=None, detail=None):
    if doc_type == "business_license":
        return extract_business_license(text, supplier)
    if doc_type == "legal_person_id":
        return extract_legal_person_id(text, supplier, detail)
    if doc_type in ("tax_credit",):
        return extract_tax_credit(text)
    if doc_type in ("financial_report",):
        return extract_financial_report(text)
    if doc_type in ("iso9001", "iso14001", "iso45001"):
        return extract_iso_cert(text, supplier, doc_type[3:])
    if doc_type == "authorization":
        return extract_authorization(text, supplier)
    if doc_type == "after_sales_statement":
        return extract_self_statement(text)
    return extract_generic_cert(text)


# ============================================================
# 文件扫描/解析/结果组装
# ============================================================
def classify_file(filename):
    """按文件名推断材料类型（files_cache 手动放置用）"""
    for keywords, types in FILENAME_KEYWORDS:
        if any(k in filename for k in keywords):
            return sorted(types)[0]
    return None


def _safe_filename(name):
    """文件名去 Windows 非法字符"""
    bad = '<>:"/\\|?*'
    return "".join("_" if c in bad else c for c in name).strip() or "unnamed"


def download_supplier_files(todo_id=None, delay=25.0):
    """从 scpma 自动下载资质文件 → files_cache/<todoId>/

    链路（2026-09-02 验证通过）：
      query_qualification_files(supinfoId) → supFilesBOList[{fileUrl(密文), fileName, fileSize}]
      → GET /apis/scpma/oss/downloadByUploadId?fileUrl=...&fileName=... （带 cookie）
    断点续下：已存在且大小一致的文件自动跳过，可直接重跑。
    """
    from auto_approve import query_qualification_files, _scpma_headers

    WAF_WAITS = [600, 900]          # 与主程序同款：被拦后等10/15分钟再试

    cache = json.loads((BASE / "cache_v4.json").read_text(encoding="utf-8")) \
        if (BASE / "cache_v4.json").exists() else {}
    targets = [todo_id] if todo_id else list(cache.keys())
    for tid in targets:
        sup = cache.get(tid, {}).get("supplier", {})
        if not sup.get("supinfo_id"):
            print(f"[跳过] {tid}: 缓存里没有 supinfo_id（先跑主程序 FETCH 模式）")
            continue
        try:
            r = query_qualification_files(sup["supinfo_id"],
                                          sup.get("bill_type", "P0702"))
            fl = r.get("data", {}).get("supFilesBOList", []) or []
        except Exception as e:
            print(f"[中断] {tid} 拉文件列表失败: {e}（稍后重跑即可续传）")
            return
        dest = FILES_DIR / tid
        dest.mkdir(parents=True, exist_ok=True)
        print(f"[{tid}] {sup.get('name', '?')} 共 {len(fl)} 个附件")
        for f in fl:
            fu, fname = f.get("fileUrl"), f.get("fileUrl") and f.get("fileName")
            if not fu or not fname:
                continue
            uid = str(f.get("uploadId") or "")
            out = dest / f"{uid}_{_safe_filename(fname)}"
            expect = int(float(f.get("fileSize") or 0))
            if out.exists() and (not expect or out.stat().st_size == expect):
                print(f"  [已有] {fname}")
                continue
            for attempt in range(len(WAF_WAITS) + 1):
                try:
                    resp = requests.get(
                        "https://scpma.iccec.cn/apis/scpma/oss/downloadByUploadId",
                        headers=_scpma_headers(),
                        params={"fileUrl": fu, "fileName": fname},
                        timeout=180,
                        proxies={"http": None, "https": None})  # 不走系统代理
                except Exception as e:
                    print(f"  [重试{attempt + 1}] {fname}: {e}")
                    resp = None
                if resp is not None and resp.status_code == 200 \
                        and (not expect or len(resp.content) == expect):
                    out.write_bytes(resp.content)
                    print(f"  [下载] {fname} ({len(resp.content)} 字节)")
                    break
                # 超时 / 非200 / 字节不符 → 疑似 WAF 限流，长冷却后重试
                if attempt < len(WAF_WAITS):
                    wait = WAF_WAITS[attempt]
                    print(f"  [等待{wait}秒] {fname} 疑似被限流，冷却后重试"
                          f"（attempt {attempt + 1}/{len(WAF_WAITS)}）...")
                    time.sleep(wait)
                else:
                    print(f"  [放弃] {fname}: 多次重试失败，下次运行续传")
            time.sleep(delay)          # 防 WAF：文件间留足间隔
    print("\n下载完成。下一步: python textin_pipeline.py parse")


def scan_files():
    """扫描 files_cache/<todoId>/ → [(todoId, path, doc_type, md_path)]
    分类优先级：classify_file(文件名) → 失败则用缓存 materials_detail 兜底
    （缓存分类基于附件说明，比纯文件名准——"李作发.jpg"这种人名命名的身份证
      会被文件名分类漏掉，但缓存里有正确 legal_person_id 标记）

    2026-09-04 保密合规改造：优先读 files_cache_desens/（脱敏后目录），
    没有再 fallback 到 files_cache/（原始文件，仅用于 OCR 不能识别时人工补救）。
    """
    import re
    tasks = []
    # 优先用脱敏目录，没有再 fallback 到原始目录（保留兜底通道）
    scan_dir = FILES_DESENS_DIR if FILES_DESENS_DIR.exists() else FILES_DIR
    if not scan_dir.exists():
        return tasks
    cache = json.loads((BASE / "cache_v4.json").read_text(encoding="utf-8")) \
        if (BASE / "cache_v4.json").exists() else {}
    for todo_dir in sorted(scan_dir.iterdir()):
        if not todo_dir.is_dir():
            continue
        todo_id = todo_dir.name
        # 该供应商缓存里的文件分类（fileName → types）
        cache_types = {}
        for d in cache.get(todo_id, {}).get("materials_detail", []):
            fn = d.get("fileName", "")
            types = d.get("types", [])
            if fn and types:
                cache_types[fn] = types
        for f in sorted(todo_dir.iterdir()):
            if f.suffix.lower() not in FILE_EXTS:
                continue
            # 去 uploadId_ 前缀，匹配缓存 types（materials_detail 的权威分类）
            m = re.match(r"^\d+_(.+)$", f.name)
            bare = m.group(1) if m else f.name
            cached_types = cache_types.get(bare) or cache_types.get(f.name) or []
            if isinstance(cached_types, str):
                cached_types = [cached_types]
            cached_types = [t for t in cached_types if t]
            if cached_types:
                # 9/9 修复：缓存 types 优先（系统权威分类），且一个文件可能对应多个材料类型
                # （如「财务报表&纳税信用等级&售后服务.pdf」types=[after_sales_cert, tax_credit]），
                # 为每个类型各生成一个 task——同一文件 OCR 一次（.md 缓存复用）、按类型分别抽取。
                # 此前只按文件名分类成单一 doc_type，导致合并文件里的 tax_credit/after_sales
                # 漏核验（A03/A07「无核验数据」）。
                for dt in cached_types:
                    tasks.append((todo_id, f, dt))
                continue
            # 缓存无分类 → 文件名兜底
            doc_type = classify_file(f.name)
            if doc_type:
                tasks.append((todo_id, f, doc_type))
    return tasks


def run_parse(only_todo=None):
    """解析 files_cache 下所有文件 → textin_results.json

    2026-09-04 保密合规改造：先调用 desensitize 助手脱敏到 files_cache_desens/，
    然后 scan_files 改读脱敏目录，OCR 永远不接触原始敏感数据。
    """
    # 阶段 1.5：脱敏（缺库时优雅降级——不改任何文件，正常返回）
    idcard_fields = {}  # PaddleOCR 识别的身份证字段（方案B：替代 TextIn 二次 OCR）
    # 9/9 优化：单家审批（only_todo）时只脱敏该家目录，不再全量跑 PaddleOCR
    # 打码全部身份证——此前单家审批却全量打码 19 张身份证，是「卡在 70%」的主因
    desens_src = (FILES_DIR / only_todo) if only_todo else FILES_DIR
    desens_dst = (FILES_DESENS_DIR / only_todo) if only_todo else FILES_DESENS_DIR
    if desens_src.exists():
        try:
            from desensitize import desensitize_dir, desensitize_id_cards_with_paddle
            desensitize_dir(desens_src, desens_dst)
            log.info(f"已脱敏到 {desens_dst}，OCR 将读取脱敏后的文件")
            # 2026-09-09：PaddleOCR 精确打码身份证，覆盖粗比例结果（失败自动保留粗比例兜底）
            idcard_fields = desensitize_id_cards_with_paddle(desens_src, desens_dst)
        except ImportError:
            log.warning("desensitize 模块未找到，跳过脱敏（不推荐——敏感信息可能泄露）")
        except Exception as e:
            log.warning(f"脱敏失败：{e}，继续扫描原始目录（不推荐）")

    cache = json.loads((BASE / "cache_v4.json").read_text(encoding="utf-8")) \
        if (BASE / "cache_v4.json").exists() else {}
    tasks = [t for t in scan_files()
             if not only_todo or t[0] == only_todo]
    if not tasks:
        print(f"files_cache/ 下没有待解析文件"
              f"{'（指定 ' + only_todo + '）' if only_todo else ''}。")
        print("请先把供应商资质文件放入 files_cache/<todoId>/ 目录。")
        return

    results = {}
    if RESULTS_FILE.exists():
        try:
            results = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            results = {}

    # 企查查结果：有企业全名（缓存里常只有简称，名称/持有人比对需要全名）
    qcc = json.loads((BASE / "qcc_results.json").read_text(encoding="utf-8")) \
        if (BASE / "qcc_results.json").exists() else {}

    n_ok = n_fail = 0
    for todo_id, fpath, doc_type in tasks:
        supplier = cache.get(todo_id, {}).get("supplier", {})
        if supplier and not supplier.get("full_name"):
            code = supplier.get("social_credit_code") or supplier.get("credit_code")
            reg = (qcc.get(code) or {}).get("reg_info") or {}
            if reg.get("企业名称"):
                supplier = dict(supplier)
                supplier["full_name"] = reg["企业名称"]
            else:
                # 9/9 补：企查查无数据时，用待办申请单位全称/标题补 full_name，
                # 避免 ISO「持有人一致」用简称（如 ZPMC沈阳伟宸）误判为不一致。
                todo = cache.get(todo_id, {}).get("todo", {}) or {}
                apply_unit = str(todo.get("applyUnitName") or "").strip()
                title = str(todo.get("title") or "").strip()
                full = None
                if apply_unit and not re.search(r"其他|部门|项目经理部|项目部", apply_unit):
                    full = apply_unit
                elif title:
                    full = title.split("/")[0].strip()
                if full and len(full) > 4:
                    supplier = dict(supplier)
                    supplier["full_name"] = full
        # 2026-09-09 方案B：身份证用 PaddleOCR 识别结果，跳过 TextIn 二次 OCR
        if doc_type == "legal_person_id":
            try:
                rel_key = fpath.relative_to(FILES_DESENS_DIR).as_posix()
            except ValueError:
                rel_key = fpath.name
            paddle_info = idcard_fields.get(rel_key) or idcard_fields.get(fpath.name)
            if paddle_info and paddle_info.get("cards"):
                r = build_idcard_from_paddle(paddle_info["cards"], supplier)
                r["file"] = fpath.name
                r["parsed_at"] = datetime.now().isoformat(timespec="seconds")
                results.setdefault(todo_id, {})[doc_type] = r
                n_ok += 1
                nm = r.get("fields", {}).get("姓名") or ""
                vu = r.get("fields", {}).get("有效期至") or ""
                print(f"  [PaddleOCR] legal_person_id: 姓名={nm} / 有效期至={vu}")
                continue
            # PaddleOCR 失败（error）或无结果 → 明确写失败原因转人工，不再回退 TextIn
            reason = ((paddle_info or {}).get("error")
                      if isinstance(paddle_info, dict) else None) or "PaddleOCR 未返回识别结果"
            r = {
                "fields": {"姓名": None, "有效期至": None},
                "checks": {"正反面齐全": None},
                "issues": [f"身份证打码识别失败：{reason}（转人工核验，请供应商确认身份证格式/清晰度）"],
            }
            r["file"] = fpath.name
            r["parsed_at"] = datetime.now().isoformat(timespec="seconds")
            results.setdefault(todo_id, {})[doc_type] = r
            n_ok += 1
            print(f"  [PaddleOCR] legal_person_id 失败转人工：{reason}")
            continue

        # 同目录已有缓存的 .md 就直接用（extract 模式 / 重跑不烧额度）
        md_path = fpath.with_suffix(fpath.suffix + ".md")
        detail_path = fpath.with_suffix(fpath.suffix + ".detail.json")
        if md_path.exists():
            md = md_path.read_text(encoding="utf-8")
            detail = json.loads(detail_path.read_text(encoding="utf-8")) \
                if detail_path.exists() else None
        else:
            try:
                print(f"[解析] {todo_id}/{fpath.name} ({doc_type}) ...")
                md, detail = parse_file_textin(fpath)
                # 9/7：缓存 markdown——身份证过滤注释（敏感信息不落盘），
                # 其他类型（营业执照等证照内容也在注释里）原样保留
                if doc_type == "legal_person_id":
                    md_path.write_text(
                        re.sub(r"<!--.*?-->", "", md, flags=re.S), encoding="utf-8")
                else:
                    md_path.write_text(md, encoding="utf-8")
                if detail:
                    detail_path.write_text(
                        json.dumps(_sanitize_detail(doc_type, detail),
                                   ensure_ascii=False), encoding="utf-8")
                time.sleep(1)
            except Exception as e:
                print(f"  [失败] {fpath.name}: {e}")
                results.setdefault(todo_id, {})[doc_type] = {
                    "file": fpath.name, "error": str(e)}
                n_fail += 1
                continue
        try:
            r = extract(doc_type, md, supplier, detail)
            r["file"] = fpath.name
            r["parsed_at"] = datetime.now().isoformat(timespec="seconds")
            results.setdefault(todo_id, {})[doc_type] = r
            n_ok += 1
            status = "✓" if not r.get("issues") else "⚠ " + "；".join(r["issues"][:2])
            print(f"  [抽取] {doc_type}: {status}")
        except Exception as e:
            print(f"  [抽取失败] {fpath.name}: {e}")
            results.setdefault(todo_id, {})[doc_type] = {
                "file": fpath.name, "error": str(e)}
            n_fail += 1

    # 避免 OCR 子进程被中断时破坏已有结果文件。
    tmp = RESULTS_FILE.with_suffix(RESULTS_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, RESULTS_FILE)
    print(f"\n完成: 成功 {n_ok} | 失败 {n_fail} → {RESULTS_FILE.name}")


# ============================================================
# 清理 files_cache/（9/2 确认：审批一周后可清理原始文件）
# ============================================================
def clean_files_cache(days=7, do_delete=False):
    """列出 files_cache/ 下超过 N 天的 todoId 目录，--go 才真删
    策略：删除原始文件（jpg/png/pdf），保留 .md 缓存（重跑 extract 不耗 TextIn 页数）
    和 textin_results.json（结构化结果）
    绝不动个人/系统目录（遵守文件操作授权）"""
    if not FILES_DIR.exists():
        print("files_cache/ 还不存在，无内容可清。")
        return
    import time
    threshold = time.time() - days * 86400
    targets = []
    for tid_dir in sorted(FILES_DIR.iterdir()):
        if not tid_dir.is_dir():
            continue
        # 用目录 mtime（最近文件下载时间）判断年龄
        mtime = max((f.stat().st_mtime for f in tid_dir.iterdir() if f.is_file()),
                    default=tid_dir.stat().st_mtime)
        age_days = (time.time() - mtime) / 86400
        files = [f for f in tid_dir.iterdir() if f.is_file()]
        raw = [f for f in files if not f.name.endswith(".md")]
        cached = [f for f in files if f.name.endswith(".md")]
        raw_size = sum(f.stat().st_size for f in raw) / 1024 / 1024
        if mtime < threshold:
            targets.append((tid_dir.name, age_days, raw_size, raw, cached))
    if not targets:
        print(f"files_cache/ 下没有超过 {days} 天的待办，无需清理。")
        return
    print(f"files_cache/ 下超过 {days} 天的待办目录（{len(targets)} 个）:\n")
    total_raw_size = 0
    for name, age, size, raw, cached in targets:
        total_raw_size += size
        print(f"  {name}  龄{age:.0f}天  原始文件{len(raw)}个({size:.1f}MB)  "
              f"缓存.md{len(cached)}个")
    print(f"\n合计原始文件 {total_raw_size:.1f}MB（清理后释放，.md 缓存保留）")
    if not do_delete:
        print(f"\n→ 预演模式（未删）。确认要清理时执行：")
        print(f"   python textin_pipeline.py clean --go --days {days}")
        return
    # 二次确认：列出每个待办 → 删除
    print("\n开始删除...")
    deleted_files = deleted_bytes = 0
    for name, age, size, raw, cached in targets:
        for f in raw:
            f.unlink()
            deleted_files += 1
            deleted_bytes += f.stat().st_size
        print(f"  {name}: 删 {len(raw)} 个原始文件，保留 {len(cached)} 个 .md 缓存")
    print(f"\n清理完成：删除 {deleted_files} 个原始文件，"
          f"释放 {deleted_bytes/1024/1024:.1f}MB。")


# ============================================================
# 离线自测（mock材料）
# ============================================================
def run_test():
    print("== TextIn 抽取逻辑离线测试 ==")
    supplier = {
        "name": "测试公司", "full_name": "测试科技有限公司",
        "social_credit_code": "91110108TESTCODE01",
        "legal_person": "张三", "registered_capital": 800.0,
        "busi_scope": "技术服务、技术开发；软件开发；计算机系统服务",
    }
    ok = 0

    # 1. 营业执照（一致）
    lic = extract("business_license",
                  "统一社会信用代码: 91110108TESTCODE01\n名称: 测试科技有限公司\n"
                  "法定代表人: 张三\n注册资本: 800万元\n营业期限: 2015-01-01 至 2045-01-01",
                  supplier)
    assert lic["checks"].get("信用代码一致") and lic["checks"].get("法人一致") \
        and lic["checks"].get("注册资本一致"), lic
    ok += 1
    print("  [✓] 营业执照·信息一致")

    # 1b. 营业执照（TextIn 真实输出形态：md表格+加粗）
    lic_t = extract("business_license",
                    "| 统一社会信用代码 | 91110108TESTCODE01 |\n| --- | --- |\n"
                    "| 名称 | **测试科技有限公司** |\n| 类型 | 有限责任公司 |\n"
                    "| 法定代表人 | 张三 |\n| 注册资本 | 800万元 |", supplier)
    assert lic_t["checks"].get("信用代码一致") and lic_t["checks"].get("法人一致") \
        and lic_t["checks"].get("注册资本一致") and lic_t["checks"].get("名称一致"), lic_t
    ok += 1
    print("  [✓] 营业执照·md表格+加粗排版")

    # 2. 营业执照（法人不一致）
    lic2 = extract("business_license",
                   "统一社会信用代码: 91110108TESTCODE01\n名称: 测试科技有限公司\n"
                   "法定代表人: 李四\n注册资本: 800万元", supplier)
    assert lic2["issues"] and "法人" in lic2["issues"][0], lic2
    ok += 1
    print("  [✓] 营业执照·法人不一致检出")

    # 2b. 营业执照·经营范围一致（9/2 新增要点）
    lic3 = extract("business_license",
                   "统一社会信用代码: 91110108TESTCODE01\n名称: 测试科技有限公司\n"
                   "法定代表人: 张三\n注册资本: 800万元\n"
                   "经营范围: 技术服务、技术开发；软件开发；计算机系统服务\n登记机关: 北京市监局",
                   supplier)
    assert lic3["checks"].get("经营范围一致") is True, lic3
    ok += 1
    print("  [✓] 营业执照·经营范围一致")

    # 2c. 营业执照·经营范围不一致检出
    lic4 = extract("business_license",
                   "统一社会信用代码: 91110108TESTCODE01\n名称: 测试科技有限公司\n"
                   "法定代表人: 张三\n注册资本: 800万元\n"
                   "经营范围: 建筑工程施工；市政工程；道路桥梁施工\n登记机关: 北京市监局",
                   supplier)
    assert not lic4["checks"].get("经营范围一致", False) \
        and any("经营范围" in i for i in lic4["issues"]), lic4
    ok += 1
    print("  [✓] 营业执照·经营范围不一致检出")

    # 2d. 营业执照·概括式经营范围（新版执照）→ 转人工
    lic5 = extract("business_license",
                   "统一社会信用代码: 91110108TESTCODE01\n名称: 测试科技有限公司\n"
                   "法定代表人: 张三\n注册资本: 800万元\n"
                   "经营范围: 软件开发（具体经营项目请登录国家企业信用信息公示系统查询，"
                   "网址http://www.gsxt.gov.cn/）\n登记机关: 北京市监局",
                   supplier)
    assert lic5["checks"].get("经营范围一致") is None \
        and any("概括式" in i for i in lic5["issues"]), lic5
    ok += 1
    print("  [✓] 营业执照·概括式经营范围转人工")

    # 2e. 售后承诺书·落款3个月内通过（9/2 确认规则）
    from datetime import timedelta
    recent = (date.today() - timedelta(days=20)).strftime("%Y年%m月%d日")
    old = (date.today() - timedelta(days=120)).strftime("%Y年%m月%d日")
    stmt1 = extract("after_sales_statement",
                    f"售后服务承诺书\n我公司就售后服务郑重承诺如下……\n"
                    f"单位名称（盖章）：测试科技有限公司\n日期：{recent}")
    assert stmt1["checks"].get("落款时间在3个月内") is True, stmt1
    ok += 1
    print("  [✓] 售后承诺书·落款3个月内通过")

    # 2f. 售后承诺书·落款超3个月检出
    stmt2 = extract("after_sales_statement",
                    f"售后服务承诺书\n我公司就售后服务郑重承诺如下……\n"
                    f"单位名称（盖章）：测试科技有限公司\n日期：{old}")
    assert stmt2["checks"].get("落款时间在3个月内") is False \
        and any("超过3个月" in i for i in stmt2["issues"]), stmt2
    ok += 1
    print("  [✓] 售后承诺书·落款超3个月检出")

    # 3. 身份证（姓名一致+有效期）
    idc = extract("legal_person_id",
                  "姓 名 张三\n公民身份号码 110108199001010011\n"
                  "有效期限 2015.01.01 - 2035.01.01", supplier)
    assert idc["checks"].get("姓名与法人一致") and idc["checks"].get("在有效期内"), idc
    ok += 1
    print("  [✓] 身份证·姓名一致且在有效期")

    # 4. 身份证（过期）
    idc2 = extract("legal_person_id",
                   "姓 名 张三\n公民身份号码 110108199001010011\n"
                   "有效期限 2010.01.01 - 2020.01.01", supplier)
    assert not idc2["checks"].get("在有效期内"), idc2
    ok += 1
    print("  [✓] 身份证·过期检出")

    # 5. 纳税等级A
    tax = extract("tax_credit",
                  "国家税务总局\nXX省税务局\n纳税人名称: 测试科技有限公司\n"
                  "年度: 2025\n纳税信用级别: A")
    assert tax["checks"].get("C级及以上") and tax["fields"]["纳税信用级别"] == "A", tax
    ok += 1
    print("  [✓] 纳税等级·A级通过")

    # 6. 纳税等级D
    tax2 = extract("tax_credit", "纳税信用级别: D\n2025年度")
    assert not tax2["checks"].get("C级及以上") and tax2["issues"], tax2
    ok += 1
    print("  [✓] 纳税等级·D级检出")

    # 7. 财报（三项全达标，比率直接印出）
    fin = extract("financial_report",
                  "2025年度审计报告\n流动比率 120%\n资产负债率 55%\n"
                  "经营活动产生的现金流量净额 350.20 万元")
    assert fin["checks"].get("资产负债率≤65%") and fin["checks"].get("流动比率≥100%") \
        and fin["checks"].get("经营现金流>0"), fin
    ok += 1
    print("  [✓] 财报·三项达标")

    # 8. 财报（科目计算：负债/资产=1300/2000=65%边界，流动比率90%，现金流负）
    fin2 = extract("financial_report",
                   "2025年度\n资产负债表\n资产合计 2,000\n负债合计 1,300\n"
                   "流动资产合计 900\n流动负债合计 1,000\n"
                   "现金流量表\n经营活动产生的现金流量净额 -50.5")
    assert fin2["checks"].get("资产负债率≤65%"), fin2   # 65%整→True边界（≤含等号）
    assert fin2["fields"]["资产负债率%"] == 65.0, fin2
    assert not fin2["checks"].get("流动比率≥100%"), fin2
    assert not fin2["checks"].get("经营现金流>0"), fin2
    ok += 1
    print("  [✓] 财报·科目计算+边界值(65%)+两项不符检出")

    # 9. ISO证书（持有人一致+效期）
    iso = extract("iso9001",
                  "质量管理体系认证证书\n证书编号: 00123Q45678R0M\n"
                  "获证组织: 测试科技有限公司\n认证状态: 有效\n"
                  "有效期至: 2027-06-30", supplier)
    assert iso["checks"].get("持有人一致") and iso["checks"].get("在有效期内") \
        and iso["checks"].get("证书状态有效"), iso
    ok += 1
    print("  [✓] ISO9001·持有人一致且有效")

    # 10. ISO证书（过期+持有人不一致）
    iso2 = extract("iso14001",
                   "获证组织: 别的公司\n有效期至: 2020-01-01", supplier)
    assert not iso2["checks"].get("持有人一致") and not iso2["checks"].get("在有效期内"), iso2
    ok += 1
    print("  [✓] ISO14001·过期+持有人不一致检出")

    # 11. 授权书
    auth = extract("authorization",
                   "授权书\n授权方: 某某制造有限公司\n被授权方: 测试科技有限公司\n"
                   "授权期限: 2026-01-01 至 2028-12-31", supplier)
    assert auth["checks"].get("被授权方为本公司") and auth["checks"].get("在有效期内"), auth
    ok += 1
    print("  [✓] 授权书·被授权方一致且有效")

    print(f"\n== 全部 {ok} 项测试通过 ==")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "test":
        run_test()
    elif cmd == "scan":
        tasks = scan_files()
        print(f"files_cache/ 共 {len(tasks)} 个待解析文件:")
        for tid, f, dt in tasks[:30]:
            print(f"  {tid}/{f.name} → {dt}")
        if not tasks:
            print("  （空。请将资质文件放入 files_cache/<todoId>/ 目录）")
    elif cmd == "download":
        download_supplier_files(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "parse":
        run_parse(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "extract":
        run_parse(sys.argv[2] if len(sys.argv) > 2 else None)  # 有.md缓存时不调API
    elif cmd == "clean":
        # 用 argparse 风格：--go 确认 / --days N 自定义天数（默认 7）
        do_delete = "--go" in sys.argv
        days = 7
        if "--days" in sys.argv:
            i = sys.argv.index("--days")
            if i + 1 < len(sys.argv):
                try: days = int(sys.argv[i + 1])
                except: pass
        clean_files_cache(days=days, do_delete=do_delete)
    else:
        print(__doc__)
