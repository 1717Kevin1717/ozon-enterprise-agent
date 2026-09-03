@echo off
setlocal EnableExtensions
cd /d "%~dp0"

py -3.14 -c "import sys; print(sys.version)" >nul 2>&1
if errorlevel 1 (
  echo.
  echo Python 3.14 was not found. The backend has not started.
  echo.
  echo This package is configured for the Python 3.14 already detected on your computer.
  echo Please contact the project owner if Python 3.14 was removed.
  echo After installation finishes, close this window and run this file again.
  echo.
  pause
  exit /b 1
)

if not exist ".venv-py314\Scripts\python.exe" (
  echo Creating Python 3.14 virtual environment...
  py -3.14 -m venv .venv-py314
  if errorlevel 1 (
    echo Virtual environment creation failed. The backend has not started.
    pause
    exit /b 1
  )
)

echo Installing or checking required packages...
.venv-py314\Scripts\python.exe -c "import sys; print('Using Python', sys.version)"
.venv-py314\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :install_failed
.venv-py314\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :install_failed

if not exist ".env" copy .env.example .env >nul
echo.
echo Starting ZMT Ozon Enterprise Backend at http://127.0.0.1:8000
start "" http://127.0.0.1:8000
.venv-py314\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
echo.
echo The backend has stopped.
pause
exit /b 0

:install_failed
echo.
echo Package installation failed. Please check the internet connection and run this file again.
echo The backend has not started.
pause
exit /b 1
