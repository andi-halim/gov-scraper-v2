"""T-71/T-72: Weighted keyword relevance scorer for crawled pages."""
import functools
import re

from bs4 import BeautifulSoup, Comment

from crawler.orchestrator import PageResult
from utils import normalize_text as _normalize

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def _strip_urls(text: str) -> str:
    """T-72: Strip URL strings from a text pool before scoring."""
    return _URL_RE.sub(" ", text)


def _extract_pools(html: str) -> tuple[str, str, str]:
    """Return (heading_text, body_text, anchor_text) from one HTML page.

    BeautifulSoup get_text() returns text-node content only, so href attribute
    values are already excluded (T-72). _strip_urls removes inline URL strings.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return "", "", ""

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for node in soup.find_all(string=lambda s: isinstance(s, Comment)):
        node.extract()

    heading_parts: list[str] = []
    title_tag = soup.find("title")
    if title_tag:
        heading_parts.append(title_tag.get_text(" ", strip=True))
    for tag in soup.find_all(["h1", "h2", "h3"]):
        heading_parts.append(tag.get_text(" ", strip=True))
        tag.decompose()  # remove from tree so body pool excludes heading text
    heading_text = _strip_urls(" ".join(heading_parts))

    anchor_text = _strip_urls(
        " ".join(a.get_text(" ", strip=True) for a in soup.find_all("a"))
    )

    body = soup.find("body")
    raw_body = body.get_text(" ", strip=True) if body else soup.get_text(" ", strip=True)
    body_text = _strip_urls(raw_body)

    return heading_text, body_text, anchor_text


@functools.lru_cache(maxsize=128)
def _compile_patterns(keywords: frozenset) -> list[tuple[str, re.Pattern]]:
    """Precompile regex patterns for a keyword set. Cached per unique frozenset."""
    return [
        (kw, re.compile(r"\b" + re.escape(_normalize(kw)) + r"\b"))
        for kw in keywords
    ]


def score_page(pages: list[PageResult], effective_keywords: frozenset) -> dict:
    """T-71: Score crawled pages against an effective keyword set.

    Aggregates text pools across all HTTP-200 pages, then applies
    weighted keyword matching per PRD §7.

    Returns {"relevance_score": int (0–100), "matched_keywords": list[str]}.
    """
    if not effective_keywords:
        return {"relevance_score": 0, "matched_keywords": []}

    heading_parts: list[str] = []
    body_parts: list[str] = []
    anchor_parts: list[str] = []

    for _url, html, http_status, _js_rendered in pages:
        if not html or http_status != 200:
            continue
        h, b, a = _extract_pools(html)
        heading_parts.append(h)
        body_parts.append(b)
        anchor_parts.append(a)

    heading_norm = _normalize(" ".join(heading_parts))
    body_norm = _normalize(" ".join(body_parts))
    anchor_norm = _normalize(" ".join(anchor_parts))

    normalization_factor = len(effective_keywords)
    weighted_hits = 0.0
    matched: list[str] = []

    for kw, pattern in _compile_patterns(effective_keywords):
        pts = 0.0
        if pattern.search(heading_norm):
            pts += 0.50
        if pattern.search(body_norm):
            pts += 0.35
        if pattern.search(anchor_norm):
            pts += 0.15
        if pts > 0:
            weighted_hits += pts
            matched.append(kw)

    relevance_score = min(100, round(weighted_hits / normalization_factor * 100))
    return {"relevance_score": relevance_score, "matched_keywords": sorted(matched)}
