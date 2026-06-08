"""Unit tests for Phase 8 (T-80–T-81): input ingestion and priority queue."""
import csv
import logging

import pytest

from crawler.orchestrator import load_urls


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_csv(path, rows, fieldnames=None):
    if fieldnames is None:
        fieldnames = ["RESOURCE_NAME", "WEB_ADDRESS", "PRIORITY_RESOURCE"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# T-80: load_urls — basic ingestion
# ---------------------------------------------------------------------------

class TestLoadUrlsBasic:
    def test_reads_urls(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "Site A", "WEB_ADDRESS": "https://example.gov/", "PRIORITY_RESOURCE": ""},
        ])
        result = load_urls(str(f))
        assert len(result) == 1
        assert result[0]["url"] == "https://example.gov/"

    def test_only_web_address_and_priority_used(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "Should be ignored", "WEB_ADDRESS": "https://example.gov/", "PRIORITY_RESOURCE": ""},
        ])
        result = load_urls(str(f))
        assert "RESOURCE_NAME" not in result[0]
        assert set(result[0].keys()) == {"url", "priority", "state"}

    def test_returns_empty_for_header_only_csv(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [])
        assert load_urls(str(f)) == []

    def test_multiple_urls_returned_in_order(self, tmp_path):
        f = tmp_path / "urls.csv"
        urls = [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://alpha.gov/", "PRIORITY_RESOURCE": ""},
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://beta.gov/", "PRIORITY_RESOURCE": ""},
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://gamma.gov/", "PRIORITY_RESOURCE": ""},
        ]
        _write_csv(f, urls)
        result = load_urls(str(f))
        assert [r["url"] for r in result] == [
            "https://alpha.gov/",
            "https://beta.gov/",
            "https://gamma.gov/",
        ]


# ---------------------------------------------------------------------------
# T-80: blank URL skipping
# ---------------------------------------------------------------------------

class TestLoadUrlsBlankSkip:
    def test_blank_web_address_skipped(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "Empty", "WEB_ADDRESS": "", "PRIORITY_RESOURCE": ""},
            {"RESOURCE_NAME": "Good", "WEB_ADDRESS": "https://example.gov/", "PRIORITY_RESOURCE": ""},
        ])
        result = load_urls(str(f))
        assert len(result) == 1
        assert result[0]["url"] == "https://example.gov/"

    def test_whitespace_only_web_address_skipped(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "Spaces", "WEB_ADDRESS": "   ", "PRIORITY_RESOURCE": ""},
        ])
        assert load_urls(str(f)) == []

    def test_blank_skip_logged(self, tmp_path, caplog):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "Empty", "WEB_ADDRESS": "", "PRIORITY_RESOURCE": ""},
        ])
        with caplog.at_level(logging.WARNING, logger="crawler.orchestrator"):
            load_urls(str(f))
        assert any("blank" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# T-80: malformed URL skipping
# ---------------------------------------------------------------------------

class TestLoadUrlsMalformedSkip:
    def test_missing_scheme_skipped(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "example.gov/page", "PRIORITY_RESOURCE": ""},
        ])
        assert load_urls(str(f)) == []

    def test_missing_netloc_skipped(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://", "PRIORITY_RESOURCE": ""},
        ])
        assert load_urls(str(f)) == []

    def test_plain_text_skipped(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "not a url at all", "PRIORITY_RESOURCE": ""},
        ])
        assert load_urls(str(f)) == []

    def test_malformed_skip_logged(self, tmp_path, caplog):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "not-a-url", "PRIORITY_RESOURCE": ""},
        ])
        with caplog.at_level(logging.WARNING, logger="crawler.orchestrator"):
            load_urls(str(f))
        assert any("malformed" in r.message.lower() for r in caplog.records)

    def test_valid_urls_not_affected_by_bad_rows(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "bad-url", "PRIORITY_RESOURCE": ""},
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://good.gov/", "PRIORITY_RESOURCE": ""},
        ])
        result = load_urls(str(f))
        assert len(result) == 1
        assert result[0]["url"] == "https://good.gov/"


# ---------------------------------------------------------------------------
# T-80: deduplication
# ---------------------------------------------------------------------------

