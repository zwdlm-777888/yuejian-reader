@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 阅见阅读器
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "阅见" pythonw "%~dp0tool.py" --silent
  exit /b
)
where pyw >nul 2>nul
if %errorlevel%==0 (
  start "阅见" pyw -3 "%~dp0tool.py" --silent
  exit /b
)
echo [阅见阅读器] 未检测到 Python，请先安装：
echo.
echo     https://www.python.org/downloads/
echo.
echo 安装时务必勾选 "Add python.exe to PATH"，装完再双击本脚本。
echo.
pause
