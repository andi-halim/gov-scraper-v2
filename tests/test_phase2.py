"""Tests for Phase 2: HttpClient and RobotsChecker."""
import time
import pytest
from unittest.mock import MagicMock, patch

import httpx

from crawler.http_client import HttpClient, USER_AGENT, DEFAULT_DELAY, _RETRY_STATUSES
from crawler.robots import RobotsChecker


def make_response(status: int, text: str = "") -> httpx.Response:
    return httpx.Response(status, text=text)


# ---------------------------------------------------------------------------
# HttpClient tests
# ---------------------------------------------------------------------------

class TestHttpClientUserAgent:
    def test_user_agent_header_on_every_request(self):
        client = HttpClient(delay=0)
        with patch.object(client._client, "get", return_value=make_response(200)) as mock_get:
            client.get("http://example.gov/page")
        mock_get.call_args.kwargs.get("headers") or {}
        # User-Agent is set on the underlying httpx.Client, not per-call.
        assert client._client.headers["user-agent"] == USER_AGENT

    def test_user_agent_is_correct_string(self):
        assert "GovScraper/2.0" in USER_AGENT
        assert "andihalim00@gmail.com" in USER_AGENT


class TestHttpClientRegisteredDomain:
    def setup_method(self):
        self.client = HttpClient(delay=0)

    def test_standard_dotgov(self):
        assert self.client._registered_domain("https://www.texas.gov/page") == "texas.gov"

    def test_state_subdomain(self):
        assert self.client._registered_domain("http://data.state.tx.us/") == "state.tx.us"

    def test_no_subdomain(self):
        assert self.client._registered_domain("https://michigan.gov/") == "michigan.gov"

    def test_two_different_subdomains_same_registered_domain(self):
        d1 = self.client._registered_domain("https://city1.example.gov/")
        d2 = self.client._registered_domain("https://city2.example.gov/")
        assert d1 == d2

    def test_localhost_no_suffix(self):
        result = self.client._registered_domain("http://localhost/path")
        assert result == "localhost"


class TestHttpClientRateLimiting:
    def test_second_request_to_same_domain_waits(self):
        delay = 0.15
        client = HttpClient(delay=delay)
        with patch.object(client._client, "get", return_value=make_response(200)):
            t0 = time.monotonic()
            client.get("http://example.gov/1")
            client.get("http://example.gov/2")
            elapsed = time.monotonic() - t0
        assert elapsed >= delay

    def test_requests_to_different_domains_not_delayed_by_each_other(self):
        delay = 0.5
        client = HttpClient(delay=delay)
        with patch.object(client._client, "get", return_value=make_response(200)):
            t0 = time.monotonic()
            client.get("http://alpha.gov/")
            client.get("http://beta.gov/")  # different domain
            elapsed = time.monotonic() - t0
        assert elapsed < delay

    def test_zero_delay_does_not_sleep(self):
        client = HttpClient(delay=0)
        with patch.object(client._client, "get", return_value=make_response(200)):
            t0 = time.monotonic()
            for _ in range(3):
                client.get("http://example.gov/")
            elapsed = time.monotonic() - t0
        assert elapsed < 0.1


class TestHttpClientRetry:
    def _client_with_responses(self, statuses):
        client = HttpClient(delay=0)
        responses = [make_response(s) for s in statuses]
        mock_get = MagicMock(side_effect=responses)
        client._client.get = mock_get
        return client, mock_get

    def test_no_retry_on_200(self):
        client, mock_get = self._client_with_responses([200])
        with patch("time.sleep"):
            resp = client.get("http://example.gov/")
        assert resp.status_code == 200
        mock_get.assert_called_once()

    def test_no_retry_on_404(self):
        client, mock_get = self._client_with_responses([404])
        with patch("time.sleep"):
            resp = client.get("http://example.gov/")
        assert resp.status_code == 404
        mock_get.assert_called_once()

    def test_retries_on_429_then_succeeds(self):
        client, mock_get = self._client_with_responses([429, 429, 200])
        with patch("time.sleep") as mock_sleep:
            resp = client.get("http://example.gov/")
        assert resp.status_code == 200
        assert mock_get.call_count == 3
        # Backoff: 1s after first 429, 2s after second 429
        sleep_calls = [c.args[0] for c in mock_sleep.call_args_list]
        assert 1 in sleep_calls
        assert 2 in sleep_calls

    def test_retries_on_503_then_succeeds(self):
        client, mock_get = self._client_with_responses([503, 200])
        with patch("time.sleep"):
            resp = client.get("http://example.gov/")
        assert resp.status_code == 200
        assert mock_get.call_count == 2

    def test_exhausts_3_retries_returns_last_response(self):
        # 1 original + 3 retries = 4 total attempts, all 429
        client, mock_get = self._client_with_responses([429, 429, 429, 429])
        with patch("time.sleep"):
            resp = client.get("http://example.gov/")
        assert resp.status_code == 429
        assert mock_get.call_count == 4

    def test_backoff_schedule_is_correct(self):
        # 4 attempts all 429: backoffs should be 1, 2, 4
        client, mock_get = self._client_with_responses([429, 429, 429, 429])
        with patch("time.sleep") as mock_sleep:
            client.get("http://example.gov/")
        sleep_args = [c.args[0] for c in mock_sleep.call_args_list]
        assert sleep_args == [1, 2, 4]

    def test_network_error_propagates_without_retry(self):
        client = HttpClient(delay=0)
        client._client.get = MagicMock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(httpx.ConnectError):
            client.get("http://example.gov/")

    def test_retry_statuses_are_429_and_503_only(self):
        assert _RETRY_STATUSES == frozenset({429, 503})


