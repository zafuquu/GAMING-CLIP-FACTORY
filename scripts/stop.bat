@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
title Gaming Clip Factory AI - Stop

if not exist ".venv\Scripts\python.exe" (
  echo No application virtual environment exists at this project path.
  exit /b 0
)

powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\stop-backend.ps1"
if errorlevel 1 (
  echo Could not stop the backend automatically. Inspect data\logs\backend.pid and process ownership.
  exit /b 1
)
exit /b 0
