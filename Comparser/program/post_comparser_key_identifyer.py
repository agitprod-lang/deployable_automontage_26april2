#!/usr/bin/env python3
"""Step 2: Enrich a basic 8-column comparison CSV with CTA/Zoom tags, semantic annotations,
and HTML metrics → fully enriched comparison CSV.

Standalone program — copies all required logic from:
  - universal_comparser_comment_subcribe_tipee_intro_zoom.py  (CTA/Zoom tagging)
  - universal_comparser_nouns_geo_quotes_numbers_titles.py    (nouns/geo/quotes/numbers/titles)
  - groq_html_comparser.py                                    (HTML metrics + manifest)
  - comparser.py                                              (collect_reference_text)
No imports from other project files.
"""

from __future__ import annotations

import argparse
import csv
import html as html_module
import json
import os
import re
import textwrap
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlparse

try:
    import anthropic  # type: ignore
except ImportError:
    anthropic = None

try:
    from bs4 import BeautifulSoup  # type: ignore
except ImportError:
    BeautifulSoup = None

# ---------------------------------------------------------------------------
# Output paths
# ---------------------------------------------------------------------------
FINAL_OUTPUT_DIR = Path("~/Desktop/code/deployable_auto-montage/Comparser/output/second_comparser_output")
COUNTRY_META = Path("~/Desktop/code/deployable_auto-montage/insert_creator/asset/country-flags/countries.json")
SUMMARY_SUFFIX = "_groq_html_summary.json"

# ---------------------------------------------------------------------------
# CTA/Zoom constants
# ---------------------------------------------------------------------------
DEFAULT_CTA_MODEL = "claude-sonnet-4-5-20250929"
FRAME_RATE = 30.0
ZOOM_VALUES = {"", "Z", "Z1", "Z2", "Z3"}
TAG_SPECS = [
    ("Commentez Tag", "commentez", "Commentez"),
    ("Tippee Tag", "tippee", "Tippee"),
    ("Abonnez Tag", "abonnez", "Abonnez"),
]

CLAUDE_CTA_SYSTEM = textwrap.dedent(
    """\
    You help label transcript rows that mention community calls to action.
    Mark a row when either the transcript text OR the reference text clearly contains these ideas:
    * commentez: telling viewers to comment.
    * tippee: asking for donations or support.
    * abonnez: telling people to subscribe.
    Additionally, flag the `zoom` level for important rows:
    * Use "Z" (or "Z1") when a single zoom (~150%) would highlight a key concept or CTA.
    * Use "Z2" when the host enumerates several key points that deserve extra emphasis (~200%).
    * Use "Z3" for very dense or mission-critical enumerations (~250%). Never go above Z3.
    * Leave `zoom` empty / omit it when no highlight is needed.
    * Avoid recommending zooms during the final 10 seconds of the video.
    Respond ONLY with JSON so the caller can parse it.
    """
)
CLAUDE_CTA_PROMPT = textwrap.dedent(
    """\
    The `rows` array below lists transcript alignment rows. Each item contains:
      - index: zero-based position in the CSV (header excluded)
      - transcript_number, status, transcript_text, reference_text, start_time, end_time

    Output JSON shaped exactly like:
    {{
      "rows": [
        {{"index": 13, "tags": ["commentez", "tippee"], "zoom": "Z2"}}
      ],
      "summary": "<optional short note>"
    }}

    Never invent new tag names. Use only: commentez, tippee, abonnez.
    `zoom` accepts only: "", "Z", "Z1", "Z2", "Z3".
    Rows:
    {payload}
    """
)

# ---------------------------------------------------------------------------
# Nouns/semantic constants
# ---------------------------------------------------------------------------
DEFAULT_NOUNS_MODEL = "claude-sonnet-4-5-20250929"
FEATURE_SPECS: Sequence[Tuple[str, str]] = (
    ("person", "Person Mention"),
    ("location", "Location Mention"),
    ("gov_institution", "Gov Institution"),
    ("brand", "Brand Mention"),
    ("quote", "Quote Extracted"),
    ("money", "Money Mention"),
    ("number", "Number Mention"),
    ("date", "Date Mention"),
    ("book", "Book Mention"),
    ("keyword", "Key Words"),
    ("feeling", "Feeling and Emotion"),
)
KEEP_COLUMN_NAME = "Keep"
TAGS_RESTRICTED_TO_KEPT_ROWS: Sequence[str] = (
    "Commentez Tag", "Tippee Tag", "Abonnez Tag", "Zoom",
)
TITLE_COLUMN_NAME = "Titles"
RELEVANT_NEWS_COLUMN_NAME = "Relevant News"
START_TIME_COLUMN_NAME = "Start Time"
END_TIME_COLUMN_NAME = "End Time"
NEWS_ARTICLES_PER_TEN_MINUTES = 3
TEN_MINUTES_SECONDS = 600
NEWS_RATE_PER_SECOND = NEWS_ARTICLES_PER_TEN_MINUTES / TEN_MINUTES_SECONDS
MIN_DURATION_SECONDS = 4.0
DEFAULT_ESTIMATED_DURATION = 8.0
MAX_NEWS_PER_ROW = 3
FALLBACK_DATE_LIMIT = 3
NEWS_SYSTEM_PROMPT = (
    "You are a senior newsroom researcher who only cites real, well-sourced articles from major outlets. "
    "Prefer February 2026 coverage or the latest available reporting that confirms the described facts."
)
TITLE_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
MIN_QUOTE_CONTENT_LEN = 8
MIN_QUOTE_WORD_COUNT = 3
QUOTE_PATTERNS: Tuple[str, ...] = (
    r'"([^"]+)"',
    r"(?<!\w)'([^']+)'(?!\w)",
    r"\u201c([^\u201d]+)\u201d",
    r"\u201e([^\u201c]+)\u201c",
    r"\u00ab([^\u00bb]+)\u00bb",
    r"\u2039([^\u203a]+)\u203a",
    r"\u201a([^\u2018]+)\u2018",
    r"\u2018([^\u2019]+)\u2019",
    r"<<([^>]+)>>",
)
WORD_TOKEN_PATTERN = re.compile(r"\b\w+\b", re.UNICODE)

NORMALIZATION_REPLACEMENTS: Tuple[Tuple[str, str], ...] = (
    ("\r", " "), ("\n", " "), ("\t", " "), ("\u00a0", " "), ("\u202f", " "),
    ("\u2018", "'"), ("\u2019", "'"), ("`", "'"),
    ("\u00ab", '"'), ("\u00bb", '"'), ("\u201c", '"'), ("\u201d", '"'),
    ("\u2010", "-"), ("\u2011", "-"), ("\u2013", "-"), ("\u2014", "-"),
)

LANGUAGE_PROFILES: Mapping[str, Mapping[str, object]] = {
    "fr": {
        "name": "French",
        "keywords": [" le ", " la ", " les ", " des ", " est ", " pas ", " avec ", " pour ",
                     " que ", " qui ", " une ", " dans ", " aux ", " sur "],
        "characters": set("àâäçéèêëîïôöùûüÿœ«»"),
        "system": textwrap.dedent("""\
            Tu analyses le texte de référence complet (version normalisée).
            Tu dois relever ces éléments dans l'ordre d'apparition:
            - `person`: personnes ou groupes humains cités nommément.
            - `location`: villes ou pays explicitement cités.
            - `gov_institution`: institutions publiques ou officielles uniquement (ministères, agences gouvernementales, tribunaux, parlement, forces de l'ordre, ONU, OTAN, banques centrales, ONG officielles reconnues). Ne pas inclure les entreprises privées ni les marques commerciales.
            - `brand`: marques commerciales, entreprises privées, produits identifiés par un nom de marque (ex: Apple, Nike, Renault, Netflix, ChatGPT).
            - `quote`: citations complètes entourées de guillemets.
            - `number`: nombres (avec unité ou symbole %) hors dates.
            - `date`: dates (réécris-les en JJ/MM/AAAA quand possible).
            - `book`: titres d'ouvrages avec l'auteur si mentionné.
            - `keyword`: 3 à 6 mots ou expressions essentiels.
            - `feeling`: sentiments, émotions ou tonalités explicitement décrites.
            Chaque entrée doit contenir `text` et `start_index`. Réponds uniquement en JSON valide.
            """),
        "user": textwrap.dedent("""\
            Données attendues:
            {{
              "person": [{{"text": "...", "start_index": 123}}],
              "location": [], "gov_institution": [], "brand": [], "quote": [], "number": [],
              "date": [], "book": [], "keyword": [], "feeling": [],
              "notes": "<optionnel>"
            }}
            Texte normalisé:
            {reference_text}
            """),
    },
    "en": {
        "name": "English",
        "keywords": [" the ", " and ", " with ", " that ", " you ", " are ", " for ",
                     " from ", " this ", " have ", " which ", " were ", " will ", " into "],
        "characters": set(""),
        "system": textwrap.dedent("""\
            You analyze the full reference article (normalized version).
            Extract the following items in order of first appearance:
            - `person`: human names or clearly identified groups.
            - `location`: explicit cities or countries.
            - `gov_institution`: official/government bodies only (ministries, government agencies, courts, parliaments, police, UN, NATO, central banks, officially recognized NGOs). Do NOT include private companies or commercial brands.
            - `brand`: commercial brands, private companies, and named products (e.g. Apple, Nike, Netflix, ChatGPT, Tesla).
            - `quote`: full passages enclosed in quotes.
            - `number`: standalone numbers with units/% (exclude dates).
            - `date`: dates reformatted as DD/MM/YYYY when possible.
            - `book`: books or essays (include author when available).
            - `keyword`: 3-6 high-signal terms that summarize the topic.
            - `feeling`: emotions or affective tones explicitly described.
            Each entry must include `text` and `start_index`. Respond with valid JSON only.
            """),
        "user": textwrap.dedent("""\
            Expected JSON structure:
            {{
              "person": [{{"text": "...", "start_index": 123}}],
              "location": [], "gov_institution": [], "brand": [], "quote": [], "number": [],
              "date": [], "book": [], "keyword": [], "feeling": [],
              "notes": "<optional>"
            }}
            Normalized text:
            {reference_text}
            """),
    },
}

TARGETED_EXTRACTION_SPECS: Mapping[str, Mapping[str, Mapping[str, str]]] = {
    "feeling": {
        "fr": {
            "system": "Tu relis l'article normalisé. Relève toutes les expressions décrivant un état émotionnel explicite. Réponds strictement en JSON: {\"items\": [{\"text\": \"...\", \"start_index\": 123}]}.",
            "user": "Texte normalisé:\n{reference_text}",
        },
        "en": {
            "system": "Extract every explicit emotional state mentioned in the prose. Return only strings that appear verbatim. Respond with pure JSON: {\"items\": [{\"text\": \"...\", \"start_index\": 123}]}.",
            "user": "Normalized text:\n{reference_text}",
        },
    },
    "concrete": {
        "fr": {
            "system": "Détecte chaque mention d'un objet ou être tangible. Renvoie le texte exact en JSON {\"items\": [{\"text\": \"...\", \"start_index\": 123}]}.",
            "user": "Texte normalisé:\n{reference_text}",
        },
        "en": {
            "system": "Identify every tangible non-human, non-institution mention. Reply strictly with JSON {\"items\": [{\"text\": \"...\", \"start_index\": 123}]}.",
            "user": "Normalized text:\n{reference_text}",
        },
    },
}
TARGETED_MAX_TOKENS = 800

LANGUAGE_HOME_COUNTRIES: Mapping[str, Tuple[str, ...]] = {
    "fr": ("France", "Québec", "Quebec"),
    "en": ("England", "US", "U.S.", "United States", "USA"),
}
LANGUAGE_LOCATION_OVERRIDES: Mapping[str, Mapping[str, str]] = {
    "fr": {
        "Paris": "Paris, France", "Marseille": "Marseille, France", "Lyon": "Lyon, France",
        "Toulouse": "Toulouse, France", "Nice": "Nice, France", "Bordeaux": "Bordeaux, France",
        "Lille": "Lille, France", "Nantes": "Nantes, France", "Strasbourg": "Strasbourg, France",
        "Montpellier": "Montpellier, France",
    },
    "en": {
        "Washington": "Washington, D.C., United States", "New York": "New York City, United States",
        "London": "London, United Kingdom",
    },
}

SLASH_DATE_PATTERN = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
ISO_DATE_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
YEAR_PATTERN = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")
GROUPED_NUMBER_PATTERN = re.compile(
    r"\b\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d+)?(?:\s?(?:%|€|euros?|dollars?|millions?|milliards?|milliers?))?",
    re.IGNORECASE,
)
SIMPLE_NUMBER_PATTERN = re.compile(
    r"\b\d+(?:[.,]\d+)?(?:\s?(?:%|€|euros?|dollars?|millions?|milliards?|milliers?))?",
    re.IGNORECASE,
)
CURRENCY_SYMBOL_PATTERN = re.compile(r"[$€£¥₩₽₹₺₫₦₱₪₭₲₵₴฿₮₸]")
CURRENCY_KEYWORD_PATTERN = re.compile(
    r"\b(?:dollar|dollars|euro|euros|pound|pounds|yen|yuan|won|rupee|rupees|peso|pesos|real|reais|lira|franc|francs|bitcoin|btc)\b",
    re.IGNORECASE,
)

INSTITUTION_EXACT_PHRASES: Tuple[str, ...] = (
    "FBI", "CIA", "NSA", "ONU", "UNESCO", "UNICEF", "OMS", "OTAN", "FMI", "IMF",
    "Banque mondiale", "World Bank", "Union européenne", "European Union",
    "Assemblée nationale", "Sénat", "Maison Blanche", "White House",
    "Conseil d'État", "Conseil d'Etat",
)
INSTITUTION_EXACT_PATTERNS: Tuple[re.Pattern, ...] = tuple(
    re.compile(re.escape(phrase), re.IGNORECASE) for phrase in INSTITUTION_EXACT_PHRASES if phrase.strip()
)
INSTITUTION_GENERIC_PATTERN = re.compile(
    r"\b(?:Conseil|Ministère|Ministry|Assemblée|Assembly|Commission|Tribunal|Cour|Court|Agence|Agency|Institut|Institute|Université|University|Fédération|Federation|Syndicat|Union|Organisation|Organization|Police|Gendarmerie|Banque|Bank|Parlement|Parliament)\b[^\n.;:!?()]{0,80}",
    re.IGNORECASE,
)

LANGUAGE_MONTH_NAMES: Mapping[str, Tuple[str, ...]] = {
    "fr": ("janvier", "février", "fevrier", "mars", "avril", "mai", "juin", "juillet",
           "août", "aout", "septembre", "octobre", "novembre", "décembre", "decembre"),
    "en": ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december"),
}
ALL_MONTH_NAMES: Tuple[str, ...] = tuple(
    dict.fromkeys(name for names in LANGUAGE_MONTH_NAMES.values() for name in names)
)

# ---------------------------------------------------------------------------
# HTML metrics constants (from groq_html_comparser)
# ---------------------------------------------------------------------------
NEW_COLUMNS: Tuple[str, ...] = (
    "Hashtags", "Article Links", "Video Links", "Image Links", "Key Points",
    "Percent Mention", "Decibel Mention", "Speed Mention",
    "Weight Object Mention", "Weight Person Mention", "Distance Mention",
    "Temperature Mention", "Surface Mention", "Volume Mention",
    "Social Network Mention", "City Mention", "Country Mention",
    "Ranking Mention", "Spoken URL", "Punctuation Signal", "Bold Text", "Concrete Emoji",
    "CTA Detected", "Italic Text", "Underlined Text", "List Marker", "List Type", "List Block",
)

