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


# ---------------------------------------------------------------------------
# T-43/T-44: PortalDetector passive detection
# ---------------------------------------------------------------------------

SOCRATA_HTML = """
<html><head></head><body>
<footer>Powered by Socrata</footer>
</body></html>
"""

SOCRATA_HEADER_HTML = "<html><body>some content here</body></html>"

CKAN_HTML = """
<html><head>
<meta name="generator" content="ckan 2.9.6" />
</head><body class="ckan-body">
<a href="/dataset">Browse datasets</a>
</body></html>
"""

ARCGIS_HTML = """
<html><head>
<meta property="og:site_name" content="ArcGIS Hub" />
</head><body>
<hub-hero></hub-hero>
</body></html>
"""

PLAIN_HTML = """
<html><head><title>State Finance Department</title></head>
<body><h1>Welcome to State Finance</h1><p>Budget reports available.</p></body>
</html>
"""


class TestPortalDetectorPassive(unittest.TestCase):
    def _detector(self) -> PortalDetector:
        return PortalDetector(MagicMock())

    def test_socrata_footer_text(self):
        det = self._detector()
        platform, method = det.detect(SOCRATA_HTML, {}, "https://data.example.gov")
        self.assertEqual(platform, "Socrata")
        self.assertEqual(method, "passive")

    def test_socrata_header(self):
        det = self._detector()
        headers = {"X-Socrata-RequestId": "abc123"}
        platform, method = det.detect(PLAIN_HTML, headers, "https://data.example.gov")
        self.assertEqual(platform, "Socrata")
        self.assertEqual(method, "passive")

    def test_socrata_tyler_footer(self):
        html = PLAIN_HTML.replace("</body>", "<footer>Powered by Tyler Data &amp; Insights</footer></body>")
        det = self._detector()
        platform, _ = det.detect(html, {}, "https://data.example.gov")
        self.assertEqual(platform, "Socrata")

    def test_ckan_meta_generator(self):
        det = self._detector()
        platform, method = det.detect(CKAN_HTML, {}, "https://data.example.gov")
        self.assertEqual(platform, "CKAN")
        self.assertEqual(method, "passive")

    def test_ckan_js_snippet(self):
        html = "<html><body><script>ckan.module('map', function(){})</script></body></html>"
        det = self._detector()
        platform, _ = det.detect(html, {}, "https://data.example.gov")
        self.assertEqual(platform, "CKAN")

    def test_ckan_body_class(self):
        html = '<html><body class="ckan-home"><p>Content</p></body></html>'
        det = self._detector()
        platform, _ = det.detect(html, {}, "https://data.example.gov")
        self.assertEqual(platform, "CKAN")

    def test_arcgis_hub_component(self):
        det = self._detector()
        platform, method = det.detect(ARCGIS_HTML, {}, "https://opendata.example.gov")
        self.assertEqual(platform, "ArcGIS Hub")
        self.assertEqual(method, "passive")

    def test_arcgis_hub_domain(self):
        det = self._detector()
        platform, method = det.detect(PLAIN_HTML, {}, "https://opendata.dc.opendata.arcgis.com")
        self.assertEqual(platform, "ArcGIS Hub")
        self.assertEqual(method, "passive")

    def test_arcgis_script_domain(self):
        html = '<html><head><script src="https://js.arcgis.com/4.28/"></script></head></html>'
        det = self._detector()
        platform, _ = det.detect(html, {}, "https://gis.example.gov")
        self.assertEqual(platform, "ArcGIS Hub")

    def test_plain_page_returns_none(self):
        det = self._detector()
        platform, method = det.detect(PLAIN_HTML, {}, "https://finance.state.gov")
        self.assertIsNone(platform)
        self.assertEqual(method, "none")

    def test_returns_none_on_empty_html(self):
        det = self._detector()
        platform, method = det.detect("", {}, "https://example.gov")
        self.assertIsNone(platform)


