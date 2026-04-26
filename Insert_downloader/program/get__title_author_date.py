#!/usr/bin/env python3
"""Extract title, author(s), and date metadata from one or more URLs."""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import textwrap
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup, Tag

try:
    import anthropic  # type: ignore
except ImportError:
    anthropic = None


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "url_metadata.json"
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-5-20250929"
DEFAULT_CLAUDE_MAX_TOKENS = 500
CLAUDE_SYSTEM_PROMPT = textwrap.dedent(
    """\
    You extract article metadata from a URL and optional HTML.
    Return strict JSON only with this exact schema:
    {
      "title": string|null,
      "authors": [string],
      "date_universal": string|null,
      "notes": string|null
    }

    Rules:
    - `title` should be the clean article headline, not a site wrapper or anti-bot page title.
    - `authors` should list human-readable bylines or agencies like AFP/Reuters when that is the credited author.
    - `date_universal` must be ISO format, ideally YYYY-MM-DD. Use a datetime only if the source clearly gives one.
    - If the HTML looks like a blocked page, infer only from the URL and the provided candidate metadata.
    - Never return commentary outside JSON.
    """
)

META_NAME_KEYS = (
    "author",
    "article:author",
    "og:author",
    "twitter:creator",
    "parsely-author",
    "dc.creator",
    "dc.creator.author",
    "citation_author",
    "sailthru.author",
)
META_TITLE_KEYS = (
    "og:title",
    "twitter:title",
    "headline",
    "parsely-title",
)
META_DATE_KEYS = (
    "article:published_time",
    "article:modified_time",
    "og:updated_time",
    "publish-date",
    "date",
    "pubdate",
    "datepublished",
    "datecreated",
    "dc.date",
    "dc.date.issued",
    "citation_publication_date",
    "parsely-pub-date",
    "sailthru.date",
)
ARTICLE_TYPES = {
    "article",
    "newsarticle",
    "blogposting",
    "report",
    "analysisnewsarticle",
    "opinionnewsarticle",
}
MONTH_MAP = {
    "janvier": 1,
    "fevrier": 2,
    "février": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "août": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
    "décembre": 12,
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch one or more URLs and extract title, author(s), and date metadata."
        )
    )
    parser.add_argument("urls", nargs="*", help="URL(s) to analyze")
    parser.add_argument(
        "-i",
        "--input-file",
        type=Path,
        help="Optional text file containing one URL per line",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output file path. Default: program/url_metadata.json",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=("json", "csv", "txt"),
        help="Force output format. By default it is inferred from the output suffix.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout in seconds. Default: 20",
    )
    parser.add_argument(
        "--claude-model",
        default=DEFAULT_CLAUDE_MODEL,
        help=f"Claude model for fallback extraction (default: {DEFAULT_CLAUDE_MODEL})",
    )
    parser.add_argument(
        "--claude-max-tokens",
        type=int,
        default=DEFAULT_CLAUDE_MAX_TOKENS,
        help=f"Max tokens for Claude fallback (default: {DEFAULT_CLAUDE_MAX_TOKENS})",
    )
    parser.add_argument(
        "--disable-claude",
        action="store_true",
        help="Disable Claude fallback and use HTML/url heuristics only.",
    )
    return parser.parse_args()


