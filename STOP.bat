@echo off
setlocal EnableExtensions
cd /d "%~dp0"
call scripts\stop.bat
exit /b %ERRORLEVEL%