# ---------------------------------------------------------------------------
# PortalDetector active probe
# ---------------------------------------------------------------------------

class TestPortalDetectorActiveProbe(unittest.TestCase):
    def _detector_with_probe_response(self, platform_confirmed: str, probe_data: dict):
        mock_client = MagicMock()
        resp = make_json_response(200, probe_data)
        mock_client.get.return_value = resp
        return PortalDetector(mock_client)

    def test_active_probe_socrata_wins_on_ambiguous(self):
        # HTML with signals for both Socrata and CKAN
        ambiguous_html = (
            SOCRATA_HTML.replace("</body>", '<script>ckan.module("x",function(){})</script></body>')
        )
        mock_client = MagicMock()
        # Probe: Socrata succeeds, CKAN fails
        def probe_side_effect(url):
            if "/api/catalog/v1" in url:
                return make_json_response(200, {"results": []})
            if "/api/3/action/site_read" in url:
                return make_json_response(200, {"success": True})
            return make_response(404)
        mock_client.get.side_effect = probe_side_effect
        det = PortalDetector(mock_client)
        platform, method = det.detect(ambiguous_html, {}, "https://data.example.gov")
        self.assertIn(platform, ("Socrata", "CKAN"))
        self.assertEqual(method, "probe")

    def test_active_probe_failure_returns_none(self):
        # Ambiguous signals but all probes fail
        ambiguous = SOCRATA_HTML.replace("</body>", '<script>ckan.module("x",fn)</script></body>')
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(404)
        det = PortalDetector(mock_client)
        platform, method = det.detect(ambiguous, {}, "https://data.example.gov")
        self.assertIsNone(platform)

    def test_single_passive_match_skips_probe(self):
        mock_client = MagicMock()
        det = PortalDetector(mock_client)
        platform, method = det.detect(SOCRATA_HTML, {}, "https://data.example.gov")
        self.assertEqual(platform, "Socrata")
        self.assertEqual(method, "passive")
        mock_client.get.assert_not_called()


# ---------------------------------------------------------------------------
# T-46: SocrataAdapter
# ---------------------------------------------------------------------------

class TestSocrataAdapter(unittest.TestCase):
    def _mock_client(self, pages: list[dict]):
        """pages is a list of catalog API response dicts (one per page request)."""
        mock_client = MagicMock()
        mock_client.get.side_effect = [make_json_response(200, p) for p in pages]
        return mock_client

    def _make_dataset(self, name: str, description: str, tags: list, permalink: str) -> dict:
        return {
            "resource": {"name": name, "description": description},
            "classification": {"domain_tags": tags},
            "permalink": permalink,
        }

    def test_single_page_result(self):
        ds = self._make_dataset(
            "County Budget 2022", "Annual budget by county", ["county", "budget"], "https://data.ex.gov/d/abc"
        )
        client = self._mock_client([{"results": [ds]}])
        adapter = SocrataAdapter("https://data.ex.gov", frozenset({"county", "budget"}), client)
        result = adapter.run()
        self.assertEqual(result["portal_dataset_count"], 1)
        self.assertGreater(result["relevance_score"], 0)
        self.assertIn("https://data.ex.gov/d/abc", result["top_dataset_urls"])

    def test_pagination_stops_when_page_smaller_than_limit(self):
        ds = self._make_dataset("DS1", "", [], "https://ex.gov/d/1")
        # First page returns 100 results (simulate by checking call count)
        # Simplified: return only 1 result which is < 100 so pagination stops
        client = self._mock_client([{"results": [ds]}])
        adapter = SocrataAdapter("https://ex.gov", frozenset(), client)
        adapter.run()
        self.assertEqual(client.get.call_count, 1)

    def test_pagination_continues_on_full_page(self):
        page1 = {"results": [self._make_dataset(f"D{i}", "", [], f"http://e.gov/{i}") for i in range(100)]}
        page2 = {"results": [self._make_dataset("Last", "", [], "http://e.gov/last")]}
        client = self._mock_client([page1, page2])
        adapter = SocrataAdapter("https://ex.gov", frozenset(), client)
        result = adapter.run()
        self.assertEqual(result["portal_dataset_count"], 101)
        self.assertEqual(client.get.call_count, 2)

    def test_http_error_stops_pagination_gracefully(self):
        client = MagicMock()
        client.get.return_value = make_response(500)
        adapter = SocrataAdapter("https://ex.gov", frozenset(), client)
        result = adapter.run()
        self.assertEqual(result["portal_dataset_count"], 0)

    def test_zero_keywords_gives_zero_score(self):
        ds = self._make_dataset("County Budget", "All about counties", ["county"], "http://e.gov/d/1")
        client = self._mock_client([{"results": [ds]}])
        adapter = SocrataAdapter("https://ex.gov", frozenset(), client)
        result = adapter.run()
        self.assertEqual(result["relevance_score"], 0)

    def test_top_urls_capped_at_10(self):
        datasets = [
            self._make_dataset(f"D{i}", "county budget data", ["county"], f"http://e.gov/{i}")
            for i in range(20)
        ]
        client = self._mock_client([{"results": datasets}])
        adapter = SocrataAdapter("https://ex.gov", frozenset({"county"}), client)
        result = adapter.run()
        self.assertLessEqual(len(result["top_dataset_urls"]), 10)


