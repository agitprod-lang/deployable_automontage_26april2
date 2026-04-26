#!/usr/bin/env python3
"""Quick test: composite text at the new MONEY_TEXT_TOP_Y position on an existing base video."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from add_text_to_animation import composite_text_over_video, MONEY_TEXT_TOP_Y

BASE_VIDEO = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/insert_creator/output"
    "/nicolascage_20260424_150638_groq_html_comparison_money_media/base_videos/money_001.mov"
)
OUTPUT_VIDEO = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/insert_creator/output"
    "/nicolascage_20260424_150638_groq_html_comparison_money_media/videos/money_001_test0.mov"
)
TEXT = "270 000 $"

print(f"Text     : {TEXT}")
print(f"Y pos    : {MONEY_TEXT_TOP_Y}  (adjust MONEY_TEXT_TOP_Y in add_text_to_animation.py)")
print(f"Base     : {BASE_VIDEO}")
print(f"Output   : {OUTPUT_VIDEO}")
print("Rendering frames... (takes ~30s)")

composite_text_over_video(BASE_VIDEO, TEXT, OUTPUT_VIDEO, fixed_y=MONEY_TEXT_TOP_Y)
print(f"\nDone -> {OUTPUT_VIDEO}")
