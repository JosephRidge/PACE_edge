@echo off
rem ONE CLICK: export my mailbox and archives (read-only), then find duplicate emails and
rem attachments and open the review page. Outlook is never changed.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0proparco-drive\extract_mail.ps1"
set "PROPARCO_DATA_DIR=%~dp0proparco-data\mailboxes\%USERNAME%"
if not exist "%PROPARCO_DATA_DIR%\acquisitions" (
  echo.
  echo The export did not save any emails for %USERNAME%, so there is nothing to review.
  echo Check the messages above, then run this again.
  pause
  exit /b 1
)
echo.
echo ================================================================
echo   Now looking for duplicate emails and attachments.
echo   The review page opens in your browser. Close this window when done.
echo ================================================================
call "%~dp0proparco-drive\start.bat" dedupe.py --attachments
pause
