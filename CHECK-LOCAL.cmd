@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  python sv_iptv.py --local
) else (
  py -3 sv_iptv.py --local
)
echo.
echo Local check finished. Results are in the local-check folder.
pause
