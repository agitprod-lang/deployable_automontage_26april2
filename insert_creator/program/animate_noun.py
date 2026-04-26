#!/usr/bin/env python3
"""Animate noun images using intro/hold/outro paper transitions."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping, Sequence

import cv2
import numpy as np
from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "output"
GREEN_TRANSPARENCY_SCRIPT = PROJECT_ROOT / "program" / "processor" / "make_green_transparent.py"
INTRO_VIDEO = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/insert_creator/asset/"
    "intro_paper_noun__minus12db.mov"
)
OUTRO_VIDEO = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/insert_creator/asset/"
    "outro_paper_nouns__minus12db.mov"
)
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
BLUE_HEX = "0x0044b9"
BLUE_SIMILARITY = 0.13
BLUE_BLEND = 0.07
GREEN_HEX = "0x00af3e"
GREEN_SIMILARITY = 0.24
GREEN_BLEND = 0.08
PLACEMENT_SCALE = 0.42
ANCHOR_X_RATIO = 0.25
ANCHOR_Y_RATIO = 0.62
PLACEMENT_WIDTH = int(round(VIDEO_WIDTH * PLACEMENT_SCALE))
PLACEMENT_HEIGHT = int(round(VIDEO_HEIGHT * PLACEMENT_SCALE))
ANCHOR_X = int(max(min(round(VIDEO_WIDTH * ANCHOR_X_RATIO - PLACEMENT_WIDTH / 2), VIDEO_WIDTH - PLACEMENT_WIDTH), 0))
ANCHOR_Y = int(max(min(round(VIDEO_HEIGHT * ANCHOR_Y_RATIO - PLACEMENT_HEIGHT / 2), VIDEO_HEIGHT - PLACEMENT_HEIGHT), 0))
HOLD_DURATION = 3.0
REMOVE_BG_CACHE_DIR = PROJECT_ROOT / ".rembg_cache"
REMOVE_BG_PYTHON = Path("/opt/homebrew/opt/python@3.11/bin/python3.11")
REMOVE_BG_SCRIPT = r"""
from rembg import remove
from PIL import Image
from pathlib import Path
import sys

source = Path(sys.argv[1]).expanduser()
target = Path(sys.argv[2]).expanduser()
target.parent.mkdir(parents=True, exist_ok=True)
with Image.open(source) as img:
    result = remove(img)
    result.save(target)
