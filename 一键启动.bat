@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title loomy2api - 一键启动

echo ==============================================
echo    loomy2api - 一键启动
echo ==============================================
echo.

REM ---------- 0. 清掉所有占用端口的旧实例 ----------
REM 注意：这里必须循环杀掉「全部」监听者。Windows 允许 bind(0.0.0.0:P) 与
REM bind(127.0.0.1:P) 同时成功，所以端口上可能同时挂着两个网关，各自跑一份
REM 账号池、互相顶掉 session，表现就是客户端请求中途静默中断。原来只保留最后
REM 一个 PID，正好留下另一个继续捣乱。
set "PORT=17990"
set "KILLED="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING" 2^>nul') do (
  echo [提示] 停止旧实例 PID %%p ...
  taskkill /PID %%p /F >nul 2>&1
  set "KILLED=1"
)
if defined KILLED (
  ping -n 3 127.0.0.1 >nul
  call :wait_port_free
)

REM ---------- 1. 检查 Python ----------
where python >nul 2>nul
if errorlevel 1 goto no_python

python -c "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)"
if errorlevel 1 goto old_python

REM ---------- 2. 初始化配置文件 ----------
if not exist config.json (
  if exist config.example.json (
    copy /y config.example.json config.json >nul
    echo [初始化] 已创建 config.json（默认配置）。
  ) else (
    echo [错误] 缺少 config.example.json，请确认仓库完整。
    goto err
  )
)
if not exist accounts.json (
  echo {"accounts": []}> accounts.json
  echo [初始化] 已创建 accounts.json（账号保存在这里）。
)

REM ---------- 3. 探测局域网 IP（排除回环/链路本地/虚拟网卡） ----------
set "LANIP="
for /f "usebackq delims=" %%a in (`powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 | Where-Object IPAddress -notlike '127.*' | Where-Object IPAddress -notlike '169.254.*' | Where-Object InterfaceAlias -notlike '*vEthernet*' | Where-Object InterfaceAlias -notlike '*Loopback*' | Select-Object -First 1 -ExpandProperty IPAddress" 2^>nul`) do set "LANIP=%%a"

REM ---------- 4. 询问是否开放局域网访问 ----------
set "BINDHOST=127.0.0.1"
if defined LANIP (
  echo.
  echo [局域网] 检测到本机局域网 IP：!LANIP!
  choice /c YN /t 10 /d N /m "开放局域网访问（其他设备用 http://!LANIP!:17990）？10 秒不选=仅本机"
  if not errorlevel 2 set "BINDHOST=0.0.0.0"
) else (
  echo [局域网] 未检测到局域网连接（WiFi/网线未连），仅本机可访问。
)

if "!BINDHOST!"=="0.0.0.0" (
  echo [局域网] 已开放：其他设备调用 http://!LANIP!:%PORT%/v1/chat/completions
  echo [安全提示] 局域网内任何人都能用你的账号池，建议在 config.json 设置 api_keys。
) else (
  echo [本机模式] 网关仅监听 127.0.0.1。
)

echo.
echo 网关启动中 ...关闭本窗口即停止（再次运行本脚本可热重启）。
echo   面板：http://127.0.0.1:%PORT%/panel
echo   健康检查：http://127.0.0.1:%PORT%/health
echo   日志：%~dp0logs\gateway.log
echo.

set "LOOMY_HOST=!BINDHOST!"
python -m loomy2api serve
set rc=%errorlevel%
if "%rc%"=="0" (
  echo 网关已停止。
) else (
  echo [错误] 网关异常退出，详见 logs\gateway.log
  pause
)
exit /b 0

:no_python
echo [错误] 未检测到 python 命令。
echo 请先安装 Python 3.9+ 并勾选 Add python.exe to PATH。
goto err

:old_python
echo [错误] Python 版本过低，本项目要求 3.9 以上。
goto err

:err
pause
exit /b 1
