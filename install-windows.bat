@echo off
setlocal EnableDelayedExpansion
title Nexus AI — Windows Installer
color 0A

echo.
echo  ================================================================
echo   NEXUS AI  v2.0 — Windows Installer
echo   Real-time data  Email  Spotify  Flights  Local AI
echo  ================================================================
echo.

set "NEXUS_DIR=%USERPROFILE%\.nexus"
set "APP_DIR=%~dp0"

:: ── Step 1: Python ──────────────────────────────────────────────────────────
echo [1/6] Checking Python 3.10+...
python --version >nul 2>&1
if errorlevel 1 (
    echo  ^!  Python not found. Opening download page...
    echo     IMPORTANT: Check "Add Python to PATH" when installing!
    start https://www.python.org/downloads/
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo  OK Python %PY_VER%

:: ── Step 2: Packages ────────────────────────────────────────────────────────
echo [2/6] Installing Python packages (2-3 minutes)...
pip install --quiet --upgrade pip >nul 2>&1

pip install fastapi "uvicorn[standard]" "python-jose[cryptography]" ^
    "passlib[bcrypt]" slowapi pydantic "pydantic-settings" ^
    python-dotenv aiosqlite structlog httpx aiofiles ^
    requests beautifulsoup4 lxml apscheduler ^
    playwright spotipy ^
    "google-auth" "google-auth-oauthlib" "google-api-python-client" ^
    "python-telegram-bot" --quiet

if errorlevel 1 (
    echo  ^!  Some packages failed. Trying with --user flag...
    pip install --user fastapi uvicorn pydantic aiosqlite httpx playwright ^
        spotipy "python-telegram-bot" --quiet
)
echo  OK Packages installed

:: ── Step 3: Playwright browser ──────────────────────────────────────────────
echo [3/6] Installing Playwright Chromium browser (~200MB)...
python -m playwright install chromium >nul 2>&1
if errorlevel 1 (
    echo  ^!  Playwright failed. Will use API fallback for web scraping.
) else (
    echo  OK Chromium installed
)

:: ── Step 4: Directories ─────────────────────────────────────────────────────
echo [4/6] Creating data directories...
if not exist "%APP_DIR%data\logs"        mkdir "%APP_DIR%data\logs"
if not exist "%APP_DIR%data\screenshots" mkdir "%APP_DIR%data\screenshots"
if not exist "%APP_DIR%data\cache"       mkdir "%APP_DIR%data\cache"
echo  OK Directories ready

:: ── Step 5: Ollama (optional, ask user) ─────────────────────────────────────
echo [5/6] Ollama (optional — for offline AI)...
ollama --version >nul 2>&1
if errorlevel 1 (
    echo  ^!  Ollama not installed.
    set /p "INSTALL_OLLAMA=  Install Ollama for offline AI? [Y/n]: "
    if /i "!INSTALL_OLLAMA!" neq "n" (
        echo     Opening Ollama download page...
        start https://ollama.com/download/windows
        echo     Install Ollama, then re-run this script to pull models.
    )
) else (
    echo  OK Ollama already installed
    echo  Pulling small model ^(llama3.2:3b, 2GB^)...
    ollama pull llama3.2:3b >nul 2>&1 && echo  OK llama3.2:3b pulled
    ollama pull nomic-embed-text >nul 2>&1 && echo  OK nomic-embed-text pulled
)

:: ── Step 6: First-time setup ─────────────────────────────────────────────────
echo [6/6] Running first-time setup...
python "%APP_DIR%scripts\setup.py" --auto
echo  OK Setup complete

:: ── Create start.bat ─────────────────────────────────────────────────────────
echo @echo off > "%APP_DIR%start.bat"
echo title Nexus AI >> "%APP_DIR%start.bat"
echo echo Starting Nexus AI... >> "%APP_DIR%start.bat"
echo ollama serve ^>nul 2^>^&1 ^& >> "%APP_DIR%start.bat"
echo timeout /t 2 /nobreak ^>nul >> "%APP_DIR%start.bat"
echo start http://127.0.0.1:8000 >> "%APP_DIR%start.bat"
echo cd /d "%APP_DIR%" >> "%APP_DIR%start.bat"
echo uvicorn src.api.main:app --host 127.0.0.1 --port 8000 >> "%APP_DIR%start.bat"
echo pause >> "%APP_DIR%start.bat"

echo.
echo  ================================================================
echo   Installation complete!
echo  ================================================================
echo.
echo   To start Nexus AI:    double-click start.bat
echo   To configure keys:    python scripts\setup.py
echo   Required:  Groq key from console.groq.com  (free, no card)
echo   Optional:  Spotify, Gmail, Telegram keys
echo.
echo   Then open:  http://127.0.0.1:8000
echo  ================================================================
echo.

set /p "RUN_NOW=Start Nexus AI now? [Y/n]: "
if /i "%RUN_NOW%" neq "n" (
    call "%APP_DIR%start.bat"
)
pause
