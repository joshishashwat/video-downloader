"""Entry point. Runs without a console window (pythonw / packaged exe)."""
import os
import sys

# pythonw and windowed exes have no stdout/stderr; some libraries still write to them.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

try:  # crisp text on high-DPI screens
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VideoDownloader.App")
except Exception:
    pass

from vdl.config import Settings, find_ffmpeg
from vdl.ui import App


def main():
    settings = Settings.load()
    app = App(settings, find_ffmpeg())
    app.mainloop()


if __name__ == "__main__":
    main()
