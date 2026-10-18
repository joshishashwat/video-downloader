"""CustomTkinter desktop UI."""
from __future__ import annotations

import io
import os
import queue
import subprocess
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog

import customtkinter as ctk
from PIL import Image, ImageDraw

from . import APP_NAME, __version__
from .config import Settings
from .engine import (AUDIO_FORMATS, VIDEO_QUALITIES, DownloadManager, Job, Status,
                     extract_urls, human_time)

_orig_on_release = ctk.CTkButton._on_release


def _on_release(self, event=None):
    # CTkButton only fires if an <Enter> event was seen first. Touch screens, pens and
    # remote-desktop tools can click without one, so also accept a release inside the widget.
    if event is not None and not self._mouse_inside:
        w = event.widget
        if 0 <= event.x < w.winfo_width() and 0 <= event.y < w.winfo_height():
            self._mouse_inside = True
    _orig_on_release(self, event)


ctk.CTkButton._on_release = _on_release

THUMB_SMALL = (112, 63)
THUMB_LARGE = (192, 108)

STATUS_COLORS = {
    Status.FETCHING: ("gray40", "gray65"),
    Status.QUEUED: ("gray40", "gray65"),
    Status.DOWNLOADING: ("#1f6aa5", "#5aa9ff"),
    Status.PROCESSING: ("#8a5a00", "#f0b44c"),
    Status.DONE: ("#1d7a3a", "#4cd17a"),
    Status.PAUSED: ("gray40", "gray65"),
    Status.ERROR: ("#b3261e", "#ff6b61"),
}

CARD = ("gray92", "gray17")
CARD_INNER = ("gray97", "gray20")


