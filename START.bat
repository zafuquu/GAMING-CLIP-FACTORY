@echo off
setlocal EnableExtensions
cd /d "%~dp0"
call scripts\start.bat
exit /b %ERRORLEVEL%
