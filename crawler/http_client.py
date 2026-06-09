import re
import time
import logging
import unicodedata

import httpx
from bs4 import BeautifulSoup
from utils import registered_domain as _registered_domain

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

_CF_HEADER = "cf-ray"
_CF_SERVER = "cloudflare"
_AKAMAI_SERVER = "akamaiGHost"
_CF_BODY_RE = re.compile(r'window\._cf_chl_opt|cf-browser-verification', re.IGNORECASE)
_AZURE_WAF_BODY_RE = re.compile(r'Azure WAF JS Challenge', re.IGNORECASE)


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


def _is_bot_challenge(html: str, status_code: int, headers: dict) -> bool:
    """Returns True if the response is a CDN bot-protection challenge (Cloudflare, Akamai).

    Only fires on 403 for header-based signals so that legitimate Cloudflare-proxied
    200 responses are not mistakenly retried. Body tokens fire on any status to catch
    Cloudflare JS challenges served as 200.
    """
    lower = {k.lower(): v.lower() for k, v in (headers or {}).items()}
    server = lower.get("server", "")
    is_cloudflare = _CF_HEADER in lower or _CF_SERVER in server
    if is_cloudflare and status_code == 403:
        return True
    if html and (_CF_BODY_RE.search(html) or _AZURE_WAF_BODY_RE.search(html)):
        return True
    if _AKAMAI_SERVER.lower() in server and status_code == 403:
        return True
    return False


class HttpClient:
    def __init__(self, delay: float = DEFAULT_DELAY) -> None:
        self._delay = delay
        self._last_request: dict[str, float] = {}
        # Headers from the most recent fetch_page() call; read by run.py for portal detection.
        self.last_response_headers: dict = {}
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=httpx.Timeout(_CONNECT_TIMEOUT, read=_READ_TIMEOUT),
        )
    
    def _wait_for_rate_limit(self, domain: str) -> None:
        last = self._last_request.get(domain, 0.0)
        wait = self._delay - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)

    def get(self, url: str, **kwargs) -> httpx.Response:
        domain = _registered_domain(url)
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

    def head(self, url: str, **kwargs) -> httpx.Response:
        """T-61: Issue a HEAD request with rate limiting. No response body is downloaded."""
        domain = _registered_domain(url)
        self._wait_for_rate_limit(domain)
        self._last_request[domain] = time.monotonic()
        return self._client.head(url, **kwargs)

    def fetch_page(self, url: str) -> tuple[str, str, int, bool, bool]:
        """T-40: GET url, falling back to Playwright for JS-heavy or bot-challenged pages.

        Returns (html, final_url, http_status, js_rendered, cdn_blocked).
        cdn_blocked is True when a CDN bot-challenge was detected but Playwright
        failed to bypass it; False in all other cases (including successful bypass).
        Network errors propagate to the caller.
        """
        response = self.get(url)
        self.last_response_headers = dict(response.headers)
        html = response.text
        final_url = str(response.url)
        http_status = response.status_code
        js_rendered = False
        cdn_blocked = False
        _playwright_tried = False

        if http_status == 200:
            content_type = response.headers.get("content-type", "")
            ct_base = (content_type or "").lower().split(";")[0].strip()
            if _is_js_heavy(html, content_type) and (not ct_base or ct_base == "text/html"):
                _playwright_tried = True
                try:
                    from crawler.playwright_client import fetch_rendered
                    html = fetch_rendered(final_url)
                    js_rendered = True
                except Exception as exc:
                    logger.warning("Playwright render failed for %s: %s", url, exc)

        headers = self.last_response_headers
        if not _playwright_tried and _is_bot_challenge(html, http_status, headers):
            cdn_blocked = True  # assume blocked until bypass succeeds
            try:
                from crawler.playwright_client import fetch_rendered
                pw_html = fetch_rendered(final_url)
                if not _is_bot_challenge(pw_html, 200, {}):
                    html = pw_html
                    http_status = 200
                    js_rendered = True
                    cdn_blocked = False
                else:
                    logger.warning("Playwright did not bypass bot protection for %s", url)
            except Exception as exc:
                logger.warning("Playwright bot-bypass attempt failed for %s: %s", url, exc)

        return html, final_url, http_status, js_rendered, cdn_blocked

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
