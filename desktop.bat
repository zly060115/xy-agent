@echo off
rem ============================================================
rem  Xiaoman Agent - desktop launcher (native window, no browser)
rem
rem  Double click this file. A native window opens; closing the
rem  window stops the app completely (no leftover python.exe).
rem
rem  Usage:
rem    desktop.bat          normal launch (no console window)
rem    desktop.bat -d       debug: console + F12 devtools in window
rem ============================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] venv not found in:
    echo         %~dp0
    echo.
    echo Run:  python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

rem ---- debug mode: keep console, enable devtools ----
if /i "%~1"=="-d" (
    title Xiaoman Agent (desktop debug)
    echo [debug] console stays open; press Ctrl+C to stop.
    echo [debug] log: %USERPROFILE%\.xy\desktop.log
    .venv\Scripts\python.exe desktop.py --debug
    echo.
    echo Desktop app exited with code %errorlevel%.
    pause
    exit /b %errorlevel%
)

rem ---- normal launch: pythonw = no black console window ----
rem Errors still land in %USERPROFILE%\.xy\desktop.log .
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" desktop.py
) else (
    start "" pythonw desktop.py
)
exit /b 0
