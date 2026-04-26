from __future__ import annotations

from pathlib import Path

FRAME_RATE = 30.0

WORKING_HEADER = [
    "Row ID",
    "Kind",
    "Start Time",
    "End Time",
    "Text",
    "Eliminate",
    "Eliminate Reason",
    "Repeat Group",
    "Repeat Role",
    "Reference Segment",
    "Match %",
    "Status",
    "Anchor ID",
    "Notes",
]

PIPE_COMPAT_HEADER = [
    "Keep",
    "Transcript #",
    "Start Time",
    "End Time",
    "Text",
    "Reference Segment",
    "Match %",
    "Status",
]

DIAGNOSTIC_HEADER = [
    "Keep",
    "Transcript #",
    "Start Time",
    "End Time",
    "Text",
    "Reference Segment",
    "Match %",
    "Status",
    "Kind",
    "Eliminate",
    "Eliminate Reason",
    "Repeat Group",
    "Repeat Role",
    "Anchor ID",
    "Notes",
]

LEGACY_TAG_HEADER = [
    "Intro Tag",
    "Commentez Tag",
    "Tippee Tag",
    "Abonnez Tag",
    "Zoom",
]

LEGACY_FEATURE_HEADER = [
    "Person Mention",
    "Location Mention",
    "Gov Institution",
    "Brand Mention",
    "Quote Extracted",
    "Money Mention",
    "Number Mention",
    "Date Mention",
    "Book Mention",
    "Key Words",
    "Feeling and Emotion",
]

LEGACY_TITLE_NEWS_HEADER = [
    "Titles",
    "Relevant News",
]

LEGACY_HTML_METRIC_HEADER = [
    "Hashtags",
    "Article Links",
    "Video Links",
    "Image Links",
    "Key Points",
    "Percent Mention",
    "Decibel Mention",
    "Speed Mention",
    "Weight Object Mention",
    "Weight Person Mention",
    "Distance Mention",
    "Temperature Mention",
    "Surface Mention",
    "Volume Mention",
    "Social Network Mention",
    "City Mention",
    "Country Mention",
    "Ranking Mention",
    "Spoken URL",
    "Punctuation Signal",
    "Bold Text",
    "Concrete Emoji",
    "CTA Detected",
    "Italic Text",
    "Underlined Text",
    "List Marker",
    "List Type",
    "List Block",
]

ENRICHED_EXPLICIT_HEADER = [
    "Title Level",
    "Excerpt Video Links",
    "Direct Video Links",
]

ENRICHED_DIAGNOSTIC_HEADER = [
    "Kind",
    "Eliminate",
    "Eliminate Reason",
    "Repeat Group",
    "Repeat Role",
    "Anchor ID",
    "Notes",
]

ENRICHED_HEADER = (
    PIPE_COMPAT_HEADER
    + LEGACY_TAG_HEADER
    + LEGACY_FEATURE_HEADER
    + LEGACY_TITLE_NEWS_HEADER
    + LEGACY_HTML_METRIC_HEADER
    + ENRICHED_EXPLICIT_HEADER
    + ENRICHED_DIAGNOSTIC_HEADER
)

REF_LEFTOVER_HEADER = [
    "Ref Span #",
    "Text",
    "Start Offset",
    "End Offset",
    "Token Count",
    "Previous Transcript #",
    "Previous Text",
    "Next Transcript #",
    "Next Text",
    "Status",
]

WORD_TIMELINE_HEADER = [
    "Row ID",
    "Transcript #",
    "Keep",
    "Eliminate",
    "Kind",
    "Status",
    "Row Start Time",
    "Row End Time",
    "Transcript Token Index",
    "Transcript Token",
    "Reference Token Index",
    "Reference Token",
    "Alignment Type",
    "Alignment Confidence",
    "Timing Source",
    "Source Start Time",
    "Source End Time",
    "Notes",
]

EDIT_TIMELINE_HEADER = WORD_TIMELINE_HEADER + [
    "Edit Start Time",
    "Edit End Time",
]

