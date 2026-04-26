#!/usr/bin/env python3
"""
Take a cropped, polished screenshot of a tweet.
- Crops out the 'Relevant' footer section
- Adds rounded corners
- Adds drop shadow
- Saves as transparent PNG

Usage:
    python3.11 tweet_screenshot_1.py "https://x.com/user/status/123"

Requirements:
    - shot-scraper installed (pip install shot-scraper && shot-scraper install)
    - Pillow installed (pip install Pillow)
    - Auth file at /tmp/x_auth.json with your x.com auth_token cookie
"""

import sys
import re
import argparse
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

AUTH_FILE = str(Path(__file__).resolve().parent / "x_auth.json")
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "cli"
TWEET_SELECTOR = "article[data-testid='tweet']"
WAIT_MS = 5000
CORNER_RADIUS = 20
SHADOW_OFFSET = (6, 8)
SHADOW_BLUR = 14
SHADOW_COLOR = (0, 0, 0, 90)
WEBSITE_VIEWPORT_WIDTH = 1200
WEBSITE_VIEWPORT_HEIGHT = 900

# Maximum height (px) allowed for a tweet screenshot before assuming a cookie
# banner / overlay is included. Tall screenshots get cropped at this limit.
TWEET_MAX_HEIGHT_PX = 700

# JavaScript injected before screenshotting to dismiss cookie/consent dialogs.
_DISMISS_COOKIE_JS = """
(() => {
  const selectors = [
    '[data-testid="accept-cookies"]',
    '[data-testid="cookieBanner"] button',
    'button[aria-label*="Accept"]',
    'button[aria-label*="Decline"]',
    '[class*="cookie"] button',
    '[id*="cookie"] button',
  ];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el) { el.click(); return; }
  }
  for (const btn of document.querySelectorAll('button, a[role="button"]')) {
    const t = (btn.textContent || '').trim().toLowerCase();
    if (['accept all', 'accept cookies', 'accepter tout', 'accepter',
         'refuse', 'refuser', 'decline', 'reject all'].some(k => t.startsWith(k))) {
      btn.click(); return;
    }
  }
})();
"""


# ── Crop ──────────────────────────────────────────────────────────────────────

def is_background_row(pixels, y, width):
    """True if the row contains only white/near-white pixels (ignoring left border)."""
    for x in range(width):
        r, g, b = pixels[x, y]
        if r < 238 or g < 238 or b < 238:
            return False
    return True


def crop_relevant_section(img):
    """
    Remove the 'Relevant' footer that X.com appends inside the article element.

    Pattern (bottom to top):
        [white tail] → [Relevant text cluster] → [white gap ≥10px] → [tweet content]

    If that pattern is found, crop at the start of the white gap.
    Otherwise return the image trimmed of trailing whitespace only.
    """
    rgb = img.convert("RGB")
    pixels = rgb.load()
    w, h = img.size

    def is_bg(y):
        return is_background_row(pixels, y, w)

    y = h - 1

    # 1. Skip trailing background rows
    while y > 0 and is_bg(y):
        y -= 1
    content_end = y  # last row with actual pixels

    # 2. Skip the potential "Relevant" content cluster (going upward)
    cluster_bottom = y
    while y > 0 and not is_bg(y):
        y -= 1
    cluster_height = cluster_bottom - y

    # 3. Check how large the gap above the cluster is
    gap_bottom = y
    while y > 0 and is_bg(y):
        y -= 1
    gap_height = gap_bottom - y

    # If a small cluster (≤25px) sits above a significant gap (≥10px),
    # that cluster is the "Relevant" bar — crop at the gap start.
    if cluster_height <= 25 and gap_height >= 10:
        crop_y = y + 1  # first row after real tweet content
    else:
        crop_y = content_end + 4  # just trim trailing whitespace

    return img.crop((0, 0, w, crop_y))


# ── Rounded corners ───────────────────────────────────────────────────────────

def add_rounded_corners(img, radius):
    img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius=radius, fill=255)
    img.putalpha(mask)
    return img


# ── Drop shadow ───────────────────────────────────────────────────────────────

def add_drop_shadow(img, offset, blur, color):
    """Composite img (RGBA) onto a transparent canvas with a blurred shadow."""
    ox, oy = offset
    pad = blur * 2
    canvas_w = img.width + abs(ox) + pad * 2
    canvas_h = img.height + abs(oy) + pad * 2

    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    # Paint shadow using the image's alpha channel as a mask
    shadow_layer = Image.new("RGBA", img.size, color)
    alpha_mask = img.split()[3]
    shadow_x = pad + max(ox, 0)
    shadow_y = pad + max(oy, 0)
    canvas.paste(shadow_layer, (shadow_x, shadow_y), alpha_mask)
    canvas = canvas.filter(ImageFilter.GaussianBlur(blur))

    # Paste the original image on top
    img_x = pad + max(-ox, 0)
    img_y = pad + max(-oy, 0)
    canvas.paste(img, (img_x, img_y), img)

    return canvas


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_tweet_url(url: str) -> bool:
    lower = url.lower()
    return "twitter.com" in lower or "x.com/" in lower


def _screenshot_tweet(url: str, raw_path: Path) -> bool:
    """Use shot-scraper with Twitter auth + tweet selector.

    Injects JavaScript to dismiss cookie/consent dialogs before the element
    is captured, then waits WAIT_MS for the page to settle.
    """
    cmd = [
        "shot-scraper", url,
        "-s", TWEET_SELECTOR,
        "-o", str(raw_path),
        "-a", AUTH_FILE,
        "--wait", str(WAIT_MS),
        "--javascript", _DISMISS_COOKIE_JS,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: {result.stderr.strip()}")
        return False
    return True


def _screenshot_website(url: str, raw_path: Path) -> bool:
    """Use shot-scraper for a generic website — viewport crop, no auth."""
    cmd = [
        "shot-scraper", url,
        "-o", str(raw_path),
        "--width", str(WEBSITE_VIEWPORT_WIDTH),
        "--height", str(WEBSITE_VIEWPORT_HEIGHT),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: {result.stderr.strip()}")
        return False
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Screenshot a tweet or website URL.")
    parser.add_argument("url", help="URL to screenshot")
    parser.add_argument("-o", "--output", type=Path, default=None,
                        help="Output PNG path (default: auto-named in output/cli/)")
    args = parser.parse_args()

    url = args.url
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", url.replace("https://", "").replace("http://", "")).strip("_")[:60]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    raw_path = OUTPUT_DIR / f"{slug}_raw.png"
    final_path = args.output if args.output is not None else OUTPUT_DIR / f"{slug}.png"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)

    # 1. Take screenshot
    print(f"Screenshotting {url} ...")
    if _is_tweet_url(url):
        ok = _screenshot_tweet(url, raw_path)
    else:
        ok = _screenshot_website(url, raw_path)
    if not ok:
        return 1

    # 2. Post-process
    print("Post-processing ...")
    img = Image.open(raw_path)

    if _is_tweet_url(url):
        img = crop_relevant_section(img)
        # Safety crop: if still taller than expected, a cookie/consent overlay
        # was probably captured — hard-crop at the known maximum tweet height.
        if img.height > TWEET_MAX_HEIGHT_PX:
            img = img.crop((0, 0, img.width, TWEET_MAX_HEIGHT_PX))
    img = add_rounded_corners(img, CORNER_RADIUS)
    img = add_drop_shadow(img, SHADOW_OFFSET, SHADOW_BLUR, SHADOW_COLOR)

    img.save(final_path, "PNG")
    raw_path.unlink(missing_ok=True)

    print(f"Saved: {final_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