# ---------------------------------------------------------------------------
# T-47: CKANAdapter
# ---------------------------------------------------------------------------

class TestCKANAdapter(unittest.TestCase):
    def _mock_client(self, pages: list[dict]):
        mock_client = MagicMock()
        mock_client.get.side_effect = [make_json_response(200, p) for p in pages]
        return mock_client

    def _make_dataset(self, title: str, notes: str, tags: list[str], url: str) -> dict:
        return {
            "title": title,
            "notes": notes,
            "tags": [{"name": t} for t in tags],
            "resources": [{"url": url, "format": "CSV"}],
        }

    def test_single_page(self):
        ds = self._make_dataset("School Districts 2022", "Enrollment data", ["school", "district"], "https://data.gov/r/abc.csv")
        page = {"success": True, "result": {"results": [ds]}}
        client = self._mock_client([page])
        adapter = CKANAdapter("https://data.gov", frozenset({"school", "district"}), client)
        result = adapter.run()
        self.assertEqual(result["portal_dataset_count"], 1)
        self.assertGreater(result["relevance_score"], 0)

    def test_stops_on_success_false(self):
        page = {"success": False, "result": {"results": []}}
        client = self._mock_client([page])
        adapter = CKANAdapter("https://data.gov", frozenset(), client)
        result = adapter.run()
        self.assertEqual(result["portal_dataset_count"], 0)

    def test_pagination(self):
        page1 = {"success": True, "result": {"results": [
            self._make_dataset(f"DS{i}", "", [], f"http://d.gov/{i}") for i in range(100)
        ]}}
        page2 = {"success": True, "result": {"results": [
            self._make_dataset("Last", "", [], "http://d.gov/last")
        ]}}
        client = self._mock_client([page1, page2])
        adapter = CKANAdapter("https://d.gov", frozenset(), client)
        result = adapter.run()
        self.assertEqual(result["portal_dataset_count"], 101)

    def test_matched_keywords_union_across_datasets(self):
        ds1 = self._make_dataset("Counties", "county data", ["county"], "http://d.gov/1")
        ds2 = self._make_dataset("Townships", "township boundaries", ["township"], "http://d.gov/2")
        page = {"success": True, "result": {"results": [ds1, ds2]}}
        client = self._mock_client([page])
        adapter = CKANAdapter("https://d.gov", frozenset({"county", "township"}), client)
        result = adapter.run()
        self.assertIn("county", result["matched_keywords"])
        self.assertIn("township", result["matched_keywords"])


