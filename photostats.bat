@echo off
chcp 65001 >nul
cd /d "%~dp0"
title photostats

rem Ищем Python 3.10 или новее. Сначала py -3: он не натыкается на заглушку
rem python.exe из Магазина Windows, которая вместо запуска открывает магазин.
set "PY="
py -3 -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
  python -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY goto :nopython

%PY% launcher.py
if errorlevel 1 pause
exit /b

:nopython
echo.
echo   Python 3.10 or newer was not found.
echo.
echo   Download it from  https://www.python.org/downloads/windows/
echo   During setup tick "Add python.exe to PATH" and keep "tcl/tk and IDLE".
echo.
echo   Then run this file again.
echo.
pause
