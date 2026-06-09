"""T-70: Load effective keyword sets for the relevance scorer."""
import csv
import functools
import json
from pathlib import Path

_CONFIG = Path(__file__).parent.parent / "config"
_KEYWORDS_PATH = _CONFIG / "keywords.csv"
_STATE_DEFS_PATH = _CONFIG / "state_definitions.json"


def _load_keywords(path: Path) -> frozenset[str]:
    keywords: set[str] = set()
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if row:
                kw = row[0].strip()
                if kw:
                    keywords.add(kw)
    return frozenset(keywords)


def _load_state_defs(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


@functools.lru_cache(maxsize=1)
def _base_keywords() -> frozenset[str]:
    return _load_keywords(_KEYWORDS_PATH)


@functools.lru_cache(maxsize=1)
def _state_defs() -> dict:
    return _load_state_defs(_STATE_DEFS_PATH)


def base_keyword_count() -> int:
    """Return the number of keywords in the base keywords.csv file.

    Used as the normalization denominator so state-specific extra terms
    don't inflate the effective-keyword count and suppress scores.
    """
    return len(_base_keywords())


def get_effective_keywords(state: str) -> frozenset[str]:
    """Return the effective keyword set for a given state tag.

    FEDERAL and NATIONAL use the base keyword set only.
    All other states get base keywords unioned with state-specific census_terms.
    """
    base = _base_keywords()
    if state == "NATIONAL":
        return base
    terms = frozenset(
        t.strip()
        for t in _state_defs().get(state, {}).get("census_terms", [])
        if t.strip()
    )
    return base | terms
