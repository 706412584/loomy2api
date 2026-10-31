@echo off
rem ---------------------------------------------------------------------------
rem loomy2api - Windows startup script
rem 直接运行本脚本，或由「任务计划程序」在登录时调用。
rem 服务自己写日志到 logs\gateway.log，这里不需要控制台重定向。
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if not exist "logs" mkdir "logs"

set "PY=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PY%" set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

rem 探测端口要用能输出到控制台的解释器（pythonw.exe 没有 stdout）
set "PROBE=%~dp0.venv\Scripts\python.exe"
if not exist "%PROBE%" set "PROBE=python"

rem ---------- 防止重复启动 ----------
rem Windows 允许 bind(0.0.0.0:P) 和 bind(127.0.0.1:P) 同时成功，所以第二个网关
rem 会悄悄分走一半连接，却共用同一个 accounts.json —— 两个账号池互相顶掉 session，
rem 表现就是客户端请求中途静默中断。loomy2api 自己也持有 gateway.lock 会拒绝，
rem 这里只是提前失败，免得等 Python 启动完才发现。
set "PORT="
for /f "delims=" %%p in ('"%PROBE%" -c "import json,pathlib;print(json.loads(pathlib.Path('config.json').read_text(encoding='utf-8')).get('port',17990))" 2^>nul') do set "PORT=%%p"
if not defined PORT set "PORT=17990"

set "RUNPID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING" 2^>nul') do set "RUNPID=%%p"
if defined RUNPID (
  echo [已经运行] 端口 %PORT% 上已有监听（PID %RUNPID%）。
  echo            同一个账号池只能由一个网关持有，重复启动会导致请求中断。
  choice /C YN /N /T 20 /D N /M "要先关闭旧实例再启动吗？(Y=关闭并启动 / N=退出) "
  if errorlevel 2 (
    echo 已退出，未做任何改动。
    endlocal
    exit /b 1
  )
  echo 正在停止旧实例 PID %RUNPID% ...
  taskkill /PID %RUNPID% /F >nul 2>&1
  timeout /t 2 /nobreak >nul
)

echo 启动 loomy2api（Ctrl+C 停止，或运行 stop.cmd）
echo   面板: http://127.0.0.1:%PORT%/panel
echo   日志: %~dp0logs\gateway.log

"%PY%" -m loomy2api serve
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo loomy2api 退出，代码 %RC%。
  if "%RC%"=="3" echo   原因见上面的「启动被拒绝」提示：已有实例在运行。
  pause
)
endlocal
