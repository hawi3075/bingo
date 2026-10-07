@echo off
cd /d "%~dp0"
python --version >nul 2>&1 || (echo Python is not installed. Install it from python.org and tick "Add Python to PATH". & pause & exit)
pip install -q -r requirements.txt
python server\server.py
pause
