#!/usr/bin/env python3
"""
Unified entrypoint that runs both the page and video downloaders.
"""

from __future__ import annotations

import logging
import sys

import image_downloader
import pages_downloader
import tweet_donwloader
import video_downloader


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    page_status = pages_downloader.main()
    image_status = image_downloader.main()
    video_status = video_downloader.main()
    tweet_status = tweet_donwloader.main()
    if page_status or image_status or video_status or tweet_status:
        logging.error(
            "Processing finished with errors (pages=%s, image=%s, video=%s, tweet=%s)",
            page_status,
            image_status,
            video_status,
            tweet_status,
        )
        return 1
    logging.info("Unified downloader finished successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
