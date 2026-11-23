@echo off
rem Starts Photo Importer. Double-click this file.
setlocal
cd /d "%~dp0"
set "PYEXE="

rem Find a Python that has the Pillow image library installed.
for %%C in (python py) do (
    if not defined PYEXE (
        for /f "usebackq delims=" %%E in (`%%C -c "import sys, PIL, tkinter; print(sys.executable)" 2^>nul`) do set "PYEXE=%%E"
    )
)

if not defined PYEXE (
    echo Photo Importer needs Python with the "Pillow" image library.
    echo.
    echo To install Pillow, run this command and then start Photo Importer again:
    echo     python -m pip install pillow
    echo.
    pause
    exit /b 1
)

rem pythonw.exe is the same Python without a console window.
set "PYW=%PYEXE:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW=%PYEXE%"
start "" "%PYW%" -m photo_importer
