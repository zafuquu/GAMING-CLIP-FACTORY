@echo off
setlocal
cd /d "%~dp0.."
echo Gaming Clip Factory AI setup
where py >nul 2>nul || (echo ERROR: Install Python 3.11+ & goto fail)
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if errorlevel 1 (echo ERROR: Python 3.11 or newer is required. Install it and retry. & goto fail)
where node >nul 2>nul || (echo ERROR: Install Node.js 20.19+ or 22.12+ & goto fail)
node -e "const v=process.versions.node.split('.').map(Number); process.exit((v[0]===20 && v[1]>=19) || (v[0]>=22 && (v[0]>22 || v[1]>=12)) ? 0 : 1)" >nul 2>nul
if errorlevel 1 (echo ERROR: Vite requires Node.js 20.19+ or 22.12+. Install a supported release and retry. & goto fail)
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -r backend\requirements.txt
if errorlevel 1 goto fail
pushd frontend
call npm install --no-audit --no-fund
if errorlevel 1 (popd & goto fail)
popd
echo Setup complete.
exit /b 0
:fail
echo.
echo Setup failed. Review errors above; nothing was auto-deleted.
exit /b 1
