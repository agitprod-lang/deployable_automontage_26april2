#!/usr/bin/env python3.11
"""
zoom_type_resolver_0.py

Reads 13_precise_comparer.csv + Insert directory.
Rewrites the Zoom column with 6 unified types:

  INTRO_ZOOM    — row during intro zoom zone  (t < intro_end)
  ZOOM_FACE     — row had Z / Z1 / Z2 / Z3   (important sentence)
  SHIFT_INTRO   — row during shift transition-in phase
  SHIFT_MIDDLE  — row during shift hold phase
  SHIFT_OUTRO   — row during shift transition-out phase
  OUTRO_ZOOM    — row during outro dip zone   (t >= outro_start)
  (empty)       — no zoom for this row

Priority (highest first): INTRO_ZOOM / OUTRO_ZOOM > SHIFT_* > ZOOM_FACE > empty

Usage:
  python3.11 zoom_type_resolver_0.py \
    --csv /path/to/13_precise_comparer.csv \
    --insert-dir /path/to/Insert/ \
    [--output-csv /path/to/output.csv]     # default: overwrites input
    [--fps 30]
    [--transition-s 1.0]
    [--gap-tolerance 0.5]
    [--dry-run]
"""

import argparse
import csv
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── insert classification (mirrors zoom_insert_shift_creator_0.py) ─────────────

TRIGGER_SUFFIXES = ("_emoji", "_cta", "_soc", "_spd", "_pct", "_cal", "_dur")
_TIMESTAMP_RE = re.compile(r"^(\d{2})h(\d{2})m(\d{2})s(\d{3})ms", re.IGNORECASE)
_CITY_RE = re.compile(r"^city_\d+_", re.IGNORECASE)


def _stem_after_ts(stem: str) -> str:
    m = _TIMESTAMP_RE.match(stem)
    if m:
        return stem[m.end():]
    # strip leading digits_
    return re.sub(r"^\d+_", "", stem)


def is_triggering_insert(path: Path) -> bool:
    stem = path.stem
    sl = stem.lower()

    # hard excludes
    if "intro_zoom" in sl or "outro_dip" in sl:
        return False
    if "transitionfilburn" in sl:
        return False
    if "DIRECT" in stem or "EXTRACT" in stem:
        return False

    suffix_part = _stem_after_ts(sl)

    if suffix_part.endswith(("_qh", "_bld", "_lst")):
        return False
    if suffix_part.startswith("title_"):
        return False
    if "url_screen" in suffix_part:
        return False
    if _CITY_RE.match(suffix_part):
        return False

    if any(suffix_part.endswith(s) for s in TRIGGER_SUFFIXES):
        return True
    if "_filled" in suffix_part:
        return True
    if "image@circle" in suffix_part:
        return True
    if "polaroid_insert" in suffix_part:
        return True
    if "@" in stem:
        return True

    return False


def parse_insert_timestamp(stem: str) -> float:
    m = _TIMESTAMP_RE.match(stem)
    if not m:
        return 0.0
    h, mn, s, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    return h * 3600 + mn * 60 + s + ms / 1000.0


def probe_duration(path: Path) -> float:
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            stderr=subprocess.DEVNULL,
        )
        return float(json.loads(out)["format"]["duration"])
    except Exception:
        return 0.0


# ── shift window logic ─────────────────────────────────────────────────────────

@dataclass
class ShiftWindow:
    start_s: float
    end_s: float
    inserts: list = field(default_factory=list)


def get_protection_zones(insert_dir: Path) -> tuple[float, float]:
    intro_end = 0.0
    outro_start = float("inf")

    for f in insert_dir.iterdir():
        sl = f.stem.lower()
        if "intro_zoom" in sl:
            ts = parse_insert_timestamp(f.stem)
            dur = probe_duration(f)
            intro_end = max(intro_end, ts + dur)
        elif "outro_dip" in sl:
            ts = parse_insert_timestamp(f.stem)
            outro_start = min(outro_start, ts)

    if outro_start == float("inf"):
        outro_start = float("inf")  # no outro found

    return intro_end, outro_start


