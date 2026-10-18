"""Settings persistence and environment helpers (paths, ffmpeg)."""
import json
import os
import shutil
from dataclasses import asdict, dataclass, fields
from pathlib import Path

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
SETTINGS_FILE = APP_DIR / "settings.json"
BIN_DIR = APP_DIR / "bin"


def default_download_dir() -> str:
    d = Path.home() / "Downloads" / "Video Downloader"
    return str(d)


@dataclass
class Settings:
    download_dir: str = ""
    max_concurrent: int = 2
    mode: str = "Video"              # "Video" | "Audio"
    video_quality: str = "Best"
    audio_format: str = "MP3"
    prefer_mp4: bool = True          # most compatible container/codecs
    embed_thumbnail: bool = True
    embed_metadata: bool = True
    subtitles: bool = False
    subtitle_langs: str = "en.*"
    playlist_subfolder: bool = True
    watch_clipboard: bool = True
    cookies_browser: str = "None"    # None | firefox | chrome | edge | brave | opera | vivaldi
    rate_limit: str = ""             # e.g. "2M" — blank for unlimited
    filename_template: str = "%(title).150B.%(ext)s"
    appearance: str = "Dark"         # Dark | Light | System

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            known = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in known:
                    setattr(s, k, v)
        except (OSError, ValueError):
            pass
        if not s.download_dir:
            s.download_dir = default_download_dir()
        return s

    def save(self) -> None:
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        except OSError:
            pass


def find_ffmpeg() -> str | None:
    """Return a path to ffmpeg: system install first, then the bundled imageio-ffmpeg binary.

    The bundled binary has a versioned file name, so it is copied once to
    BIN_DIR/ffmpeg.exe where yt-dlp recognises it.
    """
    system = shutil.which("ffmpeg")
    if system:
        return system
    local = BIN_DIR / "ffmpeg.exe"
    if local.exists():
        return str(local)
    try:
        import imageio_ffmpeg
        src = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None
    try:
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, local)
        return str(local)
    except OSError:
        return src
