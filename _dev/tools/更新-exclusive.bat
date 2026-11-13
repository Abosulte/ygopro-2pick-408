@echo off
chcp 936 >nul
cd /d "%~dp0..\.."
echo ==== 生成 exclusive.lua（并入 exclusive.ydk）====
python "%~dp0make_exclusive.py" --ydk "D://MDPro3//Deck//轮抽服务器//exclusive.ydk"
if errorlevel 1 goto :err
echo.
pause
exit /b 0
:err
echo.
echo 出错了，看上面的报错信息。
pause
