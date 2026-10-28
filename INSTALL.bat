@echo off
setlocal EnableExtensions
cd /d "%~dp0"
call scripts\install-all.bat
exit /b %ERRORLEVEL%
