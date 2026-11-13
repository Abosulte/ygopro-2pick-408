@echo off
chcp 936 >nul
cd /d "%~dp0..\.."
echo ==== 从 D:/MDPro3/Deck/轮抽combo 生成 combo.lua ====
python "%~dp0make_combo.py"
if errorlevel 1 goto :err
echo.
pause
exit /b 0
:err
echo.
echo 出错了，看上面的报错信息。
pause
