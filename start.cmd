@echo off
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
set "exitCode=%errorlevel%"
exit /b %exitCode%
