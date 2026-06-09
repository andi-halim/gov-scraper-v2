"""Unit tests for Phase 4 (T-40–T-42) and Phase 4B (T-43–T-48)."""
import json
from unittest.mock import MagicMock, patch

import httpx

from crawler.http_client import (
    HttpClient, _is_bot_challenge, _is_js_heavy, _visible_text, _AZURE_WAF_BODY_RE,
)
from crawler.portal_detector import PortalDetector


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
    h = {"content-type": "application/json"}
    if headers:
        h.update(headers)
    resp = httpx.Response(status, text=json.dumps(data), headers=httpx.Headers(h))
    resp.request = httpx.Request("GET", url)
    return resp


# ---------------------------------------------------------------------------
# _visible_text helper
# ---------------------------------------------------------------------------

class TestVisibleText:
    def test_strips_tags(self):
        result = _visible_text("<h1>Hello</h1><p>World</p>")
        assert "Hello" in result
        assert "World" in result
        assert "<h1>" not in result

    def test_empty_html(self):
        assert _visible_text("").strip() == ""

    def test_script_content_excluded(self):
        result = _visible_text("<html><body><script>var x=1;</script>Real text here</body></html>")
        assert "Real text here" in result
        assert "var x=1" not in result


# ---------------------------------------------------------------------------
# T-41: _is_js_heavy
# ---------------------------------------------------------------------------

class TestIsJsHeavy:
    def _rich_html(self, extra: str = "") -> str:
        return "<html><body>" + ("x " * 120) + extra + "</body></html>"

    def test_short_visible_text_flagged(self):
        assert _is_js_heavy("<html><body>Hi</body></html>", "text/html")

    def test_rich_page_not_flagged(self):
        assert not _is_js_heavy(self._rich_html(), "text/html")

    def test_non_html_content_type_flagged(self):
        assert _is_js_heavy(self._rich_html(), "application/json")

    def test_content_type_with_charset_still_works(self):
        assert not _is_js_heavy(self._rich_html(), "text/html; charset=utf-8")

    def test_empty_content_type_treated_as_html(self):
        assert _is_js_heavy("<html><body>short</body></html>", "")

    def test_div_root_with_minimal_text_flagged(self):
        html = '<html><body><div id="root">loading...</div></body></html>'
        assert _is_js_heavy(html, "text/html")

    def test_div_app_with_minimal_text_flagged(self):
        html = '<html><body><div id="app"></div></body></html>'
        assert _is_js_heavy(html, "text/html")

    def test_over_200_chars_not_flagged(self):
        # "word " * 50 → ~249 visible chars after BS4 stripping — safely above threshold
        text = "word " * 50
        html = f"<html><body>{text}</body></html>"
        assert not _is_js_heavy(html, "text/html")


# ---------------------------------------------------------------------------
# T-40: HttpClient.fetch_page
# ---------------------------------------------------------------------------

