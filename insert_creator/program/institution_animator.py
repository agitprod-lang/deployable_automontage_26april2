#!/usr/bin/env python3
"""Project institution images inside the green paint transition clip."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "output"
TRANSITION_VIDEO = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/asset/"
    "cleen green chalk transition.mp4"
)
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
MASK_COLOR_HEX = "0x000000"
MASK_SIMILARITY = 0.05
MASK_BLEND = 0.02
GREEN_HEX = "0x36b741"
GREEN_SIMILARITY = 0.22
GREEN_BLEND = 0.08
PLACEMENT_SCALE = 0.32
ANCHOR_X_RATIO = 0.25
ANCHOR_Y_RATIO = 0.78
PLACEMENT_WIDTH = int(round(VIDEO_WIDTH * PLACEMENT_SCALE))
PLACEMENT_HEIGHT = int(round(VIDEO_HEIGHT * PLACEMENT_SCALE))
ANCHOR_X = int(
    max(
        min(round(VIDEO_WIDTH * ANCHOR_X_RATIO - PLACEMENT_WIDTH / 2), VIDEO_WIDTH - PLACEMENT_WIDTH),
        0,
    )
)
ANCHOR_Y = int(
    max(
        min(round(VIDEO_HEIGHT * ANCHOR_Y_RATIO - PLACEMENT_HEIGHT / 2), VIDEO_HEIGHT - PLACEMENT_HEIGHT),
        0,
    )
)
ARROW_VIDEO = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/insert_creator/asset/arrows/"
    "arrow2animationd.mov"
)
ARROW_SCALE = 0.65
ARROW_X_RATIO = 0.2
ARROW_X = int(max(min(round(VIDEO_WIDTH * ARROW_X_RATIO), VIDEO_WIDTH), 0))
ARROW_Y_RATIO = 0.65
ARROW_Y = int(max(min(round(VIDEO_HEIGHT * ARROW_Y_RATIO), VIDEO_HEIGHT), 0))
MANIFEST_SUFFIX = "_institutions_images.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fill the green paint transition with institution images.")
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Path to *_institutions_images.json (defaults to the newest one under output/).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Override the output directory (defaults to output/<stem>_institution_transitions).",
    )
    parser.add_argument(
        "--transition",
        type=Path,
        default=TRANSITION_VIDEO,
        help="Path to the green paint transition clip.",
    )
    parser.add_argument(
        "--arrow",
        type=Path,
        default=ARROW_VIDEO,
        help="Overlay arrow animation placed on the right.",
    )
    return parser.parse_args()


def find_latest_manifest(output_dir: Path) -> Path:
    candidates = list(output_dir.glob(f"*{MANIFEST_SUFFIX}"))
    if not candidates:
        raise FileNotFoundError("No *_institutions_images.json files found under output/.")
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0]


def load_manifest(path: Path) -> MutableMapping[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def derive_base_name(manifest_path: Path) -> str:
    name = manifest_path.name
    if name.endswith(MANIFEST_SUFFIX):
        return name[: -len(MANIFEST_SUFFIX)]
    return manifest_path.stem


def has_audio_stream(video_path: Path) -> bool:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return bool(result.stdout.strip())


def run_ffmpeg(
    transition_video: Path,
    image_path: Path,
    arrow_path: Path,
    output_path: Path,
    include_audio: bool,
) -> tuple[bool, str]:
    arrow_x_expr = f"min({ARROW_X},main_w-overlay_w)"
    arrow_y_expr = f"min({ARROW_Y},main_h-overlay_h)"
    filter_graph = (
        f"color=c=0x00000000:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:d=999[canvas];"
        "[0:v]format=rgba,"
        f"colorkey={MASK_COLOR_HEX}:{MASK_SIMILARITY}:{MASK_BLEND},"
        "format=rgba,alphaextract[mask_base];"
        "[mask_base]split[mask_pic][mask_alpha];"
        f"[1:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
        "setsar=1,format=rgba[pic];"
        "[pic][mask_pic]alphamerge[keyed];"
        "[0:v]format=rgba,"
        f"colorkey={GREEN_HEX}:{GREEN_SIMILARITY}:{GREEN_BLEND},"
        "format=rgba[edges];"
        "[keyed][edges]overlay[revealed];"
        "[revealed][mask_alpha]alphamerge[masked];"
        f"[masked]scale=w=round(iw*{PLACEMENT_SCALE}):h=round(ih*{PLACEMENT_SCALE}),setsar=1[scaled];"
        f"[canvas][scaled]overlay=x={ANCHOR_X}:y={ANCHOR_Y}[positioned];"
        f"[2:v]scale='iw*{ARROW_SCALE}':'ih*{ARROW_SCALE}',format=rgba[arrow];"
        f"[positioned][arrow]overlay=x='{arrow_x_expr}':y='{arrow_y_expr}'[out]"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(transition_video),
        "-loop",
        "1",
        "-i",
        str(image_path),
        "-stream_loop",
        "-1",
        "-i",
        str(arrow_path),
        "-filter_complex",
        filter_graph,
        "-map",
        "[out]",
    ]
    if include_audio:
        cmd.extend(["-map", "0:a?", "-c:a", "copy"])
    else:
        cmd.append("-an")
    cmd.extend(
        [
            "-c:v",
            "prores_ks",
            "-profile:v",
            "4444",
            "-pix_fmt",
            "yuva444p10le",
            "-shortest",
            str(output_path),
        ]
    )
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, result.stderr.strip()


def prepare_output_dirs(
    manifest_path: Path,
    override: Path | None,
) -> tuple[Path, Path, Path]:
    base_name = derive_base_name(manifest_path)
    base_output = override if override else OUTPUT_DIR / f"{base_name}_institution_transitions"
    video_dir = base_output / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    manifest_out = base_output / f"{base_name}_institution_transition_manifest.json"
    return base_output, video_dir, manifest_out


def create_videos(
    entries: Sequence[Mapping[str, object]],
    transition_clip: Path,
    arrow_clip: Path,
    video_dir: Path,
) -> List[Dict[str, object]]:
    include_audio = has_audio_stream(transition_clip)
    results: List[Dict[str, object]] = []
    clip_index = 1
    for entry_index, entry in enumerate(entries, start=1):
        institution_name = str(entry.get("institution") or entry.get("noun") or "")
        image_path_value = entry.get("image_path")
        if not image_path_value:
            print(f"   ⚠️  No image path for {institution_name}")
            continue
        image_path = Path(image_path_value)
        if not image_path.exists():
            print(f"   ⚠️  Image not found for {institution_name}: {image_path}")
            continue
        output_name = f"institution_{clip_index:03d}.mov"
        output_path = video_dir / output_name
        success, stderr = run_ffmpeg(transition_clip, image_path, arrow_clip, output_path, include_audio)
        if success:
            print(f"   🎬 {output_name} ({institution_name})")
        else:
            print(f"   ⚠️  FFmpeg failed for {image_path.name}: {stderr}")
        results.append(
            {
                "entry_index": entry_index,
                "institution": institution_name,
                "image_path": str(image_path),
                "video_path": str(output_path) if success else None,
                "success": success,
            }
        )
        clip_index += 1
    return results


def write_manifest(manifest_path: Path, records: Iterable[Mapping[str, object]], source_manifest: Path) -> None:
    payload = {
        "source_manifest": str(source_manifest),
        "videos": list(records),
    }
    manifest_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    args = parse_args()
    manifest_path = args.manifest if args.manifest else find_latest_manifest(OUTPUT_DIR)
    if not manifest_path.exists():
        raise FileNotFoundError(f"{manifest_path} does not exist.")
    if not args.transition.exists():
        raise FileNotFoundError(f"Transition clip not found: {args.transition}")
    if not args.arrow.exists():
        raise FileNotFoundError(f"Arrow clip not found: {args.arrow}")
    data = load_manifest(manifest_path)
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        print("No entries found in the institution manifest.")
        return
    base_output, video_dir, manifest_out = prepare_output_dirs(manifest_path, args.output_dir)
    print(f"Rendering clips into {video_dir} ...")
    video_records = create_videos(entries, args.transition, args.arrow, video_dir)
    write_manifest(manifest_out, video_records, manifest_path)
    print("\nSummary")
    print("=" * 40)
    print(f"Source manifest : {manifest_path}")
    print(f"Transition clip : {args.transition}")
    print(f"Video directory : {video_dir}")
    print(f"Manifest output : {manifest_out}")


if __name__ == "__main__":
    main()
