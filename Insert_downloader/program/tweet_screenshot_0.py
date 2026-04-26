#!/usr/bin/env python3
"""
Take a cropped screenshot of a tweet.
Wraps shot-scraper with the tweet CSS selector to auto-crop around the tweet.

Usage:
    python3.11 tweet_screenshot_0.py "https://x.com/user/status/123"

Requirements:
    - shot-scraper installed (pip install shot-scraper && shot-scraper install)
    - Auth file at /tmp/x_auth.json with your x.com auth_token cookie
"""

import sys
import subprocess
from pathlib import Path

AUTH_FILE = "/tmp/x_auth.json"
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "cli"
TWEET_SELECTOR = "article[data-testid='tweet']"
WAIT_MS = 5000


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python3.11 tweet_screenshot_0.py <tweet_url>")
        return 1

    url = sys.argv[1]

    # Derive filename from tweet ID (last path segment)
    tweet_id = url.rstrip("/").split("/")[-1]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / f"{tweet_id}.png"

    cmd = [
        "shot-scraper", url,
        "-s", TWEET_SELECTOR,
        "-o", str(output_path),
        "-a", AUTH_FILE,
        "--wait", str(WAIT_MS),
    ]

    print(f"Screenshotting {url} ...")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.stdout:
        print(result.stdout.strip())
    if result.returncode != 0:
        print(f"ERROR: {result.stderr.strip()}")
        return 1

    print(f"Saved: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
