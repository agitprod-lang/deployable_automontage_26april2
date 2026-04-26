#!/usr/bin/env python3

from __future__ import annotations

import argparse
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.firefox.options import Options as FirefoxOptions


SCRIPT_DIR = Path(__file__).resolve().parent
FEATURE_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = FEATURE_DIR.parent.parent
ASSET_PATH = FEATURE_DIR / "asset" / "screenshot_background.png"
OUTPUT_DIR = PROJECT_ROOT / "output"

VIEWPORT_BOX = (180, 316, 1344, 973)
ADDRESS_BAR_BOX = (360, 224, 1210, 282)
PAGE_VIEWPORT = {"width": 1440, "height": 900}

DEFAULT_WAIT_MS = 2500


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture a web page and composite it into the screenshot background asset."
    )
    parser.add_argument("url", help="URL to capture.")
    parser.add_argument(
        "-o",
        "--output",
        help="Optional output file path. Defaults to /output/<sanitized-url>.png",
    )
    parser.add_argument(
        "--wait-ms",
        type=int,
        default=DEFAULT_WAIT_MS,
        help="Extra wait time after page load before capturing the screenshot.",
    )
    return parser.parse_args()


def normalize_url(raw_url: str) -> str:
    if "://" not in raw_url:
        return f"https://{raw_url}"
    return raw_url


def slugify_url(url: str) -> str:
    parsed = urlparse(url)
    slug_source = parsed.netloc + parsed.path
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", slug_source).strip("_").lower()
    return slug or "page"


def pick_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates: Iterable[Path] = (
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/System/Library/Fonts/Supplemental/Helvetica.ttc"),
        Path("/Library/Fonts/Arial.ttf"),
    )
    for candidate in candidates:
        if candidate.exists():
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def capture_page(url: str, destination: Path, wait_ms: int) -> None:
    try:
        capture_page_with_playwright(url, destination, wait_ms)
        return
    except Exception:
        capture_page_with_selenium(url, destination, wait_ms)


def capture_page_with_playwright(url: str, destination: Path, wait_ms: int) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport=PAGE_VIEWPORT, device_scale_factor=1)
        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
        except Exception:
            page.goto(url, wait_until="load", timeout=45000)
        if wait_ms > 0:
            page.wait_for_timeout(wait_ms)
        page.screenshot(path=str(destination), full_page=False)
        browser.close()


def capture_page_with_selenium(url: str, destination: Path, wait_ms: int) -> None:
    try:
        driver = build_chrome_driver()
    except Exception:
        driver = build_firefox_driver()

    try:
        driver.get(url)
        if wait_ms > 0:
            time.sleep(wait_ms / 1000)
        driver.save_screenshot(str(destination))
    finally:
        driver.quit()


def build_chrome_driver() -> webdriver.Chrome:
    options = ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument(f"--window-size={PAGE_VIEWPORT['width']},{PAGE_VIEWPORT['height']}")
    return webdriver.Chrome(options=options)


def build_firefox_driver() -> webdriver.Firefox:
    options = FirefoxOptions()
    options.add_argument("--headless")
    driver = webdriver.Firefox(options=options)
    driver.set_window_size(PAGE_VIEWPORT["width"], PAGE_VIEWPORT["height"])
    return driver


def fit_cover(image: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    target_width, target_height = target_size
    scale = max(target_width / image.width, target_height / image.height)
    resized = image.resize(
        (round(image.width * scale), round(image.height * scale)),
        Image.Resampling.LANCZOS,
    )
    left = max(0, (resized.width - target_width) // 2)
    top = max(0, (resized.height - target_height) // 2)
    return resized.crop((left, top, left + target_width, top + target_height))


def truncate_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "..."
    current = text
    while current:
        current = current[:-1]
        candidate = current.rstrip() + ellipsis
        if draw.textlength(candidate, font=font) <= max_width:
            return candidate
    return ellipsis


def compose_mockup(page_shot_path: Path, url_text: str, output_path: Path) -> None:
    background = Image.open(ASSET_PATH).convert("RGBA")
    page_shot = Image.open(page_shot_path).convert("RGBA")

    viewport_left, viewport_top, viewport_right, viewport_bottom = VIEWPORT_BOX
    viewport_width = viewport_right - viewport_left
    viewport_height = viewport_bottom - viewport_top
    fitted_page = fit_cover(page_shot, (viewport_width, viewport_height))
    background.alpha_composite(fitted_page, dest=(viewport_left, viewport_top))

    draw = ImageDraw.Draw(background)
    bar_left, bar_top, bar_right, bar_bottom = ADDRESS_BAR_BOX
    draw.rounded_rectangle(
        ADDRESS_BAR_BOX,
        radius=(bar_bottom - bar_top) // 2,
        fill=(229, 233, 238, 255),
    )

    font = pick_font(28)
    text_margin = 28
    max_text_width = (bar_right - bar_left) - (text_margin * 2)
    display_text = truncate_text(draw, url_text, font, max_text_width)
    text_bbox = draw.textbbox((0, 0), display_text, font=font)
    text_height = text_bbox[3] - text_bbox[1]
    text_x = bar_left + text_margin
    text_y = bar_top + ((bar_bottom - bar_top - text_height) // 2) - 2
    draw.text((text_x, text_y), display_text, font=font, fill=(0, 0, 0, 255))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    background.save(output_path)


def build_output_path(url: str, explicit_output: str | None) -> Path:
    if explicit_output:
        return Path(explicit_output).expanduser().resolve()
    return OUTPUT_DIR / f"{slugify_url(url)}_screen_url.png"


def main() -> int:
    args = parse_args()
    url = normalize_url(args.url)
    output_path = build_output_path(url, args.output)

    try:
        with tempfile.TemporaryDirectory(prefix="screen_url_") as temp_dir:
            screenshot_path = Path(temp_dir) / "page.png"
            capture_page(url, screenshot_path, args.wait_ms)
            compose_mockup(screenshot_path, url, output_path)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
