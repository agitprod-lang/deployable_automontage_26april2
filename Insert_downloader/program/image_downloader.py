#!/usr/bin/env python3
"""
Generate insert videos for direct image links found in the latest HTML input.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from downloader_utils import (
    BASE_DIR,
    build_summary_entry,
    clean_url,
    is_image_url,
    prepare_processing_context,
    summary_entry_is_complete,
)

logger = logging.getLogger("insert_downloader.image")

SCRIPT_DIR = Path(__file__).resolve().parent
DOWNLOAD_IMAGE_SCRIPT = SCRIPT_DIR / "download_image.py"
PYTHON_BIN = os.environ.get("INSERT_DL_PYTHON_BIN", sys.executable or "python3.11")


def _expected_insert_path(image_url: str, output_dir: Path) -> Path:
    parsed = urlparse(image_url)
    raw_name = Path(parsed.path).stem or "downloaded_image"
    safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw_name).strip("._")
    safe_name = safe_name or "downloaded_image"
    return output_dir / f"{safe_name}_downloaded_polaroid_insert.mov"


def _find_generated_insert(image_url: str, output_dir: Path) -> Optional[Path]:
    expected = _expected_insert_path(image_url, output_dir)
    if expected.exists():
        return expected
    prefix = expected.stem.removesuffix("_insert")
    candidates = sorted(
        output_dir.glob(f"{prefix}*_insert.mov"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _render_image_insert(image_url: str, output_dir: Path) -> Optional[Path]:
    cmd = [
        PYTHON_BIN,
        str(DOWNLOAD_IMAGE_SCRIPT),
        image_url,
        "--output-dir",
        str(output_dir),
    ]
    logger.info("Rendering image insert: %s", image_url)
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        logger.error("download_image.py failed for %s: %s", image_url, exc)
        return None
    generated = _find_generated_insert(image_url, output_dir)
    if generated is None or not generated.exists():
        logger.error("Could not locate generated image insert for %s", image_url)
        return None
    return generated


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        html_path, _, links, target_dir, summary = prepare_processing_context()
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1

    processed = 0
    for index, link in enumerate(links, start=1):
        cleaned_url = clean_url(link.href)
        parsed = urlparse(cleaned_url)
        if not parsed.scheme.startswith("http"):
            continue
        if not is_image_url(cleaned_url):
            continue

        existing = summary.entries.get(index)
        if summary_entry_is_complete("image", existing, url=cleaned_url):
            continue

        label = link.text.strip()
        source = parsed.netloc or None
        output_path = _render_image_insert(cleaned_url, target_dir)
        entry = build_summary_entry(
            label=label,
            url=cleaned_url,
            entry_type="image",
            source=source,
            extra={
                "title": label or None,
                "image_url": cleaned_url,
                "generated_insert_video": (
                    os.path.relpath(output_path, BASE_DIR) if output_path is not None else None
                ),
                "error": None if output_path is not None else "image_insert_generation_failed",
            },
            downloaded_file=output_path,
        )
        summary.update_entry(index, entry)
        if output_path is not None:
            processed += 1

    summary.save()
    logger.info("Processed %d image inserts", processed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
