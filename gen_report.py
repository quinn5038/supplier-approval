# -*- coding: utf-8 -*-
"""
[已废弃] 旧版报告生成器（基于日志解析）。
当前版本请使用 gen_stage2_report.py（基于 stage2_results.json 直接生成，不依赖日志格式）。
本文件保留仅作为历史参考，不在 Web UI 中调用。

生成供应商审批 HTML 报告（37家）。
数据源：
  1. approval_20260831.log —— 主源：待办行(全名+审批类型)/[审批]行/决策/审批意见/核查清单
     （按时间窗口过滤：主跑 23:40:08~00:06:55，修正跑 00:11:29 之后，修正跑记录覆盖主跑）
  2. run_v4_output2.txt + run_v4_fix3.txt —— 补充：打印块里的"类型:"(分流类型)、材料列表、认证
     （打印块因 stdout/stderr 双缓冲与日志交错，仅提取块内连续字段，意见以日志为准）
分流供应商（无日志意见）按代码模板重建意见。
"""
import json
import re
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
LOG = BASE / "approval_20260831.log"
OUT = Path(r"D:\WorkBuddy\供应商审批报告_20260901.html")

TS = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d{3} \| (\w+) \| (.*)$")


def run_window(date, hms):
    """返回记录所属跑批: main / fix / None(丢弃)"""
    if date == "2026-08-31":
        return "main" if hms >= "23:40:08" else None
    if date == "2026-09-01":
        if "00:06:56" <= hms <= "00:11:28":
            return None      # 两跑之间的杂项
        return "fix" if hms >= "00:11:29" else "main"
    return None


def parse_log():
    """解析日志 → {todoId: record}
    approval_20260831.log → 主跑（只取 23:40:08 之后）
    approval_20260901.log → 修正跑（00:11 补跑3家，覆盖主跑记录）"""
    records = {}

    def _parse(path, which, min_hms=None):
        if not path.exists():
            return
        nonlocal_cur = {"rec": None, "section": None}

        def close():
            nonlocal_cur["rec"] = None
            nonlocal_cur["section"] = None

        for line in path.read_text(encoding="utf-8").splitlines():
            m = TS.match(line)
            if m:
                date, hms, level, msg = m.groups()
                if min_hms and date == "2026-08-31" and hms < min_hms:
                    close()
                    continue
                # 状态切换行
                mm = re.match(r"^--- 待办 #(\d+): (.+?) \((.+?)\) ---$", msg)
                if mm:
                    close()
                    rec = {
                        "todo_id": mm.group(1),
                        "name": mm.group(2).strip(),
                        "bill_name": mm.group(3).strip(),
                        "type_desc": "",
                        "decision": "",
                        "opinion": "",
                        "checklist": [],
                        "from": which,
                    }
                    nonlocal_cur["rec"] = rec
                    records[rec["todo_id"]] = rec
                    continue
                if nonlocal_cur["rec"] is None:
                    continue
                mm = re.match(r"^\[审批\] (.+?) \| 类型: (.+?) \| 适用 (\d+) 条规则$", msg)
                if mm:
                    nonlocal_cur["rec"]["type_desc"] = mm.group(2).strip()
                    continue
                mm = re.match(r"^决策: (reject|manual|recommend) \| (.+)$", msg)
                if mm:
                    nonlocal_cur["rec"]["decision"] = mm.group(1)
                    continue
                if msg == "审批意见:":
                    nonlocal_cur["section"] = "opinion"
                    continue
                if msg == "核查清单:":
                    nonlocal_cur["section"] = "checklist"
                    continue
                # 其他行（超时重试/续跑跳过/结束统计等）不关记录，只结束段落收集
                nonlocal_cur["section"] = None
            else:
                # 无时间戳行：意见/清单内容
                rec = nonlocal_cur["rec"]
                if rec is None:
                    continue
                s = line.rstrip()
                if not s.strip():
                    continue
                if nonlocal_cur["section"] == "opinion":
                    rec["opinion"] += s + "\n"
                elif nonlocal_cur["section"] == "checklist":
                    cm = re.match(r"^\s*\[(pass|fail|pending|skip)\] (.+?) — (.+)$", s)
                    if cm:
                        rec["checklist"].append((cm.group(1), cm.group(2), cm.group(3)))
            # END for line

    _parse(BASE / "approval_20260831.log", "main", min_hms="23:40:08")
    _parse(BASE / "approval_20260901.log", "fix")
    return records