HASHTAG_PATTERN = re.compile(r"#([\w\d_]{2,64})", re.UNICODE)
PERCENT_PATTERN = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*%|\b\d+(?:[.,]\d+)?\s*(?:pour\s*cent|percent)\b", re.IGNORECASE,
)
DECIBEL_PATTERN = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:dB|décibels?|decibels?)\b", re.IGNORECASE)
SPEED_PATTERN = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:km/?h|kmh|km\s*h(?:eures?)?|kilom(?:è|e)tres?\s+(?:par\s+)?heure|m/s|mach|noeuds?)\b",
    re.IGNORECASE,
)
WEIGHT_OBJECT_PATTERN = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:tonnes?|t)\b", re.IGNORECASE)
WEIGHT_PERSON_PATTERN = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:kg|kilos?|kilogrammes?|livres?)\b", re.IGNORECASE)
DISTANCE_PATTERN = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:km|kilom(?:è|e)tres?|miles?)\b", re.IGNORECASE)
TEMPERATURE_PATTERN = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:°\s?[cf]|degr[ée]s?\s*(?:celsius|fahrenheit)?)\b", re.IGNORECASE,
)
SURFACE_PATTERN = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:km2|km²|kilom(?:è|e)tres?\s+carr[ée]s?|m2|m²|hectares?)\b", re.IGNORECASE,
)
VOLUME_PATTERN = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:m3|m³|litres?|liters?|gallons?|barils?)\b", re.IGNORECASE,
)
BULLET_PATTERN = re.compile(r"(?:^|\n|\r)[\s>*-]{0,3}[-•*]\s+(.+)", re.MULTILINE)
QUOTE_DETECTION_PATTERN = re.compile(r'[«"\u201c](.+?)[»"\u201d]')

URL_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".m3u8"}
URL_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
VIDEO_HOSTS = {"youtube.com", "youtu.be", "vimeo.com", "dailymotion.com"}
SOCIAL_KEYWORDS = {
    "facebook": "Facebook", "snapchat": "Snapchat", "instagram": "Instagram",
    "twitter": "Twitter", "x.com": "Twitter", "youtube": "YouTube",
    "tiktok": "TikTok", "linkedin": "LinkedIn", "telegram": "Telegram",
}

CTA_PATTERNS: Mapping[str, Tuple[re.Pattern[str], ...]] = {
    "commentez": (
        re.compile(r"\bcomment(?:ez|e|er|aire(?:z)?)\b", re.IGNORECASE),
        re.compile(r"\b(?:laisse(?:z)?|met(?:tez)?|ecri(?:s|vez)|dis)\b.{0,30}\bcomment", re.IGNORECASE),
        re.compile(r"\bcommentaire(?:s)?\b", re.IGNORECASE),
    ),
    "abonnez": (
        re.compile(r"\babonn(?:e|é|ez|er|ement|ements)[-\s]?(?:toi|vous)?\b", re.IGNORECASE),
        re.compile(r"\bsubscribe\b", re.IGNORECASE),
        re.compile(r"\bsub\b", re.IGNORECASE),
    ),
    "tippee": (
        re.compile(r"\b(?:tipeee?|patreon)\b", re.IGNORECASE),
        re.compile(r"\b(?:faites?|faire|fais)\b.{0,24}\bdon\b", re.IGNORECASE),
        re.compile(r"\b(?:don|dons)\b", re.IGNORECASE),
        re.compile(r"\b(?:soutiens?|soutenez|support(?:ez)?|donate)\b", re.IGNORECASE),
    ),
}

