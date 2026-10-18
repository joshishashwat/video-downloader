"""Download engine: wraps yt-dlp, runs a queue of jobs on worker threads.

The UI never touches yt-dlp directly. It calls DownloadManager methods and
drains `manager.events` (a thread-safe queue) on the Tk main loop.
"""
from __future__ import annotations

import itertools
import os
import queue
import re
import threading
from dataclasses import dataclass, field

import yt_dlp
from yt_dlp.utils import DownloadCancelled, parse_bytes, sanitize_filename

from .config import Settings

VIDEO_QUALITIES = ["Best", "2160p", "1440p", "1080p", "720p", "480p", "360p"]
AUDIO_FORMATS = ["MP3", "M4A", "Original"]

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def extract_urls(text: str) -> list[str]:
    """All http(s) links in a block of text, de-duplicated, order kept."""
    seen, out = set(), []
    for u in URL_RE.findall(text or ""):
        u = u.rstrip(".,);]")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


class Status:
    FETCHING = "Fetching info"
    QUEUED = "Queued"
    DOWNLOADING = "Downloading"
    PROCESSING = "Processing"
    DONE = "Done"
    PAUSED = "Paused"
    ERROR = "Error"

    ACTIVE = {DOWNLOADING, PROCESSING}


class UserCancelled(DownloadCancelled):
    msg = "Paused by user"


@dataclass
class Job:
    id: int
    url: str
    mode: str                     # "Video" | "Audio"
    quality: str                  # entry of VIDEO_QUALITIES
    audio_format: str             # entry of AUDIO_FORMATS
    title: str = ""
    uploader: str = ""
    thumbnail: str | None = None
    playlist_title: str | None = None
    status: str = Status.FETCHING
    progress: float = 0.0         # 0..1, or -1 for indeterminate
    detail: str = ""
    filepath: str | None = None
    error: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    temp_files: set = field(default_factory=set)

    @property
    def label(self) -> str:
        return self.title or self.url

    @property
    def format_label(self) -> str:
        if self.mode == "Audio":
            return f"Audio · {self.audio_format}"
        return f"Video · {self.quality}"


# ---------------------------------------------------------------- formatting

def human_bytes(n: float | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_time(sec: float | None) -> str:
    if sec is None:
        return "--:--"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _clean_error(msg: str) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(msg))
    msg = msg.replace("ERROR: ", "").strip()
    return msg.splitlines()[0] if msg else "Unknown error"


class _Logger:
    """Swallows yt-dlp console output (there is no console under pythonw)."""

    def __init__(self):
        self.last_error = None

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        self.last_error = _clean_error(msg)


PP_LABELS = {
    "Merger": "Merging video + audio",
    "ExtractAudio": "Converting audio",
    "EmbedThumbnail": "Embedding thumbnail",
    "FFmpegMetadata": "Writing metadata",
    "Metadata": "Writing metadata",
    "EmbedSubtitle": "Embedding subtitles",
    "VideoConvertor": "Converting video",
    "VideoRemuxer": "Remuxing",
    "ThumbnailsConvertor": "Preparing thumbnail",
    "FixupM3u8": "Fixing container",
    "FixupM4a": "Fixing container",
}


# ---------------------------------------------------------------- yt-dlp options

def _base_opts(settings: Settings) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "no_color": True,
        "logger": _Logger(),
        "noplaylist": True,          # watch?v=..&list=.. → just that video
        "windowsfilenames": True,
        "socket_timeout": 30,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
    }
    if settings.cookies_browser and settings.cookies_browser != "None":
        opts["cookiesfrombrowser"] = (settings.cookies_browser.lower(),)
    return opts


def build_download_opts(job: Job, settings: Settings, ffmpeg: str | None) -> dict:
    opts = _base_opts(settings)
    pps: list[dict] = []

    out_dir = settings.download_dir
    if job.playlist_title and settings.playlist_subfolder:
        out_dir = os.path.join(out_dir, sanitize_filename(job.playlist_title))
    opts["paths"] = {"home": out_dir}
    opts["outtmpl"] = {"default": settings.filename_template or "%(title).150B.%(ext)s"}

    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg

    if settings.rate_limit.strip():
        rl = parse_bytes(settings.rate_limit.strip())
        if rl:
            opts["ratelimit"] = rl

    if settings.embed_thumbnail and ffmpeg:
        opts["writethumbnail"] = True
        pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})

    if job.mode == "Audio":
        if job.audio_format == "M4A":
            opts["format"] = "ba[ext=m4a]/ba/b"
        else:
            opts["format"] = "ba/b"
        if ffmpeg:
            codec = {"MP3": "mp3", "M4A": "m4a"}.get(job.audio_format, "best")
            pps.append({"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": "0"})
    else:
        # "res" is the video's shorter side, so a 1080x1920 vertical reel counts as 1080p.
        # "res:N" prefers the best quality not above N and falls back to the closest if none.
        res = "res" if job.quality == "Best" else f"res:{int(job.quality.rstrip('p'))}"
        opts["format"] = "bv*+ba/b" if ffmpeg else "b"  # merging streams needs ffmpeg
        if settings.prefer_mp4:
            opts["format_sort"] = [res, "fps", "vcodec:h264", "acodec:aac"]
            if ffmpeg:
                opts["merge_output_format"] = "mp4"
        else:
            opts["format_sort"] = [res]
        if settings.subtitles:
            opts["writesubtitles"] = True
            opts["subtitleslangs"] = [s.strip() for s in settings.subtitle_langs.split(",") if s.strip()]
            if ffmpeg:
                pps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})

    if settings.embed_metadata and ffmpeg:
        pps.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True})
    if settings.embed_thumbnail and ffmpeg:
        pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})

    opts["postprocessors"] = pps
    return opts


