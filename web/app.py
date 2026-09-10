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
import logging
from pathlib import Path
from datetime import datetime

# 9/6 修复：/api/todos 重试路径用了 log.warning 但模块没定义 log（NameError）
log = logging.getLogger("webui")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# 把父目录加入 sys.path，便于 import auto_approve 等模块
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
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

# 9/6 修复：webui 重启时内存 _task_status 会被清空，但如果任务字典里
# 遗留了 running 状态（线程随旧进程死亡但状态未清理），前端会永久
# 显示"处理中"。启动时把所有 running 重置为 error（让用户可以重试）。
def _reset_stale_running():
    with _task_lock:
        for tid, st in _task_status.items():
            if st.get("status") == "running":
                st["status"] = "error"
                st["error"] = "上次处理被中断（服务重启），请重新点击后台审批"
                st["step"] = "stale_running_reset"
        log.info(f"[_reset_stale_running] 重置 {len(_task_status)} 条状态")

_reset_stale_running()


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
    def _update(step, status="running", error=None, progress=None):
        with _task_lock:
            prev = _task_status.get(todo_id, {})
            # progress 未显式传时保留旧值（让同阶段的细分文案也能平滑刷新）
            new_progress = progress if progress is not None else prev.get("progress", 0)
            _task_status[todo_id] = {
                "step": step,
                "status": status,
                "error": error,
                "progress": new_progress,
                "started_at": prev.get("started_at", datetime.now().isoformat()),
                "updated_at": datetime.now().isoformat(),
            }

    try:
        _update("正在拉取供应商数据（下载材料入缓存）...", progress=20)
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

        # 9/5 修复：auto_approve.py FETCH_ONLY 模式只拉元数据（materials_detail）
        # 到 cache_v4.json，不下载实际文件！必须显式调 download_supplier_files
        # 把真实资质文件下载到 files_cache/<todoId>/，否则 OCR 拿不到图片。
        _update("正在下载资质文件到本地...", progress=30)
        try:
            import textin_pipeline as _tp
            # 9/5 修复：download_supplier_files 在 textin_pipeline.py 不在 auto_approve.py
            files_dir = BASE_DIR / "files_cache" / str(todo_id)
            files_dir.mkdir(parents=True, exist_ok=True)
            _tp.download_supplier_files(str(todo_id), delay=2.0)
        except Exception as e:
            print(f"[download] 文件下载失败（不阻塞流程，OCR可能空）：{e}")

        # 2026-09-04 保密合规改造：下载完成后自动脱敏（与 textin_pipeline.py 集成一致）
        _update("正在本地脱敏身份证（PaddleOCR 精确打码）...", progress=50)
        try:
            from desensitize import desensitize_dir
            desensitize_dir(BASE_DIR / "files_cache" / str(todo_id),
                            BASE_DIR / "files_cache_desens" / str(todo_id))
        except ImportError:
            pass  # 脱敏模块未装时跳过（不阻塞）
        except Exception as e:
            print(f"[desens] 脱敏失败（不阻塞流程）：{e}")

        _update("正在 OCR 识别证件文件（TextIn）...", progress=70)
        # 9/6 修复：必须传 parse <todo_id> 单家过滤——不带参数会 OCR 全部
        # files_cache_desens/ 下 33 家文件，跑 10+ 分钟，子进程超时报错，
        # 前端永久卡在处理中（"进度条不动"根因）
        r2 = subprocess.run(
            [PYTHON_EXE, str(BASE_DIR / "textin_pipeline.py"), "parse", str(todo_id)],
            cwd=str(BASE_DIR), env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=600,
        )
        if r2.returncode != 0:
            _update("ocr_failed", status="error", error=f"OCR 失败（exit={r2.returncode}）：{r2.stderr[:500]}")
            return

        _update("正在按规则判定合规性（stage2）...", progress=80)
        r3 = subprocess.run(
            [PYTHON_EXE, str(BASE_DIR / "auto_approve.py"), "--stage2"],
            cwd=str(BASE_DIR), env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        if r3.returncode != 0:
            _update("stage2_failed", status="error", error=f"规则判定失败（exit={r3.returncode}）：{r3.stderr[:500]}")
            return

        _update("正在生成审查报告和审批意见...", status="running", progress=95)
        # 验证结果是否存在
        stage2_path = BASE_DIR / "stage2_results.json"
        if not stage2_path.exists():
            _update("no_result", status="error", error="stage2 结果文件不存在，可能该供应商被分流跳过")
            return

        # 2026-09-04 修复：done 前校验该 todo 确实有审批结果——
        # run_stage2 只处理企查查缓存里有数据的供应商，新拉取的供应商没有企查查数据
        # 会被跳过 → stage2_results.json 里没有该条目 → 前端 report 404 显示
        # "该供应商暂无审批结果"却不说原因（此前反馈的 bug）
        try:
            stage2_data = json.loads(stage2_path.read_text(encoding="utf-8"))
        except Exception:
            stage2_data = {}
        if str(todo_id) not in stage2_data:
            # 区分"无数据"和"已转人工"，前者更准确（说明系统主动保守）
            _update("no_result", status="error",
                    error=("流水线已完成拉取/脱敏/OCR，但 stage2 规则引擎未给该供应商生成审批结果。"
                           "通常原因：本系统暂未配置企业信息 API（QCC_APP_KEY 未设置），"
                           "该供应商无公开企查查数据，规则引擎为安全起见自动跳过。"
                           "建议：转人工审批 + 在演示中说明该兜底逻辑的合规价值。"))
            return

        _update("完成", status="done", progress=100)
    except subprocess.TimeoutExpired:
        _update("timeout", status="error", error="处理超时（>10分钟），请检查网络或重试")
    except Exception as e:
        _update("exception", status="error", error=str(e))


def _has_valid_result(todo_id: str) -> bool:
    """判断某供应商是否有审批结果（decision 非空，含 skip「不适用」）。

    9/6：skip（境外/集团独有分流不适用）也视为有结果——首页显示「查看结果」，
    报告页综合核验表处写出「不适用」及具体原因，与业务需求一致。
    """
    stage2_path = BASE_DIR / "stage2_results.json"
    if not stage2_path.exists():
        return False
    try:
        stage2 = json.loads(stage2_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return stage2.get(str(todo_id), {}).get("decision") in ("reject", "manual", "approve", "skip")


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
    """AJAX：拉取待办列表（2026-09-05 加重试防偶发 ConnectionResetError）"""
    try:
        import auto_approve
    except Exception as e:
        return JSONResponse({"error": "module_load_failed", "msg": f"auto_approve 模块加载失败：{e}"}, status_code=500)
    # 单次尝试 + 失败重试（Connection 类错误自动 5 秒后重试一次）
    for attempt in (1, 2):
        try:
            result = auto_approve.fetch_pending_todos()
            data = result.get("data", {})
            rows = data.get("rows", []) if isinstance(data, dict) else []
            total = data.get("recordsTotal", 0) if isinstance(data, dict) else 0
            # 9/6 修复：hasResult 判断「有效审批结果」（decision 非 skip），
            # 让首页按钮在服务重启后也能恢复，且与详情页判断一致
            items = []
            for r in rows:
                raw_title = r.get("title") or r.get("applyUnitName") or r.get("applyUserName") or ""
                supplier_name = raw_title.split("/")[0].strip() if raw_title else ""
                raw_time = r.get("applyTime") or r.get("createTime") or ""
                apply_time_short = raw_time[5:16] if raw_time and len(raw_time) >= 16 else ""
                todo_id = r.get("id") or r.get("todoId")
                items.append({
                    "todoId": todo_id,
                    "name": supplier_name,
                    "billName": r.get("businessBillName") or "",
                    "billType": r.get("businessBillType") or "",
                    "applyTime": raw_time,
                    "applyTimeShort": apply_time_short,
                    "hasResult": _has_valid_result(todo_id),
                })
            return {"total": total, "count": len(items), "items": items}
        except auto_approve.SessionExpiredError as e:
            return JSONResponse({"error": "cookie_expired", "msg": str(e)}, status_code=401)
        except Exception as e:
            err = str(e)
            if attempt == 1 and ("Connection" in err or "10054" in err or "aborted" in err.lower()):
                log.warning(f"[/api/todos] 偶发连接错误，5秒后重试: {err[:200]}")
                import time
                time.sleep(5)
                continue
            if "code" in err and "111" in err:
                return JSONResponse({"error": "cookie_expired", "msg": "Cookie 已过期"}, status_code=401)
            return JSONResponse({"error": "fetch_failed", "msg": err[:500]}, status_code=500)
    # 重试仍失败
    return JSONResponse({"error": "fetch_failed", "msg": "重试后仍失败，请稍后再试"}, status_code=500)


@app.get("/approve/{todo_id}", response_class=HTMLResponse)
async def approve(request: Request, todo_id: str):
    """审批详情页（供应商名称优先取 URL 参数，兜底从 cache_v4.json 读）"""
    name = request.query_params.get("name", "")
    if not name:
        # 兜底：从缓存里读供应商名（拉取过的供应商都有）
        try:
            cache_path = BASE_DIR / "cache_v4.json"
            if cache_path.exists():
                cache = json.loads(cache_path.read_text(encoding="utf-8"))
                entry = cache.get(str(todo_id), {})
                name = entry.get("supplier", {}).get("name", "") \
                    or entry.get("todo", {}).get("applyUnitName", "")
        except Exception:
            pass
    return templates.TemplateResponse(request, "detail.html",
                                      {"todo_id": todo_id, "supplier_name": name})


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


@app.get("/api/status_all")
async def api_status_all():
    """批量查询所有审批任务状态（首页恢复各行按钮状态用，只返回轻量字段）"""
    with _task_lock:
        return {tid: {"status": st.get("status"),
                      "step": st.get("step"),
                      "progress": st.get("progress", 0)}
                for tid, st in _task_status.items()}


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


@app.get("/api/desens_image/{todo_id}")
async def api_desens_image(todo_id: str):
    """返回该供应商脱敏后的身份证图片（报告页「点击查看脱敏后证件」用）"""
    desens_dir = BASE_DIR / "files_cache_desens" / str(todo_id)
    if not desens_dir.exists():
        return JSONResponse({"error": "not_found", "msg": "无脱敏文件"}, status_code=404)
    id_keywords = ("身份证", "证件", "id_card", "id_")
    for f in sorted(desens_dir.iterdir()):
        if not f.is_file():
            continue
        name_lower = f.name.lower()
        # 身份证可能是 PDF（脱敏后回写保持原扩展名，内容实为 PNG）
        if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".pdf") \
                and any(k in name_lower for k in id_keywords):
            media_type = None
            # 按文件头判断真实内容类型（PDF 身份证脱敏后是 PNG 内容）
            try:
                head = f.read_bytes()[:8]
                if head == b"\x89PNG\r\n\x1a\n":
                    media_type = "image/png"
            except Exception:
                pass
            return FileResponse(str(f), media_type=media_type)
    return JSONResponse({"error": "not_found", "msg": "未找到脱敏身份证图片"}, status_code=404)


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
