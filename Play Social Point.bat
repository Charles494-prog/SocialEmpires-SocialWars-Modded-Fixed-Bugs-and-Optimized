@echo off
title Social Point Launcher
cd /d "%~dp0"
py "launcher\launcher.py"
if errorlevel 1 pause
