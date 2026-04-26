#!/usr/bin/env python3
"""
Utility to trim downloaded inserts/extracts according to the timestamps
embedded in the latest swisser HTML briefing.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Iterable, List, Optional

try:
    from bs4 import BeautifulSoup  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    BeautifulSoup = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VIDEO_ROOT = PROJECT_ROOT / "output"
DEFAULT_HTML_DIR = PROJECT_ROOT.parent / "swisser" / "get_html" / "output"


@dataclass
class CutInstruction:
    label: str
    index: int
    start: float
    end: Optional[float]
    source: str

    @property
    def duration(self) -> Optional[float]:
        if self.end is None:
            return None
        return max(0, self.end - self.start)


LINE_RE = re.compile(r"^(INSERT|EXTRAIT)\s+(\d+)(.*)$", re.IGNORECASE)
COLON_TIME_RE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")
HUMAN_TIME_RE = re.compile(
    r"(?:(?P<h>\d+)\s*h\s*)?(?:(?P<m>\d+)\s*m\s*)?(?P<s>\d+(?:\.\d+)?)\s*(?:s|sec)",
    re.IGNORECASE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cut downloaded inserts based on HTML instructions.")
    parser.add_argument("--html-dir", type=Path, default=DEFAULT_HTML_DIR, help="Directory containing HTML scripts.")
    parser.add_argument(
        "--video-root",
        type=Path,
        default=DEFAULT_VIDEO_ROOT,
        help="Root directory containing per-script video folders.",
    )
    parser.add_argument("--ffmpeg-bin", default="ffmpeg", help="ffmpeg binary to use.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the ffmpeg commands that would run without touching the files.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        html_path = find_latest_html(args.html_dir)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc))

    print(f"[info] Latest HTML: {html_path}")
    session_id = html_path.stem
    session_dir = args.video_root / session_id
    if not session_dir.exists():
        raise SystemExit(f"No video folder found for {session_id} under {args.video_root}")

    lines = extract_text_lines(html_path.read_text(encoding="utf-8", errors="ignore"))
    instructions = parse_instructions(lines)
    if not instructions:
        print("[info] No cut instructions found in the HTML.")
        return

    cut_count = 0
    for instruction in instructions:
        video_path = find_video_file(session_dir, instruction.index)
        if not video_path:
            print(f"[warn] No video file found for {instruction.label} {instruction.index:02d}")
            continue

        output_path = build_output_path(video_path)
        ensure_parent(output_path)
        cmd = build_ffmpeg_command(
            args.ffmpeg_bin,
            video_path,
            output_path,
            instruction.start,
            instruction.duration,
        )
        print(f"[info] {instruction.label} {instruction.index:02d}: {instruction.source}")
        print(f"[cmd ] {' '.join(shlex.quote(part) for part in cmd)}")
        if args.dry_run:
            continue
        run_ffmpeg(cmd)
        cut_count += 1

    if args.dry_run:
        print("[info] Dry run complete; no files were modified.")
    elif cut_count == 0:
        print("[info] No videos were cut (maybe they were missing?).")
    else:
        print(f"[info] Created {cut_count} trimmed file(s).")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def find_latest_html(html_dir: Path) -> Path:
    if not html_dir.exists():
        raise FileNotFoundError(f"HTML directory {html_dir} does not exist.")
    html_files = sorted(html_dir.glob("*.html"), key=lambda p: p.stat().st_mtime)
    if not html_files:
        raise FileNotFoundError(f"No HTML files found in {html_dir}")
    return html_files[-1]


def extract_text_lines(raw_html: str) -> List[str]:
    if BeautifulSoup:
        soup = BeautifulSoup(raw_html, "html.parser")
        return [s.strip() for s in soup.stripped_strings if s.strip()]

    text = re.sub(r"<[^>]+>", "\n", raw_html)
    lines = (unescape(chunk).strip() for chunk in text.splitlines())
    return [line for line in lines if line]


def parse_instructions(lines: Iterable[str]) -> List[CutInstruction]:
    instructions: List[CutInstruction] = []
    for line in lines:
        match = LINE_RE.match(line)
        if not match:
            continue
        label, index_str, rest = match.groups()
        times = extract_timecodes(rest or "")
        if label.upper() == "INSERT":
            if not times:
                continue
            instructions.append(
                CutInstruction(
                    label="INSERT",
                    index=int(index_str),
                    start=times[0],
                    end=None,
                    source=line,
                )
            )
        else:
            if len(times) < 2:
                continue
            instructions.append(
                CutInstruction(
                    label="EXTRAIT",
                    index=int(index_str),
                    start=times[0],
                    end=times[1],
                    source=line,
                )
            )
    return instructions


def extract_timecodes(text: str) -> List[float]:
    codes: List[float] = []
    for match in COLON_TIME_RE.findall(text):
        secs = parse_timecode(match)
        _append_time(codes, secs)
    lower = text.lower()
    for match in HUMAN_TIME_RE.finditer(lower):
        h = int(match.group("h") or 0)
        m = int(match.group("m") or 0)
        s = float(match.group("s") or 0.0)
        secs = h * 3600 + m * 60 + s
        _append_time(codes, secs)
    return codes


def _append_time(codes: List[float], value: Optional[float]) -> None:
    if value is None:
        return
    if codes and abs(codes[-1] - value) < 1e-3:
        return
    codes.append(value)


def parse_timecode(token: str) -> Optional[float]:
    token = token.strip()
    if not token:
        return None
    parts = token.split(":")
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        return None
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
    elif len(numbers) == 2:
        hours = 0
        minutes, seconds = numbers
    elif len(numbers) == 1:
        hours = 0
        minutes = 0
        seconds = numbers[0]
    else:
        return None
    return hours * 3600 + minutes * 60 + seconds


def find_video_file(session_dir: Path, index: int) -> Optional[Path]:
    patterns = [
        f"*_{index:02d}_video*",
        f"*_{index}_video*",
    ]
    for pattern in patterns:
        candidates = sorted(p for p in session_dir.glob(pattern) if p.is_file())
        if candidates:
            return candidates[0]
    return None


def build_output_path(source: Path) -> Path:
    suffixes = "".join(source.suffixes)
    if suffixes:
        base = source.name[: -len(suffixes)]
    else:
        base = source.name
    new_name = f"{base}_cut{suffixes}"
    return source.with_name(new_name)


def build_ffmpeg_command(
    ffmpeg_bin: str,
    src: Path,
    dest: Path,
    start: float,
    duration: Optional[float],
) -> List[str]:
    cmd: List[str] = [ffmpeg_bin, "-y"]
    if start > 0:
        cmd += ["-ss", format_timestamp(start)]
    cmd += ["-i", str(src)]
    if duration and duration > 0:
        cmd += ["-t", format_timestamp(duration)]
    cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "fast", "-c:a", "aac", str(dest)]
    return cmd


def format_timestamp(value: float) -> str:
    milliseconds = int(round(value * 1000))
    hours, ms_rem = divmod(milliseconds, 3600 * 1000)
    minutes, ms_rem = divmod(ms_rem, 60 * 1000)
    seconds, ms = divmod(ms_rem, 1000)
    if ms:
        return f"{hours:02}:{minutes:02}:{seconds:02}.{ms:03}"
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def run_ffmpeg(cmd: List[str]) -> None:
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"ffmpeg failed with exit code {exc.returncode}")


if __name__ == "__main__":
    main()