PRECISE_ANNOTATION_HEADER = [
    "Transcript #",
    "Row ID",
    "Keep",
    "Status",
    "Annotation Column",
    "Annotation Value",
    "Locator",
    "Confidence",
    "Timing Source",
    "Timing Confidence",
    "Edit Timestamp",
    "Start Time",
    "End Time",
    "Source Timestamp",
    "Source Start Time",
    "Source End Time",
    "Text",
    "Reference Segment",
]

ILLUSTRATION_CANDIDATE_HEADER = [
    "Asset Category",
    "Annotation Column",
    "Illustration Value",
    "Transcript #",
    "Row ID",
    "Keep",
    "Status",
    "Locator",
    "Timing Source",
    "Timing Confidence",
    "Edit Timestamp",
    "Start Time",
    "End Time",
    "Source Timestamp",
    "Source Start Time",
    "Source End Time",
    "Text",
    "Reference Segment",
]

PRECISE_COMPARER_EXTRA_HEADER = [
    "Source Start Time",
    "Source End Time",
]

PRECISE_COMPARER_HEADER = ENRICHED_HEADER + PRECISE_COMPARER_EXTRA_HEADER

ILLUSTRATION_TIMING_HEADER = [
    "Asset Category",
    "Annotation Column",
    "Illustration Value",
    "Transcript #",
    "Row ID",
    "Edit Timestamp",
    "Edit Start Time",
    "Edit End Time",
    "Source Timestamp",
    "Source Start Time",
    "Source End Time",
    "Timing Source",
    "Timing Confidence",
    "Locator",
    "Asset Path",
    "Original Asset Path",
    "Manifest Path",
    "Entry ID",
    "Status",
]

SILENCE_GAP_SECONDS = 0.30
PUNCTUATION_SPLIT_GAP_SECONDS = 0.15

REPETITION_LOOKBACK = 8
REPETITION_MIN_RUN = 4
REPETITION_MIN_SHORTER_COVERAGE = 0.40

ANCHOR_MIN_SCORE = 0.95
ANCHOR_MIN_TOKENS = 8
MATCH_MIN_SCORE = 0.55
TRANSPOSED_SET_OVERLAP = 0.75
TRANSPOSED_ORDERED_MAX = 0.50
RELAXED_LOCAL_MATCH_MIN_SCORE = 0.45
RELAXED_LOCAL_SET_OVERLAP = 0.70
MAX_REF_COMBINATION = 3
ANCHOR_MAX_REF_COMBINATION = 6
GAP_MAX_REF_COMBINATION = 5
BOUNDARY_REPAIR_MAX_REF_COMBINATION = 6
BOUNDARY_REPAIR_LOW_CONFIDENCE_SCORE = 0.70
BOUNDARY_REPAIR_MIN_IMPROVEMENT = 0.10
BOUNDARY_REPAIR_MAX_LOCAL_ROWS = 5
BOUNDARY_REPAIR_MAX_LOCAL_REF_SPANS = 12
SOFT_REF_SPLIT_MIN_TOKENS = 8
CONNECTOR_REF_SPLIT_MIN_TOKENS = 14

DP_SKIP_TRANSCRIPT_PENALTY = 0.35
DP_SKIP_REF_PENALTY = 0.10

DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-5-20250929"
CLAUDE_ALLOWED_DECISIONS = (
    "ELIMINATE_OFF_TOPIC",
    "ELIMINATE_META_COMMENTARY",
    "KEEP_TRANSCRIPTION_ERROR",
    "KEEP_UNMATCHED",
)

BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = BASE_DIR / "output"
DEFAULT_SWISSER_RUSH_DIR = Path("~/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/Rush").expanduser()
DEFAULT_SWISSER_HTML_DIR = Path("~/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/html").expanduser()
DEFAULT_GROQ_NOCLAP_DIR = Path("~/Desktop/code/deployable_auto-montage/groq/output/no_clap_output").expanduser()
DEFAULT_GROQ_CLAP_DIR = Path("~/Desktop/code/deployable_auto-montage/groq/output/post_clap_output").expanduser()
