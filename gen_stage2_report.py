#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""从 stage2_results.json + textin_results.json 生成供应商材料审查对照表 HTML
用法: python gen_stage2_report.py [todoId1] [todoId2] ...   (默认全部)
输出: D:\WorkBuddy\供应商材料审查_YYYYMMDD.html
"""
import json, sys, datetime
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

    dl = DECISION_LABEL.get(decision, ("转人工","#fef7e0","#8a6d00"))

    # 文件下载情况
    cache_entry = cache.get(tid, {})
    mdetail = cache_entry.get("materials_detail", [])
    files_html = ""
    if mdetail:
        files_html = "<ul>" + "".join(
            f"<li>{esc(d.get('fileName',''))} <span style='color:#5f6368'>"
            f"[{', '.join(d.get('types') or ['未分类'])}]</span></li>"
            for d in mdetail
        ) + "</ul>"
    else:
        files_html = "<span style='color:#a52834'>缓存无数据</span>"

    # 缺失文件清单：对比 checklist 应有材料 vs 已上传 types
    # 已上传材料的 doc_type 集合
    uploaded_types = set()
    for d in mdetail:
        for t in (d.get("types") or []):
            uploaded_types.add(t)
    missing_rows = ""
    if checklist:
        miss_items = []
        for c in checklist:
            cid = c.get("id", "")
            st = c.get("status", "")
            # skip 项不报缺失（不适用）
            if st == "skip":
                continue
            need = CL_ID_TO_DOC_TYPE.get(cid)
            if not need:
                continue
            # A07 售后只对厂家必查；规则层若 status=skip 表示该供应商非厂家，跳过
            for doc_type, label in need:
                if doc_type not in uploaded_types:
                    miss_items.append((cid, c.get("name", ""), label))
        if miss_items:
            missing_rows = (
                "<table class='sub'><tr><th>编号</th><th>审核项</th>"
                "<th>缺失的材料</th></tr>"
                + "".join(
                    f"<tr><td>{esc(c)}</td><td>{esc(n)}</td>"
                    f"<td style='color:#a52834'>⚠ {esc(l)} 未上传</td></tr>"
                    for c, n, l in miss_items
                ) + "</table>"
            )
        else:
            missing_rows = "<div style='color:#1e7e34'>无缺失（应有材料已全部上传）</div>"

    # TextIn OCR 各材料结论
    t_data = textin.get(tid, {})
    ocr_html = ""
    if t_data:
        ocr_rows = ""
        for doc_type, info in t_data.items():
            if not isinstance(info, dict):
                continue
            iss = info.get("issues") or []
            checks = info.get("checks") or {}
            fields = info.get("fields") or {}
            mark = "✓" if not iss else "✗"
            color = "#1e7e34" if not iss else "#a52834"
            field_str = " ".join(f"{k}={v}" for k,v in fields.items())[:200]
            check_str = " ".join(f"{k}:{'✓' if v else '✗'}" for k,v in checks.items())
            iss_str = "; ".join(iss) if iss else "通过"
            ocr_rows += (
                f"<tr><td style='color:{color};font-weight:bold'>{mark}</td>"
                f"<td>{esc(doc_type)}</td><td style='font-size:13px;color:#5f6368'>{esc(field_str)}</td>"
                f"<td style='font-size:13px'>{esc(check_str)}</td>"
                f"<td style='color:{color}'>{esc(iss_str)}</td></tr>"
            )
        if ocr_rows:
            ocr_html = (
                "<table class='sub'><tr><th></th><th>材料</th>"
                "<th>抽取字段</th><th>核验</th><th>结论</th></tr>"
                + ocr_rows + "</table>"
            )
    else:
        ocr_html = "<div class='warn'>⚠ 该供应商文件无法下载（待办已关闭）或尚未跑 TextIn，仅规则层面初判</div>"

    # checklist 行
    cl_rows = ""
    for c in checklist:
        st = c.get("status","")
        mark, bg, color = STATUS_MARK.get(st, ("?","#fef7e0","#8a6d00"))
        cl_rows += (
            f"<tr style='background:{bg}'>"
            f"<td style='color:{color};font-weight:bold;font-size:16px'>{mark}</td>"
            f"<td>{esc(c.get('id',''))}</td><td>{esc(c.get('name',''))}</td>"
            f"<td style='font-size:13px'>{esc(c.get('message',''))}</td></tr>"
        )

    # textin_issues
    ti_html = ""
    if t_issues:
        ti_html = "<ul style='color:#a52834'>" + "".join(
            f"<li>{esc(x)}</li>" for x in t_issues
        ) + "</ul>"

    # === 审批意见：结构化表格 + 纯文本（可复制粘贴）===
    # 纯文本意见（复用 gen_opinion，可粘贴进 ICCEC 审批框）
    try:
        plain_opinion = gen_opinion.build_opinion(tid, s2, textin, cache)
    except Exception:
        plain_opinion = opinion  # fallback 到 stage2 自带 opinion

    # 结构化表格行：按 status 分类（硬伤→核验→通过→不适用）
    seq = 0
    opinion_table_rows = ""

    # 1. 须退回补材料/整改（fail 且含硬伤关键词）
    for c in checklist:
        if c.get("status") != "fail":
            continue
        desc = c.get("detail") or c.get("message") or ""
        if gen_opinion._is_hard_fail(desc):
            seq += 1
            opinion_table_rows += (
                f"<tr style='background:#fce8e6'>"
                f"<td style='font-weight:bold'>{seq}</td>"
                f"<td>[{esc(c.get('id',''))}] {esc(c.get('name',''))}</td>"
                f"<td style='color:#a52834;font-weight:bold'>✗ 退回</td>"
                f"<td style='font-size:13px'>{esc(desc)}</td></tr>"
            )
    # 2. 需人工核验（pending/partial/manual + 软 fail）
    for c in checklist:
        st = c.get("status")
        if st in ("pending", "partial", "manual"):
            seq += 1
            desc = c.get("message") or c.get("detail") or "需人工核验"
            opinion_table_rows += (
                f"<tr style='background:#fef7e0'>"
                f"<td style='font-weight:bold'>{seq}</td>"
                f"<td>[{esc(c.get('id',''))}] {esc(c.get('name',''))}</td>"
                f"<td style='color:#8a6d00;font-weight:bold'>? 核验</td>"
                f"<td style='font-size:13px'>{esc(desc)}</td></tr>"
            )
    # 3. 核验通过
    for c in checklist:
        if c.get("status") == "pass":
            seq += 1
            desc = c.get("detail") or c.get("message") or "通过"
            # 截断过长描述
            if len(desc) > 120:
                desc = desc[:117] + "..."
            opinion_table_rows += (
                f"<tr style='background:#e6f4ea'>"
                f"<td style='font-weight:bold'>{seq}</td>"
                f"<td>[{esc(c.get('id',''))}] {esc(c.get('name',''))}</td>"
                f"<td style='color:#1e7e34;font-weight:bold'>✓ 通过</td>"
                f"<td style='font-size:13px'>{esc(desc)}</td></tr>"
            )
    # 4. 不适用
    for c in checklist:
        if c.get("status") == "skip":
            opinion_table_rows += (
                f"<tr style='background:#f1f3f4'>"
                f"<td style='color:#5f6368'>—</td>"
                f"<td>[{esc(c.get('id',''))}] {esc(c.get('name',''))}</td>"
                f"<td style='color:#5f6368'>— 不适用</td>"
                f"<td style='font-size:13px;color:#5f6368'>{esc(c.get('detail','') or c.get('message',''))}</td></tr>"
            )

    opinion_table = (
        "<table class='opinion-table'>"
        "<tr><th style='width:40px'>序号</th>"
        "<th style='width:180px'>审核项</th>"
        "<th style='width:80px'>状态</th>"
        "<th>说明</th></tr>"
        + opinion_table_rows + "</table>"
    )

    return f"""
    <div class="card">
      <div class="card-head">
        <span class="title">{esc(name)}</span>
        <span class="meta">#{esc(tid)} ｜ {esc(bill)} ｜ {esc(tdesc)}</span>
        <span class="decision" style="background:{dl[1]};color:{dl[2]}">{dl[0]}</span>
      </div>
      <div class="grid">
        <div>
          <h3>核查清单</h3>
          <table><tr><th></th><th>编号</th><th>项目</th><th>说明</th></tr>{cl_rows}</table>
        </div>
        <div>
          <h3>TextIn OCR 材料核验</h3>
          {ocr_html}
        </div>
      </div>
      <div class="grid">
        <div>
          <h3>已上传文件</h3>
          {files_html}
        </div>
        <div>
          <h3>缺失文件（应传未传）</h3>
          {missing_rows if missing_rows else "<div style='color:#5f6368'>无法判定（缓存无数据）</div>"}
        </div>
      </div>
      <h3 style="margin:16px 20px 0">OCR 发现的问题</h3>
      <div style="padding:0 20px">{ti_html if ti_html else "<span style='color:#1e7e34'>无</span>"}</div>
      <h3 style="margin:16px 20px 0">审批意见（结构化表格）</h3>
      <div style="padding:8px 20px">{opinion_table}</div>
      <details style="margin:8px 20px 16px">
        <summary style="cursor:pointer;color:#1a73e8;font-size:14px;padding:8px 0">📋 点击展开纯文本审批意见（可复制粘贴进 ICCEC 审批框）</summary>
        <pre class="opinion" style="margin:8px 0">{esc(plain_opinion)}</pre>
      </details>
    </div>
    """

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
