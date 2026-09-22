@echo off
rem Every run after the first: just the dashboard, then start the game.
cd /d "D:/ai生成视频/jev模型/jev-spire-brain"
"C:/Users/Lenovo/AppData/Local/Programs/Python/Python312/python.exe" start.py %*
echo.
pause