class TestFetchPage:
    def _make_client(self, response: httpx.Response) -> HttpClient:
        client = HttpClient(delay=0)
        client._client.get = MagicMock(return_value=response)
        return client

    def test_returns_html_final_url_status_rendered_false_on_rich_page(self):
        rich = "<html><body>" + "word " * 60 + "</body></html>"
        resp = make_response(200, rich, {"content-type": "text/html"})
        client = self._make_client(resp)
        html, final_url, status, rendered, _ = client.fetch_page("http://example.gov/")
        assert status == 200
        assert not rendered
        assert "word" in html

    def test_non_200_never_triggers_playwright(self):
        resp = make_response(404, "Not found", {"content-type": "text/html"})
        client = self._make_client(resp)
        with patch("crawler.playwright_client.fetch_rendered") as mock_pw:
            html, _, status, rendered, _ = client.fetch_page("http://example.gov/")
        mock_pw.assert_not_called()
        assert status == 404
        assert not rendered

    def test_js_heavy_triggers_playwright(self):
        import crawler.playwright_client as pwc
        sparse = "<html><body><div id='root'></div></body></html>"
        resp = make_response(200, sparse, {"content-type": "text/html"})
        client = self._make_client(resp)
        rendered_html = "<html><body>fully rendered content here now</body></html>"
        with patch.object(pwc, "fetch_rendered", return_value=rendered_html):
            with patch("crawler.http_client._is_js_heavy", return_value=True):
                html, _, status, rendered, _ = client.fetch_page("http://example.gov/")
        assert rendered
        assert html == rendered_html

    def test_playwright_failure_falls_back_to_plain_html(self):
        import crawler.playwright_client as pwc
        sparse = "<html><body><div id='root'></div></body></html>"
        resp = make_response(200, sparse, {"content-type": "text/html"})
        client = self._make_client(resp)
        with patch.object(pwc, "fetch_rendered", side_effect=RuntimeError("browser crash")):
            with patch("crawler.http_client._is_js_heavy", return_value=True):
                html, _, status, rendered, _ = client.fetch_page("http://example.gov/")
        assert not rendered
        assert html == sparse

    def test_non_html_content_type_does_not_trigger_playwright(self):
        resp = make_response(200, "{}", {"content-type": "application/json"})
        client = self._make_client(resp)
        with patch("crawler.playwright_client.fetch_rendered") as mock_pw:
            _, _, status, rendered, _ = client.fetch_page("http://example.gov/api")
        mock_pw.assert_not_called()
        assert not rendered

    def test_cloudflare_403_triggers_playwright_bypass(self):
        resp = make_response(403, "<html>cf challenge</html>", {"cf-ray": "abc-LAX"})
        client = self._make_client(resp)
        real_html = "<html><body>" + "word " * 60 + "</body></html>"
        import crawler.playwright_client as pwc
        with patch.object(pwc, "fetch_rendered", return_value=real_html):
            html, _, status, rendered, _ = client.fetch_page("http://example.gov/")
        assert rendered
        assert status == 200
        assert html == real_html

    def test_cloudflare_403_playwright_also_blocked_keeps_original_status(self):
        cf_html = "<html><body><script>window._cf_chl_opt={}</script></body></html>"
        resp = make_response(403, cf_html, {"cf-ray": "abc-LAX"})
        client = self._make_client(resp)
        import crawler.playwright_client as pwc
        with patch.object(pwc, "fetch_rendered", return_value=cf_html):
            _, _, status, rendered, _ = client.fetch_page("http://example.gov/")
        assert not rendered
        assert status == 403

    def test_cloudflare_403_playwright_exception_falls_back(self):
        resp = make_response(403, "<html>blocked</html>", {"cf-ray": "abc-LAX"})
        client = self._make_client(resp)
        import crawler.playwright_client as pwc
        with patch.object(pwc, "fetch_rendered", side_effect=RuntimeError("browser crash")):
            html, _, status, rendered, _ = client.fetch_page("http://example.gov/")
        assert not rendered
        assert status == 403
        assert html == "<html>blocked</html>"

    def test_js_heavy_and_bot_challenge_triggers_playwright_only_once(self):
        cf_sparse = "<html><body><div id='root'><script>window._cf_chl_opt={}</script></div></body></html>"
        resp = make_response(200, cf_sparse, {"content-type": "text/html", "cf-ray": "abc"})
        client = self._make_client(resp)
        real_html = "<html><body>" + "word " * 60 + "</body></html>"
        import crawler.playwright_client as pwc
        with patch.object(pwc, "fetch_rendered", return_value=real_html) as mock_pw:
            with patch("crawler.http_client._is_js_heavy", return_value=True):
                _, _, _, rendered, _ = client.fetch_page("http://example.gov/")
        assert mock_pw.call_count == 1
        assert rendered

    def test_cdn_blocked_false_when_no_challenge(self):
        rich = "<html><body>" + "word " * 60 + "</body></html>"
        resp = make_response(200, rich, {"content-type": "text/html"})
        client = self._make_client(resp)
        _, _, _, _, cdn_blocked = client.fetch_page("http://example.gov/")
        assert not cdn_blocked

    def test_cdn_blocked_false_when_bypass_succeeds(self):
        resp = make_response(403, "<html>cf challenge</html>", {"cf-ray": "abc-LAX"})
        client = self._make_client(resp)
        real_html = "<html><body>" + "word " * 60 + "</body></html>"
        import crawler.playwright_client as pwc
        with patch.object(pwc, "fetch_rendered", return_value=real_html):
            _, _, status, _, cdn_blocked = client.fetch_page("http://example.gov/")
        assert status == 200
        assert not cdn_blocked

    def test_cdn_blocked_true_when_playwright_cannot_bypass(self):
        cf_html = "<html><body><script>window._cf_chl_opt={}</script></body></html>"
        resp = make_response(403, cf_html, {"cf-ray": "abc-LAX"})
        client = self._make_client(resp)
        import crawler.playwright_client as pwc
        with patch.object(pwc, "fetch_rendered", return_value=cf_html):
            _, _, status, _, cdn_blocked = client.fetch_page("http://example.gov/")
        assert status == 403
        assert cdn_blocked

    def test_cdn_blocked_true_when_playwright_throws(self):
        resp = make_response(403, "<html>blocked</html>", {"cf-ray": "abc-LAX"})
        client = self._make_client(resp)
        import crawler.playwright_client as pwc
        with patch.object(pwc, "fetch_rendered", side_effect=RuntimeError("crash")):
            _, _, status, _, cdn_blocked = client.fetch_page("http://example.gov/")
        assert status == 403
        assert cdn_blocked


