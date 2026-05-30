@echo off
REM Windows one-click build script. Requires Python 3.8+ in PATH.
REM
REM Produces: dist\TekkenRevOnline\TekkenRevOnline.exe

cd /d "%~dp0"
python build.py
pause
