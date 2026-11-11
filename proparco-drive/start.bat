@echo off
rem Proparco email assistant - Windows launcher.
rem Usage:  start.bat                      (menu)
rem         start.bat generate.py --list   (run one script directly)
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
rem Local model servers must never go through a proxy.
set NO_PROXY=127.0.0.1,localhost

set "PY=%~dp0python\win\python.exe"
if not exist "%PY%" (
  echo Portable Python is missing: %PY%
  echo Rebuild the drive with build_package.py.
  pause
  exit /b 1
)

if "%~1"=="" (
  "%PY%" scripts\menu.py
) else (
  "%PY%" scripts\%*
)
endlocal