"""
Country flag lookup builder.

Loads all country name translations from the iso-countries-languages JSON
files (24+ languages: FR, DE, ES, PT, IT, AR, RU, ZH, …) so any language
variant maps to the correct flag-of-<slug>.mov in flags_output3/.

Also includes a hardcoded alias table for abbreviated / common forms that
are not present in the official ISO names (e.g. "États-Unis", "USA",
"Allemagne" instead of the full "République fédérale d'Allemagne").
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

FLAGS_OUTPUT_DIR = Path("~/Desktop/code/deployable_auto-montage/animated_emoji/flags_output3_up").expanduser()
ISO_COUNTRIES_DIR = Path(
    "~/Desktop/code/deployable_auto-montage/shared_assets/iso-countries-languages/res/countries"
).expanduser()

# Where en.json name doesn't slugify to the actual flag filename
ISO_SLUG_OVERRIDES: Dict[str, str] = {
    "US": "united-states-usa",
    "CZ": "czech-republic",
    "CD": "democratic-republic-of-congo",
    "MK": "republic-of-macedonia",
    "TW": "republic-of-china-taiwan",
    "CG": "republic-of-congo",
    "GM": "gambia",
    "SZ": "swaziland",
    "TL": "east-timor",
    "CI": "ivory-coast",
    "VA": "vatican-city",
    "KP": "north-korea",
    "KR": "south-korea",
    "BO": "bolivia",
    "VE": "venezuela",
    "IR": "iran",
    "SY": "syria",
    "MD": "moldova",
    "TZ": "tanzania",
    "PS": "palestine",
    "SS": "south-sudan",
    "XK": "kosovo",
    "MV": "maldives",
    "FM": "federated-states-of-micronesia",
    "MH": "marshall-islands",
    "PW": "palau",
    "KI": "kiribati",
    "TV": "tuvalu",
    "NR": "nauru",
    "SM": "san-marino",
    "LI": "liechtenstein",
    "AD": "andorra",
    "MC": "monaco",
    "LC": "saint-lucia",
    "KN": "saint-kitts-and-nevis",
    "VC": "saint-vincent-and-grenadines",
    "WS": "samoa",
    "TO": "tonga",
    "VU": "vanuatu",
    "SB": "solomon-islands",
    "PG": "papua-new-guinea",
    "FJ": "fiji",
    "LA": "laos",
    "MM": "myanmar",
    "BN": "brunei",
    "GQ": "equatorial-guinea",
    "GW": "guinea-bissau",
    "ER": "eritrea",
    "ST": "sao-tome-and-principe",
    "CV": "cape-verde",
    "MG": "madagascar",
    "MU": "mauritius",
    "KM": "comoros",
    "SC": "seychelles",
    "DJ": "djibouti",
    "SL": "sierra-leone",
    "LR": "liberia",
    "GN": "guinea",
    "BJ": "benin",
    "TG": "togo",
    "CF": "central-african-republic",
    "BI": "burundi",
    "RW": "rwanda",
    "LS": "lesotho",
    "MW": "malawi",
    "ZW": "zimbabwe",
    "ZM": "zambia",
    "BW": "botswana",
    "NA": "namibia",
    "MZ": "mozambique",
    "AO": "angola",
    "TD": "chad",
    "NE": "niger",
    "ML": "mali",
    "BF": "burkina-faso",
    "GH": "ghana",
    "SN": "senegal",
    "MR": "mauritania",
}

# Extra aliases for abbreviated/common forms not present in the ISO package
COUNTRY_ALIASES: Dict[str, str] = {
    # United States
    "etats-unis": "united-states-usa",
    "états-unis": "united-states-usa",
    "usa": "united-states-usa",
    "amerique": "united-states-usa",
    "amérique": "united-states-usa",
    "america": "united-states-usa",
    "estados unidos": "united-states-usa",
    "vereinigte staaten": "united-states-usa",
    "stati uniti": "united-states-usa",
    "verenigde staten": "united-states-usa",
    # United Kingdom
    "royaume-uni": "united-kingdom",
    "grande-bretagne": "united-kingdom",
    "england": "united-kingdom",
    "angleterre": "united-kingdom",
    "uk": "united-kingdom",
    "britain": "united-kingdom",
    "gran bretana": "united-kingdom",
    "grossbritannien": "united-kingdom",
    # Germany
    "allemagne": "germany",
    "alemania": "germany",
    "deutschland": "germany",
    "germania": "germany",
    "alemanha": "germany",
    # France
    "frankreich": "france",
    "francia": "france",
    "franca": "france",
    # Russia
    "russie": "russia",
    "rusia": "russia",
    "russland": "russia",
    # China
    "chine": "china",
    "cina": "china",
    "pekin": "china",
    "pékin": "china",
    "beijing": "china",
    # Japan
    "japon": "japan",
    "giappone": "japan",
    "japao": "japan",
    # South Korea
    "coree du sud": "south-korea",
    "corée du sud": "south-korea",
    "corea del sur": "south-korea",
    "sudkorea": "south-korea",
    # North Korea
    "coree du nord": "north-korea",
    "corée du nord": "north-korea",
    "corea del norte": "north-korea",
    # Brazil
    "bresil": "brazil",
    "brésil": "brazil",
    "brasilien": "brazil",
    "brasile": "brazil",
    # Spain
    "espagne": "spain",
    "espana": "spain",
    "spanien": "spain",
    "spagna": "spain",
    "espanha": "spain",
    # Italy
    "italie": "italy",
    "italien": "italy",
    # Netherlands
    "pays-bas": "netherlands",
    "hollande": "netherlands",
    "niederlande": "netherlands",
    "olanda": "netherlands",
    "holanda": "netherlands",
    # Switzerland
    "suisse": "switzerland",
    "suiza": "switzerland",
    "schweiz": "switzerland",
    "svizzera": "switzerland",
    "suica": "switzerland",
    # Belgium
    "belgique": "belgium",
    "belgien": "belgium",
    "belgio": "belgium",
    "belgica": "belgium",
    # Sweden
    "suede": "sweden",
    "suède": "sweden",
    "suecia": "sweden",
    "schweden": "sweden",
    # Norway
    "norvege": "norway",
    "norvège": "norway",
    "noruega": "norway",
    "norwegen": "norway",
    # Denmark
    "danemark": "denmark",
    "dinamarca": "denmark",
    # Poland
    "pologne": "poland",
    "polonia": "poland",
    "polen": "poland",
    # Ukraine
    "ucrania": "ukraine",
    # Turkey
    "turquie": "turkey",
    "turquia": "turkey",
    "turkei": "turkey",
    "turchia": "turkey",
    # Saudi Arabia
    "arabie saoudite": "saudi-arabia",
    "arabia saudita": "saudi-arabia",
    "arabie": "saudi-arabia",
    # Israel
    "israël": "israel",
    # Palestine
    "palestina": "palestine",
    # Egypt
    "egypte": "egypt",
    "égypte": "egypt",
    "egipto": "egypt",
    "agypten": "egypt",
    # Morocco
    "maroc": "morocco",
    "marruecos": "morocco",
    "marokko": "morocco",
    "marocco": "morocco",
    # Algeria
    "algerie": "algeria",
    "algérie": "algeria",
    "argelia": "algeria",
    # Tunisia
    "tunisie": "tunisia",
    "tunez": "tunisia",
    # South Africa
    "afrique du sud": "south-africa",
    "sudafrica": "south-africa",
    "sudafrika": "south-africa",
    # India
    "inde": "india",
    "indien": "india",
    # Australia
    "australie": "australia",
    "australien": "australia",
    # Mexico
    "mexique": "mexico",
    "mejico": "mexico",
    "mexiko": "mexico",
    "messico": "mexico",
    # Argentina
    "argentine": "argentina",
    # Chile
    "chili": "chile",
    # Colombia
    "colombie": "colombia",
    # Peru
    "perou": "peru",
    "pérou": "peru",
    # Vietnam
    "viet nam": "vietnam",
    "viêtnam": "vietnam",
    # Thailand
    "thailande": "thailand",
    "thaïlande": "thailand",
    "tailandia": "thailand",
    # Taiwan
    "formose": "republic-of-china-taiwan",
    # Congo
    "congo": "republic-of-congo",
    "rdc": "democratic-republic-of-congo",
    "rd congo": "democratic-republic-of-congo",
    "republique democratique du congo": "democratic-republic-of-congo",
    # Vatican
    "vatican": "vatican-city",
    "saint-siege": "vatican-city",
    # Ireland
    "irlande": "ireland",
    "irlanda": "ireland",
    "irland": "ireland",
    # Czech Republic
    "republique tcheque": "czech-republic",
    "tchequie": "czech-republic",
    "chequia": "czech-republic",
    "tschechien": "czech-republic",
    # Slovakia
    "slovaquie": "slovakia",
    "eslovaquia": "slovakia",
    # Hungary
    "hongrie": "hungary",
    "hungria": "hungary",
    "ungarn": "hungary",
    # Romania
    "roumanie": "romania",
    "rumania": "romania",
    # Greece
    "grece": "greece",
    "grèce": "greece",
    "grecia": "greece",
    # Finland
    "finlande": "finland",
    "finlandia": "finland",
    "finnland": "finland",
    # Austria
    "autriche": "austria",
    "osterreich": "austria",
    # New Zealand
    "nouvelle-zelande": "new-zealand",
    "nueva zelanda": "new-zealand",
    "neuseeland": "new-zealand",
    # Singapore
    "singapour": "singapore",
    "singapur": "singapore",
    # Malaysia
    "malaisie": "malaysia",
    "malaisia": "malaysia",
    # Philippines
    "filipinas": "philippines",
}


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _normalize_for_lookup(text: str) -> str:
    """Lowercase + strip combining accents + collapse whitespace."""
    nfkd = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped.lower()).strip()


def build_flag_lookup() -> Dict[str, Path]:
    """
    Returns {normalized_country_name → flag_mov_path}.
    Covers all language files in the iso-countries-languages package
    plus the hardcoded aliases for abbreviated/common forms.
    """
    if not ISO_COUNTRIES_DIR.exists() or not FLAGS_OUTPUT_DIR.exists():
        return {}

    en_path = ISO_COUNTRIES_DIR / "en.json"
    if not en_path.exists():
        return {}

    en_data: Dict[str, str] = json.loads(en_path.read_text(encoding="utf-8"))

    # Build ISO code → flag path
    iso_to_flag: Dict[str, Path] = {}
    for iso, name in en_data.items():
        slug = ISO_SLUG_OVERRIDES.get(iso) or _slugify(name)
        flag_path = FLAGS_OUTPUT_DIR / f"flag-of-{slug}.mov"
        if flag_path.exists():
            iso_to_flag[iso] = flag_path

    # Build normalized_name → flag path from all language files
    lookup: Dict[str, Path] = {}
    for lang_file in sorted(ISO_COUNTRIES_DIR.glob("*.json")):
        try:
            lang_data: Dict[str, str] = json.loads(lang_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for iso, name in lang_data.items():
            if iso not in iso_to_flag:
                continue
            normalized = _normalize_for_lookup(name)
            if normalized and len(normalized) >= 3:
                lookup[normalized] = iso_to_flag[iso]

    # Add hardcoded aliases
    for alias, slug in COUNTRY_ALIASES.items():
        flag_path = FLAGS_OUTPUT_DIR / f"flag-of-{slug}.mov"
        if flag_path.exists():
            normalized = _normalize_for_lookup(alias)
            if normalized and len(normalized) >= 3:
                lookup[normalized] = flag_path

    return lookup


def scan_text_for_flags(
    text: str, lookup: Dict[str, Path]
) -> List[Tuple[str, Path]]:
    """
    Scan *text* for whole-word country name mentions.
    Returns list of (matched_text, flag_path) sorted by match position.
    Longer phrases are tried first to avoid partial matches.
    """
    if not text or not lookup:
        return []
    normalized_text = _normalize_for_lookup(text)
    results: List[Tuple[str, Path]] = []
    covered: set[int] = set()
    for keyword, flag_path in sorted(lookup.items(), key=lambda kv: -len(kv[0])):
        if len(keyword) < 3:
            continue
        pattern = r"(?<![a-z\-])" + re.escape(keyword) + r"(?![a-z\-])"
        for m in re.finditer(pattern, normalized_text):
            positions = set(range(m.start(), m.end()))
            if positions & covered:
                continue
            covered |= positions
            results.append((text[m.start():m.end()], flag_path))
    results.sort(key=lambda pair: normalized_text.find(_normalize_for_lookup(pair[0])))
    return results
