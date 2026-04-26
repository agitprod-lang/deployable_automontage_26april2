from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping, Sequence
import html as html_module
import re

from .constants import ENRICHED_EXPLICIT_HEADER, PIPE_COMPAT_HEADER
from .enriched_csv import (
    build_enriched_dict_rows,
    diagnostic_row_to_base,
    first_kept_row_index,
    merge_pipe_values,
    read_diagnostic_speech_rows,
    read_table,
    validate_stage07_diagnostic_header,
    write_enriched_rows,
)
from .legacy_step2 import get_legacy_step2_module


@dataclass
class StructuredTitle:
    text: str
    level: str


@dataclass
class ClassifiedLink:
    href: str
    text: str
    context: str
    category: str
    is_excerpt: bool


class _FallbackStructuredTitleExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._stack: list[tuple[str, bool]] = []
        self._buffer: list[str] = []
        self._level = ""
        self.titles: list[StructuredTitle] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        tag_lower = tag.lower()
        class_tokens = []
        for name, value in attrs:
            if name.lower() == "class" and value:
                class_tokens = [token.strip().lower() for token in value.split() if token.strip()]
                break
        is_title = tag_lower in {"h1", "h2", "h3", "h4", "h5", "h6"} or "title" in class_tokens
        level = tag_lower.upper() if tag_lower.startswith("h") and len(tag_lower) == 2 else "TITLE"
        self._stack.append((tag_lower, is_title))
        if is_title and not self._level:
            self._level = level
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        if not self._stack:
            return
        _, is_title = self._stack.pop()
        if is_title and self._level and not any(flag for _, flag in self._stack):
            text = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
            if text:
                self.titles.append(StructuredTitle(text=text, level=self._level))
            self._level = ""
            self._buffer = []

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._level:
            self._buffer.append(data)


class _FallbackLinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._current_href = ""
        self._current_text: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        if tag.lower() != "a":
            return
        attr_map = {name.lower(): (value or "") for name, value in attrs}
        self._current_href = attr_map.get("href", "").strip()
        self._current_text = []

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        if tag.lower() != "a" or not self._current_href:
            self._current_href = ""
            self._current_text = []
            return
        text = re.sub(r"\s+", " ", "".join(self._current_text)).strip()
        self.links.append((self._current_href, text))
        self._current_href = ""
        self._current_text = []

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._current_href:
            self._current_text.append(data)


def _unique_ordered(values: Sequence[str]) -> list[str]:
    results: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = re.sub(r"\s+", " ", value).strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        results.append(cleaned)
    return results


def extract_structured_titles(html_path: Path | None) -> list[StructuredTitle]:
    if not html_path or not html_path.exists():
        return []
    legacy = get_legacy_step2_module()
    html_text = html_path.read_text(encoding="utf-8", errors="ignore")
    titles: list[StructuredTitle] = []
    seen: set[str] = set()
    if getattr(legacy, "BeautifulSoup", None) is not None:
        soup = legacy.BeautifulSoup(html_text, "html.parser")
        for element in soup.find_all(True):
            name = element.name.lower() if element.name else ""
            classes = [cls.lower() for cls in element.get("class", [])]
            if name in legacy.TITLE_TAGS:
                level = name.upper()
            elif "title" in classes:
                level = next((cls.upper() for cls in classes if re.fullmatch(r"h[1-6]", cls)), "TITLE")
            else:
                continue
            text = re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()
            if not text:
                continue
            fingerprint = legacy.normalize_for_matching(text).lower()
            if not fingerprint or fingerprint in seen:
                continue
            seen.add(fingerprint)
            titles.append(StructuredTitle(text=text, level=level))
        return titles
    parser = _FallbackStructuredTitleExtractor()
    parser.feed(html_text)
    parser.close()
    for entry in parser.titles:
        fingerprint = legacy.normalize_for_matching(entry.text).lower()
        if not fingerprint or fingerprint in seen:
            continue
        seen.add(fingerprint)
        titles.append(entry)
    return titles


