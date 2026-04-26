#!/usr/bin/env python3
"""Download institution imagery from the latest universal comparser CSV."""

from __future__ import annotations

import argparse
import csv
import html
import imghdr
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping, Sequence

import requests

try:
    from serpapi import GoogleSearch  # type: ignore
except ImportError:  # pragma: no cover
    GoogleSearch = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE_BASE = PROJECT_ROOT.parent
COMPARER_OUTPUT_DIR = CODE_BASE / "Comparser" / "output"
UNIVERSAL_HTML_DIR = CODE_BASE / "swisser" / "Universal_pipe" / "html"
OUTPUT_DIR = PROJECT_ROOT / "output"
DEFAULT_MIN_FILE_SIZE = 15_000  # bytes
ALLOWED_IMAGE_TYPES = {"jpeg", "png", "webp"}
TARGET_COLUMNS = (
    ("institution", "Institution Mention"),
)
CATEGORY_FOLDERS = {
    "institution": "institutions",
}
DISALLOWED_SOURCES = (
    "alamy",
    "gettyimages",
    "shutterstock",
    "adobe stock",
    "stock.adobe",
    "istockphoto",
    "dreamstime",
    "123rf",
    "depositphotos",
)
TEXT_PANEL_KEYWORDS = (
    "pdf",
    "rapport",
    "memoire",
    "mémoire",
    "note de synthèse",
    "communiqué",
    "article",
    "texte",
    "document",
    "thèse",
    "thesis",
    "press release",
    "blog",
    "tribune",
    "manifesto",
    "lettre",
    "statement",
    "programme",
    "magazine",
    "journal",
    "publication",
    "université",
    "faculté",
    "dossier",
    "affiche",
)
BUILDING_KEYWORDS = (
    "headquarters",
    "headquarter",
    "building",
    "campus",
    "offices",
    "office",
    "site",
    "siege",
    "siège",
    "mairie",
    "ministere",
    "ministère",
    "institution",
    "university",
    "school",
    "faculté",
    "centre",
    "center",
    "bureau",
    "agency",
)
LOGO_KEYWORDS = (
    "logo",
    "emblem",
    "blason",
    "crest",
    "seal",
)


@dataclass
class InstitutionEntry:
    """Track institution metadata across rows."""

    text: str
    category: str
    count: int = 0
    rows: set[int] = field(default_factory=set)

    def add_occurrence(self, row_index: int) -> None:
        self.count += 1
        self.rows.add(row_index)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download institution imagery using SerpAPI.")
    parser.add_argument(
        "--input-csv",
        type=Path,
        help="Path to a *_comparison*.csv file (defaults to the newest file in Comparser/output).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Override output directory (defaults to insert_creator/output).",
    )
    parser.add_argument(
        "--serpapi-key",
        help="SerpAPI key (defaults to SERPAPI_KEY env variable).",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=5,
        help="Maximum image results to try per query (default: 5).",
    )
    parser.add_argument(
        "--min-file-size",
        type=int,
        default=DEFAULT_MIN_FILE_SIZE,
        help=f"Minimum image size in bytes (default: {DEFAULT_MIN_FILE_SIZE}).",
    )
    parser.add_argument(
        "--min-dimension",
        type=int,
        default=400,
        help="Reject image results with width or height below this value when metadata is available (default: 400).",
    )
    return parser.parse_args()


def find_latest_comparison_csv(directory: Path) -> Path:
    if not directory.exists():
        raise FileNotFoundError(f"{directory} does not exist.")
    candidates = [
        path
        for path in directory.rglob("*comparison.csv")
        if path.is_file()
    ]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No *_comparison.csv files found under {directory}")
    return candidates[0]


