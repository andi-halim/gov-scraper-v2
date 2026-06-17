"""Unit tests for Phase 5 (T-50–T-52): BFS depth crawler."""
from unittest.mock import MagicMock, call

import pytest

from crawler.orchestrator import _extract_links, _registered_domain, crawl_url


# ---------------------------------------------------------------------------
# _registered_domain helper
# ---------------------------------------------------------------------------

class TestRegisteredDomain:
    def test_simple_domain(self):
        assert _registered_domain("https://data.michigan.gov/path") == "michigan.gov"

    def test_subdomain_stripped(self):
        assert _registered_domain("https://treasurer.state.tx.us/page") == "state.tx.us"

    def test_no_tld(self):
        # Falls back gracefully to domain part
        result = _registered_domain("http://localhost/page")
        assert result  # non-empty

    def test_same_for_different_subdomains(self):
        d1 = _registered_domain("https://data.example.gov/")
        d2 = _registered_domain("https://api.example.gov/")
        assert d1 == d2


# ---------------------------------------------------------------------------
# _extract_links helper
# ---------------------------------------------------------------------------

class TestExtractLinks:
    def test_absolute_href(self):
        html = '<a href="https://example.gov/page">Link</a>'
        links = _extract_links(html, "https://example.gov/")
        assert "https://example.gov/page" in links

    def test_relative_href_resolved(self):
        html = '<a href="/about">About</a>'
        links = _extract_links(html, "https://example.gov/home")
        assert "https://example.gov/about" in links

    def test_fragment_stripped(self):
        html = '<a href="/page#section">Sec</a>'
        links = _extract_links(html, "https://example.gov/")
        assert "https://example.gov/page" in links
        assert all("#" not in l for l in links)

    def test_javascript_skipped(self):
        html = '<a href="javascript:void(0)">Click</a>'
        assert _extract_links(html, "https://example.gov/") == []

    def test_mailto_skipped(self):
        html = '<a href="mailto:info@example.gov">Email</a>'
        assert _extract_links(html, "https://example.gov/") == []

    def test_anchor_only_skipped(self):
        html = '<a href="#">Top</a>'
        assert _extract_links(html, "https://example.gov/") == []

    def test_multiple_links(self):
        html = '<a href="/a">A</a><a href="/b">B</a><a href="/c">C</a>'
        links = _extract_links(html, "https://example.gov/")
        assert len(links) == 3

    def test_empty_html(self):
        assert _extract_links("", "https://example.gov/") == []

    def test_empty_href_skipped(self):
        html = '<a href="">Empty</a>'
        assert _extract_links(html, "https://example.gov/") == []

    def test_tel_skipped(self):
        html = '<a href="tel:+15555555555">Call</a>'
        assert _extract_links(html, "https://example.gov/") == []


# ---------------------------------------------------------------------------
# Helper: build a mock HttpClient
# ---------------------------------------------------------------------------

def _make_client(pages: dict[str, tuple[str, str, int, bool]]) -> MagicMock:
    """pages maps url → (html, final_url, http_status, js_rendered).

    fetch_page returns a 5-tuple with cdn_blocked=False appended automatically.
    """
    client = MagicMock()

    def fetch_side_effect(url):
        if url in pages:
            return (*pages[url], False)
        return ("", url, 404, False, False)

    client.fetch_page.side_effect = fetch_side_effect
    return client


# ---------------------------------------------------------------------------
# T-50: crawl_url — basic behaviour
# ---------------------------------------------------------------------------

class TestCrawlUrlBasic:
    def test_single_page_no_links(self):
        seed = "https://example.gov/"
        client = _make_client({seed: ("<html><body>No links</body></html>", seed, 200, False)})
        pages, _, _ = crawl_url(seed, client, depth=2)
        assert len(pages) == 1
        assert pages[0][0] == seed
        assert pages[0][2] == 200

    def test_depth_reached_zero_for_seed_only(self):
        seed = "https://example.gov/"
        client = _make_client({seed: ("<html><body>content</body></html>", seed, 200, False)})
        _, depth, _ = crawl_url(seed, client, depth=2)
        assert depth == 0

    def test_seed_non_200_stops_crawl(self):
        seed = "https://example.gov/"
        client = _make_client({seed: ("", seed, 404, False)})
        pages, depth, _ = crawl_url(seed, client, depth=2)
        assert len(pages) == 1
        assert pages[0][2] == 404
        assert depth == 0

    def test_network_error_recorded_as_status_zero(self):
        client = MagicMock()
        client.fetch_page.side_effect = ConnectionError("timeout")
        seed = "https://example.gov/"
        pages, depth, _ = crawl_url(seed, client, depth=2)
        assert len(pages) == 1
        assert pages[0][0] == seed
        assert pages[0][2] == 0
        assert pages[0][1] == ""
        assert depth == 0

    def test_returns_html_and_js_rendered_flag(self):
        seed = "https://example.gov/"
        html = "<html><body>rendered</body></html>"
        client = _make_client({seed: (html, seed, 200, True)})
        pages, _, _ = crawl_url(seed, client, depth=2)
        assert pages[0][1] == html
        assert pages[0][3] is True


