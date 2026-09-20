@echo off
rem ---------------------------------------------------------------
rem  YouTube Voice App launcher (double-click me)
rem  Shows the console with logs; close the window or press Ctrl+C
rem  to stop, or use the tray icon -> Quit.
rem ---------------------------------------------------------------
title YouTube Voice App
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [voiceyt] python venv not found at .venv - run the setup first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m voiceyt
if errorlevel 1 (
    echo.
    echo [voiceyt] exited with an error - read the messages above.
    pause
)
