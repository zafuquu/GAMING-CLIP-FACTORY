@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
title Gaming Clip Factory AI - All-in-One Installer

echo.
echo ============================================================
echo       GAMING CLIP FACTORY AI - ONE-TIME INSTALL
echo ============================================================
echo This installs the app dependencies, FFmpeg/FFprobe, and
echo the Remotion renderer. It does not enable YouTube publishing.
echo.

where winget >nul 2>nul
if errorlevel 1 (
  echo NOTE: winget is unavailable. Python, Node.js, and FFmpeg
  echo must already be installed and available on PATH.
)

where py >nul 2>nul
if errorlevel 1 (
  echo Python Launcher not found. Attempting installation with winget...
  where winget >nul 2>nul
  if errorlevel 1 goto prereq_error
  winget install --id Python.Python.3.12 --exact --silent --accept-package-agreements --accept-source-agreements
  if errorlevel 1 echo WARNING: Python installation may require manual completion.
)

where node >nul 2>nul
if errorlevel 1 (
  echo Node.js not found. Attempting installation with winget...
  where winget >nul 2>nul
  if errorlevel 1 goto prereq_error
  winget install --id OpenJS.NodeJS.LTS --exact --silent --accept-package-agreements --accept-source-agreements
  if errorlevel 1 echo WARNING: Node.js installation may require manual completion.
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo FFmpeg not found. Attempting installation with winget...
  where winget >nul 2>nul
  if errorlevel 1 goto prereq_error
  winget install --id Gyan.FFmpeg.Shared --exact --silent --accept-package-agreements --accept-source-agreements
  if errorlevel 1 echo WARNING: FFmpeg installation may require manual completion.
)

echo.
echo Refreshing environment lookup for installed prerequisites...
rem winget changed the persistent PATH, but this batch process still has
rem the old copy. Re-read Machine and User PATH values into this process.
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$m=[Environment]::GetEnvironmentVariable('Path','Machine'); $u=[Environment]::GetEnvironmentVariable('Path','User'); (($m,$u,$env:PATH | Where-Object { $_ }) -join ';')"`) do set "PATH=%%P"

where py >nul 2>nul
if errorlevel 1 goto restart_shell
where node >nul 2>nul
if errorlevel 1 goto restart_shell
node -e "const v=process.versions.node.split('.').map(Number); process.exit((v[0]===20 && v[1]>=19) || (v[0]>=22 && (v[0]>22 || v[1]>=13)) ? 0 : 1)" >nul 2>nul
if errorlevel 1 (
  echo ERROR: Node.js 20.19+ or 22.13+ is required.
  echo Close this window, open a new Command Prompt, and run INSTALL.bat again.
  goto fail
)

if not exist ".env" (
  if not exist ".env.example" (
    echo ERROR: .env.example is missing.
    goto fail
  )
  copy /Y ".env.example" ".env" >nul
  if errorlevel 1 goto fail
  echo Created .env from .env.example. Add your OpenRouter key before AI judging.
)

echo.
echo [1/3] Installing verified Python and dashboard dependencies...
call scripts\setup-windows.bat
if errorlevel 1 goto fail

echo.
echo [2/3] Checking FFmpeg and FFprobe...
where ffmpeg >nul 2>nul
if errorlevel 1 (
  if not exist "tools\ffmpeg\ffmpeg.exe" (
    echo ERROR: FFmpeg was installed or expected, but is not visible on PATH.
    echo Open a new Command Prompt and run INSTALL.bat again.
    goto fail
  )
)
where ffprobe >nul 2>nul
if errorlevel 1 (
  if not exist "tools\ffmpeg\ffprobe.exe" (
    echo ERROR: FFprobe was installed or expected, but is not visible on PATH.
    echo Open a new Command Prompt and run INSTALL.bat again.
    goto fail
  )
)
echo PASS: FFmpeg and FFprobe are available.

echo.
echo [3/3] Installing Remotion and running its render smoke test...
call scripts\setup-remotion.bat
if errorlevel 1 goto fail

echo.
echo ============================================================
echo INSTALLATION COMPLETE
echo ============================================================
echo Next steps:
echo 1. Edit .env and set GCF_OPENROUTER_API_KEY=your_key
echo 2. Set GCF_RENDER_ENGINE=remotion in .env
echo 3. Double-click START.bat to launch the app.
echo 4. Double-click STOP.bat to stop it safely.
echo.
echo Dashboard: http://127.0.0.1:8765
echo YouTube publishing remains disabled by default.
pause
exit /b 0

:restart_shell
echo.
echo One or more prerequisites were just installed, but Windows has not
echo refreshed PATH in this process. Close this window and run INSTALL.bat
echo again from a new Command Prompt.
goto fail

:prereq_error
echo ERROR: Required software is missing and winget is unavailable.
echo Install Python 3.11/3.12, Node.js 20.19+/22.13+, and FFmpeg manually.
goto fail

:fail
echo.
echo Installation did not complete. Read the error above and fix that item.
pause
exit /b 1