# ---------------------------------------------------------------- probing

def probe(url: str, settings: Settings) -> dict:
    """Lightweight info for previews. Playlists are listed flat (no per-item fetch)."""
    opts = _base_opts(settings)
    opts["extract_flat"] = "in_playlist"
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            raise RuntimeError(opts["logger"].last_error or _clean_error(e)) from None
    if info is None:
        raise RuntimeError(opts["logger"].last_error or "No media found at this link")

    if info.get("_type") == "playlist" or "entries" in info:
        entries = []
        for e in info.get("entries") or []:
            if not e:
                continue
            eurl = e.get("url") or e.get("webpage_url")
            if not eurl:
                continue
            thumbs = e.get("thumbnails") or []
            entries.append({
                "url": eurl,
                "title": e.get("title") or eurl,
                "thumbnail": e.get("thumbnail") or (thumbs[-1]["url"] if thumbs else None),
            })
        thumbs = info.get("thumbnails") or []
        return {
            "kind": "playlist",
            "title": info.get("title") or "Playlist",
            "uploader": info.get("uploader") or info.get("channel") or "",
            "thumbnail": info.get("thumbnail") or (thumbs[-1]["url"] if thumbs else None)
                         or (entries[0]["thumbnail"] if entries else None),
            "entries": entries,
            "count": len(entries),
            "site": info.get("extractor_key", ""),
        }

    heights = sorted({min(f["height"], f.get("width") or f["height"]) for f in info.get("formats") or []
                      if f.get("height") and f.get("vcodec") not in (None, "none")}, reverse=True)
    return {
        "kind": "video",
        "title": info.get("title") or url,
        "uploader": info.get("uploader") or info.get("channel") or "",
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "heights": heights,
        "site": info.get("extractor_key", ""),
        "url": info.get("webpage_url") or url,
    }


# ---------------------------------------------------------------- manager

