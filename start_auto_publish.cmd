@echo off
setlocal
set "PYTHONUTF8=1"
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0auto_publish.py" --watch
    goto finished
)
py -3 "%~dp0auto_publish.py" --watch
:finished
if errorlevel 1 pause
