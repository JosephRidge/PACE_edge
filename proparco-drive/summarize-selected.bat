@echo off
rem Summarise the emails selected in Outlook: one double-click.
rem 1. In classic Outlook, select one or more emails (Ctrl+click).
rem 2. Double-click this file. The deal page opens when it is done.
call "%~dp0start.bat" summarize_selected.py
echo.
pause