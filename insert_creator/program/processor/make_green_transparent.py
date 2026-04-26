#!/usr/bin/env python3
"""Key out green backgrounds and export an alpha-enabled video."""

from __future__ import annotations

import argparse
import shutil
import string
import subprocess
import sys
from pathlib import Path


DEFAULT_COLOR = "00ff00"
DEFAULT_SIMILARITY = 0.27
DEFAULT_BLEND = 0.05
OUTPUT_SUFFIX = ".mov"
ERODE_ALPHA_COORDINATES = 255
DESPILL_MIX = 1.0
DESPILL_EXPAND = 0.12
DESPILL_BRIGHTNESS = -0.02


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def parse_hex_color(value: str) -> str:
    cleaned = value.strip().lower()
    if cleaned.startswith("#"):
        cleaned = cleaned[1:]
    if cleaned.startswith("0x"):
        cleaned = cleaned[2:]
    if len(cleaned) != 6 or any(ch not in string.hexdigits for ch in cleaned):
        raise argparse.ArgumentTypeError("color must be in RRGGBB hex format")
    return cleaned


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove green backgrounds from a video and export with transparency."
    )
    parser.add_argument("input", type=Path, help="Source video that contains the green screen.")
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        help=f"Destination video (defaults to input name + '_transparent{OUTPUT_SUFFIX}').",
    )
    parser.add_argument(
        "--color",
        type=parse_hex_color,
        default=DEFAULT_COLOR,
        metavar="RRGGBB",
        help="Green chroma color to remove (default: %(default)s).",
    )
    parser.add_argument(
        "--similarity",
        type=positive_float,
        default=DEFAULT_SIMILARITY,
        help="How closely a pixel must match the key color (higher removes more).",
    )
    parser.add_argument(
        "--blend",
        type=positive_float,
        default=DEFAULT_BLEND,
        help="Feathering value to soften the keyed edges.",
    )
    parser.add_argument(
        "--codec",
        choices=("prores_ks", "qtrle"),
        default="prores_ks",
        help="Video codec for the transparent export (default: %(default)s).",
    )
    parser.add_argument(
        "--keep-audio",
        action="store_true",
        help="Copy audio from the source into the output (off by default).",
    )
    parser.add_argument(
        "--erode-alpha",
        action="store_true",
        help="Slightly contract the keyed alpha to remove thin green halos.",
    )
    parser.add_argument(
        "--despill-green",
        action="store_true",
        help="Neutralize residual green spill before re-merging alpha.",
    )
    return parser.parse_args()


def ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        print("ffmpeg is required but was not found in PATH.", file=sys.stderr)
        sys.exit(1)


def build_output_path(src: Path, dst: Path | None) -> Path:
    if dst is not None:
        return dst if dst.suffix else dst.with_suffix(OUTPUT_SUFFIX)
    return src.with_name(f"{src.stem}_transparent{OUTPUT_SUFFIX}")


def build_filter_complex(
    color: str,
    similarity: float,
    blend: float,
    *,
    erode_alpha: bool,
    despill_green: bool,
) -> str:
    chroma = f"0x{color}"
    # Preserve any alpha already present by multiplying it with the chroma key result.
    parts = [
        "[0:v]format=rgba,split=2[key_base][alpha_src];",
        f"[key_base]colorkey={chroma}:{similarity}:{blend},split=2[key_color][key_alpha_src];",
        "[alpha_src]alphaextract[orig_alpha];",
        "[key_alpha_src]alphaextract[key_alpha];",
        "[orig_alpha][key_alpha]blend=all_mode='multiply'[combined_alpha_base];",
    ]
    if erode_alpha:
        parts.append(
            "[combined_alpha_base]format=gray,"
            f"erosion=coordinates={ERODE_ALPHA_COORDINATES}[combined_alpha];"
        )
    else:
        parts.append("[combined_alpha_base]copy[combined_alpha];")
    if despill_green:
        parts.append(
            "[key_color]"
            f"despill=type=green:mix={DESPILL_MIX}:expand={DESPILL_EXPAND}:brightness={DESPILL_BRIGHTNESS},"
            "format=rgb24[color_only];"
        )
    else:
        parts.append("[key_color]format=rgb24[color_only];")
    parts.append("[color_only][combined_alpha]alphamerge,format=rgba[out]")
    return "".join(parts)


def run_chroma_key(
    input_path: Path,
    output_path: Path,
    filter_expr: str,
    codec: str,
    keep_audio: bool,
) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-filter_complex",
        filter_expr,
        "-map",
        "[out]",
    ]
    if codec == "prores_ks":
        cmd += [
            "-c:v",
            "prores_ks",
            "-profile:v",
            "4444",
            "-pix_fmt",
            "yuva444p10le",
            "-bits_per_mb",
            "8000",
        ]
    else:
        cmd += [
            "-c:v",
            "qtrle",
            "-pix_fmt",
            "argb",
    ]
    if keep_audio:
        cmd += [
            "-map",
            "0:a?",
            "-c:a",
            "copy",
        ]
    else:
        cmd += ["-an"]
    cmd += [
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main() -> None:
    ensure_ffmpeg()
    args = parse_args()
    input_path: Path = args.input
    if not input_path.exists():
        print(f"Input video '{input_path}' not found.", file=sys.stderr)
        sys.exit(1)
    output_path = build_output_path(input_path, args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    filter_expr = build_filter_complex(
        args.color,
        args.similarity,
        args.blend,
        erode_alpha=args.erode_alpha,
        despill_green=args.despill_green,
    )
    try:
        run_chroma_key(
            input_path=input_path,
            output_path=output_path,
            filter_expr=filter_expr,
            codec=args.codec,
            keep_audio=args.keep_audio,
        )
    except subprocess.CalledProcessError:
        print("ffmpeg failed to encode the transparent video.", file=sys.stderr)
        sys.exit(1)
    print(f"Transparent video saved to {output_path}")


if __name__ == "__main__":
    main()
