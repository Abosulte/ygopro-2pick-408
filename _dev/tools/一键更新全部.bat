@echo off
chcp 936 >nul
cd /d "%~dp0..\.."
echo ==== [1/6] 重建 408.cdb（合并 轮抽服务器 + 轮抽combo 下所有 ydk）====
python "%~dp0make_draft_pool.py"
if errorlevel 1 goto :err
echo.
echo ==== [2/6] 重新生成 cardnames.txt ====
python "%~dp0make_cardnames.py"
if errorlevel 1 goto :err
echo.
echo ==== [3/6] 生成仪式配对 ritual.lua ====
python "%~dp0make_ritual.py"
if errorlevel 1 goto :err
echo.
echo ==== [4/6] 生成 combo.lua（轮抽combo 目录）====
python "%~dp0make_combo.py"
if errorlevel 1 goto :err
echo.
echo ==== [5/6] 生成 exclusive.lua（并入 exclusive.ydk）====
python "%~dp0make_exclusive.py" --ydk "D://MDPro3//Deck//轮抽服务器//exclusive.ydk"
if errorlevel 1 goto :err
echo.
echo ==== [6/6] 生成 pick.lua ====
python "%~dp0patch_pick.py"
if errorlevel 1 goto :err
echo.
echo ==== 模拟 300 局自检 ====
python "%~dp0sim_pick.py" -n 300
echo.
echo 全部生成完毕。把 2pick-408 上传到服务器后跑 push-rules.sh。
pause
exit /b 0
:err
echo.
echo 出错了，看上面的报错信息。
pause