# ---------------------------------------------------------------------------
# T-50: BFS link following
# ---------------------------------------------------------------------------

class TestCrawlUrlBFS:
    def _seed_html(self, links: list[str]) -> str:
        hrefs = "".join(f'<a href="{u}">link</a>' for u in links)
        return f"<html><body>{hrefs}</body></html>"

    def test_follows_internal_link_at_depth_1(self):
        seed = "https://example.gov/"
        child = "https://example.gov/about"
        pages_map = {
            seed: (self._seed_html([child]), seed, 200, False),
            child: ("<html><body>About</body></html>", child, 200, False),
        }
        client = _make_client(pages_map)
        pages, depth, _ = crawl_url(seed, client, depth=2)
        urls = [p[0] for p in pages]
        assert seed in urls
        assert child in urls
        assert depth == 1

    def test_follows_two_hops(self):
        seed = "https://example.gov/"
        child = "https://example.gov/page1"
        grandchild = "https://example.gov/page2"
        pages_map = {
            seed: (self._seed_html([child]), seed, 200, False),
            child: (self._seed_html([grandchild]), child, 200, False),
            grandchild: ("<html><body>deep</body></html>", grandchild, 200, False),
        }
        client = _make_client(pages_map)
        pages, depth, _ = crawl_url(seed, client, depth=2)
        urls = [p[0] for p in pages]
        assert grandchild in urls
        assert depth == 2

    def test_depth_0_fetches_only_seed(self):
        seed = "https://example.gov/"
        child = "https://example.gov/about"
        pages_map = {
            seed: (self._seed_html([child]), seed, 200, False),
            child: ("<html><body>About</body></html>", child, 200, False),
        }
        client = _make_client(pages_map)
        pages, _, _ = crawl_url(seed, client, depth=0)
        assert len(pages) == 1
        assert pages[0][0] == seed

    def test_depth_1_does_not_follow_second_hop(self):
        seed = "https://example.gov/"
        child = "https://example.gov/page1"
        grandchild = "https://example.gov/page2"
        pages_map = {
            seed: (self._seed_html([child]), seed, 200, False),
            child: (self._seed_html([grandchild]), child, 200, False),
            grandchild: ("<html><body>deep</body></html>", grandchild, 200, False),
        }
        client = _make_client(pages_map)
        pages, depth, _ = crawl_url(seed, client, depth=1)
        urls = [p[0] for p in pages]
        assert seed in urls
        assert child in urls
        assert grandchild not in urls
        assert depth == 1


# ---------------------------------------------------------------------------
# T-51: external link filtering
# ---------------------------------------------------------------------------

class TestCrawlUrlExternalFilter:
    def test_external_link_not_followed(self):
        seed = "https://michigan.gov/"
        external = "https://census.gov/data"
        html = f'<html><body><a href="{external}">Census</a></body></html>'
        client = _make_client({seed: (html, seed, 200, False)})
        pages, _, _ = crawl_url(seed, client, depth=2)
        urls = [p[0] for p in pages]
        assert external not in urls
        assert len(pages) == 1

    def test_subdomain_treated_as_same_domain(self):
        seed = "https://michigan.gov/"
        sub = "https://data.michigan.gov/datasets"
        html = f'<html><body><a href="{sub}">Data</a></body></html>'
        pages_map = {
            seed: (html, seed, 200, False),
            sub: ("<html><body>datasets</body></html>", sub, 200, False),
        }
        client = _make_client(pages_map)
        pages, _, _ = crawl_url(seed, client, depth=2)
        urls = [p[0] for p in pages]
        assert sub in urls

    def test_different_tld_is_external(self):
        seed = "https://example.gov/"
        external = "https://example.com/page"
        html = f'<html><body><a href="{external}">External</a></body></html>'
        client = _make_client({seed: (html, seed, 200, False)})
        pages, _, _ = crawl_url(seed, client, depth=2)
        assert all(p[0] != external for p in pages)


# ---------------------------------------------------------------------------
# No duplicate visits
# ---------------------------------------------------------------------------

