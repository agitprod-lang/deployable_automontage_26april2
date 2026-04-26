#!/usr/bin/env python3
"""
Zap editor: detects "auto-montage zap" in the spoken transcript and eliminates
the segment that precedes it (plus the zap cue itself) from both the
precise_annotations CSV and the main comparer CSV.

Pattern handled:
  [keep] → [silence] → [DELETE] → [silence?] → [ZAP_CUE]

Usage:
  python3.11 zap_editor.py \
    --video    /path/to/rush.mp4 \
    --annotations /path/to/groq_html_comparison_precise_annotations.csv \
    --comparer    /path/to/groq_html_comparison.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

ZAP_PHRASES = {"automontage zap", "auto-montage zap", "auto montage zap", "automontage-zap"}

ANNOTATIONS_DELIMITER = ";"
COMPARER_DELIMITER = ";"


# ---------------------------------------------------------------------------
# Text matching
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[-\s]+", " ", t)
    t = re.sub(r"[^\w\s]", "", t)
    return t.strip()


def _contains_zap(text: str) -> bool:
    normalized = _normalize(text)
    return any(phrase in normalized for phrase in ZAP_PHRASES)


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def _read_csv(path: Path, delimiter: str) -> tuple[list[str], list[dict]]:
    with path.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=delimiter)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    return fieldnames, rows


def _write_csv(fieldnames: list[str], rows: list[dict], path: Path, delimiter: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter=delimiter, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def find_zap_indices(rows: list[dict]) -> list[int]:
    """Return indices (in rows list) of speech rows that contain the zap phrase."""
    found = []
    for i, row in enumerate(rows):
        if row.get("Kind", "").strip().lower() != "speech":
            continue
        if _contains_zap(row.get("Text", "")):
            found.append(i)
    return found


def find_segment_to_delete(rows: list[dict], zap_idx: int) -> list[int]:
    """
    Walk backwards from zap_idx.
    Collect silence rows immediately before the zap, then the previous speech row.
    Returns list of row indices to eliminate (silence rows + the speech segment).
    Returns empty list if there is no prior speech segment.
    """
    trailing_silences: list[int] = []
    i = zap_idx - 1

    while i >= 0 and rows[i].get("Kind", "").strip().lower() == "silence":
        trailing_silences.append(i)
        i -= 1

    if i < 0 or rows[i].get("Kind", "").strip().lower() != "speech":
        return []

    speech_to_delete = i
    return [speech_to_delete] + list(reversed(trailing_silences))


def patch_annotations(
    rows: list[dict],
    zap_idx: int,
    delete_indices: list[int],
) -> list[dict]:
    """Mark zap cue and segment-to-delete as eliminated. Returns a new list."""
    rows = [dict(r) for r in rows]

    zap_timecode = rows[zap_idx].get("Start Time", "?")

    rows[zap_idx]["Eliminate"] = "x"
    rows[zap_idx]["Eliminate Reason"] = "ZAP_CUE – auto-montage zap spoken, cue word removed"

    for idx in delete_indices:
        kind = rows[idx].get("Kind", "").strip().lower()
        if kind == "silence":
            continue  # silence rows already carry SILENCE reason; leave them
        rows[idx]["Eliminate"] = "x"
        rows[idx]["Eliminate Reason"] = f"ZAP – segment deleted before auto-montage zap at {zap_timecode}"

    return rows


def patch_comparer(
    rows: list[dict],
    row_ids_to_delete: set[str],
    zap_row_ids: set[str],
    zap_timecodes: dict[str, str],
) -> list[dict]:
    """
    Mark rows in the main comparer CSV as eliminated.
    Matches by Transcript # == Row ID from precise_annotations.
    zap_timecodes maps each delete row_id to the timecode of the zap that triggered it.
    """
    rows = [dict(r) for r in rows]
    for row in rows:
        tid = str(row.get("Transcript #", "")).strip()
        if tid in zap_row_ids:
            row["Eliminate"] = "x"
            row["Eliminate Reason"] = "ZAP_CUE – auto-montage zap spoken, cue word removed"
            row["Keep"] = ""
        elif tid in row_ids_to_delete:
            zap_tc = zap_timecodes.get(tid, "?")
            row["Eliminate"] = "x"
            row["Eliminate Reason"] = f"ZAP – segment deleted before auto-montage zap at {zap_tc}"
            row["Keep"] = ""
    return rows


# ---------------------------------------------------------------------------
# Video probe (optional, for report)
# ---------------------------------------------------------------------------

def _probe_duration(video: Path) -> float | None:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        return float(result.stdout.strip())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _build_report(zap_events: list[dict], video: Path | None) -> dict:
    return {
        "video": str(video) if video else None,
        "zap_count": len(zap_events),
        "events": zap_events,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, help="Rush video (optional, used for report only)")
    parser.add_argument("--annotations", type=Path, required=True,
                        help="precise_annotations CSV from Comparser")
    parser.add_argument("--comparer", type=Path, required=True,
                        help="Main groq_html_comparison CSV from Comparser")
    args = parser.parse_args()

    ann_path: Path = args.annotations.expanduser().resolve()
    cmp_path: Path = args.comparer.expanduser().resolve()
    video: Path | None = args.video.expanduser().resolve() if args.video else None

    if not ann_path.exists():
        raise FileNotFoundError(f"Annotations CSV not found: {ann_path}")
    if not cmp_path.exists():
        raise FileNotFoundError(f"Comparer CSV not found: {cmp_path}")

    print(f"==> Loading annotations: {ann_path.name}")
    ann_fields, ann_rows = _read_csv(ann_path, ANNOTATIONS_DELIMITER)

    print(f"==> Loading comparer:    {cmp_path.name}")
    cmp_fields, cmp_rows = _read_csv(cmp_path, COMPARER_DELIMITER)

    zap_indices = find_zap_indices(ann_rows)
    if not zap_indices:
        print("    No 'auto-montage zap' phrase found in transcript. Nothing to do.")
        return

    print(f"    Found {len(zap_indices)} zap cue(s).")

    zap_row_ids: set[str] = set()
    delete_row_ids: set[str] = set()
    zap_timecodes: dict[str, str] = {}  # delete row_id → zap timecode
    zap_events: list[dict] = []

    for zap_idx in zap_indices:
        zap_row = ann_rows[zap_idx]
        zap_row_id = str(zap_row.get("Row ID", "")).strip()
        zap_timecode = zap_row.get("Start Time", "?")
        zap_row_ids.add(zap_row_id)

        delete_indices = find_segment_to_delete(ann_rows, zap_idx)
        speech_delete_indices = [
            idx for idx in delete_indices
            if ann_rows[idx].get("Kind", "").strip().lower() == "speech"
        ]

        for idx in speech_delete_indices:
            rid = str(ann_rows[idx].get("Row ID", "")).strip()
            delete_row_ids.add(rid)
            zap_timecodes[rid] = zap_timecode

        ann_rows = patch_annotations(ann_rows, zap_idx, delete_indices)

        deleted_segment_text = (
            ann_rows[speech_delete_indices[0]].get("Text", "") if speech_delete_indices else None
        )
        event = {
            "zap_row_id": zap_row_id,
            "zap_start": zap_timecode,
            "zap_text": zap_row.get("Text", "").strip(),
            "deleted_row_ids": [str(ann_rows[i].get("Row ID", "")) for i in speech_delete_indices],
            "deleted_text": deleted_segment_text,
        }
        zap_events.append(event)
        print(f"    Zap @ {zap_timecode} → delete segment row_id={event['deleted_row_ids']}")

    cmp_rows = patch_comparer(cmp_rows, delete_row_ids, zap_row_ids, zap_timecodes)

    stem = ann_path.stem
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    out_ann = OUTPUT_DIR / f"{stem}_zap_annotations.csv"
    out_cmp = OUTPUT_DIR / f"{cmp_path.stem}_zap_comparer.csv"
    out_report = OUTPUT_DIR / f"{stem}_zap_report.json"

    _write_csv(ann_fields, ann_rows, out_ann, ANNOTATIONS_DELIMITER)
    _write_csv(cmp_fields, cmp_rows, out_cmp, COMPARER_DELIMITER)

    report = _build_report(zap_events, video)
    out_report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n==> Outputs written to {OUTPUT_DIR}/")
    print(f"    {out_ann.name}")
    print(f"    {out_cmp.name}")
    print(f"    {out_report.name}")
    print(f"\nPass {out_cmp.name} to universal_generate_premiere_xml.py instead of the original comparer CSV.")


if __name__ == "__main__":
    main()
