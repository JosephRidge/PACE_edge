@echo off
rem Review and remove duplicate emails in MY exported mailbox (exported copies and index only; Outlook is never touched).
set "PROPARCO_DATA_DIR=%~dp0proparco-data\mailboxes\%USERNAME%"
if not exist "%PROPARCO_DATA_DIR%\acquisitions" (
  echo No exported emails for %USERNAME% in %PROPARCO_DATA_DIR%
  echo Run EXTRACT MY EMAILS.bat first.
  pause
  exit /b 1
)
call "%~dp0proparco-drive\start.bat" dedupe.py
pause
