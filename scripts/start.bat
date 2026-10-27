@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
title Gaming Clip Factory AI - Startup

echo.
echo ============================================================
echo          GAMING CLIP FACTORY AI - ONE-CLICK START
echo ============================================================
echo.

where py >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python Launcher is required. Install Python 3.11 or 3.12 and enable the py launcher.
  goto fail
)
py -3.12 -c "import sys" >nul 2>nul
if not errorlevel 1 goto python_ok
py -3.11 -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python 3.11 or 3.12 is required. Install one of these supported versions and retry.
  goto fail
)
:python_ok
where node >nul 2>nul
if errorlevel 1 (
  echo ERROR: Node.js is required for the dashboard build.
  goto fail
)
node -e "const v=process.versions.node.split('.').map(Number); process.exit((v[0]===20 && v[1]>=19) || (v[0]>=22 && (v[0]>22 || v[1]>=12)) ? 0 : 1)" >nul 2>nul
if errorlevel 1 (
  echo ERROR: Install Node.js 20.19+ or 22.12+.
  goto fail
)

if not exist ".env" (
  if not exist ".env.example" (
    echo ERROR: .env.example is missing; this repository checkout is incomplete.
    goto fail
  )
  copy /Y ".env.example" ".env" >nul
  if errorlevel 1 (
    echo ERROR: Could not create .env from .env.example.
    goto fail
  )
  echo Created .env with safe, disabled-by-default integration settings.
)

if not exist ".venv\Scripts\python.exe" goto setup
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info.major == 3 and sys.version_info.minor in (11,12) else 1)" >nul 2>nul
if errorlevel 1 goto setup
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\lock-state.ps1" -Mode test >nul 2>nul
if errorlevel 1 goto setup
if not exist "frontend\node_modules\vite\bin\vite.js" goto setup
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, pydantic, dotenv, requests, googleapiclient, google_auth_oauthlib, google_auth_httplib2, keyring, PIL, yt_dlp" >nul 2>nul
if errorlevel 1 goto setup
".venv\Scripts\python.exe" -m pip check >nul 2>nul
if errorlevel 1 goto setup
pushd frontend
call npm ls --depth=0 >nul 2>nul
set "FRONTEND_DEPS_STATUS=%ERRORLEVEL%"
popd
if not "%FRONTEND_DEPS_STATUS%"=="0" goto setup
goto build_dashboard

:setup
echo Installing or repairing application dependencies...
call scripts\setup-windows.bat
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -c "import sys,fastapi,uvicorn,pydantic,dotenv,requests,googleapiclient,google_auth_oauthlib,google_auth_httplib2,keyring,PIL,yt_dlp; raise SystemExit(0 if sys.version_info.major == 3 and sys.version_info.minor in (11,12) else 1)" >nul 2>nul
if errorlevel 1 (
  echo ERROR: Supported Python or required packages are still unavailable after setup.
  goto fail
)

:build_dashboard
findstr /R /C:"^[ ]*GCF_RENDER_ENGINE=remotion[ ]*$" ".env" >nul
if errorlevel 1 goto remotion_ready
if exist "remotion\\node_modules\\@remotion\\renderer\\package.json" goto remotion_ready
echo.
echo Remotion is selected but its runtime is missing. Installing and smoke-testing it now...
call scripts\\setup-remotion.bat
if errorlevel 1 goto fail
:remotion_ready
echo.
echo Building the optimized dashboard bundle...
pushd frontend
call npm run build
set "BUILD_STATUS=%ERRORLEVEL%"
popd
if not "%BUILD_STATUS%"=="0" (
  echo ERROR: Dashboard typecheck/build failed. Review the build output above.
  goto fail
)
if not exist "frontend\dist\index.html" (
  echo ERROR: Frontend build finished without frontend\dist\index.html.
  goto fail
)

echo.
echo Checking whether the local API is already running...
".venv\Scripts\python.exe" scripts\local_http.py health >nul 2>nul
if errorlevel 1 goto launch_backend

echo API process is already responding. Checking whether it serves the production dashboard...
".venv\Scripts\python.exe" scripts\local_http.py dashboard
if errorlevel 1 (
  echo ERROR: An older API is running on port 8765 but does not serve the built dashboard.
  echo Stop the older Gaming Clip Factory API and run start.bat again.
  goto fail
)
echo Reusing the running local API.

:wait_backend
echo Waiting for the local app...
".venv\Scripts\python.exe" scripts\local_http.py wait
if errorlevel 1 goto fail

echo.
echo Checking media pipeline readiness...
".venv\Scripts\python.exe" scripts\local_http.py readiness

echo.
echo Gaming Clip Factory AI is open.
echo Dashboard: http://127.0.0.1:8765
echo API docs: http://127.0.0.1:8765/docs
echo Readiness: http://127.0.0.1:8765/api/readiness
echo Public YouTube publishing remains disabled unless explicitly configured.
if /I "%GCF_STARTUP_SKIP_BROWSER%"=="true" (
  echo Browser launch skipped because GCF_STARTUP_SKIP_BROWSER=true.
) else (
  start "" "http://127.0.0.1:8765"
)
echo.
echo The backend runs in the background; use scripts\stop.bat to stop it.
echo This launcher window can now be closed.
exit /b 0

:launch_backend
echo Starting local API and production dashboard...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\launch-backend.ps1"
if errorlevel 1 goto fail
goto wait_backend

:fail
echo.
echo Startup could not safely complete. See the message above.
if exist "data\logs\backend.stdout.log" powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -Tail 40 -ErrorAction SilentlyContinue 'data\logs\backend.stdout.log'"
if exist "data\logs\backend.stderr.log" powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -Tail 60 -ErrorAction SilentlyContinue 'data\logs\backend.stderr.log'"
if /I "%GCF_STARTUP_NO_PAUSE%"=="true" exit /b 1
pause
exit /b 1