class TestCrawlUrlDeduplication:
    def test_same_url_not_fetched_twice(self):
        seed = "https://example.gov/"
        child = "https://example.gov/about"
        # Seed links to child twice
        html = (
            f'<a href="{child}">A</a>'
            f'<a href="{child}">B</a>'
        )
        pages_map = {
            seed: (f"<html><body>{html}</body></html>", seed, 200, False),
            child: ("<html><body>About</body></html>", child, 200, False),
        }
        client = _make_client(pages_map)
        pages, _, _ = crawl_url(seed, client, depth=2)
        fetched_urls = [p[0] for p in pages]
        assert fetched_urls.count(child) == 1

    def test_circular_link_not_loops(self):
        seed = "https://example.gov/"
        child = "https://example.gov/page"
        pages_map = {
            seed: (f'<a href="{child}">P</a>', seed, 200, False),
            # child links back to seed
            child: (f'<a href="{seed}">Home</a>', child, 200, False),
        }
        client = _make_client(pages_map)
        pages, _, _ = crawl_url(seed, client, depth=2)
        # seed should appear exactly once
        assert [p[0] for p in pages].count(seed) == 1


# ---------------------------------------------------------------------------
# T-52: crawl_depth_reached
# ---------------------------------------------------------------------------

class TestCrawlDepthReached:
    def _seed_html(self, links: list[str]) -> str:
        hrefs = "".join(f'<a href="{u}">l</a>' for u in links)
        return f"<html><body>{hrefs}</body></html>"

    def test_depth_1_when_child_200(self):
        seed = "https://example.gov/"
        child = "https://example.gov/c"
        client = _make_client({
            seed: (self._seed_html([child]), seed, 200, False),
            child: ("<html><body>ok</body></html>", child, 200, False),
        })
        _, depth, _ = crawl_url(seed, client, depth=2)
        assert depth == 1

    def test_depth_2_when_grandchild_200(self):
        seed = "https://example.gov/"
        child = "https://example.gov/c"
        grandchild = "https://example.gov/g"
        client = _make_client({
            seed: (self._seed_html([child]), seed, 200, False),
            child: (self._seed_html([grandchild]), child, 200, False),
            grandchild: ("<html><body>ok</body></html>", grandchild, 200, False),
        })
        _, depth, _ = crawl_url(seed, client, depth=2)
        assert depth == 2

    def test_child_404_does_not_advance_depth(self):
        seed = "https://example.gov/"
        child = "https://example.gov/missing"
        client = _make_client({
            seed: (self._seed_html([child]), seed, 200, False),
            child: ("", child, 404, False),
        })
        _, depth, _ = crawl_url(seed, client, depth=2)
        assert depth == 0

    def test_depth_stays_0_when_seed_fails(self):
        seed = "https://example.gov/"
        client = _make_client({seed: ("", seed, 500, False)})
        _, depth, _ = crawl_url(seed, client, depth=2)
        assert depth == 0

    def test_depth_stays_0_when_seed_network_error(self):
        client = MagicMock()
        client.fetch_page.side_effect = OSError("connection refused")
        _, depth, _ = crawl_url("https://example.gov/", client, depth=2)
        assert depth == 0


# ---------------------------------------------------------------------------
# Issue 1 fix: redirect deduplication
# ---------------------------------------------------------------------------

class TestRedirectDeduplication:
    def test_redirect_target_not_fetched_again(self):
        # Seed http://example.gov/ redirects to https://example.gov/
        seed = "http://example.gov/"
        final = "https://example.gov/"
        child = "https://example.gov/about"
        # final links to child; child also links back to final
        final_html = f'<html><body><a href="{child}">About</a></body></html>'
        child_html = f'<html><body><a href="{final}">Home</a></body></html>'

        client = MagicMock()
        def fetch_side_effect(url):
            if url == seed:
                return (final_html, final, 200, False, False)  # redirect
            if url == child:
                return (child_html, child, 200, False, False)
            return ("", url, 404, False, False)
        client.fetch_page.side_effect = fetch_side_effect

        pages, _, _ = crawl_url(seed, client, depth=2)
        fetched = [p[0] for p in pages]
        # seed fetched once, child fetched once — final (redirect target) not re-fetched
        assert fetched.count(seed) == 1
        assert fetched.count(child) == 1
        assert fetched.count(final) == 0

    def test_redirect_within_same_domain_does_not_block_other_links(self):
        # Ensure adding final_url to visited doesn't accidentally block
        # an unrelated URL on the same domain from being crawled
        seed = "http://example.gov/"
        final = "https://example.gov/"       # redirect target
        other = "https://example.gov/data"   # different path — should still be crawled

        final_html = f'<html><body><a href="{other}">Data</a></body></html>'

        client = MagicMock()
        def fetch_side_effect(url):
            if url == seed:
                return (final_html, final, 200, False, False)
            if url == other:
                return ("<html><body>datasets</body></html>", other, 200, False, False)
            return ("", url, 404, False, False)
        client.fetch_page.side_effect = fetch_side_effect

        pages, _, _ = crawl_url(seed, client, depth=2)
        fetched = [p[0] for p in pages]
        assert other in fetched


