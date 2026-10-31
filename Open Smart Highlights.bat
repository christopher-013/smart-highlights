@echo off
title Smart Highlights
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" server\local_app.py
) else (
  py -3.12 server\local_app.py
)
echo.
pause
