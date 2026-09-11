#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 stage2_results.json + textin_results.json + cache_v4.json 生成
可直接粘贴进 ICCEC 审批框的最终意见文本。

用法:
    python gen_opinion.py [todoId1] [todoId2] ...      (默认全部)
    python gen_opinion.py --all                        (显式全量)
输出:
    D:\\WorkBuddy\\最终审批意见_<todoId>_<公司名>.txt   (每家一个文件，方便复制)
    控制台同时打印
"""
import json, sys, re
from pathlib import Path
from datetime import date

BASE = Path(__file__).parent
OUT_DIR = Path(r"D:\WorkBuddy")


def _load(path):
    p = BASE / path
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


# checklist 项编号 → 中文项目名（与 rules.yaml 对齐）
CL_NAME_FALLBACK = {
    "A01": "法律主体资格",
    "A02": "法人身份证明",
    "A03": "纳税信用等级",
    "A04": "产品合规",
    "A05": "经营场所",
    "A06": "产品/服务质量",
    "A07": "售后服务",
    "A08": "资金财务状况",
    "A09": "无恶意拖欠工资",
    "A10": "商业信誉",
    "A11": "法律纠纷",
    "A12": "系统内入库",
    "A13": "检验检测资质",
    "B01": "境外判断",
    "C1_01": "资质要求",
    "C1_02": "ISO9001",
    "C1_03": "ISO14001",
    "C1_04": "ISO45001",
    "C1_05": "生产许可证",
}

# 关键词：fail 项 issues 含这些词 → 客观硬伤 → 建议退回
HARD_FAIL_KEYWORDS = [
    "应为", "已过期", "持有人", "不一致", "未上传", "材料缺失",
    "非经审计", "纳税申报表", "概括式", "缺少",
]

# 各审核项对应"应补材料"的具体描述（9/3 要求：审批意见写具体清单）
# ISO 三项 (C1_02/03/04) 合并为一条
SUPPLEMENT_TEMPLATES = {
    "A01":  "营业执照副本（须含统一社会信用代码及完整经营范围明细）",
    "A02":  "法人有效身份证正反面（须在有效期内）",
    "A03":  "国家税务总局网站下载的上年度纳税信用等级证明（需C级及以上）",
    "A07":  "售后服务证明（厂家出具的售后服务证明函，落款在3个月内；或售后服务五星认证证书）",
    "A08":  "上年度经审计的财报（须含资产负债表/利润表/现金流量表）",
    "A13":  "检验检测资质证明（CMA 资质认定证书 / CNAS 实验室认可证书等，须在有效期内）",
    "C1_02": "ISO 9001 认证证书（须为该公司认证且在有效期内）",
    "C1_03": "ISO 14001 认证证书（须为该公司认证且在有效期内）",
    "C1_04": "ISO 45001 认证证书（须为该公司认证且在有效期内）",
    "C1_05": "生产许可证或强制认证证明（须在有效期内）",
    "D1_06": "产品生产企业的代理协议或产品销售授权资质（授权一方须为该贸易公司且在有效期内）",
}


def _is_hard_fail(issue_text: str) -> bool:
    """OCR issue 内容是否构成客观硬伤（应退回）"""
    return any(k in issue_text for k in HARD_FAIL_KEYWORDS)


def _supplier_brief(s2, cache_entry):
    """生成公司概况一句话"""
    name = s2.get("name", "")
    type_desc = s2.get("type_desc", "")
    sup = cache_entry.get("supplier", {}) if cache_entry else {}
    # 兼容多种字段名（cache_v4 用 registered_capital）
    cap = (sup.get("registered_capital") or sup.get("reg_capl")
           or sup.get("reg_capital") or sup.get("reg_capital_wan"))
    cap_str = f"{cap:g}万元" if isinstance(cap, (int, float)) else (f"{cap}万元" if cap else "")
    years = sup.get("established_years") or sup.get("establish_years")
    years_str = f"成立{years}年" if years else ""
    # 法人
    legal = sup.get("legal_person", "")
    legal_str = f"法人{legal}" if legal else ""
    # 信用代码
    code = sup.get("social_credit_code") or sup.get("credit_code") or ""
    code_str = f"统一社会信用代码{code}" if code else ""

    parts = []
    if type_desc:
        parts.append(type_desc)
    if cap_str:
        parts.append(f"注册资本{cap_str}")
    if years_str:
        parts.append(years_str)
    if legal_str:
        parts.append(legal_str)
    if code_str:
        parts.append(code_str)
    return "（" + "，".join(parts) + "）" if parts else ""


def _suspect_domestic_tip(supplier):
    """疑似国内供应商提示：isOverseas=1 但国家/地区信息为中国。

    返回追加到最终审批意见末尾的可复制提示文字；不符合条件返回空串。
    """
    if not supplier:
        return ""
    is_overseas = supplier.get("is_overseas")
    country = str(supplier.get("country_name") or "").strip()
    if is_overseas and country in ("China", "中国", "CHINA"):
        return ("提示：该供应商「是否海外供应商」字段标记为海外，但国家/地区信息为中国，"
                "疑似国内供应商。建议将该供应商「是否海外供应商」字段修改为国内供应商后重新提交。")
    return ""


def _strip_dup_prefix(cname, desc):
    """去掉 issue 描述里与项目名重复的前缀（如"纳税信用等级：纳税信用等级：..."）"""
    if not desc:
        return desc
    # 去掉"项目名：" 前缀
    if desc.startswith(cname + "："):
        return desc[len(cname) + 1:]
    if desc.startswith(cname + ":"):
        return desc[len(cname) + 1:]
    return desc


# 中文编号（用于段落）
_CN_NUM = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]


def _build_pass_summary(c, textin_for_doc):
    """为通过的 checklist 项生成一句话说明"""
    cid = c.get("id", "")
    cname = c.get("name", CL_NAME_FALLBACK.get(cid, cid))
    detail = c.get("detail", "") or c.get("message", "") or ""

    # 从对应 OCR 抽取结果里取关键字段补充
    doc_map = {
        "A01": "business_license",
        "A02": "legal_person_id",
        "A03": "tax_credit",
        "A08": "financial_report",
        "C1_02": "iso9001",
        "C1_03": "iso14001",
        "C1_04": "iso45001",
        "C1_05": "production_license",
    }
    doc_type = doc_map.get(cid)
    if doc_type and textin_for_doc.get(doc_type):
        info = textin_for_doc[doc_type]
        fields = info.get("fields") or {}
        if cid == "A01":
            code = fields.get("统一社会信用代码", "")
            cap = fields.get("注册资本", "")
            return f"营业执照：统一社会信用代码{code}、注册资本{cap}与系统填写一致。"
        if cid == "A02":
            name_id = fields.get("姓名", "")
            exp = fields.get("有效期至", "")
            return f"法人身份证：姓名{name_id}、有效期{exp}，与系统法人一致。"
        if cid == "A03":
            lvl = fields.get("纳税信用级别", "")
            year = fields.get("评价年度", "")
            return f"纳税信用：{year}年度{lvl}级，C级以上，国税总局来源。"
        if cid == "A08":
            ocf = fields.get("经营现金流净额", "")
            year = fields.get("报告年度", "")
            return f"经审计财报：{year}年度，经营现金流净额{ocf}（>0）。"
        if cid in ("C1_02", "C1_03", "C1_04"):
            return f"{cname}证书持有人与供应商一致，证书在有效期内。"
        if cid == "C1_05":
            return f"生产许可证在有效期内。"
    return detail or f"{cname}通过"


def build_opinion(tid, s2, textin, cache):
    """生成最终审批意见文本"""
    cache_entry = cache.get(tid, {})
    supplier = cache_entry.get("supplier", {}) if cache_entry else {}
    textin_for_doc = textin.get(tid, {}) if textin else {}
    name = s2.get("name", tid)
    brief = _supplier_brief(s2, cache_entry)
    checklist = s2.get("checklist", []) or []
    t_issues = s2.get("textin_issues", []) or []
    q_issues = s2.get("qcc_issues", []) or []
    decision = s2.get("decision", "manual")

    # 9/6：疑似国内供应商提示（isOverseas=1 但国家为中国），追加到意见末尾
    suspect_tip = _suspect_domestic_tip(supplier)

    # 9/6：skip 项（境外/集团独有不适用）直接返回不适用意见，不走标准核验意见拼接
    if decision == "skip":
        base = s2.get("opinion") or s2.get("skip_reason") or "该供应商不适用标准审批流程"
        return base + ("\n\n" + suspect_tip if suspect_tip else "")

    # 分类：fail / pass / skip / 待人工（pending/partial/manual）
    fail_items = [c for c in checklist if c.get("status") == "fail"]
    manual_items = [c for c in checklist
                    if c.get("status") in ("pending", "partial", "manual")]

    # fail 细分：缺材料（detail 含"缺少/未上传/材料缺失"，可退回补办）vs 其他异常（不一致/已过期/核验问题，需整改）
    supplement_fails = []   # 缺材料，退回补办
    other_fails = []        # 其他 fail（整改）
    for c in fail_items:
        cid = c.get("id", "")
        cname = c.get("name", CL_NAME_FALLBACK.get(cid, cid))
        detail = c.get("detail") or c.get("message") or ""
        if ("缺少" in detail or "未上传" in detail or "材料缺失" in detail
                or "需上传" in detail):
            supplement_fails.append((cid, cname, detail))
        else:
            other_fails.append((cid, cname, detail))

    # 整体建议：缺材料 fail ≥1 → 退回；其余 fail/待人工 → 转人工；全 pass → 同意
    if supplement_fails:
        suggest_action = "退回"
    elif fail_items or manual_items:
        suggest_action = "转人工"
    else:
        suggest_action = "同意"

    # 9/11：A08 财报未传 + 企查查无数据 → 意见中独立列出「经审计的上年度财报」
    a08_needs_financial = False
    for c in manual_items:
        if c.get("id") == "A08":
            a08_detail = c.get("detail") or ""
            if not supplier.get("has_financial_report") and \
                    ("未查到" in a08_detail or "无数据" in a08_detail or "数据为空" in a08_detail):
                a08_needs_financial = True
            break

    # ---- 9/7 改造：完整列出所有异常（与综合核验表联动），单行分号分隔 ----
    lines = []

    def _clip(s, n=100):
        s = str(s or "")
        return s[:n] + "…" if len(s) > n else s

    # 决策 1：退回（补材料清单 + 整改/人工项）
    if suggest_action == "退回":
        supplement_items = []
        # ISO 三认证：只列实际失败的项（C1_02=9001 / C1_03=14001 / C1_04=45001；D1_03/04/05 同理）
        iso_fail_map = {
            "C1_02": "ISO 9001", "C1_03": "ISO 14001", "C1_04": "ISO 45001",
            "D1_03": "ISO 9001", "D1_04": "ISO 14001", "D1_05": "ISO 45001",
        }
        failed_iso = []
        for cid, cname, desc in supplement_fails:
            iso_name = iso_fail_map.get(cid)
            if iso_name and iso_name not in failed_iso:
                failed_iso.append(iso_name)
        if failed_iso:
            supplement_items.append(
                "、".join(failed_iso) + " 体系认证证书（须为该公司认证且在有效期内）")
        for cid, cname, desc in supplement_fails:
            if cid in iso_fail_map:
                continue
            tmpl = SUPPLEMENT_TEMPLATES.get(cid)
            if tmpl:
                supplement_items.append(tmpl)
        # 去重保序
        seen = set()
        dedup = [x for x in supplement_items
                 if not (x in seen or seen.add(x))]
        if dedup:
            items_str = "；".join(f"{i+1}. {x}" for i, x in enumerate(dedup))
            lines.append(f"退回。请补充资质文件：{items_str}。补充后重新提交。")
        else:
            lines.append("退回。具体见上方审查报告。")
        # 其他异常（非缺材料的 fail + 待人工项）→ 一并列出，让供应商知道所有问题
        other_issues = []
        for cid, cname, desc in other_fails:
            other_issues.append(f"[{cid}]{cname}：{_clip(desc)}")
        for c in manual_items:
            cid = c.get("id", "")
            cname = c.get("name", CL_NAME_FALLBACK.get(cid, cid))
            desc = c.get("detail") or c.get("message") or "需人工核验"
            other_issues.append(f"[{cid}]{cname}：{_clip(desc)}")
        if a08_needs_financial:
            lines.append("需补充：经审计的上年度财报。")
        if other_issues:
            lines.append("另需整改/核实：" + "；".join(other_issues) + "。")
    # 决策 2：转人工（完整列出所有异常项）
    elif suggest_action == "转人工":
        reasons = []
        for cid, cname, desc in other_fails:
            reasons.append(f"[{cid}]{cname}：{_clip(desc)}")
        for c in manual_items:
            cid = c.get("id", "")
            cname = c.get("name", CL_NAME_FALLBACK.get(cid, cid))
            desc = c.get("detail") or c.get("message") or "需人工核验"
            reasons.append(f"[{cid}]{cname}：{_clip(desc)}")
        if a08_needs_financial:
            lines.append("需补充：经审计的上年度财报。")
        if not reasons:
            reasons.append("部分审核项需人工核验")
        lines.append("转人工。" + "；".join(reasons))
    # 决策 3：同意
    else:
        lines.append("同意。各项审核均通过，建议后续常规管理。")

    # 9/5 改造：去掉末尾的"另经企查查核验..."——这是给审核员看的，不给供应商
    result = "\n".join(lines)
    # 9/6：疑似国内供应商（isOverseas=1 但国家为中国）追加提示
    if suspect_tip:
        result += "\n\n" + suspect_tip
    return result


def main():
    stage2 = _load("stage2_results.json")
    textin = _load("textin_results.json")
    cache = _load("cache_v4.json")

    args = [a for a in sys.argv[1:] if a != "--all"]
    ids = args or list(stage2.keys())

    for tid in ids:
        if tid not in stage2:
            print(f"[跳过] {tid} 不在 stage2_results")
            continue
        s2 = stage2[tid]
        opinion = build_opinion(tid, s2, textin, cache)
        name = s2.get("name", tid)
        # 文件名去非法字符
        safe_name = re.sub(r'[\\/:*?"<>|]', "_", name)[:30]
        out_path = OUT_DIR / f"最终审批意见_{tid}_{safe_name}.txt"
        out_path.write_text(opinion, encoding="utf-8")
        print(f"\n{'='*70}")
        print(f"# {tid} {name}")
        print('='*70)
        print(opinion)
        print(f"\n[已保存] {out_path}")


if __name__ == "__main__":
    main()
