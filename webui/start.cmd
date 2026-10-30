@echo off
rem Agent Bridge console launcher.
rem Double-click this file. It starts the local UI and opens the browser.
cd /d "%~dp0.."

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found in PATH. Install Python 3.11+ first.
  pause
  exit /b 1
)

echo Agent Bridge console  -^>  http://127.0.0.1:8787/
echo Close this window to stop it.
start "" http://127.0.0.1:8787/
python -m webui.server --port 8787
pause
