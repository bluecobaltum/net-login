@echo off
chcp 65001 >nul
title 校园网自动登录 - 安装
cd /d "%~dp0"

echo [1/2] 安装开机自启(任务计划程序)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows.ps1"
if errorlevel 1 (
    echo.
    echo 安装失败: 请先安装 Python 3, 然后重新双击本文件。
    pause
    exit /b 1
)

echo.
echo [2/2] 打开账号密码设置窗口...
set "GUI=%~dp0windows-setup.pyw"

where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 "%GUI%"
    goto :end
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%GUI%"
    goto :end
)
python "%GUI%"

:end
