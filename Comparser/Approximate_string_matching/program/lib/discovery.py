from __future__ import annotations

from pathlib import Path

from .constants import (
    DEFAULT_GROQ_CLAP_DIR,
    DEFAULT_GROQ_NOCLAP_DIR,
    DEFAULT_SWISSER_HTML_DIR,
    DEFAULT_SWISSER_RUSH_DIR,
)


def latest_file(directory: Path, suffixes: tuple[str, ...]) -> Path:
    if not directory.exists():
        raise FileNotFoundError(f"Missing directory: {directory}")
    candidates = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in suffixes
    ]
    if not candidates:
        raise FileNotFoundError(f"No files with suffixes {suffixes} in {directory}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def discover_latest_html(html_dir: Path | None = None) -> Path:
    return latest_file(html_dir or DEFAULT_SWISSER_HTML_DIR, (".html", ".htm"))


def discover_latest_rush(rush_dir: Path | None = None) -> Path:
    return latest_file(rush_dir or DEFAULT_SWISSER_RUSH_DIR, (".mp4", ".mov", ".mxf", ".m4v", ".avi", ".mkv"))


def discover_latest_groq_csv() -> Path:
    candidates: list[Path] = []
    for directory in (DEFAULT_GROQ_CLAP_DIR, DEFAULT_GROQ_NOCLAP_DIR):
        if not directory.exists():
            continue
        candidates.extend(
            path
            for path in directory.iterdir()
            if path.is_file()
            and path.suffix.lower() == ".csv"
            and not path.name.endswith("_words.csv")
            and not path.name.endswith("_vad_segments.csv")
        )
    if not candidates:
        raise FileNotFoundError(
            f"No Groq transcript CSV found in {DEFAULT_GROQ_NOCLAP_DIR} or {DEFAULT_GROQ_CLAP_DIR}"
        )
    return max(candidates, key=lambda path: path.stat().st_mtime)


def discover_words_for_csv(csv_path: Path) -> Path | None:
    candidate = csv_path.with_name(f"{csv_path.stem}_words.csv")
    if candidate.exists():
        return candidate
    return None