def _placeholder(size, text=""):
    img = Image.new("RGB", size, (60, 60, 60))
    if text:
        d = ImageDraw.Draw(img)
        d.text((size[0] // 2, size[1] // 2), text, fill=(170, 170, 170), anchor="mm")
    return img


def _cover(img: Image.Image, size) -> Image.Image:
    """Resize + center-crop to fill `size` exactly."""
    img = img.convert("RGB")
    tw, th = size
    scale = max(tw / img.width, th / img.height)
    img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    left = (img.width - tw) // 2
    top = (img.height - th) // 2
    return img.crop((left, top, left + tw, top + th))


class ThumbLoader:
    """Fetches thumbnails off the UI thread; results are handed back via `post`."""

    def __init__(self, post):
        self._post = post
        self._pool = ThreadPoolExecutor(max_workers=4)
        self._cache: dict[tuple, Image.Image] = {}

    def load(self, url: str | None, size, callback):
        if not url:
            return
        key = (url, size)
        if key in self._cache:
            callback(self._cache[key])
            return

        def work():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = r.read()
                img = _cover(Image.open(io.BytesIO(data)), size)
            except Exception:
                return
            self._cache[key] = img
            self._post(lambda: callback(img))

        self._pool.submit(work)


# ======================================================================= rows

class QueueRow(ctk.CTkFrame):
    def __init__(self, master, app: "App", job: Job):
        super().__init__(master, fg_color=CARD_INNER, corner_radius=10)
        self.app = app
        self.job = job
        self._indeterminate = False
        self._thumb_loaded_for = None

        self.grid_columnconfigure(1, weight=1)

        self.thumb = ctk.CTkLabel(self, text="", width=THUMB_SMALL[0], height=THUMB_SMALL[1],
                                  image=ctk.CTkImage(_placeholder(THUMB_SMALL), size=THUMB_SMALL))
        self.thumb.grid(row=0, column=0, rowspan=3, padx=(10, 12), pady=10)

        self.title = ctk.CTkLabel(self, text="", anchor="w", justify="left",
                                  font=ctk.CTkFont(size=14, weight="bold"))
        self.title.grid(row=0, column=1, sticky="ew", pady=(10, 0))

        self.sub = ctk.CTkLabel(self, text="", anchor="w", font=ctk.CTkFont(size=12))
        self.sub.grid(row=1, column=1, sticky="ew")

        self.bar = ctk.CTkProgressBar(self, height=8)
        self.bar.grid(row=2, column=1, sticky="ew", pady=(2, 12))
        self.bar.set(0)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=0, column=2, rowspan=3, padx=10)
        self.primary = ctk.CTkButton(btns, text="", width=86, height=30, command=self._primary)
        self.primary.pack(side="left", padx=(0, 6))
        self.folder = ctk.CTkButton(btns, text="Folder", width=64, height=30,
                                    fg_color="transparent", border_width=1,
                                    text_color=("gray20", "gray85"), command=self._show_in_folder)
        self.folder.pack(side="left", padx=(0, 6))
        self.remove = ctk.CTkButton(btns, text="✕", width=30, height=30,
                                    fg_color="transparent", hover_color=("gray80", "gray30"),
                                    text_color=("gray30", "gray75"), command=self._remove)
        self.remove.pack(side="left")

        for w in (self, self.thumb, self.title, self.sub):
            w.bind("<Double-Button-1>", lambda e: self._open_file())

        self.bind("<Configure>", self._on_resize)
        self.refresh()

    def _on_resize(self, event):
        wrap = max(200, event.width - THUMB_SMALL[0] - 260)
        self.title.configure(wraplength=wrap)

    # ---- actions
    def _primary(self):
        j = self.job
        if j.status in Status.ACTIVE or j.status == Status.QUEUED:
            self.app.manager.pause(j)
        elif j.status in (Status.PAUSED, Status.ERROR):
            self.app.manager.resume(j)
        elif j.status == Status.DONE:
            self._open_file()

    def _open_file(self):
        p = self.job.filepath
        if p and os.path.exists(p):
            os.startfile(p)

    def _show_in_folder(self):
        p = self.job.filepath
        if p and os.path.exists(p):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
        else:
            self.app.open_download_dir()

    def _remove(self):
        self.app.manager.remove(self.job)

    # ---- render
    def refresh(self):
        j = self.job
        title = j.label
        if len(title) > 140:
            title = title[:137] + "…"
        self.title.configure(text=title)

        status = j.status
        if status == Status.DOWNLOADING and j.detail == "Starting…":
            line = "Starting…"
        elif j.detail:
            line = f"{status} · {j.detail}" if status not in (Status.DOWNLOADING, Status.DONE) else j.detail
        else:
            line = status
        prefix = j.format_label
        if j.playlist_title:
            prefix = f"{j.playlist_title} · {prefix}"
        self.sub.configure(text=f"{prefix}   —   {line}", text_color=STATUS_COLORS.get(status))

        # progress bar
        if (j.progress < 0 and status in Status.ACTIVE) or status == Status.FETCHING:
            if not self._indeterminate:
                self.bar.configure(mode="indeterminate")
                self.bar.start()
                self._indeterminate = True
        else:
            if self._indeterminate:
                self.bar.stop()
                self.bar.configure(mode="determinate")
                self._indeterminate = False
            self.bar.set(1.0 if status == Status.DONE else max(0.0, j.progress))
        self.bar.configure(progress_color=STATUS_COLORS[Status.DONE] if status == Status.DONE else
                           STATUS_COLORS[Status.ERROR] if status == Status.ERROR else
                           ("#3a7ebf", "#1f6aa5"))

        # primary button
        if status in Status.ACTIVE or status == Status.QUEUED:
            self.primary.configure(text="Pause", state="normal")
        elif status == Status.PAUSED:
            self.primary.configure(text="Resume", state="normal")
        elif status == Status.ERROR:
            self.primary.configure(text="Retry", state="normal")
        elif status == Status.DONE:
            self.primary.configure(text="Open", state="normal")
        else:
            self.primary.configure(text="…", state="disabled")

        if j.thumbnail and self._thumb_loaded_for != j.thumbnail:
            self._thumb_loaded_for = j.thumbnail
            self.app.thumbs.load(j.thumbnail, THUMB_SMALL, self._set_thumb)

    def _set_thumb(self, img):
        if self.winfo_exists():
            self.thumb.configure(image=ctk.CTkImage(img, size=THUMB_SMALL))


# ======================================================================= settings dialog

class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        s = app.settings
        self.title("Settings")
        self.geometry("560x640")
        self.resizable(False, True)
        self.transient(app)
        # CTkToplevel can open behind the main window on Windows; raise it once mapped.
        self.after(150, self._bring_to_front)

        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(16, 0))
        body.grid_columnconfigure(1, weight=1)
        r = 0

        def section(text):
            nonlocal r
            ctk.CTkLabel(body, text=text, font=ctk.CTkFont(size=15, weight="bold"),
                         anchor="w").grid(row=r, column=0, columnspan=2, sticky="w", pady=(12, 4))
            r += 1

        def row(label, widget, hint=None):
            nonlocal r
            ctk.CTkLabel(body, text=label, anchor="w").grid(row=r, column=0, sticky="w", padx=(0, 12), pady=4)
            widget.grid(row=r, column=1, sticky="ew", pady=4)
            r += 1
            if hint:
                ctk.CTkLabel(body, text=hint, anchor="w", text_color=("gray45", "gray60"),
                             font=ctk.CTkFont(size=11), wraplength=380, justify="left"
                             ).grid(row=r, column=1, sticky="w")
                r += 1

        section("Downloads")
        self.dir_var = ctk.StringVar(value=s.download_dir)
        dir_row = ctk.CTkFrame(body, fg_color="transparent")
        ctk.CTkEntry(dir_row, textvariable=self.dir_var).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(dir_row, text="Browse", width=70, command=self._browse).pack(side="left", padx=(6, 0))
        row("Save to", dir_row)

        self.conc_var = ctk.StringVar(value=str(s.max_concurrent))
        row("Simultaneous downloads", ctk.CTkSegmentedButton(body, values=["1", "2", "3", "4", "5"],
                                                             variable=self.conc_var))
        self.rate_var = ctk.StringVar(value=s.rate_limit)
        row("Speed limit", ctk.CTkEntry(body, textvariable=self.rate_var, placeholder_text="unlimited"),
            "Per download, e.g. 500K or 2M. Leave blank for unlimited.")
        self.tmpl_var = ctk.StringVar(value=s.filename_template)
        row("File name", ctk.CTkEntry(body, textvariable=self.tmpl_var),
            "yt-dlp template. %(title)s, %(uploader)s, %(upload_date)s, %(id)s, %(ext)s …")
        self.sub_folder = ctk.BooleanVar(value=s.playlist_subfolder)
        row("Playlists", ctk.CTkSwitch(body, text="Save each playlist in its own folder", variable=self.sub_folder))

        section("Output")
        self.mp4 = ctk.BooleanVar(value=s.prefer_mp4)
        row("Compatibility", ctk.CTkSwitch(body, text="Prefer MP4 / H.264 (plays everywhere)", variable=self.mp4))
        self.thumb = ctk.BooleanVar(value=s.embed_thumbnail)
        row("Cover art", ctk.CTkSwitch(body, text="Embed thumbnail", variable=self.thumb))
        self.meta = ctk.BooleanVar(value=s.embed_metadata)
        row("Metadata", ctk.CTkSwitch(body, text="Embed title, artist, chapters", variable=self.meta))
        self.subs = ctk.BooleanVar(value=s.subtitles)
        row("Subtitles", ctk.CTkSwitch(body, text="Download & embed subtitles", variable=self.subs))
        self.sub_langs = ctk.StringVar(value=s.subtitle_langs)
        row("Subtitle languages", ctk.CTkEntry(body, textvariable=self.sub_langs),
            "Comma separated, e.g. en.*,hi,es")

        section("Access")
        self.cookies = ctk.StringVar(value=s.cookies_browser)
        row("Use cookies from", ctk.CTkOptionMenu(body, variable=self.cookies,
                                                  values=["None", "Firefox", "Edge", "Chrome", "Brave", "Opera", "Vivaldi"]),
            "For private, age-restricted or login-only posts. Uses your browser's login; "
            "Chromium browsers may need to be closed first. Firefox is the most reliable.")

        section("App")
        self.clip = ctk.BooleanVar(value=s.watch_clipboard)
        row("Clipboard", ctk.CTkSwitch(body, text="Auto-paste copied links when the window is focused",
                                       variable=self.clip))
        self.appearance = ctk.StringVar(value=s.appearance)
        row("Theme", ctk.CTkSegmentedButton(body, values=["Dark", "Light", "System"], variable=self.appearance))

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=16, pady=12)
        ctk.CTkLabel(foot, text=f"{APP_NAME} {__version__}", text_color=("gray45", "gray60")).pack(side="left")
        ctk.CTkButton(foot, text="Save", width=100, command=self._save).pack(side="right")
        ctk.CTkButton(foot, text="Cancel", width=90, fg_color="transparent", border_width=1,
                      text_color=("gray20", "gray85"), command=self.destroy).pack(side="right", padx=8)

    def _bring_to_front(self):
        self.lift()
        self.focus_force()
        try:
            self.grab_set()
        except Exception:
            pass

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or None, parent=self)
        if d:
            self.dir_var.set(os.path.normpath(d))

    def _save(self):
        s = self.app.settings
        s.download_dir = self.dir_var.get().strip() or s.download_dir
        s.max_concurrent = int(self.conc_var.get())
        s.rate_limit = self.rate_var.get().strip()
        s.filename_template = self.tmpl_var.get().strip() or "%(title).150B.%(ext)s"
        s.playlist_subfolder = self.sub_folder.get()
        s.prefer_mp4 = self.mp4.get()
        s.embed_thumbnail = self.thumb.get()
        s.embed_metadata = self.meta.get()
        s.subtitles = self.subs.get()
        s.subtitle_langs = self.sub_langs.get().strip() or "en.*"
        s.cookies_browser = self.cookies.get()
        s.watch_clipboard = self.clip.get()
        s.appearance = self.appearance.get()
        s.save()
        ctk.set_appearance_mode(s.appearance)
        self.app.manager._probe_cache.clear()
        self.app.refresh_folder_label()
        self.app.manager._schedule()
        self.destroy()


