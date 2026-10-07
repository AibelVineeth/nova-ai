@echo off
REM build.bat - runs the full Nova packaging pipeline.
REM Place this in the same folder as nova.py, nova.spec, installer.iss,
REM nova_icon.ico, yolov8n.pt, and kochi_intelligence_map.html.
REM
REM Optional, for phone calls over ADB without needing adb pre-installed
REM on the target PC: download "SDK Platform-Tools for Windows" from
REM https://developer.android.com/tools/releases/platform-tools , unzip it,
REM and put the resulting "platform-tools" folder in this same directory.
REM nova.spec will bundle it automatically if it's present.

echo ============================================
echo  Step 1: Installing PyInstaller (if needed)
echo ============================================
python -m pip install pyinstaller

echo.
echo ============================================
echo  Step 2: Building Nova.exe with PyInstaller
echo ============================================
echo This will take a while the first time - torch/mediapipe are large.
python -m PyInstaller nova.spec --noconfirm

if not exist "dist\Nova\Nova.exe" (
    echo.
    echo *** PyInstaller build failed - check the errors above. ***
    echo *** Common fix: a missing hidden import. Paste the error   ***
    echo *** back and it can be added to nova.spec.                 ***
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Step 3: Building the installer with Inno Setup
echo ============================================
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    where ISCC.exe >nul 2>nul
    if not errorlevel 1 (set "ISCC=ISCC.exe")
)
if /i not "%ISCC%"=="ISCC.exe" if not exist "%ISCC%" (
    echo.
    echo *** Inno Setup's ISCC.exe was not found.                     ***
    echo *** Install Inno Setup from https://jrsoftware.org/isdl.php  ***
    echo *** then re-run this script, or open installer.iss in the    ***
    echo *** Inno Setup Compiler and click Compile.                   ***
    pause
    exit /b 1
)

"%ISCC%" installer.iss
if errorlevel 1 (
    echo *** Inno Setup compile failed - see errors above. ***
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Done! Your installer is in:
echo  installer_output\NovaSetup.exe
echo ============================================
pause
