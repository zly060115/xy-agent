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

echo [1/2] Checking Ollama...
curl -s --noproxy 127.0.0.1 --max-time 3 http://localhost:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo       starting...
    start "" /min "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve
    timeout /t 8 /nobreak >nul
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
echo   URL: http://127.0.0.1:7860
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
