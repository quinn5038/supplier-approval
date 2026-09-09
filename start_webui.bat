@echo off
chcp 65001 >nul
title ICCEC Supplier Approval WebUI
echo ============================================
echo   ICCEC Supplier Approval WebUI
echo ============================================
echo.
echo Starting webui on port 8000 ...
echo.
echo After this window shows "Application startup complete",
echo open http://127.0.0.1:8000 in your browser.
echo.
echo KEEP THIS WINDOW OPEN while using the webui.
echo Close this window to stop the service.
echo.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python -m uvicorn web.app:app --host 0.0.0.0 --port 8000
echo.
echo ============================================
echo   WebUI stopped. Press any key to close.
echo ============================================
pause >nul