"""
ICCEC 供应商智能审批系统 - Web UI
FastAPI 主入口，复用 auto_approve.py / textin_pipeline.py / gen_opinion.py / gen_stage2_report.py

启动：
    cd web
    uvicorn app:app --host 0.0.0.0 --port 8000 --reload
浏览器打开 http://<本机IP>:8000
"""

import os
import sys
import json
import subprocess
import threading
import time
import shutil
from pathlib import Path
from datetime import datetime

# 把父目录加入 sys.path，便于 import auto_approve 等模块
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ============================================================
# 配置
# ============================================================
WEB_DIR = Path(__file__).resolve().parent
PYTHON_EXE = sys.executable

app = FastAPI(title="ICCEC 供应商智能审批", version="1.0")
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

# 审批任务进度追踪：todo_id → {status, step, error, started_at}
_task_status: dict = {}
_task_lock = threading.Lock()


# ============================================================
# 辅助函数
# ============================================================
def _env_file_path():
    return BASE_DIR / ".env"


def _backup_env_and_write(new_cookie: str, new_auth: str = ""):
    """更新 .env 中的 SCPMA_COOKIE（和可选的 SCPMA_AUTH_TOKEN）"""
    env_path = _env_file_path()
    if not env_path.exists():
        return False, ".env 文件不存在"

    # 按用户备份规则：覆盖前备份 _R1 _R2 ...
    i = 1
    while (BASE_DIR / f".env_R{i}").exists():
        i += 1
    shutil.copy2(env_path, BASE_DIR / f".env_R{i}")

    lines = env_path.read_text(encoding="utf-8").splitlines()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("SCPMA_COOKIE="):
            new_lines.append(f"SCPMA_COOKIE={new_cookie}")
        elif new_auth and stripped.startswith("SCPMA_AUTH_TOKEN="):
            new_lines.append(f"SCPMA_AUTH_TOKEN={new_auth}")
        else:
            new_lines.append(line)
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return True, f"已更新 .env（备份 .env_R{i}）"


