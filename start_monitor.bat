@echo off
rem Starts the SSH flow monitor helper, which opens the monitor in its own window.
rem Closing the monitor window or the "Flow monitor bridge" window stops both.
cd /d "%~dp0"

rem "python" may be the Microsoft Store stub, so actually run it to check.
set PY=
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY (
  echo.
  echo   Python 3 is not installed on this PC, and Flow monitor needs it.
  echo.
  echo   1. Download it from  https://www.python.org/downloads/
  echo   2. In the installer, tick "Add python.exe to PATH" and keep "py launcher" ticked.
  echo   3. Run start_monitor.bat again.
  echo.
  pause
  exit /b 1
)

set HAVESSH=
where ssh >nul 2>nul && set HAVESSH=1
if exist "%SystemRoot%\System32\OpenSSH\ssh.exe" set HAVESSH=1
if not defined HAVESSH (
  echo.
  echo   The Windows SSH client is not installed, and Flow monitor needs it.
  echo.
  echo   Open Settings ^> System ^> Optional features ^> Add a feature,
  echo   install "OpenSSH Client", then run start_monitor.bat again.
  echo.
  pause
  exit /b 1
)
rem The helper opens the monitor window itself; closing either one stops both.
start "Flow monitor bridge" /min cmd /c "%PY% monitor_bridge.py --open || pause"
