@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   SYSU Charge Watch - server for phone access
echo ============================================================
echo.
echo   On PC   : http://localhost:8787/
echo   On phone: http://YOUR-LAN-IP:8787/   ^(same WiFi required^)
echo.
echo   NOTE: the LAN IP varies by network. Run "ipconfig",
echo         find "WLAN" -^> "IPv4 Address", use that address.
echo         Phone and PC must be on the SAME WiFi / LAN.
echo.
echo   Keep this window open. Press Ctrl+C to stop the server.
echo ============================================================
echo.
python server.py --host 0.0.0.0 --port 8787
pause
