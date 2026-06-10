@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo.
echo ╔══════════════════════════════════════════════╗
echo ║   微信朋友圈自动化 — 一键安装脚本           ║
echo ╚══════════════════════════════════════════════╝
echo.

set "PROJECT_DIR=%~dp0"
cd /d "%PROJECT_DIR%"

:: ── 1. 检查 Python ──
echo [1/6] 检查 Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo   ❌ Python 未安装，请先安装 Python 3.10+
    echo   https://www.python.org/downloads/
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do echo   ✅ Python %%v

:: ── 2. 创建虚拟环境 ──
echo [2/6] 创建虚拟环境...
if not exist "venv" (
    python -m venv venv
    echo   ✅ 虚拟环境已创建
) else (
    echo   ✅ 虚拟环境已存在
)

:: ── 3. 安装 Python 依赖 ──
echo [3/6] 安装 Python 依赖...
call venv\Scripts\activate.bat
pip install -r requirements.txt -q
if %errorlevel% neq 0 (
    echo   ⚠️ 部分依赖安装可能失败，尝试继续...
)
echo   ✅ Python 依赖安装完成

:: ── 4. 创建必要目录 ──
echo [4/6] 创建目录结构...
if not exist "templates\icons" mkdir templates\icons
if not exist "logs" mkdir logs
if not exist "debug_screenshots" mkdir debug_screenshots
echo   ✅ 目录结构已就绪

:: ── 5. 编译 C# UIA 微服务 ──
echo [5/6] 编译 C# UIA 微服务...
dotnet --version >nul 2>&1
if %errorlevel% neq 0 (
    echo   ⚠️ .NET SDK 未安装，C# UIA 微服务将不可用
    echo   下载: https://dotnet.microsoft.com/download
    echo   系统将自动回退到纯 Python OCR 模式
) else (
    cd src\cs_uia_service
    dotnet restore -q 2>nul
    dotnet publish -c Release -o publish -q 2>nul
    if %errorlevel% equ 0 (
        echo   ✅ C# UIA 微服务编译成功
    ) else (
        echo   ⚠️ C# 编译失败，系统将回退到纯 Python 模式
    )
    cd ..\..
)

:: ── 6. 快速验证 ──
echo [6/6] 快速验证...
call venv\Scripts\activate.bat
python -c "from src.core import EventBus; from src.locator import OCRLocator; print('   ✅ 核心模块加载正常')" 2>nul
if %errorlevel% neq 0 (
    echo   ⚠️ 核心模块验证失败，请检查错误信息
)

echo.
echo ╔══════════════════════════════════════════════╗
echo ║  ✅ 安装完成！                              ║
echo ╚══════════════════════════════════════════════╝
echo.
echo 快速开始:
echo   venv\Scripts\activate
echo   python main.py --interactive
echo   python main.py --text "今天天气真好"
echo.
echo 启动 API Server（前端/OpenClaw 需要）:
echo   python -m uvicorn src.api.server:app --host 127.0.0.1 --port 18080
echo.
pause
