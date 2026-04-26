#!/usr/bin/env python3
"""Build animated 3D map zoom videos for locations detected by the comparser."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import shlex
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple

import requests

try:  # Playwright is optional at import time for friendlier error messages
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
except ImportError:  # pragma: no cover - Playwright may not be installed yet
    PlaywrightError = RuntimeError  # type: ignore[assignment]
    sync_playwright = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE_BASE = PROJECT_ROOT.parent
COMPARER_OUTPUT_DIR = CODE_BASE / "Comparser" / "output"
OUTPUT_DIR = PROJECT_ROOT / "output"
TEMPLATE_PATH = PROJECT_ROOT / "program" / "templates" / "maplibre_3d_renderer.html"
COUNTRY_META = PROJECT_ROOT / "asset" / "country-flags" / "countries.json"
MAPTILER_KEY_PLACEHOLDER = "{MAPTILER_KEY}"
MAPTILER_KEY_ENV = "MAPTILER_API_KEY"
MAPTILER_DEFAULT_KEY = "2Vr02EIN6UiJhZFeqhbM"
DEFAULT_STYLE_URL = "https://api.maptiler.com/maps/satellite-v4/style.json?key={MAPTILER_KEY}"
DEFAULT_TERRAIN_URL = "https://api.maptiler.com/tiles/terrain-rgb-v2/tiles.json?key={MAPTILER_KEY}"
DEFAULT_BOUNDARIES_URL = "https://api.maptiler.com/tiles/v3/tiles.json?key={MAPTILER_KEY}"
MAPTILER_KEY_FILE = PROJECT_ROOT / ".maptiler_key"
DEFAULT_DURATION_MS = 12000
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_FPS = 30
GEOCODE_CACHE = OUTPUT_DIR / "3d_location_geocode_cache.json"
FORMAT_EXTENSIONS = {
    "webm-vp9": "webm",
    "webm-vp8": "webm",
    "mp4": "mp4",
}
PREFERRED_PLACE_TYPES = (
    "city",
    "town",
    "village",
    "suburb",
    "hamlet",
    "municipality",
    "locality",
    "neighbourhood",
    "quarter",
)
ADMIN_KEYWORDS = ("province", "region", "district", "prefecture", "governorate", "state", "county", "department")
PLAYWRIGHT_MANUAL_INSTALL_ARGS = ("-m", "playwright", "install", "chromium", "--no-shell")
PLAYWRIGHT_REPAIR_ARGS = ("-m", "playwright", "install", "chromium", "--force", "--no-shell")
PLAYWRIGHT_REPAIRABLE_ERROR_MARKERS = (
    "chromium executable path is unavailable",
    "chromium executable not found",
    "chromium executable is not runnable",
    "executable doesn't exist",
    "please run the following command to download new browsers",
    "chromium distribution 'chromium' is not found",
)


@dataclass
class LocationEntry:
    name: str
    entry_type: str
    rows: set[int] = field(default_factory=set)
    count: int = 0
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    geocode_source: Optional[str] = None

    def add_row(self, index: int) -> None:
        self.rows.add(index)
        self.count += 1

    def apply_geocode(self, latitude: float, longitude: float, source: str) -> None:
        self.latitude = latitude
        self.longitude = longitude
        self.geocode_source = source


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate MapLibre based zoom clips for each unique location inside the latest "
            "universal comparser *_comparison*.csv table."
        )
    )
    parser.add_argument(
        "--input-csv",
        type=Path,
        help="Path to a *_comparison*.csv file (defaults to the newest file under Comparser/output).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Override the output directory (defaults to output/<stem>_3d_locations).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Only render the first N unique locations (sorted by frequency).",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=DEFAULT_DURATION_MS,
        help=f"Animation duration in milliseconds (default: {DEFAULT_DURATION_MS}).",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=DEFAULT_FPS,
        help=f"Video frame rate (default: {DEFAULT_FPS}).",
    )
    parser.add_argument(
        "--resolution",
        type=str,
        default=f"{DEFAULT_WIDTH}x{DEFAULT_HEIGHT}",
        help="Video resolution as WIDTHxHEIGHT (default: 1920x1080).",
    )
    parser.add_argument(
        "--animation",
        default="straight-zoom",
        help="Animation preset (MapLibre key or 'straight-zoom' for a simple globe-in zoom).",
    )
    parser.add_argument(
        "--format",
        choices=sorted(FORMAT_EXTENSIONS),
        default="webm-vp9",
        help="Video format handled by maplibre-gl-video-export (default: webm-vp9).",
    )
    parser.add_argument(
        "--bitrate",
        type=int,
        default=9000,
        help="Target video bitrate in kbps (default: 9000).",
    )
    parser.add_argument(
        "--style-url",
        default=DEFAULT_STYLE_URL,
        help=f"MapLibre style URL to use (default: {DEFAULT_STYLE_URL}).",
    )
    parser.add_argument(
        "--terrain-url",
        default=DEFAULT_TERRAIN_URL,
        help="Optional raster-dem source for terrain exaggeration (defaults to MapTiler terrain DEM when MAPTILER key is provided).",
    )
    parser.add_argument(
        "--boundaries-url",
        default=DEFAULT_BOUNDARIES_URL,
        help="Vector tile source for political boundaries (defaults to MapTiler OpenMapTiles when a MAPTILER key is provided).",
    )
    parser.add_argument(
        "--city-zoom",
        type=float,
        default=10.5,
        help="Zoom level when focusing on cities (default: 10.5).",
    )
    parser.add_argument(
        "--country-zoom",
        type=float,
        default=5.0,
        help="Zoom level when focusing on countries (default: 5.0).",
    )
    parser.add_argument(
        "--pitch",
        type=float,
        default=12.0,
        help="Camera pitch in degrees when animation starts (default: 12).",
    )
    parser.add_argument(
        "--bearing",
        type=float,
        default=0.0,
        help="Initial camera bearing in degrees (default: 0).",
    )
    parser.add_argument(
        "--render-timeout",
        type=float,
        default=150.0,
        help="Timeout per render in seconds (default: 150).",
    )
    parser.add_argument(
        "--geocode-cache",
        type=Path,
        default=GEOCODE_CACHE,
        help="Path to geocode cache JSON file.",
    )
    parser.add_argument(
        "--globe",
        action="store_true",
        help="Use the globe projection for the initial camera pose.",
    )
    parser.add_argument(
        "--flat",
        dest="globe",
        action="store_false",
        help="Force a flat Mercator projection instead of the globe view.",
    )
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Disable headless Chromium (useful for debugging the renderer).",
    )
    parser.add_argument(
        "--geocode-delay",
        type=float,
        default=1.2,
        help="Delay between Nominatim requests to stay under usage limits (default: 1.2s).",
    )
    parser.add_argument(
        "--use-geocode-cache",
        dest="refresh_geocode",
        action="store_false",
        help="Reuse cached coordinates instead of refreshing them.",
    )
    parser.set_defaults(refresh_geocode=True)
    parser.add_argument(
        "--start-delay",
        type=float,
        default=1.0,
        help="Delay (seconds) between map load and the record trigger.",
    )
    parser.add_argument(
        "--start-zoom",
        type=float,
        default=3.4,
        help="Initial zoom level before the animation begins (default: 3.4).",
    )
    parser.add_argument(
        "--start-pitch",
        type=float,
        default=12.0,
        help="Initial pitch before animation begins (default: 12).",
    )
    parser.add_argument(
        "--start-bearing",
        type=float,
        default=0.0,
        help="Initial bearing before animation begins (default: 0).",
    )
    parser.add_argument(
        "--terrain-exaggeration",
        type=float,
        default=1.2,
        help="Terrain vertical exaggeration when DEM tiles are enabled (default: 1.2).",
    )
    parser.add_argument(
        "--disable-hillshade",
        action="store_true",
        help="Skip adding the hillshade overlay when terrain is active.",
    )
    parser.add_argument(
        "--maptiler-key",
        default=None,
        help=(
            f"API key used to replace {MAPTILER_KEY_PLACEHOLDER} in the default MapTiler "
            f"style/terrain URLs (defaults to the {MAPTILER_KEY_ENV} environment variable)."
        ),
    )
    parser.add_argument(
        "--audio-track",
        type=Path,
        default=None,
        help=(
            "Optional path to an audio file (MP3, WAV, …) to mux into every rendered video. "
            "The audio is trimmed or looped to match the video duration."
        ),
    )
    parser.set_defaults(globe=False)
    return parser.parse_args()


def resolve_maptiler_placeholder(value: Optional[str], key: Optional[str], label: str) -> Optional[str]:
    """Replace the MapTiler placeholder in a URL if present."""
    if not value or MAPTILER_KEY_PLACEHOLDER not in value:
        return value
    if not key:
        raise SystemExit(
            f"{label} expects a MapTiler API key. Provide one via --maptiler-key, "
            f"the {MAPTILER_KEY_ENV} environment variable, or store it in {MAPTILER_KEY_FILE}."
        )
    return value.replace(MAPTILER_KEY_PLACEHOLDER, key)


def load_maptiler_key_from_file() -> Optional[str]:
    if MAPTILER_KEY_FILE.exists():
        try:
            return MAPTILER_KEY_FILE.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


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
        except StopIteration as exc:  # pragma: no cover - empty CSV
            raise RuntimeError(f"{path} is empty") from exc
        rows = [list(row) for row in reader]
    return header, rows


def build_header_map(header: Sequence[str]) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    for idx, column in enumerate(header):
        mapping[column.strip().lower()] = idx
    return mapping


def require_column(header_map: Mapping[str, int], column_name: str) -> int:
    key = column_name.strip().lower()
    if key not in header_map:
        raise KeyError(f"Column '{column_name}' missing from CSV.")
    return header_map[key]


def split_locations(value: str | None) -> List[str]:
    if not value:
        return []
    fragments = value.split("|")
    cleaned: List[str] = []
    for fragment in fragments:
        item = fragment.strip().strip('"').strip()
        if item:
            cleaned.append(item)
    return cleaned


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"\s+", " ", text).strip()


def normalized_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    without_marks = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    lowered = without_marks.lower()
    lowered = re.sub(r"[^a-z0-9]+", "_", lowered)
    return lowered.strip("_") or lowered


def load_country_index(path: Path) -> Dict[str, str]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    lookup: Dict[str, str] = {}
    for iso_code, name in data.items():
        lookup[normalized_key(name)] = iso_code.lower()
        lookup[normalized_key(iso_code)] = iso_code.lower()
    return lookup


COUNTRY_INDEX = load_country_index(COUNTRY_META)


def classify_location(name: str) -> str:
    key = normalized_key(name)
    return "country" if key in COUNTRY_INDEX else "city"


def collect_locations(rows: Sequence[Sequence[str]], header_map: Mapping[str, int]) -> Dict[str, LocationEntry]:
    location_idx = require_column(header_map, "Location Mention")
    manifest: Dict[str, LocationEntry] = {}
    for line_idx, row in enumerate(rows, start=1):
        if location_idx >= len(row):
            continue
        for raw in split_locations(row[location_idx]):
            normalized = normalize_text(raw)
            if not normalized:
                continue
            key = normalized_key(normalized)
            entry = manifest.get(key)
            if not entry:
                entry = LocationEntry(name=normalized, entry_type=classify_location(normalized))
                manifest[key] = entry
            entry.add_row(line_idx)
    return manifest


def parse_resolution(value: str) -> tuple[int, int]:
    match = re.match(r"^\s*(\d+)[xX](\d+)\s*$", value)
    if not match:
        raise ValueError(f"Resolution must be WIDTHxHEIGHT, got '{value}'.")
    width = int(match.group(1))
    height = int(match.group(2))
    return width, height


class GeocodeCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: Dict[str, Dict[str, Any]] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.data = {}

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        return self.data.get(key)

    def set(self, key: str, value: Dict[str, Any]) -> None:
        self.data[key] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")


class NominatimGeocoder:
    def __init__(
        self,
        cache: GeocodeCache,
        delay: float = 1.2,
        endpoint: str = "https://nominatim.openstreetmap.org/search",
        refresh: bool = False,
    ) -> None:
        self.cache = cache
        self.delay = delay
        self.endpoint = endpoint
        self.refresh = refresh
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "insert_creator/3d_location (maplibre automation)",
                "Accept": "application/json",
            }
        )
        self._last_request = 0.0

    def geocode(self, name: str, entry_type: str) -> Dict[str, Any]:
        key = f"{entry_type}:{normalized_key(name)}"
        cached = None if self.refresh else self.cache.get(key)
        if cached:
            if cached.get("ok"):
                return cached
            raise RuntimeError(cached.get("error") or f"Previous geocode failed for '{name}'.")
        now = time.time()
        since_last = now - self._last_request
        if since_last < self.delay:
            time.sleep(self.delay - since_last)
        params = {
            "format": "json",
            "limit": 5 if entry_type == "city" else 1,
            "q": name,
            "accept-language": "en",
            "addressdetails": 1,
        }
        if entry_type == "country":
            params["featuretype"] = "country"
        response = self.session.get(self.endpoint, params=params, timeout=30)
        self._last_request = time.time()
        if response.status_code >= 400:
            message = f"Nominatim error {response.status_code}: {response.text[:200]}"
            self.cache.set(key, {"ok": False, "error": message})
            self.cache.save()
            raise RuntimeError(message)
        payload: List[MutableMapping[str, Any]] = response.json()
        if not payload:
            message = f"No geocode result for '{name}'."
            self.cache.set(key, {"ok": False, "error": message})
            self.cache.save()
            raise RuntimeError(message)
        candidate = self._choose_candidate(entry_type, name, payload)
        first = candidate or payload[0]
        try:
            latitude = float(first["lat"])
            longitude = float(first["lon"])
        except (KeyError, ValueError) as exc:
            message = f"Malformed geocode payload for '{name}': {first}"
            self.cache.set(key, {"ok": False, "error": message})
            self.cache.save()
            raise RuntimeError(message) from exc
        record = {
            "ok": True,
            "latitude": latitude,
            "longitude": longitude,
            "display_name": first.get("display_name"),
            "source": "nominatim",
        }
        self.cache.set(key, record)
        self.cache.save()
        return record

    def _candidate_score(self, entry_type: str, name: str, candidate: Mapping[str, Any]) -> float:
        score = float(candidate.get("importance") or 0.0)
        class_name = str(candidate.get("class") or "")
        place_type = str(candidate.get("type") or "")
        display_lower = str(candidate.get("display_name") or "").lower()
        name_lower = name.lower()
        if entry_type == "city":
            if class_name == "place":
                score += 2.5
            if place_type in PREFERRED_PLACE_TYPES:
                score += 3.0
            elif place_type in ("city_district", "borough"):
                score += 1.0
            if class_name == "boundary":
                score -= 1.5
            if any(term in display_lower for term in ADMIN_KEYWORDS):
                score -= 1.0
            if name_lower in display_lower:
                score += 0.5
        else:
            if class_name == "boundary" and place_type == "administrative":
                score += 1.0
        return score

    def _choose_candidate(
        self, entry_type: str, name: str, candidates: Sequence[Mapping[str, Any]]
    ) -> Optional[Mapping[str, Any]]:
        best: Optional[Mapping[str, Any]] = None
        best_score = float("-inf")
        for candidate in candidates:
            if not candidate.get("lat") or not candidate.get("lon"):
                continue
            score = self._candidate_score(entry_type, name, candidate)
            if score > best_score:
                best_score = score
                best = candidate
        return best


class MapLibreRenderer:
    def __init__(self, template_path: Path, headless: bool = True) -> None:
        if sync_playwright is None:  # pragma: no cover - optional dep
            raise RuntimeError(
                "Playwright is required for rendering. Install it via "
                f"`pip install playwright` followed by `{manual_playwright_install_command()}`."
            )
        if not template_path.exists():
            raise FileNotFoundError(f"Template HTML not found: {template_path}")
        self.template_url = template_path.as_uri()
        self.headless = headless
        self._playwright: Any = None
        self._browser: Any = None
        self._repair_attempted = False

    def __enter__(self) -> "MapLibreRenderer":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def start(self) -> None:
        if self._browser:
            return
        while True:
            try:
                self._start_browser()
                return
            except RuntimeError as exc:
                self.stop()
                if self._repair_attempted or not self._is_repairable_bootstrap_error(exc):
                    raise
                self._repair_attempted = True
                self._repair_chromium_install(exc)

    def stop(self) -> None:
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None

    def _start_browser(self) -> None:
        self._playwright = sync_playwright().start()
        browser_type = self._playwright.chromium
        self._preflight_chromium_executable(browser_type.executable_path)
        args = [
            "--autoplay-policy=no-user-gesture-required",
            "--disable-web-security",
            "--allow-file-access-from-files",
            "--use-fake-ui-for-media-stream",
        ]
        try:
            self._browser = browser_type.launch(
                channel="chromium",
                headless=self.headless,
                args=args,
            )
        except PlaywrightError as exc:  # pragma: no cover - runtime specific
            raise RuntimeError(f"Playwright bootstrap failed: {exc}") from exc

    def _preflight_chromium_executable(self, executable_path: str) -> None:
        command = manual_playwright_install_command()
        if not executable_path:
            raise RuntimeError(
                "Chromium executable path is unavailable. "
                f"Run `{command}` to install the Playwright browser bundle."
            )
        executable = Path(executable_path)
        if not executable.exists():
            raise RuntimeError(
                f"Chromium executable not found at {executable}. "
                f"Run `{command}` to install the Playwright browser bundle."
            )
        if not executable.is_file() or executable.stat().st_size <= 0 or not os.access(executable, os.X_OK):
            raise RuntimeError(
                f"Chromium executable is not runnable at {executable}. "
                f"Run `{command}` to repair the Playwright browser bundle."
            )

    def _is_repairable_bootstrap_error(self, exc: BaseException) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in PLAYWRIGHT_REPAIRABLE_ERROR_MARKERS)

    def _repair_chromium_install(self, exc: BaseException) -> None:
        command = [sys.executable, *PLAYWRIGHT_REPAIR_ARGS]
        print(f"  ⚠️  Playwright bootstrap issue detected: {exc}")
        print(f"  ↺ Repairing Playwright Chromium via: {format_shell_command(command)}")
        result = self._run_install_command(command)
        if result.returncode == 0:
            return
        output = (result.stderr or result.stdout or "").strip()
        if len(output) > 1200:
            output = output[-1200:]
        detail = f"\n{output}" if output else ""
        raise RuntimeError(
            "Playwright Chromium auto-repair failed "
            f"(exit {result.returncode}) while handling: {exc}.{detail}"
        )

    def _run_install_command(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, capture_output=True, text=True)

    def render(self, config: Mapping[str, Any], timeout: float = 120.0) -> Dict[str, Any]:
        if not self._browser:
            raise RuntimeError("Renderer not started.")
        page = self._browser.new_page(
            viewport={"width": config.get("width", DEFAULT_WIDTH), "height": config.get("height", DEFAULT_HEIGHT)},
            no_viewport=False,
        )
        try:
            page.goto(self.template_url, wait_until="load")
            page.evaluate(
                """(config) => {
                    window.__AUTO_CONFIG = config;
                }""",
                config,
            )
            page.evaluate("window.renderLocationVideo(window.__AUTO_CONFIG);")
            page.wait_for_function(
                "window.__videoResult !== null || window.__videoError !== null",
                timeout=timeout * 1000,
            )
            error = page.evaluate("window.__videoError")
            if error:
                raise RuntimeError(str(error))
            result = page.evaluate("window.__videoResult")
            if not isinstance(result, dict) or "base64" not in result:
                raise RuntimeError("Renderer returned an unexpected payload.")
            return result
        except PlaywrightError as exc:  # pragma: no cover - runtime specific
            raise RuntimeError(f"Playwright error: {exc}") from exc
        finally:
            page.close()


def sanitize_filename(value: str) -> str:
    cleaned = normalized_key(value)
    return cleaned or "location"


def format_shell_command(parts: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def manual_playwright_install_command() -> str:
    return format_shell_command([Path(sys.executable).name, *PLAYWRIGHT_MANUAL_INSTALL_ARGS])


def ensure_output_dirs(csv_path: Path, override: Optional[Path]) -> tuple[Path, Path, Path]:
    stem = csv_path.stem
    base_output = override if override else OUTPUT_DIR / f"{stem}_3d_locations"
    video_dir = base_output / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = base_output / f"{stem}_3d_location_manifest.json"
    return base_output, video_dir, manifest_path


def write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def prepare_render_config(
    entry: LocationEntry,
    width: int,
    height: int,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    zoom = args.city_zoom if entry.entry_type == "city" else args.country_zoom
    config: Dict[str, Any] = {
        "latitude": entry.latitude,
        "longitude": entry.longitude,
        "locationName": entry.name,
        "zoom": zoom,
        "pitch": args.pitch,
        "bearing": args.bearing,
        "startZoom": args.start_zoom,
        "startPitch": args.start_pitch,
        "startBearing": args.start_bearing,
        "terrainExaggeration": args.terrain_exaggeration,
        "hillshade": not args.disable_hillshade,
        "duration": args.duration,
        "fps": args.fps,
        "width": width,
        "height": height,
        "animation": args.animation,
        "format": args.format,
        "bitrate": args.bitrate,
        "styleUrl": args.style_url,
        "terrainSourceUrl": args.terrain_url,
        "boundariesSourceUrl": args.boundaries_url,
        "globe": args.globe,
        "startDelay": max(args.start_delay, 0.2) * 1000,
    }
    return config


def add_audio_track(video_path: Path, audio_path: Path, duration_ms: int) -> None:
    """Mux *audio_path* into *video_path* in-place, trimmed to *duration_ms* milliseconds.

    WebM containers require Opus audio; MP4 containers use AAC.
    The original video is replaced atomically (temp file → rename).
    Raises RuntimeError if ffmpeg is not on PATH or returns an error.
    """
    duration_sec = duration_ms / 1000.0
    suffix = video_path.suffix.lower()
    # WebM only supports Opus/Vorbis — AAC would silently produce a broken file
    audio_codec = "libopus" if suffix == ".webm" else "aac"
    tmp_path = video_path.with_suffix(".audio_tmp" + video_path.suffix)
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(video_path),
        "-stream_loop", "-1",      # loop audio if shorter than video
        "-i", str(audio_path),
        "-map", "0:v:0",           # video stream from first input
        "-map", "1:a:0",           # audio stream from second input (looped)
        "-t", str(duration_sec),   # trim output to exact video length
        "-c:v", "copy",            # no re-encode of video
        "-c:a", audio_codec,
        "-b:a", "192k",
        "-shortest",
        str(tmp_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"ffmpeg failed (exit {result.returncode}):\n{result.stderr[-1000:]}"
        )
    tmp_path.replace(video_path)


def main() -> None:
    args = parse_args()
    maptiler_key = (
        args.maptiler_key
        or os.environ.get(MAPTILER_KEY_ENV)
        or load_maptiler_key_from_file()
        or MAPTILER_DEFAULT_KEY
    )
    args.style_url = resolve_maptiler_placeholder(args.style_url, maptiler_key, "--style-url")
    args.terrain_url = resolve_maptiler_placeholder(args.terrain_url, maptiler_key, "--terrain-url")
    args.boundaries_url = resolve_maptiler_placeholder(args.boundaries_url, maptiler_key, "--boundaries-url")
    csv_path = args.input_csv or find_latest_comparison_csv(COMPARER_OUTPUT_DIR)
    width, height = parse_resolution(args.resolution)
    header, rows = load_csv(csv_path)
    header_map = build_header_map(header)
    _, video_dir, manifest_path = ensure_output_dirs(csv_path, args.output_dir)
    manifest: Dict[str, Any] = {
        "source_csv": str(csv_path),
        "template": str(TEMPLATE_PATH),
        "videos": [],
    }
    locations = collect_locations(rows, header_map)
    if not locations:
        manifest["summary"] = {"success": 0, "failed": 0, "total": 0}
        write_manifest(manifest_path, manifest)
        print(f"No locations found in the CSV; skipping 3D location rendering. Manifest: {manifest_path}")
        return
    entries = sorted(locations.values(), key=lambda item: (-item.count, item.name))
    if args.limit:
        entries = entries[: args.limit]
    cache = GeocodeCache(args.geocode_cache)
    geocoder = NominatimGeocoder(cache, delay=args.geocode_delay, refresh=args.refresh_geocode)
    extension = FORMAT_EXTENSIONS.get(args.format, "webm")
    successes = 0
    failures = 0
    renderer = MapLibreRenderer(TEMPLATE_PATH, headless=not args.show_browser)
    try:
        renderer.start()
    except RuntimeError as exc:
        manifest["bootstrap_error"] = str(exc)
        manifest["summary"] = {"success": 0, "failed": len(entries), "total": len(entries)}
        write_manifest(manifest_path, manifest)
        print(f"\n⚠️  Renderer bootstrap failed: {exc}")
        print(f"    Wrote manifest with bootstrap error: {manifest_path}")
        return
    try:
        for index, entry in enumerate(entries, start=1):
            print(f"[{index}/{len(entries)}] Resolving {entry.name} ({entry.entry_type})…")
            try:
                geocode = geocoder.geocode(entry.name, entry.entry_type)
                entry.apply_geocode(geocode["latitude"], geocode["longitude"], str(geocode.get("source") or "nominatim"))
            except RuntimeError as exc:
                print(f"  ⚠️  Geocode failed: {exc}")
                failures += 1
                manifest["videos"].append(
                    {
                        "name": entry.name,
                        "type": entry.entry_type,
                        "rows": sorted(entry.rows),
                        "success": False,
                        "error": str(exc),
                    }
                )
                continue
            config = prepare_render_config(entry, width, height, args)
            video_name = f"{entry.entry_type}_{index:03d}_{sanitize_filename(entry.name)}.{extension}"
            output_path = video_dir / video_name
            try:
                result = renderer.render(config, timeout=args.render_timeout)
                base64_payload = result.get("base64")
                if not isinstance(base64_payload, str):
                    raise RuntimeError("Renderer did not return a valid base64 payload.")
                video_bytes = base64.b64decode(base64_payload)
                output_path.write_bytes(video_bytes)
                audio_ok = False
                if args.audio_track:
                    audio_path = Path(args.audio_track)
                    if audio_path.exists():
                        try:
                            add_audio_track(output_path, audio_path, args.duration)
                            audio_ok = True
                            print(f"  🔊 Audio track added.")
                        except RuntimeError as audio_exc:
                            print(f"  ⚠️  Audio mux failed: {audio_exc}")
                    else:
                        print(f"  ⚠️  Audio file not found: {audio_path}")
                successes += 1
                manifest["videos"].append(
                    {
                        "name": entry.name,
                        "type": entry.entry_type,
                        "rows": sorted(entry.rows),
                        "latitude": entry.latitude,
                        "longitude": entry.longitude,
                        "geocode_source": entry.geocode_source,
                        "video_path": str(output_path),
                        "success": True,
                        "audio_track": str(args.audio_track) if audio_ok else None,
                        "mime_type": result.get("mimeType"),
                        "frames": result.get("frames"),
                    }
                )
                print(f"  ✅ Wrote {output_path}")
            except Exception as exc:  # noqa: BLE001 - surfaces to caller via manifest
                failures += 1
                print(f"  ❌ Renderer failed: {exc}")
                manifest["videos"].append(
                    {
                        "name": entry.name,
                        "type": entry.entry_type,
                        "rows": sorted(entry.rows),
                        "latitude": entry.latitude,
                        "longitude": entry.longitude,
                        "geocode_source": entry.geocode_source,
                        "success": False,
                        "error": str(exc),
                    }
                )
    finally:
        renderer.stop()
    manifest["summary"] = {"success": successes, "failed": failures, "total": len(entries)}
    write_manifest(manifest_path, manifest)
    print(f"\nDone. {successes} succeeded, {failures} failed. Manifest: {manifest_path}")


if __name__ == "__main__":  # pragma: no cover
    main()