def parse_print_blocks(path):
    """从 stdout 打印块提取: todoId → {ptype, materials, certs}
    仅取块内连续字段（供应商行→决策行→材料段→认证行），不跨分隔线。"""
    result = {}
    sup = None
    in_mat = False
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        m = re.match(r"^供应商: (.+?) \| todoId: (\d+) \| 类型: (.+)$", s)
        if m:
            sup = {"ptype": m.group(3).strip(), "materials": [], "certs": ""}
            result[m.group(2)] = sup
            in_mat = False
            continue
        if sup is None:
            continue
        if s.startswith("--- 已上传材料"):
            in_mat = True
            continue
        if s.startswith("--- 认证:"):
            sup["certs"] = s.replace("--- 认证:", "").strip().rstrip("-").strip()
            in_mat = False
            continue
        if s.startswith("---") or s.startswith("决策:") or s.startswith("注册资本:") \
                or s == "=" * 60 or s.startswith("审批意见"):
            in_mat = False
            continue
        if in_mat and s.startswith("["):
            sup["materials"].append(s)
    return result


def rebuild_opinion(rec):
    """分流供应商（无日志意见）按代码模板重建"""
    ptype = rec.get("ptype") or rec["type_desc"]
    bill = rec["bill_name"]
    if rec["decision"] == "reject":
        return "退回。国外供应商请通过中交海外采购专区进行注册申请。"
    if ptype and ptype.startswith("国外供应商"):
        if re.search(r"\((China|中国)?\)$", ptype) and ("(China)" in ptype or "()" in ptype):
            return ("转人工复核。系统数据异常：该供应商标记为境外注册，但国家信息为中国（或为空），"
                    "请人工核实其注册渠道与主体信息后，再决定是否按国外或国内标准审查。")
        return f"转人工复核。国外供应商（审查类型：{bill}），需人工审核。"
    if ptype and "港澳台" in ptype:
        return "转人工复核。港澳台地区供应商需人工审批。"
    return f"转人工复核。该供应商属于集团独有类别（{ptype}），审查标准较复杂，需人工审核。"


STATUS_MAP = {"pass": ("✓", "通过"), "fail": ("✗", "不通过"),
              "partial": ("◐", "部分通过"),
              "pending": ("待核", "待核验"), "skip": ("跳过", "跳过")}


