import time
import logging

import httpx
import tldextract

logger = logging.getLogger(__name__)

USER_AGENT = "GovScraper/2.0 (contact: andihalim00@gmail.com)"
DEFAULT_DELAY = 2.0
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 30.0
_MAX_RETRIES = 3
_RETRY_STATUSES = frozenset({429, 503})
_BACKOFF_SCHEDULE = (1, 2, 4)


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

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
