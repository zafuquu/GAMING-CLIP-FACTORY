@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
title Gaming Clip Factory AI - Remotion Setup

echo.
echo ============================================================
echo      GAMING CLIP FACTORY AI - REMOTION RENDERER SETUP
echo ============================================================
echo.

where node >nul 2>nul
if errorlevel 1 (
  echo ERROR: Node.js is required. Install Node.js 20.19+ or 22.13+ and retry.
  exit /b 1
)
node -e "const v=process.versions.node.split('.').map(Number); process.exit(((v[0]===20 && v[1]>=19) || (v[0]===22 && v[1]>=13) || v[0]>22) ? 0 : 1)" >nul 2>nul
if errorlevel 1 (
  echo ERROR: Remotion requires Node.js 20.19+ or 22.13+.
  exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
  echo ERROR: npm was not found. Reinstall Node.js with npm enabled.
  exit /b 1
)

pushd remotion
echo Installing the committed, lockfile-pinned Remotion runtime...
if not exist "package-lock.json" (
  popd
  echo ERROR: remotion\package-lock.json is missing. Restore the committed lockfile before setup.
  exit /b 1
)
call npm ci --no-audit --no-fund
if errorlevel 1 (
  popd
  echo ERROR: Remotion dependency installation failed. Review the npm output above.
  exit /b 1
)
echo.
echo Running the synthetic video render smoke test...
call npm run test:render
set "TEST_STATUS=%ERRORLEVEL%"
popd

if not "%TEST_STATUS%"=="0" (
  echo ERROR: Remotion smoke test failed. Keep GCF_RENDER_ENGINE=ffmpeg until this is fixed.
  exit /b 1
)
echo.
echo Remotion runtime installed and the synthetic render smoke test passed.
echo To use Remotion, set GCF_RENDER_ENGINE=remotion in .env and restart the app.
echo This local render test did not connect YouTube or upload anything.
exit /b 0
