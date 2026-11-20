@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 阅见阅读器-退出
if not exist "_port" (
  echo 阅见阅读器当前没有在运行。
  timeout /t 2 >nul
  exit /b
)
where pythonw >nul 2>nul && (
  pythonw "%~dp0tool.py" --quit
  timeout /t 2 >nul
  exit /b
)
where pyw >nul 2>nul && (
  pyw -3 "%~dp0tool.py" --quit
  timeout /t 2 >nul
  exit /b
)
echo [阅见阅读器] 未检测到 Python，无法执行退出脚本。
echo 请安装 Python（https://www.python.org/downloads/），或直接关窗口即可。
pause
