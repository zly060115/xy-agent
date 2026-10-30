@echo off
cd /d "%~dp0"
if /i "%1"=="web" (
    .venv\Scripts\python.exe web.py
) else (
    .venv\Scripts\python.exe main.py %*
)