"""
PAPER_TEXTURE_DIR = Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/shared_assets/paper_cut/paper")
NOISY_EXPANSION = 55
NOISY_NOISE_AMOUNT = 25
NOISY_PADDING = 20

FACE_MODEL_PATH = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/Trash/cutter/res10_300x300_ssd_iter_140000.caffemodel"
)
FACE_CONFIG_PATH = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/Trash/cutter/deploy.prototxt"
)
EYE_CASCADE_PATH = Path(cv2.data.haarcascades) / "haarcascade_eye.xml"
FACE_TARGET_X = ANCHOR_X + PLACEMENT_WIDTH / 2
FACE_TARGET_Y = ANCHOR_Y + PLACEMENT_HEIGHT / 2
FACE_INTERMEDIATE_X = FACE_TARGET_X / PLACEMENT_SCALE - ANCHOR_X / PLACEMENT_SCALE
FACE_INTERMEDIATE_Y = FACE_TARGET_Y / PLACEMENT_SCALE - ANCHOR_Y / PLACEMENT_SCALE

_FACE_NET: "cv2.dnn_Net | None" = None
_EYE_CASCADE: "cv2.CascadeClassifier | None" = None
_FACE_DETECTORS_LOADED = False


def _load_face_detectors() -> tuple["cv2.dnn_Net | None", "cv2.CascadeClassifier | None"]:
    global _FACE_NET, _EYE_CASCADE, _FACE_DETECTORS_LOADED
    if _FACE_DETECTORS_LOADED:
        return _FACE_NET, _EYE_CASCADE
    _FACE_DETECTORS_LOADED = True
    if FACE_MODEL_PATH.exists() and FACE_CONFIG_PATH.exists():
        try:
            _FACE_NET = cv2.dnn.readNetFromCaffe(str(FACE_CONFIG_PATH), str(FACE_MODEL_PATH))
        except cv2.error:
            _FACE_NET = None
    if EYE_CASCADE_PATH.exists():
        cascade = cv2.CascadeClassifier(str(EYE_CASCADE_PATH))
        if not cascade.empty():
            _EYE_CASCADE = cascade
    return _FACE_NET, _EYE_CASCADE


def detect_face_focus(image_path: Path) -> tuple[float, float] | None:
    """Return (x, y) of the face focus in source-image pixel coordinates, or None."""
    net, eye_cascade = _load_face_detectors()
    if net is None:
        return None
    raw = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
    if raw is None:
        return None
    if raw.ndim == 2:
        frame = cv2.cvtColor(raw, cv2.COLOR_GRAY2BGR)
    elif raw.shape[2] == 4:
        alpha = raw[..., 3:4].astype(np.float32) / 255.0
        frame = (raw[..., :3].astype(np.float32) * alpha).astype(np.uint8)
    else:
        frame = raw
    h, w = frame.shape[:2]
    blob = cv2.dnn.blobFromImage(frame, 1.0, (300, 300), (104.0, 177.0, 123.0))
    net.setInput(blob)
    dets = net.forward()
    best_box: tuple[int, int, int, int] | None = None
    best_score = 0.0
    for i in range(dets.shape[2]):
        confidence = float(dets[0, 0, i, 2])
        if confidence < 0.55:
            continue
        box = dets[0, 0, i, 3:7] * np.array([w, h, w, h])
        x1, y1, x2, y2 = box.astype(int)
        x1 = max(0, min(x1, w - 1))
        y1 = max(0, min(y1, h - 1))
        x2 = max(x1 + 1, min(x2, w))
        y2 = max(y1 + 1, min(y2, h))
        score = confidence * (x2 - x1) * (y2 - y1)
        if score > best_score:
            best_score = score
            best_box = (x1, y1, x2, y2)
    if best_box is None:
        return None
    x1, y1, x2, y2 = best_box
    if eye_cascade is not None:
        roi = frame[y1:y2, x1:x2]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        eyes = eye_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(12, 12))
        if len(eyes) >= 2:
            sorted_eyes = sorted(eyes, key=lambda b: b[2] * b[3], reverse=True)[:2]
            cx = sum(x1 + ex + ew / 2.0 for ex, ey, ew, eh in sorted_eyes) / len(sorted_eyes)
            cy = sum(y1 + ey + eh / 2.0 for ex, ey, ew, eh in sorted_eyes) / len(sorted_eyes)
            return cx, cy
    return x1 + (x2 - x1) / 2.0, y1 + (y2 - y1) * 0.38


def build_face_centered_canvas(source_png: Path, target_png: Path) -> tuple[bool, str]:
    """Render source_png onto a 1920x1080 transparent canvas, positioned so that the
    detected face focus (eyes if available, otherwise face center biased upward) lands
    at (FACE_INTERMEDIATE_X, FACE_INTERMEDIATE_Y) — the intermediate-canvas coordinates
    that, after the fixed PLACEMENT_SCALE/ANCHOR pipeline, map to (FACE_TARGET_X,
    FACE_TARGET_Y) in the final frame. Falls back to image-centered layout when no
    face is detected."""
    try:
        with Image.open(source_png) as img:
            image = img.convert("RGBA")
    except Exception as exc:  # noqa: BLE001
        return False, f"Failed to load {source_png}: {exc}"
    w, h = image.size
    scale = min(VIDEO_WIDTH / w, VIDEO_HEIGHT / h)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    scaled = image.resize((new_w, new_h), Image.Resampling.LANCZOS)

    focus = detect_face_focus(source_png)
    if focus is None:
        offset_x = (VIDEO_WIDTH - new_w) // 2
        offset_y = (VIDEO_HEIGHT - new_h) // 2
        method = "center (no face detected)"
    else:
        fx, fy = focus
        offset_x = int(round(FACE_INTERMEDIATE_X - fx * scale))
        offset_y = int(round(FACE_INTERMEDIATE_Y - fy * scale))
        method = f"face@({fx:.0f},{fy:.0f})"

    canvas = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    canvas.paste(scaled, (offset_x, offset_y), scaled)
    target_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target_png)
    return True, method


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fill the blue canvas of the unfolding paper transition with noun imagery."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Path to *_nouns_images.json (defaults to the newest one under output/).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Override the output directory (defaults to output/<stem>_nouns_transitions).",
    )
    parser.add_argument(
        "--intro",
        type=Path,
        default=INTRO_VIDEO,
        help="Intro transition video that reveals the noun image.",
    )
    parser.add_argument(
        "--outro",
        type=Path,
        default=OUTRO_VIDEO,
        help="Outro transition video.",
    )
    parser.add_argument(
        "--hold-duration",
        type=float,
        default=HOLD_DURATION,
        help="Hold duration between intro and outro with a static image (seconds).",
    )
    return parser.parse_args()


def find_latest_manifest(output_dir: Path) -> Path:
    candidates = sorted(output_dir.glob("*_nouns_images.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError("No *_nouns_images.json files found under output/.")
    return candidates[0]


def load_manifest(path: Path) -> MutableMapping[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def derive_base_name(manifest_path: Path) -> str:
    suffix = "_nouns_images.json"
    name = manifest_path.name
    if name.endswith(suffix):
        return name[: -len(suffix)]
    return manifest_path.stem


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = normalized.lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    normalized = normalized.strip("_")
    return normalized or "noun"


def prepare_output_dirs(base_name: str, manifest_path: Path, override: Path | None) -> tuple[Path, Path, Path]:
    base_output = override if override else OUTPUT_DIR / f"{base_name}_nouns_transitions"
    video_dir = base_output / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    manifest_out = base_output / f"{base_name}_nouns_transition_manifest.json"
    return base_output, video_dir, manifest_out


def iter_image_entries(data: Mapping[str, object]) -> Iterable[Mapping[str, object]]:
    entries = data.get("entries")
    if not isinstance(entries, Sequence):
        return []
    return (entry for entry in entries if isinstance(entry, Mapping))


def remove_background_image(source: Path, destination: Path) -> tuple[bool, str]:
    cache_dir = REMOVE_BG_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("NUMBA_CACHE_DIR", str(cache_dir))
    cmd = [
        str(REMOVE_BG_PYTHON),
        "-c",
        REMOVE_BG_SCRIPT,
        str(source),
        str(destination),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return result.returncode == 0, (result.stderr.strip() or result.stdout.strip())


def prepare_cutout(image_path: Path, cutout_dir: Path) -> tuple[Path | None, str | None]:
    cutout_dir.mkdir(parents=True, exist_ok=True)
    cutout_path = cutout_dir / f"{image_path.stem}_nobg.png"
    if cutout_path.exists():
        try:
            if cutout_path.stat().st_mtime >= image_path.stat().st_mtime:
                return cutout_path, None
        except FileNotFoundError:
            pass
    success, message = remove_background_image(image_path, cutout_path)
    if success:
        return cutout_path, None
    return None, message or "Background removal failed."


def add_noise_to_contour(contour: np.ndarray, noise_amount: float) -> np.ndarray:
    if contour.ndim != 3:
        contour = contour.reshape(-1, 1, 2)
    noisy = contour.copy().astype(np.float32)
    noise = np.random.uniform(-noise_amount, noise_amount, size=noisy.shape)
    noisy += noise
    return noisy.astype(np.int32)


def create_noisy_paper_shape(
    cutout_path: Path,
    destination: Path,
    paper_texture_path: Path,
    expansion: int = NOISY_EXPANSION,
    noise_amount: float = NOISY_NOISE_AMOUNT,
) -> tuple[bool, str | None]:
    try:
        with Image.open(cutout_path) as img:
            rgba = img.convert("RGBA")
    except Exception as exc:  # pragma: no cover - IO edge cases
        return False, str(exc)
    try:
        with Image.open(paper_texture_path) as texture_img:
            paper_texture = texture_img.convert("RGB")
    except Exception as exc:  # pragma: no cover
        return False, f"Failed to load paper texture: {exc}"
    alpha = np.array(rgba.split()[-1])
    if not np.any(alpha):
        return False, "Alpha channel empty."
    contours, _ = cv2.findContours(alpha, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return False, "No contours detected."
    kernel_size = max(expansion * 2 + 1, 3)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    dilated = cv2.dilate(alpha, kernel, iterations=1)
    expanded_contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not expanded_contours:
        return False, "Failed to expand contours."
    expanded = max(expanded_contours, key=cv2.contourArea)
    noisy_contour = add_noise_to_contour(expanded, noise_amount)
    points = noisy_contour.reshape(-1, 2)
    min_x = int(np.min(points[:, 0]))
    max_x = int(np.max(points[:, 0]))
    min_y = int(np.min(points[:, 1]))
    max_y = int(np.max(points[:, 1]))
    width = max(max_x - min_x + 2 * NOISY_PADDING, 2)
    height = max(max_y - min_y + 2 * NOISY_PADDING, 2)
    texture_resized = paper_texture.resize((width, height), Image.Resampling.LANCZOS).convert("RGBA")
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    offset_x = NOISY_PADDING - min_x
    offset_y = NOISY_PADDING - min_y
    adjusted = [(int(pt[0] + offset_x), int(pt[1] + offset_y)) for pt in points]
    draw.polygon(adjusted, fill=255)
    textured_shape = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    textured_shape.paste(texture_resized, (0, 0))
    textured_shape.putalpha(mask)
    final_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    final_img.paste(textured_shape, (0, 0), textured_shape)
    final_img.paste(rgba, (int(offset_x), int(offset_y)), rgba)
    destination.parent.mkdir(parents=True, exist_ok=True)
    final_img.save(destination)
    return True, None


def prepare_noisy_shape(
    cutout_path: Path,
    noisy_dir: Path,
    paper_texture_path: Path,
) -> tuple[Path | None, str | None]:
    noisy_dir.mkdir(parents=True, exist_ok=True)
    noisy_path = noisy_dir / f"{cutout_path.stem}_paper.png"
    try:
        cutout_mtime = cutout_path.stat().st_mtime
    except FileNotFoundError:
        return None, "Cutout missing."
    if noisy_path.exists():
        try:
            if noisy_path.stat().st_mtime >= cutout_mtime:
                return noisy_path, None
        except FileNotFoundError:
            pass
    success, message = create_noisy_paper_shape(cutout_path, noisy_path, paper_texture_path)
    if success:
        return noisy_path, None
    return None, message or "Paper cut effect failed."


def find_paper_texture(texture_dir: Path = PAPER_TEXTURE_DIR) -> Path:
    candidates: List[Path] = []
    for pattern in ("*.png", "*.jpg", "*.jpeg", "*.PNG", "*.JPG", "*.JPEG"):
        candidates.extend(texture_dir.glob(pattern))
    if not candidates:
        raise FileNotFoundError(f"No paper textures found under {texture_dir}")
    return sorted(candidates)[0]


def has_audio_stream(video_path: Path) -> bool:
    ffprobe_bin = find_ffprobe_binary()
    cmd = [
        ffprobe_bin,
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
    return result.returncode == 0 and bool(result.stdout.strip())


def find_ffprobe_binary() -> str:
    candidate = shutil.which("ffprobe")
    if candidate:
        return candidate
    fallback = Path("/opt/homebrew/bin/ffprobe")
    if fallback.exists():
        return str(fallback)
    raise FileNotFoundError("ffprobe executable not found. Install ffmpeg or adjust PATH.")


def render_transition_segment(
    transition_video: Path,
    image_path: Path,
    output_path: Path,
    include_audio: bool,
) -> tuple[bool, str]:
    filter_graph = (
        "[0:v]format=rgba,"
        f"colorkey={BLUE_HEX}:{BLUE_SIMILARITY}:{BLUE_BLEND},"
        "format=rgba[transition_blue_keyed];"
        "[1:v]scale="
        f"{VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:({VIDEO_WIDTH}-iw)/2:({VIDEO_HEIGHT}-ih)/2:color=#00000000@0,"
        "setsar=1,format=rgba[photo];"
        "[photo][transition_blue_keyed]overlay=format=auto[combined];"
        "[combined]format=rgba,"
        f"colorkey={GREEN_HEX}:{GREEN_SIMILARITY}:{GREEN_BLEND},"
        "format=rgba[keyed];"
        "[keyed]scale="
        f"w=round(iw*{PLACEMENT_SCALE}):h=round(ih*{PLACEMENT_SCALE})[scaled];"
        f"[scaled]pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:{ANCHOR_X}:{ANCHOR_Y}:color=0x00000000,"
        "format=rgba[out]"
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
        "-filter_complex",
        filter_graph,
        "-map",
        "[out]",
        "-c:v",
        "prores_ks",
        "-profile:v",
        "4444",
        "-pix_fmt",
        "yuva444p10le",
    ]
    if include_audio:
        cmd.extend(
            [
                "-map",
                "0:a?",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
            ]
        )
    else:
        cmd.append("-an")
    cmd.extend(["-shortest", str(output_path)])
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, result.stderr.strip()


def render_hold_segment(
    image_path: Path,
    duration: float,
    output_path: Path,
) -> tuple[bool, str]:
    filter_graph = (
        "[0:v]scale="
        f"{VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:({VIDEO_WIDTH}-iw)/2:({VIDEO_HEIGHT}-ih)/2:color=#00000000,"
        "setsar=1,format=rgba[photo];"
        "[photo]scale="
        f"w=round(iw*{PLACEMENT_SCALE}):h=round(ih*{PLACEMENT_SCALE})[scaled];"
        f"[scaled]pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:{ANCHOR_X}:{ANCHOR_Y}:color=0x00000000,"
        "format=rgba[out]"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-i",
        str(image_path),
        "-f",
        "lavfi",
        "-t",
        f"{duration}",
        "-i",
        "anullsrc=r=48000:cl=stereo",
        "-filter_complex",
        filter_graph,
        "-t",
        f"{duration}",
        "-map",
        "[out]",
        "-map",
        "1:a",
        "-c:v",
        "prores_ks",
        "-profile:v",
        "4444",
        "-pix_fmt",
        "yuva444p10le",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-shortest",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, result.stderr.strip()


def apply_png_alpha_matte(
    video_path: Path,
    png_path: Path,
    output_path: Path,
) -> tuple[bool, str]:
    filter_graph = (
        "[0:v]format=rgba,"
        f"colorkey={GREEN_HEX}:{GREEN_SIMILARITY}:{GREEN_BLEND},"
        "format=rgba[anim];"
        "[1:v]scale="
        f"{VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:({VIDEO_WIDTH}-iw)/2:({VIDEO_HEIGHT}-ih)/2:color=#00000000@0,"
        "setsar=1,format=rgba[photo_padded];"
        "[photo_padded]scale="
        f"w=round(iw*{PLACEMENT_SCALE}):h=round(ih*{PLACEMENT_SCALE})[photo_scaled];"
        f"[photo_scaled]pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:{ANCHOR_X}:{ANCHOR_Y}:color=0x00000000,"
        "format=rgba[positioned];"
        "[positioned]alphaextract,format=gray[alpha_mask];"
        "[anim][alpha_mask]alphamerge[out]"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-loop",
        "1",
        "-i",
        str(png_path),
        "-filter_complex",
        filter_graph,
        "-map",
        "[out]",
        "-map",
        "0:a?",
        "-c:v",
        "prores_ks",
        "-profile:v",
        "4444",
        "-pix_fmt",
        "yuva444p10le",
        "-c:a",
        "copy",
        "-shortest",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, result.stderr.strip()


def apply_green_transparency(videos: Iterable[Path]) -> Dict[Path, Path]:
    script_path = GREEN_TRANSPARENCY_SCRIPT
    if not script_path.exists():
        print(f"⚠️  Green transparency script not found: {script_path}")
        return {}
    python_exec = sys.executable or "python3"
    processed: Dict[Path, Path] = {}
    seen: set[Path] = set()
    for video in videos:
        if not video:
            continue
        video_path = Path(video)
        if video_path in seen:
            continue
        seen.add(video_path)
        if not video_path.exists():
            print(f"   ⚠️  Video missing for transparency: {video_path}")
            continue
        transparent_path = video_path.with_name(f"{video_path.stem}_transparent.mov")
        cmd = [
            python_exec,
            str(script_path),
            str(video_path),
            "--keep-audio",
            "--erode-alpha",
            "--despill-green",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            try:
                if video_path.exists():
                    video_path.unlink()
            except OSError:
                pass
            try:
                transparent_path.rename(video_path)
                processed[video_path] = video_path
                print(f"      ↳ Green removed -> {video_path.name}")
            except OSError as exc:
                print(f"   ⚠️  Failed to replace {video_path.name}: {exc}")
                try:
                    transparent_path.unlink()
                except OSError:
                    pass
        else:
            detail = result.stderr.strip() or result.stdout.strip() or "Unknown error."
            print(f"   ⚠️  Green transparency failed for {video_path.name}: {detail}")
            if transparent_path.exists():
                try:
                    transparent_path.unlink()
                except OSError:
                    pass
    return processed


def concat_segments(segments: Sequence[Path], output_path: Path) -> tuple[bool, str]:
    if len(segments) != 3:
        raise ValueError("concat_segments expects exactly three segments (intro, hold, outro).")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(segments[0]),
        "-i",
        str(segments[1]),
        "-i",
        str(segments[2]),
        "-filter_complex",
        "[0:v][0:a][1:v][1:a][2:v][2:a]concat=n=3:v=1:a=1[v][a]",
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "prores_ks",
        "-profile:v",
        "4444",
        "-pix_fmt",
        "yuva444p10le",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, result.stderr.strip()


def create_videos(
    entries: Iterable[Mapping[str, object]],
    intro_clip: Path,
    outro_clip: Path,
    cutout_dir: Path,
    noisy_dir: Path,
    paper_texture_path: Path,
    video_dir: Path,
    hold_duration: float,
    intro_has_audio: bool,
    outro_has_audio: bool,
) -> List[Dict[str, object]]:
    records: List[Dict[str, object]] = []
    slug_counts: Dict[str, int] = {}
    cutout_cache: Dict[Path, Path] = {}
    noisy_cache: Dict[Path, Path] = {}
    for entry in entries:
        image_path = entry.get("image_path")
        if not image_path:
            continue
        noun = str(entry.get("noun") or "noun")
        img_path = Path(str(image_path))
        if not img_path.exists():
            print(f"⚠️  Missing image for {noun}: {img_path}")
            continue
        cutout_path = cutout_cache.get(img_path)
        if not cutout_path:
            cutout_path, removal_error = prepare_cutout(img_path, cutout_dir)
            if not cutout_path:
                detail = removal_error.splitlines()[-1] if removal_error else "remove_bg run failed."
                print(f"   ❌ Background removal failed for {noun}: {detail}")
                continue
            cutout_cache[img_path] = cutout_path
        noisy_path = noisy_cache.get(cutout_path)
        if not noisy_path:
            noisy_path, noisy_error = prepare_noisy_shape(cutout_path, noisy_dir, paper_texture_path)
            if not noisy_path:
                detail = noisy_error.splitlines()[-1] if noisy_error else "Paper cut pipeline failed."
                print(f"   ❌ Paper texture failed for {noun}: {detail}")
                continue
            noisy_cache[cutout_path] = noisy_path
        slug = slugify(noun)
        slug_counts[slug] = slug_counts.get(slug, 0) + 1
        suffix = slug_counts[slug]
        output_slug = f"{slug}_{suffix}" if suffix > 1 else slug
        output_path = video_dir / f"{output_slug}.mov"
        print(f"🎞️  Rendering {noun} -> {output_path.name}")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            face_centered_path = tmpdir_path / "face_centered.png"
            centered_ok, centering_method = build_face_centered_canvas(noisy_path, face_centered_path)
            if not centered_ok:
                print(f"   ❌ Face-center preprocessing failed for {noun}: {centering_method}")
                continue
            print(f"   🎯 Face-centered via {centering_method}")
            render_source = face_centered_path
            intro_segment = tmpdir_path / "intro.mov"
            hold_segment = tmpdir_path / "hold.mov"
            outro_segment = tmpdir_path / "outro.mov"
            intro_success, intro_err = render_transition_segment(
                intro_clip, render_source, intro_segment, intro_has_audio
            )
            if not intro_success:
                message = intro_err.splitlines()[-1] if intro_err else "Intro render failed."
                print(f"   ❌ Intro failed for {noun}: {message}")
                continue
            hold_success, hold_err = render_hold_segment(
                render_source, hold_duration, hold_segment
            )
            if not hold_success:
                message = hold_err.splitlines()[-1] if hold_err else "Hold render failed."
                print(f"   ❌ Hold failed for {noun}: {message}")
                continue
            outro_success, outro_err = render_transition_segment(
                outro_clip, render_source, outro_segment, outro_has_audio
            )
            if not outro_success:
                message = outro_err.splitlines()[-1] if outro_err else "Outro render failed."
                print(f"   ❌ Outro failed for {noun}: {message}")
                continue
            final_success, final_err = concat_segments(
                (intro_segment, hold_segment, outro_segment), output_path
            )
            if final_success:
                print(f"   ✅ Saved {output_path}")
                filled_path = output_path.with_name(f"{output_path.stem}_filled.mov")
                filled_success, filled_err = apply_png_alpha_matte(output_path, render_source, filled_path)
                final_output_path = output_path
                if filled_success:
                    print(f"   🎯 Matte applied -> {filled_path.name}")
                    try:
                        if output_path.exists():
                            output_path.unlink()
                    except OSError:
                        pass
                    final_output_path = filled_path
                else:
                    detail = filled_err.splitlines()[-1] if filled_err else "Matte render failed."
                    print(f"   ⚠️  Matte application failed for {noun}: {detail}")
                    try:
                        if filled_path.exists():
                            filled_path.unlink()
                    except Exception:
                        pass
                records.append(
                    {
                        "noun": noun,
                        "category": entry.get("category"),
                        "image_path": str(img_path),
                        "cutout_path": str(cutout_path),
                        "paper_path": str(noisy_path),
                        "video_path": str(final_output_path),
                    }
                )
            else:
                message = final_err.splitlines()[-1] if final_err else "Concat failed."
                print(f"   ❌ Failed to assemble {noun}: {message}")
    return records


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir
    try:
        manifest_path = args.manifest if args.manifest else find_latest_manifest(OUTPUT_DIR)
    except FileNotFoundError as exc:
        print(f"❌ {exc}")
        return
    try:
        manifest_data = load_manifest(manifest_path)
    except json.JSONDecodeError as exc:
        print(f"❌ Failed to parse manifest {manifest_path}: {exc}")
        return
    base_name = derive_base_name(manifest_path)
    base_output, video_dir, manifest_out = prepare_output_dirs(base_name, manifest_path, output_dir)
    intro_clip = args.intro
    outro_clip = args.outro
    hold_duration = max(float(args.hold_duration), 0.1)
    for asset_path, label in (
        (intro_clip, "Intro transition"),
        (outro_clip, "Outro transition"),
    ):
        if not asset_path.exists():
            print(f"❌ {label} not found: {asset_path}")
            return
    if not REMOVE_BG_PYTHON.exists():
        print(f"❌ Background removal interpreter not found: {REMOVE_BG_PYTHON}")
        return
    try:
        paper_texture_path = find_paper_texture()
    except FileNotFoundError as exc:
        print(f"❌ {exc}")
        return
    cutout_dir = base_output / "nobg_images"
    noisy_dir = base_output / "paper_textured_images"
    intro_has_audio = has_audio_stream(intro_clip)
    outro_has_audio = has_audio_stream(outro_clip)
    missing_transition_audio: List[str] = []
    if not intro_has_audio:
        missing_transition_audio.append(f"intro transition audio missing: {intro_clip}")
    if not outro_has_audio:
        missing_transition_audio.append(f"outro transition audio missing: {outro_clip}")
    if missing_transition_audio:
        print("❌ Noun transition assets must include embedded paper audio.")
        for detail in missing_transition_audio:
            print(f"   - {detail}")
        return
    entries = list(iter_image_entries(manifest_data))
    if not entries:
        print("❌ Manifest does not contain any entries.")
        return
    records = create_videos(
        entries,
        intro_clip,
        outro_clip,
        cutout_dir,
        noisy_dir,
        paper_texture_path,
        video_dir,
        hold_duration,
        intro_has_audio,
        outro_has_audio,
    )
    if not records:
        print("❌ No videos were created.")
        return
    videos_for_transparency: List[Path] = []
    for record in records:
        video_raw = record.get("video_path")
        if video_raw:
            videos_for_transparency.append(Path(video_raw))
    if videos_for_transparency:
        print("\nApplying final green transparency cleanup…")
        apply_green_transparency(videos_for_transparency)
    manifest_out.write_text(json.dumps({"entries": records}, indent=2), encoding="utf-8")
    print(f"\nManifest written to {manifest_out}")


if __name__ == "__main__":
    main()
