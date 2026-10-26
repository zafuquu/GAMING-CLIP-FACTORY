@echo off
setlocal
cd /d "%~dp0..\frontend"
if not exist "node_modules\.bin\vite.cmd" (echo Run scripts\setup-windows.bat first & pause & exit /b 1)
call npm run dev
pause
