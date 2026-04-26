#!/usr/bin/env python3
"""
ref_comparser.py  —  standalone batch runner

Place files in  ../input/:
    <stem>.mp4  (or .mov / .avi / .mkv / .m4v)
    <stem>.html  ← reference text (required, same stem as video)

Run:
    python3.11 ref_comparser.py

For each video+html pair:
  1. Groq: extract audio → transcribe → segments CSV + words CSV
  2. Comparser: run pipeline (stages 00-13) via pipeline.py
  3. XML A: silence only  — from 01_marked_silence.csv
  4. XML B: full elimination — from 07_step1_compat.csv (no silence + no repetition + no metacommentary)

All outputs land in  ../output/<stem>/
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR  = Path(__file__).resolve().parent
INPUT_DIR   = SCRIPT_DIR.parent / "input"
OUTPUT_DIR  = SCRIPT_DIR.parent / "output"

VIDEO_TO_AUDIO  = SCRIPT_DIR / "video_to_audio.py"
GROQ_VAD_SCRIPT = SCRIPT_DIR / "groq_vad_csv_maker.py"
GROQ_AUDIO_DIR  = SCRIPT_DIR.parent / "output" / "groq" / "audio"
GROQ_NOCLAP_DIR = SCRIPT_DIR.parent / "output" / "groq" / "transcripts"

MAKE_XML_SCRIPT     = SCRIPT_DIR / "make_xml_without_silence.py"
UNIVERSAL_XML_SCRIPT = SCRIPT_DIR / "universal_generate_premiere_xml.py"
TRIM_BOUNDARIES     = SCRIPT_DIR / "trim_boundaries.py"
STITCH_PARTIALS     = SCRIPT_DIR / "stitch_partials.py"
VAD_REFINE          = SCRIPT_DIR / "vad_refine.py"

VIDEO_EXTS = {".mp4", ".mov", ".MOV", ".MP4", ".avi", ".AVI",
              ".mkv", ".MKV", ".m4v", ".M4V"}

# ---------------------------------------------------------------------------
# Import the core pipeline from pipeline.py (same directory)
# ---------------------------------------------------------------------------

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pipeline import run_pipeline  # noqa: E402


# ---------------------------------------------------------------------------
# Groq transcription helpers
# ---------------------------------------------------------------------------

def _run(cmd: list, label: str) -> None:
    print(f"  [{label}] {' '.join(str(c) for c in cmd)}")
    r = subprocess.run([str(c) for c in cmd])
    if r.returncode != 0:
        raise RuntimeError(f"[{label}] failed (exit {r.returncode})")



def _groq_transcribe(video_path: Path, fps: int = 30) -> tuple[Path, Path]:
    GROQ_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    GROQ_NOCLAP_DIR.mkdir(parents=True, exist_ok=True)

    audio_path = GROQ_AUDIO_DIR / f"{video_path.stem}.mp3"
    segs_csv   = GROQ_NOCLAP_DIR / f"{video_path.stem}.csv"
    words_csv  = GROQ_NOCLAP_DIR / f"{video_path.stem}_words.csv"

    if segs_csv.exists() and words_csv.exists():
        print(f"\n  [groq] Transcripts already exist — skipping Groq ({segs_csv.name})")
        return segs_csv, words_csv

    if not audio_path.exists():
        print(f"\n  [groq] Extracting audio …")
        _run(["python3.11", VIDEO_TO_AUDIO, str(video_path),
              "--output", str(audio_path)], "video_to_audio")

    if not audio_path.exists():
        raise FileNotFoundError(f"Audio not found: {audio_path}")

    print(f"  [groq] Transcribing …")
    _run(["python3.11", GROQ_VAD_SCRIPT, str(audio_path),
          "--output", str(segs_csv),
          "--frame-rate", str(fps)], "groq_vad")

    words_csv = GROQ_NOCLAP_DIR / f"{video_path.stem}_words.csv"
    if not segs_csv.exists() or not words_csv.exists():
        raise FileNotFoundError(
            f"Groq output CSVs not found after transcription.\n"
            f"  Expected: {segs_csv}\n"
            f"  Expected: {words_csv}"
        )
    return segs_csv, words_csv


# ---------------------------------------------------------------------------
# Premiere XML generation
# ---------------------------------------------------------------------------

def _build_premiere_xml(run_dir: Path, video_path: Path,
                        words_csv: Path | None) -> Path | None:
    silence_csv = run_dir / "01_marked_silence.csv"
    if not silence_csv.exists():
        print(f"  [xml] 01_marked_silence.csv missing — skipping XML")
        return None
    xml_out = run_dir / f"{video_path.stem}_no_silence.xml"
    cmd = ["python3.11", MAKE_XML_SCRIPT,
           "--silence-csv", str(silence_csv),
           "--rush", str(video_path),
           "--output", str(xml_out)]
    if words_csv and words_csv.exists():
        cmd += ["--word-csv", str(words_csv)]
    try:
        _run(cmd, "make_xml")
    except RuntimeError as exc:
        print(f"  [xml] Warning: {exc}")
        return None
    return xml_out if xml_out.exists() else None


# ---------------------------------------------------------------------------
# Full-elimination XML (no silence + no repetition + no metacommentary)
# ---------------------------------------------------------------------------

def _stitch_partials(run_dir: Path, html_path: Path) -> Path | None:
    """Dedup subset-match kept rows and stitch partial takes for uncovered reference sentences."""
    diagnostic_csv = run_dir / "07_step1_diagnostic.csv"
    compat_csv     = run_dir / "07_step1_compat.csv"
    if not diagnostic_csv.exists() or not compat_csv.exists():
        return None
    stitched_csv = run_dir / "07_step1_compat_stitched.csv"
    cmd = [
        "python3.11", STITCH_PARTIALS,
        "--compat",     str(compat_csv),
        "--diagnostic", str(diagnostic_csv),
        "--html",       str(html_path),
        "--output",     str(stitched_csv),
    ]
    try:
        _run(cmd, "stitch_partials")
    except RuntimeError as exc:
        print(f"  [stitch] Warning: {exc}")
        return None
    return stitched_csv if stitched_csv.exists() else None


def _trim_boundaries(run_dir: Path, words_csv: Path, compat_csv: Path | None = None) -> Path | None:
    """Trim clip boundaries using usable_word_range from diagnostic CSV."""
    diagnostic_csv = run_dir / "07_step1_diagnostic.csv"
    if compat_csv is None:
        compat_csv = run_dir / "07_step1_compat.csv"
    if not diagnostic_csv.exists() or not compat_csv.exists():
        return None
    trimmed_csv = run_dir / "07_step1_compat_trimmed.csv"
    cmd = [
        "python3.11", TRIM_BOUNDARIES,
        "--diagnostic", str(diagnostic_csv),
        "--compat",     str(compat_csv),
        "--words",      str(words_csv),
        "--output",     str(trimmed_csv),
    ]
    try:
        _run(cmd, "trim_boundaries")
    except RuntimeError as exc:
        print(f"  [trim] Warning: {exc}")
        return None
    return trimmed_csv if trimmed_csv.exists() else None


def _vad_refine(run_dir: Path, words_csv: Path, audio_path: Path,
                trimmed_csv: Path) -> Path | None:
    """Silero-VAD-based boundary refinement for rows hiding multiple takes."""
    diagnostic_csv = run_dir / "07_step1_diagnostic.csv"
    if not diagnostic_csv.exists() or not trimmed_csv.exists() or not audio_path.exists():
        return None
    refined_csv = run_dir / "07_step1_compat_vad.csv"
    cmd = [
        "python3.11", VAD_REFINE,
        "--compat",     str(trimmed_csv),
        "--diagnostic", str(diagnostic_csv),
        "--words",      str(words_csv),
        "--audio",      str(audio_path),
        "--output",     str(refined_csv),
    ]
    try:
        _run(cmd, "vad_refine")
    except RuntimeError as exc:
        print(f"  [vad_refine] Warning: {exc}")
        return None
    return refined_csv if refined_csv.exists() else None


def _build_full_elimination_xml(run_dir: Path, video_path: Path,
                                html_path: Path | None = None,
                                words_csv: Path | None = None,
                                audio_path: Path | None = None,
                                fps: int = 30) -> Path | None:
    """Generate XML from 07_step1_compat.csv — all bad takes eliminated."""
    compat_csv = run_dir / "07_step1_compat.csv"
    if not compat_csv.exists():
        print(f"  [xml_full] 07_step1_compat.csv missing — skipping")
        return None

    # Stitch partial takes for reference sentences without a clean single-take cover,
    # and dedup subset-ref_span kept rows, before boundary trimming.
    if html_path and html_path.exists():
        stitched = _stitch_partials(run_dir, html_path)
        if stitched:
            compat_csv = stitched

    if words_csv and words_csv.exists():
        trimmed = _trim_boundaries(run_dir, words_csv, compat_csv=compat_csv)
        if trimmed:
            compat_csv = trimmed
            if audio_path and audio_path.exists():
                refined = _vad_refine(run_dir, words_csv, audio_path, trimmed)
                if refined:
                    compat_csv = refined

    xml_out = run_dir / f"{video_path.stem}_no_silence_no_repetition_no_metacommentary.xml"
    cmd = [
        "python3.11", UNIVERSAL_XML_SCRIPT,
        "--csv",   str(compat_csv),
        "--media", str(video_path),
        "--fps",   str(fps),
        "--output", str(xml_out),
    ]
    try:
        _run(cmd, "universal_xml")
    except RuntimeError as exc:
        print(f"  [xml_full] Warning: {exc}")
        return None
    return xml_out if xml_out.exists() else None


# ---------------------------------------------------------------------------
# Per-video processing
# ---------------------------------------------------------------------------

def process_video(video_path: Path, html_path: Path) -> bool:
    stem = video_path.stem
    print(f"\n{'='*60}")
    print(f"Processing : {stem}")
    print(f"  Video    : {video_path}")
    print(f"  HTML     : {html_path}")
    print(f"{'='*60}")

    out_dir = OUTPUT_DIR / stem
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        segs_csv, words_csv = _groq_transcribe(video_path)
        print(f"  Segments CSV : {segs_csv}")
        print(f"  Words CSV    : {words_csv}")
    except Exception as exc:
        print(f"  [ERROR] Groq failed: {exc}", file=sys.stderr)
        return False

    try:
        summary = run_pipeline(
            csv_path=segs_csv,
            html_path=html_path,
            words_path=words_csv,
            output_dir=out_dir,
        )
    except Exception as exc:
        print(f"  [ERROR] Pipeline failed: {exc}", file=sys.stderr)
        return False

    run_dir  = Path(summary["summary_path"]).parent

    xml_silence = _build_premiere_xml(run_dir, video_path, words_csv)
    if xml_silence:
        print(f"  XML (no silence)                          : {xml_silence.name}")

    audio_path = GROQ_AUDIO_DIR / f"{video_path.stem}.mp3"
    xml_full = _build_full_elimination_xml(
        run_dir, video_path,
        html_path=html_path,
        words_csv=words_csv,
        audio_path=audio_path,
    )
    if xml_full:
        print(f"  XML (no silence/repetition/metacommentary): {xml_full.name}")

    print(f"  Outputs in   : {run_dir}")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    pairs: list[tuple[Path, Path]] = []
    for video_path in sorted(INPUT_DIR.iterdir()):
        if video_path.suffix not in VIDEO_EXTS:
            continue
        html_path = INPUT_DIR / f"{video_path.stem}.html"
        if not html_path.exists():
            print(f"[skip] {video_path.name} — no matching .html found")
            continue
        pairs.append((video_path, html_path))

    if not pairs:
        print(f"No video+html pairs found in {INPUT_DIR}")
        print("Place files like:  input/my_video.mp4  +  input/my_video.html")
        sys.exit(0)

    print(f"Found {len(pairs)} video(s) to process.")

    results: list[tuple[str, bool]] = []
    for video_path, html_path in pairs:
        ok = process_video(video_path, html_path)
        results.append((video_path.stem, ok))

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for stem, ok in results:
        print(f"  {'OK    ' if ok else 'FAILED'}  {stem}")
    if any(not ok for _, ok in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
