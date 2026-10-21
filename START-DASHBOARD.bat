@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m bounty_hunter.desktop
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python 3.9 or newer is required to run Bounty Hunter.
    echo You can also download the Windows .exe from GitHub Actions artifacts.
    pause
    exit /b 1
  )
  python -m bounty_hunter.desktop
)
if errorlevel 1 (
  echo.
  echo Dashboard could not start. Review the error shown above.
  pause
  exit /b 1
)