class TestHttpClientContextManager:
    def test_close_called_on_exit(self):
        client = HttpClient(delay=0)
        with patch.object(client, "close") as mock_close:
            with client:
                pass
        mock_close.assert_called_once()

    def test_returns_self_on_enter(self):
        client = HttpClient(delay=0)
        with client as c:
            assert c is client


# ---------------------------------------------------------------------------
# RobotsChecker tests
# ---------------------------------------------------------------------------

class TestRobotsCheckerCaching:
    def _make_checker(self, robots_text: str, status: int = 200):
        http_client = MagicMock()
        http_client.get.return_value = make_response(status, robots_text)
        return RobotsChecker(http_client), http_client

    def test_robots_fetched_only_once_per_netloc(self):
        checker, mock_client = self._make_checker("User-agent: *\nDisallow:")
        checker.is_allowed("http://example.gov/page1")
        checker.is_allowed("http://example.gov/page2")
        checker.is_allowed("http://example.gov/page3")
        mock_client.get.assert_called_once_with("http://example.gov/robots.txt")

    def test_different_netlocs_fetch_separately(self):
        checker, mock_client = self._make_checker("User-agent: *\nDisallow:")
        checker.is_allowed("http://alpha.gov/page")
        checker.is_allowed("http://beta.gov/page")
        assert mock_client.get.call_count == 2

    def test_correct_robots_url_is_fetched(self):
        checker, mock_client = self._make_checker("")
        checker.is_allowed("http://data.state.tx.us/datasets/list")
        mock_client.get.assert_called_once_with("http://data.state.tx.us/robots.txt")

    def test_https_scheme_preserved_in_robots_url(self):
        checker, mock_client = self._make_checker("")
        checker.is_allowed("https://opendata.cityofchicago.gov/")
        mock_client.get.assert_called_once_with("https://opendata.cityofchicago.gov/robots.txt")


class TestRobotsCheckerAllowed:
    def _checker(self, robots_text: str, status: int = 200):
        http_client = MagicMock()
        http_client.get.return_value = make_response(status, robots_text)
        return RobotsChecker(http_client)

    def test_empty_robots_allows_all(self):
        checker = self._checker("")
        allowed, status = checker.is_allowed("http://example.gov/anything")
        assert allowed
        assert status == "allowed"

    def test_wildcard_disallow_all(self):
        checker = self._checker("User-agent: *\nDisallow: /")
        allowed, status = checker.is_allowed("http://example.gov/page")
        assert not allowed
        assert status == "disallowed"

    def test_wildcard_allow_all(self):
        checker = self._checker("User-agent: *\nDisallow:")
        allowed, status = checker.is_allowed("http://example.gov/page")
        assert allowed
        assert status == "allowed"

    def test_govscraper_specific_disallow(self):
        robots = "User-agent: GovScraper\nDisallow: /\n\nUser-agent: *\nDisallow:"
        checker = self._checker(robots)
        allowed, status = checker.is_allowed("http://example.gov/page")
        assert not allowed
        assert status == "disallowed"

    def test_govscraper_specific_allow_overrides_wildcard_disallow(self):
        robots = "User-agent: *\nDisallow: /\n\nUser-agent: GovScraper\nDisallow:"
        checker = self._checker(robots)
        allowed, status = checker.is_allowed("http://example.gov/page")
        assert allowed
        assert status == "allowed"

    def test_path_specific_disallow(self):
        robots = "User-agent: *\nDisallow: /private/"
        checker = self._checker(robots)
        assert not checker.is_allowed("http://example.gov/private/data")[0]
        assert checker.is_allowed("http://example.gov/public/data")[0]


class TestRobotsCheckerFailOpen:
    def test_404_returns_unavailable_and_allows(self):
        http_client = MagicMock()
        http_client.get.return_value = make_response(404)
        checker = RobotsChecker(http_client)
        allowed, status = checker.is_allowed("http://example.gov/page")
        assert allowed
        assert status == "unavailable"

    def test_network_error_returns_unavailable_and_allows(self):
        http_client = MagicMock()
        http_client.get.side_effect = httpx.ConnectError("timeout")
        checker = RobotsChecker(http_client)
        allowed, status = checker.is_allowed("http://example.gov/page")
        assert allowed
        assert status == "unavailable"

    def test_timeout_returns_unavailable_and_allows(self):
        http_client = MagicMock()
        http_client.get.side_effect = httpx.TimeoutException("timeout")
        checker = RobotsChecker(http_client)
        allowed, status = checker.is_allowed("http://example.gov/page")
        assert allowed
        assert status == "unavailable"

    def test_500_returns_unavailable_and_allows(self):
        http_client = MagicMock()
        http_client.get.return_value = make_response(500)
        checker = RobotsChecker(http_client)
        allowed, status = checker.is_allowed("http://example.gov/page")
        assert allowed
        assert status == "unavailable"

    def test_unavailable_is_cached_no_refetch(self):
        http_client = MagicMock()
        http_client.get.side_effect = httpx.ConnectError("down")
        checker = RobotsChecker(http_client)
        checker.is_allowed("http://example.gov/a")
        checker.is_allowed("http://example.gov/b")
        http_client.get.assert_called_once()
