@echo off
cd /d "%~dp0"
set "PYTHONPATH=%CD%\src;%CD%;%PYTHONPATH%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw scripts\one_click_launcher.py
) else (
    start "" python scripts\one_click_launcher.py
)
