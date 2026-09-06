#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""从 stage2_results.json + textin_results.json 生成供应商材料审查对照表 HTML
用法: python gen_stage2_report.py [todoId1] [todoId2] ...   (默认全部)
输出: D:\WorkBuddy\供应商材料审查_YYYYMMDD.html
"""
import json, sys, datetime
from datetime import date
from pathlib import Path

# 复用 gen_opinion 的意见生成逻辑（9/3：意见表与审查报告整合成一个 HTML）
import gen_opinion

BASE = Path(__file__).parent
OUT_DIR = Path(r"D:\WorkBuddy")

STATUS_MARK = {
    "pass":   ("✓", "#e6f4ea", "#1e7e34"),   # 绿
    "fail":   ("✗", "#fce8e6", "#a52834"),   # 红
    "manual": ("?", "#fef7e0", "#8a6d00"),   # 黄(待核验)
    "partial":("?", "#fef7e0", "#8a6d00"),
    "skip":   ("—", "#f1f3f4", "#5f6368"),   # 灰(不适用)
}
DECISION_LABEL = {
    "reject": ("退回", "#fce8e6", "#a52834"),
    "manual": ("转人工", "#fef7e0", "#8a6d00"),
    "approve": ("建议同意", "#e6f4ea", "#1e7e34"),
    "skip": ("不适用", "#f1f3f4", "#5f6368"),
}

# checklist 项 → 应有的材料 doc_type（与 auto_approve._TEXTIN_DOC_TYPE_KEYWORDS 对称）
# 用于"缺失文件"清单：列出应有但未上传的材料
CL_ID_TO_DOC_TYPE = {
    "A01": [("business_license",      "营业执照副本")],
    "A02": [("legal_person_id",       "法人身份证正反面")],
    "A03": [("tax_credit",            "纳税信用等级证明")],
    "A07": [("after_sales_cert",      "售后服务证明（机构认证证书或厂家证明函）")],
    "A08": [("financial_report",      "上年度经审计财报")],
    "C1_02": [("iso9001",            "ISO9001 体系认证证书")],
    "C1_03": [("iso14001",           "ISO14001 环境管理体系证书")],
    "C1_04": [("iso45001",           "ISO45001 职业健康安全证书")],
    "C1_05": [("production_license", "生产许可证/强制认证")],
    "A13":  [("inspection_cert",     "检验检测机构资质认定证书（CMA/CNAS）")],
}
# 中文 doc_type 名（表头用）
DOC_TYPE_LABEL = {
    "business_license": "营业执照",
    "legal_person_id": "法人身份证",
    "tax_credit": "纳税信用等级",
    "after_sales_cert": "售后服务证明",
    "after_sales_statement": "售后承诺书（自拟）",
    "financial_report": "审计财报",
    "iso9001": "ISO9001",
    "iso14001": "ISO14001",
    "iso45001": "ISO45001",
    "production_license": "生产许可证",
    "tax_cert": "税务登记",
}

def esc(s):
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def render_supplier(tid, s2, textin, cache):
    name = s2.get("name") or tid
    bill = s2.get("billName") or ""
    tdesc = s2.get("type_desc") or ""
    decision = s2.get("decision") or "manual"
    opinion = s2.get("opinion") or ""
    checklist = s2.get("checklist") or []
    t_issues = s2.get("textin_issues") or []
    q_issues = s2.get("qcc_issues") or []
    cache_entry = cache.get(tid, {})
    supplier = cache_entry.get("supplier", {})

    # 9/5 改造：供应商类型只显示系统分类（推断信息已在 type_desc 里展示，meta 行不重复）
    raw_sup_type = supplier.get("sup_type_name") or supplier.get("supTypeName") or ""
    sup_type_html = f"<span class='sup-type'>{esc(raw_sup_type) or '未分类'}</span>"

    dl = DECISION_LABEL.get(decision, ("转人工","#fef7e0","#8a6d00"))

    # 9/5 改造：大表 5 列
    cache_entry = cache.get(tid, {})
    mdetail = cache_entry.get("materials_detail", [])
    uploaded_types = set()
    file_names = {}
    for d in mdetail:
        for t in (d.get("types") or []):
            uploaded_types.add(t)
            file_names.setdefault(t, d.get("fileName", ""))

    files_html = ""
    if mdetail:
        files_html = "<ul>" + "".join(
            f"<li>{esc(d.get('fileName',''))} "
            f"<span style='color:#5f6368;font-size:11px'>"
            f"[{', '.join(d.get('types') or ['未分类'])}]</span></li>"
            for d in mdetail
        ) + "</ul>"
    else:
        files_html = "<span style='color:#a52834'>缓存无文件数据</span>"

    missing_rows = ""
    if checklist:
        miss_items = []
        for c in checklist:
            cid = c.get("id", "")
            cname = c.get("name", "")
            # 9/5：A13 skip 项不报缺失（is_inspection=False 的供应商不需要）
            if c.get("status") == "skip":
                continue
            # 9/5：决策依据从 rules.yaml 读 requirement（避免与 cache 内置字段脱节）
            decision_basis = _load_decision_basis(cid, cname)
            # 缺失材料：D 类（贸易经销商）单独处理
            if cid.startswith("D1_03"):
                miss_items.append((cid, cname, "ISO 9001 质量管理体系认证证书（可上传其代理厂家的认证，须在有效期内）"))
                continue
            if cid.startswith("D1_04"):
                miss_items.append((cid, cname, "ISO 14001 环境管理体系认证证书（须为该贸易公司自己的认证且在有效期内）"))
                continue
            if cid.startswith("D1_05"):
                miss_items.append((cid, cname, "ISO 45001 职业健康安全管理体系认证证书（须为该贸易公司自己的认证且在有效期内）"))
                continue
            if cid.startswith("D1_06"):
                miss_items.append((cid, cname, "产品生产企业的代理协议或产品销售授权资质（授权一方须为该贸易公司且在有效期内）"))
                continue
            if cid.startswith("D2_"):
                continue  # D2 国外贸易商不适用
            need = CL_ID_TO_DOC_TYPE.get(cid)
            if not need:
                continue
            for doc_type, label in need:
                if doc_type not in uploaded_types:
                    miss_items.append((cid, c.get("name", ""), label))
        if miss_items:
            # 9/5：去重（同一个审核项可能多个材料类型缺失，只显示一行）
            seen_keys = set()
            dedup = []
            for cid, cname, label in miss_items:
                key = (cid, label)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                dedup.append((cid, cname, label))
            missing_rows = (
                "<table class='sub'><tr><th>编号</th><th>审核项</th>"
                "<th>缺失的材料</th></tr>"
                + "".join(
                    f"<tr><td>{esc(c)}</td><td>{esc(n)}</td>"
                    f"<td style='color:#a52834'>⚠ {esc(l)} 未上传</td></tr>"
                for c, n, l in dedup
                ) + "</table>"
            )
        else:
            missing_rows = "<div style='color:#1e7e34'>无缺失（应有材料已全部上传）</div>"
    else:
        if decision == "skip":
            missing_rows = "<div style='color:#5f6368'>不适用（分流项，无需材料核验）</div>"
        else:
            missing_rows = "<div style='color:#5f6368'>无 checklist 数据</div>"

    t_data = textin.get(tid, {}) or {}

    big_rows = ""
    for c in checklist:
        cid = c.get("id", "")
        cname = c.get("name", "")
        st = c.get("status", "")
        # 9/5 改造：A13（检验检测资质）只对 is_inspection 类供应商展示，贸易商/厂家不显示
        if cid == "A13" and st == "skip":
            continue
        mark, bg, color = STATUS_MARK.get(st, ("?", "#fef7e0", "#8a6d00"))
        detail = c.get("detail") or c.get("message") or ""
        # 9/6 新增：从 rules.yaml 读 check_type，区分「需上传材料」与「企查查核验」
        check_type = _load_check_type(cid)

        need = CL_ID_TO_DOC_TYPE.get(cid, [])
        mat_status_parts = []
        check_detail_parts = []  # 核验结果列（material 类型：OCR 字段）
        for doc_type, label in need:
            if doc_type in uploaded_types:
                if doc_type in ("legal_person_id", "financial_report"):
                    desens = "✓已脱敏"
                else:
                    desens = "原样"
                td = t_data.get(doc_type) if isinstance(t_data, dict) else None
                if td and not td.get("_deprecated"):
                    fields = td.get("fields") or {}
                    checks = td.get("checks") or {}
                    iss = td.get("issues") or []
                    # 核验结果：列出 OCR 抽取的关键字段与系统字段对比
                    for k, v in list(fields.items())[:3]:
                        s = str(v)
                        if len(s) > 30:
                            s = s[:30] + "…"
                        check_detail_parts.append(
                            f"<span class='kv'><span class='k'>{esc(k)}</span>=<span class='v'>{esc(s)}</span></span>"
                        )
                    if iss:
                        check_detail_parts.append(
                            f"<span class='issue'>{esc(iss[0])}</span>"
                        )
                    elif checks:
                        ok = sum(1 for cv in checks.values() if cv is True)
                        ng = sum(1 for cv in checks.values() if cv is False)
                        if ng == 0:
                            check_detail_parts.append(
                                f"<span class='ok-mini'>核验 {ok} 项全通过</span>"
                            )
                        else:
                            check_detail_parts.append(
                                f"<span class='ng-mini'>{ng} 项不符</span>"
                            )
                elif td and td.get("_deprecated"):
                    desens += "+OCR禁用"
                else:
                    desens += "+未OCR"
                mat_status_parts.append(f"<span style='font-size:12px;color:#5f6368'>"
                                        f"{label}：{desens}</span>")
        # ---- 9/6 修复：材料状态列按 check_type 区分 ----
        # material 类才需要供应商上传材料；qichacha/auto/skip 类不需要
        if check_type and check_type != "material":
            mat_status = "<span style='color:#5f6368;font-size:12px'>无需提交材料</span>"
        elif mat_status_parts:
            mat_status = "<br>".join(mat_status_parts)
        else:
            mat_status = "<span style='color:#a52834'>材料未上传</span>"

        # ---- 9/6 修复：核验结果列按 check_type 区分 ----
        # qichacha 类（A08 财报/A10 商业信誉）→ 显示企查查/天眼查核验结论（detail）
        # material 类 → 显示 OCR 抽取字段
        # auto/skip 类 → 显示规则判定结论（detail）或「无需核验」
        if check_type == "qichacha":
            check_detail = (esc(detail).replace("\n", "<br>")
                            if detail
                            else "<span style='color:#5f6368;font-size:12px'>无核验数据</span>")
        elif check_type in ("auto", "skip"):
            check_detail = (esc(detail).replace("\n", "<br>")
                            if detail
                            else "<span style='color:#5f6368;font-size:12px'>无需核验</span>")
        else:  # material 或未识别
            check_detail = ("<br>".join(check_detail_parts)
                            if check_detail_parts
                            else "<span style='color:#5f6368;font-size:12px'>无核验数据</span>")

        # 决策依据：9/5 改造为从 rules.yaml 读 requirement
        decision_basis = _load_decision_basis(cid, cname)

        big_rows += (
            f"<tr style='background:{bg}'>"
            f"<td style='color:{color};font-weight:bold;font-size:16px;text-align:center'>{mark}</td>"
            f"<td>{esc(cid)}</td>"
            f"<td>{esc(cname)}</td>"
            f"<td style='font-size:13px'>{esc(decision_basis)}</td>"
            f"<td>{mat_status}</td>"
            f"<td>{check_detail}</td>"
            f"</tr>"
        )

    # 9/6：skip 项（不适用）——综合核验表位置显示不适用说明，而非空表格
    if decision == "skip":
        skip_reason = (s2.get("skip_reason")
                       or opinion.replace("不适用。", "").rstrip("。").strip()
                       or "该供应商不适用标准审批流程")
        big_table = (
            "<div class='skip-notice' style='padding:28px 24px;text-align:center;"
            "background:#f8f9fa;border:1px dashed #c0c4cc;border-radius:6px;margin:8px 0;'>"
            "<div style='font-size:16px;font-weight:600;color:#5f6368;margin-bottom:10px;'>"
            "该供应商不适用标准审批流程</div>"
            f"<div style='font-size:13px;color:#6b7280;line-height:1.6;'>{esc(skip_reason)}</div>"
            "</div>"
        )
    else:
        big_table = (
            "<table class='big-table'>"
            "<tr>"
            "<th style='width:40px'></th>"
            "<th style='width:60px'>编号</th>"
            "<th style='width:140px'>项目</th>"
            "<th style='width:200px'>决策依据</th>"
            "<th style='width:220px'>材料状态（脱敏 + OCR）</th>"
            "<th>核验结果（OCR/企查查/系统对比）</th>"
            "</tr>"
            + big_rows + "</table>"
        )

    try:
        plain_opinion = gen_opinion.build_opinion(tid, s2, textin, cache)
    except Exception:
        plain_opinion = opinion

    today = date.today().strftime("%Y年%m月%d日")
    auto_gen = f"（自动生成于 {today}，依据《中港采购发〔2025〕161号》准入审查规则）"

    return f"""
    <div class="card">
      <div class="card-head">
        <span class="title">{esc(name)}</span>
        <span class="meta">#{esc(tid)} ｜ {esc(bill)} ｜ {esc(tdesc)} ｜ {sup_type_html}</span>
        <span class="decision" style="background:{dl[1]};color:{dl[2]}">{dl[0]}</span>
      </div>
      <div class="auto-gen">{auto_gen}</div>
      <h3 style="margin:16px 20px 8px">综合核验表</h3>
      <div style="padding:0 20px">{big_table}</div>
      <div class="grid">
        <div>
          <h3>已上传文件</h3>
          {files_html}
        </div>
        <div>
          <h3>缺失材料</h3>
          {missing_rows}
        </div>
      </div>
      <!-- 9/5 反转修复：删除卡片内 opinion-area，避免与下方"最终审批意见"section 重复 -->
    </div>
    """


# 9/5 新增：决策依据从 rules.yaml 读 requirement（同类供应商固定不变）
def _load_decision_basis(cid, cname_fallback):
    """从 rules.yaml 读对应编号规则的 requirement 作为决策依据。

    加载后缓存到模块字典，避免每次重新 IO。
    """
    if not hasattr(_load_decision_basis, "_cache"):
        _load_decision_basis._cache = {}
    if cid in _load_decision_basis._cache:
        return _load_decision_basis._cache[cid] or cname_fallback
    try:
        import yaml
        rules_path = Path(__file__).parent / "rules.yaml"
        if rules_path.exists():
            with open(rules_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            # 递归查找所有规则列表
            for part in ("part_1_basic", "part_2_by_type", "part_3_special_categories"):
                bucket = cfg.get(part) or {}
                rules = []
                if isinstance(bucket, dict):
                    for v in bucket.values():
                        if isinstance(v, list):
                            rules.extend(v)
                elif isinstance(bucket, list):
                    rules.extend(bucket)
                for r in rules:
                    if isinstance(r, dict) and r.get("id") == cid:
                        _load_decision_basis._cache[cid] = r.get("requirement") or r.get("verify") or ""
                        return _load_decision_basis._cache[cid] or cname_fallback
    except Exception as e:
        print(f"[basis] 加载 rules.yaml 失败：{e}")
    return cname_fallback


# 9/6 新增：从 rules.yaml 读对应编号规则的 check_type（material/qichacha/auto/skip）
# 用于判断该核验项是否需要供应商上传材料（材料状态列）、以及核验结果列的展示方式
def _load_check_type(cid):
    """从 rules.yaml 读对应编号规则的 check_type。

    加载后缓存到模块字典，避免每次重新 IO。
    找不到时返回空字符串（调用方按 material 兜底）。
    """
    if not hasattr(_load_check_type, "_cache"):
        _load_check_type._cache = {}
    if cid in _load_check_type._cache:
        return _load_check_type._cache[cid]
    try:
        import yaml
        rules_path = Path(__file__).parent / "rules.yaml"
        if rules_path.exists():
            with open(rules_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            for part in ("part_1_basic", "part_2_by_type", "part_3_special_categories"):
                bucket = cfg.get(part) or {}
                rules = []
                if isinstance(bucket, dict):
                    for v in bucket.values():
                        if isinstance(v, list):
                            rules.extend(v)
                elif isinstance(bucket, list):
                    rules.extend(bucket)
                for r in rules:
                    if isinstance(r, dict) and r.get("id") == cid:
                        _load_check_type._cache[cid] = r.get("check_type") or ""
                        return _load_check_type._cache[cid]
    except Exception as e:
        print(f"[check_type] 加载 rules.yaml 失败：{e}")
    _load_check_type._cache[cid] = ""
    return ""


# 9/5 新增：决策依据静态文本（同类供应商固定不变，避免每次生成都不一样）
_STATIC_BASIS = {
    "A01": "《公司法》登记成立；营业执照信息与基本信息栏一致",
    "A02": "法人身份证正反面在有效期；与联系信息栏法人一致；股权穿透无中交关联",
    "A03": "上年度纳税信用等级证明为 C 级及以上",
    "A07": "售后服务五星认证或厂家/供应商出具的售后服务证明函（落款 3 个月内有效）",
    "A08": "上年度经审计财报三指标达标：流动比率≥100%、资产负债率≤65%、经营性现金流>0",
    "A10": "天眼查/企查查无失信/被执行/限消令/经营异常/严重违法记录",
    "C1_02": "ISO 9001 质量管理体系认证证书在有效期",
    "C1_03": "ISO 14001 环境管理体系认证证书在有效期",
    "C1_04": "ISO 45001 职业健康安全管理体系认证证书在有效期",
    "C1_05": "生产许可证 / 强制认证证书在有效期",
    "D-1": "ISO 三认证齐全 + 经销授权证明（贸易经销商适用）",
    "D-2": "OEM 合作协议 / 授权委托书（适用）",
}


# 9/5 新增：从供应商经营范围推断类目（与系统类型交叉验证）
_TRADE_HINTS = ("销售", "批发", "零售", "贸易", "经销", "代理")
_MANUFACTURE_HINTS = ("生产", "制造", "加工", "研发")
_SERVICE_HINTS = ("服务", "咨询", "运输", "工程", "维修")


def _infer_supplier_type(supplier, tdesc):
    """根据经营范围与公司类型推断供应商实际类目（用于与系统 sup_type_name 交叉验证）"""
    busi = (supplier.get("busi_scope") or "") + " " + (tdesc or "")
    if not busi.strip():
        return ""
    if any(k in busi for k in _TRADE_HINTS):
        return "贸易商/经销商"
    if any(k in busi for k in _MANUFACTURE_HINTS):
        return "生产/制造商"
    if any(k in busi for k in _SERVICE_HINTS):
        return "服务商"
    return ""

def main():
    stage2 = json.loads((BASE/"stage2_results.json").read_text(encoding="utf-8"))
    textin = {}
    tp = BASE/"textin_results.json"
    if tp.exists():
        textin = json.loads(tp.read_text(encoding="utf-8"))
    cache = {}
    cp = BASE/"cache_v4.json"
    if cp.exists():
        cache = json.loads(cp.read_text(encoding="utf-8"))

    ids = [a for a in sys.argv[1:]] or list(stage2.keys())
    today = datetime.datetime.now().strftime("%Y%m%d")

    bodies = "".join(render_supplier(tid, stage2.get(tid,{}), textin, cache)
                     for tid in ids if tid in stage2)
    n = len([t for t in ids if t in stage2])

    # 统计
    dec_cnt = {}
    for tid in ids:
        if tid in stage2:
            d = stage2[tid].get("decision","manual")
            dec_cnt[d] = dec_cnt.get(d,0)+1
    summary = " ｜ ".join(f"{DECISION_LABEL.get(k,('?',))[0]} {v}" for k,v in dec_cnt.items())

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>供应商材料审查 {today}</title>
<style>
body{{font-family:'Microsoft YaHei',sans-serif;background:#f5f5f5;margin:0;padding:20px;color:#202124}}
.head{{max-width:1100px;margin:0 auto 20px;background:#fff;padding:20px;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.1)}}
.head h1{{margin:0 0 8px;font-size:22px;color:#000}}
.head .sub{{color:#5f6368;font-size:14px}}
.container{{max-width:1100px;margin:0 auto}}
.card{{background:#fff;margin-bottom:20px;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.1);overflow:hidden}}
.card-head{{padding:16px 20px;border-bottom:1px solid #e8eaed;display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
.title{{font-size:18px;font-weight:bold;color:#000}}
.meta{{color:#5f6368;font-size:13px}}
.decision{{padding:3px 12px;border-radius:12px;font-size:13px;font-weight:bold}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:0}}
.grid>div{{padding:16px 20px}}
.grid>div:first-child{{border-right:1px solid #e8eaed}}
h3{{margin:0 0 10px;font-size:15px;color:#202124;border-left:3px solid #1a73e8;padding-left:8px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
table th{{background:#f8f9fa;text-align:left;padding:6px 8px;font-weight:600;color:#5f6368;font-size:12px}}
table td{{padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}}
.sub{{margin-top:8px}}
.sub th{{font-size:11px}}
.warn{{background:#fef7e0;color:#8a6d00;padding:10px;border-radius:4px;font-size:13px}}
.opinion{{white-space:pre-wrap;background:#f8f9fa;padding:14px;border-radius:4px;font-size:13px;line-height:1.6;margin:16px 20px;border:1px solid #e8eaed}}
.opinion-table{{width:100%;border-collapse:collapse;font-size:13px;margin:8px 0}}
.opinion-table th{{background:#f8f9fa;text-align:left;padding:6px 8px;font-weight:600;color:#5f6368;font-size:11px;border-bottom:2px solid #e8eaed}}
.opinion-table td{{padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}}
details summary{{user-select:none}}
details[open] summary{{margin-bottom:8px}}
.legend{{background:#fff;padding:12px 20px;border-radius:8px;margin-bottom:16px;font-size:13px;color:#5f6368;max-width:1100px;margin:0 auto 16px}}
.legend span{{margin-right:16px}}
</style></head><body>
<div class="head"><h1>供应商材料审查报告</h1>
<div class="sub">{today} ｜ 共 {n} 家 ｜ {summary}</div></div>
<div class="legend">
<span style="color:#1e7e34">✓ 通过</span>
<span style="color:#a52834">✗ 问题（须处理）</span>
<span style="color:#8a6d00">? 待人工核验</span>
<span style="color:#5f6368">— 不适用</span>
</div>
<div class="container">{bodies}</div>
</body></html>"""

    out = OUT_DIR / f"供应商材料审查_{today}.html"
    out.write_text(html, encoding="utf-8")
    print(f"报告已生成: {out}")

if __name__ == "__main__":
    main()
