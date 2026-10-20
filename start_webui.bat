@echo off
chcp 65001 >nul
title ICCEC Supplier Approval WebUI - DUAL OCR
echo ============================================
echo   ICCEC Supplier Approval WebUI - ONLINE / OFFLINE OCR
echo ============================================
echo.
echo Starting unified WebUI on port 8000 ...
echo.
echo After this window shows "Application startup complete",
echo open http://127.0.0.1:8000 in your browser.
echo.
echo KEEP THIS WINDOW OPEN while using the webui.
echo Close this window to stop the service.
echo.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
REM Do not inherit a stale global Python home/path. Either can make a valid
REM virtual environment fail before startup with "No module named encodings".
set PYTHONHOME=
set PYTHONPATH=
set "PROJECT_PYTHON=%~dp0.venv\Scripts\python.exe"

call :check_python
if errorlevel 1 (
    echo Python environment is missing or damaged. Repairing it now ...
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" -InstallOnly
    if errorlevel 1 (
        echo.
        echo Automatic repair failed. Run setup.ps1 manually and keep this window open.
        pause
        exit /b 1
    )
    call :check_python
    if errorlevel 1 (
        echo Python environment is still unavailable after repair.
        pause
        exit /b 1
    )
)

if /I "%~1"=="--check" (
    echo WebUI Python environment check passed.
    exit /b 0
)

"%PROJECT_PYTHON%" launch_web.py
echo.
echo ============================================
echo   WebUI stopped. Press any key to close.
echo ============================================
pause >nul
exit /b 0

:check_python
if not exist "%PROJECT_PYTHON%" exit /b 1
"%PROJECT_PYTHON%" -I -c "import encodings, fastapi, jinja2, requests, uvicorn, yaml" >nul 2>&1
exit /b %errorlevel%