BLOCK_TAGS: frozenset[str] = frozenset({"p", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6"})
STYLE_TAGS: frozenset[str] = frozenset({"b", "strong", "i", "em", "u", "ins", "span", "a"})
REFERENCE_BLOCK_TAGS: frozenset[str] = frozenset({"p", "li"})
LIST_MIN_ITEMS = 2
LIST_MAX_ITEMS = 8
CHECKLIST_PATTERN = re.compile(r"^\s*(\[(?:x|X| )\]|☐|☑|☒|✅|✔|✓)\s*(.+?)\s*$")
NUMBERED_LIST_PATTERN = re.compile(r"^\s*((?:\d+|[A-Za-z])[\.\)])\s*(.+?)\s*$")
DASH_LIST_PATTERN = re.compile(r"^\s*([-–—])\s*(.+?)\s*$")
BULLET_LINE_PATTERN = re.compile(r"^\s*([•◦▪▫◾◽◉○◯◆◇*])\s*(.+?)\s*$")

RANKING_PATTERN = re.compile(
    r"\b\d+\s*[eè][rme]{0,2}\s*(?:place?|position|rang|classement|rank(?:ing)?)\b"
    r"|\b(?:premi[eè]re?|premier|first|second|third)\s*(?:place?|position|rang|rank(?:ing)?)\b"
    r"|\b\d+(?:st|nd|rd|th)\s*(?:place?|position|rank(?:ing)?)?\b"
    r"|\bm[ée]daille\s+d[\''](?:or|argent)\b"
    r"|\bm[ée]daille\s+de\s+bronze\b"
    r"|\b(?:gold|silver|bronze)\s+medal\b"
    r"|\bpodium\b"
    r"|\b(?:class[ée]|rang|ranked?)\s+\d+\b",
    re.IGNORECASE | re.UNICODE,
)
SPOKEN_URL_PATTERN = re.compile(
    r"\b[a-zA-Z][\w-]{1,}\."
    r"(?:com|fr|net|org|io|eu|uk|de|es|it|be|ch|ca|au|co|tv|news|info|biz|me|app|edu|gov)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Emoji sets for concrete_emoji column
# ---------------------------------------------------------------------------
EMOJI_JOBS: frozenset = frozenset({
    "👨‍💼", "👩‍💼", "🧑‍💼", "👨‍🔧", "👩‍🔧", "🧑‍🔧",
    "👨‍⚕️", "👩‍⚕️", "🧑‍⚕️", "👨‍🍳", "👩‍🍳", "🧑‍🍳",
    "👨‍🏫", "👩‍🏫", "🧑‍🏫", "👨‍🌾", "👩‍🌾", "🧑‍🌾",
    "👨‍🚒", "👩‍🚒", "🧑‍🚒", "👮", "👷", "💼",
    "👨‍✈️", "👩‍✈️", "🧑‍✈️", "👨‍🚀", "👩‍🚀", "🧑‍🚀",
    "👨‍🎨", "👩‍🎨", "🧑‍🎨", "👨‍⚖️", "👩‍⚖️", "🧑‍⚖️",
    "🕵️", "👨‍💻", "👩‍💻", "🧑‍💻", "👨‍🏭", "👩‍🏭", "🧑‍🏭",
    "👨‍🔬", "👩‍🔬", "🧑‍🔬", "👨‍🎤", "👩‍🎤", "🧑‍🎤",
    "👨‍🎓", "👩‍🎓", "🧑‍🎓",
})
EMOJI_SPORTS: frozenset = frozenset({
    "⚽", "🏀", "🏈", "⚾", "🥎", "🎾", "🏐", "🏉", "🥏",
    "🎱", "🏓", "🏸", "🥊", "🥋", "🎽", "⛷️", "🏂", "🏋️",
    "🤸", "🤼", "🤺", "🏊", "🚴", "🏇", "🤾", "🏄", "🧗",
    "🏌️", "🤿", "🥌", "🛷", "🏒", "🏑", "🏏", "🥅", "⛳",
    "🏹", "🎿", "🛶", "🏆", "🥇", "🥈", "🥉", "🎯", "🎣",
    "🤽", "🧘", "⛹️",
})
EMOJI_FOOD: frozenset = frozenset({
    "🍕", "🍔", "🍟", "🌭", "🍿", "🧆", "🥚", "🍳", "🧇",
    "🥞", "🧈", "🍞", "🥐", "🥖", "🥨", "🧀", "🥗", "🥙",
    "🌮", "🌯", "🥪", "🍜", "🍝", "🍛", "🍣", "🍱", "🍤",
    "🍙", "🍚", "🍘", "🍥", "🥮", "🍢", "🍡", "🍦", "🍧",
    "🍨", "🍩", "🍪", "🍰", "🧁", "🥧", "🍫", "🍬",
    "🍭", "🍮", "🍯", "🍎", "🍐", "🍊", "🍋", "🍌", "🍉",
    "🍇", "🍓", "🫐", "🍈", "🍒", "🍑", "🥭", "🍍", "🥥",
    "🥝", "🍅", "🍆", "🥑", "🥦", "🥬", "🥒", "🌶️", "🫑",
    "🥕", "🧄", "🧅", "🥔", "🍠", "🌽", "🍄", "🥜", "🌰",
    "☕", "🍵", "🧃", "🥤", "🧊", "🍺", "🍻", "🥂", "🍷",
    "🥃", "🍸", "🍹", "🧋", "🍾",
})
EMOJI_ANIMALS: frozenset = frozenset({
    "🐶", "🐱", "🐭", "🐹", "🐰", "🦊", "🐻", "🐼", "🐨",
    "🐯", "🦁", "🐮", "🐷", "🐸", "🐵", "🙈", "🙉", "🙊",
    "🐔", "🐧", "🐦", "🐤", "🦆", "🦅", "🦉", "🦇", "🐺",
    "🐗", "🦄", "🐝", "🐛", "🦋", "🐌", "🐞", "🐜", "🦟",
    "🦗", "🦂", "🐢", "🐍", "🦎", "🐙", "🦑", "🦐", "🦞",
    "🦀", "🐡", "🐟", "🐠", "🐬", "🐳", "🐋", "🦈", "🐊",
    "🦓", "🦍", "🐆", "🐅", "🦏", "🦛", "🐘", "🦒", "🦘",
    "🐃", "🐂", "🐄", "🦌", "🐎", "🐖", "🐏", "🐑", "🦙",
    "🐐", "🦃", "🦚", "🦜", "🦢", "🦩", "🕊️", "🐇", "🦔",
    "🐁", "🐀", "🦦", "🦥", "🦨", "🦡", "🐿️", "🐩",
})
EMOJI_EVENTS: frozenset = frozenset({
    "🎂",   # birthday / anniversary
    "🎄",   # christmas / xmas
    "🎃",   # halloween
    "💍",   # wedding / marriage
})
CONCRETE_EMOJI_ALL: frozenset = EMOJI_JOBS | EMOJI_SPORTS | EMOJI_FOOD | EMOJI_ANIMALS | EMOJI_EVENTS

REFERENCE_COLUMN_NAME = "Reference Segment"
TEXT_COLUMN_NAME = "Text"
QUOTE_COLUMN_NAME = "Quote Extracted"
LOCATION_COLUMN_NAME = "Location Mention"

# ---------------------------------------------------------------------------
# Inlined from comparser.py: HTML reference collection
# ---------------------------------------------------------------------------

# Strips timecode annotations from extracted paragraph text so they are never
# fed into transcript matching.
#
# _TIMECODE_ANNOTATION_RE  — bracketed form: (01:00-01:15) or [1:00 to 1:10]
#                            applied to every paragraph.
# _BARE_TIMECODE_RE        — bare form: 03:10-4:10 or 1min02-1min10
#                            applied only to paragraphs that contain a video link
#                            (gated to avoid false positives in normal prose).

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
    + _TC + _SEP_BARE + r'(?:' + _TC + r')?-?'  # range: TC SEP [TC][-]
    + r'|-(?:' + _TC + r')'                       # end-only: -TC
    + r'|(?:' + _TC + r')-'                       # start-only explicit: TC-
    + r')(?!\w)'
)
_BARE_TIMECODE_RE = re.compile(_bare_tc_pattern, re.IGNORECASE)


class ParagraphExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._capture_tag: Optional[str] = None
        self._buffer: List[str] = []
        self.paragraphs: List[str] = []
        self._has_video_link: bool = False

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        tag_lower = tag.lower()
        if tag_lower in REFERENCE_BLOCK_TAGS:
            self._capture_tag = tag_lower
            self._buffer = []
            self._has_video_link = False
        elif tag_lower == "br" and self._capture_tag:
            self._buffer.append(" ")
        elif tag_lower == "a" and self._capture_tag:
            href = dict(attrs).get("href") or ""
            if any(kw in href for kw in _VIDEO_HOST_KEYWORDS_TC):
                self._has_video_link = True

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        tag_lower = tag.lower()
        if tag_lower == self._capture_tag:
            raw = html_module.unescape("".join(self._buffer))
            text = re.sub(r"\s+", " ", raw).strip()
            text = _TIMECODE_ANNOTATION_RE.sub("", text).strip()
            if self._has_video_link:
                text = _BARE_TIMECODE_RE.sub("", text).strip()
            if text:
                self.paragraphs.append(text)
            self._capture_tag = None
            self._buffer = []
            self._has_video_link = False

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._capture_tag:
            self._buffer.append(data)

    def handle_entityref(self, name: str) -> None:  # type: ignore[override]
        if self._capture_tag:
            self._buffer.append(html_module.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:  # type: ignore[override]
        if self._capture_tag:
            self._buffer.append(html_module.unescape(f"&#{name};"))


def normalize_lookup_text(value: Optional[str]) -> str:
    text = normalize_for_matching(value)
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-z0-9#]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def dedupe_texts(values: Iterable[str], *, min_len: int = 1) -> List[str]:
    results: List[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = re.sub(r"\s+", " ", value).strip()
        if len(cleaned) < min_len:
            continue
        key = normalize_lookup_text(cleaned)
        if not key or key in seen:
            continue
        seen.add(key)
        results.append(cleaned)
    return results


def dedupe_list_signals(values: Iterable[str]) -> List[str]:
    results: List[str] = []
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


def parse_list_line(text: str) -> Optional[Dict[str, object]]:
    stripped = text.strip()
    if not stripped:
        return None
    checklist_match = CHECKLIST_PATTERN.match(stripped)
    if checklist_match:
        marker = checklist_match.group(1)
        signals = ["case"]
        if marker.casefold() not in {"[ ]", "☐"}:
            signals.append("check mark")
        return {
            "text": checklist_match.group(2).strip(),
            "marker_signals": dedupe_list_signals(signals),
            "list_type": "check",
        }
    numbered_match = NUMBERED_LIST_PATTERN.match(stripped)
    if numbered_match:
        return {
            "text": numbered_match.group(2).strip(),
            "marker_signals": ["keypoint", "numbered"],
            "list_type": "number",
        }
    dash_match = DASH_LIST_PATTERN.match(stripped)
    if dash_match:
        return {
            "text": dash_match.group(2).strip(),
            "marker_signals": ["-"],
            "list_type": "dash",
        }
    bullet_match = BULLET_LINE_PATTERN.match(stripped)
    if bullet_match:
        return {
            "text": bullet_match.group(2).strip(),
            "marker_signals": ["keypoint", "puce"],
            "list_type": "bullet",
        }
    return None


def infer_marker_signals_from_text(text: str) -> List[str]:
    parsed = parse_list_line(text)
    if not parsed:
        return []
    return [str(value) for value in parsed.get("marker_signals", []) if str(value).strip()]


def extract_line_list_entries(raw_text: str) -> List[Dict[str, object]]:
    entries: List[Dict[str, object]] = []
    for line in raw_text.splitlines():
        parsed = parse_list_line(line)
        if not parsed:
            continue
        cleaned = str(parsed.get("text") or "").strip()
        if not cleaned:
            continue
        entries.append(
            {
                "text": cleaned,
                "norm_text": normalize_lookup_text(cleaned),
                "marker_signals": dedupe_list_signals(parsed.get("marker_signals", [])),
                "list_type": str(parsed.get("list_type") or "").strip(),
            }
        )
    return entries


def build_list_groups(blocks: Sequence[Mapping[str, object]]) -> List[Dict[str, object]]:
    list_groups: List[Dict[str, object]] = []
    current_group: List[Mapping[str, object]] = []
    for block in blocks:
        list_type = str(block.get("list_type") or "").strip()
        block_index = int(block.get("block_index", -1))
        if list_type:
            if current_group:
                previous_index = int(current_group[-1].get("block_index", -999))
                previous_type = str(current_group[-1].get("list_type") or "").strip()
                if block_index != previous_index + 1 or list_type != previous_type:
                    if LIST_MIN_ITEMS <= len(current_group) <= LIST_MAX_ITEMS:
                        list_groups.append(
                            {
                                "list_type": previous_type,
                                "items": [str(entry.get("text") or "").strip() for entry in current_group],
                                "text": " | ".join(str(entry.get("text") or "").strip() for entry in current_group),
                                "block_indices": [int(entry.get("block_index", 0)) for entry in current_group],
                            }
                        )
                    current_group = []
            current_group.append(block)
            continue
        if LIST_MIN_ITEMS <= len(current_group) <= LIST_MAX_ITEMS:
            list_groups.append(
                {
                    "list_type": str(current_group[0].get("list_type") or "").strip(),
                    "items": [str(entry.get("text") or "").strip() for entry in current_group],
                    "text": " | ".join(str(entry.get("text") or "").strip() for entry in current_group),
                    "block_indices": [int(entry.get("block_index", 0)) for entry in current_group],
                }
            )
        current_group = []
    if LIST_MIN_ITEMS <= len(current_group) <= LIST_MAX_ITEMS:
        list_groups.append(
            {
                "list_type": str(current_group[0].get("list_type") or "").strip(),
                "items": [str(entry.get("text") or "").strip() for entry in current_group],
                "text": " | ".join(str(entry.get("text") or "").strip() for entry in current_group),
                "block_indices": [int(entry.get("block_index", 0)) for entry in current_group],
            }
        )
    return list_groups


def _decode_css_content(value: str) -> str:
    def replace_unicode(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        try:
            return chr(int(raw, 16))
        except ValueError:
            return ""

    decoded = re.sub(r"\\([0-9a-fA-F]{1,6})\s?", replace_unicode, value)
    decoded = decoded.replace(r"\"", '"').replace(r"\'", "'")
    return html_module.unescape(decoded)


def parse_css_class_styles(html_text: str) -> Dict[str, Dict[str, object]]:
    mapping: Dict[str, Dict[str, object]] = {}
    for class_name, definition in re.findall(r"\.([a-zA-Z0-9_-]+)\{([^}]*)\}", html_text):
        lowered = definition.lower()
        font_weight_match = re.search(r"font-weight\s*:\s*([0-9]+|bold)", lowered)
        font_weight_value = font_weight_match.group(1) if font_weight_match else ""
        is_bold = font_weight_value == "bold" or (font_weight_value.isdigit() and int(font_weight_value) >= 600)
        is_italic = "font-style:italic" in lowered
        is_underline = "text-decoration:underline" in lowered
        content_match = re.search(r'content\s*:\s*"([^"]*)"', definition, re.IGNORECASE)
        marker_text = _decode_css_content(content_match.group(1)) if content_match else ""
        mapping[class_name] = {
            "bold": is_bold,
            "italic": is_italic,
            "underline": is_underline,
            "marker_text": marker_text,
        }
    return mapping


def merge_style_flags(base: Mapping[str, bool], extra: Mapping[str, bool]) -> Dict[str, bool]:
    return {
        "bold": base.get("bold", False) or extra.get("bold", False),
        "italic": base.get("italic", False) or extra.get("italic", False),
        "underline": base.get("underline", False) or extra.get("underline", False),
    }


def extract_marker_signals(marker_text: str, class_tokens: Sequence[str], list_kind: Optional[str]) -> List[str]:
    signals: List[str] = []
    normalized_marker = marker_text.strip().lower()
    normalized_classes = {token.lower() for token in class_tokens}
    if any(token.startswith("li-bullet") for token in normalized_classes) or list_kind == "ul":
        signals.extend(["keypoint", "puce"])
    if list_kind == "ol":
        signals.extend(["keypoint", "numbered"])
    if normalized_marker.startswith("-"):
        signals.append("-")
    if any(symbol in marker_text for symbol in ("•", "◦", "▪", "▫", "◾", "◽", "◉", "○", "◯", "◆", "◇")):
        signals.extend(["keypoint", "puce"])
    if any(symbol in marker_text for symbol in ("☐", "☑", "☒", "✅", "✔", "✓")):
        signals.extend(["case", "check mark"])
    return dedupe_list_signals(signals)


def classify_list_type(marker_signals: Sequence[str], list_kind: Optional[str]) -> str:
    lowered = {signal.strip().lower() for signal in marker_signals if signal.strip()}
    if "check mark" in lowered or "case" in lowered:
        return "check"
    if "numbered" in lowered or list_kind == "ol":
        return "number"
    if "-" in lowered:
        return "dash"
    if "puce" in lowered or list_kind == "ul":
        return "bullet"
    return ""


class HtmlFeatureExtractor(HTMLParser):
    def __init__(self, class_styles: Mapping[str, Mapping[str, object]]) -> None:
        super().__init__()
        self.class_styles = class_styles
        self.blocks: List[Dict[str, object]] = []
        self.links: List[str] = []
        self.hashtags: List[str] = []
        self._style_stack: List[Dict[str, bool]] = [{"bold": False, "italic": False, "underline": False}]
        self._list_stack: List[Tuple[str, List[str]]] = []
        self._current_block: Optional[Dict[str, object]] = None
        # Counts handle_data calls since last bold/italic/underline fragment.
        # Zero means the previous call was the same styled type → adjacent tags can merge.
        self._since_bold: int = 0
        self._since_italic: int = 0
        self._since_underline: int = 0

    def _current_style(self) -> Dict[str, bool]:
        return self._style_stack[-1]

    def _start_block(
        self,
        tag: str,
        marker_signals: Optional[Sequence[str]] = None,
        list_kind: Optional[str] = None,
    ) -> None:
        self._finalize_block()
        self._current_block = {
            "tag": tag,
            "text_parts": [],
            "bold_parts": [],
            "italic_parts": [],
            "underline_parts": [],
            "marker_signals": list(marker_signals or []),
            "list_kind": list_kind or "",
        }
        self._since_bold = 0
        self._since_italic = 0
        self._since_underline = 0

    def _finalize_block(self) -> None:
        if not self._current_block:
            return
        raw_text = "".join(self._current_block["text_parts"])
        marker_signals = dedupe_list_signals(self._current_block["marker_signals"])
        parsed_line = parse_list_line(raw_text)
        if parsed_line:
            text = str(parsed_line.get("text") or "").strip()
            marker_signals = dedupe_list_signals(marker_signals + list(parsed_line.get("marker_signals", [])))
            list_kind = str(parsed_line.get("list_type") or "").strip()
        else:
            text = re.sub(r"\s+", " ", raw_text).strip()
            if not marker_signals:
                marker_signals = infer_marker_signals_from_text(raw_text)
            list_kind = str(self._current_block.get("list_kind") or "").strip().lower()
        if text:
            block = {
                "tag": self._current_block["tag"],
                "text": text,
                "norm_text": normalize_lookup_text(text),
                "raw_text": raw_text,
                "bold_parts": dedupe_texts(self._current_block["bold_parts"], min_len=2),
                "italic_parts": dedupe_texts(self._current_block["italic_parts"], min_len=2),
                "underline_parts": dedupe_texts(self._current_block["underline_parts"], min_len=2),
                "marker_signals": marker_signals,
                "list_type": classify_list_type(
                    marker_signals,
                    list_kind or None,
                ),
            }
            self.blocks.append(block)
            self.hashtags.extend(detect_hashtags(text))
        self._current_block = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:  # type: ignore[override]
        tag_lower = tag.lower()
        attr_map = {name.lower(): value or "" for name, value in attrs}
        class_tokens = [token for token in attr_map.get("class", "").split() if token]
        class_flags = {"bold": False, "italic": False, "underline": False}
        marker_texts: List[str] = []
        for token in class_tokens:
            style = self.class_styles.get(token, {})
            class_flags = merge_style_flags(class_flags, {
                "bold": bool(style.get("bold")),
                "italic": bool(style.get("italic")),
                "underline": bool(style.get("underline")),
            })
            marker_value = style.get("marker_text")
            if isinstance(marker_value, str) and marker_value.strip():
                marker_texts.append(marker_value)
        tag_flags = {"bold": tag_lower in {"b", "strong"}, "italic": tag_lower in {"i", "em"}, "underline": tag_lower in {"u", "ins"}}
        if tag_lower not in {"br", "input"}:
            self._style_stack.append(merge_style_flags(self._current_style(), merge_style_flags(class_flags, tag_flags)))

        if tag_lower in {"ul", "ol"}:
            self._list_stack.append((tag_lower, dedupe_texts(marker_texts)))
        elif tag_lower == "li":
            list_kind = self._list_stack[-1][0] if self._list_stack else None
            inherited_markers = self._list_stack[-1][1] if self._list_stack else []
            marker_signals = extract_marker_signals(" ".join(inherited_markers + marker_texts), class_tokens, list_kind)
            self._start_block(tag_lower, marker_signals, list_kind=list_kind)
        elif tag_lower in BLOCK_TAGS:
            self._start_block(tag_lower)
        elif tag_lower == "br" and self._current_block:
            self._current_block["text_parts"].append("\n")
        elif tag_lower == "a":
            href = attr_map.get("href", "").strip()
            if href:
                self.links.append(href)
        elif tag_lower == "input":
            input_type = attr_map.get("type", "").lower()
            if input_type == "checkbox":
                if self._current_block is None:
                    self._start_block("li", ["case"])
                self._current_block["marker_signals"].append("case")
                if "checked" in attr_map:
                    self._current_block["marker_signals"].append("check mark")

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        tag_lower = tag.lower()
        if tag_lower in BLOCK_TAGS:
            self._finalize_block()
        elif tag_lower in {"ul", "ol"} and self._list_stack:
            self._list_stack.pop()
        if len(self._style_stack) > 1:
            self._style_stack.pop()

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:  # type: ignore[override]
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if not self._current_block:
            return
        self._current_block["text_parts"].append(data)
        if data.strip():
            style = self._current_style()

            if style.get("bold"):
                parts = self._current_block["bold_parts"]
                if parts and self._since_bold == 0:
                    parts[-1] += data
                else:
                    parts.append(data)
                self._since_bold = 0
            else:
                self._since_bold += 1

            if style.get("italic"):
                parts = self._current_block["italic_parts"]
                if parts and self._since_italic == 0:
                    parts[-1] += data
                else:
                    parts.append(data)
                self._since_italic = 0
            else:
                self._since_italic += 1

            if style.get("underline"):
                parts = self._current_block["underline_parts"]
                if parts and self._since_underline == 0:
                    parts[-1] += data
                else:
                    parts.append(data)
                self._since_underline = 0
            else:
                self._since_underline += 1
        else:
            # Whitespace-only data between styled tags breaks adjacency.
            self._since_bold += 1
            self._since_italic += 1
            self._since_underline += 1

    def handle_entityref(self, name: str) -> None:  # type: ignore[override]
        self.handle_data(html_module.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:  # type: ignore[override]
        self.handle_data(html_module.unescape(f"&#{name};"))

    def close(self) -> None:  # type: ignore[override]
        super().close()
        self._finalize_block()


_URL_PATTERN = re.compile(r"(https?://\S+|www\.\S+)", re.IGNORECASE)


def _clean_paragraph(text: str) -> str:
    stripped = re.sub(r"\s+", " ", text).strip()
    if not stripped:
        return ""
    upper = stripped.upper()
    if upper.startswith("EXTRAIT") or upper.startswith("INSERT"):
        return ""
    stripped = _URL_PATTERN.sub(" ", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def collect_reference_text(html_path: Path) -> str:
    parser = ParagraphExtractor()
    parser.feed(html_path.read_text(encoding="utf-8"))
    cleaned: List[str] = []
    for paragraph in parser.paragraphs:
        normalized = _clean_paragraph(paragraph)
        if normalized:
            cleaned.append(normalized)
    return "\n".join(cleaned)


def strip_reference_title(reference_text: str) -> str:
    lines = reference_text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].strip().upper().startswith("TITRE"):
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Nouns: title extractor
# ---------------------------------------------------------------------------

class _FallbackTitleExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.capture_depth = 0
        self.buffer: List[str] = []
        self.titles: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag_lower = tag.lower()
        class_tokens: List[str] = []
        for name, value in attrs:
            if name.lower() == "class" and value:
                class_tokens = [token.strip().lower() for token in value.split() if token.strip()]
                break
        is_title = tag_lower in TITLE_TAGS or "title" in class_tokens
        if is_title:
            if self.capture_depth == 0:
                self.buffer = []
            self.capture_depth += 1
        elif self.capture_depth:
            self.capture_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if not self.capture_depth:
            return
        self.capture_depth -= 1
        if self.capture_depth == 0:
            text = re.sub(r"\s+", " ", "".join(self.buffer)).strip()
            if text:
                self.titles.append(text)
            self.buffer = []

    def handle_data(self, data: str) -> None:
        if self.capture_depth:
            self.buffer.append(data)


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def load_csv(path: Path) -> Tuple[List[str], List[List[str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter=";")
        try:
            header = next(reader)
        except StopIteration:
            raise RuntimeError(f"{path} is empty.")
        rows = [list(row) for row in reader]
    return list(header), rows


def write_csv(path: Path, header: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(list(header))
        writer.writerows(rows)


def build_header_map(header: Sequence[str]) -> Dict[str, int]:
    return {column.strip().lower(): idx for idx, column in enumerate(header)}


def ensure_column(header: List[str], rows: List[List[str]], column: str) -> int:
    header_map = build_header_map(header)
    idx = header_map.get(column.strip().lower())
    if idx is None:
        header.append(column)
        idx = len(header) - 1
        for row in rows:
            if len(row) < len(header):
                row.extend([""] * (len(header) - len(row)))
            row[idx] = ""
    else:
        for row in rows:
            if idx >= len(row):
                row.extend([""] * (idx + 1 - len(row)))
            row[idx] = ""
    return idx


def require_column(header_map: Mapping[str, int], column_name: str) -> int:
    key = column_name.strip().lower()
    if key not in header_map:
        raise KeyError(f"Required column '{column_name}' not found in CSV header.")
    return header_map[key]


def split_pipe_values(value: Optional[str]) -> List[str]:
    if not value:
        return []
    fragments = [fragment.strip() for fragment in value.split("|")]
    return [fragment for fragment in fragments if fragment]


# ---------------------------------------------------------------------------
# Run summary
# ---------------------------------------------------------------------------

def _stringify(value) -> str:
    from collections.abc import Mapping as _Mapping, Sequence as _Sequence
    if value is None:
        return "None"
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, _Mapping):
        return ", ".join(f"{k}: {_stringify(v)}" for k, v in value.items())
    if isinstance(value, _Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return ", ".join(_stringify(item) for item in value)
    return str(value)


def print_run_summary(inputs, outputs=None) -> None:
    print("\n=== Run Summary ===")
    print("Inputs:")
    if inputs:
        for key, value in inputs.items():
            print(f"  - {key}: {_stringify(value)}")
    else:
        print("  - <none>")
    print("Outputs:")
    if outputs:
        for key, value in outputs.items():
            print(f"  - {key}: {_stringify(value)}")
    else:
        print("  - <none>")
    print("===================\n")


# ---------------------------------------------------------------------------
# API key resolver
# ---------------------------------------------------------------------------

def resolve_api_key(candidate: Optional[str]) -> str:
    api_key = candidate or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Set --claude-api-key or export ANTHROPIC_API_KEY.")
    return api_key


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def truncate(value: str, limit: int = 800) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def escape_braces(value: str) -> str:
    return value.replace("{", "{{").replace("}", "}}")


def extract_response_text(response) -> str:
    parts: List[str] = []
    for block in response.content:
        text = getattr(block, "text", "")
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _escape_line_breaks_in_strings(value: str) -> Tuple[str, bool]:
    result: List[str] = []
    in_string = False
    escape = False
    changed = False
    for char in value:
        if in_string:
            if escape:
                result.append(char)
                escape = False
                continue
            if char == "\\":
                result.append(char)
                escape = True
                continue
            if char == '"':
                result.append(char)
                in_string = False
                continue
            if char == "\n":
                result.append("\\n")
                changed = True
                continue
            if char == "\r":
                result.append("\\r")
                changed = True
                continue
            result.append(char)
        else:
            result.append(char)
            if char == '"':
                in_string = True
            else:
                escape = False
    return "".join(result), changed


def _append_missing_closers(value: str) -> Tuple[str, bool]:
    stack: List[str] = []
    in_string = False
    escape = False
    for char in value:
        if in_string:
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            stack.append("}")
        elif char == "[":
            stack.append("]")
        elif char in "}]" and stack:
            stack.pop()
    if not stack:
        return value, False
    return value + "".join(reversed(stack)), True


def parse_claude_json(raw_content: str) -> MutableMapping[str, object]:
    if not raw_content:
        raise RuntimeError("Claude returned an empty response.")
    if raw_content.startswith("```"):
        lines = [line for line in raw_content.splitlines() if not line.startswith("```")]
        raw_content = "\n".join(lines).strip()
    start = raw_content.find("{")
    end = raw_content.rfind("}")
    if start != -1 and end != -1 and end >= start:
        raw_content = raw_content[start: end + 1]
    raw_content = raw_content.replace(",\n}", "\n}").replace(",\n]", "\n]")
    attempts = [raw_content]
    missing_comma_fixed = re.sub(r"}(\s+){", r"},\1{", raw_content)
    if missing_comma_fixed != raw_content:
        attempts.append(missing_comma_fixed)
    sanitized, changed = _escape_line_breaks_in_strings(raw_content)
    if changed:
        attempts.append(sanitized)
    balanced, balance_changed = _append_missing_closers(raw_content)
    if balance_changed:
        attempts.append(balanced)
    last_error: Optional[Exception] = None
    for candidate in attempts:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
    assert last_error is not None
    raise RuntimeError(f"Failed to parse Claude response as JSON: {last_error}") from last_error


def parse_claude_json_simple(raw_content: str) -> Dict[str, object]:
    if not raw_content:
        raise RuntimeError("Claude returned an empty response.")
    if raw_content.startswith("```"):
        lines = [line for line in raw_content.splitlines() if not line.startswith("```")]
        raw_content = "\n".join(lines).strip()
    start = raw_content.find("{")
    end = raw_content.rfind("}")
    if start != -1 and end != -1 and end >= start:
        raw_content = raw_content[start: end + 1]
    raw_content = raw_content.replace(",\n}", "\n}").replace(",\n]", "\n]")
    return json.loads(raw_content)


# ---------------------------------------------------------------------------
# CTA/Zoom tagging
# ---------------------------------------------------------------------------

def timecode_to_seconds(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parts = [int(part) for part in raw.split(":")]
    except ValueError:
        return None
    if len(parts) == 4:
        hours, minutes, seconds, frames = parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
        frames = 0
    elif len(parts) == 2:
        hours = 0
        minutes, seconds = parts
        frames = 0
    else:
        return None
    return hours * 3600 + minutes * 60 + seconds + frames / FRAME_RATE


def collect_timeline_metadata(rows: Sequence[Sequence[str]]) -> Tuple[List[Optional[float]], float]:
    starts: List[Optional[float]] = []
    total_duration = 0.0
    for row in rows:
        start = timecode_to_seconds(row[2] if len(row) > 2 else None)
        end = timecode_to_seconds(row[3] if len(row) > 3 else None)
        starts.append(start)
        if end is not None:
            total_duration = max(total_duration, end)
    return starts, total_duration


def format_payload_rows(batch: Sequence[Sequence[str]], offset: int) -> str:
    payload: List[Dict[str, str]] = []
    for local_idx, row in enumerate(batch):
        payload.append({
            "index": offset + local_idx,
            "transcript_number": row[1] if len(row) > 1 else "",
            "start_time": row[2] if len(row) > 2 else "",
            "end_time": row[3] if len(row) > 3 else "",
            "status": row[7] if len(row) > 7 else "",
            "transcript_text": truncate(row[4] if len(row) > 4 else ""),
            "reference_text": truncate(row[5] if len(row) > 5 else ""),
        })
    return json.dumps(payload, ensure_ascii=False, indent=2)


def tag_rows_with_claude(
    rows: Sequence[Sequence[str]],
    api_key: str,
    model: str,
    max_tokens: int,
    batch_size: int,
) -> Tuple[Dict[int, set], Dict[int, str]]:
    if not rows:
        return {}, {}
    if anthropic is None:
        raise RuntimeError("Install the `anthropic` package (pip install anthropic).")
    client = anthropic.Anthropic(api_key=api_key)
    tags: Dict[int, set] = {}
    zoom_marks: Dict[int, str] = {}
    for start in range(0, len(rows), batch_size):
        batch = rows[start: start + batch_size]
        payload = format_payload_rows(batch, start)
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=0,
            system=CLAUDE_CTA_SYSTEM,
            messages=[{"role": "user", "content": CLAUDE_CTA_PROMPT.format(payload=payload)}],
        )
        content = extract_response_text(response)
        data = parse_claude_json_simple(content)
        for entry in data.get("rows", []):
            idx = entry.get("index")
            if not isinstance(idx, int) or not (0 <= idx < len(rows)):
                continue
            row_tags = tags.setdefault(idx, set())
            for label in entry.get("tags", []):
                if isinstance(label, str):
                    slug = label.strip().lower()
                    if slug in {"commentez", "tippee", "abonnez"}:
                        row_tags.add(slug)
            zoom_value = entry.get("zoom")
            if isinstance(zoom_value, str):
                sanitized = zoom_value.strip().upper()
                if sanitized in ZOOM_VALUES:
                    zoom_marks[idx] = sanitized
        print(f"[Claude CTA] tagged rows {start}-{start + len(batch) - 1}")
    return tags, zoom_marks


def enforce_single_intro_tag(tags: Dict[int, set]) -> None:
    pass


def suppress_zoom_for_list_rows(header: List[str], rows: List[List[str]]) -> None:
    header_map = build_header_map(header)
    zoom_idx = header_map.get("zoom")
    list_marker_idx = header_map.get("list marker")
    list_type_idx = header_map.get("list type")
    if zoom_idx is None:
        return
    for row in rows:
        if zoom_idx >= len(row):
            continue
        has_list = (
            (list_marker_idx is not None and list_marker_idx < len(row) and (row[list_marker_idx] or "").strip())
            or (list_type_idx is not None and list_type_idx < len(row) and (row[list_type_idx] or "").strip())
        )
        if has_list:
            row[zoom_idx] = ""


def downgrade_zoom_level(value: str) -> str:
    if value == "Z3":
        return "Z2"
    if value == "Z2":
        return "Z1"
    if value == "Z1":
        return "Z"
    return ""


def upgrade_zoom_level(value: str) -> str:
    if value == "Z":
        return "Z1"
    if value == "Z1":
        return "Z2"
    if value == "Z2":
        return "Z3"
    return value


def adjust_zoom_bias(
    zoom_marks: Dict[int, str],
    start_seconds: Sequence[Optional[float]],
    total_duration: float,
) -> None:
    if not zoom_marks or total_duration <= 0:
        return
    last_ten_cutoff = max(total_duration - 10, 0)
    soft_end_cutoff = max(total_duration - 30, 0)
    mid_low = total_duration * 0.35
    mid_high = total_duration * 0.7
    for idx, zoom in list(zoom_marks.items()):
        start = start_seconds[idx] if idx < len(start_seconds) else None
        if start is None:
            continue
        level = zoom.strip().upper()
        if level not in ZOOM_VALUES:
            zoom_marks.pop(idx, None)
            continue
        if start >= last_ten_cutoff:
            zoom_marks.pop(idx, None)
            continue
        if start >= soft_end_cutoff:
            downgraded = downgrade_zoom_level(level)
            if downgraded:
                zoom_marks[idx] = downgraded
            else:
                zoom_marks.pop(idx, None)
            continue
        if mid_low <= start <= mid_high:
            zoom_marks[idx] = upgrade_zoom_level(level)


def normalize_zoom_sequences(zoom_marks: Dict[int, str], start_seconds: Sequence[Optional[float]], cluster_gap: float = 8.0) -> None:
    if not zoom_marks:
        return

    def flush(cluster: List[int]) -> None:
        if not cluster:
            return
        if len(cluster) == 1:
            zoom_marks[cluster[0]] = "Z"
            return
        pattern = ["Z1", "Z2", "Z3"]
        for idx, row_idx in enumerate(cluster):
            zoom_marks[row_idx] = pattern[min(idx, len(pattern) - 1)]

    sorted_rows = sorted(
        zoom_marks.keys(),
        key=lambda idx: ((start_seconds[idx] if idx < len(start_seconds) else None) or float("inf"), idx),
    )
    cluster: List[int] = []
    prev_time: Optional[float] = None
    for row_idx in sorted_rows:
        start = start_seconds[row_idx] if row_idx < len(start_seconds) else None
        if start is None:
            flush(cluster)
            cluster = []
            zoom_marks[row_idx] = "Z"
            prev_time = None
            continue
        if prev_time is None or (start - prev_time) <= cluster_gap:
            cluster.append(row_idx)
        else:
            flush(cluster)
            cluster = [row_idx]
        prev_time = start
    flush(cluster)


def normalize_keep_column(rows: List[List[str]]) -> None:
    for row in rows:
        if not row:
            continue
        transcript_text = row[4].strip() if len(row) > 4 and row[4] else ""
        reference_segment = row[5].strip() if len(row) > 5 and row[5] else ""
        row[0] = "X" if transcript_text and reference_segment else ""


def apply_deterministic_cta_tags(rows: Sequence[Sequence[str]], tags: Dict[int, set]) -> None:
    for idx, row in enumerate(rows):
        combined = " ".join(
            segment.strip()
            for segment in (
                row[4] if len(row) > 4 else "",
                row[5] if len(row) > 5 else "",
            )
            if segment and segment.strip()
        )
        if not combined:
            continue
        row_tags = tags.setdefault(idx, set())
        for slug, patterns in CTA_PATTERNS.items():
            if any(pattern.search(combined) for pattern in patterns):
                row_tags.add(slug)


def apply_tag_columns(header: List[str], rows: List[List[str]], tags: Dict[int, set]) -> None:
    column_positions = {column_name: ensure_column(header, rows, column_name) for column_name, _, _ in TAG_SPECS}
    for idx, row in enumerate(rows):
        row_tags = tags.get(idx, set())
        for column_name, slug, label in TAG_SPECS:
            row[column_positions[column_name]] = label if slug in row_tags else ""


def apply_zoom_column(header: List[str], rows: List[List[str]], zoom_marks: Dict[int, str]) -> None:
    zoom_idx = ensure_column(header, rows, "Zoom")
    for idx, row in enumerate(rows):
        zoom = zoom_marks.get(idx, "")
        normalized = zoom.strip().upper()
        if normalized not in ZOOM_VALUES:
            normalized = ""
        row[zoom_idx] = normalized


# ---------------------------------------------------------------------------
# Nouns/semantic enrichment helpers
# ---------------------------------------------------------------------------

def normalize_for_matching(value: Optional[str]) -> str:
    if not value:
        return ""
    text = value
    for src, dst in NORMALIZATION_REPLACEMENTS:
        text = text.replace(src, dst)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _normalize_geo_term(value: Optional[str]) -> str:
    if not value:
        return ""
    text = normalize_for_matching(value).lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    tokens = re.findall(r"[a-z0-9]+", text)
    return " ".join(tokens)


LANGUAGE_HOME_COUNTRIES_NORMALIZED: Dict[str, set] = {
    code: {normalized for normalized in (_normalize_geo_term(name) for name in names) if normalized}
    for code, names in LANGUAGE_HOME_COUNTRIES.items()
}
LANGUAGE_LOCATION_OVERRIDE_LOOKUP: Dict[str, Dict[str, str]] = {
    code: {
        normalized: override
        for key, override in mapping.items()
        for normalized in [_normalize_geo_term(key)]
        if normalized and override
    }
    for code, mapping in LANGUAGE_LOCATION_OVERRIDES.items()
}


def prepare_reference_analysis_text(reference_text: str) -> str:
    return normalize_for_matching(reference_text)


def detect_language_from_text(text: str) -> str:
    corpus = (text or "").lower()
    if not corpus:
        return "fr"
    best_code = "fr"
    best_score = float("-inf")
    for code, profile in LANGUAGE_PROFILES.items():
        score = 0.0
        keywords = profile.get("keywords", [])
        characters = profile.get("characters", set())
        for keyword in keywords:
            score += corpus.count(keyword)
        if characters:
            score += sum(corpus.count(ch) for ch in characters) * 1.5
        if score > best_score:
            best_score = score
            best_code = code
    return best_code


def ensure_feature_columns(header: List[str], rows: List[List[str]]) -> Dict[str, int]:
    positions: Dict[str, int] = {}
    for _, display_name in FEATURE_SPECS:
        header_map = build_header_map(header)
        key = display_name.strip().lower()
        idx = header_map.get(key)
        if idx is None:
            insert_idx = len(header)
            if display_name == "Money Mention":
                number_idx = header_map.get("number mention")
                if number_idx is not None:
                    insert_idx = number_idx
            header.insert(insert_idx, display_name)
            for row in rows:
                while len(row) < insert_idx:
                    row.append("")
                row.insert(insert_idx, "")
            idx = insert_idx
        positions[display_name] = idx
    for row in rows:
        if len(row) < len(header):
            row.extend([""] * (len(header) - len(row)))
        for idx in positions.values():
            row[idx] = ""
    return positions


def ensure_titles_column(header: List[str], rows: List[List[str]]) -> int:
    header_map = build_header_map(header)
    idx = header_map.get(TITLE_COLUMN_NAME.lower())
    if idx is None:
        header.append(TITLE_COLUMN_NAME)
        idx = len(header) - 1
        for row in rows:
            if len(row) < len(header):
                row.extend([""] * (len(header) - len(row)))
    for row in rows:
        if len(row) < len(header):
            row.extend([""] * (len(header) - len(row)))
        row[idx] = ""
    return idx


def ensure_relevant_news_column(header: List[str], rows: List[List[str]]) -> int:
    header_map = build_header_map(header)
    idx = header_map.get(RELEVANT_NEWS_COLUMN_NAME.lower())
    if idx is None:
        header.append(RELEVANT_NEWS_COLUMN_NAME)
        idx = len(header) - 1
        for row in rows:
            if len(row) < len(header):
                row.extend([""] * (len(header) - len(row)))
    for row in rows:
        if len(row) < len(header):
            row.extend([""] * (len(header) - len(row)))
        row[idx] = ""
    return idx


def truncate_text(value: str, limit: int) -> str:
    normalized = (value or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def parse_timecode(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    tokenized = value.strip().split(":")
    if len(tokenized) < 3:
        return None
    try:
        hours = int(tokenized[0])
        minutes = int(tokenized[1])
        seconds = float(tokenized[2])
        frames = float(tokenized[3]) if len(tokenized) > 3 else 0.0
    except ValueError:
        return None
    total = hours * 3600 + minutes * 60 + seconds
    if len(tokenized) > 3:
        total += frames / 25.0
    return float(total)


def estimate_row_duration(start_value: Optional[str], end_value: Optional[str]) -> float:
    start_seconds = parse_timecode(start_value)
    end_seconds = parse_timecode(end_value)
    if start_seconds is None or end_seconds is None:
        return DEFAULT_ESTIMATED_DURATION
    delta = end_seconds - start_seconds
    if delta <= 0:
        return max(MIN_DURATION_SECONDS, DEFAULT_ESTIMATED_DURATION)
    return max(delta, MIN_DURATION_SECONDS)


def compute_kept_row_durations(
    rows: Sequence[Sequence[str]],
    keep_idx: int,
    start_idx: Optional[int],
    end_idx: Optional[int],
) -> Tuple[List[float], float]:
    durations: List[float] = [0.0] * len(rows)
    total = 0.0
    for idx, row in enumerate(rows):
        if keep_idx >= len(row):
            continue
        keep_value = row[keep_idx].strip()
        if not keep_value:
            continue
        start_value = row[start_idx] if start_idx is not None and start_idx < len(row) else None
        end_value = row[end_idx] if end_idx is not None and end_idx < len(row) else None
        duration = estimate_row_duration(start_value, end_value)
        durations[idx] = duration
        total += duration
    return durations, total


def allocate_news_targets(durations: Sequence[float]) -> Dict[int, int]:
    assignments: Dict[int, int] = {}
    accumulator = 0.0
    for idx, duration in enumerate(durations):
        if duration <= 0:
            continue
        accumulator += duration * NEWS_RATE_PER_SECOND
        if accumulator >= 0.999:
            raw_target = int(accumulator)
            target = max(1, min(raw_target, MAX_NEWS_PER_ROW))
            assignments[idx] = target
            accumulator -= target
    if not assignments:
        first_idx = next((idx for idx, value in enumerate(durations) if value > 0), None)
        if first_idx is not None:
            assignments[first_idx] = 1
    return assignments


def has_minimum_quote_content(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < MIN_QUOTE_CONTENT_LEN:
        return False
    return len(WORD_TOKEN_PATTERN.findall(stripped)) >= MIN_QUOTE_WORD_COUNT


def detect_quotes_in_text(analysis_text: str) -> List[Dict[str, object]]:
    quotes: List[Dict[str, object]] = []
    seen: set = set()
    for pattern in QUOTE_PATTERNS:
        regex = re.compile(pattern, re.DOTALL)
        for match in regex.finditer(analysis_text):
            inner = match.group(1).strip()
            if not has_minimum_quote_content(inner):
                continue
            start = match.start(0)
            end = match.end(0)
            while start < end and analysis_text[start].isspace():
                start += 1
            while end > start and analysis_text[end - 1].isspace():
                end -= 1
            snippet = analysis_text[start:end]
            if not snippet:
                continue
            key = (start, snippet)
            if key in seen:
                continue
            seen.add(key)
            quotes.append({"text": snippet, "start_index": start})
    quotes.sort(key=lambda item: item["start_index"])
    return quotes


def _clean_institution_snippet(value: str) -> str:
    cleaned = value.strip(" \t\r\n\"'\u201c\u201d\u201e\u201f\u2039\u203a\u00ab\u00bb()[]{}-\u2013\u2014,.:;")
    return re.sub(r"\s+", " ", cleaned)


def detect_institution_candidates(analysis_text: str) -> List[Dict[str, object]]:
    if not analysis_text:
        return []
    candidates: List[Dict[str, object]] = []
    seen: set = set()
    for pattern in INSTITUTION_EXACT_PATTERNS:
        for match in pattern.finditer(analysis_text):
            snippet = _clean_institution_snippet(match.group(0))
            if not snippet:
                continue
            start = match.start()
            key = (start, snippet.lower())
            if key in seen:
                continue
            seen.add(key)
            candidates.append({"text": snippet, "start_index": start})
    for match in INSTITUTION_GENERIC_PATTERN.finditer(analysis_text):
        snippet = match.group(0)
        if not snippet:
            continue
        cleaned = _clean_institution_snippet(snippet)
        if not cleaned or len(cleaned) < 4:
            continue
        start = match.start()
        key = (start, cleaned.lower())
        if key in seen:
            continue
        seen.add(key)
        candidates.append({"text": cleaned, "start_index": start})
    candidates.sort(key=lambda item: item["start_index"])
    return candidates


def _month_regex_for_language(language: str) -> Optional[str]:
    month_names = LANGUAGE_MONTH_NAMES.get(language, ALL_MONTH_NAMES)
    if not month_names:
        return None
    unique = sorted(list(dict.fromkeys(month_names)), key=len, reverse=True)
    return "|".join(re.escape(name) for name in unique)


def detect_date_candidates(analysis_text: str, language: str) -> Tuple[List[Dict[str, object]], List[Tuple[int, int]]]:
    if not analysis_text:
        return [], []
    candidates: List[Dict[str, object]] = []
    spans: List[Tuple[int, int]] = []
    seen: set = set()

    def record(start: int, end: int) -> None:
        snippet = analysis_text[start:end].strip()
        if len(snippet) < 2:
            return
        key = (start, snippet.lower())
        if key in seen:
            return
        seen.add(key)
        candidates.append({"text": snippet, "start_index": start})
        spans.append((start, end))

    month_pattern = _month_regex_for_language(language)
    if month_pattern:
        day_month = re.compile(rf"\b\d{{1,2}}(?:er|e)?\s+(?:{month_pattern})(?:\s+\d{{2,4}})?", re.IGNORECASE)
        for match in day_month.finditer(analysis_text):
            record(match.start(), match.end())
        just_month = re.compile(rf"\b(?:{month_pattern})\s+\d{{4}}\b", re.IGNORECASE)
        for match in just_month.finditer(analysis_text):
            record(match.start(), match.end())
    for regex in (SLASH_DATE_PATTERN, ISO_DATE_PATTERN, YEAR_PATTERN):
        for match in regex.finditer(analysis_text):
            record(match.start(), match.end())
    candidates.sort(key=lambda item: item["start_index"])
    spans.sort()
    return candidates, spans


def detect_number_candidates(analysis_text: str, excluded_spans: Optional[Sequence[Tuple[int, int]]] = None) -> List[Dict[str, object]]:
    if not analysis_text:
        return []
    candidates: List[Dict[str, object]] = []
    seen: set = set()
    excluded_spans = excluded_spans or []

    def overlaps(start: int, end: int) -> bool:
        for span_start, span_end in excluded_spans:
            if start < span_end and end > span_start:
                return True
        return False

    def record(start: int, end: int) -> None:
        if overlaps(start, end):
            return
        snippet = analysis_text[start:end].strip()
        if len(snippet) < 1:
            return
        key = (start, snippet.lower())
        if key in seen:
            return
        seen.add(key)
        candidates.append({"text": snippet, "start_index": start})

    for regex in (GROUPED_NUMBER_PATTERN, SIMPLE_NUMBER_PATTERN):
        for match in regex.finditer(analysis_text):
            record(match.start(), match.end())
    candidates.sort(key=lambda item: item["start_index"])
    return candidates


def _is_money_mention(value: str) -> bool:
    if not value:
        return False
    if CURRENCY_SYMBOL_PATTERN.search(value):
        return True
    return bool(CURRENCY_KEYWORD_PATTERN.search(value))


def _looks_like_date(text: str, language: str) -> bool:
    normalized = text.lower()
    month_pattern = _month_regex_for_language(language)
    if month_pattern and re.search(rf"\b(?:{month_pattern})\b", normalized):
        return True
    if SLASH_DATE_PATTERN.search(text) or ISO_DATE_PATTERN.search(text) or YEAR_PATTERN.search(text):
        return True
    return False


def merge_annotation_entries(
    primary: object,
    fallback: object,
    *,
    count_fallback: bool = True,
) -> Tuple[List[Dict[str, object]], int]:
    merged: List[Dict[str, object]] = []
    seen: set = set()
    added = 0

    def consume(entries: object, count_as_new: bool) -> None:
        nonlocal added
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            text_value = entry.get("text")
            start_index = entry.get("start_index")
            if not isinstance(text_value, str) or not isinstance(start_index, int):
                continue
            cleaned = text_value.strip()
            if not cleaned:
                continue
            key = (start_index, cleaned.lower())
            if key in seen:
                continue
            seen.add(key)
            merged.append({"text": cleaned, "start_index": start_index})
            if count_as_new:
                added += 1

    consume(primary, False)
    consume(fallback, count_fallback)
    merged.sort(key=lambda item: item["start_index"])
    return merged, added


def split_legacy_number_entries(data: MutableMapping[str, object], language: str) -> None:
    legacy_entries = data.pop("number_or_date", None)
    if not isinstance(legacy_entries, list):
        return
    numbers: List[Dict[str, object]] = []
    dates: List[Dict[str, object]] = []
    for entry in legacy_entries:
        if not isinstance(entry, Mapping):
            continue
        text_value = entry.get("text")
        start_index = entry.get("start_index")
        if not isinstance(text_value, str) or not isinstance(start_index, int):
            continue
        bucket = dates if _looks_like_date(text_value, language) else numbers
        bucket.append({"text": text_value.strip(), "start_index": start_index})
    if dates:
        merged, _ = merge_annotation_entries(dates, data.get("date"), count_fallback=False)
        data["date"] = merged
    if numbers:
        merged, _ = merge_annotation_entries(numbers, data.get("number"), count_fallback=False)
        data["number"] = merged


def split_money_number_mentions(data: MutableMapping[str, object]) -> None:
    entries = data.get("number")
    if not isinstance(entries, list):
        return
    money_entries: List[Dict[str, object]] = []
    general_entries: List[Dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        text_value = entry.get("text")
        start_index = entry.get("start_index")
        if not isinstance(text_value, str) or not isinstance(start_index, int):
            continue
        cleaned = text_value.strip()
        if not cleaned:
            continue
        bucket = money_entries if _is_money_mention(cleaned) else general_entries
        bucket.append({"text": cleaned, "start_index": start_index})
    merged_numbers, _ = merge_annotation_entries(general_entries, None, count_fallback=False)
    data["number"] = merged_numbers
    if money_entries or isinstance(data.get("money"), list):
        merged_money, _ = merge_annotation_entries(money_entries, data.get("money"), count_fallback=False)
        data["money"] = merged_money


def _contains_blocked_term(value: str, blocked_terms: set) -> bool:
    if not value:
        return False
    padded_value = f" {value} "
    for term in blocked_terms:
        if not term:
            continue
        if value == term or value.startswith(f"{term} ") or value.endswith(f" {term}") or f" {term} " in padded_value:
            return True
    return False


def suppress_default_country_mentions(data: MutableMapping[str, object], language: str) -> None:
    blocked_terms = LANGUAGE_HOME_COUNTRIES_NORMALIZED.get(language)
    if not blocked_terms:
        return
    locations = data.get("location")
    if not isinstance(locations, list):
        return
    filtered: List[object] = []
    removed = 0
    for entry in locations:
        if isinstance(entry, Mapping):
            text_value = entry.get("text")
            if isinstance(text_value, str):
                normalized_value = _normalize_geo_term(text_value)
                if normalized_value and _contains_blocked_term(normalized_value, blocked_terms):
                    removed += 1
                    continue
        filtered.append(entry)
    if removed:
        data["location"] = filtered


def apply_language_location_overrides(data: MutableMapping[str, object], language: str) -> None:
    overrides = LANGUAGE_LOCATION_OVERRIDE_LOOKUP.get(language)
    if not overrides:
        return
    entries = data.get("location")
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, MutableMapping):
            continue
        text_value = entry.get("text")
        if not isinstance(text_value, str):
            continue
        normalized_value = _normalize_geo_term(text_value)
        if not normalized_value:
            continue
        override = overrides.get(normalized_value)
        if override and text_value.strip() != override:
            entry["text"] = override


def limit_unique_entries(entries: List[Dict[str, object]], max_unique: int) -> List[Dict[str, object]]:
    if max_unique <= 0:
        return []
    filtered: List[Dict[str, object]] = []
    seen: set = set()
    for entry in entries:
        text_value = entry.get("text")
        if not isinstance(text_value, str):
            continue
        normalized = text_value.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        filtered.append(entry)
        if len(filtered) >= max_unique:
            break
    return filtered


def normalize_feeling_annotations(data: MutableMapping[str, object]) -> None:
    entries = data.get("feeling")
    if entries is None:
        return
    if not isinstance(entries, list):
        data["feeling"] = []
        return
    filtered: List[Dict[str, object]] = []
    seen: set = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        text_value = entry.get("text")
        if not isinstance(text_value, str):
            continue
        cleaned_text = text_value.strip()
        if not cleaned_text:
            continue
        start_index = entry.get("start_index")
        normalized_index: Optional[int] = start_index if isinstance(start_index, int) else None
        fingerprint = (cleaned_text.lower(), normalized_index)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        payload: Dict[str, object] = {"text": cleaned_text}
        if normalized_index is not None:
            payload["start_index"] = normalized_index
        filtered.append(payload)
    data["feeling"] = filtered


def extract_mentions_with_claude(
    analysis_text: str,
    api_key: str,
    model: str,
    max_tokens: int,
    language: str,
) -> MutableMapping[str, object]:
    if not analysis_text:
        return {}
    if anthropic is None:
        raise RuntimeError("Install the `anthropic` package.")
    profile = LANGUAGE_PROFILES.get(language, LANGUAGE_PROFILES["en"])
    system_prompt = profile["system"]
    user_template = profile["user"]
    client = anthropic.Anthropic(api_key=api_key)
    user_prompt = user_template.format(reference_text=escape_braces(analysis_text))
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    content = extract_response_text(message)
    data = parse_claude_json(content)
    fallback_quotes = detect_quotes_in_text(analysis_text)
    quotes = data.get("quote")
    if not isinstance(quotes, list) or not quotes:
        if fallback_quotes:
            data["quote"] = fallback_quotes
    return data


def extract_targeted_annotations(
    kind: str,
    analysis_text: str,
    api_key: str,
    model: str,
    max_tokens: int,
    language: str,
) -> List[Dict[str, object]]:
    specs = TARGETED_EXTRACTION_SPECS.get(kind)
    if not specs:
        return []
    locale = specs.get(language) or specs.get("en")
    if not locale or not analysis_text:
        return []
    if anthropic is None:
        raise RuntimeError("Install the `anthropic` package.")
    client = anthropic.Anthropic(api_key=api_key)
    user_prompt = locale["user"].format(reference_text=escape_braces(analysis_text))
    capped_tokens = max(300, min(max_tokens, TARGETED_MAX_TOKENS))
    message = client.messages.create(
        model=model,
        max_tokens=capped_tokens,
        temperature=0,
        system=locale["system"],
        messages=[{"role": "user", "content": user_prompt}],
    )
    content = extract_response_text(message)
    data = parse_claude_json(content)
    items = data.get("items")
    results: List[Dict[str, object]] = []
    if not isinstance(items, list):
        return results
    for entry in items:
        if not isinstance(entry, Mapping):
            continue
        text_value = entry.get("text")
        start_index = entry.get("start_index")
        if not isinstance(text_value, str):
            continue
        cleaned = text_value.strip()
        if not cleaned:
            continue
        payload: Dict[str, object] = {"text": cleaned}
        if isinstance(start_index, int):
            payload["start_index"] = start_index
        results.append(payload)
    return results


def extract_titles_from_html(html_path: Path) -> List[str]:
    html_text = html_path.read_text(encoding="utf-8")
    titles: List[str] = []
    seen_norm: set = set()
    if BeautifulSoup is not None:
        soup = BeautifulSoup(html_text, "html.parser")
        for element in soup.find_all(True):
            name = element.name.lower() if element.name else ""
            classes = [cls.lower() for cls in element.get("class", [])]
            if "title" not in classes and name not in TITLE_TAGS:
                continue
            text = element.get_text(" ", strip=True)
            cleaned = re.sub(r"\s+", " ", text).strip()
            if not cleaned:
                continue
            normalized = normalize_for_matching(cleaned).lower()
            if not normalized or normalized in seen_norm:
                continue
            seen_norm.add(normalized)
            titles.append(cleaned)
    else:
        parser = _FallbackTitleExtractor()
        parser.feed(html_text)
        parser.close()
        for raw in parser.titles:
            cleaned = re.sub(r"\s+", " ", raw).strip()
            if not cleaned:
                continue
            normalized = normalize_for_matching(cleaned).lower()
            if not normalized or normalized in seen_norm:
                continue
            seen_norm.add(normalized)
            titles.append(cleaned)
    return titles


def locate_titles_in_text(titles: Sequence[str], analysis_text: str) -> List[Dict[str, object]]:
    located: List[Dict[str, object]] = []
    cursor = 0
    for title in titles:
        normalized = normalize_for_matching(title)
        if not normalized:
            continue
        idx = analysis_text.find(normalized, cursor)
        if idx == -1:
            idx = analysis_text.find(normalized)
        if idx == -1:
            continue
        located.append({"text": title, "start_index": idx})
        cursor = idx + len(normalized)
    return located


def build_row_reference_spans(
    rows: Sequence[Sequence[str]],
    ref_idx: int,
    analysis_text: str,
) -> List[Optional[Tuple[int, int]]]:
    spans: List[Optional[Tuple[int, int]]] = []
    cursor = 0
    for row in rows:
        snippet = normalize_for_matching(row[ref_idx] if ref_idx < len(row) else "")
        if not snippet:
            spans.append(None)
            continue
        idx = analysis_text.find(snippet, cursor)
        if idx == -1:
            idx = analysis_text.find(snippet)
            if idx == -1:
                spans.append(None)
                continue
        spans.append((idx, idx + len(snippet)))
        cursor = idx + len(snippet)
    return spans


def find_row_for_offset(spans: Sequence[Optional[Tuple[int, int]]], offset: Optional[int]) -> Optional[int]:
    if offset is None:
        return None
    for idx, span in enumerate(spans):
        if not span:
            continue
        start, end = span
        if start <= offset < end:
            return idx
    return None


def find_row_for_offset_or_neighbor(spans: Sequence[Optional[Tuple[int, int]]], offset: Optional[int]) -> Optional[int]:
    direct = find_row_for_offset(spans, offset)
    if direct is not None or offset is None:
        return direct
    next_idx: Optional[int] = None
    next_start: Optional[int] = None
    for idx, span in enumerate(spans):
        if not span:
            continue
        start, _ = span
        if start >= offset and (next_start is None or start < next_start):
            next_start = start
            next_idx = idx
    if next_idx is not None:
        return next_idx
    prev_idx: Optional[int] = None
    prev_end: Optional[int] = None
    for idx, span in enumerate(spans):
        if not span:
            continue
        _, end = span
        if end <= offset and (prev_end is None or end > prev_end):
            prev_end = end
            prev_idx = idx
    return prev_idx


def fallback_row_lookup(
    rows: Sequence[Sequence[str]],
    ref_idx: int,
    text_idx: int,
    target: str,
) -> Optional[int]:
    normalized_target = normalize_for_matching(target)
    if not normalized_target:
        return None
    target_no_punct = normalized_target.replace(".", "")
    for idx, row in enumerate(rows):
        ref_text = normalize_for_matching(row[ref_idx] if ref_idx < len(row) else "")
        ref_no_punct = ref_text.replace(".", "") if ref_text else ""
        if ref_text and (normalized_target in ref_text or ref_text in normalized_target
                         or (ref_no_punct and ref_no_punct in target_no_punct)):
            return idx
        transcript_text = normalize_for_matching(row[text_idx] if text_idx < len(row) else "")
        transcript_no_punct = transcript_text.replace(".", "") if transcript_text else ""
        if transcript_text and (normalized_target in transcript_text or transcript_text in normalized_target
                                 or (transcript_no_punct and transcript_no_punct in target_no_punct)):
            return idx
    return None


def map_mentions_to_rows(
    data: Mapping[str, object],
    spans: Sequence[Optional[Tuple[int, int]]],
    rows: Sequence[Sequence[str]],
    ref_idx: int,
    text_idx: int,
) -> Dict[int, Dict[str, List[str]]]:
    annotations: Dict[int, Dict[str, List[str]]] = {}
    for key, _ in FEATURE_SPECS:
        entries = data.get(key, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            text_value = entry.get("text")
            if not isinstance(text_value, str):
                continue
            start_index = entry.get("start_index")
            position: Optional[int] = start_index if isinstance(start_index, int) and start_index >= 0 else None
            row_idx = find_row_for_offset(spans, position)
            if row_idx is None:
                row_idx = fallback_row_lookup(rows, ref_idx, text_idx, text_value)
            if row_idx is None:
                continue
            row_features = annotations.setdefault(row_idx, {})
            row_features.setdefault(key, []).append(text_value.strip())
    return annotations


def map_titles_to_rows(
    titles: Sequence[Mapping[str, object]],
    spans: Sequence[Optional[Tuple[int, int]]],
    rows: Sequence[Sequence[str]],
    ref_idx: int,
    text_idx: int,
) -> Dict[int, List[str]]:
    assignments: Dict[int, List[str]] = {}
    for entry in titles:
        text_value = entry.get("text") if isinstance(entry, Mapping) else None
        if not isinstance(text_value, str):
            continue
        start_index = entry.get("start_index") if isinstance(entry, Mapping) else None
        offset: Optional[int] = start_index if isinstance(start_index, int) else None
        row_idx = find_row_for_offset_or_neighbor(spans, offset)
        if row_idx is None:
            row_idx = fallback_row_lookup(rows, ref_idx, text_idx, text_value)
        if row_idx is None:
            continue
        bucket = assignments.setdefault(row_idx, [])
        normalized = text_value.strip()
        if normalized and normalized not in bucket:
            bucket.append(normalized)
    return assignments


def apply_feature_values(
    rows: List[List[str]],
    annotations: Mapping[int, Mapping[str, List[str]]],
    column_positions: Mapping[str, int],
) -> None:
    for idx, row in enumerate(rows):
        features = annotations.get(idx, {})
        for key, column_name in FEATURE_SPECS:
            column_idx = column_positions[column_name]
            values = features.get(key, [])
            if not values:
                row[column_idx] = ""
                continue
            deduped: List[str] = []
            for value in values:
                if key == "quote" and not has_minimum_quote_content(value):
                    continue
                if value not in deduped:
                    deduped.append(value)
            row[column_idx] = " | ".join(deduped)


def apply_title_annotations(
    rows: List[List[str]],
    title_map: Mapping[int, Sequence[str]],
    column_idx: int,
) -> None:
    for row_idx, titles in title_map.items():
        if row_idx < 0 or row_idx >= len(rows):
            continue
        row = rows[row_idx]
        if len(row) <= column_idx:
            row.extend([""] * (column_idx + 1 - len(row)))
        filtered: List[str] = []
        for value in titles:
            if value and value not in filtered:
                filtered.append(value)
        row[column_idx] = " | ".join(filtered)


def restrict_tag_columns_to_kept_rows(
    header: Sequence[str],
    rows: List[List[str]],
) -> None:
    header_map = build_header_map(header)
    keep_idx = header_map.get(KEEP_COLUMN_NAME.lower())
    if keep_idx is None:
        return
    tag_indices = [
        header_map.get(name.strip().lower())
        for name in TAGS_RESTRICTED_TO_KEPT_ROWS
        if name.strip()
    ]
    tag_indices = [idx for idx in tag_indices if idx is not None]
    if not tag_indices:
        return
    for row in rows:
        if keep_idx >= len(row):
            continue
        keep_value = (row[keep_idx] or "").strip().upper()
        if keep_value == "X":
            continue
        for idx in tag_indices:
            if idx >= len(row):
                continue
            row[idx] = ""


def build_reference_summary(reference_text: str) -> str:
    normalized = (reference_text or "").strip()
    return normalized[:2400] if len(normalized) > 2400 else normalized


def extract_reference_window(analysis_text: str, span: Optional[Tuple[int, int]], window: int = 800) -> str:
    if not analysis_text or not span:
        return ""
    start, end = span
    if start < 0 or end <= start:
        return ""
    half = max(120, window // 2)
    left = max(0, start - half)
    right = min(len(analysis_text), end + half)
    return analysis_text[left:right].strip()


def build_news_context_block(
    row: Sequence[str],
    row_idx: int,
    start_idx: Optional[int],
    end_idx: Optional[int],
    text_idx: int,
    ref_idx: int,
    reference_summary: Optional[str] = None,
    reference_context: Optional[str] = None,
) -> str:
    start_value = row[start_idx] if start_idx is not None and start_idx < len(row) else ""
    end_value = row[end_idx] if end_idx is not None and end_idx < len(row) else ""
    transcript = row[text_idx] if text_idx < len(row) else ""
    reference = row[ref_idx] if ref_idx < len(row) else ""
    blocks = [
        f"Row #{row_idx + 1}",
        f"Start: {start_value or 'n/a'} | End: {end_value or 'n/a'}",
        "",
        "Transcript excerpt:",
        truncate_text(transcript, 900) or "[no transcript available]",
        "",
        "Reference segment:",
        truncate_text(reference, 900) or "[no reference available]",
    ]
    if reference_context:
        blocks.extend(["", "Extended reference context:", truncate_text(reference_context, 1200)])
    if reference_summary:
        blocks.extend(["", "Global reference recap:", reference_summary])
    return "\n".join(blocks).strip()


def request_relevant_news(
    client,
    context_block: str,
    target_count: int,
    model: str,
    max_tokens: int,
    language_name: str,
) -> List[Dict[str, str]]:
    if target_count <= 0:
        return []
    template = textwrap.dedent("""
        You are working for Parole d'honneur. Based on the context below, list up to {target_count}
        real, reputable news articles (Le Monde, AFP, Reuters, etc.) that corroborate the facts.
        Prefer February 2026 coverage. Respond strictly in JSON as {{
          "articles": [
            {{"title": "...", "url": "https://...", "source": "Outlet", "published_at": "YYYY-MM-DD", "summary": "reason"}}
          ]
        }}.
        Answer in {language_name}.

        Row context:
        {context_block}
    """).strip()
    user_prompt = template.format(
        target_count=target_count,
        language_name=language_name,
        context_block=escape_braces(context_block),
    )
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0,
        system=NEWS_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    content = extract_response_text(message)
    data = parse_claude_json(content)
    articles = data.get("articles")
    results: List[Dict[str, str]] = []
    if isinstance(articles, list):
        for entry in articles:
            if not isinstance(entry, Mapping):
                continue
            title = str(entry.get("title") or "").strip()
            url = str(entry.get("url") or "").strip()
            if not title or not url:
                continue
            results.append({
                "title": title,
                "url": url,
                "source": str(entry.get("source") or "").strip(),
                "published_at": str(entry.get("published_at") or "").strip(),
                "summary": str(entry.get("summary") or "").strip(),
            })
    return results


def generate_relevant_news_annotations(
    rows: Sequence[Sequence[str]],
    assignments: Mapping[int, int],
    text_idx: int,
    ref_idx: int,
    start_idx: Optional[int],
    end_idx: Optional[int],
    spans: Sequence[Optional[Tuple[int, int]]],
    analysis_text: str,
    api_key: str,
    model: str,
    max_tokens: int,
    language_name: str,
    reference_summary: str,
) -> Dict[int, List[Dict[str, str]]]:
    if not assignments:
        return {}
    if anthropic is None:
        raise RuntimeError("Install the `anthropic` package.")
    client = anthropic.Anthropic(api_key=api_key)
    results: Dict[int, List[Dict[str, str]]] = {}
    for row_idx, target in assignments.items():
        if row_idx < 0 or row_idx >= len(rows):
            continue
        span = spans[row_idx] if row_idx < len(spans) else None
        reference_context = extract_reference_window(analysis_text, span)
        context_block = build_news_context_block(
            rows[row_idx], row_idx, start_idx, end_idx, text_idx, ref_idx, reference_summary, reference_context,
        )
        entries = request_relevant_news(client, context_block, target, model, max_tokens, language_name)
        if entries:
            results[row_idx] = entries
            continue
        fallback_entries = request_relevant_news(
            client,
            context_block + "\n\nIMPORTANT : Fournis au moins un article même s'il est antérieur à 2026.",
            max(1, target), model, max_tokens, language_name,
        )
        if fallback_entries:
            results[row_idx] = fallback_entries
    return results


def format_news_cell(items: Sequence[Mapping[str, str]]) -> str:
    urls: List[str] = []
    for entry in items:
        url = str(entry.get("url") or "").strip()
        if url and url not in urls:
            urls.append(url)
    return " | ".join(urls)


def apply_relevant_news(
    rows: List[List[str]],
    column_idx: int,
    assignments: Mapping[int, Sequence[Mapping[str, str]]],
) -> None:
    for row_idx, entries in assignments.items():
        if row_idx < 0 or row_idx >= len(rows):
            continue
        rows[row_idx][column_idx] = format_news_cell(entries)


def _reference_fallback_from_rows(
    rows: Sequence[Sequence[str]],
    reference_idx: int,
    text_idx: int,
) -> str:
    content: List[str] = []
    for row in rows:
        candidate = ""
        if 0 <= reference_idx < len(row):
            candidate = row[reference_idx].strip()
        if not candidate and 0 <= text_idx < len(row):
            candidate = row[text_idx].strip()
        if candidate:
            content.append(candidate)
    if not content:
        raise RuntimeError("Unable to build a fallback reference from the comparison CSV.")
    return "\n".join(content)


# ---------------------------------------------------------------------------
# HTML metrics (from groq_html_comparser)
# ---------------------------------------------------------------------------

def detect_hashtags(*sources: Optional[str]) -> List[str]:
    collected: List[str] = []
    seen: set = set()
    for source in sources:
        if not source:
            continue
        for match in HASHTAG_PATTERN.findall(source):
            hashtag = f"#{match}"
            lowered = hashtag.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            collected.append(hashtag)
    return collected


def detect_pattern_matches(pattern: re.Pattern, *sources: Optional[str]) -> List[str]:
    values: List[str] = []
    seen: set = set()
    for source in sources:
        if not source:
            continue
        for match in pattern.findall(source):
            cleaned = re.sub(r"\s+", " ", (match if isinstance(match, str) else " ".join(match))).strip()
            if not cleaned:
                continue
            lowered = cleaned.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            values.append(cleaned)
    return values


def classify_weight_entries(entries: Iterable[str]) -> Tuple[List[str], List[str]]:
    object_values: List[str] = []
    person_values: List[str] = []
    for entry in entries:
        normalized = entry.lower()
        if "tonne" in normalized or normalized.endswith(" t"):
            object_values.append(entry)
        elif "kg" in normalized or "kilogram" in normalized or "livre" in normalized:
            person_values.append(entry)
        else:
            object_values.append(entry)
    return object_values, person_values


_ARTICLE_PATH_PATTERN = re.compile(
    r"(?:"
    r"/\d{4}/\d{1,2}/"                               # date in path: /2024/01/
    r"|/\d{4}-\d{2}-\d{2}[/-]"                       # ISO date: /2024-01-15/
    r"|/(article|articles|post|posts|news|blog|story|stories|actualite|actualites)/"
    r")",
    re.IGNORECASE,
)
_ARTICLE_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){4,}$", re.IGNORECASE)


def _url_looks_like_article(path: str) -> bool:
    """Return True if the URL path pattern suggests a specific article rather than a generic homepage."""
    if _ARTICLE_PATH_PATTERN.search(path):
        return True
    slug = path.rstrip("/").rsplit("/", 1)[-1]
    if _ARTICLE_SLUG_RE.match(slug):
        return True
    return False


def categorize_link(href: str) -> str:
    cleaned = href.strip()
    if not cleaned:
        return "article"
    candidates = [cleaned]
    parsed = urlparse(cleaned)
    if parsed.query:
        query = parse_qs(parsed.query)
        for key in ("q", "url", "imgurl", "mediaurl"):
            for value in query.get(key, []):
                candidate = unquote(value).strip()
                if candidate.startswith(("http://", "https://")) and candidate not in candidates:
                    candidates.append(candidate)
    for candidate in candidates:
        lower = candidate.lower()
        candidate_path = urlparse(candidate).path.lower()
        for extension in URL_VIDEO_EXTENSIONS:
            if lower.endswith(extension) or candidate_path.endswith(extension):
                return "video"
        for extension in URL_IMAGE_EXTENSIONS:
            if lower.endswith(extension) or candidate_path.endswith(extension):
                return "image"
        for host in VIDEO_HOSTS:
            if host in lower:
                return "video"
    # Distinguish generic website homepages from specific article URLs.
    # Check all candidates so Google-redirect URLs (google.com/url?q=…) resolve correctly.
    for candidate in candidates:
        if _url_looks_like_article(urlparse(candidate).path):
            return "article"
    return "website"


def parse_html_feature_bundle(html_path: Optional[Path]) -> Dict[str, object]:
    bundle: Dict[str, object] = {
        "hashtags": set(),
        "article_links": set(),
        "video_links": set(),
        "image_links": set(),
        "bullet_points": [],
        "blocks": [],
        "bold_texts": [],
        "italic_texts": [],
        "underline_texts": [],
        "list_groups": [],
    }
    if not html_path or not html_path.exists():
        return bundle
    html_text = html_path.read_text(encoding="utf-8", errors="ignore")
    parser = HtmlFeatureExtractor(parse_css_class_styles(html_text))
    parser.feed(html_text)
    parser.close()
    article_links: set[str] = set()
    video_links: set[str] = set()
    image_links: set[str] = set()
    for link in parser.links:
        categorized = categorize_link(link)
        if categorized == "video":
            video_links.add(link)
        elif categorized == "image":
            image_links.add(link)
        else:
            article_links.add(link)
    blocks: List[Dict[str, object]] = []
    for parsed_block in parser.blocks:
        if not parsed_block.get("text"):
            continue
        block = dict(parsed_block)
        line_entries = extract_line_list_entries(str(block.get("raw_text") or ""))
        if len(line_entries) >= LIST_MIN_ITEMS:
            block["marker_signals"] = []
            block["list_type"] = ""
        blocks.append(block)
        if len(line_entries) >= LIST_MIN_ITEMS:
            for entry in line_entries:
                blocks.append(
                    {
                        "tag": "li",
                        "text": entry["text"],
                        "norm_text": entry["norm_text"],
                        "raw_text": entry["text"],
                        "bold_parts": [],
                        "italic_parts": [],
                        "underline_parts": [],
                        "marker_signals": entry["marker_signals"],
                        "list_type": entry["list_type"],
                    }
                )
    for index, block in enumerate(blocks):
        block["block_index"] = index

    list_groups = build_list_groups(blocks)
    bundle["hashtags"] = set(parser.hashtags)
    bundle["article_links"] = article_links
    bundle["video_links"] = video_links
    bundle["image_links"] = image_links
    bundle["bullet_points"] = [str(block["text"]) for block in blocks if block.get("marker_signals")]
    bundle["blocks"] = blocks
    bundle["list_groups"] = list_groups
    bundle["bold_texts"] = dedupe_texts(
        text for block in blocks for text in block.get("bold_parts", [])
    )
    bundle["italic_texts"] = dedupe_texts(
        text for block in blocks for text in block.get("italic_parts", [])
    )
    bundle["underline_texts"] = dedupe_texts(
        text for block in blocks for text in block.get("underline_parts", [])
    )
    return bundle


def parse_html_context(html_path: Optional[Path]) -> Dict[str, object]:
    bundle = parse_html_feature_bundle(html_path)
    return {
        "hashtags": bundle["hashtags"],
        "article_links": bundle["article_links"],
        "video_links": bundle["video_links"],
        "image_links": bundle["image_links"],
        "bullet_points": bundle["bullet_points"],
    }


def load_country_index(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    lookup: Dict[str, str] = {}
    for iso_code, name in data.items():
        for candidate in (iso_code, name):
            normalized = unicodedata.normalize("NFKD", candidate.lower())
            normalized = "".join(c for c in normalized if not unicodedata.combining(c))
            tokens = re.findall(r"[a-z0-9]+", normalized)
            key = " ".join(tokens)
            if key:
                lookup[key] = iso_code.lower()
    return lookup


COUNTRY_INDEX: Dict[str, str] = load_country_index(COUNTRY_META.expanduser())


def normalized_key(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    tokens = re.findall(r"[a-z0-9]+", text)
    return " ".join(tokens)


def classify_locations(value: Optional[str]) -> Tuple[List[str], List[str]]:
    cities: List[str] = []
    countries: List[str] = []
    for fragment in split_pipe_values(value):
        key = normalized_key(fragment)
        if key in COUNTRY_INDEX:
            countries.append(fragment)
        else:
            cities.append(fragment)
    return cities, countries


def update_quote_column(rows: List[List[str]], header_map: Mapping[str, int]) -> None:
    quote_idx = header_map.get(QUOTE_COLUMN_NAME.lower())
    if quote_idx is None:
        return
    text_idx = header_map.get(TEXT_COLUMN_NAME.lower())
    ref_idx = header_map.get(REFERENCE_COLUMN_NAME.lower())
    for row in rows:
        if quote_idx >= len(row):
            continue
        if row[quote_idx].strip():
            continue
        sources = []
        if text_idx is not None and text_idx < len(row):
            sources.append(row[text_idx])
        if ref_idx is not None and ref_idx < len(row):
            sources.append(row[ref_idx])
        quotes = [
            entry["text"].strip()
            for entry in detect_quotes_in_text(" ".join(source for source in sources if source))
            if isinstance(entry.get("text"), str)
        ]
        if quotes:
            row[quote_idx] = " | ".join(dedupe_texts(quotes, min_len=MIN_QUOTE_CONTENT_LEN))


def _leading_digits(s: str) -> str:
    """Return the leading digit string from a measurement like '900 km/h' → '900'."""
    m = re.match(r"[\d.,]+", s.strip())
    return m.group() if m else ""


def detect_pure_distances(text: str) -> List[str]:
    """Extract distance values, excluding overlaps with speed or surface pattern spans,
    and also excluding distances whose numeric value is already captured as a speed."""
    if not text:
        return []
    excluded: List[Tuple[int, int]] = []
    for pattern in (SPEED_PATTERN, SURFACE_PATTERN):
        for m in pattern.finditer(text):
            excluded.append((m.start(), m.end()))
    results: List[str] = []
    seen: set = set()
    for m in DISTANCE_PATTERN.finditer(text):
        start, end = m.start(), m.end()
        if any(s <= start < e or s < end <= e for s, e in excluded):
            continue
        suffix = text[end:end + 4]
        if re.match(r"\s*/[hH]|\s*[hH]\b|\s*[²2]", suffix):
            continue
        raw = m.group()
        key = re.sub(r"\s+", "", raw).lower()
        if key not in seen:
            seen.add(key)
            results.append(re.sub(r"\s+", " ", raw).strip())
    # Post-filter: drop any distance whose number is already present in a speed mention.
    # This handles the case where transcript and reference appear at different positions
    # in `combined` (e.g. transcript ends at "900 km" while reference has "900 km/h").
    if results:
        speed_nums = {_leading_digits(m.group()) for m in SPEED_PATTERN.finditer(text)}
        speed_nums.discard("")
        if speed_nums:
            results = [r for r in results if _leading_digits(r) not in speed_nums]
    return results


def detect_concrete_emoji(text: str) -> List[str]:
    if not text:
        return []
    found: List[str] = []
    seen: set = set()
    for emoji_str in CONCRETE_EMOJI_ALL:
        if emoji_str in text and emoji_str not in seen:
            seen.add(emoji_str)
            found.append(emoji_str)
    return found


def extract_bold_text_from_html(html_path: Optional[Path]) -> List[str]:
    return list(parse_html_feature_bundle(html_path).get("bold_texts", []))


def match_html_blocks_to_row(
    row_text: str,
    row_reference: str,
    blocks: Sequence[Mapping[str, object]],
) -> List[Mapping[str, object]]:
    row_norm = normalize_lookup_text(" ".join(part for part in (row_text, row_reference) if part))
    if not row_norm:
        return []
    matches: List[Mapping[str, object]] = []
    for block in blocks:
        block_norm = str(block.get("norm_text", ""))
        if not block_norm:
            continue
        if row_norm in block_norm or block_norm in row_norm:
            matches.append(block)
            continue
        row_tokens = set(row_norm.split())
        block_tokens = set(block_norm.split())
        if row_tokens and block_tokens:
            overlap = len(row_tokens & block_tokens)
            if overlap >= max(3, min(len(row_tokens), len(block_tokens)) // 2):
                matches.append(block)
    return matches


def extract_matching_html_fragments(
    row_norm: str,
    blocks: Sequence[Mapping[str, object]],
    fragment_key: str,
) -> List[str]:
    matches: List[str] = []
    for block in blocks:
        for fragment in block.get(fragment_key, []):
            fragment_text = str(fragment)
            fragment_norm = normalize_lookup_text(fragment_text)
            if fragment_norm and (fragment_norm in row_norm or row_norm in fragment_norm):
                matches.append(fragment_text)
    return dedupe_texts(matches, min_len=2)


_NUMBER_METRIC_PATTERNS: Tuple[re.Pattern, ...] = (
    PERCENT_PATTERN, SPEED_PATTERN, WEIGHT_OBJECT_PATTERN, WEIGHT_PERSON_PATTERN,
    DISTANCE_PATTERN, TEMPERATURE_PATTERN, SURFACE_PATTERN, VOLUME_PATTERN,
    DECIBEL_PATTERN, RANKING_PATTERN,
)


def filter_number_mention_against_metrics(rows: List[List[str]], header: List[str]) -> None:
    """Remove from Number Mention any values already covered by metric-specific columns."""
    header_map = build_header_map(header)
    num_idx = header_map.get("number mention")
    if num_idx is None:
        return
    for row in rows:
        if num_idx >= len(row) or not row[num_idx].strip():
            continue
        values = split_pipe_values(row[num_idx])
        filtered = [val for val in values if not any(p.search(val) for p in _NUMBER_METRIC_PATTERNS)]
        row[num_idx] = " | ".join(filtered)


def enrich_html_metrics(
    header: List[str],
    rows: List[List[str]],
    html_path: Optional[Path],
    manifest_path: Optional[Path],
) -> None:
    header_map = build_header_map(header)
    column_positions = {name: ensure_column(header, rows, name) for name in NEW_COLUMNS}
    text_idx = header_map.get(TEXT_COLUMN_NAME.lower())
    ref_idx = header_map.get(REFERENCE_COLUMN_NAME.lower())
    location_idx = header_map.get(LOCATION_COLUMN_NAME.lower())
    comment_tag_idx = build_header_map(header).get("commentez tag")
    tip_tag_idx = build_header_map(header).get("tippee tag")
    subscribe_tag_idx = build_header_map(header).get("abonnez tag")

    html_bundle = parse_html_feature_bundle(html_path)
    html_context = {
        "hashtags": html_bundle["hashtags"],
        "article_links": html_bundle["article_links"],
        "video_links": html_bundle["video_links"],
        "image_links": html_bundle["image_links"],
        "bullet_points": html_bundle["bullet_points"],
    }
    html_blocks = list(html_bundle.get("blocks", []))
    _html_full_text = ""
    if html_path and html_path.exists():
        _html_raw = html_path.read_text(encoding="utf-8", errors="ignore")
        if BeautifulSoup is not None:
            _html_full_text = BeautifulSoup(_html_raw, "html.parser").get_text(" ")
        else:
            _html_full_text = re.sub(r"<[^>]+>", " ", _html_raw)
            _html_full_text = html_module.unescape(_html_full_text)
    summary: Dict[str, object] = {
        "html": str(html_path) if html_path else None,
        "hashtags": set(),
        "article_links": sorted(html_context["article_links"]),
        "video_links": sorted(html_context["video_links"]),
        "image_links": sorted(html_context["image_links"]),
        "bullet_points": list(html_context["bullet_points"]),
    }

    for row in rows:
        row_text = []
        if text_idx is not None and text_idx < len(row):
            row_text.append(row[text_idx])
        if ref_idx is not None and ref_idx < len(row):
            row_text.append(row[ref_idx])
        combined = " ".join(part for part in row_text if part)
        row_norm = normalize_lookup_text(combined)
        matching_blocks = match_html_blocks_to_row(
            row[text_idx] if text_idx is not None and text_idx < len(row) else "",
            row[ref_idx] if ref_idx is not None and ref_idx < len(row) else "",
            html_blocks,
        )

        hashtags = detect_hashtags(combined)
        if hashtags:
            summary["hashtags"].update(hashtags)
            row[column_positions["Hashtags"]] = " | ".join(hashtags)

        key_points = detect_pattern_matches(BULLET_PATTERN, combined)
        key_points.extend(str(block["text"]) for block in matching_blocks if block.get("marker_signals"))
        key_points = dedupe_texts(key_points, min_len=2)
        if key_points:
            row[column_positions["Key Points"]] = " | ".join(key_points)

        for metric, pattern, col in [
            ("Percent Mention", PERCENT_PATTERN, "Percent Mention"),
            ("Decibel Mention", DECIBEL_PATTERN, "Decibel Mention"),
            ("Speed Mention", SPEED_PATTERN, "Speed Mention"),
            ("Temperature Mention", TEMPERATURE_PATTERN, "Temperature Mention"),
            ("Surface Mention", SURFACE_PATTERN, "Surface Mention"),
            ("Volume Mention", VOLUME_PATTERN, "Volume Mention"),
        ]:
            values = detect_pattern_matches(pattern, combined)
            if values:
                row[column_positions[col]] = " | ".join(values)

        weight_entries = detect_pattern_matches(WEIGHT_OBJECT_PATTERN, combined) + detect_pattern_matches(WEIGHT_PERSON_PATTERN, combined)
        if weight_entries:
            object_values, person_values = classify_weight_entries(weight_entries)
            if object_values:
                row[column_positions["Weight Object Mention"]] = " | ".join(object_values)
            if person_values:
                row[column_positions["Weight Person Mention"]] = " | ".join(person_values)

        distances = detect_pure_distances(combined)
        if distances:
            row[column_positions["Distance Mention"]] = " | ".join(distances)

        socials: List[str] = []
        lowered = combined.lower()
        for keyword, label in SOCIAL_KEYWORDS.items():
            if keyword in lowered:
                socials.append(label)
        if socials:
            row[column_positions["Social Network Mention"]] = " | ".join(sorted(set(socials)))
            # Remove social network names from Brand Mention — Social Network Mention takes priority
            _brand_idx = header_map.get("brand mention")
            if _brand_idx is not None and _brand_idx < len(row) and row[_brand_idx].strip():
                _social_lower = {s.lower() for s in socials}
                _brand_filtered = [b for b in split_pipe_values(row[_brand_idx]) if b.lower() not in _social_lower]
                row[_brand_idx] = " | ".join(_brand_filtered)

        if location_idx is not None and location_idx < len(row):
            cities, countries = classify_locations(row[location_idx])
            if cities:
                row[column_positions["City Mention"]] = " | ".join(cities)
            if countries:
                row[column_positions["Country Mention"]] = " | ".join(countries)

        ranking_matches = detect_pattern_matches(RANKING_PATTERN, combined)
        if ranking_matches:
            _seen_rank: set = set()
            _deduped_rank: List[str] = []
            for _r in ranking_matches:
                _norm = re.sub(r"\s+", " ", unicodedata.normalize("NFD", _r.lower()))
                _norm = "".join(c for c in _norm if not unicodedata.combining(c))
                if _norm not in _seen_rank:
                    _seen_rank.add(_norm)
                    _deduped_rank.append(_r)
            ranking_matches = _deduped_rank
        if ranking_matches:
            row[column_positions["Ranking Mention"]] = " | ".join(ranking_matches)

        spoken_urls = detect_pattern_matches(SPOKEN_URL_PATTERN, combined)
        if spoken_urls:
            row[column_positions["Spoken URL"]] = " | ".join(spoken_urls)

        ref_text_cell = row[ref_idx] if ref_idx is not None and ref_idx < len(row) else ""
        punct_signals: List[str] = []
        if "?" in ref_text_cell:
            punct_signals.append("?")
        if "!" in ref_text_cell:
            punct_signals.append("!")
        if "..." in ref_text_cell or "\u2026" in ref_text_cell:
            punct_signals.append("...")
        if punct_signals:
            row[column_positions["Punctuation Signal"]] = " | ".join(punct_signals)

        matching_bold = extract_matching_html_fragments(row_norm, matching_blocks, "bold_parts")
        if matching_bold:
            row[column_positions["Bold Text"]] = " | ".join(matching_bold)

        matching_italic = extract_matching_html_fragments(row_norm, matching_blocks, "italic_parts")
        if matching_italic:
            row[column_positions["Italic Text"]] = " | ".join(matching_italic)

        matching_underline = extract_matching_html_fragments(row_norm, matching_blocks, "underline_parts")
        if matching_underline:
            row[column_positions["Underlined Text"]] = " | ".join(matching_underline)

        marker_signals = dedupe_list_signals(
            signal
            for block in matching_blocks
            for signal in block.get("marker_signals", [])
        )
        if marker_signals:
            row[column_positions["List Marker"]] = " | ".join(marker_signals)
            inferred_list_type = classify_list_type(marker_signals, None)
            if inferred_list_type:
                row[column_positions["List Type"]] = inferred_list_type

        matching_block_indices = {
            int(block.get("block_index", -1))
            for block in matching_blocks
            if int(block.get("block_index", -1)) >= 0
        }
        for group in html_bundle.get("list_groups", []):
            if not isinstance(group, Mapping):
                continue
            group_indices = {
                int(value)
                for value in group.get("block_indices", [])
                if isinstance(value, int) or (isinstance(value, str) and value.isdigit())
            }
            if not group_indices or not (matching_block_indices & group_indices):
                continue
            list_type = str(group.get("list_type") or "").strip()
            if list_type:
                row[column_positions["List Type"]] = list_type
            list_text = str(group.get("text") or "").strip()
            if list_text:
                row[column_positions["List Block"]] = list_text
            break

        cta_labels: List[str] = []
        if subscribe_tag_idx is not None and subscribe_tag_idx < len(row) and row[subscribe_tag_idx].strip():
            cta_labels.append("subscribe")
        if comment_tag_idx is not None and comment_tag_idx < len(row) and row[comment_tag_idx].strip():
            cta_labels.append("comment")
        if tip_tag_idx is not None and tip_tag_idx < len(row) and row[tip_tag_idx].strip():
            cta_labels.append("donate")
        if cta_labels:
            row[column_positions["CTA Detected"]] = " | ".join(cta_labels)

        _emoji_ctx = combined
        if _html_full_text and ref_idx is not None and ref_idx < len(row):
            _ref_anchor = (row[ref_idx] or "")[:60]
            if _ref_anchor:
                _pos = _html_full_text.find(_ref_anchor)
                if _pos != -1:
                    _w_start = max(0, _pos - 300)
                    _w_end = min(len(_html_full_text), _pos + len(row[ref_idx] or "") + 300)
                    _emoji_ctx = _html_full_text[_w_start:_w_end] + " " + combined
        concrete_emojis = detect_concrete_emoji(_emoji_ctx)
        if concrete_emojis:
            row[column_positions["Concrete Emoji"]] = " ".join(concrete_emojis)

    filter_number_mention_against_metrics(rows, header)

    # Attach global HTML data to the first row
    if rows:
        first_row = rows[0]
        if html_context["hashtags"]:
            all_hashtags = sorted(set(summary["hashtags"]) | set(html_context["hashtags"]))
            first_row[column_positions["Hashtags"]] = " | ".join(all_hashtags)
            summary["hashtags"] = set(all_hashtags)
        if html_context["article_links"]:
            first_row[column_positions["Article Links"]] = " | ".join(sorted(html_context["article_links"]))
        if html_context["video_links"]:
            first_row[column_positions["Video Links"]] = " | ".join(sorted(html_context["video_links"]))
        if html_context["image_links"]:
            first_row[column_positions["Image Links"]] = " | ".join(sorted(html_context["image_links"]))

    # Update quote column from row text where empty
    update_quote_column(rows, build_header_map(header))

    if manifest_path:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps({
                "html": summary["html"],
                "hashtags": sorted(summary["hashtags"]),
                "article_links": summary["article_links"],
                "video_links": summary["video_links"],
                "image_links": summary["image_links"],
                "bullet_points": summary["bullet_points"],
            }, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 2: enrich 8-column comparison CSV with CTA/Zoom tags, semantic annotations, and HTML metrics."
    )
    parser.add_argument("--input", type=Path, required=True, help="Path to the step-1 comparison CSV.")
    parser.add_argument("--html", type=Path, help="Path to the HTML reference file.")
    parser.add_argument("--output", type=Path, help="Output path for the enriched CSV.")
    parser.add_argument("--claude-api-key", help="Anthropic API key (defaults to ANTHROPIC_API_KEY env var).")
    parser.add_argument("--claude-model", default=DEFAULT_CTA_MODEL, help=f"Claude model for CTA/Zoom tagging (default: {DEFAULT_CTA_MODEL}).")
    parser.add_argument("--claude-max-tokens", type=int, default=1200, help="Max tokens for CTA/Zoom stage.")
    parser.add_argument("--claude-batch-size", type=int, default=60, help="Batch size for CTA/Zoom stage.")
    parser.add_argument("--nouns-claude-model", default=DEFAULT_NOUNS_MODEL, help=f"Claude model for nouns enrichment (default: {DEFAULT_NOUNS_MODEL}).")
    parser.add_argument("--nouns-claude-max-tokens", type=int, default=1500, help="Max tokens for nouns stage.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = args.input
    html_path = args.html

    print(f"[Step 2] Loading step-1 CSV: {input_path}")
    header, rows = load_csv(input_path)
    header_map = build_header_map(header)

    normalize_keep_column(rows)

    # --- CTA/Zoom tagging ---
    print("[Step 2] CTA/Zoom tagging with Claude...")
    api_key = resolve_api_key(args.claude_api_key)
    start_seconds, total_duration = collect_timeline_metadata(rows)
    claude_tags, claude_zoom = tag_rows_with_claude(
        rows, api_key, args.claude_model, args.claude_max_tokens, args.claude_batch_size,
    )
    apply_deterministic_cta_tags(rows, claude_tags)
    adjust_zoom_bias(claude_zoom, start_seconds, total_duration)
    normalize_zoom_sequences(claude_zoom, start_seconds)
    apply_tag_columns(header, rows, claude_tags)
    apply_zoom_column(header, rows, claude_zoom)

    # --- Nouns/semantic enrichment ---
    print("[Step 2] Nouns/semantic enrichment with Claude...")
    nouns_api_key = api_key  # same key, could be different via env

    ref_idx = require_column(build_header_map(header), "Reference Segment")
    text_idx = require_column(build_header_map(header), "Text")
    keep_idx = require_column(build_header_map(header), KEEP_COLUMN_NAME)
    start_idx = build_header_map(header).get(START_TIME_COLUMN_NAME.lower())
    end_idx = build_header_map(header).get(END_TIME_COLUMN_NAME.lower())

    if html_path and html_path.exists():
        reference_text = strip_reference_title(collect_reference_text(html_path))
    else:
        print("    No HTML reference; deriving context from comparison CSV.")
        reference_text = _reference_fallback_from_rows(rows, ref_idx, text_idx)

    analysis_text = prepare_reference_analysis_text(reference_text)
    reference_summary = build_reference_summary(reference_text)

    language = detect_language_from_text(analysis_text)
    language_name = LANGUAGE_PROFILES.get(language, LANGUAGE_PROFILES["en"]).get("name", "Unknown")
    print(f"    Detected language: {language_name} ({language})")

    column_positions = ensure_feature_columns(header, rows)
    titles_column_idx = ensure_titles_column(header, rows)
    news_column_idx = ensure_relevant_news_column(header, rows)
    spans = build_row_reference_spans(rows, ref_idx, analysis_text)

    if html_path and html_path.exists():
        html_titles = extract_titles_from_html(html_path)
        located_titles = locate_titles_in_text(html_titles, analysis_text)
    else:
        html_titles = []
        located_titles = []

    mention_data = extract_mentions_with_claude(
        analysis_text, nouns_api_key, args.nouns_claude_model, args.nouns_claude_max_tokens, language,
    )
    normalize_feeling_annotations(mention_data)
    split_legacy_number_entries(mention_data, language)

    fallback_dates, date_spans = detect_date_candidates(analysis_text, language)
    fallback_numbers = detect_number_candidates(analysis_text, date_spans)

    merged_dates, injected_dates = merge_annotation_entries(mention_data.get("date"), fallback_dates)
    if merged_dates:
        if html_path is None and injected_dates and len(merged_dates) > FALLBACK_DATE_LIMIT:
            mention_data["date"] = limit_unique_entries(merged_dates, FALLBACK_DATE_LIMIT)
        else:
            mention_data["date"] = merged_dates

    merged_numbers, injected_numbers = merge_annotation_entries(mention_data.get("number"), fallback_numbers)
    if merged_numbers:
        mention_data["number"] = merged_numbers
    split_money_number_mentions(mention_data)

    fallback_institutions = detect_institution_candidates(analysis_text)
    merged_institutions, _ = merge_annotation_entries(mention_data.get("gov_institution"), fallback_institutions)
    if merged_institutions:
        mention_data["gov_institution"] = merged_institutions

    feeling_entries = mention_data.get("feeling")
    if not isinstance(feeling_entries, list) or not feeling_entries:
        targeted_feelings = extract_targeted_annotations(
            "feeling", analysis_text, nouns_api_key, args.nouns_claude_model, args.nouns_claude_max_tokens, language,
        )
        merged_feelings, _ = merge_annotation_entries(feeling_entries, targeted_feelings)
        if merged_feelings:
            mention_data["feeling"] = merged_feelings

    suppress_default_country_mentions(mention_data, language)
    apply_language_location_overrides(mention_data, language)

    annotations = map_mentions_to_rows(mention_data, spans, rows, ref_idx, text_idx)
    apply_feature_values(rows, annotations, column_positions)
    title_assignments = map_titles_to_rows(located_titles, spans, rows, ref_idx, text_idx)
    apply_title_annotations(rows, title_assignments, titles_column_idx)
    restrict_tag_columns_to_kept_rows(header, rows)

    durations, _ = compute_kept_row_durations(rows, keep_idx, start_idx, end_idx)
    news_targets = allocate_news_targets(durations)
    news_annotations = generate_relevant_news_annotations(
        rows, news_targets, text_idx, ref_idx, start_idx, end_idx,
        spans, analysis_text, nouns_api_key, args.nouns_claude_model, args.nouns_claude_max_tokens,
        language_name, reference_summary,
    )
    apply_relevant_news(rows, news_column_idx, news_annotations)

    # --- HTML metrics ---
    print("[Step 2] Adding HTML metrics...")
    final_output_dir = FINAL_OUTPUT_DIR.expanduser()
    final_output_dir.mkdir(parents=True, exist_ok=True)

    if args.output:
        output_path = args.output
        output_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        output_path = final_output_dir / (input_path.stem + "_full.csv")

    manifest_path = output_path.with_name(f"{output_path.stem}_summary.json")
    enrich_html_metrics(header, rows, html_path, manifest_path)
    suppress_zoom_for_list_rows(header, rows)

    write_csv(output_path, header, rows)
    print(f"[Step 2] Enriched CSV written to {output_path}")
    print_run_summary(
        {"Input CSV": input_path, "HTML": html_path, "Language": language_name},
        {"Enriched CSV": output_path, "Manifest": manifest_path},
    )


if __name__ == "__main__":
    main()
