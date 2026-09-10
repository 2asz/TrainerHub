@echo off
rem 统一测试入口：双击或 cmd 运行，顺序跑完全部回归并汇总。
rem 用法：tests\run_all.bat  （自动定位项目根与 venv 解释器）
setlocal
cd /d "%~dp0.."
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo ===== th_lib_test =====
"%PY%" tests\th_lib_test.py
if errorlevel 1 goto :fail

echo ===== th_ui_test =====
"%PY%" tests\th_ui_test.py
if errorlevel 1 goto :fail

echo RUN_ALL_OK
exit /b 0

:fail
echo RUN_ALL_FAILED
exit /b 1