def load_stage2():
    """加载阶段2（企查查增强）结果: {todoId: record}"""
    p = BASE / "stage2_results.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main():
    records = parse_log()
    print_blocks = {}
    for p in (BASE / "run_v4_output2.txt", BASE / "run_v4_fix3.txt"):
        print_blocks.update(parse_print_blocks(p))

    # 合并打印块信息 + 重建分流意见
    for tid, rec in records.items():
        pb = print_blocks.get(tid)
        if pb:
            rec["ptype"] = pb["ptype"]
            rec["materials"] = pb["materials"]
            rec["certs"] = pb["certs"]
            if not rec["type_desc"]:
                rec["type_desc"] = pb["ptype"]
        else:
            rec["ptype"] = ""
            rec["materials"] = []
            rec["certs"] = ""
        rec["opinion"] = rec["opinion"].strip()
        if not rec["opinion"]:
            rec["opinion"] = rebuild_opinion(rec)

    # 合并阶段2（企查查增强）结果：清单/意见/决策以增强后为准
    stage2 = load_stage2()
    n_s2 = 0
    for tid, s2 in stage2.items():
        rec = records.get(tid)
        if not rec:
            continue
        rec["checklist"] = [(c["status"], f"{c['id']} {c['name']}", c["detail"])
                            for c in s2.get("checklist", [])]
        rec["opinion"] = s2.get("opinion", rec["opinion"])
        rec["decision"] = s2.get("decision", rec["decision"])
        rec["stage2"] = True
        rec["qcc_issues"] = s2.get("qcc_issues", [])
        n_s2 += 1

    print(f"解析记录: {len(records)} 家（含企查查增强 {n_s2} 家）")
    n_reject = sum(1 for r in records.values() if r["decision"] == "reject")
    n_manual = sum(1 for r in records.values() if r["decision"] == "manual")
    n_recommend = sum(1 for r in records.values() if r["decision"] == "recommend")
    n_cl = sum(1 for r in records.values() if r["checklist"])
    print(f"退回 {n_reject} | 转人工 {n_manual} | 建议同意 {n_recommend} | 有核查清单 {n_cl}")

    order = {"reject": 0, "manual": 1, "recommend": 2}
    items = sorted(records.values(), key=lambda r: (order.get(r["decision"], 3), r["todo_id"]))

    ICON_COLOR = {"pass": "#16a34a", "fail": "#dc2626", "partial": "#2563eb",
                  "pending": "#d97706", "skip": "#6b7280"}
    DEC_CN = {"reject": "退回", "manual": "转人工", "recommend": "建议同意"}
    DEC_COLOR = {"reject": "#dc2626", "manual": "#d97706", "recommend": "#16a34a"}

    def esc(t):
        return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    # ---------- 汇总行 ----------
    rows = []
    for r in items:
        fails = [c[1] for c in r["checklist"] if c[0] == "fail"]
        d = r["decision"]
        if d == "reject":
            if fails:
                reason = "、".join(f.split(" ")[0] for f in fails)
            else:
                reason = "国外注册渠道"
        elif d == "manual":
            t = r["type_desc"]
            if "系统数据异常" in r["opinion"]:
                reason = "数据冲突：境外标记+国家为中国"
            elif t.startswith("国外供应商"):
                reason = "国外供应商·非注册类"
            elif "港澳台" in t:
                reason = "港澳台供应商"
            elif t and t not in ("境内",):
                reason = f"集团独有类别（{t}）"
            else:
                reason = "材料齐全·待核验"
        else:
            reason = "全部达标·待人工核实"
        if r.get("stage2"):
            reason += "｜✓企查查已核验"
        rows.append(
            f"<tr><td>{esc(r['name'])}</td><td>{r['todo_id']}</td>"
            f"<td>{esc(r['type_desc'] or r['bill_name'])}</td>"
            f"<td style='color:{DEC_COLOR[d]};font-weight:600'>{DEC_CN[d]}</td>"
            f"<td>{esc(reason)}</td></tr>"
        )

    # ---------- 详情 ----------
    details = []
    for r in items:
        d = r["decision"]
        if r["checklist"]:
            cl_rows = []
            for st, code, detail in r["checklist"]:
                icon = STATUS_MAP[st][0]
                color = ICON_COLOR[st]
                cl_rows.append(
                    f"<tr><td style='white-space:nowrap;color:{color};font-weight:700'>{icon}</td>"
                    f"<td style='white-space:nowrap'>{esc(code)}</td><td>{esc(detail)}</td></tr>"
                )
            cl_html = ("<table class='cl'><tr><th>结果</th><th>检查项</th><th>说明</th></tr>"
                       + "".join(cl_rows) + "</table>")
        else:
            cl_html = "<p class='muted'>（分流处理：未走逐项核查清单）</p>"

        mat_html = ""
        if r["materials"]:
            mat_li = "".join(f"<li><code>{esc(m)}</code></li>" for m in r["materials"])
            mat_html = (f"<details><summary>已上传材料（{len(r['materials'])} 类）</summary>"
                        f"<ul class='mat'>{mat_li}</ul></details>")

        details.append(f"""
<div class="card">
  <div class="card-head">
    <span class="name">{esc(r['name'])}</span>
    <span class="badge" style="background:{DEC_COLOR[d]}">{DEC_CN[d]}</span>
    {('<span class="badge" style="background:#2563eb">企查查已核验</span>' if r.get('stage2') else '')}
  </div>
  <div class="meta">todoId: {r['todo_id']} ｜ 审批类型: {esc(r['bill_name'])}
  {('｜ 分类: ' + esc(r['type_desc'])) if r['type_desc'] else ''}
  {('｜ 认证: ' + esc(r['certs'])) if r['certs'] else ''}</div>
  <h4>① 核查清单</h4>
  {cl_html}
  {mat_html}
  <h4>② 审批意见（可直接复制）</h4>
  <pre class="opinion">{esc(r['opinion'])}</pre>
</div>""")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>供应商审批报告 2026-09-01（37家·演练）</title>
<style>
  body {{ font-family: "Microsoft YaHei", sans-serif; margin: 0; background: #f5f6f8; color: #111; }}
  .wrap {{ max-width: 960px; margin: 0 auto; padding: 24px 16px 60px; }}
  h1 {{ font-size: 22px; }}
  .stats {{ display: flex; gap: 12px; margin: 16px 0 24px; flex-wrap: wrap; }}
  .stat {{ background: #fff; border-radius: 10px; padding: 14px 22px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .stat b {{ font-size: 26px; display: block; }}
  .stat.rej b {{ color: #dc2626; }} .stat.man b {{ color: #d97706; }}
  .stat.tot b {{ color: #111; }}
  h2 {{ font-size: 17px; margin: 30px 0 10px; }}
  table.sum {{ width: 100%; border-collapse: collapse; background: #fff; font-size: 13px;
    box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .sum th, .sum td {{ padding: 7px 10px; border-bottom: 1px solid #eee; text-align: left; }}
  .sum th {{ background: #1f2937; color: #fff; white-space: nowrap; }}
  .card {{ background: #fff; border-radius: 10px; padding: 18px 20px; margin: 14px 0;
    box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .card-head {{ display: flex; align-items: center; gap: 10px; }}
  .name {{ font-size: 16px; font-weight: 700; }}
  .badge {{ color: #fff; border-radius: 4px; padding: 2px 10px; font-size: 12px; font-weight: 600; }}
  .meta {{ color: #6b7280; font-size: 12px; margin: 6px 0 4px; }}
  h4 {{ margin: 14px 0 6px; font-size: 14px; color: #374151; }}
  table.cl {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  .cl th {{ background: #f3f4f6; text-align: left; padding: 6px 8px; border: 1px solid #e5e7eb; }}
  .cl td {{ padding: 6px 8px; border: 1px solid #e5e7eb; vertical-align: top; }}
  .mat {{ font-size: 12px; color: #4b5563; }}
  .mat li {{ margin: 2px 0; }}
  .opinion {{ background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px;
    padding: 10px 12px; font-size: 13px; white-space: pre-wrap; font-family: "Microsoft YaHei", sans-serif; }}
  .muted {{ color: #9ca3af; font-size: 13px; }}
  .note {{ background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px;
    padding: 10px 14px; font-size: 13px; margin: 14px 0; }}
  details summary {{ cursor: pointer; color: #2563eb; font-size: 13px; margin: 8px 0; }}
  code {{ background: #f3f4f6; border-radius: 3px; padding: 1px 5px; font-size: 12px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>供应商审批报告 · 2026-09-01</h1>
  <div style="color:#6b7280;font-size:13px">
    标准：中港采购发〔2025〕161号 ｜ 模式：演练（DRY_RUN，未真实执行审批）｜ 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}
  </div>

  <div class="stats">
    <div class="stat tot"><b>{len(records)}</b>总待审</div>
    <div class="stat rej"><b>{n_reject}</b>建议退回</div>
    <div class="stat man"><b>{n_manual}</b>转人工</div>
  </div>

  <div class="note">
    <b>说明</b>：① 本轮为演练模式，所有"退回"均未真正下发，意见可直接复制到审批系统使用；
    ② 按约定，初期无自动通过项——材料齐全的也输出"转人工+核验点清单"，由你核实后通过；
    ③ 3家（广州岚湾、霍尔果斯兵港、SIEC）系统数据自相矛盾（标记境外但国家为中国），已改为转人工核实，不自动退回；
    ④ 带「企查查已核验」标记的供应商已完成执照核验/股权穿透/财务指标自动核验（◐=部分通过），
    其余"待核"项待 TextIn 接入后自动化。
  </div>

  <h2>一、总览（{len(records)} 家）</h2>
  <table class="sum">
    <tr><th>供应商</th><th>todoId</th><th>分类</th><th>决策</th><th>主要原因</th></tr>
    {''.join(rows)}
  </table>

  <h2>二、明细（每家：核查清单 + 审批意见）</h2>
  {''.join(details)}

  <p class="muted" style="margin-top:24px">
    依据：《供应商准入核对表》+ 中港采购发〔2025〕161号 ｜ 脚本：auto_approve.py v4（断点续跑+WAF熔断）
  </p>
</div>
</body>
</html>"""

    OUT.write_text(html, encoding="utf-8")
    print(f"报告已生成: {OUT}")


if __name__ == "__main__":
    main()
