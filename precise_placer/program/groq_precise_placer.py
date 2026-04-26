#!/usr/bin/env python3
"""Groq-friendly shim around the precise placer that tolerates missing HTML docs."""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional


BASE_PRECISE_SCRIPT = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/precise_placer/program/independant_precise_renamer_with_extract.py"
)
DEFAULT_TRANSCRIPT_DIRS = [
    Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/Comparser/output"),
    Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/Comparser/output/first_comparser_output"),
]
DEFAULT_DOC_DIR = Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/html")
DEFAULT_OUTPUT_DIR = Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/precise_placer/output")
DEFAULT_ASSETS_DIR = Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/Insert")
DOC_EXTENSIONS = (".html", ".htm")
CSV_HEADER = [
    "link_index",
    "timestamp_seconds",
    "timestamp_label",
    "timecode",
    "source_timestamp_seconds",
    "source_timecode",
    "match_ratio",
    "url",
    "snippet",
    "context",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Attempt a precise insert alignment, but gracefully fall back when no HTML doc is available."
    )
    parser.add_argument("--transcript", help="Explicit transcript CSV path (defaults to newest comparer output).")
    parser.add_argument("--doc", help="Optional saved Google Doc HTML path.")
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"Directory for generated reports (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--assets",
        default=str(DEFAULT_ASSETS_DIR),
        help=f"Directory containing insert assets (default: {DEFAULT_ASSETS_DIR}).",
    )
    parser.add_argument(
        "--frame-rate",
        type=int,
        default=25,
        help="Frame rate for timestamp calculations when delegating to the legacy script (default: 25).",
    )
    parser.add_argument(
        "--context-before",
        type=int,
        default=80,
        help="Characters of context to capture before each link when the HTML doc exists.",
    )
    parser.add_argument(
        "--context-after",
        type=int,
        default=80,
        help="Characters of context to capture after each link when the HTML doc exists.",
    )
    parser.add_argument(
        "--base-offset",
        type=float,
        help="Forwarded to the legacy precise renamer when an HTML doc is available.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.45,
        help="Similarity warning threshold forwarded to the legacy script (default: 0.45).",
    )
    parser.add_argument(
        "--rename",
        dest="rename",
        action="store_true",
        default=None,
        help="Force asset renaming when an HTML doc exists.",
    )
    parser.add_argument(
        "--no-rename",
        dest="rename",
        action="store_false",
        help="Skip renaming even if the HTML doc exists.",
    )
    parser.add_argument(
        "--write-list",
        action="store_true",
        help="Update list.txt inside the assets directory even when falling back.",
    )
    parser.add_argument(
        "--require-precise-comparer",
        action="store_true",
        help="Fail instead of delegating or writing placeholders when precise comparer timing is unavailable.",
    )
    return parser.parse_args()


def _latest_file(directories: Iterable[Path], extensions: tuple[str, ...]) -> Path | None:
    candidates: list[Path] = []
    normalized = {ext.lower() for ext in extensions}
    for directory in directories:
        if not directory.exists():
            continue
        for entry in directory.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix.lower() in normalized:
                candidates.append(entry)
    if not candidates:
        return None
    candidates.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    return candidates[0]


def resolve_transcript_path(candidate: str | None) -> Path:
    if candidate:
        path = Path(candidate).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Transcript CSV not found: {path}")
        return path
    path = _latest_file(DEFAULT_TRANSCRIPT_DIRS, (".csv",))
    if path is None:
        raise FileNotFoundError(
            "Unable to locate a comparer CSV. Provide --transcript explicitly or export one from the comparser pipeline."
        )
    return path


def resolve_doc_path(candidate: str | None) -> Path | None:
    if candidate:
        path = Path(candidate).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"HTML document not found: {path}")
        return path
    path = _latest_file([DEFAULT_DOC_DIR], DOC_EXTENSIONS)
    return path