def collect_urls(args: argparse.Namespace) -> List[str]:
    urls: List[str] = list(args.urls)
    if args.input_file:
        urls.extend(
            line.strip()
            for line in args.input_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    if not urls and not sys.stdin.isatty():
        urls.extend(line.strip() for line in sys.stdin if line.strip())
    deduped: List[str] = []
    seen = set()
    for url in urls:
        normalized = url.strip()
        if normalized and normalized not in seen:
            deduped.append(normalized)
            seen.add(normalized)
    return deduped


def fetch_url(url: str, timeout: float) -> str:
    response = requests.get(
        url,
        timeout=timeout,
        headers=REQUEST_HEADERS,
    )
    response.raise_for_status()
    response.encoding = response.encoding or response.apparent_encoding or "utf-8"
    return response.text


def meta_content(soup: BeautifulSoup, keys: Iterable[str]) -> Optional[str]:
    for key in keys:
        tag = soup.find("meta", attrs={"name": key}) or soup.find(
            "meta", attrs={"property": key}
        )
        if tag and tag.get("content"):
            return clean_text(tag["content"])
    return None


def clean_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = html.unescape(str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def normalize_title_candidate(value: Optional[str]) -> Optional[str]:
    text = clean_text(value)
    if not text:
        return None
    text = re.sub(r"\s+[|:-]\s+(?:Le Monde|AFP|Reuters|Associated Press|AP|BBC|CNN|The Guardian)$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" -|:")
    return text or None


def resolve_api_key(candidate: Optional[str] = None) -> Optional[str]:
    return candidate or os.environ.get("ANTHROPIC_API_KEY")


def looks_like_block_page(title: Optional[str], html_text: str = "") -> bool:
    title_text = (title or "").strip().lower()
    html_lower = html_text.lower()
    suspicious_titles = {
        "client challenge",
        "just a moment",
        "attention required",
        "access denied",
        "security check",
    }
    if title_text in suspicious_titles:
        return True
    suspicious_markers = (
        "cf-challenge",
        "cloudflare",
        "captcha",
        "client challenge",
        "enable javascript and cookies",
        "checking if the site connection is secure",
    )
    return any(marker in html_lower for marker in suspicious_markers)


def split_authors(value: str) -> List[str]:
    parts = re.split(r"\s*(?:,|;| and | et |&|\|)\s*", value, flags=re.IGNORECASE)
    return [part.strip() for part in parts if part.strip()]


def normalize_author_name(value: str) -> Optional[str]:
    cleaned = clean_text(value)
    if not cleaned:
        return None
    cleaned = re.sub(r"^(?:by|par)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^[^-:|]+?\s+(?:avec|with)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;:-|")
    lowered = cleaned.lower()
    if not cleaned:
        return None
    if lowered in {"staff", "editorial staff", "admin"}:
        return None
    if lowered == "le monde":
        return None
    if lowered in {"afp", "ap"}:
        return cleaned.upper()
    if "subscribe" in lowered or "newsletter" in lowered:
        return None
    return cleaned


def add_author_value(container: List[str], value: Any) -> None:
    if value is None:
        return
    if isinstance(value, list):
        for item in value:
            add_author_value(container, item)
        return
    if isinstance(value, dict):
        for key in ("name", "author", "alternateName"):
            if value.get(key):
                add_author_value(container, value[key])
        return
    if isinstance(value, str):
        for candidate in split_authors(value):
            normalized = normalize_author_name(candidate)
            if normalized and normalized not in container:
                container.append(normalized)


def flatten_json_ld_nodes(payload: Any) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            nodes.extend(flatten_json_ld_nodes(item))
    elif isinstance(payload, dict):
        nodes.append(payload)
        graph = payload.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                nodes.extend(flatten_json_ld_nodes(item))
    return nodes


def parse_json_ld(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        nodes.extend(flatten_json_ld_nodes(payload))
    return nodes


def schema_type_matches(node: Dict[str, Any], expected: set[str]) -> bool:
    raw_type = node.get("@type")
    if isinstance(raw_type, list):
        lowered = {str(item).lower() for item in raw_type}
    else:
        lowered = {str(raw_type).lower()}
    return bool(lowered & expected)


def title_candidates_from_json_ld(nodes: List[Dict[str, Any]]) -> List[str]:
    candidates: List[str] = []
    for node in nodes:
        if not schema_type_matches(node, ARTICLE_TYPES):
            continue
        for key in ("headline", "name", "title"):
            text = normalize_title_candidate(node.get(key))
            if text and text not in candidates:
                candidates.append(text)
    for node in nodes:
        for key in ("headline", "name", "title"):
            text = normalize_title_candidate(node.get(key))
            if text and text not in candidates:
                candidates.append(text)
    return candidates


def authors_from_json_ld(nodes: List[Dict[str, Any]]) -> List[str]:
    authors: List[str] = []
    for node in nodes:
        if schema_type_matches(node, ARTICLE_TYPES):
            add_author_value(authors, node.get("author"))
            add_author_value(authors, node.get("creator"))
    if authors:
        return authors
    for node in nodes:
        add_author_value(authors, node.get("author"))
        add_author_value(authors, node.get("creator"))
    return authors


def normalize_iso_date(value: str) -> Optional[str]:
    text = clean_text(value)
    if not text:
        return None

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text

    candidate = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
        if dt.tzinfo is None:
            return dt.isoformat(timespec="seconds")
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
    except ValueError:
        pass

    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            return dt.isoformat(timespec="seconds")
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
    except (TypeError, ValueError, IndexError):
        pass

    date_match = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", text)
    if date_match:
        year, month, day = date_match.groups()
        try:
            return date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            return None

    localized_match = re.search(
        r"(?P<day>\d{1,2})\s+(?P<month>[A-Za-zÀ-ÿ]+)\s+(?P<year>\d{4})", text
    )
    if localized_match:
        month_name = localized_match.group("month").lower()
        month_number = MONTH_MAP.get(month_name)
        if month_number:
            try:
                return date(
                    int(localized_match.group("year")),
                    month_number,
                    int(localized_match.group("day")),
                ).isoformat()
            except ValueError:
                return None
    return None


def date_from_json_ld(nodes: List[Dict[str, Any]]) -> Optional[str]:
    for node in nodes:
        if not schema_type_matches(node, ARTICLE_TYPES):
            continue
        for key in ("datePublished", "dateCreated", "dateModified", "uploadDate"):
            normalized = normalize_iso_date(str(node.get(key, "")))
            if normalized:
                return normalized
    return None


def extract_date_from_url(url: str) -> Optional[str]:
    path = urlparse(url).path
    match = re.search(r"/(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:/|$)", path)
    if match:
        year, month, day = match.groups()
        try:
            return date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            return None
    compact = re.search(r"/(\d{4})(\d{2})(\d{2})(?:/|$)", path)
    if compact:
        year, month, day = compact.groups()
        try:
            return date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            return None
    return None


def slug_query_from_url(url: str) -> str:
    parsed = urlparse(url)
    slug = Path(unquote(parsed.path)).stem
    slug = re.sub(r"_\d+(?:_\d+)*$", "", slug)
    slug = slug.replace("-", " ")
    slug = re.sub(r"\b(?:article|politique|societe|france|monde|international)\b", " ", slug, flags=re.IGNORECASE)
    slug = re.sub(r"\s+", " ", slug).strip()
    site = parsed.netloc.replace("www.", "").split(".")[0]
    return f"site:{parsed.netloc} {slug} {site}".strip()


def title_from_url_slug(url: str) -> Optional[str]:
    parsed = urlparse(url)
    slug = Path(unquote(parsed.path)).stem
    slug = re.sub(r"_\d+(?:_\d+)*$", "", slug)
    slug = slug.replace("-", " ")
    slug = re.sub(r"\s+", " ", slug).strip()
    if not slug:
        return None
    words = []
    for token in slug.split():
        if token.isdigit():
            continue
        words.append(token)
    if not words:
        return None
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def title_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9à-ÿ]+", value.lower())
        if len(token) >= 3
    }


def _decode_search_result_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    if "duckduckgo." in parsed.netloc:
        uddg = parse_qs(parsed.query).get("uddg")
        if uddg:
            return unquote(uddg[0])
    return raw_url


def search_fallback_title(url: str, timeout: float) -> Optional[str]:
    query = slug_query_from_url(url)
    try:
        response = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            timeout=timeout,
            headers=REQUEST_HEADERS,
        )
        response.raise_for_status()
    except Exception:
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    source_host = urlparse(url).netloc.lower()
    slug_title = title_from_url_slug(url) or ""
    slug_tokens = title_tokens(slug_title)
    best_title: Optional[str] = None
    best_score = 0.0
    for link in soup.select("a.result__a"):
        href = _decode_search_result_url(link.get("href") or "")
        link_host = urlparse(href).netloc.lower()
        if source_host and link_host and source_host not in link_host and link_host not in source_host:
            continue
        title = normalize_title_candidate(link.get_text(" ", strip=True))
        if not title:
            continue
        candidate_tokens = title_tokens(title)
        if not candidate_tokens:
            continue
        overlap = len(slug_tokens & candidate_tokens)
        coverage = overlap / max(1, len(slug_tokens))
        precision = overlap / max(1, len(candidate_tokens))
        score = (coverage * 0.7) + (precision * 0.3)
        if overlap < 3 or score < 0.35:
            continue
        if score > best_score:
            best_title = title
            best_score = score
    return best_title


def authors_from_dom(soup: BeautifulSoup) -> List[str]:
    authors: List[str] = []
    meta_author = meta_content(soup, META_NAME_KEYS)
    if meta_author:
        add_author_value(authors, meta_author)

    for tag in soup.select('[itemprop="author"], [rel="author"], [class*="author"], [class*="byline"]'):
        if not isinstance(tag, Tag):
            continue
        text = clean_text(tag.get_text(" ", strip=True))
        if text and len(text) <= 200:
            add_author_value(authors, text)
    return authors


def date_from_dom(soup: BeautifulSoup) -> Optional[str]:
    meta_date = meta_content(soup, META_DATE_KEYS)
    normalized = normalize_iso_date(meta_date or "")
    if normalized:
        return normalized

    selectors = (
        "time",
        "[itemprop='datePublished']",
        "[itemprop='dateCreated']",
        "[itemprop='dateModified']",
        "[property='article:published_time']",
        "[property='article:modified_time']",
    )
    for selector in selectors:
        for tag in soup.select(selector):
            for attr in (
                "datetime",
                "content",
                "title",
                "data-date",
                "data-time",
                "data-published",
                "data-modified",
            ):
                normalized = normalize_iso_date(str(tag.get(attr, "")))
                if normalized:
                    return normalized
            normalized = normalize_iso_date(tag.get_text(" ", strip=True))
            if normalized:
                return normalized
    return None


def title_from_dom(soup: BeautifulSoup) -> Optional[str]:
    meta_title = meta_content(soup, META_TITLE_KEYS)
    if meta_title:
        return meta_title
    title_tag = soup.find("title")
    if title_tag:
        title_text = clean_text(title_tag.get_text(" ", strip=True))
        if title_text:
            return title_text
    h1 = soup.find("h1")
    if h1:
        h1_text = clean_text(h1.get_text(" ", strip=True))
        if h1_text:
            return h1_text
    return None


def title_candidates_from_dom(soup: BeautifulSoup) -> List[str]:
    candidates: List[str] = []
    for selector in ("h1", "title"):
        tag = soup.find(selector)
        if tag:
            text = normalize_title_candidate(tag.get_text(" ", strip=True))
            if text and text not in candidates:
                candidates.append(text)
    meta_title = meta_content(soup, META_TITLE_KEYS)
    if meta_title:
        normalized = normalize_title_candidate(meta_title)
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    return candidates


def canonical_title_key(value: str) -> str:
    return re.sub(r"[\W_]+", "", value).lower()


def pick_best_title(candidates: Iterable[str]) -> Optional[str]:
    filtered = [candidate for candidate in candidates if candidate]
    if not filtered:
        return None
    unique: List[str] = []
    seen_keys = set()
    for candidate in filtered:
        key = canonical_title_key(candidate)
        if key and key not in seen_keys:
            seen_keys.add(key)
            unique.append(candidate)
    if not unique:
        return None

    best = unique[0]
    for candidate in unique[1:]:
        best_key = canonical_title_key(best)
        candidate_key = canonical_title_key(candidate)
        if best_key and candidate_key:
            if best_key in candidate_key and len(candidate) + 12 < len(best):
                best = candidate
                continue
            if candidate_key in best_key and len(best) + 12 < len(candidate):
                continue
        if len(candidate) < len(best) and len(best) - len(candidate) >= 20:
            best = candidate
    return best


def parse_claude_json(raw_content: str) -> Dict[str, Any]:
    content = (raw_content or "").strip()
    if not content:
        raise RuntimeError("Claude returned an empty response.")
    candidates = [content]
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", content, flags=re.DOTALL | re.IGNORECASE)
    candidates.extend(chunk.strip() for chunk in fenced if chunk.strip())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError("Claude did not return valid JSON.")


def extract_response_text(response: "anthropic.types.Message") -> str:
    parts: List[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def claude_extract_metadata(
    *,
    url: str,
    html_text: str,
    current_title: Optional[str],
    current_authors: List[str],
    current_date: Optional[str],
    model: str,
    max_tokens: int,
) -> Optional[Dict[str, Any]]:
    api_key = resolve_api_key()
    if not api_key or anthropic is None:
        return None

    html_excerpt = html_text[:12000]
    prompt = {
        "url": url,
        "current_extraction": {
            "title": current_title,
            "authors": current_authors,
            "date_universal": current_date,
        },
        "html_excerpt": html_excerpt,
    }
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=CLAUDE_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(prompt, ensure_ascii=False),
                }
            ],
        )
        content = extract_response_text(response)
        data = parse_claude_json(content)
    except Exception:
        return None

    raw_title = normalize_title_candidate(data.get("title"))
    raw_authors = data.get("authors") if isinstance(data.get("authors"), list) else []
    authors: List[str] = []
    for item in raw_authors:
        if isinstance(item, str):
            normalized = normalize_author_name(item)
            if normalized and normalized not in authors:
                authors.append(normalized)
    date_value = normalize_iso_date(str(data.get("date_universal", "")))
    return {
        "title": raw_title,
        "authors": authors,
        "authors_text": ", ".join(authors) if authors else None,
        "date_universal": date_value,
        "notes": clean_text(data.get("notes")),
    }


