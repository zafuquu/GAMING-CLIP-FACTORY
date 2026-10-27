@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Run scripts\setup-windows.bat first.
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\install-ml-dependencies.ps1"
if errorlevel 1 (
  echo Optional ML install failed. Review the error above.
  pause
  exit /b 1
)
echo Optional transcription dependencies installed.
echo Configure GCF_ENABLE_TRANSCRIPTION=true and a local model path or explicit download permission in .env.
pause
