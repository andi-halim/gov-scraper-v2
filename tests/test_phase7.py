"""Unit tests for Phase 7 (T-70–T-72): Keyword Loader and Relevance Scorer."""
import csv
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

import scorer.keyword_loader as kl_module
from scorer.keyword_loader import get_effective_keywords
from scorer.scorer import _extract_pools, _normalize, score_page


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _page(url: str, html: str, status: int = 200) -> tuple:
    return (url, html, status, False)


def _kw(*words: str) -> frozenset:
    return frozenset(words)


# ---------------------------------------------------------------------------
# keyword_loader — get_effective_keywords (T-70)
# ---------------------------------------------------------------------------

class TestGetEffectiveKeywords:
    def _patch(self, base: frozenset, state_defs: dict):
        """Patch both lru_cache functions to return controlled data."""
        return (
            patch.object(kl_module, "_base_keywords", return_value=base),
            patch.object(kl_module, "_state_defs", return_value=state_defs),
        )

    def test_national_returns_base_only(self):
        base = frozenset(["county", "municipal"])
        with patch.object(kl_module, "_base_keywords", return_value=base), \
             patch.object(kl_module, "_state_defs", return_value={}):
            result = get_effective_keywords("NATIONAL")
        assert result == base

    def test_state_returns_base_union_census_terms(self):
        base = frozenset(["county", "municipal"])
        defs = {"LA": {"census_terms": ["parish", "police jury"]}}
        with patch.object(kl_module, "_base_keywords", return_value=base), \
             patch.object(kl_module, "_state_defs", return_value=defs):
            result = get_effective_keywords("LA")
        assert result == frozenset(["county", "municipal", "parish", "police jury"])

    def test_state_missing_from_defs_returns_base_only(self):
        base = frozenset(["county"])
        with patch.object(kl_module, "_base_keywords", return_value=base), \
             patch.object(kl_module, "_state_defs", return_value={}):
            result = get_effective_keywords("ZZ")
        assert result == base

    def test_returns_frozenset(self):
        base = frozenset(["county"])
        with patch.object(kl_module, "_base_keywords", return_value=base), \
             patch.object(kl_module, "_state_defs", return_value={}):
            result = get_effective_keywords("NATIONAL")
        assert isinstance(result, frozenset)

    def test_empty_census_terms_returns_base_only(self):
        base = frozenset(["county"])
        defs = {"TX": {"census_terms": []}}
        with patch.object(kl_module, "_base_keywords", return_value=base), \
             patch.object(kl_module, "_state_defs", return_value=defs):
            result = get_effective_keywords("TX")
        assert result == base

    def test_blank_census_terms_stripped(self):
        base = frozenset(["county"])
        defs = {"TX": {"census_terms": ["  ", "municipality", ""]}}
        with patch.object(kl_module, "_base_keywords", return_value=base), \
             patch.object(kl_module, "_state_defs", return_value=defs):
            result = get_effective_keywords("TX")
        assert "municipality" in result
        assert "" not in result
        assert "  " not in result

    def test_base_keywords_loaded_from_csv(self, tmp_path):
        csv_file = tmp_path / "keywords.csv"
        csv_file.write_text("county\nmunicipal\n\n", encoding="utf-8")
        assert kl_module._load_keywords(csv_file) == frozenset(["county", "municipal"])

    def test_state_defs_loaded_from_json(self, tmp_path):
        json_file = tmp_path / "state_definitions.json"
        json_file.write_text(
            json.dumps({"LA": {"census_terms": ["parish"], "notes": ""}}),
            encoding="utf-8",
        )
        assert kl_module._load_state_defs(json_file)["LA"]["census_terms"] == ["parish"]

    def test_missing_state_defs_file_returns_empty_dict(self, tmp_path):
        assert kl_module._load_state_defs(tmp_path / "nonexistent.json") == {}


# ---------------------------------------------------------------------------
# scorer — _normalize
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_lowercases(self):
        assert _normalize("County") == "county"

    def test_strips_diacritics(self):
        assert _normalize("café") == "cafe"
        assert _normalize("Ångström") == "angstrom"

    def test_nfc_normalization(self):
        # NFC: composed form; both should normalize to the same string
        composed = "é"      # é as single codepoint
        decomposed = "é"   # e + combining acute
        assert _normalize(composed) == _normalize(decomposed)

    def test_empty_string(self):
        assert _normalize("") == ""


# ---------------------------------------------------------------------------
# scorer — _extract_pools
# ---------------------------------------------------------------------------

