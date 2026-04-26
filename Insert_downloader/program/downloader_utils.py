#!/usr/bin/env python3
"""Shared helpers for Insert Downloader scripts."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import textwrap
import unicodedata
from dataclasses import dataclass
from datetime import datetime, date
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Any
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image, ImageDraw, ImageFont
from bs4 import BeautifulSoup, Tag
import requests


BASE_DIR = Path(__file__).resolve().parents[1]
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
FALLBACK_INPUT_DIR = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/html"
)

VIDEO_HOST_KEYWORDS = (
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "dailymotion.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "fb.watch",
    "instagram.com",
    "tiktok.com",
    "vm.tiktok.com",
    "reddit.com",
    "rumble.com",
    "odysee.com",
)
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")

USER_AGENT = (
    "InsertDownloader/1.0 (+https://github.com/mathieusandana) "
    "python-requests"
)

FRENCH_MONTHS = {
    1: "Janvier",
    2: "Février",
    3: "Mars",
    4: "Avril",
    5: "Mai",
    6: "Juin",
    7: "Juillet",
    8: "Août",
    9: "Septembre",
    10: "Octobre",
    11: "Novembre",
    12: "Décembre",
}

# Template background for title cards
TITLE_CARD_TEMPLATE = (
    BASE_DIR
    / "program"
    / "background"
    / "good paper papier-blanc-dechire-message-dechire-papier-blanc-decharge-message-decharge-transparent_1028938-327852.png"
)

# Prefer H.264 + AAC where possible to keep Premiere compatibility.
YTDLP_FORMAT = (
    "bv*[vcodec~='^((he?|a)vc|h264)'][ext=mp4]+ba[acodec~='^(mp4a|aac)'][ext=m4a]/"
    "bv*[vcodec~='^((he?|a)vc|h264)']+ba[acodec~='^(mp4a|aac)']/"
    "bv*+ba/b"
)
# Progressive fallback keeps downloads working when DASH formats are blocked (HTTP 403)
YTDLP_PROGRESSIVE_FALLBACK_FORMAT = (
    "b[acodec!=none][vcodec~='^((he?|a)vc|h264)'][ext=mp4]/"
    "best[acodec!=none][ext=mp4]/b"
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("insert_downloader")


@dataclass
class LinkRecord:
    text: str
    href: str


class AnchorCollector(HTMLParser):
    """Simple parser that collects anchor tags and their text content."""

    def __init__(self) -> None:
        super().__init__()
        self._active_link: Optional[LinkRecord] = None
        self.links: List[LinkRecord] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        if tag.lower() != "a":
            return
        attrs_dict = dict(attrs)
        href = attrs_dict.get("href")
        if not href:
            return
        self._active_link = LinkRecord(text="", href=href)

    def handle_data(self, data: str):
        if self._active_link is not None:
            self._active_link.text += data

    def handle_endtag(self, tag: str):
        if tag.lower() != "a" or self._active_link is None:
            return
        self.links.append(self._active_link)
        self._active_link = None


def find_latest_html(source_dir: Path) -> Optional[Path]:
    if not source_dir.exists():
        return None
    html_files = sorted(
        (
            p
            for p in source_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".html", ".htm"}
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if html_files:
        return html_files[0]
    return None


def directory_is_empty(path: Path) -> bool:
    if not path.exists():
        return True
    try:
        next(path.iterdir())
        return False
    except StopIteration:
        return True


def pick_html_file() -> Path:
    primary = find_latest_html(INPUT_DIR)
    if primary:
        logger.info("Using latest /input HTML file: %s", primary.name)
        return primary

    fallback = find_latest_html(FALLBACK_INPUT_DIR)
    if fallback:
        logger.info("No local input found, falling back to: %s", fallback)
        if directory_is_empty(INPUT_DIR):
            INPUT_DIR.mkdir(parents=True, exist_ok=True)
            copy_path = INPUT_DIR / fallback.name
            try:
                shutil.copy2(fallback, copy_path)
                logger.info("Copied fallback HTML into /input as %s", copy_path.name)
                return copy_path
            except OSError as exc:
                logger.warning("Could not copy fallback HTML into /input: %s", exc)
        return fallback

    raise FileNotFoundError(
        "No HTML input files found in /input or fallback directory"
    )


def yt_dlp_cookie_options() -> Dict[str, object]:
    """
    Build yt-dlp cookie settings from the environment.

    Supported variables:
      INSERT_DL_COOKIES_FILE    -> absolute or relative path to Netscape cookie file.
      INSERT_DL_COOKIES_BROWSER -> browser name for yt-dlp's cookies-from-browser feature.
