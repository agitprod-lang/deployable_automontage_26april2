#!/usr/bin/env python3
"""
Cut downloaded video inserts using timecodes found near video links in the HTML.

For each video link (YouTube, Dailymotion, Twitter/X, Facebook…) in the HTML
that has a time indication in its surrounding text, cut the corresponding
downloaded video with ffmpeg.

Supported time indication formats (before or after the link, optionally in
parentheses or square brackets):

  Range:
    (01:00-01:15)   [1:00 - 1:15]   1:00-1:10   00:01:00 - 00:01:15
    1:00 to 1:15    1:00 à 1:15     1:00 a 1:15
    0h1m0s-0h1m15s  1 heure 2 minutes 3 secondes to 1h1m15s

  Start only (play from TC to end of video):
    1:00    1:00-    1min    1min-

  End only (keep from 0 to TC):
    -1:10   -1min10s

Usage:
  python3.11 cut_video_with_timecode_0.py [--html PATH] [--output-dir PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

try:
    from bs4 import BeautifulSoup, NavigableString, Tag
except ModuleNotFoundError:
    print(
        "ERROR: beautifulsoup4 is required. Install with: pip install beautifulsoup4",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SELF = Path(__file__).resolve()
# parents[0]=cut_video_with_timecode/ parents[1]=program/ parents[2]=Insert_downloader/
INSERT_DOWNLOADER_DIR = _SELF.parents[2]
BASE_DIR = INSERT_DOWNLOADER_DIR.parent           # …/deployable_auto-montage
INPUT_DIR = INSERT_DOWNLOADER_DIR / "input"
OUTPUT_DIR = INSERT_DOWNLOADER_DIR / "output"
FALLBACK_INPUT_DIR = BASE_DIR / "swisser" / "Universal_pipe" / "html"

VIDEO_HOST_KEYWORDS = (
    "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com",
    "twitter.com", "x.com", "facebook.com", "fb.watch",
    "instagram.com", "tiktok.com", "vm.tiktok.com",
    "reddit.com", "rumble.com", "odysee.com",
)

FFMPEG_BIN = "ffmpeg"
CONTEXT_WINDOW = 300  # chars on each side of the link to search for timecodes


# ---------------------------------------------------------------------------
# Timecode parsing
# ---------------------------------------------------------------------------

# Colon-separated: MM:SS or HH:MM:SS (1-2 digit parts)
_COLON_TC_RE = re.compile(r"(?<!\d)(\d{1,2}:\d{2}(?::\d{2})?)(?!\d)")

# Human-readable: e.g. "1h2m3s", "1 heure 2 minutes 3 secondes"
_HUMAN_TC_RE = re.compile(
    r"(?:(?P<h>\d+)\s*h(?:eure?s?|ours?)?\s*)?"
    r"(?:(?P<m>\d+)\s*m(?:in(?:utes?)?)?\s*)?"
    r"(?:(?P<s>\d+(?:\.\d+)?)\s*s(?:ec(?:ondes?|s)?)?)?",
    re.IGNORECASE,
)

# Range separators between two timecodes
_SEP_RE = re.compile(r"\s*(?:-|to\b|à\b|a\b)\s*", re.IGNORECASE)

# Content inside () or []
_BRACKET_RE = re.compile(r"[\(\[]\s*(.*?)\s*[\)\]]", re.DOTALL)


def _parse_colon_tc(token: str) -> Optional[float]:
    parts = token.strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 1:
        return nums[0]
    return None


def _parse_human_tc(text: str) -> Optional[float]:
    m = _HUMAN_TC_RE.fullmatch(text.strip())
    if not m or not any(m.group(g) for g in ("h", "m", "s")):
        return None
    h = float(m.group("h") or 0)
    mins = float(m.group("m") or 0)
    s = float(m.group("s") or 0)
    result = h * 3600 + mins * 60 + s
    return result if result > 0 else None


def _find_timecodes(text: str) -> List[Tuple[float, int, int]]:
    """Return list of (seconds, start_char_pos, end_char_pos) found in text."""
    results: List[Tuple[float, int, int]] = []
    covered: List[Tuple[int, int]] = []

    def _overlaps(a: int, b: int) -> bool:
        return any(not (b <= s or a >= e) for s, e in covered)

    # Colon timecodes first (highest precision)
    for m in _COLON_TC_RE.finditer(text):
        val = _parse_colon_tc(m.group(1))
        if val is None:
            continue
        if _overlaps(m.start(), m.end()):
            continue
        results.append((val, m.start(), m.end()))
        covered.append((m.start(), m.end()))

    # Human timecodes – scan for any h/m/s sequence not already matched
    for m in _HUMAN_TC_RE.finditer(text):
        if not any(m.group(g) for g in ("h", "m", "s")):
            continue
        if _overlaps(m.start(), m.end()):
            continue
        val = _parse_human_tc(m.group(0))
        if val is None:
            continue
        results.append((val, m.start(), m.end()))
        covered.append((m.start(), m.end()))

    results.sort(key=lambda x: x[1])
    return results


def parse_time_range(text: str) -> Optional[Tuple[Optional[float], Optional[float]]]:
    """
    Parse a time range from text.
    Returns (start_sec, end_sec) where either may be None:
      (X, Y)     → cut from X to Y
      (X, None)  → start from X, play to end
      (None, Y)  → play from 0 to Y
    Returns None when no valid timecode is found.
    """
    text = text.strip()
    if not text:
        return None

    # "-TC" pattern → end-only
    if text.startswith("-"):
        rest = text[1:].strip()
        tcs = _find_timecodes(rest)
        if tcs:
            return (None, tcs[0][0])

    tcs = _find_timecodes(text)

    if not tcs:
        return None

    if len(tcs) >= 2:
        # Two timecodes → range
        return (tcs[0][0], tcs[1][0])

    # Single timecode
    tc_val, _, tc_end = tcs[0]
    trailing = text[tc_end:].strip()
    # "TC-" → start-only (trailing dash = "to end")
    if trailing.startswith("-"):
        return (tc_val, None)
    # Plain "TC" → start-only
    return (tc_val, None)


def _extract_candidate_texts(surrounding: str) -> List[str]:
    """
    Return candidate strings to test for timecodes.
    Prioritise bracketed content; fall back to the full surrounding text.
    """
    candidates: List[str] = []
    for m in _BRACKET_RE.finditer(surrounding):
        inner = m.group(1).strip()
        if inner:
            candidates.append(inner)
    candidates.append(surrounding)
    return candidates


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _is_video_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(kw in host for kw in VIDEO_HOST_KEYWORDS)


def _clean_url(raw: str) -> str:
    url = raw.strip()
    parsed = urlparse(url)
    if parsed.netloc.endswith("google.com") and parsed.path == "/url":
        target = parse_qs(parsed.query).get("q", [None])[0]
        if target:
            return unquote(target)
    return url


_BLOCK_TAGS = {"p", "div", "li", "td", "th", "section", "article", "blockquote", "dd", "dt"}


def _sibling_text(node, direction: str, max_chars: int = CONTEXT_WINDOW) -> str:
    """Collect text from NavigableString/Tag siblings in one direction."""
    collected: List[str] = []
    total = 0
    sibling = getattr(node, f"{direction}_sibling", None)
    while sibling is not None and total < max_chars:
        if isinstance(sibling, NavigableString):
            chunk = str(sibling)
            collected.append(chunk)
            total += len(chunk)
        elif isinstance(sibling, Tag):
            chunk = sibling.get_text(" ", strip=True)
            collected.append(chunk)
            total += len(chunk)
        sibling = getattr(sibling, f"{direction}_sibling", None)
    if direction == "previous":
        collected.reverse()
    return " ".join(collected)


def _surrounding_text(tag: Tag) -> str:
    # Prefer the full text of the nearest block-level ancestor (e.g. <p>),
    # which captures timecodes nested in sibling <span>/<em>/… elements.
    block = tag.find_parent(_BLOCK_TAGS)
    if block:
        return block.get_text(" ", strip=True)
    # Fallback: direct siblings of the <a> and its immediate parent
    before = _sibling_text(tag, "previous")[-CONTEXT_WINDOW:]
    link_text = tag.get_text(" ", strip=True)
    after_direct = _sibling_text(tag, "next")[:CONTEXT_WINDOW]
    parent_after = _sibling_text(tag.parent, "next")[:CONTEXT_WINDOW] if tag.parent else ""
    return f"{before} {link_text} {after_direct} {parent_after}"


# ---------------------------------------------------------------------------
# HTML → video link + timecode
# ---------------------------------------------------------------------------

@dataclass
class VideoInstruction:
    url: str
    link_text: str
    start: Optional[float]
    end: Optional[float]
    link_index: int  # 1-based order of video links in the document


def extract_video_instructions(html_text: str) -> List[VideoInstruction]:
    soup = BeautifulSoup(html_text, "html.parser")
    instructions: List[VideoInstruction] = []
    video_index = 0

    for tag in soup.find_all("a", href=True):
        raw_href = tag.get("href", "")
        url = _clean_url(raw_href)
        if not _is_video_url(url):
            continue
        video_index += 1

        surrounding = _surrounding_text(tag)
        candidates = _extract_candidate_texts(surrounding)

        parsed: Optional[Tuple[Optional[float], Optional[float]]] = None
        for candidate in candidates:
            result = parse_time_range(candidate)
            if result is not None:
                parsed = result
                break

        if parsed is None:
            continue  # no timecode found near this link

        start, end = parsed
        instructions.append(
            VideoInstruction(
                url=url,
                link_text=tag.get_text(" ", strip=True),
                start=start,
                end=end,
                link_index=video_index,
            )
        )

    return instructions


# ---------------------------------------------------------------------------
# Metadata JSON → URL→file mapping
# ---------------------------------------------------------------------------

def load_url_to_file_map(output_dir: Path) -> Dict[str, Path]:
    """
    Load the metadata JSON written by video_downloader / unified_downloader.
    Returns {source_url: absolute_path_to_downloaded_file}.
    """
    mapping: Dict[str, Path] = {}
    json_files = list(output_dir.glob("*_metadata.json"))
    if not json_files:
        return mapping
    meta_path = sorted(json_files, key=lambda p: p.stat().st_mtime)[-1]
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return mapping
    base = INSERT_DOWNLOADER_DIR
    for entry in data.get("entries", []):
        url = entry.get("source_url")
        rel = entry.get("downloaded_file")
        if url and rel:
            abs_path = (base / rel).resolve()
            if abs_path.exists():
                mapping[url] = abs_path
    return mapping


def find_video_by_index(output_dir: Path, video_index: int) -> Optional[Path]:
    """Index-based fallback: look for *_<NN>_video* or *_<N>_video* files."""
    for pattern in (f"*_{video_index:02d}_video*", f"*_{video_index}_video*"):
        candidates = sorted(p for p in output_dir.glob(pattern) if p.is_file())
        if candidates:
            return candidates[0]
    return None


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------

def _format_ts(value: float) -> str:
    ms = int(round(value * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1_000)
    if ms:
        return f"{h:02}:{m:02}:{s:02}.{ms:03}"
    return f"{h:02}:{m:02}:{s:02}"


def build_cut_command(
    src: Path,
    dest: Path,
    start: Optional[float],
    end: Optional[float],
    ffmpeg_bin: str = FFMPEG_BIN,
) -> List[str]:
    cmd = [ffmpeg_bin, "-y"]
    if start and start > 0:
        cmd += ["-ss", _format_ts(start)]
    cmd += ["-i", str(src)]
    if end is not None:
        # Use -to with absolute position when there's a seek; otherwise -t
        if start and start > 0:
            # After -ss fast-seek, timestamps are re-based to 0 → use -t (duration)
            duration = max(0.0, end - (start or 0.0))
            cmd += ["-t", _format_ts(duration)]
        else:
            cmd += ["-to", _format_ts(end)]
    cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "fast", "-c:a", "aac", str(dest)]
    return cmd


def _temp_path(src: Path) -> Path:
    suffix = src.suffix
    stem = src.name[: -len(suffix)] if suffix else src.name
    return src.with_name(f"{stem}_tmp_cutting{suffix}")


def run_cut_inplace(cmd: List[str], original: Path, tmp: Path) -> None:
    """Run ffmpeg to tmp, then atomically replace the original on success."""
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        if tmp.exists():
            tmp.unlink()
        raise SystemExit(f"ffmpeg failed (exit {exc.returncode})") from exc
    tmp.replace(original)


# ---------------------------------------------------------------------------
# HTML auto-detection
# ---------------------------------------------------------------------------

def _find_latest_html(directory: Path) -> Optional[Path]:
    if not directory.exists():
        return None
    files = sorted(
        (p for p in directory.iterdir() if p.suffix.lower() in {".html", ".htm"}),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def pick_html_file(explicit: Optional[Path] = None) -> Path:
    if explicit:
        if not explicit.exists():
            raise FileNotFoundError(f"HTML file not found: {explicit}")
        return explicit
    html = _find_latest_html(INPUT_DIR) or _find_latest_html(FALLBACK_INPUT_DIR)
    if html:
        return html
    raise FileNotFoundError(
        f"No HTML found in {INPUT_DIR} or {FALLBACK_INPUT_DIR}. Use --html."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cut downloaded videos using timecodes found near video links in the HTML."
    )
    parser.add_argument("--html", type=Path, default=None, help="HTML input file (auto-detected if omitted).")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory containing downloaded videos (default: Insert_downloader/output/<html_stem>/).",
    )
    parser.add_argument("--ffmpeg-bin", default=FFMPEG_BIN, help="ffmpeg binary to use.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        html_path = pick_html_file(args.html)
    except FileNotFoundError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    print(f"[info] HTML: {html_path}")
    html_text = html_path.read_text(encoding="utf-8", errors="ignore")
    instructions = extract_video_instructions(html_text)

    if not instructions:
        print("[info] No video links with timecodes found.")
        return 0

    output_dir = args.output_dir or (OUTPUT_DIR / html_path.stem)
    if not output_dir.exists():
        print(f"[warn] Output directory does not exist: {output_dir}", file=sys.stderr)
        print("[warn] Run the video downloader first, or pass --output-dir.", file=sys.stderr)
        return 1

    url_map = load_url_to_file_map(output_dir)
    cut_count = 0

    for instr in instructions:
        print(
            f"\n[info] Link #{instr.link_index}: {instr.url}\n"
            f"       text='{instr.link_text}'\n"
            f"       start={instr.start}s  end={instr.end}s"
        )

        video_path = url_map.get(instr.url)
        if video_path is None:
            video_path = find_video_by_index(output_dir, instr.link_index)
        if video_path is None:
            print(f"[warn] No downloaded file found for link #{instr.link_index}; skipping.")
            continue

        tmp = _temp_path(video_path)
        cmd = build_cut_command(video_path, tmp, instr.start, instr.end, args.ffmpeg_bin)
        print(f"[cmd ] {' '.join(shlex.quote(p) for p in cmd)}")
        print(f"       → will overwrite {video_path.name} in-place")

        if args.dry_run:
            continue

        run_cut_inplace(cmd, video_path, tmp)
        print(f"[ok  ] {video_path.name} replaced with cut version")
        cut_count += 1

    if args.dry_run:
        print("\n[info] Dry run — no files written.")
    else:
        print(f"\n[info] Done. {cut_count} video(s) cut.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
