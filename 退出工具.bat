@echo off
cd /d "%~dp0"
title 阅见阅读器-退出
if not exist "_port" (
  echo 服务没有在运行。
  timeout /t 2 >nul
  exit /b
)
set PYW=C:\Users\Z-WD\AppData\Local\Programs\Python\Python314\pythonw.exe
if not exist "%PYW%" set PYW=pythonw.exe
echo 正在关闭阅见阅读器...
"%PYW%" "%~dp0tool.py" --quit
timeout /t 2 >nul