def locate_structured_titles(
    titles: Sequence[StructuredTitle],
    analysis_text: str,
) -> list[dict[str, Any]]:
    legacy = get_legacy_step2_module()
    located: list[dict[str, Any]] = []
    cursor = 0
    for title in titles:
        normalized = legacy.normalize_for_matching(title.text)
        if not normalized:
            continue
        index = analysis_text.find(normalized, cursor)
        if index == -1:
            index = analysis_text.find(normalized)
        payload: dict[str, Any] = {"text": title.text, "level": title.level}
        if index != -1:
            payload["start_index"] = index
            cursor = index + len(normalized)
        located.append(payload)
    return located


def map_title_levels_to_rows(
    titles: Sequence[Mapping[str, Any]],
    spans: Sequence[tuple[int, int] | None],
    rows: Sequence[Sequence[str]],
    ref_idx: int,
    text_idx: int,
) -> dict[int, list[str]]:
    legacy = get_legacy_step2_module()
    assignments: dict[int, list[str]] = {}
    for entry in titles:
        text_value = entry.get("text")
        level = entry.get("level")
        if not isinstance(text_value, str) or not isinstance(level, str):
            continue
        offset = entry.get("start_index")
        position = offset if isinstance(offset, int) else None
        row_idx = legacy.find_row_for_offset_or_neighbor(spans, position)
        if row_idx is None:
            row_idx = legacy.fallback_row_lookup(rows, ref_idx, text_idx, text_value)
        if row_idx is None:
            continue
        bucket = assignments.setdefault(row_idx, [])
        normalized_level = level.strip().upper()
        if normalized_level and normalized_level not in bucket:
            bucket.append(normalized_level)
    return assignments


def extract_classified_links(html_path: Path | None) -> list[ClassifiedLink]:
    if not html_path or not html_path.exists():
        return []
    legacy = get_legacy_step2_module()
    html_text = html_path.read_text(encoding="utf-8", errors="ignore")
    links: list[ClassifiedLink] = []
    if getattr(legacy, "BeautifulSoup", None) is not None:
        soup = legacy.BeautifulSoup(html_text, "html.parser")
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href") or "").strip()
            if not href:
                continue
            text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
            parent = anchor.parent.get_text(" ", strip=True) if anchor.parent else text
            context = re.sub(r"\s+", " ", parent).strip()
            category = legacy.categorize_link(href)
            excerpt_context = f"{text} {context}".casefold()
            is_excerpt = category == "video" and "extrait" in excerpt_context
            links.append(ClassifiedLink(href=href, text=text, context=context, category=category, is_excerpt=is_excerpt))
        return links
    parser = _FallbackLinkExtractor()
    parser.feed(html_text)
    parser.close()
    for href, text in parser.links:
        category = legacy.categorize_link(href)
        excerpt_context = f"{text} {text}".casefold()
        is_excerpt = category == "video" and "extrait" in excerpt_context
        links.append(ClassifiedLink(href=href, text=text, context=text, category=category, is_excerpt=is_excerpt))
    return links


def _apply_title_levels(
    header: list[str],
    rows: list[list[str]],
    level_map: Mapping[int, Sequence[str]],
) -> None:
    legacy = get_legacy_step2_module()
    column_idx = legacy.ensure_column(header, rows, "Title Level")
    for row_idx, levels in level_map.items():
        if not (0 <= row_idx < len(rows)):
            continue
        rows[row_idx][column_idx] = " | ".join(_unique_ordered(list(levels)))


