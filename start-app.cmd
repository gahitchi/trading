@echo off
title Trading Lab
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\launch_windows.ps1" %*
if errorlevel 1 pause
