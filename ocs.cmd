@echo off
rem ocs launcher for Windows (cmd.exe)
setlocal
set "DIR=%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
  python "%DIR%ocs.py" %*
) else (
  py -3 "%DIR%ocs.py" %*
)
