@echo off
REM ===========================================================================
REM Autobot Trader - one-click launcher (Windows)
REM   Double-click run.bat
REM First run: creates .env with a generated encryption key, installs backend
REM deps, builds the dashboard. Every run: starts http://localhost:8000 and
REM opens your browser. Close this window (or Ctrl+C) to stop.
REM ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
set PORT=8000

where python >nul 2>nul || (echo [autobot] ERROR: Python not found - install Python 3.11+ from python.org and tick "Add to PATH" & pause & exit /b 1)
where npm >nul 2>nul || (echo [autobot] ERROR: npm not found - install Node.js 18+ from nodejs.org & pause & exit /b 1)
where ffmpeg >nul 2>nul || echo [autobot] note: ffmpeg not found - only the YouTube live pipeline needs it

if not exist backend\.venv\Scripts\python.exe (
    echo [autobot] creating Python virtualenv...
    python -m venv backend\.venv || (pause & exit /b 1)
)
if not exist backend\.venv\Scripts\uvicorn.exe (
    echo [autobot] installing backend dependencies - one-time, a few minutes...
    backend\.venv\Scripts\python -m pip install --quiet --upgrade pip
    pushd backend
    .venv\Scripts\pip install --quiet -e . || (popd & pause & exit /b 1)
    popd
)

if not exist .env (
    echo [autobot] creating .env from .env.example...
    copy /y .env.example .env >nul
)
findstr /r "^AUTOBOT_MASTER_KEY=." .env >nul 2>nul
if errorlevel 1 (
    echo [autobot] generating encryption master key...
    backend\.venv\Scripts\python -c "import re,pathlib;from cryptography.fernet import Fernet;k=Fernet.generate_key().decode();p=pathlib.Path('.env');t=p.read_text();t=re.sub(r'(?m)^AUTOBOT_MASTER_KEY=.*$','AUTOBOT_MASTER_KEY='+k,t) if 'AUTOBOT_MASTER_KEY=' in t else t+'\nAUTOBOT_MASTER_KEY='+k+'\n';p.write_text(t)"
)

if not exist backend\static\index.html (
    echo [autobot] building the dashboard - one-time...
    pushd frontend
    call npm install --no-audit --no-fund --loglevel=error || (popd & pause & exit /b 1)
    call npm run build || (popd & pause & exit /b 1)
    popd
)

echo [autobot] starting Autobot Trader on http://localhost:%PORT%
echo [autobot] login password is AUTOBOT_ADMIN_PASSWORD from .env (default: change-me)
start "" "http://localhost:%PORT%"
cd backend
.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port %PORT%
pause
