"""Phase 5 depth crawler (T-50–T-52)."""
import logging
from collections import deque
from urllib.parse import urljoin, urlparse

import tldextract
from bs4 import BeautifulSoup

from crawler.http_client import HttpClient

logger = logging.getLogger(__name__)

# Namedtuple-style alias for the per-page result
# (url, html, http_status, js_rendered)
PageResult = tuple[str, str, int, bool]

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
        href = tag["href"].strip()
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
