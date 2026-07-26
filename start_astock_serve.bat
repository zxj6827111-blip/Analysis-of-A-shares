@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONPATH=%CD%"
title AStock Serve 8765

REM ---- load machine-local config from .env (KEY=VALUE lines, # = comment) ----
if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
    if not "%%a"=="" set "%%a=%%b"
  )
  echo [env] loaded .env
) else (
  echo [env] WARNING: .env not found. Copy .env.example to .env and set
  echo [env]          MARKET_DATA_ROOT before formal use.
)
if not defined ASTOCK_ENV set "ASTOCK_ENV=production"

echo.
echo ============================================
echo   A-stock console: http://127.0.0.1:8765/
echo   Keep this window OPEN while using the site.
echo   Close this window to stop the service.
echo ============================================
echo.
echo Working directory: %CD%
echo PYTHONPATH=%PYTHONPATH%
echo ASTOCK_ENV=%ASTOCK_ENV%
echo MARKET_DATA_ROOT=%MARKET_DATA_ROOT%
where python
python --version
echo.

REM If port already in use, show who holds it then exit with message (not silent)
netstat -ano | findstr ":8765" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo [WARN] Port 8765 is already LISTENING.
  echo        Another AStock instance may already be running.
  echo        Open: http://127.0.0.1:8765/
  echo        Or close the other python window / kill the process first.
  echo.
  netstat -ano | findstr ":8765"
  echo.
  echo Press any key to close this window...
  pause >nul
  exit /b 0
)

python -m wtpy.apps.astock serve --host 127.0.0.1 --port 8765
set "EC=%ERRORLEVEL%"
echo.
if not "%EC%"=="0" (
  echo [ERROR] Service exited with code %EC%.
  echo Common causes:
  echo   1^) python not in PATH
  echo   2^) missing dependency: pip install fastapi uvicorn
  echo   3^) import error after code update — scroll up for traceback
  echo.
) else (
  echo Service stopped normally.
)
echo Press any key to close this window...
pause >nul
endlocal
exit /b %EC%
