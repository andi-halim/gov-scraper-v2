import re
import time
import logging
import unicodedata

import httpx
import tldextract
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = "GovScraper/2.0 (contact: andihalim00@gmail.com)"
DEFAULT_DELAY = 2.0
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 30.0
_MAX_RETRIES = 3
_RETRY_STATUSES = frozenset({429, 503})
_BACKOFF_SCHEDULE = (1, 2, 4)

_MIN_VISIBLE_TEXT = 200
_JS_ROOT_RE = re.compile(r'<div\s+id=["\'](?:root|app)["\']', re.IGNORECASE)


def _visible_text(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "lxml")
        return soup.get_text(" ", strip=True)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)


def _is_js_heavy(html: str, content_type: str) -> bool:
    """T-41: Returns True if the page is likely JS-rendered and needs Playwright."""
    ct = (content_type or "").lower().split(";")[0].strip()
    # Non-HTML response types are flagged so the caller can decide
    if ct and ct != "text/html":
        return True
    visible = _visible_text(html)
    if len(visible) < _MIN_VISIBLE_TEXT:
        return True
    # SPA root container with minimal surrounding text
    if _JS_ROOT_RE.search(html) and len(visible) < _MIN_VISIBLE_TEXT * 2:
        return True
    return False


class HttpClient:
    def __init__(self, delay: float = DEFAULT_DELAY) -> None:
        self._delay = delay
        self._last_request: dict[str, float] = {}
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=httpx.Timeout(_CONNECT_TIMEOUT, read=_READ_TIMEOUT),
        )

    def _registered_domain(self, url: str) -> str:
        ext = tldextract.extract(url)
        if ext.suffix:
            return f"{ext.domain}.{ext.suffix}"
        return ext.domain or url

    def _wait_for_rate_limit(self, domain: str) -> None:
        last = self._last_request.get(domain, 0.0)
        wait = self._delay - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)

    def get(self, url: str, **kwargs) -> httpx.Response:
        domain = self._registered_domain(url)
        self._wait_for_rate_limit(domain)

        last_response: httpx.Response | None = None
        for attempt in range(_MAX_RETRIES + 1):
            if attempt > 0:
                backoff = _BACKOFF_SCHEDULE[attempt - 1]
                logger.warning(
                    "HTTP %d for %s; retrying in %ds (attempt %d/%d)",
                    last_response.status_code,  # type: ignore[union-attr]
                    url,
                    backoff,
                    attempt,
                    _MAX_RETRIES,
                )
                time.sleep(backoff)

            self._last_request[domain] = time.monotonic()
            last_response = self._client.get(url, **kwargs)

            if last_response.status_code not in _RETRY_STATUSES:
                return last_response

        return last_response  # type: ignore[return-value]  # exhausted retries

    def fetch_page(self, url: str) -> tuple[str, str, int, bool]:
        """T-40: GET url, falling back to Playwright for JS-heavy pages.

        Returns (html, final_url, http_status, js_rendered).
        Network errors propagate to the caller.
        """
        response = self.get(url)
        html = response.text
        final_url = str(response.url)
        http_status = response.status_code
        js_rendered = False

        if http_status == 200:
            content_type = response.headers.get("content-type", "")
            ct_base = (content_type or "").lower().split(";")[0].strip()
            if _is_js_heavy(html, content_type) and (not ct_base or ct_base == "text/html"):
                try:
                    from crawler.playwright_client import fetch_rendered
                    html = fetch_rendered(final_url)
                    js_rendered = True
                except Exception as exc:
                    logger.warning("Playwright render failed for %s: %s", url, exc)

        return html, final_url, http_status, js_rendered

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
