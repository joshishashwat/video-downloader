@echo off
rem Builds a standalone "dist\Video Downloader\Video Downloader.exe" (no Python needed to run it).
cd /d "%~dp0"
".venv\Scripts\python" -m pip install --upgrade pyinstaller || goto :eof
".venv\Scripts\pyinstaller" --noconfirm --windowed --name "Video Downloader" ^
    --icon vdl\assets\icon.ico ^
    --add-data "vdl\assets;vdl\assets" ^
    --collect-data customtkinter ^
    --collect-binaries imageio_ffmpeg ^
    main.pyw
echo.
echo Done: dist\Video Downloader\Video Downloader.exe
pause
