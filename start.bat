@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title LichtFeld RunPod
echo.
echo LichtFeld RunPod — starting dashboard
echo.

if exist "%SystemRoot%\System32\OpenSSH\ssh.exe" set "PATH=%SystemRoot%\System32\OpenSSH;%PATH%"

if not exist "%~dp0.venv\Scripts\python.exe" (
    call "%~dp0setup.bat" --from-start
    if errorlevel 1 (
        echo.
        pause
        exit /b 1
    )
)

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo ERROR: .venv\Scripts\python.exe is missing after setup.
    pause
    exit /b 1
)

"%VENV_PY%" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/', timeout=1)" >nul 2>&1
if not errorlevel 1 (
    echo Dashboard is already running.
    echo Opening http://127.0.0.1:8765
    start "" "http://127.0.0.1:8765"
    timeout /t 3 /nobreak >nul
    exit /b 0
)

echo Opening http://127.0.0.1:8765
echo Leave this window open while you use the dashboard. Close it to stop the app.
echo.
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8765"

"%VENV_PY%" -m lichtfeld_runpod --ui
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo Dashboard exited with error code %RC%.
    pause
    exit /b %RC%
)
echo Dashboard stopped.
pause
exit /b 0
