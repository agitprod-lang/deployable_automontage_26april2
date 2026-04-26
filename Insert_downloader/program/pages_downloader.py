#!/usr/bin/env python3
"""
Download supporting material for non-video inserts (articles, pages, donations, etc.).
Mirrors smart_insertor's behaviour by storing HTML dumps and markdown snapshots.
"""

from __future__ import annotations

import csv
import logging
import sys
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import anthropic

_ARTICLE_PATH_PATTERN = re.compile(
    r"/\d{4}/\d{1,2}/"
    r"|/\d{4}-\d{2}-\d{2}[/-]"
    r"|/(article|articles|post|posts|news|blog|story|stories|actualite|actualites)/",
    re.IGNORECASE,
)
_ARTICLE_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){4,}$")

_ai_classification_cache: dict[str, bool] = {}


def _classify_url_as_article_with_ai(url: str) -> bool:
    """Ask Claude whether a URL points to an article, news story, or blog post.

    Uses claude-haiku for speed and cost. Result is cached per URL so the same
    URL is never classified twice in a single run.
    Returns False and logs a warning if the API call fails.
    """
    if url in _ai_classification_cache:
        return _ai_classification_cache[url]

    _log = logging.getLogger("insert_downloader.pages")
    try:
        client = anthropic.Anthropic()
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{
                "role": "user",
                "content": (
                    "Is this URL a specific article, news story, or blog post page "
                    "(not a website homepage, category page, or search result)? "
                    "Answer only YES or NO.\n"
                    f"{url}"
                ),
            }],
        )
        answer = message.content[0].text.strip().upper()
        result = answer.startswith("YES")
        _log.info("AI classified %s → %s", url, "article" if result else "website")
    except Exception as exc:
        _log.warning("AI URL classification failed for %s: %s — treating as non-article", url, exc)
        result = False

    _ai_classification_cache[url] = result
    return result


def _url_looks_like_article(url: str) -> bool:
    """Return True if the URL points to a specific article page.

    Uses fast regex for confident cases, then falls back to a Claude API call
    for ambiguous URLs (non-root paths that don't match known article patterns).
    Plain website homepages (path is / or empty) always return False.
    """
    path = urlparse(url).path
    # Root page: definitively a website homepage — no AI needed
    if not path or path.strip("/") == "":
        return False
    # Confident regex matches
    if _ARTICLE_PATH_PATTERN.search(path):
        return True
    slug = path.rstrip("/").rsplit("/", 1)[-1]
    if _ARTICLE_SLUG_RE.match(slug):
        return True
    # Ambiguous: non-root URL that doesn't match known patterns → ask Claude
    return _classify_url_as_article_with_ai(url)


from downloader_utils import (
    BASE_DIR,
    build_insert_stem,
    build_summary_entry,
    clean_url,
    download_logo_for_domain,
    fetch_article_metadata,
    is_image_url,
    is_video_url,
    prepare_processing_context,
    rename_with_schema,
)
from dynamyc_article import create_title_card_video

logger = logging.getLogger("insert_downloader.pages")


NAME_SPLIT_PATTERN = r"\s*(?:,|;|&| et | and )\s*"


def _clean_name_token(token: str) -> Optional[str]:
    stripped = token.strip(" \t\n\r.,;:-'\"")
    if not stripped:
        return None
    if not re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", stripped):
        return None
    return stripped