# ---------------------------------------------------------------------------
# prefetched_seed parameter
# ---------------------------------------------------------------------------

class TestCrawlUrlPrefetchedSeed:
    def _seed_html(self, links: list[str]) -> str:
        hrefs = "".join(f'<a href="{u}">link</a>' for u in links)
        return f"<html><body>{hrefs}</body></html>"

    def test_seed_not_re_fetched_when_prefetched(self):
        seed = "https://example.gov/"
        client = _make_client({seed: ("<html>should not be fetched</html>", seed, 200, False)})
        html = "<html><body>prefetched content</body></html>"
        crawl_url(seed, client, depth=1, prefetched_seed=(html, seed, 200, False))
        client.fetch_page.assert_not_called()

    def test_prefetched_html_appears_in_pages(self):
        seed = "https://example.gov/"
        client = _make_client({})
        html = "<html><body>prefetched content</body></html>"
        pages, _, _ = crawl_url(seed, client, depth=1, prefetched_seed=(html, seed, 200, False))
        assert pages[0][1] == html
        assert pages[0][2] == 200

    def test_prefetched_seed_links_followed_at_hop1(self):
        seed = "https://example.gov/"
        child = "https://example.gov/child"
        seed_html = self._seed_html([child])
        client = _make_client({child: ("<html><body>child</body></html>", child, 200, False)})
        pages, depth, _ = crawl_url(seed, client, depth=1, prefetched_seed=(seed_html, seed, 200, False))
        urls = [p[0] for p in pages]
        assert seed in urls
        assert child in urls
        assert depth == 1

    def test_prefetched_seed_non_200_no_children_crawled(self):
        seed = "https://example.gov/"
        child = "https://example.gov/child"
        client = _make_client({child: ("<html>child</html>", child, 200, False)})
        pages, depth, _ = crawl_url(seed, client, depth=1,
                                  prefetched_seed=(self._seed_html([child]), seed, 403, False))
        assert len(pages) == 1
        assert depth == 0
        client.fetch_page.assert_not_called()

    def test_prefetched_seed_depth_zero_no_children(self):
        seed = "https://example.gov/"
        child = "https://example.gov/child"
        client = _make_client({child: ("<html>child</html>", child, 200, False)})
        pages, _, _ = crawl_url(seed, client, depth=0,
                              prefetched_seed=(self._seed_html([child]), seed, 200, False))
        assert len(pages) == 1
        client.fetch_page.assert_not_called()

    def test_prefetched_redirect_final_url_tracked(self):
        seed = "http://example.gov/"
        final = "https://example.gov/"
        child = "https://example.gov/about"
        html = self._seed_html([child])
        client = _make_client({child: ("<html><body>about</body></html>", child, 200, False)})
        pages, _, _ = crawl_url(seed, client, depth=1, prefetched_seed=(html, final, 200, False))
        assert pages[0][0] == seed
        fetched_urls = [p[0] for p in pages]
        assert child in fetched_urls
        # final (redirect target) must not appear as a separate fetched page
        assert fetched_urls.count(final) == 0

    def test_none_prefetched_seed_behaves_as_before(self):
        seed = "https://example.gov/"
        client = _make_client({seed: ("<html><body>fetched</body></html>", seed, 200, False)})
        pages, _, _ = crawl_url(seed, client, depth=1, prefetched_seed=None)
        client.fetch_page.assert_called()
        assert pages[0][2] == 200


# ---------------------------------------------------------------------------
# Issue 3 fix: _extract_links logs on parse failure
# ---------------------------------------------------------------------------

class TestExtractLinksLogging:
    def test_parse_failure_logs_warning(self, caplog):
        import logging
        # Force BeautifulSoup to raise by monkeypatching
        from unittest.mock import patch
        with patch("crawler.orchestrator.BeautifulSoup", side_effect=RuntimeError("bad html")):
            with caplog.at_level(logging.WARNING, logger="crawler.orchestrator"):
                result = _extract_links("<html>bad</html>", "https://example.gov/")
        assert result == []
        assert any("Failed to parse HTML" in r.message for r in caplog.records)
