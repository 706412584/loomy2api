@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title loomy2api - 一键启动（自动重启）

echo ==============================================
echo    loomy2api - 一键启动
echo ==============================================
echo.

REM ---------- 0. 先停掉占用端口的旧实例（免手动停止） ----------
set "PORT=17990"
set "KILLED="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING" 2^>nul') do (
  echo [重启] 停止旧实例 PID %%p ...
  taskkill /PID %%p /F >nul 2>&1
  set "KILLED=1"
)
if defined KILLED (
  timeout /t 1 /nobreak >nul
  echo [重启] 旧实例已停止。
)

REM ---------- 1. 检查 Python ----------
where python >nul 2>nul
if errorlevel 1 goto no_python

REM ---------- 2. 检查 Python 版本 ----------
python -c "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)"
if errorlevel 1 goto old_python

REM ---------- 3. 初始化 config.json ----------
if not exist config.json (
  if exist config.example.json (
    copy /y config.example.json config.json >nul
    echo [初始化] 已生成 config.json（默认配置）。
  ) else (
    echo [错误] 缺少 config.example.json，请确认仓库完整。
    goto err
  )
)

REM ---------- 4. 初始化 accounts.json ----------
if not exist accounts.json (
  echo {"accounts": []}> accounts.json
  echo [初始化] 已生成 accounts.json（账号保存处）。
)

echo.
echo 网关启动中 ...关闭本窗口即停止；改代码后再点一次本脚本即可热重启。
echo   面板：http://127.0.0.1:%PORT%/panel
echo   健康：http://127.0.0.1:%PORT%/health
echo   日志：%~dp0logs\gateway.log
echo.

python -m loomy2api serve
set rc=%errorlevel%
if "%rc%"=="0" (
  echo 网关已停止。
) else (
  echo [错误] 网关异常退出，请查看日志 logs\gateway.log
  pause
)
exit /b 0

:no_python
echo [错误] 未检测到 python 命令。
echo 请先安装 Python 3.9+ 并勾选 Add python.exe to PATH。
echo 下载地址：https://www.python.org/downloads/
goto err

:old_python
echo [错误] Python 版本过低，本项目要求 3.9 或以上。
goto err

:err
pause
exit /b 1
