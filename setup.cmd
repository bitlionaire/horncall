@echo off
rem Horncall setup: hooks up your AI tools, pairs the phone and sends a test order.
rem It copies Horncall to %LOCALAPPDATA%\Horncall\app - this folder can be deleted afterwards.
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
  echo Python 3.10 or newer is needed: https://www.python.org/downloads/
  echo During installation tick "Add python.exe to PATH", then run setup.cmd again.
  pause
  exit /b 1
)
%PY% plugin\hooks\approver_launcher.py setup
rem The wizard waits for Enter itself; pause only if it stopped with an error.
if errorlevel 1 pause