# ---------------------------------------------------------------------------
# T-48: ArcGISHubAdapter
# ---------------------------------------------------------------------------

class TestArcGISHubAdapter(unittest.TestCase):
    def _mock_client(self, pages: list[dict]):
        mock_client = MagicMock()
        mock_client.get.side_effect = [make_json_response(200, p) for p in pages]
        return mock_client

    def _make_dataset(self, name: str, description: str, tags: list[str], download_url: str) -> dict:
        return {
            "attributes": {
                "name": name,
                "description": description,
                "tags": tags,
                "access": {"urls": {"download": download_url}},
            }
        }

    def test_single_page_no_next(self):
        ds = self._make_dataset("Municipal Boundaries", "City limits data", ["municipal", "boundary"], "https://hub.ex.com/d/abc.csv")
        page = {"data": [ds], "meta": {}}
        client = self._mock_client([page])
        adapter = ArcGISHubAdapter("https://hub.ex.com", frozenset({"municipal"}), client)
        result = adapter.run()
        self.assertEqual(result["portal_dataset_count"], 1)
        self.assertGreater(result["relevance_score"], 0)

    def test_pagination_follows_meta_next(self):
        page1 = {
            "data": [self._make_dataset(f"D{i}", "", [], f"http://e.com/{i}") for i in range(100)],
            "meta": {"next": "/api/v3/datasets?page[size]=100&page[number]=2"},
        }
        page2 = {
            "data": [self._make_dataset("Last", "", [], "http://e.com/last")],
            "meta": {},
        }
        client = self._mock_client([page1, page2])
        adapter = ArcGISHubAdapter("https://e.com", frozenset(), client)
        result = adapter.run()
        self.assertEqual(result["portal_dataset_count"], 101)

    def test_empty_catalog(self):
        page = {"data": [], "meta": {}}
        client = self._mock_client([page])
        adapter = ArcGISHubAdapter("https://hub.ex.com", frozenset({"county"}), client)
        result = adapter.run()
        self.assertEqual(result["portal_dataset_count"], 0)
        self.assertEqual(result["relevance_score"], 0)


# ---------------------------------------------------------------------------
# portals.score_metadata
# ---------------------------------------------------------------------------

class TestScoreMetadata(unittest.TestCase):
    def test_exact_keyword_match(self):
        score, matched = score_metadata("county budget report", frozenset({"county", "budget"}))
        self.assertEqual(len(matched), 2)
        self.assertGreater(score, 0)

    def test_no_match_gives_zero(self):
        score, matched = score_metadata("sports and recreation", frozenset({"county", "budget"}))
        self.assertEqual(score, 0)
        self.assertEqual(matched, [])

    def test_empty_keywords_gives_zero(self):
        score, matched = score_metadata("county data", frozenset())
        self.assertEqual(score, 0)

    def test_case_insensitive(self):
        score, matched = score_metadata("COUNTY budget", frozenset({"county"}))
        self.assertGreater(score, 0)
        self.assertIn("county", matched)

    def test_whole_word_boundary(self):
        # "county" in "countywide" should NOT match
        score, _ = score_metadata("countywide analysis", frozenset({"county"}))
        self.assertEqual(score, 0)

    def test_score_capped_at_100(self):
        keywords = frozenset({"a", "b"})
        score, _ = score_metadata("a b a b a b", keywords)
        self.assertLessEqual(score, 100)

    def test_diacritic_normalization(self):
        # "école" should match "ecole" after diacritic stripping
        score, matched = score_metadata("école primaire", frozenset({"ecole"}))
        self.assertGreater(score, 0)

    def test_matched_keywords_sorted(self):
        _, matched = score_metadata("county municipal township", frozenset({"township", "county", "municipal"}))
        self.assertEqual(matched, sorted(matched))


if __name__ == "__main__":
    unittest.main()