class DownloadManager:
    def __init__(self, settings: Settings, ffmpeg: str | None):
        self.settings = settings
        self.ffmpeg = ffmpeg
        self.jobs: list[Job] = []
        self.events: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self._probe_cache: dict[str, dict] = {}

    # ---- events
    def _emit(self, kind: str, job: Job):
        self.events.put((kind, job))

    # ---- probe (used by the UI preview; results reused when queueing)
    def probe(self, url: str) -> dict:
        if url in self._probe_cache:
            return self._probe_cache[url]
        info = probe(url, self.settings)
        self._probe_cache[url] = info
        return info

    # ---- public API
    def submit(self, urls: list[str], mode: str, quality: str, audio_format: str) -> None:
        for url in urls:
            job = Job(next(self._ids), url, mode, quality, audio_format)
            with self._lock:
                self.jobs.append(job)
            self._emit("added", job)
            threading.Thread(target=self._resolve, args=(job,), daemon=True).start()

    def pause(self, job: Job):
        if job.status in Status.ACTIVE:
            job.cancel.set()
        elif job.status == Status.QUEUED:
            job.status = Status.PAUSED
            job.detail = ""
            self._emit("update", job)

    def resume(self, job: Job):
        if job.status in (Status.PAUSED, Status.ERROR):
            job.cancel.clear()
            job.error = None
            job.status = Status.QUEUED
            job.detail = ""
            self._emit("update", job)
            self._schedule()

    def remove(self, job: Job):
        if job.status in Status.ACTIVE:
            job.cancel.set()
            job._remove_after_stop = True
            return
        self._drop(job)

    def _drop(self, job: Job):
        with self._lock:
            if job in self.jobs:
                self.jobs.remove(job)
        if job.status != Status.DONE:
            leftovers = set()
            for f in job.temp_files:
                leftovers.update((f, f + ".part", f + ".ytdl"))
                # "Title.f137.mp4.part" -> "Title": also drop the thumbnail written beside it
                stem = re.sub(r"(\.part|\.ytdl)$", "", f)
                stem = re.sub(r"(\.f[\w-]+)?\.\w+$", "", stem)
                leftovers.update(stem + ext for ext in (".jpg", ".webp", ".png"))
            for p in leftovers:
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                except OSError:
                    pass
        self._emit("removed", job)
        self._schedule()

    def clear_finished(self):
        with self._lock:
            done = [j for j in self.jobs if j.status == Status.DONE]
        for j in done:
            self._drop(j)

    def pause_all(self):
        for j in list(self.jobs):
            self.pause(j)

    def resume_all(self):
        for j in list(self.jobs):
            if j.status == Status.PAUSED:
                self.resume(j)

    def active_count(self) -> int:
        return sum(1 for j in self.jobs if j.status in Status.ACTIVE)

    # ---- internals
    def _resolve(self, job: Job):
        try:
            info = self.probe(job.url)
        except Exception as e:
            job.status = Status.ERROR
            job.error = str(e)
            job.detail = job.error
            self._emit("update", job)
            return

        if info["kind"] == "playlist":
            children = []
            for e in info["entries"]:
                c = Job(next(self._ids), e["url"], job.mode, job.quality, job.audio_format,
                        title=e["title"], thumbnail=e["thumbnail"],
                        playlist_title=info["title"], status=Status.QUEUED)
                children.append(c)
            with self._lock:
                idx = self.jobs.index(job) if job in self.jobs else len(self.jobs)
                if job in self.jobs:
                    self.jobs.remove(job)
                self.jobs[idx:idx] = children
            self._emit("removed", job)
            for c in children:
                self._emit("added", c)
            if not children:
                job.status = Status.ERROR
                job.error = "Playlist is empty"
                with self._lock:
                    self.jobs.insert(idx, job)
                self._emit("added", job)
        else:
            job.title = info["title"]
            job.uploader = info["uploader"]
            job.thumbnail = info["thumbnail"]
            job.status = Status.QUEUED
            self._emit("update", job)
        self._schedule()

    def _schedule(self):
        with self._lock:
            free = max(1, int(self.settings.max_concurrent)) - self.active_count()
            for job in self.jobs:
                if free <= 0:
                    break
                if job.status == Status.QUEUED:
                    job.status = Status.DOWNLOADING
                    job.detail = "Starting…"
                    job.progress = -1
                    self._emit("update", job)
                    threading.Thread(target=self._run, args=(job,), daemon=True).start()
                    free -= 1

    def _run(self, job: Job):
        opts = build_download_opts(job, self.settings, self.ffmpeg)
        logger = opts["logger"]

        def progress_hook(d):
            if job.cancel.is_set():
                raise UserCancelled()
            if d.get("tmpfilename"):
                job.temp_files.add(d["tmpfilename"])
            if d.get("filename"):
                job.temp_files.add(d["filename"])
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                if total:
                    job.progress = min(done / total, 1.0)
                elif d.get("fragment_count"):
                    job.progress = (d.get("fragment_index") or 0) / d["fragment_count"]
                else:
                    job.progress = -1
                fi = d.get("info_dict") or {}
                stream = ""
                if fi.get("vcodec") == "none" and job.mode == "Video":
                    stream = "Audio stream · "
                elif fi.get("acodec") == "none" and fi.get("vcodec") not in (None, "none"):
                    stream = "Video stream · "
                job.status = Status.DOWNLOADING
                job.detail = (f"{stream}{human_bytes(done)} / {human_bytes(total)}"
                              f"  ·  {human_bytes(d.get('speed'))}/s  ·  ETA {human_time(d.get('eta'))}")
                self._emit("update", job)

        def pp_hook(d):
            if d["status"] == "started":
                name = d.get("postprocessor", "")
                job.status = Status.PROCESSING
                job.progress = -1
                job.detail = PP_LABELS.get(name, "Processing…")
                self._emit("update", job)

        def post_hook(path):
            job.filepath = path

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.add_progress_hook(progress_hook)
                ydl.add_postprocessor_hook(pp_hook)
                ydl.add_post_hook(post_hook)
                ret = ydl.download([job.url])
            if ret != 0:
                raise RuntimeError(logger.last_error or "Download failed")
            job.status = Status.DONE
            job.progress = 1.0
            size = os.path.getsize(job.filepath) if job.filepath and os.path.exists(job.filepath) else None
            job.detail = f"Saved · {human_bytes(size)}" if size else "Saved"
        except DownloadCancelled:
            job.status = Status.PAUSED
            job.detail = "Paused — resume to continue where it left off"
        except Exception as e:
            job.status = Status.ERROR
            job.error = logger.last_error or _clean_error(e)
            job.detail = job.error
        self._emit("update", job)

        if getattr(job, "_remove_after_stop", False):
            self._drop(job)
        else:
            self._schedule()
