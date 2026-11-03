@echo off
chcp 65001 >nul
cd /d "%~dp0"
title BabyGrowth Monitor Bot
set PY=python
where python >nul 2>nul || set PY=py
if not exist "venv\Scripts\activate.bat" (
    echo Menyiapkan lingkungan Python, tunggu sebentar...
    %PY% -m venv venv
)
call "venv\Scripts\activate.bat"
python -m pip install -q -r requirements.txt
python bot.py
echo.
echo Bot berhenti. Tekan tombol apa saja untuk menutup.
pause >nul
