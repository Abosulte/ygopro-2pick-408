@echo off
chcp 936 >nul
cd /d "%~dp0..\.."
echo ==== [1/2] 重建卡池 408.cdb ====
python "%~dp0make_draft_pool.py"
if errorlevel 1 goto :err
echo.
echo ==== [2/2] 重新生成仪式配对 ritual.lua ====
python "%~dp0make_ritual.py"
if errorlevel 1 goto :err
echo.
echo 完成：2pick\408.cdb、2pick\ritual.lua
pause
exit /b 0
:err
echo.
echo 出错了，看上面的报错信息。
pause
