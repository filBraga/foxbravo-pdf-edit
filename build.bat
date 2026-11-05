@echo off
setlocal
cd /d "%~dp0"
title Building Foxbravo PDF Edit
echo.
echo === Foxbravo PDF Edit - build ===
echo.

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python was not found.
    echo Install Python 3.10 or newer from https://www.python.org/downloads/
    echo During setup, tick "Add python.exe to PATH", then run this file again.
    pause
    exit /b 1
)

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/
    pause
    exit /b 1
)
%PY% -c "import tkinter" 2>nul
if errorlevel 1 (
    echo Your Python is missing Tkinter. Run the Python installer again, choose Modify,
    echo and turn on "tcl/tk and IDLE".
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating a private Python environment in the .venv folder...
    %PY% -m venv .venv || goto :fail
)

echo Installing what the app needs. The first time takes a minute or two...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller || goto :fail

echo.
echo Building the .exe...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name "FoxbravoPDFEdit" "foxbravo_pdf_edit.py" || goto :fail

copy /y "dist\FoxbravoPDFEdit.exe" "%~dp0" >nul || goto :fail

echo.
echo Done! Your app is here:
echo   %~dp0FoxbravoPDFEdit.exe
echo.
pause
exit /b 0

:fail
echo.
echo The build failed. Scroll up to see the error message.
pause
exit /b 1
