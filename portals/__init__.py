"""T-45: Portal adapter package. Shared metadata scoring utility."""
import re

from utils import normalize_text as _normalize_text, plural_aware_pattern as _plural_pattern


def score_metadata(text: str, keywords: frozenset) -> tuple[int, list[str]]:
    """Score concatenated dataset metadata against an effective keyword set.

    Returns (score 0-100, sorted list of matched keywords).
    Each unique keyword match contributes equally; score is the fraction of
    the keyword set that matched, scaled to 100 and capped.
    """
    if not keywords:
        return 0, []

    text_norm = _normalize_text(text)
    matched = [
        kw for kw in keywords
        if re.search(_plural_pattern(_normalize_text(kw)), text_norm)
    ]

    score = min(100, round(len(matched) / len(keywords) * 100))
    return score, sorted(matched)
