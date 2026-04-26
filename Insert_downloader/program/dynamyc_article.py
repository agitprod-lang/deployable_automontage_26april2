#!/usr/bin/env python3
"""
Generate short article cards as video clips.

Overlays title, author, date and an optional source logo on top of the
paper-folding background video between start_time and end_time.
Text is in Times New Roman, black, sized to fit inside the white paper area.
The alpha channel of the background is preserved in the output (ProRes 4444).
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import tempfile
import json
import re
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from PIL import Image, ImageDraw, ImageFont


logger = logging.getLogger("insert_downloader.dynamic_article")

_ROUND_CORNER_DIR = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/shared_assets/round_corner"
)


def _rounded_logo(logo_path: Path) -> Optional[Path]:
    """Return a temp PNG of the logo with rounded corners (caller must delete it)."""
    try:
        import sys as _sys
        if str(_ROUND_CORNER_DIR) not in _sys.path:
            _sys.path.insert(0, str(_ROUND_CORNER_DIR))
        from round_corner import round_corners  # type: ignore
        tmp = tempfile.NamedTemporaryFile(
            suffix="_rounded.png", delete=False, dir=tempfile.gettempdir()
        )
        tmp.close()
        round_corners(str(logo_path), tmp.name, radius_percent=20)
        return Path(tmp.name)
    except Exception as exc:
        logger.warning("Could not round logo corners: %s", exc)
        return None

SCRIPT_DIR = Path(__file__).resolve().parent
CODE_VIDEO_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_BACKGROUND = (
    SCRIPT_DIR
    / "background"
    / "GREEN SCREEN PAPER FOLDING ANIMATED HD FREE TO USE GRAPHICS ANIMATIONS.mov"
)
DEFAULT_BACKGROUND_AUDIO_SOURCE = (
    SCRIPT_DIR
    / "background"
    / "bakgroundarticlereplace.mov"
)

FFMPEG_BIN = os.environ.get("INSERT_DL_FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.environ.get("INSERT_DL_FFPROBE_BIN", "ffprobe")

MAX_TITLE_LINES = 4
LINE_SPACING = 18
TITLE_FONT_SIZE = 60
MIN_TITLE_FONT_SIZE = 30
DETAIL_FONT_SIZE = 34
MIN_DETAIL_FONT_SIZE = 22
LOGO_HEIGHT = 128        # px — max height for source logo above the title
LOGO_MAX_WIDTH = int(1920 * 0.20)  # 384px — 20% of canvas width, keeps aspect ratio
OUTPUT_CANVAS_WIDTH = 1920
OUTPUT_CANVAS_HEIGHT = 1080
CARD_TARGET_WIDTH_RATIO = 0.64
CARD_LEFT_MARGIN = 28
CARD_BOTTOM_MARGIN = 28
TITLE_SAFE_WIDTH_RATIO = 0.46
DETAIL_SAFE_WIDTH_RATIO = 0.54
STATIC_CARD_FRAME_TIME = 2.2

TIMES_NEW_ROMAN = "/System/Library/Fonts/Supplemental/Times New Roman.ttf"
DEFAULT_INSERT_AUDIO_REDUCTION_DB = -10.0
DEFAULT_INSERT_AUDIO_MULTIPLIER = math.pow(10.0, DEFAULT_INSERT_AUDIO_REDUCTION_DB / 20.0)


def _strip_emoji(text: str) -> str:
    """Remove emoji and other characters Times New Roman cannot render."""
    import unicodedata
    replacements = {
        "\u00a0": " ",
        "\u202f": " ",
        "«": "",
        "»": "",
        "“": "",
        "”": "",
        "‘": "",
        "’": "",
        "–": "-",
        "—": "-",
    }
    kept = []
    for ch in text:
        ch = replacements.get(ch, ch)
        if not ch:
            continue
        cp = ord(ch)
        # Keep Basic Latin, Latin-1 Supplement, Latin Extended A/B,
        # common punctuation blocks, and general whitespace.
        if cp <= 0x024F:          # Latin + extended Latin
            kept.append(ch)
        elif cp in (0x2018, 0x2019, 0x201C, 0x201D,  # curly quotes
                    0x2013, 0x2014,                    # en/em dash
                    0x2026,                            # ellipsis …
                    0x00B7,                            # middle dot ·
                    0x2022):                           # bullet •
            kept.append(ch)
        elif unicodedata.category(ch) in ("Zs",):     # other spaces
            kept.append(" ")
        # everything else (emoji, CJK, symbols…) is dropped
    # Collapse any runs of spaces left behind
    import re as _re
    cleaned = _re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ0-9\s,.;:!?()/-]", "", "".join(kept))
    return _re.sub(r" {2,}", " ", cleaned).strip()


def _fallback_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _extract_background_frame(background: Path, output_path: Path) -> Path:
    ffmpeg = _ffmpeg_bin()
    _run_ffmpeg(
        [
            ffmpeg,
            "-y",
            "-ss",
            f"{STATIC_CARD_FRAME_TIME:.2f}",
            "-i",
            str(background),
            "-frames:v",
            "1",
            str(output_path),
        ]
    )
    return output_path


def _detect_white_bounds(canvas: Image.Image) -> tuple[int, int, int, int]:
    px = canvas.load()
    min_x = canvas.width
    min_y = canvas.height
    max_x = 0
    max_y = 0
    for y in range(canvas.height):
        for x in range(canvas.width):
            r, g, b, a = px[x, y]
            if a > 200 and r > 220 and g > 220 and b > 220:
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                max_x = max(max_x, x)
                max_y = max(max_y, y)
    return min_x, min_y, max_x, max_y


def _render_static_card_frame(
    template_path: Path,
    output_path: Path,
    *,
    title: str,
    logo_path: Optional[Path],
    author: Optional[str],
    display_date: Optional[str],
) -> Optional[Path]:
    try:
        canvas = Image.open(template_path).convert("RGBA")
    except OSError as exc:
        logger.error("Could not open static card frame %s: %s", template_path, exc)
        return None


def _render_text_overlay(
    output_path: Path,
    *,
    title: str,
    author: Optional[str],
    display_date: Optional[str],
    logo_path: Optional[Path],
) -> Optional[Path]:
    canvas = Image.new("RGBA", (OUTPUT_CANVAS_WIDTH, OUTPUT_CANVAS_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    text_sections = _compose_text_block(title, author, display_date)
    title_text = str(text_sections["title"] or "Article")
    title_font_size = int(text_sections["title_size"])
    date_text = str(text_sections["date"])
    date_font_size = int(text_sections["date_size"])
    author_text = str(text_sections["author"])
    author_font_size = int(text_sections["author_size"])

    title_line_h = int(title_font_size * 1.2)
    date_line_h = int(date_font_size * 1.2) if date_text else 0
    author_line_h = int(author_font_size * 1.2) if author_text else 0
    section_gap = 16
    title_h_est = _line_count(title_text) * title_line_h + max(0, _line_count(title_text) - 1) * LINE_SPACING
    date_h_est = _line_count(date_text) * date_line_h + max(0, _line_count(date_text) - 1) * 10 if date_text else 0
    author_h_est = _line_count(author_text) * author_line_h + max(0, _line_count(author_text) - 1) * 10 if author_text else 0

    use_logo = logo_path and Path(logo_path).exists()
    text_h_est = title_h_est
    if date_h_est:
        text_h_est += section_gap + date_h_est
    if author_h_est:
        text_h_est += section_gap + author_h_est
    logo_gap = 20
    total_h = LOGO_HEIGHT + logo_gap + text_h_est if use_logo else text_h_est
    block_top = (OUTPUT_CANVAS_HEIGHT - total_h) // 2
    title_y = block_top + (LOGO_HEIGHT + logo_gap if use_logo else 0)
    date_y = title_y + title_h_est + (section_gap if date_h_est else 0)
    author_y = date_y + date_h_est + (section_gap if author_h_est and date_h_est else 0)

    if use_logo:
        logo = Image.open(logo_path).convert("RGBA")
        # Scale to fill the logo frame (LOGO_MAX_WIDTH × LOGO_HEIGHT), upscaling allowed.
        scale = min(
            LOGO_MAX_WIDTH / logo.width if logo.width else 1.0,
            LOGO_HEIGHT / logo.height if logo.height else 1.0,
        )
        logo = logo.resize(
            (max(1, int(logo.width * scale)), max(1, int(logo.height * scale))),
            Image.LANCZOS,
        )
        canvas.alpha_composite(logo, ((canvas.width - logo.width) // 2, block_top))

    title_font = _load_font(title_font_size)
    current_y = title_y
    for line in title_text.splitlines():
        width = _text_width(title_font, line)
        draw.text(((canvas.width - width) // 2, current_y), line, font=title_font, fill=(0, 0, 0, 230))
        current_y += title_line_h + LINE_SPACING

    if date_text:
        date_font = _load_font(date_font_size)
        for idx, line in enumerate(date_text.splitlines()):
            width = _text_width(date_font, line)
            draw.text(((canvas.width - width) // 2, date_y + idx * (date_line_h + 10)), line, font=date_font, fill=(0, 0, 0, 220))

    if author_text:
        author_font = _load_font(author_font_size)
        for idx, line in enumerate(author_text.splitlines()):
            width = _text_width(author_font, line)
            draw.text(((canvas.width - width) // 2, author_y + idx * (author_line_h + 10)), line, font=author_font, fill=(0, 0, 0, 220))

    try:
        canvas.save(output_path)
        return output_path
    except OSError as exc:
        logger.error("Could not save text overlay %s: %s", output_path, exc)
        return None

    draw = ImageDraw.Draw(canvas)
    box_left, box_top, box_right, box_bottom = _detect_white_bounds(canvas)
    inner_left = box_left + 135
    inner_right = box_right - 135
    inner_top = box_top + 150
    inner_bottom = box_bottom - 135
    inner_width = max(200, inner_right - inner_left)
    y_cursor = inner_top

    if logo_path and Path(logo_path).exists():
        try:
            logo = Image.open(logo_path).convert("RGBA")
            max_logo_width = min(150, inner_width // 3)
            max_logo_height = 70
            scale = min(
                max_logo_width / logo.width if logo.width else 1.0,
                max_logo_height / logo.height if logo.height else 1.0,
                1.0,
            )
            logo = logo.resize(
                (
                    max(1, int(logo.width * scale)),
                    max(1, int(logo.height * scale)),
                ),
                Image.LANCZOS,
            )
            x_pos = (canvas.width - logo.width) // 2
            canvas.alpha_composite(logo, (x_pos, box_top + 85))
            y_cursor = box_top + 85 + logo.height + 38
        except OSError as exc:
            logger.warning("Could not render logo %s: %s", logo_path, exc)

    title_text, title_font_size = _fit_text_block(
        title,
        initial_size=68,
        min_size=36,
        max_width=inner_width,
        max_lines=5,
    )
    title_font = _load_font(title_font_size)
    title_line_height = int(title_font_size * 1.12)
    for line in title_text.splitlines():
        text_width = _text_width(title_font, line)
        x_pos = (canvas.width - text_width) // 2
        draw.text((x_pos, y_cursor), line, font=title_font, fill=(0, 0, 0, 230))
        y_cursor += title_line_height + 10

    info_parts = [part for part in (author, display_date) if part]
    if info_parts:
        info_text, info_font_size = _fit_text_block(
            " • ".join(info_parts),
            initial_size=20,
            min_size=16,
            max_width=inner_width,
            max_lines=2,
        )
        info_font = _load_font(info_font_size)
        info_y = min(inner_bottom, max(y_cursor + 10, box_bottom - 95))
        for line in info_text.splitlines():
            text_width = _text_width(info_font, line)
            x_pos = (canvas.width - text_width) // 2
            draw.text((x_pos, info_y), line, font=info_font, fill=(0, 0, 0, 220))
            info_y += int(info_font_size * 1.2)

    try:
        canvas.save(output_path)
        return output_path
    except OSError as exc:
        logger.error("Could not save static card frame %s: %s", output_path, exc)
        return None


def _wrap_text(text: str, width: int, max_lines: int) -> list[str]:
    words = re.sub(r"\s+", " ", text.strip()).split(" ")
    if not words or words == [""]:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
            continue
        lines.append(current)
        current = word
    if current:
        lines.append(current)
    if len(lines) <= max_lines:
        return lines
    trimmed = lines[: max_lines - 1]
    overflow = " ".join(lines[max_lines - 1 :]).strip()
    if overflow:
        trimmed.append((overflow[: max(1, width - 1)] + "…").strip())
    return trimmed


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if Path(TIMES_NEW_ROMAN).exists():
        return ImageFont.truetype(TIMES_NEW_ROMAN, size=size)
    return ImageFont.load_default()


def _text_width(font: ImageFont.FreeTypeFont | ImageFont.ImageFont, text: str) -> int:
    left, _, right, _ = font.getbbox(text or " ")
    return max(0, right - left)


def _trim_line_to_width(
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> str:
    candidate = text.strip()
    if not candidate:
        return ""
    if _text_width(font, candidate) <= max_width:
        return candidate
    ellipsis = "…"
    while candidate and _text_width(font, candidate + ellipsis) > max_width:
        candidate = candidate[:-1].rstrip()
    return (candidate + ellipsis).strip() if candidate else ellipsis


def _wrap_text_to_pixels(
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    words = re.sub(r"\s+", " ", text.strip()).split(" ")
    if not words or words == [""]:
        return []

    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if current and _text_width(font, candidate) > max_width:
            lines.append(current)
            current = word
            if _text_width(font, current) > max_width:
                lines.append(_trim_line_to_width(current, font, max_width))
                current = ""
        else:
            current = candidate

    if current:
        lines.append(current)

    if len(lines) <= max_lines:
        return lines

    trimmed = lines[: max_lines - 1]
    overflow = " ".join(lines[max_lines - 1 :]).strip()
    trimmed.append(_trim_line_to_width(overflow, font, max_width))
    return trimmed


def _fit_text_block(
    text: Optional[str],
    *,
    initial_size: int,
    min_size: int,
    max_width: int,
    max_lines: int,
) -> tuple[str, int]:
    safe_text = _strip_emoji((text or "").strip())
    if not safe_text:
        return "", initial_size

    chosen_lines: list[str] = []
    chosen_size = min_size
    for size in range(initial_size, min_size - 1, -2):
        font = _load_font(size)
        candidate_lines = _wrap_text_to_pixels(safe_text, font, max_width, max_lines)
        if candidate_lines and len(candidate_lines) <= max_lines:
            chosen_lines = candidate_lines
            chosen_size = size
            break

    if not chosen_lines:
        font = _load_font(min_size)
        chosen_lines = _wrap_text_to_pixels(safe_text, font, max_width, max_lines)
        chosen_size = min_size

    return "\n".join(chosen_lines).strip(), chosen_size


def _compose_text_block(
    title: Optional[str],
    author: Optional[str],
    display_date: Optional[str],
) -> dict[str, str | int]:
    title_text, title_size = _fit_text_block(
        title or "Article",
        initial_size=TITLE_FONT_SIZE,
        min_size=MIN_TITLE_FONT_SIZE,
        max_width=int(OUTPUT_CANVAS_WIDTH * TITLE_SAFE_WIDTH_RATIO),
        max_lines=MAX_TITLE_LINES,
    )
    date_text, detail_size = _fit_text_block(
        display_date,
        initial_size=DETAIL_FONT_SIZE,
        min_size=MIN_DETAIL_FONT_SIZE,
        max_width=int(OUTPUT_CANVAS_WIDTH * DETAIL_SAFE_WIDTH_RATIO),
        max_lines=2,
    )
    author_text, author_size = _fit_text_block(
        author,
        initial_size=DETAIL_FONT_SIZE,
        min_size=MIN_DETAIL_FONT_SIZE,
        max_width=int(OUTPUT_CANVAS_WIDTH * DETAIL_SAFE_WIDTH_RATIO),
        max_lines=3,
    )
    return {
        "title": title_text or "Article",
        "title_size": title_size,
        "date": date_text,
        "date_size": detail_size,
        "author": author_text,
        "author_size": author_size,
    }


def _write_temp_text(lines: str) -> Path:
    temp = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".txt")
    with temp:
        temp.write(lines)
    return Path(temp.name)


def _line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + 1


def _ffmpeg_bin() -> str:
    if shutil.which(FFMPEG_BIN):
        return FFMPEG_BIN
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    raise FileNotFoundError("ffmpeg binary not found on PATH.")


def _ffprobe_bin() -> str:
    if shutil.which(FFPROBE_BIN):
        return FFPROBE_BIN
    if shutil.which("ffprobe"):
        return "ffprobe"
    raise FileNotFoundError("ffprobe binary not found on PATH.")


def _run_ffmpeg(cmd: Iterable[str]) -> None:
    try:
        subprocess.run(list(cmd), check=True, capture_output=False)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffmpeg failed with exit code {exc.returncode}") from exc


def _probe_duration_seconds(media_path: Path) -> float:
    ffprobe = _ffprobe_bin()
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(media_path),
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        payload = json.loads(result.stdout or "{}")
        duration_value = payload.get("format", {}).get("duration")
        duration = float(duration_value)
    except (subprocess.CalledProcessError, ValueError, json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(f"Could not determine media duration for {media_path}") from exc
    if duration <= 0:
        raise RuntimeError(f"Media duration must be positive for {media_path}")
    return duration


def create_title_card_video(
    title: Optional[str],
    output_path: Path,
    *,
    author: Optional[str] = None,
    display_date: Optional[str] = None,
    background_video: Optional[Path] = None,
    logo_path: Optional[Path] = None,
    start_time: float = 1.0,
    end_time: float = 5.0,
    audio_source: Optional[Path] = DEFAULT_BACKGROUND_AUDIO_SOURCE,
    skip_audio: bool = False,
) -> Optional[Path]:
    """
    Render a short video with title/author/date (and optional logo) overlaid
    on the paper-folding background between start_time and end_time.
    Output is ProRes 4444 with alpha preserved.
    """

    background = Path(background_video) if background_video else DEFAULT_BACKGROUND
    if not background.exists():
        logger.error("Background video not found: %s", background)
        return None
    if not skip_audio:
        if audio_source is None:
            logger.error("Article background audio is required but no source was provided.")
            return None
        audio_source = Path(audio_source)
        if not audio_source.exists():
            logger.error("Article background audio source not found: %s", audio_source)
            return None

    if output_path.suffix.lower() not in {".mov", ".mp4", ".m4v"}:
        output_path = output_path.with_suffix(".mov")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg = _ffmpeg_bin()
    try:
        clip_duration = _probe_duration_seconds(background)
    except (FileNotFoundError, RuntimeError) as exc:
        logger.error("Could not inspect background video %s: %s", background, exc)
        return None
    card_target_width = int(round(OUTPUT_CANVAS_WIDTH * CARD_TARGET_WIDTH_RATIO))
    card_target_height = int(round(card_target_width * OUTPUT_CANVAS_HEIGHT / OUTPUT_CANVAS_WIDTH))
    card_overlay_y = max(0, OUTPUT_CANVAS_HEIGHT - card_target_height - CARD_BOTTOM_MARGIN)

    sanitized_title = _strip_emoji((title or "").strip()) or "Article"
    sanitized_author = _strip_emoji((author or "").strip()) or None
    sanitized_display_date = _strip_emoji((display_date or "").strip()) or None

    stable_start = max(start_time, 1.6)
    stable_end = min(end_time, max(stable_start + 0.2, clip_duration - 1.0))
    enable = f"between(t,{stable_start:.2f},{stable_end:.2f})"
    overlay_png = Path(tempfile.NamedTemporaryFile(suffix=".png", delete=False).name)
    rendered_overlay = _render_text_overlay(
        overlay_png,
        title=sanitized_title,
        author=sanitized_author,
        display_date=sanitized_display_date,
        logo_path=Path(logo_path) if logo_path and Path(logo_path).exists() else None,
    )
    if not rendered_overlay:
        overlay_png.unlink(missing_ok=True)
        return None

    filter_complex = (
        f"[0:v]format=yuva444p10le,split=2[base][canvas_src];"
        f"[canvas_src]colorchannelmixer=aa=0[canvas];"
        f"[1:v]format=rgba[overlay_rgba];"
        f"[base][overlay_rgba]overlay=x=0:y=0:enable='{enable}'[card];"
        f"[card]scale={card_target_width}:{card_target_height}[card_scaled];"
        f"[canvas][card_scaled]overlay=x={CARD_LEFT_MARGIN}:y={card_overlay_y}:format=auto[out]"
    )
    cmd = [ffmpeg, "-y", "-i", str(background), "-loop", "1", "-i", str(rendered_overlay)]
    if not skip_audio and audio_source is not None:
        filter_complex += (
            f";[2:a]volume={DEFAULT_INSERT_AUDIO_MULTIPLIER:.6f},"
            f"atrim=0:{clip_duration:.6f},asetpts=PTS-STARTPTS,"
            f"apad=pad_dur={clip_duration:.6f},atrim=0:{clip_duration:.6f}[audio]"
        )
        cmd.extend(["-i", str(audio_source)])

    cmd.extend(["-filter_complex", filter_complex, "-map", "[out]"])
    if not skip_audio and audio_source is not None:
        cmd.extend(["-map", "[audio]", "-c:a", "aac", "-b:a", "192k"])
    else:
        cmd.append("-an")
    cmd.extend([
        "-t", f"{clip_duration:.6f}",
        "-c:v", "prores_ks",
        "-profile:v", "4",
        "-pix_fmt", "yuva444p10le",
        str(output_path),
    ])

    try:
        _run_ffmpeg(cmd)
    except (FileNotFoundError, RuntimeError) as exc:
        logger.error("Could not create dynamic card %s: %s", output_path.name, exc)
        output_path.unlink(missing_ok=True)
        return None
    finally:
        overlay_png.unlink(missing_ok=True)

    return output_path


__all__ = ["create_title_card_video"]
