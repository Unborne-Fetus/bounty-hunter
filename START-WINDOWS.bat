@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m bounty_hunter.cli %*
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python 3.9+ is required. Install Python, then run this launcher again.
    pause
    exit /b 1
  )
  python -m bounty_hunter.cli %*
)
if errorlevel 1 (
  echo.
  echo Scan failed. Check the error above.
  pause
  exit /b 1
)
echo.
echo Scan finished. See reports\opportunities.md
pause