class TestLoadUrlsDeduplication:
    def test_exact_duplicate_removed(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "A", "WEB_ADDRESS": "https://example.gov/page", "PRIORITY_RESOURCE": ""},
            {"RESOURCE_NAME": "B", "WEB_ADDRESS": "https://example.gov/page", "PRIORITY_RESOURCE": ""},
        ])
        result = load_urls(str(f))
        assert len(result) == 1

    def test_first_occurrence_kept(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "First", "WEB_ADDRESS": "https://example.gov/page", "PRIORITY_RESOURCE": "YES"},
            {"RESOURCE_NAME": "Second", "WEB_ADDRESS": "https://example.gov/page", "PRIORITY_RESOURCE": ""},
        ])
        result = load_urls(str(f))
        assert len(result) == 1
        assert result[0]["priority"] is True

    def test_case_insensitive_scheme_and_host_dedup(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://Example.Gov/path", "PRIORITY_RESOURCE": ""},
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://example.gov/path", "PRIORITY_RESOURCE": ""},
        ])
        result = load_urls(str(f))
        assert len(result) == 1

    def test_different_paths_not_deduped(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://example.gov/a", "PRIORITY_RESOURCE": ""},
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://example.gov/b", "PRIORITY_RESOURCE": ""},
        ])
        result = load_urls(str(f))
        assert len(result) == 2

    def test_duplicate_logged(self, tmp_path, caplog):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://example.gov/", "PRIORITY_RESOURCE": ""},
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://example.gov/", "PRIORITY_RESOURCE": ""},
        ])
        with caplog.at_level(logging.WARNING, logger="crawler.orchestrator"):
            load_urls(str(f))
        assert any("duplicate" in r.message.lower() for r in caplog.records)

    def test_query_string_ignored_in_dedup(self, tmp_path):
        # Two URLs with same scheme+host+path but different query strings are duplicates
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://example.gov/page?v=1", "PRIORITY_RESOURCE": ""},
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://example.gov/page?v=2", "PRIORITY_RESOURCE": ""},
        ])
        result = load_urls(str(f))
        assert len(result) == 1

    def test_trailing_slash_root_urls_are_duplicates(self, tmp_path):
        # https://example.gov and https://example.gov/ are the same site
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://example.gov", "PRIORITY_RESOURCE": ""},
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://example.gov/", "PRIORITY_RESOURCE": ""},
        ])
        result = load_urls(str(f))
        assert len(result) == 1

    def test_trailing_slash_first_occurrence_kept(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://example.gov", "PRIORITY_RESOURCE": ""},
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://example.gov/", "PRIORITY_RESOURCE": ""},
        ])
        result = load_urls(str(f))
        assert result[0]["url"] == "https://example.gov"  # first occurrence preserved verbatim


# ---------------------------------------------------------------------------
# T-81: priority sorting
# ---------------------------------------------------------------------------

class TestLoadUrlsPriority:
    def test_priority_flag_set_true_for_yes(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://example.gov/", "PRIORITY_RESOURCE": "YES"},
        ])
        result = load_urls(str(f))
        assert result[0]["priority"] is True

    def test_priority_flag_false_for_empty(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://example.gov/", "PRIORITY_RESOURCE": ""},
        ])
        result = load_urls(str(f))
        assert result[0]["priority"] is False

    def test_priority_flag_false_for_no(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://example.gov/", "PRIORITY_RESOURCE": "NO"},
        ])
        result = load_urls(str(f))
        assert result[0]["priority"] is False

    def test_yes_case_insensitive(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://a.gov/", "PRIORITY_RESOURCE": "yes"},
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://b.gov/", "PRIORITY_RESOURCE": "Yes"},
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://c.gov/", "PRIORITY_RESOURCE": "YES"},
        ])
        result = load_urls(str(f))
        assert all(r["priority"] is True for r in result)

    def test_priority_urls_sorted_first(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://normal.gov/", "PRIORITY_RESOURCE": ""},
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://priority.gov/", "PRIORITY_RESOURCE": "YES"},
        ])
        result = load_urls(str(f))
        assert result[0]["url"] == "https://priority.gov/"
        assert result[1]["url"] == "https://normal.gov/"

    def test_relative_order_preserved_within_priority_group(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://p1.gov/", "PRIORITY_RESOURCE": "YES"},
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://n1.gov/", "PRIORITY_RESOURCE": ""},
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://p2.gov/", "PRIORITY_RESOURCE": "YES"},
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://n2.gov/", "PRIORITY_RESOURCE": ""},
        ])
        result = load_urls(str(f))
        urls = [r["url"] for r in result]
        # Priority group: p1 before p2 (original order)
        assert urls.index("https://p1.gov/") < urls.index("https://p2.gov/")
        # Non-priority group: n1 before n2 (original order)
        assert urls.index("https://n1.gov/") < urls.index("https://n2.gov/")
        # All priority come before non-priority
        assert urls.index("https://p1.gov/") < urls.index("https://n1.gov/")
        assert urls.index("https://p2.gov/") < urls.index("https://n1.gov/")

    def test_all_priority_preserves_order(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://a.gov/", "PRIORITY_RESOURCE": "YES"},
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://b.gov/", "PRIORITY_RESOURCE": "YES"},
        ])
        result = load_urls(str(f))
        assert [r["url"] for r in result] == ["https://a.gov/", "https://b.gov/"]

    def test_all_non_priority_preserves_order(self, tmp_path):
        f = tmp_path / "urls.csv"
        _write_csv(f, [
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://a.gov/", "PRIORITY_RESOURCE": ""},
            {"RESOURCE_NAME": "", "WEB_ADDRESS": "https://b.gov/", "PRIORITY_RESOURCE": ""},
        ])
        result = load_urls(str(f))
        assert [r["url"] for r in result] == ["https://a.gov/", "https://b.gov/"]
