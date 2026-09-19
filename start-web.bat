@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   网页界面已停用 —— 当前阶段只优化后端
echo ============================================================
echo.
echo 本项目已切到「只对后端进行优化测试」阶段，前端 UI 层整体冻结：
echo   webapp.py / llama_bridge.py / web/llama-ui
echo.
echo 请改用 CLI 消息平台：
echo     .\.venv\Scripts\python.exe main.py
echo.
echo 确实需要临时起一次网页界面（排查 UI 自身问题）：
echo     set FORGE_ENABLE_UI=1
echo     .\.venv\Scripts\python.exe webapp.py
echo.
echo 冻结范围与边界约定见 ui_frozen.py
echo ============================================================
echo.
echo 按任意键关闭窗口。
pause >nul