def normalize_author_line(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    parts = re.split(NAME_SPLIT_PATTERN, raw, flags=re.IGNORECASE)
    cleaned_names: list[str] = []
    for part in parts:
        name = part.strip()
        if not name:
            continue
        name = re.sub(r"^(?:par|by)\s+", "", name, flags=re.IGNORECASE).strip()
        lower = name.lower()
        if any(keyword in lower for keyword in ("collectif", "expert", "experte")):
            continue
        tokens = [
            cleaned
            for cleaned in (_clean_name_token(chunk) for chunk in re.split(r"[\s\u00A0]+", name))
            if cleaned
        ]
        if len(tokens) < 2:
            continue
        normalized = " ".join(tokens)
        if normalized not in cleaned_names:
            cleaned_names.append(normalized)
    if not cleaned_names:
        return None
    return ", ".join(cleaned_names)


def universal_display_date(raw: Optional[str], fallback: Optional[date] = None) -> Optional[str]:
    if raw:
        text = raw.strip()
        if text:
            return text
    if fallback:
        return fallback.isoformat()
    return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        html_path, _, links, target_dir, summary = prepare_processing_context()
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1

    processed = 0
    article_dir = target_dir / "articles"
    card_dir = target_dir / "title_cards"
    card_dir.mkdir(parents=True, exist_ok=True)
    document_date = None
    match = re.match(r"(\d{4}-\d{2}-\d{2})", html_path.name)
    if match:
        try:
            document_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            document_date = None

    records_for_csv = []

    for index, link in enumerate(links, start=1):
        cleaned_url = clean_url(link.href)
        parsed = urlparse(cleaned_url)
        if not parsed.scheme.startswith("http"):
            continue
        if is_video_url(cleaned_url):
            continue
        if is_image_url(cleaned_url):
            continue

        if not _url_looks_like_article(cleaned_url):
            logger.info("Skipping plain website URL (not an article): %s", cleaned_url)
            continue

        label = link.text.strip()
        source = parsed.netloc or None
        name_seed = f"{html_path.stem}_{index:02d}_article"
        fallback_title = label or cleaned_url
        article_meta = fetch_article_metadata(cleaned_url, article_dir, name_seed)
        title_hint = article_meta.get("title") or fallback_title
        source_candidates = (article_meta.get("source"), source)
        extrait_stem = build_insert_stem(
            index,
            artifact="Extrait",
            source_candidates=source_candidates,
            netloc=parsed.netloc,
            title_hint=title_hint,
            fallback_title=fallback_title,
        )
        article_stem = build_insert_stem(
            index,
            artifact="Article",
            source_candidates=source_candidates,
            netloc=parsed.netloc,
            title_hint=title_hint,
            fallback_title=fallback_title,
        )
        logo_stem = build_insert_stem(
            index,
            artifact="Logo",
            source_candidates=source_candidates,
            netloc=parsed.netloc,
            title_hint=title_hint,
            fallback_title=fallback_title,
        )
        titre_stem = build_insert_stem(
            index,
            artifact="Titre",
            source_candidates=source_candidates,
            netloc=parsed.netloc,
            title_hint=title_hint,
            fallback_title=fallback_title,
        )
        snapshot_rel = article_meta.get("snapshot_file")
        if snapshot_rel:
            snapshot_path = BASE_DIR / snapshot_rel
            renamed = rename_with_schema(snapshot_path, extrait_stem)
            if renamed:
                article_meta["snapshot_file"] = os.path.relpath(renamed, BASE_DIR)
        html_rel = article_meta.get("html_file")
        if html_rel:
            html_path_out = BASE_DIR / html_rel
            renamed_html = rename_with_schema(html_path_out, article_stem)
            if renamed_html:
                article_meta["html_file"] = os.path.relpath(renamed_html, BASE_DIR)
        author_line = normalize_author_line(article_meta.get("author"))
        display_date = universal_display_date(article_meta.get("published"), document_date)
        logo_path = download_logo_for_domain(
            article_meta.get("source") or source, article_dir, logo_stem
        )
        card_output = card_dir / f"{titre_stem}.mov"
        card_path = create_title_card_video(
            article_meta.get("title") or label,
            card_output,
            author=author_line,
            display_date=display_date,
            logo_path=logo_path,
        )
        entry = build_summary_entry(
            label=label,
            url=cleaned_url,
            entry_type="article",
            source=article_meta.get("source") or source,
            extra={
                "title": article_meta.get("title"),
                "description": article_meta.get("description"),
                "excerpt": article_meta.get("excerpt"),
                "error": article_meta.get("error"),
                "article_metadata": article_meta.get("metadata_file"),
                "article_snapshot": article_meta.get("snapshot_file"),
                "article_html": article_meta.get("html_file"),
                "author": author_line,
                "published": display_date,
                "logo_file": (
                    os.path.relpath(logo_path, BASE_DIR) if logo_path else None
                ),
                "title_card_video": (
                    os.path.relpath(card_path, BASE_DIR) if card_path else None
                ),
                "title_card_image": None,
            },
            downloaded_file=None,
        )
        summary.update_entry(index, entry)
        records_for_csv.append(
            [
                index,
                label,
                article_meta.get("title") or label,
                author_line or "",
                display_date or "",
            ]
        )
        processed += 1

    summary.save()
    if records_for_csv:
        csv_path = card_dir / "title_cards_summary.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(["insert", "label", "title", "authors", "date"])
            writer.writerows(records_for_csv)
    logger.info("Processed %d article inserts", processed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
