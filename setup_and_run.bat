@echo off
REM setup_and_run.bat — double-click to install Nova deps and launch the app.
REM Put this in the same folder as setup_nova.py and nova.py.

cd /d "%~dp0"

echo.
echo  ========================================
echo   Nova — automatic setup ^& launch
echo  ========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo  Python was not found on PATH.
    echo  Install Python 3.10+ from https://www.python.org/downloads/
    echo  and check "Add Python to PATH" during setup.
    echo.
    pause
    exit /b 1
)

if not exist "nova.py" (
    echo  nova.py is missing in this folder.
    echo  Download it from the Nova site and place it next to this script.
    echo.
    pause
    exit /b 1
)

if not exist "setup_nova.py" (
    echo  setup_nova.py is missing in this folder.
    echo.
    pause
    exit /b 1
)

python setup_nova.py %*
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
    echo.
    echo  Setup or launch exited with code %EXITCODE%.
    pause
)

exit /b %EXITCODE%
