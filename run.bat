@echo off
setlocal
cd /d "%~dp0"
title RTSP Portable Launcher

set "ENV_DIR=%~dp0rtsp_env"
if not exist "%ENV_DIR%\python.exe" (
  echo [ERROR] Portable environment not found at "%ENV_DIR%"
  echo Extract the full portable ZIP and run this file again.
  pause
  exit /b 1
)

set "GST_ROOT=%~dp0gstreamer\1.0\msvc_x86_64"
if not exist "%GST_ROOT%" set "GST_ROOT=%~dp0gstreamer"
if exist "%GST_ROOT%\bin" set "PATH=%GST_ROOT%\bin;%PATH%"
if exist "%GST_ROOT%\lib\site-packages" set "PYTHONPATH=%GST_ROOT%\lib\site-packages;%PYTHONPATH%"

if not exist "%ENV_DIR%\_unpack_done.flag" (
  echo Preparing portable environment ^(first run may take 1-3 minutes^)...
  if exist "%ENV_DIR%\Scripts\conda-unpack.exe" (
    "%ENV_DIR%\Scripts\conda-unpack.exe"
  ) else (
    if exist "%ENV_DIR%\Scripts\conda-unpack-script.py" (
      "%ENV_DIR%\python.exe" "%ENV_DIR%\Scripts\conda-unpack-script.py"
    )
  )

  if errorlevel 1 (
    echo.
    echo [ERROR] conda-unpack failed.
    pause
    exit /b 1
  )

  > "%ENV_DIR%\_unpack_done.flag" echo ok
)

"%ENV_DIR%\python.exe" -c "import gi, PyQt5" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Dependency check failed. Showing detailed Python error...
  "%ENV_DIR%\python.exe" app.py
  pause
  exit /b 1
)

echo Starting RTSP client GUI...
if exist "%ENV_DIR%\pythonw.exe" (
  start "" "%ENV_DIR%\pythonw.exe" app.py
) else (
  start "" "%ENV_DIR%\python.exe" app.py
)

echo Launched. You can close this window.

endlocal
