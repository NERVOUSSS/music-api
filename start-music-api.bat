@echo off
cd /d "%~dp0"
if not exist ".ncm-qr-adapter\node_modules\NeteaseCloudMusicApi\app.js" (
    where node >nul 2>nul
    if errorlevel 1 (
        echo Node.js is required for NetEase QR login. Install Node.js LTS and run this script again.
        exit /b 1
    )
    call npm.cmd install --prefix .ncm-qr-adapter NeteaseCloudMusicApi@4.32.0
    if errorlevel 1 exit /b 1
)
if exist ".qishui-qr-bridge\package.json" (
    if not exist ".qishui-qr-bridge\node_modules\electron\dist\electron.exe" (
        where node >nul 2>nul
        if errorlevel 1 (
            echo Node.js is required for Qishui Music authorization bridge. Install Node.js LTS and run this script again.
            exit /b 1
        )
        call npm.cmd install --prefix .qishui-qr-bridge
        if errorlevel 1 exit /b 1
    )
)
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
)
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe server.py
