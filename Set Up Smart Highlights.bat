@echo off
title Set up Smart Highlights
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Setting up. This takes a few minutes, and only happens once...
  py -3.12 -m venv .venv || goto failed
)
".venv\Scripts\python.exe" -m pip install -q -r server\requirements.txt || goto failed
".venv\Scripts\python.exe" server\install_app.py || goto failed
echo.
pause
exit /b 0
:failed
echo.
echo Setup didn't finish. It needs Python 3.12 and an internet connection.
pause
exit /b 1
