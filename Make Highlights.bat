@echo off
title Smart Highlights
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" engine\make_reel.py %*
) else (
  py -3.12 engine\make_reel.py %*
)
echo.
pause
