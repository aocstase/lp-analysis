@echo off
setlocal
set "PYTHONUTF8=1"
if defined VIRTUAL_ENV if exist "%VIRTUAL_ENV%\Scripts\python.exe" (
    "%VIRTUAL_ENV%\Scripts\python.exe" "%~dp0run_gui.py" %*
    exit /b
)
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0run_gui.py" %*
    exit /b
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0run_gui.py" %*
    exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
    python "%~dp0run_gui.py" %*
    exit /b
)
echo [ERROR] Install Python 3.10 or later, then follow README.md. 1>&2
exit /b 1