def get_shift_windows(insert_dir: Path, gap_tolerance: float) -> list[ShiftWindow]:
    triggering = []
    for f in insert_dir.iterdir():
        if not f.is_file():
            continue
        if not is_triggering_insert(f):
            continue
        ts = parse_insert_timestamp(f.stem)
        dur = probe_duration(f)
        triggering.append((ts, ts + dur, f.name))

    if not triggering:
        return []

    triggering.sort(key=lambda x: x[0])

    windows: list[ShiftWindow] = []
    cur_start, cur_end, cur_inserts = triggering[0]
    cur_inserts = [cur_inserts]

    for ts, te, name in triggering[1:]:
        if ts <= cur_end + gap_tolerance:
            cur_end = max(cur_end, te)
            cur_inserts.append(name)
        else:
            windows.append(ShiftWindow(cur_start, cur_end, cur_inserts))
            cur_start, cur_end, cur_inserts = ts, te, [name]

    windows.append(ShiftWindow(cur_start, cur_end, cur_inserts))
    return windows


# ── timecode parsing ───────────────────────────────────────────────────────────

def tc_to_seconds(tc: str, fps: float) -> float:
    """Convert HH:MM:SS:FF to seconds."""
    parts = tc.strip().split(":")
    if len(parts) != 4:
        return 0.0
    h, m, s, f = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
    return h * 3600 + m * 60 + s + f / fps


# ── classification ─────────────────────────────────────────────────────────────

FACE_ZOOM_VALUES = {"Z", "Z1", "Z2", "Z3"}


def classify_row(
    src_start_s: float,
    original_zoom: str,
    intro_end: float,
    outro_start: float,
    shift_windows: list[ShiftWindow],
    transition_s: float,
) -> str:
    # Priority 1: intro / outro zones
    if src_start_s < intro_end:
        return "INTRO_ZOOM"
    if src_start_s >= outro_start:
        return "OUTRO_ZOOM"

    # Priority 2: shift phases
    for w in shift_windows:
        trans_in_start = w.start_s - transition_s
        trans_out_end = w.end_s + transition_s

        if trans_in_start <= src_start_s < w.start_s:
            return "SHIFT_INTRO"
        if w.start_s <= src_start_s <= w.end_s:
            return "SHIFT_MIDDLE"
        if w.end_s < src_start_s <= trans_out_end:
            return "SHIFT_OUTRO"

    # Priority 3: face zoom
    if original_zoom.strip() in FACE_ZOOM_VALUES:
        return "ZOOM_FACE"

    return ""


# ── main ───────────────────────────────────────────────────────────────────────

def update_annotations_csv(
    annotations_path: Path,
    output_path: Path,
    intro_end: float,
    outro_start: float,
    shift_windows: list[ShiftWindow],
    transition_s: float,
    fps: float,
    dry_run: bool,
) -> None:
    """Update Zoom annotation values in 13_precise_annotations.csv."""
    with open(annotations_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    updated = 0
    out_rows = []
    for row in rows:
        if row.get("Annotation Column", "").strip() != "Zoom":
            out_rows.append(row)
            continue

        original_val = row.get("Annotation Value", "").strip()
        src_tc = row.get("Source Start Time", "").strip()
        src_s = tc_to_seconds(src_tc, fps) if src_tc else 0.0

        new_type = classify_row(src_s, original_val, intro_end, outro_start, shift_windows, transition_s)
        if new_type:
            row["Annotation Value"] = new_type
            updated += 1
            out_rows.append(row)
        # rows with new_type == "" (lost zoom status) are dropped

    print(f"  Annotations updated: {updated} Zoom rows → new types")

    if dry_run:
        return

    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"  Annotations written → {output_path}")


