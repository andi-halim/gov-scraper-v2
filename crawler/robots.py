import logging
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

logger = logging.getLogger(__name__)

_GOVSCRAPER_AGENT = "GovScraper"


class RobotsChecker:
    def __init__(self, http_client) -> None:
        self._client = http_client
        # Keyed by netloc; None means unavailable (fail-open).
        self._cache: dict[str, RobotFileParser | None] = {}

    def _get_parser(self, netloc: str, scheme: str) -> RobotFileParser | None:
        if netloc in self._cache:
            return self._cache[netloc]

        robots_url = f"{scheme}://{netloc}/robots.txt"
        parser: RobotFileParser | None = None
        try:
            response = self._client.get(robots_url)
            if response.status_code == 200:
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
            else:
                logger.warning(
                    "robots.txt unavailable for %s (HTTP %d)",
                    netloc,
                    response.status_code,
                )
        except Exception as exc:
            logger.warning("Could not fetch robots.txt for %s: %s", netloc, exc)

        self._cache[netloc] = parser
        return parser

    def is_allowed(self, url: str) -> tuple[bool, str]:
        parsed = urlparse(url)
        parser = self._get_parser(parsed.netloc, parsed.scheme)

        if parser is None:
            return True, "unavailable"

        allowed = parser.can_fetch(_GOVSCRAPER_AGENT, url)
        return allowed, "allowed" if allowed else "disallowed"
