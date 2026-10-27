@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (echo Run scripts\setup-windows.bat first. & pause & exit /b 1)
echo Installing optional local speech-recognition dependencies.
echo Model weights are separate and are not downloaded by this script.
".venv\Scripts\python.exe" -m pip install --require-hashes -r backend\requirements-ml-lock.txt
if errorlevel 1 (echo Install failed. Review output above. & pause & exit /b 1)
echo Optional transcription dependencies installed.
echo Configure GCF_ENABLE_TRANSCRIPTION=true and a local model path or explicit download permission in .env.
pause
