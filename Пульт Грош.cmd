@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Не найден Python в .venv. Сначала установите зависимости проекта.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m scripts.dashboard
if errorlevel 1 pause
