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
  echo ERROR: Python 3.11+ is required. Install Python and enable the py launcher.
  goto fail
)
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python 3.11 or newer is required.
  goto fail
)
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
if not exist "frontend\node_modules\vite\bin\vite.js" goto setup
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, pydantic, dotenv, requests, googleapiclient, google_auth_oauthlib, google_auth_httplib2, keyring, PIL" >nul 2>nul
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
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, pydantic, dotenv, requests, googleapiclient, google_auth_oauthlib, google_auth_httplib2, keyring, PIL" >nul 2>nul
if errorlevel 1 (
  echo ERROR: Required Python packages are still unavailable after setup.
  goto fail
)

:build_dashboard
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
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8765/api/health; if($r.StatusCode -eq 200){exit 0}; exit 1 } catch { exit 1 }" >nul 2>nul
if errorlevel 1 goto launch_backend

echo API process is already responding. Checking whether it serves the production dashboard...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 http://127.0.0.1:8765/; if($r.StatusCode -eq 200 -and $r.Content -match '<div id=.root.'){exit 0}; exit 1 } catch { exit 1 }" >nul 2>nul
if errorlevel 1 (
  echo ERROR: An older API is running on port 8765 but does not serve the built dashboard.
  echo Stop the older Gaming Clip Factory API and run start.bat again.
  goto fail
)
echo Reusing the running local API.

:wait_backend
echo Waiting for the local app...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ok=$false; for($i=0;$i -lt 45;$i++){ try { $h=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8765/api/health; $home=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8765/; if($h.StatusCode -eq 200 -and $home.StatusCode -eq 200){$ok=$true;break} } catch {}; Start-Sleep -Seconds 1 }; if(-not $ok){Write-Error 'The API and production dashboard did not both become ready within 45 seconds. Inspect the backend window.'; exit 1 }"
if errorlevel 1 goto fail

echo.
echo Checking media pipeline readiness...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r=Invoke-RestMethod -TimeoutSec 5 http://127.0.0.1:8765/api/readiness; $r.checks.PSObject.Properties | ForEach-Object { if($_.Value.ready){Write-Host ('  OK    '+$_.Name)} else {Write-Host ('  CHECK '+$_.Name+' - '+$_.Value.reason)} }; if(-not $r.ready){Write-Host 'Media production is DEGRADED until all required tools and paths are valid.'} } catch { Write-Warning ('Readiness report unavailable: '+$_.Exception.Message) }"

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
rem Detach the backend with persistent logs instead of nested START/CMD quoting.
rem The API process inherits the explicit repository root and uses backend/ as cwd.
set "GCF_PROJECT_ROOT=%CD%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=(Get-Location).Path; $logs=Join-Path $root 'data\logs'; New-Item -ItemType Directory -Force -Path $logs | Out-Null; $python=Join-Path $root '.venv\Scripts\python.exe'; $stdout=Join-Path $logs 'backend.stdout.log'; $stderr=Join-Path $logs 'backend.stderr.log'; $pidFile=Join-Path $logs 'backend.pid'; $p=Start-Process -FilePath $python -ArgumentList '-m app' -WorkingDirectory (Join-Path $root 'backend') -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru; Set-Content -Path $pidFile -Value $p.Id -Encoding ascii; Write-Output ('Started backend PID '+$p.Id)"
if errorlevel 1 (
  echo ERROR: Could not launch the backend. See data\logs\backend.stderr.log.
  goto fail
)
goto wait_backend

:fail
echo.
echo Startup could not safely complete. See the message above.
if exist "data\logs\backend.stderr.log" (
  echo.
  echo Last backend error log lines - data\logs\backend.stderr.log:
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -Tail 60 -ErrorAction SilentlyContinue 'data\logs\backend.stderr.log'"
)
if /I "%GCF_STARTUP_NO_PAUSE%"=="true" exit /b 1
pause
exit /b 1
