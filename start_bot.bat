@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Music Finder Bot
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PIP_PROGRESS_BAR=off

echo ========================================
echo   Advanced Telegram Music Bot
echo ========================================
echo.

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo [ERROR] .venv not found.
  echo Creating venv and installing requirements...
  py -3.12 -m venv "%~dp0.venv"
  if errorlevel 1 (
    echo [ERROR] Could not create venv. Install Python 3.12 and retry.
    pause
    exit /b 1
  )
  "%~dp0.venv\Scripts\python.exe" -m pip install --progress-bar off -U pip
  "%~dp0.venv\Scripts\python.exe" -m pip install --progress-bar off -r "%~dp0requirements.txt"
  if errorlevel 1 (
    echo [ERROR] pip install failed
    pause
    exit /b 1
  )
)

if /I "%~1"=="--update" (
  echo Updating packages...
  "%~dp0.venv\Scripts\python.exe" -m pip install --progress-bar off -r "%~dp0requirements.txt"
  if errorlevel 1 (
    echo [ERROR] pip install failed
    pause
    exit /b 1
  )
  echo.
)

for /f "delims=" %%I in ('dir /s /b "%USERPROFILE%\Desktop\ffmpeg\ffmpeg.exe" 2^>nul') do (
  set "PATH=%%~dpI;%PATH%"
  goto ffmpeg_done
)
:ffmpeg_done

if not exist "%~dp0.env" (
  echo [ERROR] .env file missing. Copy .env.example to .env and set BOT_TOKEN.
  pause
  exit /b 1
)

echo Starting... Keep this window open.
echo.
"%~dp0.venv\Scripts\python.exe" "%~dp0bot.py"
echo.
pause