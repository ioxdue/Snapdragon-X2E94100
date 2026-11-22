@echo off
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch-tui.ps1" -ControlCenter
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo Avvio del centro di controllo Hexagon non riuscito.
    pause
)
exit /b %EXIT_CODE%