def main():
    ap = argparse.ArgumentParser(description="Resolve unified Zoom types in comparer CSV")
    ap.add_argument("--csv", required=True, help="Path to 13_precise_comparer.csv")
    ap.add_argument("--insert-dir", required=True, help="Insert folder to scan")
    ap.add_argument("--output-csv", default=None, help="Output path (default: overwrite input)")
    ap.add_argument("--annotations-csv", default=None, help="Path to 13_precise_annotations.csv (optional, updated in place)")
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--transition-s", type=float, default=1.0)
    ap.add_argument("--gap-tolerance", type=float, default=0.5)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    insert_dir = Path(args.insert_dir)
    output_path = Path(args.output_csv) if args.output_csv else csv_path

    if not csv_path.exists():
        print(f"ERROR: CSV not found: {csv_path}", file=sys.stderr)
        sys.exit(1)
    if not insert_dir.exists():
        print(f"ERROR: Insert dir not found: {insert_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning insert dir: {insert_dir}")
    intro_end, outro_start = get_protection_zones(insert_dir)
    print(f"  Intro zoom ends at: {intro_end:.3f}s")
    print(f"  Outro dip starts at: {outro_start:.3f}s" if outro_start < float("inf") else "  No outro_dip found")

    windows = get_shift_windows(insert_dir, args.gap_tolerance)
    print(f"  Shift windows found: {len(windows)}")
    for i, w in enumerate(windows, 1):
        print(f"    Window {i}: {w.start_s:.3f}s – {w.end_s:.3f}s  ({len(w.inserts)} inserts)")
        print(f"      SHIFT_INTRO:  {w.start_s - args.transition_s:.3f}s – {w.start_s:.3f}s")
        print(f"      SHIFT_MIDDLE: {w.start_s:.3f}s – {w.end_s:.3f}s")
        print(f"      SHIFT_OUTRO:  {w.end_s:.3f}s – {w.end_s + args.transition_s:.3f}s")

    # read CSV
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    if "Zoom" not in fieldnames:
        print("ERROR: 'Zoom' column not found in CSV", file=sys.stderr)
        sys.exit(1)

    if "Source Start Time" not in fieldnames:
        print("ERROR: 'Source Start Time' column not found in CSV", file=sys.stderr)
        sys.exit(1)

    counts: dict[str, int] = {}
    for row in rows:
        src_tc = row.get("Source Start Time", "").strip()
        original_zoom = row.get("Zoom", "").strip()

        if not src_tc:
            row["Zoom"] = ""
            counts[""] = counts.get("", 0) + 1
            continue

        src_s = tc_to_seconds(src_tc, args.fps)
        zoom_type = classify_row(
            src_s, original_zoom, intro_end, outro_start, windows, args.transition_s
        )
        row["Zoom"] = zoom_type
        counts[zoom_type] = counts.get(zoom_type, 0) + 1

    # report
    print("\nZoom type distribution:")
    for k in ["INTRO_ZOOM", "SHIFT_INTRO", "SHIFT_MIDDLE", "SHIFT_OUTRO", "OUTRO_ZOOM", "ZOOM_FACE", ""]:
        n = counts.get(k, 0)
        if n or k in ("ZOOM_FACE", "INTRO_ZOOM", "OUTRO_ZOOM"):
            label = k if k else "(empty — no zoom)"
            print(f"  {label:<18} : {n}")

    if args.dry_run:
        print("\n[dry-run] CSV not written.")
    else:
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter=";", extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nWrote updated CSV → {output_path}")

    if args.annotations_csv:
        ann_path = Path(args.annotations_csv)
        if ann_path.exists():
            print(f"\nUpdating annotations CSV: {ann_path}")
            update_annotations_csv(
                ann_path, ann_path, intro_end, outro_start,
                windows, args.transition_s, args.fps, args.dry_run,
            )
        else:
            print(f"WARNING: --annotations-csv not found: {ann_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
