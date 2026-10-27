@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
echo Gaming Clip Factory AI setup

where py >nul 2>nul
if errorlevel 1 (
  echo ERROR: Install the Python Launcher and Python 3.11 or 3.12.
  goto fail
)
set "PYTHON_SELECTOR="
py -3.12 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYTHON_SELECTOR=-3.12"
if not defined PYTHON_SELECTOR (
  py -3.11 -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PYTHON_SELECTOR=-3.11"
)
if not defined PYTHON_SELECTOR (
  echo ERROR: Python 3.11 or 3.12 is required. Install one supported version and retry.
  goto fail
)

where node >nul 2>nul
if errorlevel 1 (
  echo ERROR: Install Node.js 20.19+ or 22.12+.
  goto fail
)
node -e "const v=process.versions.node.split('.').map(Number); process.exit((v[0]===20 && v[1]>=19) || (v[0]>=22 && (v[0]>22 || v[1]>=12)) ? 0 : 1)" >nul 2>nul
if errorlevel 1 (
  echo ERROR: Vite requires Node.js 20.19+ or 22.12+.
  goto fail
)

if not exist ".venv\Scripts\python.exe" goto create_venv
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info.major == 3 and sys.version_info.minor in (11,12) else 1)" >nul 2>nul
if errorlevel 1 goto backup_venv
goto install_dependencies

:backup_venv
echo Existing virtual environment uses an unsupported Python version.
echo Preserving it under a timestamped backup name before rebuilding.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$old=Join-Path (Get-Location).Path '.venv'; $stamp=Get-Date -Format 'yyyyMMdd-HHmmss'; $backup=Join-Path (Get-Location).Path ('.venv.backup-'+$stamp); Move-Item -LiteralPath $old -Destination $backup; Write-Output ('Preserved old environment at '+$backup)"
if errorlevel 1 goto fail

:create_venv
echo Creating virtual environment with %PYTHON_SELECTOR%...
py %PYTHON_SELECTOR% -m venv .venv
if errorlevel 1 goto fail

:install_dependencies
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info.major == 3 and sys.version_info.minor in (11,12) else 1)" >nul 2>nul
if errorlevel 1 (
  echo ERROR: New virtual environment did not use Python 3.11 or 3.12.
  goto fail
)
:dependencies
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install --require-hashes -r backend\requirements-lock.txt
if errorlevel 1 goto fail
pushd frontend
call npm ci --no-audit --no-fund
set "NPM_STATUS=%ERRORLEVEL%"
popd
if not "%NPM_STATUS%"=="0" goto fail
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\lock-state.ps1" -Mode write
if errorlevel 1 goto fail
echo Setup complete.
exit /b 0

:fail
echo.
echo Setup failed. Review errors above; no user data or existing environment backup was deleted.
exit /b 1
