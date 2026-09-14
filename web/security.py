"""Local accounts, bounded sessions, CSRF checks and login throttling."""
import hmac
import os
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

_sessions = {}
_attempts = {}
_lock = threading.Lock()
TTL = 1800
_local_csrf = secrets.token_urlsafe(32)

LOGIN = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>登录供应商辅助审批</title><h1>登录供应商辅助审批</h1>
<form method="post" action="/login"><label for="username">账户</label>
<input id="username" name="username" autocomplete="username" required>
<label for="password">密码</label><input id="password" name="password" type="password"
autocomplete="current-password" required><button>登录</button></form></html>"""


def same_origin(request):
    origin = request.headers.get("origin", "")
    return origin == f"{request.url.scheme}://{request.headers.get('host', '')}"


def install_security(app):
    @app.middleware("http")
    async def guard(request, call_next):
        host = urlsplit("//" + request.headers.get("host", "")).hostname
        allowed = {"127.0.0.1", "localhost", "::1"} | set(os.getenv("WEB_ALLOWED_HOSTS", "").split(","))
        if host not in allowed:
            return JSONResponse({"error": "invalid_host"}, 400)
        prefixes = ("/approve/", "/api/approve/", "/api/status/", "/api/report/", "/api/desens_image/")
        if any(request.url.path.startswith(p) for p in prefixes):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request.url.path.rsplit("/", 1)[-1]):
                return JSONResponse({"error": "invalid_todo_id"}, 400)
        if request.url.path == "/health":
            return await call_next(request)
        if os.getenv("WEB_AUTH_MODE", "local") == "local":
            if host not in {"127.0.0.1", "localhost", "::1"} or not request.client or request.client.host not in {"127.0.0.1", "::1"}:
                return JSONResponse({"error": "local_only"}, 403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"error": "cross_site_denied"}, 403)
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                if not same_origin(request) or not hmac.compare_digest(request.headers.get("x-csrf-token", ""), _local_csrf):
                    return JSONResponse({"error": "csrf_failed"}, 403)
            request.state.role = "admin"
            response = await call_next(request)
            response.set_cookie("supplier_csrf", _local_csrf, samesite="strict")
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            return response
        if not os.getenv("WEB_ADMIN_PASSWORD"):
            return HTMLResponse("请配置 WEB_ADMIN_PASSWORD 或使用本机模式。", 503)
        if request.url.path == "/login":
            return await call_next(request)
        now = time.monotonic()
        with _lock:
            for key in list(_sessions):
                if _sessions[key]["expires"] <= now:
                    del _sessions[key]
            session = _sessions.get(request.cookies.get("supplier_session"))
        if session is None:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": "login_required"}, 401)
            return RedirectResponse("/login", 303)
        request.state.role = session["role"]
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if session["role"] != "admin":
                return JSONResponse({"error": "forbidden"}, 403)
            if not same_origin(request) or not hmac.compare_digest(
                    request.headers.get("x-csrf-token", ""), session["csrf"]):
                return JSONResponse({"error": "csrf_failed"}, 403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/login")
    def login_form():
        return HTMLResponse(LOGIN)

    @app.post("/login")
    async def login(request: Request):
        if not same_origin(request):
            return JSONResponse({"error": "csrf_failed"}, 403)
        peer = request.client.host if request.client else "unknown"
        now = time.monotonic()
        with _lock:
            for key in list(_attempts):
                if now - _attempts[key][0] > 300:
                    del _attempts[key]
            started, count = _attempts.get(peer, (now, 0))
            if count >= 5 or len(_sessions) >= 1000:
                return JSONResponse({"error": "请稍后重试"}, 429)
            _attempts[peer] = (started, count + 1)
        form = await request.form()
        username, password = str(form.get("username", "")), str(form.get("password", ""))
        expected = os.getenv("WEB_ADMIN_PASSWORD" if username == "admin" else "WEB_VIEWER_PASSWORD", "")
        if username not in ("admin", "viewer") or not expected or not hmac.compare_digest(password.encode(), expected.encode()):
            return HTMLResponse("账户或密码错误。<a href='/login'>重新登录</a>", 401)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with _lock:
            _sessions[token] = {"role": username, "csrf": csrf, "expires": now + TTL}
            _attempts.pop(peer, None)
        response = RedirectResponse("/", 303)
        secure = request.url.scheme == "https"
        response.set_cookie("supplier_session", token, httponly=True, secure=secure, samesite="strict", max_age=TTL)
        response.set_cookie("supplier_csrf", csrf, secure=secure, samesite="strict", max_age=TTL)
        return response

    @app.post("/logout")
    async def logout(request: Request):
        with _lock:
            _sessions.pop(request.cookies.get("supplier_session"), None)
        response = RedirectResponse("/login", 303)
        response.delete_cookie("supplier_session")
        response.delete_cookie("supplier_csrf")
        return response
