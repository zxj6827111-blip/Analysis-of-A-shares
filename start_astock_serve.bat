@echo off
cd /d "E:\Software Development\wtpy-master"
set PYTHONPATH=E:\Software Development\wtpy-master
title AStock Serve 8765
echo.
echo ============================================
echo   A-stock console: http://127.0.0.1:8765/
echo   Keep this window OPEN while using the site.
echo   Close this window to stop the service.
echo ============================================
echo.
python -m wtpy.apps.astock serve --host 127.0.0.1 --port 8765
echo.
echo Service exited. Press any key to close.
pause >nul
