#!/usr/bin/env python3
"""Create a transparent ransom-title MOV over the animated title background."""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, MutableMapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "output"
BACKGROUND_PATH = PROJECT_ROOT / "asset" / "background_for_title.mov"

RANSOM_NOTE_DIR = Path.home() / "Desktop" / "code" / "code_text" / "ransom-note-generator"
RANSOM_DIST_INDEX = RANSOM_NOTE_DIR / "dist" / "index.js"

DEFAULT_SPACING = 10
DEFAULT_MAX_LETTERS = 10
DEFAULT_TARGET_WIDTH = 1000
DEFAULT_SHOW_FROM_SECONDS = 1.0
DEFAULT_SHOW_UNTIL_SECONDS = 4.0
DEFAULT_CHANGE_EVERY_FRAMES = 10


@dataclass(frozen=True)
class BackgroundInfo:
    width: int
    height: int
    fps: float
    duration: float
    frame_count: int


@dataclass(frozen=True)
class PipelineContext:
    source_csv: Path
    base_output: Path
    video_dir: Path
    manifest_path: Path
    title_id: int
    row_index: int
    transcript_number: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a transparent ransom-note title MOV from direct text input."
    )
    parser.add_argument(
        "text",
        nargs="*",
        help="Optional title text. If omitted, the script reads stdin or prompts interactively.",
    )
    parser.add_argument(
        "--title-text",
        help="Explicit title text. Takes priority over positional text arguments.",
    )
    parser.add_argument(
        "--background",
        type=Path,
        default=BACKGROUND_PATH,
        help=f"Animated transparent background MOV (default: {BACKGROUND_PATH}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Exact output MOV path. Defaults to output/<slug>.mov.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=f"Override output directory when --output is not set (default: {OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--source-csv",
        type=Path,
        help="Source comparer CSV used to derive the pipeline manifest/output folder.",
    )
    parser.add_argument(
        "--title-id",
        type=int,
        help="Stable sequential title id for pipeline mode.",
    )
    parser.add_argument(
        "--row-index",
        type=int,
        help="Original comparer row index for pipeline mode.",
    )
    parser.add_argument(
        "--transcript-number",
        help="Transcript number metadata for pipeline mode.",
    )
    parser.add_argument(
        "--append-manifest",
        action="store_true",
        help="Merge the current title into the pipeline manifest instead of replacing it.",
    )
    parser.add_argument(
        "--spacing",
        type=int,
        default=DEFAULT_SPACING,
        help=f"Letter spacing passed to the ransom-note generator (default: {DEFAULT_SPACING}).",
    )
    parser.add_argument(
        "--max-letters",
        type=int,
        default=DEFAULT_MAX_LETTERS,
        help=f"Maximum letters per line before wrapping (default: {DEFAULT_MAX_LETTERS}).",
    )
    parser.add_argument(
        "--target-width",
        type=int,
        default=DEFAULT_TARGET_WIDTH,
        help=f"Target width used when normalizing ransom-note frames (default: {DEFAULT_TARGET_WIDTH}).",
    )
    parser.add_argument(
        "--show-from",
        type=float,
        default=DEFAULT_SHOW_FROM_SECONDS,
        help=f"Time in seconds when the title first appears (default: {DEFAULT_SHOW_FROM_SECONDS}).",
    )
    parser.add_argument(
        "--show-until",
        type=float,
        default=DEFAULT_SHOW_UNTIL_SECONDS,
        help=f"Time in seconds when the title disappears (default: {DEFAULT_SHOW_UNTIL_SECONDS}).",
    )
    parser.add_argument(
        "--change-every-frames",
        type=int,
        default=DEFAULT_CHANGE_EVERY_FRAMES,
        help=f"Hold each ransom-note variation for this many video frames (default: {DEFAULT_CHANGE_EVERY_FRAMES}).",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep the temporary PNG frame folders instead of deleting them.",
    )
    return parser.parse_args()


def ensure_dependencies() -> None:
    if shutil.which("node") is None:
        raise FileNotFoundError("`node` is required but was not found in PATH.")
    if shutil.which("ffmpeg") is None:
        raise FileNotFoundError("`ffmpeg` is required but was not found in PATH.")
    if shutil.which("ffprobe") is None:
        raise FileNotFoundError("`ffprobe` is required but was not found in PATH.")
    if not RANSOM_NOTE_DIR.exists():
        raise FileNotFoundError(f"Ransom note project missing at {RANSOM_NOTE_DIR}")
    if not RANSOM_DIST_INDEX.exists():
        raise FileNotFoundError(
            f"Ransom note build not found at {RANSOM_DIST_INDEX}. Run `npm run build` inside the repo first."
        )


def resolve_title_text(args: argparse.Namespace) -> str:
    if args.title_text and args.title_text.strip():
        return args.title_text.strip()
    joined = " ".join(part.strip() for part in args.text if part.strip()).strip()
    if joined:
        return joined
    if not sys.stdin.isatty():
        piped = sys.stdin.read().strip()
        if piped:
            return piped
    prompted = input("Title: ").strip()
    if prompted:
        return prompted
    raise ValueError("Title text is required.")


def normalize_text(value: str) -> str:
    """Strip accents, punctuation, and emoji so only ransom-supported characters remain."""
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = "".join(char for char in normalized if char.isascii())
    filtered = []
    for char in ascii_only:
        if char.isalnum() or char.isspace():
            filtered.append(char)
        else:
            filtered.append(" ")
    compacted = re.sub(r"\s+", " ", "".join(filtered)).strip()
    return compacted


def wrap_title_lines(value: str, max_letters: int) -> list[str]:
    if max_letters <= 0:
        raise ValueError("--max-letters must be greater than 0.")
    cleaned = normalize_text(value)
    if not cleaned:
        return []
    wrapped = textwrap.wrap(
        cleaned,
        width=max_letters,
        break_long_words=True,
        drop_whitespace=True,
        replace_whitespace=False,
    )
    return [line.strip().upper() for line in wrapped if line.strip()]


def slugify_filename(value: str) -> str:
    slug = normalize_text(value).lower().replace(" ", "_")
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "title"


def prepare_pipeline_output_dirs(base_dir: Path, stem: str) -> tuple[Path, Path, Path]:
    base_output = base_dir / f"{stem}_ransom_titles"
    video_dir = base_output / "videos"
    manifest_path = base_output / f"{stem}_ransom_titles_manifest.json"
    video_dir.mkdir(parents=True, exist_ok=True)
    return base_output, video_dir, manifest_path


def build_pipeline_context(args: argparse.Namespace) -> PipelineContext | None:
    if args.source_csv is None:
        if args.append_manifest:
            raise ValueError("--source-csv is required when using --append-manifest.")
        return None
    if args.title_id is None:
        raise ValueError("--title-id is required when using --source-csv.")
    csv_path = args.source_csv.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else OUTPUT_DIR
    base_output, video_dir, manifest_path = prepare_pipeline_output_dirs(output_dir, csv_path.stem)
    return PipelineContext(
        source_csv=csv_path,
        base_output=base_output,
        video_dir=video_dir,
        manifest_path=manifest_path,
        title_id=args.title_id,
        row_index=args.row_index or 1,
        transcript_number=(args.transcript_number or "").strip() or None,
    )


def load_manifest(path: Path) -> MutableMapping[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, MutableMapping):
        raise RuntimeError(f"Manifest at {path} is not a JSON object.")
    return data


def write_manifest(
    manifest_path: Path,
    titles: Sequence[Mapping[str, object]],
    context: PipelineContext,
    args: argparse.Namespace,
) -> None:
    data = {
        "source_csv": str(context.source_csv),
        "background_path": str(args.background.expanduser().resolve()),
        "options": {
            "spacing": args.spacing,
            "max_letters": args.max_letters,
            "target_width": args.target_width,
            "show_from_seconds": args.show_from,
            "show_until_seconds": args.show_until,
            "change_every_frames": args.change_every_frames,
        },
        "titles": list(titles),
    }
    manifest_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def find_existing_title_record(
    titles: Sequence[object],
    entry_id: int,
    row_index: int,
) -> MutableMapping[str, object] | None:
    for item in titles:
        if not isinstance(item, MutableMapping):
            continue
        if item.get("id") == entry_id or item.get("row_index") == row_index:
            return item
    return None


def merge_manifest_record(
    manifest_path: Path,
    record: Mapping[str, object],
    context: PipelineContext,
    args: argparse.Namespace,
) -> None:
    if manifest_path.exists():
        data = load_manifest(manifest_path)
    else:
        data = {}
    titles = data.get("titles")
    if not isinstance(titles, list):
        titles = []
        data["titles"] = titles
    existing = find_existing_title_record(titles, context.title_id, context.row_index)
    if existing is None:
        titles.append(dict(record))
    else:
        existing.clear()
        existing.update(record)
    titles.sort(key=lambda item: (int(item.get("id") or 0), int(item.get("row_index") or 0)))
    write_manifest(manifest_path, titles, context, args)


def parse_fraction(value: str | None) -> float:
    if not value:
        return 0.0
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        try:
            top = float(numerator)
            bottom = float(denominator)
        except ValueError:
            return 0.0
        if bottom == 0:
            return 0.0
        return top / bottom
    try:
        return float(value)
    except ValueError:
        return 0.0


def probe_background(path: Path) -> BackgroundInfo:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height,r_frame_rate,duration:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(completed.stdout)
    streams = payload.get("streams") or []
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if video_stream is None:
        raise RuntimeError(f"No video stream found in {path}")

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    fps = parse_fraction(video_stream.get("r_frame_rate")) or 25.0
    duration = float(video_stream.get("duration") or payload.get("format", {}).get("duration") or 0.0)
    if width <= 0 or height <= 0 or duration <= 0 or fps <= 0:
        raise RuntimeError(f"Unable to read valid video metadata from {path}")
    frame_count = max(1, int(round(duration * fps)))
    return BackgroundInfo(width=width, height=height, fps=fps, duration=duration, frame_count=frame_count)


def list_frame_paths(frame_dir: Path) -> list[Path]:
    return sorted(frame_dir.glob("frame_*.png"))


def render_ransom_frame_sequence(
    text: str,
    output_dir: Path,
    total_frame_count: int,
    active_start_frame: int,
    active_end_frame: int,
    spacing: int,
    seed: int,
    target_width: int,
    change_every_frames: int,
) -> None:
    script = textwrap.dedent(
        f"""
        const fs = require('node:fs/promises');
        const path = require('node:path');
        const sharp = require('sharp');
        const {{ RansomNote, BACKGROUND_COLOR }} = require({json.dumps(str(RANSOM_DIST_INDEX))});

        async function main() {{
            const outputDir = {json.dumps(str(output_dir))};
            const text = {json.dumps(text)};
            const totalFrameCount = {total_frame_count};
            const activeStartFrame = {active_start_frame};
            const activeEndFrame = {active_end_frame};
            const spacing = {spacing};
            const seed = {seed};
            const targetWidth = {target_width};
            const changeEveryFrames = {change_every_frames};
            const backgroundColor = BACKGROUND_COLOR.TRANSPARENT;
            const ransom = new RansomNote({{ seed, spacing, backgroundColor }});
            const resizedFrames = [];
            let maxWidth = 0;
            let maxHeight = 0;
            const activeFrameCount = Math.max(0, activeEndFrame - activeStartFrame);
            const variantCount = Math.max(1, Math.ceil(activeFrameCount / changeEveryFrames));

            await fs.mkdir(outputDir, {{ recursive: true }});
            if (activeFrameCount <= 0) {{
                throw new Error('Visible title window does not contain any frames.');
            }}

            for (let index = 0; index < variantCount; index += 1) {{
                const result = await ransom.generateImageBuffer(text, {{ seed, spacing, backgroundColor }});
                const resized = await sharp(result.imageBuffer)
                    .resize({{
                        width: targetWidth,
                        fit: 'contain',
                        position: 'right',
                        background: {{ r: 0, g: 0, b: 0, alpha: 0 }},
                    }})
                    .png()
                    .toBuffer();
                const metadata = await sharp(resized).metadata();
                maxWidth = Math.max(maxWidth, metadata.width || targetWidth);
                maxHeight = Math.max(maxHeight, metadata.height || 1);
                resizedFrames.push(resized);
            }}

            const blankFrame = await sharp({{
                create: {{
                    width: maxWidth,
                    height: maxHeight,
                    channels: 4,
                    background: {{ r: 0, g: 0, b: 0, alpha: 0 }},
                }},
            }}).png().toBuffer();

            const normalizedFrames = [];
            for (let index = 0; index < resizedFrames.length; index += 1) {{
                const normalized = await sharp(resizedFrames[index])
                    .resize(maxWidth, maxHeight, {{
                        fit: 'contain',
                        position: 'right',
                        background: {{ r: 0, g: 0, b: 0, alpha: 0 }},
                    }})
                    .png()
                    .toBuffer();
                normalizedFrames.push(normalized);
            }}

            for (let index = 0; index < totalFrameCount; index += 1) {{
                let frameBuffer = blankFrame;
                if (index >= activeStartFrame && index < activeEndFrame) {{
                    const relativeIndex = index - activeStartFrame;
                    const variantIndex = Math.min(
                        normalizedFrames.length - 1,
                        Math.floor(relativeIndex / changeEveryFrames),
                    );
                    frameBuffer = normalizedFrames[variantIndex];
                }}
                const filename = path.join(outputDir, `frame_${{String(index + 1).padStart(5, '0')}}.png`);
                await fs.writeFile(filename, frameBuffer);
            }}
        }}

        main().catch((error) => {{
            console.error(error && error.stack ? error.stack : error);
            process.exit(1);
        }});
        """
    ).strip()
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=str(RANSOM_NOTE_DIR),
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        message = stderr or stdout or "Unknown node error"
        raise RuntimeError(message)

def build_output_path(raw_text: str, args: argparse.Namespace, context: PipelineContext | None) -> Path:
    if context is not None:
        return context.video_dir / f"title_{context.title_id:03d}.mov"
    if args.output:
        return args.output.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else OUTPUT_DIR
    return output_dir / f"{slugify_filename(raw_text)}.mov"


def build_final_video(
    background_path: Path,
    line_dirs: Sequence[Path],
    output_path: Path,
    background_info: BackgroundInfo,
) -> None:
    if not line_dirs:
        raise RuntimeError("At least one line frame directory is required.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fps_value = f"{background_info.fps:.6f}"
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(background_path),
    ]
    for line_dir in line_dirs:
        command.extend(
            [
                "-framerate",
                fps_value,
                "-start_number",
                "1",
                "-i",
                str(line_dir / "frame_%05d.png"),
            ]
        )

    if len(line_dirs) == 1:
        filter_complex = "[0:v][1:v]overlay=x=(main_w-overlay_w)/2:y=(main_h-overlay_h)/2:format=auto[v]"
    else:
        stacked_inputs = "".join(f"[{index}:v]" for index in range(1, len(line_dirs) + 1))
        filter_complex = (
            f"{stacked_inputs}vstack=inputs={len(line_dirs)}[stacked];"
            "[0:v][stacked]overlay=x=(main_w-overlay_w)/2:y=(main_h-overlay_h)/2:format=auto[v]"
        )

    command.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-c:v",
            "prores_ks",
            "-profile:v",
            "4444",
            "-pix_fmt",
            "yuva444p10le",
            "-alpha_bits",
            "16",
            "-r",
            fps_value,
            "-c:a",
            "copy",
            "-shortest",
            str(output_path),
        ]
    )
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        message = stderr or stdout or "Unknown ffmpeg error"
        raise RuntimeError(message)