def _run_pipeline_for_one(todo_id: str):
    """在后台线程中跑完整审批流水线（单家供应商）"""
    def _update(step, status="running", error=None):
        with _task_lock:
            _task_status[todo_id] = {
                "step": step,
                "status": status,
                "error": error,
                "started_at": _task_status.get(todo_id, {}).get("started_at", datetime.now().isoformat()),
                "updated_at": datetime.now().isoformat(),
            }

    try:
        _update("正在拉取供应商数据（下载材料入缓存）...")
        env = os.environ.copy()
        env["FETCH_ONLY"] = "true"
        env["FETCH_IDS"] = str(todo_id)
        env["PYTHONIOENCODING"] = "utf-8"
        r1 = subprocess.run(
            [PYTHON_EXE, str(BASE_DIR / "auto_approve.py")],
            cwd=str(BASE_DIR), env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=300,
        )
        if r1.returncode == 3:
            _update("cookie_expired", status="error", error="Cookie 已过期，请点击右上角更新Cookie")
            return
        if r1.returncode != 0:
            _update("fetch_failed", status="error", error=f"拉取失败（exit={r1.returncode}）：{r1.stderr[:500]}")
            return

        _update("正在 OCR 识别证件文件（TextIn）...")
        r2 = subprocess.run(
            [PYTHON_EXE, str(BASE_DIR / "textin_pipeline.py")],
            cwd=str(BASE_DIR), env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=600,
        )
        if r2.returncode != 0:
            _update("ocr_failed", status="error", error=f"OCR 失败（exit={r2.returncode}）：{r2.stderr[:500]}")
            return

        _update("正在按规则判定合规性（stage2）...")
        r3 = subprocess.run(
            [PYTHON_EXE, str(BASE_DIR / "auto_approve.py"), "--stage2"],
            cwd=str(BASE_DIR), env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        if r3.returncode != 0:
            _update("stage2_failed", status="error", error=f"规则判定失败（exit={r3.returncode}）：{r3.stderr[:500]}")
            return

        _update("正在生成审查报告和审批意见...", status="running")
        # 验证结果是否存在
        stage2_path = BASE_DIR / "stage2_results.json"
        if not stage2_path.exists():
            _update("no_result", status="error", error="stage2 结果文件不存在，可能该供应商被分流跳过")
            return

        _update("完成", status="done")
    except subprocess.TimeoutExpired:
        _update("timeout", status="error", error="处理超时（>10分钟），请检查网络或重试")
    except Exception as e:
        _update("exception", status="error", error=str(e))


def _load_stage2_for_todo(todo_id: str):
    """加载某家供应商的 stage2 + textin + cache 数据"""
    stage2_path = BASE_DIR / "stage2_results.json"
    textin_path = BASE_DIR / "textin_results.json"
    cache_path = BASE_DIR / "cache_v4.json"

    stage2 = {}
    if stage2_path.exists():
        stage2 = json.loads(stage2_path.read_text(encoding="utf-8"))
    textin = {}
    if textin_path.exists():
        textin = json.loads(textin_path.read_text(encoding="utf-8"))
    cache = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    return stage2.get(str(todo_id), {}), textin, cache


# ============================================================
# 路由
# ============================================================
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """首页：待办列表"""
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/todos")
async def api_todos():
    """AJAX：拉取待办列表"""
    try:
        import auto_approve
    except Exception as e:
        return JSONResponse({"error": "module_load_failed", "msg": f"auto_approve 模块加载失败：{e}"}, status_code=500)
    try:
        result = auto_approve.fetch_pending_todos()
        data = result.get("data", {})
        rows = data.get("rows", []) if isinstance(data, dict) else []
        total = data.get("recordsTotal", 0) if isinstance(data, dict) else 0
        items = []
        for r in rows:
            # 标题格式："供应商名/统一社会信用代码"，去掉后缀只取供应商名
            raw_title = r.get("title") or r.get("applyUnitName") or r.get("applyUserName") or ""
            supplier_name = raw_title.split("/")[0].strip() if raw_title else ""
            # 申请时间格式化（原始 "2026-09-04 12:28:55" → "09-04 12:28"）
            raw_time = r.get("applyTime") or r.get("createTime") or ""
            apply_time_short = ""
            if raw_time and len(raw_time) >= 16:
                apply_time_short = raw_time[5:16]  # "09-04 12:28"
            items.append({
                "todoId": r.get("id") or r.get("todoId"),
                "name": supplier_name,
                "billName": r.get("businessBillName") or "",
                "billType": r.get("businessBillType") or "",
                "applyTime": raw_time,
                "applyTimeShort": apply_time_short,
            })
        return {"total": total, "count": len(items), "items": items}
    except auto_approve.SessionExpiredError as e:
        return JSONResponse({"error": "cookie_expired", "msg": str(e)}, status_code=401)
    except Exception as e:
        err = str(e)
        if "code" in err and "111" in err:
            return JSONResponse({"error": "cookie_expired", "msg": "Cookie 已过期"}, status_code=401)
        return JSONResponse({"error": "fetch_failed", "msg": err[:500]}, status_code=500)


@app.get("/approve/{todo_id}", response_class=HTMLResponse)
async def approve(request: Request, todo_id: str):
    """审批详情页"""
    return templates.TemplateResponse(request, "detail.html", {"todo_id": todo_id})


@app.post("/api/approve/{todo_id}")
async def api_start_approve(todo_id: str):
    """触发审批流水线（后台线程）"""
    with _task_lock:
        existing = _task_status.get(todo_id, {})
        if existing.get("status") == "running":
            return JSONResponse({"msg": "正在处理中，请勿重复点击", "step": existing.get("step")}, status_code=409)

    # 启动后台线程
    t = threading.Thread(target=_run_pipeline_for_one, args=(todo_id,), daemon=True)
    t.start()
    return {"msg": "已开始处理", "todo_id": todo_id}


@app.get("/api/status/{todo_id}")
async def api_status(todo_id: str):
    """查询审批进度"""
    with _task_lock:
        st = _task_status.get(todo_id, {"status": "idle", "step": "未开始"})
    return st


@app.get("/api/report/{todo_id}")
async def api_report(todo_id: str):
    """返回审查报告 HTML 片段 + 审批意见纯文本"""
    s2, textin, cache = _load_stage2_for_todo(todo_id)
    if not s2:
        return JSONResponse({"error": "no_data", "msg": "该供应商暂无审批结果，请先点击审批按钮"}, status_code=404)

    try:
        import gen_stage2_report
        import gen_opinion
        report_html = gen_stage2_report.render_supplier(str(todo_id), s2, textin, cache)
        opinion_text = gen_opinion.build_opinion(str(todo_id), s2, textin, cache)
        return {
            "report_html": report_html,
            "opinion_text": opinion_text,
            "decision": s2.get("decision", ""),
            "name": s2.get("name", ""),
        }
    except Exception as e:
        return JSONResponse({"error": "render_failed", "msg": str(e)[:500]}, status_code=500)


@app.post("/api/cookie")
async def api_update_cookie(cookie: str = Form(...), auth_token: str = Form("")):
    """更新 .env 中的 Cookie"""
    cookie = cookie.strip()
    if not cookie:
        return JSONResponse({"error": "cookie 为空"}, status_code=400)
    ok, msg = _backup_env_and_write(cookie, auth_token.strip())
    if not ok:
        return JSONResponse({"error": msg}, status_code=500)
    # 重新加载环境变量到当前进程
    os.environ["SCPMA_COOKIE"] = cookie
    if auth_token.strip():
        os.environ["SCPMA_AUTH_TOKEN"] = auth_token.strip()
    return {"msg": msg, "ok": True}


@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