# ======================================================================= main window

class App(ctk.CTk):
    def __init__(self, settings: Settings, ffmpeg: str | None):
        ctk.set_appearance_mode(settings.appearance)
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.settings = settings
        self.ffmpeg = ffmpeg
        self.manager = DownloadManager(settings, ffmpeg)
        self._ui_calls: queue.Queue = queue.Queue()
        self.thumbs = ThumbLoader(self._ui_calls.put)
        self.rows: dict[int, QueueRow] = {}
        self._last_clip = None
        self._probe_after = None
        self._probe_token = 0
        self._preview_urls: list[str] = []
        self._settings_dialog = None

        self.title(APP_NAME)
        self.geometry("980x720")
        self.minsize(760, 560)
        try:
            icon = os.path.join(os.path.dirname(__file__), "assets", "icon.ico")
            if os.path.exists(icon):
                self.iconbitmap(icon)
        except Exception:
            pass

        self._build()
        self.bind("<FocusIn>", self._on_focus)
        self.after(100, self._pump)
        self.after(400, self._check_clipboard)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ layout
    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # header
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 8))
        ctk.CTkLabel(head, text=APP_NAME, font=ctk.CTkFont(size=24, weight="bold")).pack(side="left")
        ctk.CTkLabel(head, text="   YouTube · Instagram · TikTok · X · Facebook · Reddit · Vimeo + 1000 more",
                     text_color=("gray45", "gray60")).pack(side="left", pady=(6, 0))
        ctk.CTkButton(head, text="⚙  Settings", width=100, fg_color="transparent", border_width=1,
                      text_color=("gray20", "gray85"), command=self.open_settings).pack(side="right")

        # input card
        card = ctk.CTkFrame(self, fg_color=CARD, corner_radius=14)
        card.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 10))
        card.grid_columnconfigure(0, weight=1)

        self.url_var = ctk.StringVar()
        self.url_entry = ctk.CTkEntry(card, textvariable=self.url_var, height=44,
                                      font=ctk.CTkFont(size=14),
                                      placeholder_text="Paste a link (or several) — video, reel, short, playlist…")
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(14, 8), pady=(14, 8))
        self.url_entry.bind("<Return>", lambda e: self.download())
        self.url_var.trace_add("write", lambda *a: self._schedule_probe())

        ctk.CTkButton(card, text="Paste", width=80, height=44, fg_color="transparent", border_width=1,
                      text_color=("gray20", "gray85"), command=self.paste).grid(row=0, column=1, padx=(0, 8), pady=(14, 8))
        self.dl_btn = ctk.CTkButton(card, text="Download", width=130, height=44,
                                    font=ctk.CTkFont(size=15, weight="bold"), command=self.download)
        self.dl_btn.grid(row=0, column=2, padx=(0, 14), pady=(14, 8))

        opts = ctk.CTkFrame(card, fg_color="transparent")
        opts.grid(row=1, column=0, columnspan=3, sticky="ew", padx=14, pady=(0, 12))

        self.mode_var = ctk.StringVar(value=self.settings.mode)
        ctk.CTkSegmentedButton(opts, values=["Video", "Audio"], variable=self.mode_var,
                               command=lambda v: self._on_mode()).pack(side="left")
        self.quality_var = ctk.StringVar()
        self.quality_menu = ctk.CTkOptionMenu(opts, variable=self.quality_var, width=120,
                                              command=lambda v: self._remember_quality())
        self.quality_menu.pack(side="left", padx=10)
        self._on_mode()

        self.folder_btn = ctk.CTkButton(opts, text="", fg_color="transparent", anchor="w",
                                        text_color=("gray30", "gray70"), hover_color=("gray85", "gray25"),
                                        command=self.choose_folder)
        self.folder_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.refresh_folder_label()

        # preview (hidden until a link is analysed)
        self.preview = ctk.CTkFrame(card, fg_color=CARD_INNER, corner_radius=10)
        self.preview.grid_columnconfigure(1, weight=1)
        self.pv_thumb = ctk.CTkLabel(self.preview, text="", width=THUMB_LARGE[0], height=THUMB_LARGE[1])
        self.pv_thumb.grid(row=0, column=0, rowspan=3, padx=10, pady=10)
        self.pv_title = ctk.CTkLabel(self.preview, text="", anchor="w", justify="left",
                                     font=ctk.CTkFont(size=15, weight="bold"), wraplength=560)
        self.pv_title.grid(row=0, column=1, sticky="sw", padx=(0, 12), pady=(12, 0))
        self.pv_meta = ctk.CTkLabel(self.preview, text="", anchor="w", justify="left",
                                    text_color=("gray40", "gray65"))
        self.pv_meta.grid(row=1, column=1, sticky="nw", padx=(0, 12))
        self.pv_note = ctk.CTkLabel(self.preview, text="", anchor="w", justify="left",
                                    text_color=("gray40", "gray65"), font=ctk.CTkFont(size=12))
        self.pv_note.grid(row=2, column=1, sticky="nw", padx=(0, 12), pady=(0, 10))

        # queue header
        qh = ctk.CTkFrame(self, fg_color="transparent")
        qh.grid(row=2, column=0, sticky="ew", padx=20, pady=(6, 4))
        self.q_label = ctk.CTkLabel(qh, text="Downloads", font=ctk.CTkFont(size=17, weight="bold"))
        self.q_label.pack(side="left")
        self.q_counts = ctk.CTkLabel(qh, text="", text_color=("gray45", "gray60"))
        self.q_counts.pack(side="left", padx=10)
        small = dict(height=28, fg_color="transparent", border_width=1, text_color=("gray20", "gray85"))
        ctk.CTkButton(qh, text="Clear finished", width=110, command=self.manager.clear_finished,
                      **small).pack(side="right")
        ctk.CTkButton(qh, text="Resume all", width=90, command=self.manager.resume_all,
                      **small).pack(side="right", padx=6)
        ctk.CTkButton(qh, text="Pause all", width=80, command=self.manager.pause_all,
                      **small).pack(side="right")
        ctk.CTkButton(qh, text="Open folder", width=100, command=self.open_download_dir,
                      **small).pack(side="right", padx=6)

        # queue list
        self.qframe = ctk.CTkScrollableFrame(self, fg_color=CARD, corner_radius=14)
        self.qframe.grid(row=3, column=0, sticky="nsew", padx=20, pady=(0, 10))
        self.qframe.grid_columnconfigure(0, weight=1)
        self.empty = ctk.CTkLabel(
            self.qframe, justify="center", text_color=("gray45", "gray60"), font=ctk.CTkFont(size=14),
            text="\n\nNo downloads yet\n\nCopy a link anywhere, come back here and press Download.\n"
                 "Paste several links at once to queue them all.")
        self.empty.grid(row=0, column=0, pady=40)

        # status bar
        self.status = ctk.CTkLabel(self, text="", anchor="w", text_color=("gray45", "gray60"))
        self.status.grid(row=4, column=0, sticky="ew", padx=22, pady=(0, 10))
        if not self.ffmpeg:
            self.set_status("⚠ ffmpeg not found — downloads limited to single-file formats; MP3 conversion disabled.")

    # ------------------------------------------------------------------ helpers
    def set_status(self, text):
        self.status.configure(text=text)

    def refresh_folder_label(self):
        self.folder_btn.configure(text=f"📁  Save to:  {self.settings.download_dir}")

    def _on_mode(self):
        mode = self.mode_var.get()
        self.settings.mode = mode
        if mode == "Audio":
            self.quality_menu.configure(values=AUDIO_FORMATS)
            self.quality_var.set(self.settings.audio_format)
        else:
            self.quality_menu.configure(values=VIDEO_QUALITIES)
            self.quality_var.set(self.settings.video_quality)
        self.settings.save()

    def _remember_quality(self):
        if self.mode_var.get() == "Audio":
            self.settings.audio_format = self.quality_var.get()
        else:
            self.settings.video_quality = self.quality_var.get()
        self.settings.save()

    def choose_folder(self):
        d = filedialog.askdirectory(initialdir=self.settings.download_dir or None, parent=self)
        if d:
            self.settings.download_dir = os.path.normpath(d)
            self.settings.save()
            self.refresh_folder_label()

    def open_download_dir(self):
        d = self.settings.download_dir
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    def open_settings(self):
        if self._settings_dialog is not None and self._settings_dialog.winfo_exists():
            self._settings_dialog._bring_to_front()
            return
        self._settings_dialog = SettingsDialog(self)

    def paste(self):
        try:
            text = self.clipboard_get()
        except Exception:
            return
        urls = extract_urls(text)
        if urls:
            self.url_var.set(" ".join(urls))
            self.url_entry.icursor("end")

    # ------------------------------------------------------------------ clipboard
    def _on_focus(self, event):
        if event.widget is self:
            self.after(50, self._check_clipboard)

    def _check_clipboard(self):
        if not self.settings.watch_clipboard:
            return
        try:
            text = self.clipboard_get()
        except Exception:
            return
        if text == self._last_clip:
            return
        self._last_clip = text
        urls = extract_urls(text)
        if urls and not self.url_var.get().strip():
            self.url_var.set(" ".join(urls))
            self.url_entry.icursor("end")
            self.set_status("Link picked up from clipboard — press Download (or Enter).")

    # ------------------------------------------------------------------ preview
    def _schedule_probe(self):
        if self._probe_after:
            self.after_cancel(self._probe_after)
        self._probe_after = self.after(500, self._probe)

    def _probe(self):
        self._probe_after = None
        urls = extract_urls(self.url_var.get())
        self._probe_token += 1
        token = self._probe_token
        if urls == self._preview_urls:
            return
        self._preview_urls = urls
        if not urls:
            self.preview.grid_forget()
            return
        self.preview.grid(row=2, column=0, columnspan=3, sticky="ew", padx=14, pady=(0, 14))
        self.pv_thumb.configure(image=ctk.CTkImage(_placeholder(THUMB_LARGE, "…"), size=THUMB_LARGE))
        if len(urls) > 1:
            self.pv_title.configure(text=f"{len(urls)} links ready")
            self.pv_meta.configure(text="\n".join(u if len(u) < 90 else u[:87] + "…" for u in urls[:4])
                                   + ("\n…" if len(urls) > 4 else ""))
            self.pv_note.configure(text="Press Download to queue them all.")
            return
        self.pv_title.configure(text="Looking up link…")
        self.pv_meta.configure(text=urls[0])
        self.pv_note.configure(text="")

        def work():
            try:
                info = self.manager.probe(urls[0])
                err = None
            except Exception as e:
                info, err = None, str(e)
            self._ui_calls.put(lambda: self._show_preview(token, info, err))

        threading.Thread(target=work, daemon=True).start()

    def _show_preview(self, token, info, err):
        if token != self._probe_token:
            return
        if err:
            self.pv_title.configure(text="Couldn't read this link")
            self.pv_meta.configure(text=err[:300])
            self.pv_note.configure(text="Tip: login-only posts may need Settings → Use cookies from your browser.")
            self.pv_thumb.configure(image=ctk.CTkImage(_placeholder(THUMB_LARGE, "!"), size=THUMB_LARGE))
            return
        self.pv_title.configure(text=info["title"][:200])
        if info["kind"] == "playlist":
            meta = [p for p in (info["site"], info["uploader"], f"Playlist · {info['count']} items") if p]
            note = "Download will queue every item in this playlist."
        else:
            meta = [p for p in (info["site"], info["uploader"],
                                human_time(info["duration"]) if info.get("duration") else "") if p]
            h = info.get("heights") or []
            note = f"Available up to {h[0]}p" if h else "Audio / single-format media"
        self.pv_meta.configure(text="  ·  ".join(meta))
        self.pv_note.configure(text=note)
        if info.get("thumbnail"):
            self.thumbs.load(info["thumbnail"], THUMB_LARGE,
                             lambda img: token == self._probe_token and
                             self.pv_thumb.configure(image=ctk.CTkImage(img, size=THUMB_LARGE)))

    # ------------------------------------------------------------------ actions
    def download(self):
        urls = extract_urls(self.url_var.get())
        if not urls:
            self.set_status("Paste a link first.")
            self.url_entry.focus_set()
            return
        mode = self.mode_var.get()
        q = self.quality_var.get()
        self.manager.submit(urls, mode,
                            q if mode == "Video" else self.settings.video_quality,
                            q if mode == "Audio" else self.settings.audio_format)
        self.url_var.set("")
        self._preview_urls = []
        self.preview.grid_forget()
        self.set_status(f"Added {len(urls)} link{'s' if len(urls) > 1 else ''} to the queue.")

    # ------------------------------------------------------------------ event pump
    def _pump(self):
        changed = False
        try:
            while True:
                self._ui_calls.get_nowait()()
        except queue.Empty:
            pass
        try:
            for _ in range(500):
                kind, job = self.manager.events.get_nowait()
                changed = True
                if kind == "added":
                    self._add_row(job)
                elif kind == "removed":
                    row = self.rows.pop(job.id, None)
                    if row:
                        row.destroy()
                elif kind == "update":
                    row = self.rows.get(job.id)
                    if row:
                        row.refresh()
        except queue.Empty:
            pass
        if changed:
            self._relayout()
        self.after(100, self._pump)

    def _add_row(self, job):
        if job.id in self.rows:
            return
        self.rows[job.id] = QueueRow(self.qframe, self, job)

    def _relayout(self):
        order = [j.id for j in self.manager.jobs]
        if not order:
            self.empty.grid(row=0, column=0, pady=40)
        else:
            self.empty.grid_forget()
        for i, jid in enumerate(order):
            row = self.rows.get(jid)
            if row:
                row.grid(row=i, column=0, sticky="ew", padx=8, pady=4)

        jobs = self.manager.jobs
        active = sum(1 for j in jobs if j.status in Status.ACTIVE)
        waiting = sum(1 for j in jobs if j.status in (Status.QUEUED, Status.FETCHING))
        done = sum(1 for j in jobs if j.status == Status.DONE)
        failed = sum(1 for j in jobs if j.status == Status.ERROR)
        parts = []
        if active:
            parts.append(f"{active} downloading")
        if waiting:
            parts.append(f"{waiting} waiting")
        if done:
            parts.append(f"{done} done")
        if failed:
            parts.append(f"{failed} failed")
        self.q_counts.configure(text="  ·  ".join(parts))
        self.title(f"({active}) {APP_NAME}" if active else APP_NAME)

    def _on_close(self):
        if self.manager.active_count():
            from tkinter import messagebox
            if not messagebox.askyesno(APP_NAME, "Downloads are still running. Quit anyway?\n"
                                                 "Unfinished downloads can be resumed next time from the "
                                                 "same folder by adding the link again.", parent=self):
                return
            self.manager.pause_all()
        self.settings.save()
        self.destroy()
