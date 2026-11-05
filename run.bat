@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
title Warehouse Sort AI
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8765'"
python app.py
if errorlevel 1 (
  echo.
  echo [ERROR] app.py exited. Read the messages above.
  pause
)
exit /b 0
