@echo off
cd /d "%~dp0"

title Xiaoman Agent
echo ============================================
echo   Xiaoman Agent
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] venv not found in:
    echo         %~dp0
    echo.
    pause
    exit /b 1
)

set "PORT=7860"

rem ---- Step 0: already running? open the browser instead of fighting for the port ----
rem When the port is taken, gradio raises OSError and exits at once, which looks like
rem "double click does nothing". Probe first: HTTP 200 means Xiaoman is up.
curl -sf --noproxy 127.0.0.1 --max-time 3 -o nul http://127.0.0.1:%PORT%/ >nul 2>&1
if not errorlevel 1 (
    echo [i] Xiaoman is already running - opening browser
    echo     http://127.0.0.1:%PORT%
    start "" http://127.0.0.1:%PORT%
    exit /b 0
)

rem ---- Port held by some other program: say so clearly ----
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul
if not errorlevel 1 (
    echo [WARN] Port %PORT% is taken by another program:
    netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"
    echo        Close it, or run "set WEB_PORT=7861" before this script.
    echo.
)

echo [1/2] Checking Ollama...
curl -s --noproxy 127.0.0.1 --max-time 3 http://localhost:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo       starting...
    start "" /min "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve
    ping -n 9 127.0.0.1 >nul
    curl -s --noproxy 127.0.0.1 --max-time 5 http://localhost:11434/api/tags >nul 2>&1
    if errorlevel 1 (
        echo       [WARN] Ollama failed to start
    ) else (
        echo       ready
    )
) else (
    echo       already running
)

echo [2/2] Starting web UI...
echo.
echo   URL: http://127.0.0.1:%PORT%
echo.
echo   ==========================================
echo    Keep this window open.
echo    Closing it stops the service.
echo    Log file: web.log (for troubleshooting)
echo   ==========================================
echo.

.venv\Scripts\python.exe -u web.py 2>&1 | .venv\Scripts\python.exe .workbuddy\logtee.py

echo.
echo Service stopped.
pause
