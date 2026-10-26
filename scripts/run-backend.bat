@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (echo Run scripts\setup-windows.bat first & pause & exit /b 1)
set "GCF_PROJECT_ROOT=%CD%"
cd backend
"..\.venv\Scripts\python.exe" -m app
pause