class TestExtractPools:
    def test_title_in_heading_pool(self):
        html = "<html><head><title>County Budget</title></head><body></body></html>"
        heading, _, _ = _extract_pools(html)
        assert "county budget" in heading.lower()

    def test_h1_h2_h3_in_heading_pool(self):
        html = "<body><h1>County</h1><h2>Municipal</h2><h3>Township</h3></body>"
        heading, _, _ = _extract_pools(html)
        assert "county" in heading.lower()
        assert "municipal" in heading.lower()
        assert "township" in heading.lower()

    def test_h4_not_in_heading_pool(self):
        html = "<body><h4>Special District</h4></body>"
        heading, _, _ = _extract_pools(html)
        assert "special district" not in heading.lower()

    def test_anchor_text_in_anchor_pool(self):
        html = '<body><a href="/data">County Records</a></body>'
        _, _, anchor = _extract_pools(html)
        assert "county records" in anchor.lower()

    def test_href_excluded_from_anchor_pool(self):
        html = '<body><a href="https://county.gov/data">Click</a></body>'
        _, _, anchor = _extract_pools(html)
        assert "county.gov" not in anchor

    def test_body_text_extracted(self):
        html = "<body><p>Municipal budget report</p></body>"
        _, body, _ = _extract_pools(html)
        assert "municipal budget report" in body.lower()

    def test_script_content_excluded(self):
        html = "<body><script>var county = 'data';</script><p>Hello</p></body>"
        _, body, _ = _extract_pools(html)
        assert "var county" not in body

    def test_style_content_excluded(self):
        html = "<body><style>.county { color: red; }</style><p>Text</p></body>"
        _, body, _ = _extract_pools(html)
        assert ".county" not in body

    def test_url_stripped_from_heading_pool(self):
        html = "<html><head><title>Visit https://example.gov/county today</title></head></html>"
        heading, _, _ = _extract_pools(html)
        assert "https://example.gov/county" not in heading

    def test_url_stripped_from_body_pool(self):
        html = "<body><p>Download from https://data.gov/county.csv for data</p></body>"
        _, body, _ = _extract_pools(html)
        assert "https://data.gov/county.csv" not in body

    def test_www_url_stripped(self):
        html = "<body><p>Visit www.example.gov for more info</p></body>"
        _, body, _ = _extract_pools(html)
        assert "www.example.gov" not in body

    def test_malformed_html_returns_empty_strings(self):
        h, b, a = _extract_pools("")
        assert h == "" and b == "" and a == ""


# ---------------------------------------------------------------------------
# scorer — score_page, weighting and scoring (T-71, T-72, T-113)
# ---------------------------------------------------------------------------

class TestScorePageWeighting:
    def test_heading_scores_higher_than_body_only(self):
        """T-113: keyword in <h1> should outscore same keyword in body only."""
        kws = _kw("county")
        heading_page = _page("http://x.gov/", "<h1>county government</h1>")
        body_page = _page("http://x.gov/", "<body><p>county government</p></body>")

        heading_result = score_page([heading_page], kws)
        body_result = score_page([body_page], kws)

        assert heading_result["relevance_score"] > body_result["relevance_score"]

    def test_anchor_text_also_counted_in_body_pool(self):
        # Anchor text is visible body text, so a keyword in an anchor matches
        # both body (0.35) and anchor (0.15) pools → higher score than a keyword
        # in a plain <p> which only matches body (0.35).
        kws = _kw("county")
        paragraph_page = _page("http://x.gov/", "<body><p>county records</p></body>")
        anchor_page = _page("http://x.gov/", '<body><a href="#">county records</a></body>')

        para_result = score_page([paragraph_page], kws)
        anchor_result = score_page([anchor_page], kws)

        assert anchor_result["relevance_score"] > para_result["relevance_score"]

    def test_all_three_tiers_additive(self):
        kws = _kw("county")
        all_tiers = _page(
            "http://x.gov/",
            '<html><head><title>county</title></head>'
            '<body><p>county</p><a href="#">county</a></body></html>',
        )
        heading_only = _page("http://x.gov/", "<h1>county</h1>")

        all_result = score_page([all_tiers], kws)
        heading_result = score_page([heading_only], kws)

        assert all_result["relevance_score"] > heading_result["relevance_score"]

    def test_max_one_point_per_keyword(self):
        # keyword in all three tiers: max 0.50+0.35+0.15 = 1.00 pts
        # with 1 keyword, normalization_factor=1, score = min(100, round(1.00/1*100)) = 100
        kws = _kw("county")
        html = (
            "<html><head><title>county</title></head>"
            "<body><h1>county</h1><p>county</p><a href='#'>county</a></body></html>"
        )
        result = score_page([_page("http://x.gov/", html)], kws)
        assert result["relevance_score"] == 100

    def test_score_capped_at_100(self):
        kws = _kw("a", "b")
        html = "<h1>a b</h1><body><p>a b</p><a href='#'>a b</a></body>"
        result = score_page([_page("http://x.gov/", html)], kws)
        assert result["relevance_score"] <= 100

    def test_normalization_scales_by_keyword_count(self):
        # keyword in <h1> only: heading pool (0.50 pts), body pool is empty after
        # decompose. 0.50/4 * 100 = 12.5 → rounds to 13
        kws = _kw("county", "municipal", "township", "borough")
        html = "<h1>county</h1>"
        result = score_page([_page("http://x.gov/", html)], kws)
        assert 10 <= result["relevance_score"] <= 15

    def test_cross_page_tier_accumulation(self):
        # Heading match on page 1 and body match on page 2 are additive:
        # 0.50 (heading) + 0.35 (body) = 0.85/1 * 100 = 85
        kws = _kw("county")
        p1 = _page("http://x.gov/", "<h1>county</h1>")
        p2 = _page("http://x.gov/p2", "<body><p>county records</p></body>")

        result_both = score_page([p1, p2], kws)
        result_heading_only = score_page([p1], kws)
        result_body_only = score_page([p2], kws)

        assert result_heading_only["relevance_score"] == 50
        assert result_body_only["relevance_score"] == 35
        assert result_both["relevance_score"] == 85


