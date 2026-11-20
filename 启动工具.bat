@echo off
cd /d "%~dp0"
title ÔÄ¼ûÔÄ¶ÁÆ÷-Æô¶¯Æ÷
set PYW=C:\Users\Z-WD\AppData\Local\Programs\Python\Python314\pythonw.exe
if not exist "%PYW%" set PYW=pythonw.exe
start "ÔÄ¼ûÔÄ¶ÁÆ÷" "%PYW%" "%~dp0tool.py" --silent
