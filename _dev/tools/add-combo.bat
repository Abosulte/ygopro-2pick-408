@echo off
cd /d "%~dp0..\.."
chcp 65001 >nul
python "%~dp0add_combo_from_ypk.py"
if errorlevel 1 pause
