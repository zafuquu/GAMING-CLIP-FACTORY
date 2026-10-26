@echo off
setlocal
cd /d "%~dp0.."
echo Gaming Clip Factory AI setup
where py >nul 2>nul || (echo ERROR: Install Python 3.11+ & pause & exit /b 1)
where node >nul 2>nul || (echo ERROR: Install Node.js 20+ & pause & exit /b 1)
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
echo Setup complete. Start scripts\run-backend.bat and scripts\run-dashboard.bat
pause
exit /b 0
:fail
echo Setup failed. Review errors above; nothing was auto-deleted.
pause
exit /b 1
