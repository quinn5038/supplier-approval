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

    # ---- 9/5 改造：一个大表整合核查清单 + OCR + 企查查 ----
    cache_entry = cache.get(tid, {})
    mdetail = cache_entry.get("materials_detail", [])
    uploaded_types = set()
    file_names = {}  # doc_type → fileName
    for d in mdetail:
        for t in (d.get("types") or []):
            uploaded_types.add(t)
            file_names.setdefault(t, d.get("fileName", ""))

    # 已上传文件清单（简洁）
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

    # 缺失文件清单
    missing_rows = ""
    if checklist:
        miss_items = []
        for c in checklist:
            cid = c.get("id", "")
            if c.get("status") == "skip":
                continue
            need = CL_ID_TO_DOC_TYPE.get(cid)
            if not need:
                continue
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
    else:
        missing_rows = "<div style='color:#5f6368'>无 checklist 数据</div>"

    # TextIn OCR 抽取（按材料类）
    t_data = textin.get(tid, {}) or {}

    # 构建"大表"主体行
    big_rows = ""
    for c in checklist:
        cid = c.get("id", "")
        cname = c.get("name", "")
        st = c.get("status", "")
        mark, bg, color = STATUS_MARK.get(st, ("?", "#fef7e0", "#8a6d00"))
        detail = c.get("detail") or c.get("message") or ""

        # 材料状态：脱敏 + OCR
        need = CL_ID_TO_DOC_TYPE.get(cid, [])
        # 整合所需材料类型的脱敏状态
        mat_status_parts = []
        ocr_findings = []   # 本项对应的 OCR 抽取结果
        for doc_type, label in need:
            if doc_type in uploaded_types:
                # 脱敏状态
                if doc_type in ("legal_person_id", "financial_report"):
                    desens = "✓已脱敏"
                else:
                    desens = "原样"
                # OCR 状态
                td = t_data.get(doc_type) if isinstance(t_data, dict) else None
                if td and not td.get("_deprecated"):
                    fields = td.get("fields") or {}
                    checks = td.get("checks") or {}
                    if fields or checks:
                        fld = ", ".join(f"{k}={v}" for k, v in list(fields.items())[:2])[:80]
                        ocr_findings.append(f"<span style='font-size:12px'>{fld}</span>")
                elif td and td.get("_deprecated"):
                    desens += "+OCR禁用"
                else:
                    desens += "+未OCR"
                mat_status_parts.append(f"<span style='font-size:12px;color:#5f6368'>"
                                        f"{label}：{desens}</span>")
        mat_status = "<br>".join(mat_status_parts) if mat_status_parts else \
                     "<span style='color:#a52834'>材料未上传</span>"

        # 企查查结果（如果 checklist 项是 qichacha check_type）
        qcc_extra = ""
        if cid == "A08" and "A08 财报" in str(q_issues):
            qcc_extra = "<div style='margin-top:4px;font-size:12px;color:#8a6d00'>"
            qcc_extra += "企查查：" + next((x for x in q_issues if "A08" in x), "")
            qcc_extra += "</div>"

        big_rows += (
            f"<tr style='background:{bg}'>"
            f"<td style='color:{color};font-weight:bold;font-size:16px;text-align:center'>{mark}</td>"
            f"<td>{esc(cid)}</td>"
            f"<td>{esc(cname)}</td>"
            f"<td style='font-size:13px'>{esc(detail)}</td>"
            f"<td>{mat_status}{qcc_extra}</td>"
            f"</tr>"
        )

    # 缺失文件也加一行（状态 fail）
    if missing_rows and "未上传" in missing_rows:
        for cid, cname, label in []:  # 已在 checklist 中体现，单独行会增加冗余
            pass

    big_table = (
        "<table class='big-table'>"
        "<tr>"
        "<th style='width:40px'></th>"
        "<th style='width:60px'>编号</th>"
        "<th style='width:140px'>项目</th>"
        "<th>说明 / 决策依据</th>"
        "<th style='width:280px'>材料状态（脱敏 + OCR）</th>"
        "</tr>"
        + big_rows + "</table>"
    )

    # 纯文本意见（复用 gen_opinion）
    try:
        plain_opinion = gen_opinion.build_opinion(tid, s2, textin, cache)
    except Exception:
        plain_opinion = opinion

    return f"""
    <div class="card">
      <div class="card-head">
        <span class="title">{esc(name)}</span>
        <span class="meta">#{esc(tid)} ｜ {esc(bill)} ｜ {esc(tdesc)}</span>
        <span class="decision" style="background:{dl[1]};color:{dl[2]}">{dl[0]}</span>
      </div>
      <h3 style="margin:16px 20px 8px">综合核验表（核查清单 + 材料状态 + OCR/企查查）</h3>
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
      <details style="margin:8px 20px 16px">
        <summary style="cursor:pointer;color:#1a73e8;font-size:14px;padding:8px 0">
          📋 点击展开纯文本审批意见（可复制粘贴进 ICCEC 审批框）
        </summary>
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
