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

# 各审核项对应"应补材料"的具体描述（Quinn 9/3 要求：审批意见写具体清单）
# ISO 三项 (C1_02/03/04) 合并为一条
SUPPLEMENT_TEMPLATES = {
    "A01":  "营业执照副本（须含统一社会信用代码及完整经营范围明细）",
    "A02":  "法人有效身份证正反面（须在有效期内）",
    "A03":  "国家税务总局网站下载的上年度纳税信用等级证明（需C级及以上）",
    "A07":  "售后服务证明（厂家出具的售后服务证明函，落款在3个月内；或售后服务五星认证证书）",
    "A08":  "上年度经审计的财报（须含资产负债表/利润表/现金流量表）",
    "A13":  "检验检测资质证明（CMA 资质认定证书 / CNAS 实验室认可证书等，须在有效期内）",
    "C1_02": "ISO 9001、14001 和 45001 体系认证证书（须为该公司认证且在有效期内）",
    "C1_03": "ISO 9001、14001 和 45001 体系认证证书（须为该公司认证且在有效期内）",
    "C1_04": "ISO 9001、14001 和 45001 体系认证证书（须为该公司认证且在有效期内）",
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
    textin_for_doc = textin.get(tid, {}) if textin else {}
    name = s2.get("name", tid)
    brief = _supplier_brief(s2, cache_entry)
    checklist = s2.get("checklist", []) or []
    t_issues = s2.get("textin_issues", []) or []
    q_issues = s2.get("qcc_issues", []) or []
    decision = s2.get("decision", "manual")

    # 分类：fail / pass / skip / pending-or-manual
    fail_items = [c for c in checklist if c.get("status") == "fail"]
    pass_items = [c for c in checklist if c.get("status") == "pass"]
    skip_items = [c for c in checklist if c.get("status") == "skip"]
    pending_items = [c for c in checklist
                     if c.get("status") in ("pending", "partial", "manual")]

    # 判断整体处置建议
    # 有 fail 项 → 看是否全是硬伤
    hard_fails = []
    soft_fails = []
    fail_issue_map = {}  # cid → issue 描述
    for c in fail_items:
        cid = c.get("id", "")
        cname = c.get("name", CL_NAME_FALLBACK.get(cid, cid))
        # 找对应 OCR issue
        issue_desc = c.get("detail") or c.get("message") or ""
        # 从 textin_issues 里按关键词匹配（OCR 增强时已写进去）
        for ti in t_issues:
            if cname.split("/")[0].strip() in ti or cid in ti:
                issue_desc = ti
                break
        fail_issue_map[cid] = issue_desc
        if _is_hard_fail(issue_desc):
            hard_fails.append((cid, cname, issue_desc))
        else:
            soft_fails.append((cid, cname, issue_desc))

    # 整体建议：硬伤 ≥1 → 建议退回；其余 fail/pending → 转人工；全 pass → 建议同意
    if hard_fails:
        suggest_action = "退回"
        suggest_reason = "存在须退回补材料/整改的客观硬伤"
    elif fail_items or pending_items:
        suggest_action = "转人工"
        suggest_reason = "存在需人工核验的事项"
    else:
        suggest_action = "同意"
        suggest_reason = "所有审核项均通过"

    # ---- 拼接意见文本 ----
    lines = []
    today = date.today().strftime("%Y年%m月%d日")
    lines.append(f"经审核，{name}{brief}：")
    lines.append("")

    # 动态段落编号：每次取下一个中文数字
    state = {"idx": 0}

    def take_sec():
        i = state["idx"]
        state["idx"] += 1
        return _CN_NUM[i] if i < len(_CN_NUM) else str(i + 1)

    # 一、须退回补材料/整改（硬伤）
    if hard_fails:
        lines.append(f"{take_sec()}、须退回补材料/整改（以下为客观不符项）：")
        for i, (cid, cname, desc) in enumerate(hard_fails, 1):
            desc = _strip_dup_prefix(cname, desc)
            lines.append(f"  {i}. [{cid}] {cname}：{desc}")
        lines.append("")

    # 二、需人工核验（软 fail + pending）
    soft_pending = []
    for c in pending_items:
        cid = c.get("id", "")
        cname = c.get("name", CL_NAME_FALLBACK.get(cid, cid))
        msg = c.get("message") or c.get("detail") or "需人工核验"
        msg = _strip_dup_prefix(cname, msg)
        soft_pending.append((cid, cname, msg))
    soft_pending.extend([(c, n, _strip_dup_prefix(n, d)) for c, n, d in soft_fails])

    if soft_pending:
        lines.append(f"{take_sec()}、需人工核验事项：")
        for i, (cid, cname, desc) in enumerate(soft_pending, 1):
            lines.append(f"  {i}. [{cid}] {cname}：{desc}")
        lines.append("")

    # 三、核验通过项
    if pass_items:
        lines.append(f"{take_sec()}、核验通过项：")
        for c in pass_items:
            cid = c.get("id", "")
            cname = c.get("name", CL_NAME_FALLBACK.get(cid, cid))
            summary = _build_pass_summary(c, textin_for_doc)
            lines.append(f"  - [{cid}] {cname}：{summary}")
        lines.append("")

    # 四、不适用项（简略）
    if skip_items:
        skip_names = "、".join(c.get("name", c.get("id", ""))
                              for c in skip_items)
        lines.append(f"{take_sec()}、不适用项：{skip_names}")
        lines.append("")

    # 五、企查查发现
    if q_issues:
        lines.append(f"{take_sec()}、企查查核验发现：")
        for i, q in enumerate(q_issues, 1):
            lines.append(f"  {i}. {q}")
        lines.append("")

    # 最终建议（具体审批意见格式，Quinn 9/3 要求）
    lines.append(f"{take_sec()}、审批意见")
    if suggest_action == "退回":
        # 按材料类别汇总应补材料，ISO 三项合并为一条
        supplement_items = []
        seen_iso = False
        for cid, cname, desc in hard_fails:
            if cid in ("C1_02", "C1_03", "C1_04"):
                if not seen_iso:
                    supplement_items.append(("ISO", SUPPLEMENT_TEMPLATES["C1_02"]))
                    seen_iso = True
                continue
            tmpl = SUPPLEMENT_TEMPLATES.get(cid)
            if tmpl:
                supplement_items.append((cid, tmpl))
        # 去重保序（同一 cid 只保留一条）
        seen = set()
        dedup = []
        for cid, txt in supplement_items:
            if cid not in seen:
                seen.add(cid)
                dedup.append(txt)
        lines.append("退回。")
        if dedup:
            lines.append("请补充资质文件：")
            for i, item in enumerate(dedup, 1):
                lines.append(f"  {i}. {item}")
            lines.append("补充后重新提交。")
        else:
            lines.append("请整改后重新提交。")
    elif suggest_action == "转人工":
        lines.append("转人工。")
        if soft_pending:
            lines.append("需核验：")
            for i, (cid, cname, desc) in enumerate(soft_pending, 1):
                lines.append(f"  {i}. {cname}：{desc}")
            lines.append("核验通过后决定。")
    else:
        lines.append("同意。")
        lines.append("（建议人工最终确认后正式准入）")

    lines.append("")
    lines.append(f"（自动生成于 {today}，依据《中港采购发〔2025〕161号》准入审查规则）")
    return "\n".join(lines)


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