INSERT_DL_COOKIES_PROFILE -> optional browser profile name.
"""
    options: Dict[str, object] = {}
    cookie_file = os.environ.get("INSERT_DL_COOKIES_FILE")
    browser = os.environ.get("INSERT_DL_COOKIES_BROWSER")
    profile = os.environ.get("INSERT_DL_COOKIES_PROFILE")

    if cookie_file:
        path = Path(cookie_file).expanduser()
        if path.exists():
            options["cookiefile"] = str(path)
        else:
            logger.warning("COOKIE file %s not found; ignoring INSERT_DL_COOKIES_FILE", path)
        return options

    if browser:
        options["cookiesfrombrowser"] = (browser, profile, None, None)
        return options

    default_profile = BASE_DIR.parent / "uploader" / "selenium_profile" / "Default"
    if default_profile.exists():
        options["cookiesfrombrowser"] = ("chrome", str(default_profile), None, None)
        logger.info("Using Chrome selenium profile for cookies: %s", default_profile)

    return options


def _relative_path_exists(relative_path: Optional[str]) -> bool:
    if not relative_path:
        return False
    return (BASE_DIR / relative_path).exists()


def summary_entry_is_complete(
    expected_type: str,
    entry: Optional[Dict[str, Any]],
    *,
    url: Optional[str] = None,
) -> bool:
    """
    Determine if a summary entry matches the expected type/url and has its artifacts.
    """
    if not entry:
        return False
    if entry.get("type") != expected_type:
        return False
    if url and entry.get("source_url") != url:
        return False
    if expected_type == "video":
        downloaded_file = entry.get("downloaded_file")
        if not _relative_path_exists(downloaded_file):
            return False
        # Old direct-video inserts were converted into transparent overlay .mov files.
        # Treat those cached entries as stale so the downloader regenerates raw video outputs.
        if entry.get("overlay_canvas"):
            return False
        if isinstance(downloaded_file, str) and downloaded_file.lower().endswith(".mov"):
            return False
        return True
    if expected_type == "article":
        if entry.get("error"):
            return False
        required = (
            "article_snapshot",
            "article_html",
            "logo_file",
            "title_card_image",
        )
        return all(_relative_path_exists(entry.get(key)) for key in required)
    if expected_type == "image":
        downloaded_file = entry.get("downloaded_file")
        return _relative_path_exists(downloaded_file)
    return False


def clean_url(raw_url: str) -> str:
    url = raw_url.strip()
    parsed = urlparse(url)
    if parsed.netloc.endswith("google.com") and parsed.path == "/url":
        target = parse_qs(parsed.query).get("q", [None])[0]
        if target:
            return unquote(target)
    return url


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _sanitize_component(
    value: Optional[str],
    *,
    lowercase: bool,
    limit: int,
) -> Optional[str]:
    if not value:
        return None
    cleaned = (
        _strip_accents(value)
        .replace("@", " ")
        .replace("#", " ")
        .strip()
    )
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", cleaned)
    cleaned = cleaned.strip("_")
    if not cleaned:
        return None
    if lowercase:
        cleaned = cleaned.lower()
    if limit > 0 and len(cleaned) > limit:
        cleaned = cleaned[:limit]
    return cleaned


def _normalize_host(host: Optional[str]) -> Optional[str]:
    if not host:
        return None
    lowered = host.lower()
    lowered = lowered.split(":")[0]
    lowered = re.sub(r"^(?:www\d*\.|m\.)", "", lowered)
    return lowered


PLATFORM_CODE_MAP: Tuple[Tuple[str, str], ...] = (
    ("youtube.com", "ytb"),
    ("youtu.be", "ytb"),
    ("twitter.com", "x"),
    ("x.com", "x"),
    ("facebook.com", "fb"),
    ("instagram.com", "insta"),
    ("tiktok.com", "tkt"),
    ("vm.tiktok.com", "tkt"),
    ("reddit.com", "rdt"),
    ("vimeo.com", "vimeo"),
    ("dailymotion.com", "dai"),
    ("odysee.com", "odysee"),
    ("rumble.com", "rumble"),
)


def infer_platform_code(netloc: Optional[str]) -> str:
    host = _normalize_host(netloc)
    if not host:
        return "web"
    for keyword, code in PLATFORM_CODE_MAP:
        if keyword in host:
            return code
    return "web"


def build_source_token(*candidates: Optional[str]) -> str:
    for candidate in candidates:
        if not candidate:
            continue
        token = _sanitize_component(candidate, lowercase=True, limit=32)
        if token and not token.startswith("google"):
            return token
    return "source"


def build_title_token(title: Optional[str], fallback: str, *, limit: int = 80) -> str:
    token = _sanitize_component(title or fallback, lowercase=False, limit=limit)
    return token or "insert"


def build_insert_stem(
    insert_index: int,
    *,
    artifact: Optional[str],
    source_candidates: Sequence[Optional[str]],
    netloc: Optional[str],
    title_hint: Optional[str],
    fallback_title: str,
) -> str:
    artifact_token = _sanitize_component(artifact, lowercase=False, limit=24)
    source_token = build_source_token(*source_candidates, _normalize_host(netloc))
    platform_token = infer_platform_code(netloc)
    title_token = build_title_token(title_hint, fallback_title)
    name = f"{insert_index}_"
    if artifact_token:
        name += artifact_token
    name += f"@{source_token}"
    extra_parts: List[str] = []
    if platform_token:
        extra_parts.append(platform_token)
    if title_token:
        extra_parts.append(title_token)
    if extra_parts:
        name += "_" + "_".join(extra_parts)
    return name


def rename_with_schema(file_path: Optional[Path], desired_stem: str) -> Optional[Path]:
    if file_path is None or not file_path.exists():
        return file_path
    suffix = file_path.suffix
    target = file_path.parent / f"{desired_stem}{suffix}"
    if target.exists() and target != file_path:
        try:
            target.unlink()
        except OSError as exc:  # noqa: BLE001
            logger.debug("Could not remove previous file %s: %s", target, exc)
            return file_path
    if target == file_path:
        return file_path
    file_path.rename(target)
    return target


def is_video_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    for keyword in VIDEO_HOST_KEYWORDS:
        if keyword in host:
            return True
    path = parsed.path.lower()
    return path.endswith((".mp4", ".mov", ".mkv", ".webm"))


def is_image_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    return path.endswith(IMAGE_EXTENSIONS)


def extract_document_title(html_text: str) -> Optional[str]:
    title_match = re.search(r"TITRE\s*[:\-]\s*(.+)", html_text, re.IGNORECASE)
    if title_match:
        return html.unescape(title_match.group(1)).strip()
    head_match = re.search(
        r"<title[^>]*>(?P<title>.+?)</title>",
        html_text,
        re.IGNORECASE | re.DOTALL,
    )
    if head_match:
        return html.unescape(head_match.group("title").strip())
    return None


def prepare_processing_context() -> Tuple[
    Path, Optional[str], List[LinkRecord], Path, "SummaryContext"
]:
    html_path = pick_html_file()
    html_text = html_path.read_text(encoding="utf-8")
    document_title = extract_document_title(html_text)
    parser = AnchorCollector()
    parser.feed(html_text)
    target_output_dir = OUTPUT_DIR / html_path.stem
    target_output_dir.mkdir(parents=True, exist_ok=True)
    summary = load_summary_context(html_path, document_title, target_output_dir)
    if not parser.links:
        logger.warning("No anchor links found inside %s", html_path.name)
    return html_path, document_title, parser.links, target_output_dir, summary


def fetch_article_metadata(
    url: str,
    artifact_dir: Path,
    name_seed: str,
) -> Dict[str, Optional[str]]:
    from get__title_author_date import extract_metadata, extract_metadata_from_html

    metadata: Dict[str, Optional[str]] = {
        "source": None,
        "title": None,
        "description": None,
        "excerpt": None,
        "author": None,
        "published": None,
        "snapshot_file": None,
        "html_file": None,
        "metadata_file": None,
        "error": None,
    }
    parsed = urlparse(url)
    metadata["source"] = parsed.netloc or None
    response_text: Optional[str] = None
    extracted: Dict[str, Any] = {
        "url": url,
        "source": metadata["source"],
        "title": None,
        "authors": [],
        "authors_text": None,
        "date_universal": None,
        "status": "error",
        "error": None,
    }
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        response_text = resp.text
    except Exception as exc:  # noqa: BLE001
        metadata["error"] = str(exc)
        extracted = extract_metadata(url, timeout=20.0)
        metadata["source"] = extracted.get("source") or metadata["source"]
        metadata["title"] = extracted.get("title")
        metadata["author"] = extracted.get("authors_text")
        metadata["published"] = extracted.get("date_universal")
    else:
        soup = BeautifulSoup(response_text, "html.parser")
        extracted = extract_metadata_from_html(url, response_text)
        metadata["source"] = extracted.get("source") or metadata["source"]
        metadata["title"] = extracted.get("title")
        metadata["author"] = extracted.get("authors_text")
        metadata["published"] = extracted.get("date_universal")

        def _meta_content(names: Tuple[str, ...]) -> Optional[str]:
            for name in names:
                tag = soup.find("meta", attrs={"name": name}) or soup.find(
                    "meta", attrs={"property": name}
                )
                if tag and tag.get("content"):
                    return html.unescape(tag["content"].strip())
            return None

        title_tag = soup.find("title")
        og_title = _meta_content(("og:title", "twitter:title"))
        raw_title = og_title or (
            title_tag.string.strip() if title_tag and title_tag.string else None
        )
        metadata["title"] = metadata["title"] or raw_title
        metadata["description"] = _meta_content(
            ("description", "og:description", "twitter:description")
        )

        def _gather_paragraphs(root: BeautifulSoup) -> List[str]:
            texts: List[str] = []
            for para in root.find_all("p"):
                text = para.get_text(separator=" ", strip=True)
                if text:
                    texts.append(text)
                if len(texts) >= 8:
                    break
            return texts

        text_candidates: List[str] = []
        for selector in ("article", "main"):
            section = soup.find(selector)
            if section:
                text_candidates = _gather_paragraphs(section)
                if text_candidates:
                    break
        if not text_candidates:
            text_candidates = _gather_paragraphs(soup)

        if text_candidates:
            metadata["excerpt"] = "\n\n".join(text_candidates[:5])

        metadata["author"] = metadata["author"] or _detect_authors(soup, _meta_content)
        metadata["published"] = metadata["published"] or _meta_content(
            (
                "article:published_time",
                "article:modified_time",
                "og:updated_time",
                "publish-date",
                "date",
                "pubdate",
            )
        )
        if not metadata["published"]:
            metadata["published"] = _guess_article_date(url, soup)

    artifact_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = artifact_dir / f"{name_seed}.md"
    html_dump_path = artifact_dir / f"{name_seed}.html"
    metadata_json_path = artifact_dir / f"{name_seed}.json"

    summary_lines = [
        f"# {metadata['title'] or 'Article Snapshot'}",
        "",
        f"- URL: {url}",
        f"- Source: {metadata['source'] or 'Unknown'}",
        f"- Retrieved: {datetime.utcnow().isoformat(timespec='seconds')}Z",
    ]
    if metadata["description"]:
        summary_lines.extend(["", f"**Description:** {metadata['description']}"])
    if metadata["excerpt"]:
        summary_lines.extend(["", "## Excerpt", metadata["excerpt"]])
    if metadata["error"]:
        summary_lines.extend(["", f"**Fetch error:** {metadata['error']}"])

    snapshot_path.write_text("\n".join(summary_lines), encoding="utf-8")
    html_dump_path.write_text(response_text or "", encoding="utf-8")
    metadata_json_path.write_text(
        json.dumps(
            {
                "url": url,
                "source": metadata["source"],
                "title": metadata["title"],
                "authors": extracted.get("authors") or [],
                "authors_text": metadata["author"],
                "date_universal": metadata["published"],
                "description": metadata["description"],
                "excerpt": metadata["excerpt"],
                "status": extracted.get("status"),
                "error": metadata["error"],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    metadata["snapshot_file"] = os.path.relpath(snapshot_path, BASE_DIR)
    metadata["html_file"] = os.path.relpath(html_dump_path, BASE_DIR)
    metadata["metadata_file"] = os.path.relpath(metadata_json_path, BASE_DIR)
    return metadata


def _detect_authors(
    soup: BeautifulSoup, meta_lookup
) -> Optional[str]:
    authors: List[str] = []
    meta_author = meta_lookup(("author", "article:author", "og:author", "twitter:creator"))
    if meta_author:
        authors.extend(_split_authors(meta_author))

    for tag in soup.select('[itemprop="author"], [rel="author"]'):
        text = tag.get_text(strip=True)
        if text:
            authors.extend(_split_authors(text))

    def _class_matches(cls: Optional[str]) -> bool:
        if not cls:
            return False
        lowered = cls.lower()
        return "author" in lowered or "byline" in lowered

    def _class_predicate(cls_value):
        if isinstance(cls_value, (list, tuple, set)):
            return any(_class_matches(str(c)) for c in cls_value)
        if cls_value is None:
            return False
        return _class_matches(str(cls_value))

    for tag in soup.find_all(class_=_class_predicate):
        text = tag.get_text(separator=" ", strip=True)
        if text and len(text) < 200:
            authors.extend(_split_authors(text))

    cleaned: List[str] = []
    for name in authors:
        name = name.strip(" \n\r\t,;")
        if not name:
            continue
        if name.lower().startswith("par "):
            name = name[4:].strip()
        lowered = name.lower()
        if "collectif" in lowered or "expert" in lowered or "experte" in lowered:
            continue
        if name and name not in cleaned:
            cleaned.append(name)
    if cleaned:
        return ", ".join(cleaned)
    return None


def _split_authors(value: str) -> List[str]:
    if not value:
        return []
    parts = re.split(r"\s*(?:,| et | and |&)\s*", value)
    return [part.strip() for part in parts if part.strip()]


MIN_LOGO_SIZE_PX = 64  # reject logos smaller than this in both dimensions


def _scrape_logo_urls_from_homepage(domain: str) -> List[str]:
    """Fetch the site homepage and return high-quality logo image URL candidates.

    Priority order:
    1. Largest apple-touch-icon (standardised 180×180 brand icon)
    2. <img> tags with logo/brand keywords in header
    3. JSON-LD Organisation.logo
    SVG sources are skipped (PIL cannot open them without extra deps).
    """
    urls: List[str] = []
    base = f"https://{domain}"
    try:
        resp = requests.get(
            base, timeout=10,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
        if resp.status_code != 200:
            return urls
        soup = BeautifulSoup(resp.text, "html.parser")

        # 1. Apple touch icons — standardised brand icons, usually 180×180
        touch_icons: List[Tuple[int, str]] = []
        for link in soup.find_all("link"):
            rel = link.get("rel") or []
            if isinstance(rel, str):
                rel = [rel]
            if any(r in rel for r in ("apple-touch-icon", "apple-touch-icon-precomposed")):
                href = (link.get("href") or "").strip()
                if not href or href.lower().endswith(".svg"):
                    continue
                sizes = link.get("sizes") or ""
                try:
                    size_px = int(sizes.split("x")[0]) if "x" in sizes else 0
                except (ValueError, AttributeError):
                    size_px = 0
                full = href if href.startswith("http") else (base + href if href.startswith("/") else base + "/" + href)
                touch_icons.append((size_px, full))
        touch_icons.sort(reverse=True)
        urls.extend(url for _, url in touch_icons)

        # 2. Logo <img> inside <header> (or whole page if no header tag)
        header_root = soup.find("header") or soup
        for img in header_root.find_all("img"):
            src = (img.get("src") or "").strip()
            if not src or src.lower().endswith(".svg"):
                continue
            alt = (img.get("alt") or "").lower()
            cls = " ".join(img.get("class") or []).lower()
            img_id = (img.get("id") or "").lower()
            src_lower = src.lower()
            if any(kw in s for s in (src_lower, alt, cls, img_id) for kw in ("logo", "brand", "header-img", "site-logo")):
                full = src if src.startswith("http") else (base + src if src.startswith("/") else base + "/" + src)
                if full not in urls:
                    urls.append(full)

        # 3. JSON-LD Organisation.logo
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    logo_raw = (
                        item.get("logo")
                        or (item.get("publisher") or {}).get("logo")
                        or ""
                    )
                    logo_url = logo_raw.get("url", "") if isinstance(logo_raw, dict) else logo_raw
                    if (
                        isinstance(logo_url, str)
                        and logo_url.startswith("http")
                        and not logo_url.lower().endswith(".svg")
                        and logo_url not in urls
                    ):
                        urls.append(logo_url)
            except Exception:
                pass

    except Exception as exc:
        logger.debug("Homepage scrape for logo failed (%s): %s", domain, exc)
    return urls


def _logo_fallback_sources(domain: str) -> List[str]:
    """Favicon-service fallbacks — low resolution, used only when scraping fails."""
    return [
        f"https://logo.clearbit.com/{domain}",
        f"https://{domain}/apple-touch-icon.png",
        f"https://{domain}/apple-touch-icon-precomposed.png",
        f"https://www.google.com/s2/favicons?domain={domain}&sz=256",
        f"https://icon.horse/icon/{domain}",
        f"https://www.google.com/s2/favicons?domain={domain}&sz=128",
        f"https://{domain}/favicon.ico",
    ]


def _try_download_logo(url: str, dest: Path, headers: Dict[str, str]) -> Optional[Path]:
    """Download a single logo URL, validate size, save to dest. Returns dest or None."""
    try:
        resp = requests.get(url, allow_redirects=True, timeout=10, headers=headers)
    except Exception as exc:
        logger.debug("Logo fetch failed from %s: %s", url, exc)
        return None
    if resp.status_code != 200 or len(resp.content) < 500:
        return None
    content_type = resp.headers.get("content-type", "")
    if "image" not in content_type and not url.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".ico")):
        return None
    try:
        from io import BytesIO
        img = Image.open(BytesIO(resp.content))
        w, h = img.size
        if w < MIN_LOGO_SIZE_PX or h < MIN_LOGO_SIZE_PX:
            logger.debug("Logo too small (%dx%d) from %s — skipped", w, h, url)
            return None
    except Exception:
        return None
    try:
        dest.write_bytes(resp.content)
        return dest
    except OSError as exc:
        logger.debug("Could not store logo %s: %s", dest, exc)
        return None


def download_logo_for_domain(
    domain: Optional[str], dest_dir: Path, output_stem: str
) -> Optional[Path]:
    if not domain:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = dest_dir / f"{output_stem}.png"
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13.0)"}

    # First: scrape the homepage for real brand logos
    scraped = _scrape_logo_urls_from_homepage(domain)
    for url in scraped:
        result = _try_download_logo(url, filename, headers)
        if result:
            logger.info("Logo found via page scrape: %s (%s)", url, domain)
            return result

    # Fallback: favicon/logo services (may be low-res)
    for url in _logo_fallback_sources(domain):
        result = _try_download_logo(url, filename, headers)
        if result:
            logger.info("Logo found via fallback service: %s (%s)", url, domain)
            return result

    logger.warning("No logo found for domain %s", domain)
    return None


def _load_title_font(size: int = 36) -> ImageFont.ImageFont:
    candidate_paths = [
        Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/shared_assets/montserrat/Montserrat-BlackItalic.ttf"),
        Path("/System/Library/Fonts/SFNSDisplay.ttf"),
        Path("/System/Library/Fonts/SFNS.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/Library/Fonts/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidate_paths:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap_text_for_width(
    text: str, font: ImageFont.ImageFont, max_width: int
) -> List[str]:
    if not text:
        return []
    dummy = Image.new("L", (1, 1))
    draw = ImageDraw.Draw(dummy)
    words = text.split()
    lines: List[str] = []
    current: List[str] = []

    def _split_long_word(word: str) -> List[str]:
        pieces: List[str] = []
        chunk = ""
        for char in word:
            candidate = chunk + char
            if not chunk or draw.textlength(candidate, font=font) <= max_width:
                chunk = candidate
                continue
            pieces.append(chunk)
            chunk = char
        if chunk:
            pieces.append(chunk)
        return pieces

    for word in words:
        candidate = " ".join(current + [word]).strip()
        width = draw.textlength(candidate, font=font)
        if width <= max_width:
            current.append(word)
            continue
        if not current:
            long_chunks = _split_long_word(word)
            lines.extend(long_chunks)
            continue
        lines.append(" ".join(current))
        current = []
        word_width = draw.textlength(word, font=font)
        if word_width > max_width:
            long_chunks = _split_long_word(word)
            lines.extend(long_chunks)
            continue
        current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


def _parse_french_date(text: str) -> Optional[date]:
    # ISO formats
    normalized = text.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(normalized[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        pass

    match = re.search(
        r"(?P<day>\d{1,2})\s+(?P<month>[a-zéûôî]+)\s+(?P<year>\d{4})",
        text,
        re.IGNORECASE,
    )
    if match:
        month_name = match.group("month").lower()
        month_map = {
            "janvier": 1,
            "février": 2,
            "fevrier": 2,
            "mars": 3,
            "avril": 4,
            "mai": 5,
            "juin": 6,
            "juillet": 7,
            "août": 8,
            "aout": 8,
            "septembre": 9,
            "octobre": 10,
            "novembre": 11,
            "décembre": 12,
            "decembre": 12,
        }
        month_num = month_map.get(month_name)
        if month_num:
            return date(int(match.group("year")), month_num, int(match.group("day")))
    return None


def _normalize_date_candidate(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    parsed = _parse_french_date(value.strip())
    if not parsed:
        return None
    return parsed.isoformat()


def _extract_date_from_url(url: str) -> Optional[str]:
    parsed = urlparse(url)
    path = parsed.path
    match = re.search(r"/(\d{4})[/-](\d{1,2})[/-](\d{1,2})", path)
    if match:
        year, month, day = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    match = re.search(r"/(\d{4})(\d{2})(\d{2})/", path)
    if match:
        year, month, day = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return None


def _tag_has_date_hint(tag: Tag) -> bool:
    if not isinstance(tag, Tag):
        return False
    classes = tag.get("class")
    if classes:
        for cls in classes:
            lowered = str(cls).lower()
            if any(keyword in lowered for keyword in ("date", "time", "publi", "update", "posted")):
                return True
    attrs_to_check = ("data-date", "data-time", "data-published", "data-updated")
    return any(tag.get(attr) for attr in attrs_to_check)


def _date_from_tag(tag: Tag) -> Optional[str]:
    if not isinstance(tag, Tag):
        return None
    candidate_attrs = (
        "datetime",
        "content",
        "data-date",
        "data-time",
        "data-published",
        "data-updated",
        "data-modified",
        "title",
    )
    for attr in candidate_attrs:
        normalized = _normalize_date_candidate(tag.get(attr))
        if normalized:
            return normalized
    text = tag.get_text(" ", strip=True)
    return _normalize_date_candidate(text)


def _guess_article_date(url: str, soup: BeautifulSoup) -> Optional[str]:
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
            result = _date_from_tag(tag)
            if result:
                return result
    for tag in soup.find_all(_tag_has_date_hint):
        result = _date_from_tag(tag)
        if result:
            return result
    url_guess = _extract_date_from_url(url)
    if url_guess:
        return url_guess
    return None


def format_display_date(
    raw: Optional[str], fallback: Optional[date] = None
) -> Optional[str]:
    target_date: Optional[date] = None
    if raw:
        text = raw.strip()
        lowered = text.lower()
        if "aujourd" in lowered or "today" in lowered:
            target_date = fallback
        else:
            target_date = _parse_french_date(text)

    if not target_date:
        target_date = fallback

    if not target_date:
        return None
    return f"{target_date.day} {FRENCH_MONTHS[target_date.month]} {target_date.year}"


def create_title_card_image(
    title: Optional[str],
    logo_path: Optional[Path],
    output_path: Path,
    *,
    author: Optional[str] = None,
    published: Optional[str] = None,
    fallback_date: Optional[date] = None,
) -> Optional[Path]:
    if not TITLE_CARD_TEMPLATE.exists():
        logger.error("Template image not found: %s", TITLE_CARD_TEMPLATE)
        return None

    try:
        template = Image.open(TITLE_CARD_TEMPLATE).convert("RGBA")
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to open template: %s", exc)
        return None

    canvas = template.copy()
    draw = ImageDraw.Draw(canvas)
    margin_x = 80
    top_margin = 170
    bottom_margin = 220
    inner_width = canvas.width - 2 * margin_x
    y_cursor = top_margin

    if logo_path and Path(logo_path).exists():
        try:
            logo = Image.open(logo_path).convert("RGBA")
            max_width = inner_width
            max_height = int(canvas.height * 0.28)
            scale = min(
                max_width / logo.width if logo.width else 1.0,
                max_height / logo.height if logo.height else 1.0,
                1.0,
            )
            new_size = (
                max(1, int(logo.width * scale * 0.75)),
                max(1, int(logo.height * scale * 0.75)),
            )
            logo = logo.resize(new_size, Image.LANCZOS)

            mask = Image.new("L", logo.size, 0)
            mask_draw = ImageDraw.Draw(mask)
            radius = int(min(logo.size) * 0.15)
            mask_draw.rounded_rectangle(
                [(0, 0), logo.size], radius=radius, fill=255
            )
            rounded_logo = Image.new("RGBA", logo.size)
            rounded_logo.paste(logo, mask=mask)

            x_pos = (canvas.width - rounded_logo.width) // 2
            canvas.alpha_composite(rounded_logo, (x_pos, y_cursor))
            y_cursor += rounded_logo.height + 12
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to render logo %s: %s", logo_path, exc)

    display_title = (title or "").strip() or "Article"
    max_text_width = int(inner_width * 0.75)
    reserved_bottom = bottom_margin + 40
    available_height = canvas.height - y_cursor - reserved_bottom
    line_spacing = 6
    base_font_size = 36
    min_font_size = 18
    chosen_font: Optional[ImageFont.ImageFont] = None
    chosen_lines: List[str] = []
    chosen_line_height = 0

    for size in range(base_font_size, min_font_size - 1, -2):
        candidate_font = _load_title_font(size=size)
        candidate_lines = _wrap_text_for_width(display_title, candidate_font, max_text_width)
        if not candidate_lines:
            candidate_lines = [display_title]
        candidate_line_height = (
            candidate_font.getbbox("Ag")[3] - candidate_font.getbbox("Ag")[1]
        )
        max_lines = max(1, available_height // (candidate_line_height + line_spacing))
        if len(candidate_lines) <= max_lines:
            chosen_font = candidate_font
            chosen_lines = candidate_lines
            chosen_line_height = candidate_line_height
            break

    if not chosen_lines:
        chosen_font = _load_title_font(size=min_font_size)
        chosen_lines = _wrap_text_for_width(display_title, chosen_font, max_text_width)
        if not chosen_lines:
            chosen_lines = [display_title]
        chosen_line_height = chosen_font.getbbox("Ag")[3] - chosen_font.getbbox("Ag")[1]
        max_lines = max(1, available_height // (chosen_line_height + line_spacing))
        if len(chosen_lines) > max_lines:
            chosen_lines = chosen_lines[:max_lines]
            if chosen_lines:
                chosen_lines[-1] += "…"
    if chosen_font is None:
        chosen_font = _load_title_font(size=base_font_size)
        chosen_line_height = chosen_font.getbbox("Ag")[3] - chosen_font.getbbox("Ag")[1]
        if not chosen_lines:
            chosen_lines = [display_title]

    for line in chosen_lines:
        bbox = draw.textbbox((0, 0), line, font=chosen_font)
        text_width = bbox[2] - bbox[0]
        x_pos = max(margin_x, (canvas.width - text_width) // 2)
        x_pos = min(x_pos, canvas.width - margin_x - text_width)
        draw.text(
            (x_pos, y_cursor),
            line,
            font=chosen_font,
            fill=(0, 0, 0, 210),
        )
        y_cursor += chosen_line_height + line_spacing

    info_parts = []
    if author:
        info_parts.append(author.strip())
    formatted_date = format_display_date(published, fallback_date)
    if formatted_date:
        info_parts.append(formatted_date)
    if info_parts:
        info_text = " • ".join(info_parts)
        info_font = _load_title_font(size=16)
        info_lines = _wrap_text_for_width(info_text, info_font, inner_width)
        if not info_lines:
            info_lines = [info_text]
        info_line_height = info_font.getbbox("Ag")[3] - info_font.getbbox("Ag")[1]
        info_spacing = 4
        total_height = (
            len(info_lines) * info_line_height
            + info_spacing * (len(info_lines) - 1 if len(info_lines) > 1 else 0)
        )
        info_y = canvas.height - bottom_margin - total_height
        for line in info_lines:
            info_bbox = draw.textbbox((0, 0), line, font=info_font)
            info_width = info_bbox[2] - info_bbox[0]
            info_x = max(margin_x, (canvas.width - info_width) // 2)
            info_x = min(info_x, canvas.width - margin_x - info_width)
            draw.text(
                (info_x, info_y),
                line,
                font=info_font,
                fill=(0, 0, 0, 200),
            )
            info_y += info_line_height + info_spacing

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(output_path)
        return output_path
    except OSError as exc:  # noqa: BLE001
        logger.error("Could not save title card %s: %s", output_path, exc)
        return None


def cleanup_download_artifacts(base_path: Path, keep: Optional[Path]) -> None:
    parent = base_path.parent
    prefix = base_path.name
    for candidate in parent.glob(f"{prefix}*"):
        if keep is not None and candidate.resolve() == keep.resolve():
            continue
        if candidate.is_dir():
            continue
        try:
            candidate.unlink()
        except OSError as exc:
            logger.debug("Could not delete artifact %s: %s", candidate, exc)


def build_summary_entry(
    *,
    label: str,
    url: str,
    entry_type: str,
    source: Optional[str],
    extra: Dict[str, Optional[str]],
    downloaded_file: Optional[Path],
) -> Dict[str, Optional[str]]:
    entry: Dict[str, Optional[str]] = {
        "label": label or None,
        "type": entry_type,
        "source_url": url,
        "source": source,
    }
    entry.update(extra)
    if downloaded_file is not None:
        entry["downloaded_file"] = os.path.relpath(downloaded_file, BASE_DIR)
    return entry


@dataclass
class SummaryContext:
    html_path: Path
    document_title: Optional[str]
    summary_path: Path
    entries: Dict[int, Dict[str, Optional[str]]]

    def update_entry(self, insert_index: int, entry: Dict[str, Optional[str]]) -> None:
        entry = dict(entry)
        entry["insert_index"] = insert_index
        self.entries[insert_index] = entry

    def _serializable(self) -> Dict[str, object]:
        ordered = [self.entries[idx] for idx in sorted(self.entries)]
        return {
            "input_file": str(self.html_path),
            "document_title": self.document_title,
            "entries": ordered,
        }

    def save(self) -> None:
        payload = self._serializable()
        self.summary_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def load_summary_context(
    html_path: Path, document_title: Optional[str], target_output_dir: Path
) -> SummaryContext:
    summary_path = target_output_dir / f"{html_path.stem}_metadata.json"
    entries: Dict[int, Dict[str, Optional[str]]] = {}
    if summary_path.exists():
        existing = json.loads(summary_path.read_text(encoding="utf-8"))
        document_title = existing.get("document_title") or document_title
        for idx, entry in enumerate(existing.get("entries", []), start=1):
            insert_idx = entry.get("insert_index") or idx
            entries[int(insert_idx)] = entry
    return SummaryContext(html_path, document_title, summary_path, entries)
