import re
import logging
from urllib.parse import urlparse

import tldextract
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# T-31: Canonical state name → two-letter abbreviation (all 50 states + DC)
STATE_NAME_TO_ABBREV: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}

# Reverse lookup: abbreviation → name
ABBREV_TO_STATE_NAME: dict[str, str] = {v: k for k, v in STATE_NAME_TO_ABBREV.items()}

# All valid state abbreviations
STATE_ABBREVS: frozenset[str] = frozenset(STATE_NAME_TO_ABBREV.values())

# T-32: Known federal agency domains (extensible)
FEDERAL_DOMAINS: frozenset[str] = frozenset({
    "hud.gov",
    "epa.gov",
    "census.gov",
    "usda.gov",
    "faa.gov",
    "usa.gov",
    "data.gov",
    "va.gov",   # Dept of Veterans Affairs — two-letter domain collides with VA (Virginia)
})

# State names sorted longest-first so multi-word names match before substrings
_SORTED_STATE_NAMES = sorted(STATE_NAME_TO_ABBREV.keys(), key=len, reverse=True)

# Regex patterns compiled once
_STATE_US_PATTERN = re.compile(r"\.state\.([a-z]{2})\.us$", re.IGNORECASE)


class StateTagger:
    """Tags a URL with a two-letter state code, FEDERAL, or NATIONAL.

    Resolution follows the six-priority chain from PRD §8. Pass `html`
    to enable the page-content fallback (Priority 4); omit it to use
    URL-only resolution (Priorities 1–3 and 5–6).
    """

    def tag(self, url: str, html: str = "") -> str:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        ext = tldextract.extract(url)

        # Priority 1: *.state.XX.us
        m = _STATE_US_PATTERN.search(hostname)
        if m:
            code = m.group(1).upper()
            if code in STATE_ABBREVS:
                logger.debug("Tagged %s → %s (Priority 1: *.state.XX.us)", url, code)
                return code

        # Priority 2: two-letter registered domain before .gov (e.g. ca.gov, mo.gov)
        # Explicit federal domains (va.gov, etc.) are excluded even if two-letter.
        _reg = f"{ext.domain}.{ext.suffix}" if ext.suffix else ext.domain
        if ext.suffix == "gov" and len(ext.domain) == 2 and _reg.lower() not in FEDERAL_DOMAINS:
            code = ext.domain.upper()
            if code in STATE_ABBREVS:
                logger.debug("Tagged %s → %s (Priority 2: XX.gov subdomain)", url, code)
                return code

        # Priority 3: full state name in registered domain (exact or embedded substring).
        # Multi-word names are compressed ("new mexico" → "newmexico") before matching.
        # Longest-first order ensures "westvirginia" matches before "virginia".
        domain_lower = ext.domain.lower()
        code = None
        for name in _SORTED_STATE_NAMES:
            if name.replace(" ", "") in domain_lower:
                code = STATE_NAME_TO_ABBREV[name]
                break
        if code:
            logger.debug("Tagged %s → %s (Priority 3: state name in domain)", url, code)
            return code

        # Priority 4: page content fallback
        if html:
            code = self._tag_from_content(html)
            if code:
                logger.debug("Tagged %s → %s (Priority 4: page content)", url, code)
                return code

        # Priority 5: known federal domain
        registered = f"{ext.domain}.{ext.suffix}" if ext.suffix else ext.domain
        if registered.lower() in FEDERAL_DOMAINS:
            logger.debug("Tagged %s → FEDERAL (Priority 5: federal domain list)", url)
            return "FEDERAL"

        # Priority 6: unresolved
        logger.debug("Tagged %s → NATIONAL (Priority 6: unresolved)", url)
        return "NATIONAL"

    def _tag_from_content(self, html: str) -> str | None:
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception as exc:
            logger.warning("State content parse failed: %s", exc)
            return None

        texts: list[str] = []
        title_tag = soup.find("title")
        if title_tag:
            texts.append(title_tag.get_text(" ", strip=True))
        h1_tag = soup.find("h1")
        if h1_tag:
            texts.append(h1_tag.get_text(" ", strip=True))

        if not texts:
            return None

        combined = " ".join(texts)
        combined_lower = combined.lower()

        # Full state names first (longest-first avoids "new" matching before "new mexico")
        for name in _SORTED_STATE_NAMES:
            if re.search(r"\b" + re.escape(name) + r"\b", combined_lower):
                return STATE_NAME_TO_ABBREV[name]

        # Abbreviations: match only against original-case text to avoid false positives
        # on common English words that happen to be valid abbreviations (or, in, me, etc.)
        for abbrev in STATE_ABBREVS:
            if re.search(r"\b" + abbrev + r"\b", combined):
                return abbrev

        return None
