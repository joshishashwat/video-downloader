# Video Downloader

A Windows desktop app for downloading video and audio from YouTube, Instagram, TikTok, X/Twitter, Facebook, Reddit, Vimeo and [1000+ other sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md). It runs on [yt-dlp](https://github.com/yt-dlp/yt-dlp).

## Run

Double-click **`run.bat`**. The first run sets up a local Python environment, which takes about a minute. Requires Python 3.10+.

To get a standalone `.exe` that doesn't need Python, run **`build.bat`**. The output goes to `dist\Video Downloader\`.

If a site stops working, run **`update.bat`**. Sites change often, and a newer yt-dlp usually fixes it.

## Features

- **Paste and go**: copy a link anywhere and switch to the app. The link is filled in automatically, so you just press **Enter**.
- **Several links at once**: paste a block of text and every link in it is queued.
- **Playlists and channels** are expanded into individual items, and each playlist can go into its own folder.
- **Quality picker**: Best, 4K, 1440p, 1080p, 720p, 480p or 360p. If a video doesn't offer the quality you picked, the app takes the next best one.
- **Audio only**: MP3, M4A or the original stream.
- **A preview before you download** shows the thumbnail, title, channel, length and the highest available quality.
- **Download queue** with a live progress bar, speed and time remaining. You can choose how many downloads run at once (1–5).
- **Pause and resume**: a paused download continues from where it stopped. **Retry** for failed downloads.
- Downloads are saved as **MP4 / H.264**, so they play everywhere, with cover art, title, artist and chapters built into the file. Optional subtitles.
- **Cookies from your browser**, for private, age-restricted or login-only posts.
- Optional speed limit, a custom file-name pattern, and Dark, Light or System theme.
- ffmpeg comes bundled, so you don't install anything separately. If ffmpeg is already installed on your system, the app uses that copy.

Settings are stored in `%APPDATA%\VideoDownloader\settings.json`.

## Layout

```
main.pyw          entry point (no console window)
vdl/config.py     settings + ffmpeg discovery
vdl/engine.py     yt-dlp wrapper, job queue, worker threads
vdl/ui.py         CustomTkinter interface
```

Only download content you have the rights to, and follow each site's terms of service.
