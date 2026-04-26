from __future__ import annotations

import html as html_module
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import List

from .constants import CONNECTOR_REF_SPLIT_MIN_TOKENS, SOFT_REF_SPLIT_MIN_TOKENS
from .model import RefSpan
from .text_utils import normalize_text, normalize_whitespace, tokenize

BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "div"}
BREAK_TAGS = {"br"}
IGNORED_TAGS = {"head", "style", "script", "noscript"}
STRONG_SPLIT_CHARS = {".", "?", "!", ":", ";"}
SOFT_SPLIT_CHARS = {",", "…"}
CONNECTOR_PATTERN = re.compile(
    r"\b(?:and|but|or|so|because|then|et|mais|ou|donc|car|puis|alors|avec|pour|vers)\b",
    re.IGNORECASE,
)

# Strips timecode annotations from extracted block text so they are never fed
# into transcript matching.
# _TIMECODE_ANNOTATION_RE: bracketed form — applied to every block.
# _BARE_TIMECODE_RE: bare form — applied only to blocks containing a video link.

_TIMECODE_ANNOTATION_RE = re.compile(
    r'\s*[\(\[]'
    r'\s*-?\d{1,2}:\d{2}(?::\d{2})?'
    r'(?:\s*(?:[-–—]|to|à|a)\s*\d{1,2}:\d{2}(?::\d{2})?)?'
    r'-?\s*[\)\]]',
    re.IGNORECASE,
)

_VIDEO_HOST_KEYWORDS_TC = (
    "youtube.com", "youtu.be", "dailymotion.com", "vimeo.com",
    "twitter.com", "x.com", "facebook.com", "fb.watch",
    "instagram.com", "tiktok.com", "rumble.com",
)

_TC_COLON = r'\d{1,2}:\d{2}(?::\d{2})?'
_TC_HUMAN = (
    r'(?:'
    r'\d+\s*h(?:eure?s?|ours?)?(?:\s*\d+\s*m(?:in(?:utes?)?)?(?:\s*\d+(?:\.\d+)?\s*s(?:ec(?:ondes?|s)?)?)?)?'
    r'|\d+\s*m(?:in(?:utes?)?)?(?:\s*\d+(?:\.\d+)?\s*(?:s(?:ec(?:ondes?|s)?)?)?)?'
    r'|\d+(?:\.\d+)?\s*s(?:ec(?:ondes?|s)?)?'
    r')'
)
_TC = f'(?:{_TC_COLON}|{_TC_HUMAN})'
_SEP_BARE = r'(?:\s*[-–—]\s*|\s+(?:to\b|à\b)\s*)'

_bare_tc_pattern = (
    r'(?<!\w)(?:'
    + _TC + _SEP_BARE + r'(?:' + _TC + r')?-?'
    + r'|-(?:' + _TC + r')'
    + r'|(?:' + _TC + r')-'
    + r')(?!\w)'
)
_BARE_TIMECODE_RE = re.compile(_bare_tc_pattern, re.IGNORECASE)


class ReferenceHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._buffer: List[str] = []
        self.blocks: List[str] = []
        self._ignored_depth = 0
        self._has_video_link: bool = False

    def _flush(self) -> None:
        raw = html_module.unescape("".join(self._buffer))
        if not raw.strip():
            self._buffer = []
            return
        lines = [normalize_whitespace(_TIMECODE_ANNOTATION_RE.sub("", piece)) for piece in raw.replace("\xa0", " ").split("\n")]
        if self._has_video_link:
            lines = [_BARE_TIMECODE_RE.sub("", line).strip() for line in lines]
            self._has_video_link = False
        normalized = "\n".join(piece for piece in lines if piece)
        if normalized:
            self.blocks.append(normalized)
        self._buffer = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        if tag in IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth > 0:
            return
        if tag in BLOCK_TAGS and self._buffer:
            self._flush()
        elif tag in BREAK_TAGS:
            self._buffer.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            if any(kw in href for kw in _VIDEO_HOST_KEYWORDS_TC):
                self._has_video_link = True

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        if tag in IGNORED_TAGS:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth > 0:
            return
        if tag in BLOCK_TAGS:
            self._flush()
        elif tag in BREAK_TAGS:
            self._buffer.append("\n")

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._ignored_depth > 0:
            return
        self._buffer.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def extract_reference_blocks(html_path: Path) -> list[str]:
    parser = ReferenceHTMLParser()
    parser.feed(html_path.read_text(encoding="utf-8", errors="ignore"))
    parser.close()
    return parser.blocks


def _make_span(text: str, start_offset: int, end_offset: int) -> RefSpan | None:
    candidate = normalize_whitespace(text)
    if not candidate:
        return None
    stripped_left = len(text) - len(text.lstrip())
    stripped_right = len(text) - len(text.rstrip())
    adjusted_start = start_offset + stripped_left
    adjusted_end = end_offset - stripped_right
    return RefSpan(
        index=-1,
        text=candidate,
        normalized=normalize_text(candidate),
        start_offset=adjusted_start,
        end_offset=adjusted_end,
    )


def _split_by_chars(text: str, base_offset: int, split_chars: set[str]) -> list[RefSpan]:
    spans: list[RefSpan] = []
    start = 0
    for cursor, char in enumerate(text):
        if char not in split_chars:
            continue
        span = _make_span(text[start:cursor + 1], base_offset + start, base_offset + cursor + 1)
        if span is not None:
            spans.append(span)
        start = cursor + 1
    tail = _make_span(text[start:], base_offset + start, base_offset + len(text))
    if tail is not None:
        spans.append(tail)
    return spans


def _split_on_connectors(span: RefSpan) -> list[RefSpan]:
    if len(tokenize(span.text)) < CONNECTOR_REF_SPLIT_MIN_TOKENS:
        return [span]
    pieces: list[RefSpan] = []
    start = 0
    for match in CONNECTOR_PATTERN.finditer(span.text):
        left_text = span.text[start:match.start()]
        right_text = span.text[match.start():]
        if len(tokenize(left_text)) < 6 or len(tokenize(right_text)) < 6:
            continue
        piece = _make_span(
            span.text[start:match.start()],
            span.start_offset + start,
            span.start_offset + match.start(),
        )
        if piece is not None:
            pieces.append(piece)
        start = match.start()
    tail = _make_span(
        span.text[start:],
        span.start_offset + start,
        span.end_offset,
    )
    if tail is not None:
        pieces.append(tail)
    return pieces or [span]


def _split_block_to_spans(block_text: str, base_offset: int) -> tuple[list[RefSpan], int]:
    strong_spans = _split_by_chars(block_text, base_offset, STRONG_SPLIT_CHARS)
    refined: list[RefSpan] = []
    for strong_span in strong_spans:
        if len(tokenize(strong_span.text)) >= SOFT_REF_SPLIT_MIN_TOKENS:
            soft_spans = _split_by_chars(strong_span.text, strong_span.start_offset, SOFT_SPLIT_CHARS)
        else:
            soft_spans = [strong_span]
        for soft_span in soft_spans:
            refined.extend(_split_on_connectors(soft_span))
    return refined, base_offset + len(block_text) + 1


def collect_reference_spans(html_path: Path) -> tuple[list[RefSpan], str]:
    blocks = extract_reference_blocks(html_path)
    all_spans: list[RefSpan] = []
    combined_text_parts: list[str] = []
    offset = 0
    for block in blocks:
        spans, offset = _split_block_to_spans(block, offset)
        for span in spans:
            span.index = len(all_spans)
            all_spans.append(span)
        combined_text_parts.append(block)
    return all_spans, "\n".join(combined_text_parts)