def dedupe_keep_order(values: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def extract_metadata_from_html(
    url: str,
    html_text: str,
    *,
    use_claude_fallback: bool = True,
    claude_model: str = DEFAULT_CLAUDE_MODEL,
    claude_max_tokens: int = DEFAULT_CLAUDE_MAX_TOKENS,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "url": url,
        "source": urlparse(url).netloc or None,
        "title": None,
        "authors": [],
        "authors_text": None,
        "date_universal": None,
        "status": "ok",
        "error": None,
    }

    soup = BeautifulSoup(html_text, "html.parser")
    json_ld_nodes = parse_json_ld(soup)

    title = pick_best_title(
        title_candidates_from_dom(soup) + title_candidates_from_json_ld(json_ld_nodes)
    ) or title_from_dom(soup)
    authors = dedupe_keep_order(
        authors_from_json_ld(json_ld_nodes) + authors_from_dom(soup)
    )
    published = (
        date_from_json_ld(json_ld_nodes)
        or date_from_dom(soup)
        or extract_date_from_url(url)
    )

    record["title"] = title
    record["authors"] = authors
    record["authors_text"] = ", ".join(authors) if authors else None
    record["date_universal"] = published
    used_slug_fallback = False
    if looks_like_block_page(title, html_text):
        fallback_title = search_fallback_title(url, timeout=12.0)
        if fallback_title:
            record["title"] = fallback_title
        else:
            record["title"] = title_from_url_slug(url)
            used_slug_fallback = True
        if record["title"] and looks_like_block_page(record["title"], ""):
            record["title"] = None
    should_use_claude = use_claude_fallback and (
        looks_like_block_page(title, html_text)
        or used_slug_fallback
        or not record["title"]
        or not record["authors"]
        or not record["date_universal"]
    )
    if should_use_claude:
        claude_result = claude_extract_metadata(
            url=url,
            html_text=html_text,
            current_title=record["title"],
            current_authors=record["authors"],
            current_date=record["date_universal"],
            model=claude_model,
            max_tokens=claude_max_tokens,
        )
        if claude_result:
            if claude_result.get("title"):
                record["title"] = claude_result["title"]
            if claude_result.get("authors"):
                record["authors"] = claude_result["authors"]
                record["authors_text"] = claude_result["authors_text"]
            if claude_result.get("date_universal"):
                record["date_universal"] = claude_result["date_universal"]
    return record


def extract_metadata(
    url: str,
    timeout: float,
    *,
    use_claude_fallback: bool = True,
    claude_model: str = DEFAULT_CLAUDE_MODEL,
    claude_max_tokens: int = DEFAULT_CLAUDE_MAX_TOKENS,
) -> Dict[str, Any]:
    try:
        html_text = fetch_url(url, timeout)
    except Exception as exc:  # noqa: BLE001
        return {
            "url": url,
            "source": urlparse(url).netloc or None,
            "title": None,
            "authors": [],
            "authors_text": None,
            "date_universal": None,
            "status": "error",
            "error": str(exc),
        }
    return extract_metadata_from_html(
        url,
        html_text,
        use_claude_fallback=use_claude_fallback,
        claude_model=claude_model,
        claude_max_tokens=claude_max_tokens,
    )


def output_format(output_path: Path, forced_format: Optional[str]) -> str:
    if forced_format:
        return forced_format
    suffix = output_path.suffix.lower().lstrip(".")
    if suffix in {"json", "csv", "txt"}:
        return suffix
    return "json"


def write_json(records: List[Dict[str, Any]], path: Path) -> None:
    path.write_text(
        json.dumps(records if len(records) != 1 else records[0], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def write_csv(records: List[Dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "url",
                "source",
                "title",
                "authors_text",
                "date_universal",
                "status",
                "error",
            ],
        )
        writer.writeheader()
        for record in records:
            row = dict(record)
            row.pop("authors", None)
            writer.writerow(row)


def write_txt(records: List[Dict[str, Any]], path: Path) -> None:
    lines: List[str] = []
    for record in records:
        lines.extend(
            [
                f"URL: {record['url']}",
                f"Source: {record.get('source') or ''}",
                f"Title: {record.get('title') or ''}",
                f"Authors: {record.get('authors_text') or ''}",
                f"Date (universal): {record.get('date_universal') or ''}",
                f"Status: {record.get('status') or ''}",
                f"Error: {record.get('error') or ''}",
                "",
            ]
        )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    urls = collect_urls(args)
    if not urls:
        print("No URL provided. Pass one or more URLs, --input-file, or pipe URLs on stdin.", file=sys.stderr)
        return 1

    records = [
        extract_metadata(
            url,
            args.timeout,
            use_claude_fallback=not args.disable_claude,
            claude_model=args.claude_model,
            claude_max_tokens=args.claude_max_tokens,
        )
        for url in urls
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fmt = output_format(args.output, args.format)

    if fmt == "json":
        write_json(records, args.output)
    elif fmt == "csv":
        write_csv(records, args.output)
    else:
        write_txt(records, args.output)

    print(args.output)
    return 0 if any(record["status"] == "ok" for record in records) else 2


if __name__ == "__main__":
    raise SystemExit(main())
