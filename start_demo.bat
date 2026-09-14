@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run SupplierApproval-Setup.exe first.
    pause
    exit /b 1
)
set PYTHONIOENCODING=utf-8
set DEMO_MODE=true
".venv\Scripts\python.exe" launch_web.py
pause
