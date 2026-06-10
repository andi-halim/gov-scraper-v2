"""Phase 6 dataset detector (T-60–T-61)."""
import re
import logging
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Matches a known dataset extension at a word/segment boundary.
# Excludes '.' from the lookahead so compound extensions like .csv.gz don't falsely match .csv.
_EXT_RE = re.compile(r'\.(csv|xlsx|xls|json|xml|pdf)(?=[^a-zA-Z0-9.]|$)', re.IGNORECASE)

# URL path tokens that suggest a server-side download endpoint worth a HEAD probe.
_DOWNLOAD_PATH_RE = re.compile(
    r'/(?:download|downloads|export|exports|getfile|get[-_]file|file[-_]download'
    r'|dl|file|files|serve|attachment|attachments|document|documents)(?:[/?#]|$)',
    re.IGNORECASE,
)

# Higher tier = ranked first when truncating at the 50-URL cap.
_FORMAT_TIER: dict[str, int] = {
    "csv": 3, "json": 3, "xlsx": 3,
    "xls": 2, "xml": 2,
    "pdf": 1,
    "": 1,  # Content-Disposition attachment with unknown extension
}

from page_result import PageResult


def _extract_format_from_url(url: str) -> str | None:
    """Return the lowercase format name if the URL points to a known dataset file, else None."""
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    # Check path (e.g. /files/data.csv or /data/report.xlsx?v=2)
    m = _EXT_RE.search(parsed.path)
    if m:
        return m.group(1).lower()
    # Check query string (e.g. download.php?file=data.csv)
    if parsed.query:
        m = _EXT_RE.search(parsed.query)
        if m:
            return m.group(1).lower()
    return None


def _check_content_disposition(url: str, http_client) -> str | None:
    """T-61: Issue a HEAD request to check for Content-Disposition: attachment.

    Returns the file format (e.g. "csv") if a recognized extension appears in the
    header filename, "" if attachment is present but no extension is found, or None
    if the header is absent or the request fails.
    """
    try:
        response = http_client.head(url)
        cd = response.headers.get("content-disposition", "")
        if "attachment" not in cd.lower():
            return None
        m = _EXT_RE.search(cd)
        if m:
            return m.group(1).lower()
        return ""  # Attachment present but format unknown
    except Exception as exc:
        logger.warning("HEAD request failed for %s: %s", url, exc)
        return None


def _dataset_score(
    fmt: str,
    anchor_text: str,
    page_depth: int,
    effective_keywords: frozenset | None,
) -> int:
    """Score a candidate dataset URL for ranking.

    Components (max 7):
      - Format tier (1–3): machine-readable > PDF/unknown
      - Keyword match in anchor text (0–2): Census vocabulary hit in link label
      - Depth bonus (0–2): links found closer to the seed are ranked higher
    """
    score = _FORMAT_TIER.get(fmt, 1)
    if effective_keywords:
        anchor_lower = anchor_text.lower()
        if any(kw.lower() in anchor_lower for kw in effective_keywords):
            score += 2
    score += max(0, 2 - page_depth)
    return score


def detect_datasets(
    pages: list[PageResult],
    http_client=None,
    effective_keywords: frozenset | None = None,
    page_depths: dict[str, int] | None = None,
) -> tuple[bool, list[str], list[str]]:
    """T-60: Scan crawled pages for downloadable dataset links.

    Args:
        pages: list of PageResult from the depth crawler.
        http_client: optional HttpClient used for Content-Disposition HEAD probes (T-61).
        effective_keywords: Census vocabulary used to boost anchor-text matches during ranking.
        page_depths: mapping of page URL to crawl hop depth; seed=0, depth-1 pages=1, etc.

    Returns:
        (found, dataset_urls, dataset_formats) where dataset_urls is ranked by relevance
        (format tier + Census keyword match in anchor text + crawl depth proximity) and
        capped at 50. dataset_formats reflects only the formats present in the returned URLs.
    """
    candidates: list[tuple[int, str, str]] = []  # (score, url, fmt)
    seen_urls: set[str] = set()
    _depths = page_depths or {}

    for page_url, html, http_status, _js_rendered in pages:
        if http_status != 200 or not html:
            continue

        page_depth = _depths.get(page_url, 0)

        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception as exc:
            logger.warning("Failed to parse HTML from %s for dataset detection: %s", page_url, exc)
            continue

        for tag in soup.find_all("a", href=True):
            href = tag["href"].strip()
            if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
                continue

            try:
                absolute = urljoin(page_url, href)
                # Strip fragment so data.json#section1 and data.json#section2 deduplicate to the same URL
                absolute = urlparse(absolute)._replace(fragment="").geturl()
            except Exception:
                continue

            if absolute in seen_urls:
                continue

            fmt = _extract_format_from_url(absolute)

            # T-61: for ambiguous download-like paths, probe via HEAD
            if fmt is None and http_client is not None:
                parsed_path = urlparse(absolute).path
                if _DOWNLOAD_PATH_RE.search(parsed_path):
                    fmt = _check_content_disposition(absolute, http_client)

            if fmt is not None:
                seen_urls.add(absolute)
                anchor_text = tag.get_text(strip=True)
                score = _dataset_score(fmt, anchor_text, page_depth, effective_keywords)
                candidates.append((score, absolute, fmt))

    _MAX_DATASET_URLS = 50
    candidates.sort(key=lambda x: x[0], reverse=True)
    if len(candidates) > _MAX_DATASET_URLS:
        logger.warning(
            "Dataset URL count (%d) exceeds cap of %d; ranking by relevance and truncating",
            len(candidates), _MAX_DATASET_URLS,
        )
        candidates = candidates[:_MAX_DATASET_URLS]

    dataset_urls = [url for _, url, _ in candidates]
    format_set = {fmt for _, _, fmt in candidates if fmt}

    found = bool(dataset_urls)
    formats = sorted(format_set)
    return found, dataset_urls, formats
