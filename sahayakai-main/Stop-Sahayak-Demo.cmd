@echo off
REM Stops only the processes Start-Sahayak-Demo.cmd started (recorded in %TEMP%\riya-local\pids.json).
setlocal
cd /d "%~dp0"
set "NODE_EXE=node"
where node >nul 2>nul || set "NODE_EXE=E:\NodeJS\node.exe"
"%NODE_EXE%" scripts\sampark\riya-local.mjs --stop
ping -n 4 127.0.0.1 >nul
endlocal
