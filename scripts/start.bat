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
  echo ERROR: Node.js is required for the dashboard.
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
goto start_services

:setup
echo Installing or repairing application dependencies...
call scripts\setup-windows.bat
if errorlevel 1 goto fail

:start_services
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Backend virtual environment is missing after setup.
  goto fail
)
if not exist "frontend\node_modules\vite\bin\vite.js" (
  echo ERROR: Frontend dependencies are missing after setup.
  goto fail
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8765/api/health; exit 0 } catch { exit 1 }" >nul 2>nul
if not errorlevel 1 (
  echo Backend is already responding at http://127.0.0.1:8765. Reusing it.
) else (
  echo Starting backend...
  start "Gaming Clip Factory AI - Backend" cmd /k "cd /d ""%CD%"" ^&^& scripts\run-backend.bat"
)

echo Waiting for backend readiness...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ok=$false; for($i=0;$i -lt 45;$i++){ try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8765/api/health; if($r.StatusCode -eq 200){$ok=$true;break} } catch {}; Start-Sleep -Seconds 1 }; if(-not $ok){Write-Error 'Backend did not become ready within 45 seconds. Inspect the backend window.'; exit 1 }"
if errorlevel 1 goto fail

echo.
echo Checking media pipeline readiness...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r=Invoke-RestMethod -TimeoutSec 5 http://127.0.0.1:8765/api/readiness; $r.checks.PSObject.Properties | ForEach-Object { if($_.Value.ready){Write-Host ('  OK    '+$_.Name)} else {Write-Host ('  CHECK '+$_.Name+' - '+$_.Value.reason)} }; if(-not $r.ready){Write-Host 'Media production is DEGRADED until all required tools and paths are valid.'} } catch { Write-Warning ('Readiness report unavailable: '+$_.Exception.Message) }"

powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:5173/; exit 0 } catch { exit 1 }" >nul 2>nul
if not errorlevel 1 (
  echo Dashboard is already responding at http://127.0.0.1:5173. Reusing it.
) else (
  echo Starting dashboard...
  start "Gaming Clip Factory AI - Dashboard" cmd /k "cd /d ""%CD%\frontend"" ^&^& npm run dev"
)

echo Waiting for dashboard...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ok=$false; for($i=0;$i-lt 45;$i++){ try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:5173/; if($r.StatusCode -eq 200){$ok=$true;break} } catch {}; Start-Sleep -Seconds 1 }; if(-not $ok){Write-Error 'Dashboard did not become ready within 45 seconds. Inspect the dashboard window.'; exit 1 }"
if errorlevel 1 goto fail

echo.
echo Gaming Clip Factory AI is open.
echo Dashboard: http://127.0.0.1:5173
echo API health: http://127.0.0.1:8765/api/health
echo Readiness: http://127.0.0.1:8765/api/readiness
echo Public YouTube publishing remains disabled unless you explicitly configure it.
start "" "http://127.0.0.1:5173"
echo.
echo Close the separate backend/dashboard windows to stop those processes.
echo This launcher window can now be closed.
exit /b 0

:fail
echo.
echo Startup could not safely complete. See the message above and the backend/dashboard windows.
pause
exit /b 1
