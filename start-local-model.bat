@echo off
chcp 65001 >nul
echo ============================================
echo  Spark-X2.5-4B 本地模型服务（A770 GPU 加速）
echo ============================================
echo.

set SERVER=G:\llama.cpp-spark\build-vulkan\bin\Release\llama-server.exe
set MODEL=G:\models\Spark-X2.5-4B.gguf

if not exist "%SERVER%" (
    echo [错误] 找不到 llama-server.exe:
    echo   %SERVER%
    echo 请确认 G:\llama.cpp-spark 已编译 build-vulkan。
    pause
    exit /b 1
)

if not exist "%MODEL%" (
    echo [错误] 找不到模型文件:
    echo   %MODEL%
    pause
    exit /b 1
)

echo [启动] 加载模型: %MODEL%
echo [启动] 后端: Vulkan (Intel Arc A770)
echo [启动] 监听: http://127.0.0.1:8080
echo [启动] 思考链: 已关闭 (--reasoning off)
echo.
echo 关闭本窗口即停止服务。
echo ============================================
echo.

"%SERVER%" -m "%MODEL%" --host 127.0.0.1 --port 8080 --ctx-size 8192 --reasoning off -ngl 99 --device Vulkan0

echo.
echo [退出] 服务已停止。
pause
