@echo off
rem Sites change often; updating yt-dlp fixes most "can't download" problems.
cd /d "%~dp0"
".venv\Scripts\python" -m pip install --upgrade yt-dlp
pause