def write_placeholder_precise_links(output_dir: Path, transcript_path: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "precise_links.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADER)
        writer.writeheader()
        placeholder = {key: "" for key in CSV_HEADER}
        placeholder["context"] = (
            f"No HTML document available; placeholder generated from {transcript_path.name}."
        )
        writer.writerow(placeholder)
    return csv_path


def _normalize_header(name: str | None) -> str:
    return (name or "").strip().lower()


def _canonical_header(name: str | None) -> str:
    return _normalize_header(name).replace(" ", "").replace("_", "").replace("#", "")


def _seconds_to_label(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    minutes = total_seconds // 60
    secs = total_seconds % 60
    return f"{minutes}m{secs:02d}"


def _seconds_to_timecode(seconds: float, frame_rate: int) -> str:
    frame_count = max(0, int(round(seconds * max(frame_rate, 1))))
    total_seconds = frame_count // max(frame_rate, 1)
    frames = frame_count % max(frame_rate, 1)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hours:02}:{minutes:02}:{secs:02}:{frames:02}"


def _timecode_to_seconds(value: str, frame_rate: int) -> Optional[float]:
    raw = (value or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) == 4:
        hours, minutes, seconds, frames = parts
    elif len(parts) == 3:
        hours = "0"
        minutes, seconds, frames = parts
    else:
        return None
    try:
        hours_i = int(hours)
        minutes_i = int(minutes)
        seconds_i = int(seconds)
        frames_i = int(frames)
    except ValueError:
        return None
    fps = max(frame_rate, 1)
    return hours_i * 3600 + minutes_i * 60 + seconds_i + frames_i / fps


def _detect_delimiter(sample: str) -> str:
    first_nonempty_line = next((line for line in sample.splitlines() if line.strip()), "")
    semicolon_count = first_nonempty_line.count(";")
    comma_count = first_nonempty_line.count(",")
    if semicolon_count > 0 and semicolon_count >= comma_count:
        return ";"
    if comma_count > 0 and comma_count > semicolon_count:
        return ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
    except csv.Error:
        return ";"
    delimiter = str(getattr(dialect, "delimiter", ";") or ";")
    return delimiter if delimiter in {",", ";"} else ";"


def _open_dict_reader(transcript_path: Path) -> tuple[csv.DictReader, object]:
    handle = transcript_path.open("r", encoding="utf-8-sig", newline="")
    delimiter = _detect_delimiter(handle.read(8192))
    handle.seek(0)
    return csv.DictReader(handle, delimiter=delimiter), handle


def comparer_has_precise_timing_fields(transcript_path: Path) -> bool:
    reader, handle = _open_dict_reader(transcript_path)
    with handle:
        fieldnames = reader.fieldnames or []
    normalized = {_canonical_header(name) for name in fieldnames}
    return "sourcestarttime" in normalized and "sourceendtime" in normalized


def build_links_from_comparer_csv(
    transcript_path: Path,
    output_dir: Path,
    frame_rate: int,
) -> tuple[Path, int]:
    reader, handle = _open_dict_reader(transcript_path)
    with handle:
        if not reader.fieldnames:
            raise RuntimeError("Comparer CSV does not contain headers.")
        header_map: Dict[str, str] = {}
        for name in reader.fieldnames:
            header_map[_canonical_header(name)] = name

        keep_field = header_map.get("keep")
        start_field = header_map.get("starttime") or header_map.get("start")
        source_start_field = header_map.get("sourcestarttime")
        text_field = header_map.get("text")
        if start_field is None or text_field is None:
            raise RuntimeError("Comparer CSV lacks Start Time or Text columns.")

        output_dir.mkdir(parents=True, exist_ok=True)
        csv_path = output_dir / "precise_links.csv"
        rows_written = 0
        with csv_path.open("w", newline="", encoding="utf-8") as out_handle:
            writer = csv.DictWriter(out_handle, fieldnames=CSV_HEADER)
            writer.writeheader()
            for idx, row in enumerate(reader, start=1):
                keep_value = (row.get(keep_field, "") if keep_field else "x").strip().lower()
                if keep_field and keep_value not in {"x", "✓", "1", "keep"}:
                    continue
                start_raw = (row.get(start_field) or "").strip()
                edit_seconds = _timecode_to_seconds(start_raw, frame_rate)
                if edit_seconds is None:
                    continue
                source_raw = (row.get(source_start_field) or "").strip() if source_start_field else ""
                source_seconds = _timecode_to_seconds(source_raw, frame_rate)
                if source_seconds is None:
                    source_seconds = edit_seconds
                text_value = (row.get(text_field) or "").strip()
                timestamp_seconds = f"{edit_seconds:.3f}"
                source_timestamp_seconds = f"{source_seconds:.3f}"
                entry = {
                    "link_index": rows_written + 1,
                    "timestamp_seconds": timestamp_seconds,
                    "timestamp_label": _seconds_to_label(edit_seconds),
                    "timecode": start_raw or _seconds_to_timecode(edit_seconds, frame_rate),
                    "source_timestamp_seconds": source_timestamp_seconds,
                    "source_timecode": source_raw or _seconds_to_timecode(source_seconds, frame_rate),
                    "match_ratio": "1.0",
                    "url": "",
                    "snippet": text_value,
                    "context": text_value or f"Groq row {idx}",
                }
                writer.writerow(entry)
                rows_written += 1
    if rows_written == 0:
        raise RuntimeError("Comparer fallback did not produce any usable rows.")
    return csv_path, rows_written


def ensure_assets_list(assets_path: Path) -> Path:
    assets_path.mkdir(parents=True, exist_ok=True)
    list_path = assets_path / "list.txt"
    if not list_path.exists():
        list_path.write_text("", encoding="utf-8")
    return list_path


def run_full_precise(
    transcript_path: Path,
    doc_path: Path,
    args: argparse.Namespace,
) -> None:
    cmd = [
        sys.executable,
        str(BASE_PRECISE_SCRIPT),
        "--transcript",
        str(transcript_path),
        "--doc",
        str(doc_path),
        "--output-dir",
        args.output_dir,
        "--assets",
        args.assets,
        "--frame-rate",
        str(args.frame_rate),
        "--context-before",
        str(args.context_before),
        "--context-after",
        str(args.context_after),
        "--threshold",
        str(args.threshold),
    ]
    if args.base_offset is not None:
        cmd.extend(["--base-offset", str(args.base_offset)])
    if args.rename is True:
        cmd.append("--rename")
    elif args.rename is False:
        cmd.append("--no-rename")
    if args.write_list:
        cmd.append("--write-list")
    subprocess.run(cmd, check=True)


def main() -> None:
    args = parse_args()
    try:
        transcript_path = resolve_transcript_path(args.transcript)
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        raise SystemExit(1)
    output_dir = Path(args.output_dir).expanduser()
    assets_dir = Path(args.assets).expanduser()

    try:
        doc_path = resolve_doc_path(args.doc)
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        raise SystemExit(1)

    if comparer_has_precise_timing_fields(transcript_path):
        print("Precise comparer CSV detected; deriving insert timings directly from comparer data.")
        try:
            csv_path, count = build_links_from_comparer_csv(transcript_path, output_dir, args.frame_rate)
            print(f"Generated {count} precise link(s) at {csv_path}")
        except Exception as exc:  # pragma: no cover - defensive
            if args.require_precise_comparer:
                print(f"error: Unable to derive precise links from comparer CSV ({exc}).")
                raise SystemExit(1)
            print(f"error: Unable to derive precise links from comparer CSV ({exc}); writing placeholder instead.")
            csv_path = write_placeholder_precise_links(output_dir, transcript_path)
            print(f"Placeholder precise links written to {csv_path}")
        if args.write_list:
            list_path = ensure_assets_list(assets_dir)
            print(f"Ensured empty list.txt exists at {list_path}")
        return

    if args.require_precise_comparer:
        print(
            "error: Precise comparer timing fields are required, but the provided transcript CSV does not contain them."
        )
        raise SystemExit(1)

    if doc_path:
        print(f"HTML document detected ({doc_path}); delegating to the legacy precise placer.")
        run_full_precise(transcript_path, doc_path, args)
        return

    print("No HTML document detected; deriving precise timings directly from the comparer CSV.")
    try:
        csv_path, count = build_links_from_comparer_csv(transcript_path, output_dir, args.frame_rate)
        print(f"Generated {count} fallback precise link(s) at {csv_path}")
    except Exception as exc:  # pragma: no cover - defensive
        print(f"error: Unable to derive comparer-based precise links ({exc}); writing placeholder instead.")
        csv_path = write_placeholder_precise_links(output_dir, transcript_path)
        print(f"Placeholder precise links written to {csv_path}")
    if args.write_list:
        list_path = ensure_assets_list(assets_dir)
        print(f"Ensured empty list.txt exists at {list_path}")


if __name__ == "__main__":
    main()
