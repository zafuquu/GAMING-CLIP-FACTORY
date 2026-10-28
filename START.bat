@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Gaming Clip Factory AI - START
echo.
echo ============================================================
echo       GAMING CLIP FACTORY AI - START
echo ============================================================
echo Project: %CD%
echo.
if not exist "scripts\start.bat" (
  echo ERROR: scripts\start.bat is missing.
  echo Make sure you extracted the complete repository.
  goto finish_error
)
call "scripts\start.bat"
set "START_RESULT=%ERRORLEVEL%"
echo.
if "%START_RESULT%"=="0" (
  echo START launcher returned success.
) else (
  echo START launcher returned error code %START_RESULT%.
)
echo.
echo If startup failed, copy the error text above and send it for diagnosis.
echo This window is intentionally kept open.
pause
exit /b %START_RESULT%

:finish_error
set "START_RESULT=1"
echo.
pause
exit /b 1
