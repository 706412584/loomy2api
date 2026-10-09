@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title loomy2api - 一键停止

set "PORT=17990"
set "FOUND="

for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
  echo 结束进程 PID %%p ...
  taskkill /PID %%p /F >nul 2>&1
  set "FOUND=1"
)

if not defined FOUND (
  echo 端口 %PORT% 上没有监听进程，loomy2api 本来就没在运行。
) else (
  echo loomy2api 已停止。
)
echo.
pause
