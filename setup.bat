@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "FROM_START=0"
if /i "%~1"=="--from-start" set "FROM_START=1"

title LichtFeld RunPod setup
echo.
echo LichtFeld RunPod — Windows setup
echo.

if not exist "%~dp0pyproject.toml" (
    echo ERROR: pyproject.toml not found.
    echo Run this script from the lichtfeld-studio-runpod-remote folder.
    goto :fail
)

set "PYTHON="
call :try_python py -3.13
if defined PYTHON goto :have_python
call :try_python py -3.12
if defined PYTHON goto :have_python
call :try_python py -3.11
if defined PYTHON goto :have_python
call :try_python py -3
if defined PYTHON goto :have_python
call :try_python python
if defined PYTHON goto :have_python
call :try_python python3
if defined PYTHON goto :have_python

echo ERROR: Python 3.11 or newer was not found.
echo Install it from https://www.python.org/downloads/windows/
echo and tick "Add python.exe to PATH".
goto :fail

:have_python
echo Using Python: %PYTHON%
%PYTHON% -c "import sys; print('  ' + sys.version.split()[0] + '  (' + sys.executable + ')')"
if errorlevel 1 goto :fail
echo.

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo Creating virtual environment in .venv ...
    %PYTHON% -m venv .venv
    if errorlevel 1 (
        echo ERROR: could not create .venv
        goto :fail
    )
    echo Upgrading pip ...
    "%VENV_PY%" -m pip install --upgrade pip
    if errorlevel 1 goto :fail
    echo.
)

echo Installing lichtfeld-runpod ...
"%VENV_PY%" -m pip install -e .
if errorlevel 1 (
    echo ERROR: pip install failed
    goto :fail
)
echo.

echo Writing config.yaml and .env if they are missing ...
"%VENV_PY%" -m lichtfeld_runpod --init
if errorlevel 1 goto :fail
echo.

call :prefer_openssh
call :check_tool ssh "Enable OpenSSH Client under Settings, Apps, Optional features."
call :check_tool scp "Enable OpenSSH Client under Settings, Apps, Optional features."
call :check_tool ssh-keygen "Enable OpenSSH Client under Settings, Apps, Optional features."
call :check_tool curl "curl.exe is included with Windows 10+."

echo.
echo Setup finished.
echo Put secrets in .env or enter them later under Settings in the dashboard.
echo Then run start.bat to open the dashboard.
echo.
if "%FROM_START%"=="0" pause
exit /b 0

:try_python
%* -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PYTHON=%*"
exit /b 0

:prefer_openssh
set "OPENSSH=%SystemRoot%\System32\OpenSSH"
if exist "%OPENSSH%\ssh.exe" set "PATH=%OPENSSH%;%PATH%"
exit /b 0

:check_tool
where %~1 >nul 2>&1
if not errorlevel 1 goto :tool_ok
echo WARNING: %~1 not found on PATH.
echo          %~2
exit /b 0
:tool_ok
echo Found %~1.
exit /b 0

:fail
echo.
echo Setup did not complete.
if "%FROM_START%"=="0" pause
exit /b 1