class TestScorePageBasic:
    def test_empty_keywords_returns_zero(self):
        page = _page("http://x.gov/", "<h1>county</h1>")
        result = score_page([page], frozenset())
        assert result == {"relevance_score": 0, "matched_keywords": []}

    def test_empty_pages_list_returns_zero(self):
        result = score_page([], _kw("county"))
        assert result["relevance_score"] == 0
        assert result["matched_keywords"] == []

    def test_no_match_returns_zero(self):
        page = _page("http://x.gov/", "<h1>weather forecast</h1>")
        result = score_page([page], _kw("county", "municipal"))
        assert result["relevance_score"] == 0
        assert result["matched_keywords"] == []

    def test_matched_keywords_sorted(self):
        kws = _kw("township", "county", "municipal")
        html = "<body><p>county municipal township records</p></body>"
        result = score_page([_page("http://x.gov/", html)], kws)
        assert result["matched_keywords"] == sorted(result["matched_keywords"])

    def test_matched_keywords_contains_only_hits(self):
        kws = _kw("county", "municipal", "township")
        html = "<body><p>county records only</p></body>"
        result = score_page([_page("http://x.gov/", html)], kws)
        assert "county" in result["matched_keywords"]
        assert "municipal" not in result["matched_keywords"]
        assert "township" not in result["matched_keywords"]

    def test_non_200_page_skipped(self):
        good = _page("http://x.gov/", "<h1>county</h1>", 200)
        bad = _page("http://x.gov/p2", "<h1>county</h1>", 404)
        result_good = score_page([good], _kw("county"))
        result_mixed = score_page([bad], _kw("county"))
        assert result_mixed["relevance_score"] == 0
        assert result_good["relevance_score"] > 0

    def test_zero_status_page_skipped(self):
        page = ("http://x.gov/", "<h1>county</h1>", 0, False)
        result = score_page([page], _kw("county"))
        assert result["relevance_score"] == 0

    def test_empty_html_page_skipped(self):
        page = ("http://x.gov/", "", 200, False)
        result = score_page([page], _kw("county"))
        assert result["relevance_score"] == 0

    def test_multiple_pages_aggregated(self):
        p1 = _page("http://x.gov/", "<h1>county</h1>")
        p2 = _page("http://x.gov/p2", "<h1>municipal</h1>")
        kws = _kw("county", "municipal")
        result = score_page([p1, p2], kws)
        assert "county" in result["matched_keywords"]
        assert "municipal" in result["matched_keywords"]


class TestScorePageUrlExclusion:
    """T-72: URL and domain text must not contribute to the score."""

    def test_url_in_href_not_scored(self):
        # href="https://county.gov" — "county" only appears in the URL, not text
        html = '<body><a href="https://county.gov/data">Click here</a></body>'
        result = score_page([_page("http://x.gov/", html)], _kw("county"))
        assert result["relevance_score"] == 0

    def test_inline_url_in_body_not_scored(self):
        # "county" appears only inside a URL string in body text
        html = "<body><p>Download from https://data.gov/county-data.csv</p></body>"
        result = score_page([_page("http://x.gov/", html)], _kw("county"))
        assert result["relevance_score"] == 0

    def test_inline_url_in_heading_not_scored(self):
        html = "<title>Visit https://county.example.gov today</title>"
        result = score_page([_page("http://x.gov/", html)], _kw("county"))
        assert result["relevance_score"] == 0


class TestScorePageNormalization:
    def test_case_insensitive_match(self):
        kws = _kw("County")
        html = "<h1>COUNTY RECORDS</h1>"
        result = score_page([_page("http://x.gov/", html)], kws)
        assert "County" in result["matched_keywords"]

    def test_diacritic_stripping(self):
        kws = _kw("cafe")
        html = "<h1>café district</h1>"
        result = score_page([_page("http://x.gov/", html)], kws)
        assert result["relevance_score"] > 0

    def test_whole_word_boundary_no_partial_match(self):
        kws = _kw("county")
        html = "<body><p>multicounty region</p></body>"
        result = score_page([_page("http://x.gov/", html)], kws)
        assert result["relevance_score"] == 0

    def test_whole_word_boundary_matches_standalone(self):
        kws = _kw("county")
        html = "<body><p>county records available here</p></body>"
        result = score_page([_page("http://x.gov/", html)], kws)
        assert result["relevance_score"] > 0

    def test_multi_word_keyword_matched(self):
        kws = _kw("special district")
        html = "<body><p>This covers the special district boundary</p></body>"
        result = score_page([_page("http://x.gov/", html)], kws)
        assert "special district" in result["matched_keywords"]