def _move_global_link_bundles(
    header: list[str],
    rows: list[list[str]],
    row_dicts: Sequence[dict[str, str]],
    links: Sequence[ClassifiedLink],
) -> None:
    if not rows:
        return
    legacy = get_legacy_step2_module()
    article_idx = legacy.ensure_column(header, rows, "Article Links")
    video_idx = legacy.ensure_column(header, rows, "Video Links")
    image_idx = legacy.ensure_column(header, rows, "Image Links")
    excerpt_idx = legacy.ensure_column(header, rows, "Excerpt Video Links")
    direct_idx = legacy.ensure_column(header, rows, "Direct Video Links")

    target_index = first_kept_row_index(row_dicts)
    if target_index is None:
        return

    article_links = _unique_ordered([link.href for link in links if link.category == "article"])
    video_links = _unique_ordered([link.href for link in links if link.category == "video"])
    image_links = _unique_ordered([link.href for link in links if link.category == "image"])
    excerpt_links = _unique_ordered([link.href for link in links if link.category == "video" and link.is_excerpt])
    direct_links = _unique_ordered([link.href for link in links if link.category == "video" and not link.is_excerpt])

    for row in rows:
        for idx in (article_idx, video_idx, image_idx, excerpt_idx, direct_idx):
            row[idx] = ""

    rows[target_index][article_idx] = " | ".join(sorted(article_links))
    rows[target_index][video_idx] = " | ".join(sorted(video_links))
    rows[target_index][image_idx] = " | ".join(sorted(image_links))
    rows[target_index][excerpt_idx] = " | ".join(sorted(excerpt_links))
    rows[target_index][direct_idx] = " | ".join(sorted(direct_links))


def _summary_counts(rows: Sequence[dict[str, str]]) -> dict[str, int]:
    return {
        "rows": len(rows),
        "kept_rows": sum(1 for row in rows if (row.get("Keep") or "").strip().lower() == "x"),
        "eliminated_rows": sum(1 for row in rows if (row.get("Eliminate") or "").strip().lower() == "x"),
        "titled_rows": sum(1 for row in rows if row.get("Titles")),
        "video_link_rows": sum(1 for row in rows if row.get("Video Links")),
    }


def run_stage_08(input_path: Path, html_path: Path | None, output_path: Path) -> dict[str, Any]:
    source_header, _ = read_table(input_path)
    validate_stage07_diagnostic_header(source_header)
    source_rows = read_diagnostic_speech_rows(input_path)
    legacy = get_legacy_step2_module()

    header = list(PIPE_COMPAT_HEADER)
    rows = [[diagnostic_row_to_base(row).get(column, "") for column in header] for row in source_rows]

    deterministic_tags: dict[int, set[str]] = {}
    legacy.apply_deterministic_cta_tags(rows, deterministic_tags)
    legacy.apply_tag_columns(header, rows, deterministic_tags)
    legacy.apply_zoom_column(header, rows, {})
    legacy.ensure_feature_columns(header, rows)
    titles_column_idx = legacy.ensure_titles_column(header, rows)
    legacy.ensure_relevant_news_column(header, rows)

    title_level_map: dict[int, list[str]] = {}
    structured_titles: list[StructuredTitle] = []
    if html_path and html_path.exists():
        reference_text = legacy.strip_reference_title(legacy.collect_reference_text(html_path))
        analysis_text = legacy.prepare_reference_analysis_text(reference_text)
        ref_idx = legacy.require_column(legacy.build_header_map(header), "Reference Segment")
        text_idx = legacy.require_column(legacy.build_header_map(header), "Text")
        spans = legacy.build_row_reference_spans(rows, ref_idx, analysis_text)
        structured_titles = extract_structured_titles(html_path)
        located_titles = locate_structured_titles(structured_titles, analysis_text)
        title_assignments = legacy.map_titles_to_rows(located_titles, spans, rows, ref_idx, text_idx)
        legacy.apply_title_annotations(rows, title_assignments, titles_column_idx)
        title_level_map = map_title_levels_to_rows(located_titles, spans, rows, ref_idx, text_idx)

    _apply_title_levels(header, rows, title_level_map)
    legacy.enrich_html_metrics(header, rows, html_path, None)
    classified_links = extract_classified_links(html_path)
    _move_global_link_bundles(header, rows, source_rows, classified_links)

    enriched_rows = build_enriched_dict_rows(header, rows, source_rows, extra_columns=ENRICHED_EXPLICIT_HEADER)
    write_enriched_rows(output_path, enriched_rows)
    return {
        **_summary_counts(enriched_rows),
        "titles_detected": len(structured_titles),
        "excerpt_video_links": sum(1 for link in classified_links if link.category == "video" and link.is_excerpt),
        "direct_video_links": sum(1 for link in classified_links if link.category == "video" and not link.is_excerpt),
    }
