"""Depth crawler (T-50–T-52) and input ingestion (T-80–T-81)."""
import csv
import logging
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse

import tldextract
from bs4 import BeautifulSoup

from crawler.http_client import HttpClient

logger = logging.getLogger(__name__)

# Namedtuple-style alias for the per-page result
# (url, html, http_status, js_rendered)
PageResult = tuple[str, str, int, bool]


def _normalize_url_for_dedup(url: str) -> str:
    """Return lowercase scheme+netloc+path for deduplication."""
    p = urlparse(url)
    return f"{p.scheme.lower()}://{p.netloc.lower()}{p.path.lower()}"


def load_urls(csv_path: str) -> list[dict]:
    """T-80/T-81: Read a urls.csv and return a priority-sorted list of URL dicts.

    Each dict has keys: 'url' (str), 'priority' (bool).
    Reads only WEB_ADDRESS and PRIORITY_RESOURCE columns; all others are ignored.
    Skips blank and malformed entries. Deduplicates by normalized URL.
    Priority URLs (PRIORITY_RESOURCE == 'YES', case-insensitive) sort first;
    relative order within each group is preserved.
    """
    rows: list[dict] = []
    seen: set[str] = set()

    with Path(csv_path).open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for lineno, row in enumerate(reader, start=2):
            raw_url = (row.get("WEB_ADDRESS") or "").strip()
            priority_raw = (row.get("PRIORITY_RESOURCE") or "").strip()

            if not raw_url:
                logger.warning("Row %d: skipping blank WEB_ADDRESS", lineno)
                continue

            try:
                parsed = urlparse(raw_url)
                if not parsed.scheme or not parsed.netloc:
                    raise ValueError("missing scheme or netloc")
            except Exception:
                logger.warning("Row %d: skipping malformed URL %r", lineno, raw_url)
                continue

            normalized = _normalize_url_for_dedup(raw_url)
            if normalized in seen:
                logger.warning("Row %d: duplicate URL %r — skipping", lineno, raw_url)
                continue
            seen.add(normalized)

            rows.append({"url": raw_url, "priority": priority_raw.upper() == "YES"})

    rows.sort(key=lambda r: 0 if r["priority"] else 1)
    return rows


# Convert below function into a util at Phase 10
def _registered_domain(url: str) -> str:
    ext = tldextract.extract(url)
    if ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
    return ext.domain or url


def _extract_links(html: str, base_url: str) -> list[str]:
    """Return absolute, fragment-stripped hrefs found in <a> tags."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as exc:
        logger.warning("Failed to parse HTML for link extraction from %s: %s", base_url, exc)
        return []
    links: list[str] = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip() if tag["href"] else ""
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        try:
            absolute = urljoin(base_url, href)
            parsed = urlparse(absolute)
            # Drop fragment and normalize to a clean URL
            normalized = parsed._replace(fragment="").geturl()
            links.append(normalized)
        except Exception:
            continue
    return links


def crawl_url(
    seed_url: str,
    http_client: HttpClient,
    depth: int = 2,
) -> tuple[list[PageResult], int]:
    """T-50/T-51/T-52: BFS depth crawler.

    Fetches seed_url and follows same-domain internal links up to `depth` hops.
    Returns (pages, crawl_depth_reached).

    pages: list of (url, html, http_status, js_rendered) for every URL attempted.
    crawl_depth_reached: deepest hop level with at least one HTTP-200 response
                         (0 if the seed itself failed or only seed succeeded).
    """
    pages: list[PageResult] = []
    visited: set[str] = set()
    crawl_depth_reached = 0

    seed_domain = _registered_domain(seed_url)

    # BFS queue: (url, hop_depth)
    queue: deque[tuple[str, int]] = deque([(seed_url, 0)])
    visited.add(seed_url)

    while queue:
        url, hop = queue.popleft()

        try:
            html, final_url, http_status, js_rendered = http_client.fetch_page(url)
        except Exception as exc:
            logger.warning("Fetch error for %s: %s", url, exc)
            pages.append((url, "", 0, False))
            continue

        pages.append((url, html, http_status, js_rendered))

        # Guard the resolved URL so a redirect target is never fetched twice
        if final_url != url:
            visited.add(final_url)

        if http_status != 200:
            continue

        # T-52: track the deepest hop at which we got a 200
        if hop > crawl_depth_reached:
            crawl_depth_reached = hop

        if hop >= depth:
            continue

        # T-51: enqueue only same-registered-domain links not yet visited
        for link in _extract_links(html, final_url):
            if link in visited:
                continue
            if _registered_domain(link) != seed_domain:
                continue
            visited.add(link)
            queue.append((link, hop + 1))

    return pages, crawl_depth_reached
