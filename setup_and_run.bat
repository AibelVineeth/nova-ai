@echo off
REM setup_and_run.bat — double-click to install Nova deps and launch the app.
REM Do NOT run nova.py from IDLE — use this script or: python setup_nova.py

cd /d "%~dp0"

echo.
echo  ========================================
echo   Nova — automatic setup ^& launch
echo  ========================================
echo.
echo  Tip: Do not open nova.py in IDLE (causes SpeechRecognition errors).
echo  Ollama (optional AI): install from https://ollama.com
echo         then run:  ollama pull llama3.2-vision
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
    echo  Unzip Nova-Bundle.zip fully first.
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
    echo  If you saw SpeechRecognition / pygame errors, run:
    echo    python -m pip install SpeechRecognition pygame edge-tts Pillow pystray psutil PyAudio
    echo  then run this script again.
    pause
)

exit /b %EXITCODE%
