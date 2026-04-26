#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import Error as PlaywrightError, sync_playwright
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.firefox.options import Options as FirefoxOptions


SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR.parents[2] / ".env")
FEATURE_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = FEATURE_DIR.parent.parent
ASSET_PATH = FEATURE_DIR / "asset" / "screenshot_background.png"
OUTPUT_DIR = PROJECT_ROOT / "output"

VIEWPORT_BOX = (180, 316, 1344, 973)
ADDRESS_BAR_BOX = (360, 224, 1210, 282)
PAGE_VIEWPORT = {"width": 1440, "height": 900}

DEFAULT_WAIT_MS = 2500
CLOUDFLARE_WAIT_MS = 8000

SCREENSHOTONE_ENDPOINT = "https://api.screenshotone.com/take"

_PLAYWRIGHT_REPAIRABLE_MARKERS = (
    "chromium executable path is unavailable",
    "chromium executable not found",
    "chromium executable is not runnable",
    "executable doesn't exist",
    "please run the following command to download new browsers",
    "chromium distribution 'chromium' is not found",
)
_PLAYWRIGHT_REPAIR_ARGS = ("-m", "playwright", "install", "chromium", "--force", "--no-shell")

_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
Object.defineProperty(navigator, 'languages', {get: () => ['fr-FR', 'fr', 'en-US', 'en']});
window.chrome = {runtime: {}};
"""

_REAL_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_CLOUDFLARE_MARKERS = [
    "performing security verification",
    "verify you are human",
    "enable javascript and cookies",
    "checking your browser",
    "just a moment",
    "ddos protection by cloudflare",
]


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


def _is_repairable_playwright_error(exc: BaseException) -> bool:
    return any(marker in str(exc).lower() for marker in _PLAYWRIGHT_REPAIRABLE_MARKERS)


def _repair_playwright() -> None:
    """Re-install Chromium and strip the macOS Gatekeeper quarantine attribute."""
    cmd = [sys.executable, *_PLAYWRIGHT_REPAIR_ARGS]
    print(f"[screen_url] Playwright repair: {' '.join(cmd)}", file=sys.stderr)
    subprocess.run(cmd, check=False)
    cache_dir = Path.home() / ".cache" / "ms-playwright"
    if cache_dir.exists():
        subprocess.run(
            ["xattr", "-r", "-d", "com.apple.quarantine", str(cache_dir)],
            check=False,
            capture_output=True,
        )


def _is_cloudflare_blocked(page) -> bool:
    title = (page.title() or "").lower()
    body = ""
    try:
        body = page.inner_text("body").lower()
    except Exception:
        pass
    combined = title + " " + body
    return any(marker in combined for marker in _CLOUDFLARE_MARKERS)


def capture_page(url: str, destination: Path, wait_ms: int) -> None:
    """Try stealth Playwright, fall back to ScreenshotOne API if Cloudflare blocks."""
    cloudflare_error = None

    try:
        capture_page_with_playwright(url, destination, wait_ms)
        return
    except RuntimeError as exc:
        if "cloudflare" in str(exc).lower():
            cloudflare_error = exc
        else:
            raise
    except Exception as exc:
        print(f"[screen_url] Playwright failed for {url}: {exc}", file=sys.stderr)

    # Playwright failed — try ScreenshotOne
    try:
        capture_page_with_screenshotone(url, destination)
        return
    except Exception as api_exc:
        if cloudflare_error:
            raise RuntimeError(
                f"Cloudflare blocked headless browser and ScreenshotOne also failed: {api_exc}"
            ) from cloudflare_error
        raise


def capture_page_with_playwright(url: str, destination: Path, wait_ms: int) -> None:
    """Launch Playwright with one auto-repair attempt on Chromium installation errors."""
    repair_attempted = False
    while True:
        try:
            _run_playwright_capture(url, destination, wait_ms)
            return
        except RuntimeError as exc:
            if not repair_attempted and _is_repairable_playwright_error(exc):
                repair_attempted = True
                print(f"[screen_url] Playwright bootstrap issue detected: {exc}", file=sys.stderr)
                _repair_playwright()
                continue
            raise


def _run_playwright_capture(url: str, destination: Path, wait_ms: int) -> None:
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                ]
            )
        except PlaywrightError as exc:
            raise RuntimeError(f"Playwright launch failed: {exc}") from exc

        context = browser.new_context(
            viewport=PAGE_VIEWPORT,
            device_scale_factor=1,
            user_agent=_REAL_USER_AGENT,
            locale="fr-FR",
            timezone_id="Europe/Paris",
        )
        context.add_init_script(_STEALTH_SCRIPT)
        page = context.new_page()

        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
        except Exception:
            page.goto(url, wait_until="load", timeout=45000)

        if wait_ms > 0:
            page.wait_for_timeout(wait_ms)

        if _is_cloudflare_blocked(page):
            page.wait_for_timeout(CLOUDFLARE_WAIT_MS)

        if _is_cloudflare_blocked(page):
            browser.close()
            raise RuntimeError(
                f"Cloudflare bot protection blocked access to {url}."
            )

        page.screenshot(path=str(destination), full_page=False)
        browser.close()


def capture_page_with_screenshotone(url: str, destination: Path) -> None:
    api_key = os.environ.get("SCREENSHOTONE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "SCREENSHOTONE_API_KEY environment variable is not set. "
            "Get a free key at https://screenshotone.com"
        )

    params = {
        "access_key": api_key,
        "url": url,
        "viewport_width": PAGE_VIEWPORT["width"],
        "viewport_height": PAGE_VIEWPORT["height"],
        "format": "png",
        "full_page": "false",
        "wait_until": "networkidle2",
        "delay": 2,
    }

    response = requests.get(SCREENSHOTONE_ENDPOINT, params=params, timeout=90)

    if response.status_code != 200:
        raise RuntimeError(
            f"ScreenshotOne API returned {response.status_code}: {response.text[:200]}"
        )

    destination.write_bytes(response.content)


def capture_page_with_selenium(url: str, destination: Path, wait_ms: int) -> None:
    try:
        driver = build_chrome_driver()
    except Exception:
        driver = build_firefox_driver()

    try:
        driver.get(url)
        if wait_ms > 0:
            time.sleep(wait_ms / 1000)

        title = driver.title.lower()
        if any(marker in title for marker in _CLOUDFLARE_MARKERS):
            time.sleep(CLOUDFLARE_WAIT_MS / 1000)

        title = driver.title.lower()
        if any(marker in title for marker in _CLOUDFLARE_MARKERS):
            raise RuntimeError(
                f"Cloudflare bot protection blocked access to {url}."
            )

        driver.save_screenshot(str(destination))
    finally:
        driver.quit()


def build_chrome_driver() -> webdriver.Chrome:
    options = ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(f"--user-agent={_REAL_USER_AGENT}")
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
