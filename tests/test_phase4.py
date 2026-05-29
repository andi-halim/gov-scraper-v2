"""Unit tests for Phase 4 (T-40–T-42) and Phase 4B (T-43–T-48)."""
import unittest
from unittest.mock import MagicMock, patch

import httpx

from crawler.http_client import HttpClient, _is_js_heavy, _visible_text
from crawler.portal_detector import PortalDetector
from portals import score_metadata
from portals.socrata import SocrataAdapter
from portals.ckan import CKANAdapter
from portals.arcgis_hub import ArcGISHubAdapter


def make_response(
    status: int,
    text: str = "",
    headers: dict | None = None,
    url: str = "http://example.gov/",
) -> httpx.Response:
    resp = httpx.Response(status, text=text, headers=httpx.Headers(headers or {}))
    resp.request = httpx.Request("GET", url)
    return resp


def make_json_response(
    status: int, data: dict, headers: dict | None = None, url: str = "http://example.gov/"
) -> httpx.Response:
    import json
    h = {"content-type": "application/json"}
    if headers:
        h.update(headers)
    resp = httpx.Response(status, text=json.dumps(data), headers=httpx.Headers(h))
    resp.request = httpx.Request("GET", url)
    return resp


# ---------------------------------------------------------------------------
# _visible_text helper
# ---------------------------------------------------------------------------

class TestVisibleText(unittest.TestCase):
    def test_strips_tags(self):
        result = _visible_text("<h1>Hello</h1><p>World</p>")
        self.assertIn("Hello", result)
        self.assertIn("World", result)
        self.assertNotIn("<h1>", result)

    def test_empty_html(self):
        self.assertEqual(_visible_text("").strip(), "")

    def test_script_content_excluded(self):
        result = _visible_text("<html><body><script>var x=1;</script>Real text here</body></html>")
        self.assertIn("Real text here", result)
        self.assertNotIn("var x=1", result)


# ---------------------------------------------------------------------------
# T-41: _is_js_heavy
# ---------------------------------------------------------------------------

class TestIsJsHeavy(unittest.TestCase):
    def _rich_html(self, extra: str = "") -> str:
        return (
            "<html><body>" + ("x " * 120) + extra + "</body></html>"
        )

    def test_short_visible_text_flagged(self):
        self.assertTrue(_is_js_heavy("<html><body>Hi</body></html>", "text/html"))

    def test_rich_page_not_flagged(self):
        self.assertFalse(_is_js_heavy(self._rich_html(), "text/html"))

    def test_non_html_content_type_flagged(self):
        self.assertTrue(_is_js_heavy(self._rich_html(), "application/json"))

    def test_content_type_with_charset_still_works(self):
        self.assertFalse(_is_js_heavy(self._rich_html(), "text/html; charset=utf-8"))

    def test_empty_content_type_treated_as_html(self):
        self.assertTrue(_is_js_heavy("<html><body>short</body></html>", ""))

    def test_div_root_with_minimal_text_flagged(self):
        html = '<html><body><div id="root">loading...</div></body></html>'
        self.assertTrue(_is_js_heavy(html, "text/html"))

    def test_div_app_with_minimal_text_flagged(self):
        html = '<html><body><div id="app"></div></body></html>'
        self.assertTrue(_is_js_heavy(html, "text/html"))

    def test_over_200_chars_not_flagged(self):
        # "word " * 50 → ~249 visible chars after BS4 stripping — safely above threshold
        text = "word " * 50
        html = f"<html><body>{text}</body></html>"
        self.assertFalse(_is_js_heavy(html, "text/html"))


# ---------------------------------------------------------------------------
# T-40: HttpClient.fetch_page
# ---------------------------------------------------------------------------

class TestFetchPage(unittest.TestCase):
    def _make_client(self, response: httpx.Response) -> HttpClient:
        client = HttpClient(delay=0)
        client._client.get = MagicMock(return_value=response)
        return client

    def test_returns_html_final_url_status_rendered_false_on_rich_page(self):
        rich = "<html><body>" + "word " * 60 + "</body></html>"
        resp = make_response(200, rich, {"content-type": "text/html"})
        client = self._make_client(resp)
        html, final_url, status, rendered = client.fetch_page("http://example.gov/")
        self.assertEqual(status, 200)
        self.assertFalse(rendered)
        self.assertIn("word", html)

    def test_non_200_never_triggers_playwright(self):
        resp = make_response(404, "Not found", {"content-type": "text/html"})
        client = self._make_client(resp)
        with patch("crawler.playwright_client.fetch_rendered") as mock_pw:
            html, _, status, rendered = client.fetch_page("http://example.gov/")
        mock_pw.assert_not_called()
        self.assertEqual(status, 404)
        self.assertFalse(rendered)

    def test_js_heavy_triggers_playwright(self):
        import crawler.playwright_client as pwc
        sparse = "<html><body><div id='root'></div></body></html>"
        resp = make_response(200, sparse, {"content-type": "text/html"})
        client = self._make_client(resp)
        rendered_html = "<html><body>fully rendered content here now</body></html>"
        with patch.object(pwc, "fetch_rendered", return_value=rendered_html):
            with patch("crawler.http_client._is_js_heavy", return_value=True):
                html, _, status, rendered = client.fetch_page("http://example.gov/")
        self.assertTrue(rendered)
        self.assertEqual(html, rendered_html)

    def test_playwright_failure_falls_back_to_plain_html(self):
        import crawler.playwright_client as pwc
        sparse = "<html><body><div id='root'></div></body></html>"
        resp = make_response(200, sparse, {"content-type": "text/html"})
        client = self._make_client(resp)
        with patch.object(pwc, "fetch_rendered", side_effect=RuntimeError("browser crash")):
            with patch("crawler.http_client._is_js_heavy", return_value=True):
                html, _, status, rendered = client.fetch_page("http://example.gov/")
        self.assertFalse(rendered)
        self.assertEqual(html, sparse)

    def test_non_html_content_type_does_not_trigger_playwright(self):
        resp = make_response(200, "{}", {"content-type": "application/json"})
        client = self._make_client(resp)
        with patch("crawler.playwright_client.fetch_rendered") as mock_pw:
            _, _, status, rendered = client.fetch_page("http://example.gov/api")
        mock_pw.assert_not_called()
        self.assertFalse(rendered)


if __name__ == "__main__":
    unittest.main()
