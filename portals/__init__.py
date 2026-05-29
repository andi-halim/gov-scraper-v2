"""T-45: Portal adapter package. Shared metadata scoring utility."""
import re
import unicodedata


def _normalize_text(text: str) -> str:
    nfc = unicodedata.normalize("NFC", text)
    nfd = unicodedata.normalize("NFD", nfc)
    stripped = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return stripped.lower()


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
        if re.search(r"\b" + re.escape(_normalize_text(kw)) + r"\b", text_norm)
    ]

    score = min(100, round(len(matched) / len(keywords) * 100))
    return score, sorted(matched)
