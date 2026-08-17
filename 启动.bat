@echo off
rem ============================================
rem  Trainer Hub 启动器
rem  首次运行自动创建虚拟环境并安装依赖
rem  之后无窗口直接启动
rem ============================================
setlocal

set "ROOT=%~dp0"
cd /d "%ROOT%"

set "VENV=%ROOT%.venv"
set "PYW=%VENV%\Scripts\pythonw.exe"
set "PY=%VENV%\Scripts\python.exe"
set "MAIN=%ROOT%main.py"

if not exist "%PYW%" (
    echo [1/3] 正在创建虚拟环境...
    where python >nul 2>nul
    if errorlevel 1 (
        echo.
        echo [错误] 未找到 Python，请先安装 Python 3.10 并勾选 Add to PATH
        pause
        exit /b 1
    )
    python -m venv "%VENV%"
    if errorlevel 1 (
        echo [错误] 虚拟环境创建失败
        pause
        exit /b 1
    )
    echo [2/3] 正在安装依赖，请耐心等待...
    "%PY%" -m pip install --disable-pip-version-check -r "%ROOT%requirements.txt"
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败，请检查网络后重试
        pause
        exit /b 1
    )
)

echo [3/3] 正在启动 Trainer Hub...
start "" "%PYW%" "%MAIN%"
endlocal
