@echo off
chcp 936 >nul
cd /d "%~dp0"
title 一键安装朗读组件

echo ============================================
echo   阅见阅读器 · 一键安装朗读组件
echo   只装朗读需要的组件（edge-tts），不会动别的
echo   已装过的话会自动跳过，重复双击没有副作用
echo ============================================
echo.

rem ---- 找 Python：优先 py 启动器，退而求 python ----
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py"
if not defined PYEXE (
  where python >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
  echo [未通过] 没检测到 Python。
  echo.
  echo 请先安装 Python：https://www.python.org/downloads/
  echo 安装时务必勾选 "Add python.exe to PATH"，装完再双击本脚本。
  echo.
  pause
  exit /b 1
)

echo [1/3] 检测到 Python（%PYEXE%），核对版本：
%PYEXE% --version || goto :fail
echo.

echo [2/3] 检查朗读组件是否已装（需要 7.3.1 及以上）：
%PYEXE% -m pip show edge-tts >nul 2>nul
if %errorlevel%==0 (
  echo   已安装，跳过安装步骤。
  goto :check_ver
)
echo   未安装，开始安装（国内走清华镜像，可能要几十秒）：
%PYEXE% -m pip install "edge-tts==7.3.1" -i https://pypi.tuna.tsinghua.edu.cn/simple || goto :retry_official
goto :check_ver

:retry_official
echo   镜像源失败，换官方源再试一次：
%PYEXE% -m pip install "edge-tts==7.3.1" || goto :fail
goto :check_ver

:check_ver
%PYEXE% -c "from importlib.metadata import version; sys_ok=__import__('sys').exit(0 if tuple(map(int,version('edge-tts').split('.')[:2]))>=(7,3) else 1)" >nul 2>nul
if %errorlevel%==0 (
  echo   版本达标（7.3.1 及以上）。
) else (
  echo   已装但版本偏老（微软 2026-10-13 改了鉴权，老版本朗读会 403），升级中：
  %PYEXE% -m pip install -U "edge-tts==7.3.1" -i https://pypi.tuna.tsinghua.edu.cn/simple || %PYEXE% -m pip install -U "edge-tts==7.3.1" || goto :fail
)
echo.

echo [3/3] 完成校验：
%PYEXE% -c "from importlib.metadata import version; print('   朗读组件就绪：', version('edge-tts'))" || goto :fail
echo.
echo ============================================
echo   [完成] 朗读组件装好了。
echo   双击「启动工具.bat」就能用，朗读功能直接可用。
echo ============================================
echo.
pause
exit /b 0

:fail
echo.
echo [未通过] 安装没成功，常见原因：
echo   1. 没联网或网络不稳 —— 重试一次本脚本
echo   2. 公司/学校电脑限制装包 —— 找管理员开放 pip
echo   3. 多个 Python 混装 —— 命令行里跑 py -m pip install "edge-tts==7.3.1" 手动装
echo.
echo 说明：不装朗读组件也能用阅读器（读、翻页、搜索、进度都在），
echo       只是「朗读本章」用不了。
echo.
pause
exit /b 1
