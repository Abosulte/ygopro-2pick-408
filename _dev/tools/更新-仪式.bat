@echo off
chcp 936 >nul
cd /d "%~dp0..\.."
echo ==== 生成仪式配对表 ritual.lua ====
python "%~dp0make_ritual.py"
if errorlevel 1 goto :err
echo.
pause
exit /b 0
:err
echo.
echo 出错了，看上面的报错信息。
pause
