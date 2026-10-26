@echo off
rem Start Home Control. Double-click, or run "run.bat --demo" to try it with pretend lights.
cd /d "%~dp0"
python -m home_control %*
if errorlevel 1 pause
