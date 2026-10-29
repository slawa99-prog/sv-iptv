@echo off
cd /d "%~dp0"
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo Install ffmpeg and ffprobe and add them to PATH first.
  pause
  exit /b 1
)
where py >nul 2>nul
if errorlevel 1 (
  python sv_iptv.py
) else (
  py -3 sv_iptv.py
)
echo.
echo Results: status.json. The playlist is tv.m3u after a successful check.
pause
