@echo off
setlocal
set "PYTHONUTF8=1"
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0update_app.py" --target "%~dp0." --gui
    goto finished
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0update_app.py" --target "%~dp0." --gui
    goto finished
)
python "%~dp0update_app.py" --target "%~dp0." --gui
:finished
if errorlevel 1 pause
