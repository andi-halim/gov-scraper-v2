"""Shared text-processing utilities used across scorer and portal adapters."""
import re
import unicodedata


def normalize_text(text: str) -> str:
    """NFC-normalize, strip diacritics, and lowercase a string."""
    nfc = unicodedata.normalize("NFC", text)
    nfd = unicodedata.normalize("NFD", nfc)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()


def plural_aware_pattern(normalized_term: str) -> str:
    """Return a regex pattern matching both singular and plural of normalized_term.

    Input must already be normalized (lowercase, NFC, diacritics stripped).
    Each keyword still counts as 1 in the normalization factor — the pattern
    just broadens what text it matches.

    Handles:
    - consonant-y / -ies alternation  authority ↔ authorities, county ↔ counties
    - -es plurals                      parish ↔ parishes, tax ↔ taxes
    - regular -s plurals               district ↔ districts, borough ↔ boroughs
    """
    t = normalized_term.strip()
    if not t:
        return r'(?!)'  # empty term — matches nothing

    # Already a -ies plural: authorities → authorit(y|ies)
    if t.endswith('ies') and len(t) > 3:
        stem = re.escape(t[:-3])
        return rf'\b{stem}(?:y|ies)\b'

    # Consonant-y singular: authority → authorit(y|ies)
    if t.endswith('y') and len(t) > 2 and t[-2] not in 'aeiou':
        stem = re.escape(t[:-1])
        return rf'\b{stem}(?:y|ies)\b'

    # Already a -es plural where stem ends in sh/ch/s/x/z: parishes → parish(es)?
    if t.endswith('es') and len(t) > 3:
        stem = t[:-2]
        if stem and (stem[-1] in 'sxz' or stem.endswith(('sh', 'ch'))):
            return rf'\b{re.escape(stem)}(?:es)?\b'

    # Singular ending in sh/ch/x/z → plural adds -es: parish, tax
    if t.endswith(('sh', 'ch')) or (len(t) > 1 and t[-1] in 'xz'):
        return rf'\b{re.escape(t)}(?:es)?\b'

    # Regular -s plural: districts → district(s?)
    if t.endswith('s') and len(t) > 2 and not t.endswith('ss'):
        stem = re.escape(t[:-1])
        return rf'\b{stem}s?\b'

    # Default: regular singular, add optional -s
    return rf'\b{re.escape(t)}s?\b'
