"""Shared text-processing utilities used across scorer and portal adapters."""
import unicodedata


def normalize_text(text: str) -> str:
    """NFC-normalize, strip diacritics, and lowercase a string."""
    nfc = unicodedata.normalize("NFC", text)
    nfd = unicodedata.normalize("NFD", nfc)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()
