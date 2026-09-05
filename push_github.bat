@echo off
chcp 65001 >nul
title Push to GitHub
echo ============================================
echo   Push supplier-approval to GitHub
echo ============================================
echo.
echo NOTE: your proxy client (Clash etc.) must be RUNNING,
echo       otherwise github.com is unreachable.
echo.
cd /d "C:\Users\CHEC\WorkBuddy\2026-08-12-10-09-50\supplier_approval_starter"
set GIT="C:\Users\CHEC\.workbuddy\binaries\PortableGit\versions\1.2.0\cmd\git.exe"
%GIT% push -u origin master
echo.
echo ============================================
if %errorlevel%==0 (
    echo   Push OK!
) else (
    echo   Push FAILED. Common causes:
    echo   1. Proxy client not running
    echo   2. GitHub login cancelled - run again
)
echo ============================================
pause