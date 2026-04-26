#!/usr/bin/env python3
"""
Download all video inserts defined in the latest HTML input.
Keeps numbering aligned with source document regardless of execution order.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import DownloadError

from downloader_utils import (
    YTDLP_FORMAT,
    YTDLP_PROGRESSIVE_FALLBACK_FORMAT,
    build_insert_stem,
    build_summary_entry,
    clean_url,
    cleanup_download_artifacts,
    is_video_url,
    prepare_processing_context,
    rename_with_schema,
    yt_dlp_cookie_options,
    summary_entry_is_complete,
)

logger = logging.getLogger("insert_downloader.video")

TWITTER_HOSTS = ("twitter.com", "www.twitter.com", "mobile.twitter.com", "x.com", "www.x.com")
JS_RUNTIME_CANDIDATES = {
    "node": "node",
    "deno": "deno",
    "bun": "bun",
    "quickjs": "qjs",
}
REMOTE_COMPONENTS = {"ejs:github"}
FFPROBE_BIN = os.environ.get("INSERT_DL_FFPROBE_BIN", "ffprobe")
FFMPEG_BIN = os.environ.get("INSERT_DL_FFMPEG_BIN", "ffmpeg")
PREMIERE_SAFE_CODECS = {"h264", "avc1"}
PREMIERE_SAFE_PIXEL_FORMATS = {"yuv420p", "yuvj420p"}
PREMIERE_CRF = os.environ.get("INSERT_DL_H264_CRF", "18")
PREMIERE_PRESET = os.environ.get("INSERT_DL_H264_PRESET", "medium")
PREMIERE_AUDIO_BITRATE = os.environ.get("INSERT_DL_AAC_BITRATE", "192k")


def _discover_js_runtimes() -> Dict[str, Dict[str, str]]:
    runtimes: Dict[str, Dict[str, str]] = {}
    for name, executable in JS_RUNTIME_CANDIDATES.items():
        path = shutil.which(executable)
        if path:
            runtimes[name] = {"path": path}
    if runtimes:
        logger.debug(
            "Enabled JavaScript runtimes for yt-dlp: %s",
            ", ".join(f"{name} ({cfg['path']})" for name, cfg in runtimes.items()),
        )
    else:
        logger.debug("No JavaScript runtimes detected; yt-dlp will fall back to JS-less mode")
    return runtimes


AVAILABLE_JS_RUNTIMES = _discover_js_runtimes()


def _should_use_progressive_fallback(error: Exception) -> bool:
    if not YTDLP_PROGRESSIVE_FALLBACK_FORMAT:
        return False
    message = str(error)
    return "HTTP Error 403" in message or "403: Forbidden" in message


def _probe_video_stream(file_path: Path) -> Optional[Dict[str, str]]:
    cmd = [
        FFPROBE_BIN,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,pix_fmt",
        "-of",
        "json",
        str(file_path),
    ]
    try:
        result = subprocess.run(
            cmd, check=True, capture_output=True, text=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning("ffprobe failed for %s: %s", file_path.name, exc)
        return None
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        logger.warning("ffprobe produced invalid JSON for %s: %s", file_path.name, exc)
        return None
    streams = payload.get("streams") or []
    if not streams:
        return None
    stream = streams[0]
    stream["codec_name"] = (stream.get("codec_name") or "").lower()
    stream["pix_fmt"] = (stream.get("pix_fmt") or "").lower()
    return stream


def _needs_premiere_transcode(stream: Optional[Dict[str, str]]) -> bool:
    if not stream:
        return True
    codec = stream.get("codec_name")
    pix_fmt = stream.get("pix_fmt")
    if codec == "prores" and pix_fmt and pix_fmt.startswith("yuva"):
        return False
    if codec not in PREMIERE_SAFE_CODECS:
        return True
    if not pix_fmt:
        return True
    return pix_fmt not in PREMIERE_SAFE_PIXEL_FORMATS


def _transcode_to_premiere_safe(file_path: Path) -> bool:
    temp_path = file_path.with_name(f"{file_path.stem}_premiere{file_path.suffix}")
    if temp_path.exists():
        try:
            temp_path.unlink()
        except OSError:
            pass
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-i",
        str(file_path),
        "-c:v",
        "libx264",
        "-preset",
        PREMIERE_PRESET,
        "-crf",
        PREMIERE_CRF,
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "high",
        "-movflags",
        "+faststart",
        "-c:a",
        "aac",
        "-b:a",
        PREMIERE_AUDIO_BITRATE,
        str(temp_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.error("ffmpeg transcoding failed for %s: %s", file_path.name, exc)
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        return False
    try:
        file_path.unlink()
        temp_path.rename(file_path)
    except OSError as exc:
        logger.error("Could not finalize transcoded file for %s: %s", file_path.name, exc)
        return False
    return True


def ensure_premiere_safe(file_path: Optional[Path]) -> Dict[str, Optional[str]]:
    if file_path is None or not file_path.exists():
        return {}
    stream = _probe_video_stream(file_path)
    if not _needs_premiere_transcode(stream):
        return {}
    logger.info("Transcoding %s to Premiere-safe H.264/AAC", file_path.name)
    if _transcode_to_premiere_safe(file_path):
        return {"premiere_transcode": "success"}
    return {"premiere_transcode": "failed"}


def download_video(
    url: str, output_dir: Path, name_seed: str
) -> Tuple[Optional[Path], Dict[str, Optional[str]]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    base_path = output_dir / name_seed
    output_template = f"{base_path}.%(ext)s"
    base_opts = {
        "outtmpl": output_template,
        "quiet": False,
        "noprogress": True,
        "merge_output_format": "mp4",
        "postprocessors": [
            {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
        ],
    }
    cookie_opts = yt_dlp_cookie_options()
    if cookie_opts:
        base_opts.update(cookie_opts)
    if AVAILABLE_JS_RUNTIMES:
        base_opts["js_runtimes"] = {
            name: cfg.copy() for name, cfg in AVAILABLE_JS_RUNTIMES.items()
        }
        base_opts["remote_components"] = set(REMOTE_COMPONENTS)

    def _extract_with_format(format_selector: str):
        opts = dict(base_opts)
        opts["format"] = format_selector
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    logger.info("Downloading video: %s", url)
    used_progressive_fallback = False
    try:
        info = _extract_with_format(YTDLP_FORMAT)
    except DownloadError as exc:
        if _should_use_progressive_fallback(exc):
            logger.warning(
                "Primary DASH download failed for %s (HTTP 403). Retrying with progressive fallback.",
                url,
            )
            cleanup_download_artifacts(base_path, None)
            try:
                info = _extract_with_format(YTDLP_PROGRESSIVE_FALLBACK_FORMAT)
                used_progressive_fallback = True
            except Exception as fallback_exc:  # noqa: BLE001
                logger.error("Progressive fallback failed for %s: %s", url, fallback_exc)
                cleanup_download_artifacts(base_path, None)
                return None, {"title": None, "error": str(fallback_exc)}
        else:
            logger.error("yt-dlp failed for %s: %s", url, exc)
            cleanup_download_artifacts(base_path, None)
            return None, {"title": None, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.error("yt-dlp failed for %s: %s", url, exc)
        cleanup_download_artifacts(base_path, None)
        return None, {"title": None, "error": str(exc)}

    final_file = base_path.with_suffix(".mp4")
    if not final_file.exists():
        guessed = Path(output_template % {"ext": info.get("ext", "mp4")})
        if guessed.exists():
            final_file = guessed
        else:
            final_file = None
    cleanup_download_artifacts(
        base_path, final_file if final_file is not None and final_file.exists() else None
    )
    video_meta: Dict[str, Optional[str]] = {
        "title": info.get("title"),
        "uploader": info.get("uploader"),
    }
    if used_progressive_fallback:
        video_meta["download_strategy"] = "progressive_fallback"
    return final_file, video_meta


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        html_path, _, links, target_dir, summary = prepare_processing_context()
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1

    processed = 0
    video_tasks = []
    for index, link in enumerate(links, start=1):
        cleaned_url = clean_url(link.href)
        parsed = urlparse(cleaned_url)
        if not parsed.scheme.startswith("http"):
            continue
        if not is_video_url(cleaned_url):
            continue
        if any(parsed.netloc.lower().endswith(host) for host in TWITTER_HOSTS):
            continue
        label = link.text.strip()
        source = parsed.netloc or None
        video_tasks.append((index, cleaned_url, parsed, label, source))

    pending_tasks = []
    for index, cleaned_url, parsed, label, source in video_tasks:
        entry = summary.entries.get(index)
        if summary_entry_is_complete("video", entry, url=cleaned_url):
            continue
        pending_tasks.append((index, cleaned_url, parsed, label, source))

    if not pending_tasks:
        logger.info("All non-Twitter video inserts already processed; skipping video downloader.")
        summary.save()
        return 0

    for index, cleaned_url, parsed, label, source in pending_tasks:
        name_seed = f"{html_path.stem}_{index:02d}_video"
        file_path, video_meta = download_video(cleaned_url, target_dir, name_seed)
        video_meta.update(ensure_premiere_safe(file_path))
        final_stem = build_insert_stem(
            index,
            artifact=None,
            source_candidates=(
                video_meta.get("uploader"),
                video_meta.get("platform"),
                source,
            ),
            netloc=parsed.netloc,
            title_hint=video_meta.get("title") or label,
            fallback_title=label or f"Insert {index}",
        )
        file_path = rename_with_schema(file_path, final_stem)
        entry = build_summary_entry(
            label=label,
            url=cleaned_url,
            entry_type="video",
            source=source,
            extra=video_meta,
            downloaded_file=file_path,
        )
        summary.update_entry(index, entry)
        processed += 1

    summary.save()
    logger.info("Processed %d video inserts", processed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