def main() -> None:
    args = parse_args()
    ensure_dependencies()

    raw_text = resolve_title_text(args)
    wrapped_lines = wrap_title_lines(raw_text, args.max_letters)
    if not wrapped_lines:
        raise ValueError("Title is empty after removing unsupported characters and emojis.")
    if args.target_width <= 0:
        raise ValueError("--target-width must be greater than 0.")
    if args.show_from < 0:
        raise ValueError("--show-from must be greater than or equal to 0.")
    if args.show_until <= args.show_from:
        raise ValueError("--show-until must be greater than --show-from.")
    if args.change_every_frames <= 0:
        raise ValueError("--change-every-frames must be greater than 0.")

    pipeline_context = build_pipeline_context(args)
    background_path = args.background.expanduser().resolve()
    if not background_path.exists():
        raise FileNotFoundError(f"Background video not found: {background_path}")

    background_info = probe_background(background_path)
    visible_start_frame = min(background_info.frame_count, max(0, int(round(args.show_from * background_info.fps))))
    visible_end_frame = min(background_info.frame_count, int(round(args.show_until * background_info.fps)))
    if visible_end_frame <= visible_start_frame:
        raise ValueError("Visible title window falls outside the background duration.")
    output_path = build_output_path(raw_text, args, pipeline_context)
    temp_root = Path(tempfile.mkdtemp(prefix="ransom_title_video_"))

    try:
        line_dirs: list[Path] = []
        for line_index, line_text in enumerate(wrapped_lines, start=1):
            line_dir = temp_root / f"line_{line_index:02d}"
            render_ransom_frame_sequence(
                text=line_text,
                output_dir=line_dir,
                total_frame_count=background_info.frame_count,
                active_start_frame=visible_start_frame,
                active_end_frame=visible_end_frame,
                spacing=args.spacing,
                seed=random.randint(1000, 9_999_999),
                target_width=args.target_width,
                change_every_frames=args.change_every_frames,
            )
            if len(list_frame_paths(line_dir)) != background_info.frame_count:
                raise RuntimeError(f"Incomplete frame sequence generated for line {line_index}: {line_text}")
            line_dirs.append(line_dir)

        build_final_video(background_path, line_dirs, output_path, background_info)
    finally:
        if args.keep_temp:
            print(f"Temporary frames kept at: {temp_root}")
        else:
            shutil.rmtree(temp_root, ignore_errors=True)

    if pipeline_context is not None:
        record = {
            "id": pipeline_context.title_id,
            "row_index": pipeline_context.row_index,
            "transcript_number": pipeline_context.transcript_number,
            "title": raw_text,
            "sanitized_title": normalize_text(raw_text),
            "video_path": str(output_path),
            "show_from_seconds": args.show_from,
            "show_until_seconds": args.show_until,
            "visible_duration_seconds": args.show_until - args.show_from,
            "change_every_frames": args.change_every_frames,
            "duration_seconds": background_info.duration,
            "success": True,
        }
        if args.append_manifest:
            merge_manifest_record(pipeline_context.manifest_path, record, pipeline_context, args)
        else:
            write_manifest(pipeline_context.manifest_path, [record], pipeline_context, args)

    print(f"Title      : {raw_text}")
    print(f"Sanitized  : {normalize_text(raw_text)}")
    print(f"Lines      : {' | '.join(wrapped_lines)}")
    print(f"Background : {background_path}")
    print(f"Output     : {output_path}")
    if pipeline_context is not None:
        print(f"Manifest   : {pipeline_context.manifest_path}")
    print(f"Visible    : {args.show_from:.3f}s -> {args.show_until:.3f}s")
    print(f"Change     : every {args.change_every_frames} frames")
    print(f"Duration   : {background_info.duration:.3f}s @ {background_info.fps:.3f} fps")


if __name__ == "__main__":
    main()
