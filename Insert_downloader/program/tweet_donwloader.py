#!/usr/bin/env python3
"""
Process Twitter/X links:
  - Tweet WITH embedded video  → download the video with yt-dlp  (.mp4)
  - Tweet WITHOUT video        → screenshot the card + mosaic animation  (.mov)
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

import yt_dlp

from downloader_utils import (
    OUTPUT_DIR,
    YTDLP_FORMAT,
    build_insert_stem,
    build_summary_entry,
    clean_url,
    cleanup_download_artifacts,
    prepare_processing_context,
    rename_with_schema,
    summary_entry_is_complete,
    yt_dlp_cookie_options,
)

logger = logging.getLogger("insert_downloader.twitter")

PYTHON_BIN = "python3.11"
_PROGRAM_DIR = Path(__file__).resolve().parent
TWEET_SCREENSHOT_SCRIPT = _PROGRAM_DIR / "tweet_screenshot_1.py"
MOSAIC_VIDEO_SCRIPT = _PROGRAM_DIR / "tweetcapture" / "tweet_screen_to_blur_mosaic_video.py"

TWITTER_HOSTS = (
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
    "x.com",
    "www.x.com",
)


def is_twitter_link(netloc: str) -> bool:
    lowered = netloc.lower()
    return any(lowered.endswith(host) for host in TWITTER_HOSTS)


# ── Video download (tweet contains a video) ───────────────────────────────────

def _try_download_video(
    url: str, output_dir: Path, name_seed: str
) -> Tuple[Optional[Path], Dict[str, Optional[str]]]:
    """
    Attempt to download an embedded video from a tweet via yt-dlp.
    Returns (file_path, meta) on success, (None, meta) if no video is found,
    and raises on unexpected errors.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    base_path = output_dir / name_seed
    ydl_opts = {
        "outtmpl": f"{base_path}.%(ext)s",
        "quiet": True,
        "noprogress": True,
        "format": YTDLP_FORMAT,
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
    }
    cookie_opts = yt_dlp_cookie_options()
    if cookie_opts:
        ydl_opts.update(cookie_opts)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc).lower()
        # yt-dlp raises DownloadError when there is no video to download
        if any(phrase in msg for phrase in ("no video", "unsupported url", "this tweet", "no formats")):
            logger.info("No embedded video found in tweet: %s", url)
            cleanup_download_artifacts(base_path, None)
            return None, {"title": None, "platform": "twitter", "has_video": False}
        raise

    final_file = base_path.with_suffix(".mp4")
    if not final_file.exists():
        guessed = Path(str(base_path) + f".{info.get('ext', 'mp4')}")
        final_file = guessed if guessed.exists() else None
    cleanup_download_artifacts(
        base_path, final_file if final_file is not None and final_file.exists() else None
    )
    return final_file, {
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "platform": "twitter",
        "has_video": True,
    }


# ── Screenshot + mosaic animation (tweet has no video) ────────────────────────