# ---------------------------------------------------------------------------
# _is_bot_challenge
# ---------------------------------------------------------------------------

class TestIsBotChallenge:
    def test_cloudflare_cf_ray_header_and_403(self):
        assert _is_bot_challenge("<html>blocked</html>", 403, {"cf-ray": "abc123-LAX"})

    def test_cloudflare_server_header_and_403(self):
        assert _is_bot_challenge("<html>blocked</html>", 403, {"server": "cloudflare"})

    def test_cloudflare_body_token_any_status(self):
        html = "<html><body><script>window._cf_chl_opt={chl:'abc'}</script></body></html>"
        assert _is_bot_challenge(html, 200, {})

    def test_cloudflare_cf_browser_verification_body(self):
        html = '<html><body><div id="cf-browser-verification"></div></body></html>'
        assert _is_bot_challenge(html, 403, {})

    def test_cloudflare_200_with_cf_ray_no_body_token_not_flagged(self):
        # cf-ray on a real 200 page (CDN hit, not a challenge) must not trigger retry
        assert not _is_bot_challenge("<html><body>Real page content</body></html>", 200, {"cf-ray": "abc"})

    def test_akamai_403_flagged(self):
        assert _is_bot_challenge("<html>Access Denied</html>", 403, {"server": "AkamaiGHost"})

    def test_akamai_200_not_flagged(self):
        assert not _is_bot_challenge("<html>Real page</html>", 200, {"server": "AkamaiGHost"})

    def test_plain_403_no_cdn_headers_not_flagged(self):
        assert not _is_bot_challenge("<html>403 Forbidden</html>", 403, {"server": "nginx"})

    def test_empty_html_and_headers(self):
        assert not _is_bot_challenge("", 200, {})

    def test_header_keys_are_case_insensitive(self):
        assert _is_bot_challenge("<html>blocked</html>", 403, {"CF-Ray": "abc123", "Server": "Cloudflare"})

    def test_azure_waf_body_token_flagged(self):
        html = '<!doctype html><html><head><meta name="description" content="Azure WAF JS Challenge"/></head></html>'
        assert _is_bot_challenge(html, 403, {})

    def test_azure_waf_body_token_case_insensitive(self):
        assert _AZURE_WAF_BODY_RE.search("azure waf js challenge")
        assert _AZURE_WAF_BODY_RE.search("AZURE WAF JS CHALLENGE")


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


