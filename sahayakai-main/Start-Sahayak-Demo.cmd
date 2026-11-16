@echo off
REM One click: Firestore + Storage emulators -> seed demo data -> VIDYA voice sidecar
REM -> Sahayak (next dev) -> health checks -> opens http://localhost:3000/
REM Re-running while it is up just opens the browser. Stop with Stop-Sahayak-Demo.cmd.
REM Practice mode only: no real phone rings. Logs: %TEMP%\riya-local\
setlocal
cd /d "%~dp0"
set "NODE_EXE=node"
where node >nul 2>nul || set "NODE_EXE=E:\NodeJS\node.exe"
title Sahayak AI local demo (keep this window open)
"%NODE_EXE%" scripts\sampark\riya-local.mjs --persist --no-menu --ticker --admin dev-user-123 --demo-family --open-home --port 3000
if errorlevel 1 (
  echo.
  echo Startup failed - see the STOPPED message above and the logs in %TEMP%\riya-local\
  pause
)
endlocal