def _screenshot_and_animate(url: str, output_dir: Path, name_seed: str) -> Optional[Path]:
    """
    Screenshot the tweet card and produce a mosaic-animated ProRes .mov.
    Returns the .mov path on success, None on failure.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    tmp_png = output_dir / f"{name_seed}_tmp.png"
    out_mov = output_dir / f"{name_seed}.mov"

    logger.info("Screenshotting tweet card: %s", url)
    result = subprocess.run(
        [PYTHON_BIN, str(TWEET_SCREENSHOT_SCRIPT), url, "-o", str(tmp_png)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.error("tweet_screenshot_1.py failed for %s:\n%s", url, result.stderr.strip())
        return None

    logger.info("Generating mosaic animation: %s", tmp_png.name)
    result = subprocess.run(
        [PYTHON_BIN, str(MOSAIC_VIDEO_SCRIPT), str(tmp_png), str(out_mov)],
        capture_output=True, text=True,
    )
    if tmp_png.exists():
        tmp_png.unlink()
    if result.returncode != 0:
        logger.error("tweet_screen_to_blur_mosaic_video.py failed:\n%s", result.stderr.strip())
        return None

    return out_mov if out_mov.exists() else None


# ── Unified per-tweet processor ───────────────────────────────────────────────

def process_tweet(
    url: str, output_dir: Path, name_seed: str
) -> Tuple[Optional[Path], Dict[str, Optional[str]]]:
    """
    Route a tweet URL to the right pipeline:
      - Has embedded video → yt-dlp download → .mp4
      - Text/image only   → screenshot + mosaic → .mov
    """
    file_path, meta = _try_download_video(url, output_dir, name_seed)
    if file_path is not None:
        logger.info("Tweet video downloaded: %s", file_path.name)
        return file_path, meta

    # No embedded video — fall back to screenshot + mosaic
    logger.info("No video in tweet; switching to screenshot pipeline: %s", url)
    mov_path = _screenshot_and_animate(url, output_dir, name_seed)
    meta["has_video"] = False
    return mov_path, meta


# ── CLI single-URL entry point ────────────────────────────────────────────────

def _process_single_url(url: str) -> int:
    cleaned_url = clean_url(url)
    parsed = urlparse(cleaned_url)
    if not parsed.scheme.startswith("http"):
        logger.error("Invalid URL (no http scheme): %s", url)
        return 1
    if not is_twitter_link(parsed.netloc):
        logger.error("Not a Twitter/X URL: %s", url)
        return 1

    parts = [p for p in parsed.path.split("/") if p]
    tweet_id = parts[-1] if parts else "tweet"
    output_dir = OUTPUT_DIR / "cli"

    file_path, meta = process_tweet(cleaned_url, output_dir, f"cli_{tweet_id}")
    if file_path is None:
        logger.error("Tweet processing failed.")
        return 1

    final_stem = build_insert_stem(
        1,
        artifact=None,
        source_candidates=(meta.get("uploader"), "twitter", parsed.netloc),
        netloc=parsed.netloc,
        title_hint=meta.get("title") or tweet_id,
        fallback_title=tweet_id,
    )
    file_path = rename_with_schema(file_path, final_stem)
    logger.info("Saved to: %s", file_path)
    return 0


# ── Batch entry point (called by unified_downloader) ─────────────────────────

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if len(sys.argv) > 1:
        return _process_single_url(sys.argv[1])

    if not TWEET_SCREENSHOT_SCRIPT.exists():
        logger.error("tweet_screenshot_1.py not found at %s", TWEET_SCREENSHOT_SCRIPT)
        return 1
    if not MOSAIC_VIDEO_SCRIPT.exists():
        logger.error("tweet_screen_to_blur_mosaic_video.py not found at %s", MOSAIC_VIDEO_SCRIPT)
        return 1

    try:
        html_path, _, links, target_dir, summary = prepare_processing_context()
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1

    twitter_tasks = []
    for index, link in enumerate(links, start=1):
        cleaned_url = clean_url(link.href)
        parsed = urlparse(cleaned_url)
        if not parsed.scheme.startswith("http"):
            continue
        if not is_twitter_link(parsed.netloc):
            continue
        twitter_tasks.append((index, cleaned_url, parsed, link.text.strip()))

    pending_tasks = [
        t for t in twitter_tasks
        if not summary_entry_is_complete("video", summary.entries.get(t[0]), url=t[1])
    ]

    if not pending_tasks:
        logger.info("All Twitter inserts already processed; skipping tweet downloader.")
        summary.save()
        return 0

    processed = 0
    for index, cleaned_url, parsed, label in pending_tasks:
        name_seed = f"{html_path.stem}_{index:02d}"
        file_path, meta = process_tweet(cleaned_url, target_dir, name_seed)
        if file_path is None:
            logger.warning("Skipping tweet %s (processing failed)", cleaned_url)
            continue

        final_stem = build_insert_stem(
            index,
            artifact=None,
            source_candidates=(meta.get("uploader"), "twitter", parsed.netloc),
            netloc=parsed.netloc,
            title_hint=meta.get("title") or label or f"tweet_{index}",
            fallback_title=label or f"Insert {index}",
        )
        file_path = rename_with_schema(file_path, final_stem)
        entry = build_summary_entry(
            label=label,
            url=cleaned_url,
            entry_type="video",
            source=parsed.netloc,
            extra=meta,
            downloaded_file=file_path,
        )
        summary.update_entry(index, entry)
        processed += 1

    summary.save()
    logger.info("Processed %d twitter inserts", processed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