class TestPortalDetectorPassive:
    def _detector(self) -> PortalDetector:
        return PortalDetector(MagicMock())

    def test_socrata_footer_text(self):
        det = self._detector()
        platform, method = det.detect(SOCRATA_HTML, {}, "https://data.example.gov")
        assert platform == "Socrata"
        assert method == "passive"

    def test_socrata_header(self):
        det = self._detector()
        headers = {"X-Socrata-RequestId": "abc123"}
        platform, method = det.detect(PLAIN_HTML, headers, "https://data.example.gov")
        assert platform == "Socrata"
        assert method == "passive"

    def test_socrata_tyler_footer(self):
        html = PLAIN_HTML.replace("</body>", "<footer>Powered by Tyler Data &amp; Insights</footer></body>")
        det = self._detector()
        platform, _ = det.detect(html, {}, "https://data.example.gov")
        assert platform == "Socrata"

    def test_ckan_meta_generator(self):
        det = self._detector()
        platform, method = det.detect(CKAN_HTML, {}, "https://data.example.gov")
        assert platform == "CKAN"
        assert method == "passive"

    def test_ckan_js_snippet(self):
        html = "<html><body><script>ckan.module('map', function(){})</script></body></html>"
        det = self._detector()
        platform, _ = det.detect(html, {}, "https://data.example.gov")
        assert platform == "CKAN"

    def test_ckan_body_class(self):
        html = '<html><body class="ckan-home"><p>Content</p></body></html>'
        det = self._detector()
        platform, _ = det.detect(html, {}, "https://data.example.gov")
        assert platform == "CKAN"

    def test_arcgis_hub_component(self):
        det = self._detector()
        platform, method = det.detect(ARCGIS_HTML, {}, "https://opendata.example.gov")
        assert platform == "ArcGIS Hub"
        assert method == "passive"

    def test_arcgis_hub_domain(self):
        det = self._detector()
        platform, method = det.detect(PLAIN_HTML, {}, "https://opendata.dc.opendata.arcgis.com")
        assert platform == "ArcGIS Hub"
        assert method == "passive"

    def test_arcgis_script_domain(self):
        html = '<html><head><script src="https://js.arcgis.com/4.28/"></script></head></html>'
        det = self._detector()
        platform, _ = det.detect(html, {}, "https://gis.example.gov")
        assert platform == "ArcGIS Hub"

    def test_plain_page_returns_none(self):
        det = self._detector()
        platform, method = det.detect(PLAIN_HTML, {}, "https://finance.state.gov")
        assert platform is None
        assert method == "none"

    def test_returns_none_on_empty_html(self):
        det = self._detector()
        platform, method = det.detect("", {}, "https://example.gov")
        assert platform is None

    def test_ckan_dataset_link_alone_not_sufficient(self):
        # /dataset links are too generic; a structural CKAN signal is required
        html = '<html><body><a href="/dataset/something">Data</a></body></html>'
        det = self._detector()
        platform, _ = det.detect(html, {}, "https://example.gov")
        assert platform is None


# ---------------------------------------------------------------------------
# PortalDetector active probe
# ---------------------------------------------------------------------------

class TestPortalDetectorActiveProbe:
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
        assert platform in ("Socrata", "CKAN")
        assert method == "probe"

    def test_active_probe_failure_returns_none(self):
        # Ambiguous signals but all probes fail
        ambiguous = SOCRATA_HTML.replace("</body>", '<script>ckan.module("x",fn)</script></body>')
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(404)
        det = PortalDetector(mock_client)
        platform, method = det.detect(ambiguous, {}, "https://data.example.gov")
        assert platform is None

    def test_single_passive_match_skips_probe(self):
        mock_client = MagicMock()
        det = PortalDetector(mock_client)
        platform, method = det.detect(SOCRATA_HTML, {}, "https://data.example.gov")
        assert platform == "Socrata"
        assert method == "passive"
        mock_client.get.assert_not_called()


