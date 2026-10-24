@echo off
title Trading Lab
cd /d "%~dp0"
py tools\start_app.py || pause
