@echo off
chcp 65001 >nul
cd /d "%~dp0"
".\.venv\Scripts\python.exe" webapp.py --open
echo.
echo 服务已退出。按任意键关闭窗口。
pause >nul
