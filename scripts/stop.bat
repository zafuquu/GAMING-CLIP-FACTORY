@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
title Gaming Clip Factory AI - Stop

if not exist ".venv\Scripts\python.exe" (
  echo No application virtual environment exists at this project path.
  exit /b 0
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=(Get-Location).Path; $python=Join-Path $root '.venv\Scripts\python.exe'; $logDir=Join-Path $root 'data\logs'; $pidFile=Join-Path $logDir 'backend.pid'; $stopped=$false; if(Test-Path $pidFile){$raw=(Get-Content -Raw $pidFile).Trim(); $id=0; if([int]::TryParse($raw,[ref]$id)){$p=Get-CimInstance Win32_Process -Filter ('ProcessId = '+$id) -ErrorAction SilentlyContinue; if($p -and $p.ExecutablePath -eq $python -and $p.CommandLine -match '(^|\s)-m\s+app(\s|$)'){Stop-Process -Id $id -Force -ErrorAction SilentlyContinue; $stopped=$true}}; Remove-Item -Force $pidFile -ErrorAction SilentlyContinue}; $appProcesses=Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath -eq $python -and $_.CommandLine -match '(^|\s)-m\s+app(\s|$)'}; foreach($p in $appProcesses){Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue; $stopped=$true}; if($stopped){Write-Output 'Gaming Clip Factory AI backend stopped.'}else{Write-Output 'No running Gaming Clip Factory AI backend was found for this project.'}"
if errorlevel 1 (
  echo Could not stop the backend automatically. Inspect data\logs\backend.pid and process ownership.
  exit /b 1
)
exit /b 0