def load_csv(path: Path) -> tuple[List[str], List[List[str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter=";")
        try:
            header = next(reader)
        except StopIteration:  # pragma: no cover
            raise RuntimeError(f"{path} does not contain any rows") from None
        rows = [list(row) for row in reader]
    return header, rows


def build_header_map(header: Sequence[str]) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    for idx, name in enumerate(header):
        mapping[name.strip().lower()] = idx
    return mapping


def require_column(header_map: Mapping[str, int], column_name: str) -> int:
    key = column_name.strip().lower()
    if key not in header_map:
        raise KeyError(f"Required column '{column_name}' missing from CSV.")
    return header_map[key]


def split_multi_value(value: str | None) -> List[str]:
    if not value:
        return []
    parts = value.split("|")
    cleaned: List[str] = []
    for part in parts:
        fragment = part.strip().strip('"').strip()
        if fragment:
            cleaned.append(fragment)
    return cleaned


def sanitize_filename(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = normalized.lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    normalized = normalized.strip("_")
    return normalized or "institution"


def normalize_institution(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = normalized.strip()
    return normalized


def collect_institutions(
    rows: Sequence[Sequence[str]],
    header_map: Mapping[str, int],
) -> Dict[str, InstitutionEntry]:
    manifest: Dict[str, InstitutionEntry] = {}
    for category, column_name in TARGET_COLUMNS:
        try:
            column_index = require_column(header_map, column_name)
        except KeyError:
            continue
        for row_index, row in enumerate(rows, start=1):
            if column_index >= len(row):
                continue
            cell_value = row[column_index]
            for institution in split_multi_value(cell_value):
                normalized = normalize_institution(institution)
                if not normalized:
                    continue
                bucket_key = f"{category}::{normalized.lower()}"
                entry = manifest.get(bucket_key)
                if not entry:
                    entry = InstitutionEntry(text=normalized, category=category)
                    manifest[bucket_key] = entry
                entry.add_occurrence(row_index)
    return manifest


def resolve_serpapi_key(candidate: str | None) -> str:
    api_key = candidate or os.environ.get("SERPAPI_KEY")
    if not api_key:
        raise RuntimeError("SerpAPI key is required. Pass --serpapi-key or set SERPAPI_KEY.")
    return api_key


class ImageFetcher:
    def __init__(
        self,
        api_key: str,
        base_output: Path,
        max_results: int = 5,
        min_file_size: int = DEFAULT_MIN_FILE_SIZE,
        min_dimension: int = 400,
    ) -> None:
        if GoogleSearch is None:
            raise RuntimeError("Install serpapi (`pip install serpapi`) to fetch images.")
        self.api_key = api_key
        self.max_results = max_results
        self.min_file_size = max(min_file_size, 1024)
        self.min_dimension = max(min_dimension, 0)
        self.base_output = base_output
        self.session = requests.Session()
        categories = {category for category, _ in TARGET_COLUMNS}
        self.category_dirs: Dict[str, Path] = {}
        for category in categories:
            folder_name = CATEGORY_FOLDERS.get(category, f"{category}s")
            directory = base_output / folder_name
            directory.mkdir(parents=True, exist_ok=True)
            self.category_dirs[category] = directory

    def build_queries(self, institution: str, category: str) -> List[str]:
        base_queries = [
            f'"{institution}" headquarters',
            f'"{institution}" building',
            f'"{institution}" campus',
            f'"{institution}" signage',
            f'"{institution}" site',
            f'"{institution}"',
        ]
        if "université" in institution.lower() or "university" in institution.lower():
            base_queries.insert(0, f'"{institution}" campus photo')
        return base_queries

    def search_images(self, query: str) -> List[MutableMapping[str, str]]:
        params = {
            "engine": "google_images",
            "q": query,
            "api_key": self.api_key,
            "num": self.max_results,
            "safe": "active",
        }
        search = GoogleSearch(params)  # type: ignore[operator]
        results = search.get_dict()
        if "error" in results:
            raise RuntimeError(f"SerpAPI error for '{query}': {results['error']}")
        return results.get("images_results") or []

    def _contains_disallowed_source(self, text: str | None) -> bool:
        if not text:
            return False
        haystack = text.lower()
        return any(token in haystack for token in DISALLOWED_SOURCES)

    def _result_metadata_text(self, result: Mapping[str, object]) -> str:
        parts: List[str] = []
        for key in ("title", "snippet", "source", "displayed_link", "link"):
            value = result.get(key)
            if isinstance(value, str):
                parts.append(value)
        return " ".join(parts)

    def _compute_result_score(self, result: Mapping[str, object]) -> float:
        text = (result.get("title") or "") + " " + (result.get("snippet") or "")
        lowered = text.lower()
        score = 0.0
        for term in BUILDING_KEYWORDS:
            if term in lowered:
                score += 1.2
        for term in LOGO_KEYWORDS:
            if term in lowered:
                score += 0.5
        link = str(result.get("displayed_link") or "").lower()
        if "wikipedia" in link or "wikimedia" in link:
            score += 0.5
        width = result.get("original_width") or result.get("width")
        height = result.get("original_height") or result.get("height")
        try:
            width_val = int(width) if width is not None else None
            height_val = int(height) if height is not None else None
        except (TypeError, ValueError):
            width_val = None
            height_val = None
        if width_val and height_val and height_val > 0:
            ratio = width_val / height_val
            if 0.7 <= ratio <= 2.5:
                score += 1.0
            if ratio < 0.5 or ratio > 3.0:
                score -= 1.0
        return score

    def _looks_like_text_panel(self, result: Mapping[str, object]) -> bool:
        text = self._result_metadata_text(result)
        lowered = text.lower()
        if lowered and any(keyword in lowered for keyword in TEXT_PANEL_KEYWORDS):
            return True
        width = result.get("original_width") or result.get("width")
        height = result.get("original_height") or result.get("height")
        try:
            width_val = int(width) if width is not None else None
            height_val = int(height) if height is not None else None
        except (TypeError, ValueError):
            return False
        if width_val and height_val:
            ratio = width_val / height_val
            if 0.65 <= ratio <= 1.6 and lowered and ("université" in lowered or "memoire" in lowered):
                return True
        return False

    def _is_resolution_sufficient(self, result: Mapping[str, object]) -> bool:
        if self.min_dimension <= 0:
            return True
        width = result.get("original_width") or result.get("width")
        height = result.get("original_height") or result.get("height")
        try:
            width_val = int(width) if width is not None else None
            height_val = int(height) if height is not None else None
        except (TypeError, ValueError):
            return True
        if width_val is None or height_val is None:
            return True
        return width_val >= self.min_dimension and height_val >= self.min_dimension

    def download_image(self, url: str, destination: Path) -> Path | None:
        try:
            response = self.session.get(url, timeout=20)
        except requests.RequestException as exc:
            print(f"   ⚠️  Request failed for {url}: {exc}")
            return None
        if response.status_code != 200:
            print(f"   ⚠️  HTTP {response.status_code} while fetching {url}")
            return None
        if len(response.content) < self.min_file_size:
            print(f"   ⚠️  Image too small ({len(response.content)} bytes)")
            return None
        tmp_path = destination.with_suffix(".tmp")
        tmp_path.write_bytes(response.content)
        detected = imghdr.what(tmp_path)
        if detected not in ALLOWED_IMAGE_TYPES:
            print(f"   ⚠️  Unsupported image type: {detected or 'unknown'}")
            tmp_path.unlink(missing_ok=True)
            return None
        final_path = destination.with_suffix(".jpg" if detected == "jpeg" else f".{detected}")
        tmp_path.replace(final_path)
        return final_path

    def fetch(self, institution: str, category: str) -> Path | None:
        target_dir = self.category_dirs.get(category)
        if not target_dir:
            raise ValueError(f"Unsupported category '{category}'")
        filename = sanitize_filename(institution)
        existing = list(target_dir.glob(f"{filename}.*"))
        if existing:
            return existing[0]
        queries = self.build_queries(institution, category)
        for query in queries:
            print(f"   🔍 Searching: {query}")
            try:
                results = self.search_images(query)
            except RuntimeError as exc:
                print(f"   ⚠️  {exc}")
                continue
            scored_results = []
            for result in results:
                if not result:
                    continue
                score = self._compute_result_score(result)
                scored_results.append((score, result))
            scored_results.sort(key=lambda item: item[0], reverse=True)
            for idx, (score, result) in enumerate(scored_results, start=1):
                image_url = (
                    result.get("original")
                    or result.get("link")
                    or result.get("thumbnail")
                )
                if not image_url:
                    continue
                if self._looks_like_text_panel(result):
                    print("   ⚠️  Skipping document/screenshot style result.")
                    continue
                if not self._is_resolution_sufficient(result):
                    print(
                        f"   ⚠️  Skipping low-resolution result "
                        f"({result.get('original_width')}x{result.get('original_height')})"
                    )
                    continue
                source_text = " ".join(
                    filter(
                        None,
                        [
                            result.get("source"),
                            result.get("displayed_link"),
                            result.get("link"),
                            image_url,
                        ],
                    )
                )
                if self._contains_disallowed_source(source_text):
                    print(f"   ⚠️  Skipping stock provider: {source_text}")
                    continue
                print(f"   📸 Trying result #{idx}: {image_url}")
                destination = target_dir / filename
                saved_path = self.download_image(image_url, destination)
                if saved_path:
                    print(f"   ✅ Saved {saved_path}")
                    return saved_path
            print(f"   ⚠️  No usable results for query '{query}'")
        print(f"   ❌ Failed to download image for {institution}")
        return None


def prepare_output_paths(base_dir: Path, stem: str) -> tuple[Path, Path]:
    asset_dir = base_dir / f"{stem}_institutions_images"
    manifest_path = base_dir / f"{stem}_institutions_images.json"
    asset_dir.mkdir(parents=True, exist_ok=True)
    return asset_dir, manifest_path


def write_manifest(manifest_path: Path, records: Iterable[Mapping[str, object]]) -> None:
    data = {"entries": list(records)}
    manifest_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    args = parse_args()
    csv_path = args.input_csv if args.input_csv else find_latest_comparison_csv(COMPARER_OUTPUT_DIR)
    output_base = args.output_dir if args.output_dir else OUTPUT_DIR
    header, rows = load_csv(csv_path)
    header_map = build_header_map(header)
    institutions = collect_institutions(rows, header_map)
    asset_dir, manifest_path = prepare_output_paths(output_base, csv_path.stem)
    if not institutions:
        print("No institutions detected in the CSV. Writing empty manifest.")
        write_manifest(manifest_path, [])
        print("\nSummary")
        print("=" * 40)
        print(f"Source CSV    : {csv_path}")
        print(f"Institutions   : 0")
        print(f"Output dir    : {asset_dir}")
        print(f"Manifest      : {manifest_path}")
        return
    fetcher: ImageFetcher | None = None
    serpapi_error: str | None = None
    try:
        api_key = resolve_serpapi_key(args.serpapi_key)
    except RuntimeError as exc:
        serpapi_error = str(exc)
    else:
        try:
            fetcher = ImageFetcher(
                api_key,
                asset_dir,
                args.max_results,
                args.min_file_size,
                args.min_dimension,
            )
        except RuntimeError as exc:
            serpapi_error = str(exc)
    if fetcher is None:
        if serpapi_error:
            print(f"⚠️  {serpapi_error}")
        print("⚠️  Continuing without image downloads; entries will have empty image paths.")
    records: List[Dict[str, object]] = []
    for entry in sorted(institutions.values(), key=lambda item: (item.category, item.text.lower())):
        print(f"\n📂 {entry.category.upper()} — {entry.text}")
        if fetcher is None:
            print("   ⚠️  Skipping download; SerpAPI unavailable.")
            image_path = None
        else:
            image_path = fetcher.fetch(entry.text, entry.category)
            if not image_path:
                print("   ➖ No usable image found; continuing without an asset.")
        records.append(
            {
                "institution": entry.text,
                "category": entry.category,
                "occurrences": entry.count,
                "rows": sorted(entry.rows),
                "image_path": str(image_path) if image_path else None,
            }
        )
    write_manifest(manifest_path, records)
    print("\nSummary")
    print("=" * 40)
    print(f"Source CSV    : {csv_path}")
    print(f"Institutions   : {len(records)}")
    print(f"Output dir    : {asset_dir}")
    print(f"Manifest      : {manifest_path}")


if __name__ == "__main__":
    main()
