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
cd /d "C:\Users\CHEC\WorkBuddy\2026-08-12-10-09-50\supplier_approval_starter"
set PYTHONIOENCODING=utf-8
"C:\Users\CHEC\.workbuddy\binaries\python\envs\default\Scripts\uvicorn.exe" web.app:app --host 0.0.0.0 --port 8000
echo.
echo ============================================
echo   WebUI stopped. Press any key to close.
echo ============================================
pause >nul