"""Unit tests for Phase 9 (T-90–T-94): ReportWriter."""
import csv
from pathlib import Path

import pytest

from reporter.writer import COLUMNS, ReportWriter, _serialize


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _full_result(**overrides) -> dict:
    """Return a complete result dict with sensible defaults."""
    base = {
        "url": "https://example.gov/",
        "priority": False,
        "state": "NATIONAL",
        "active": True,
        "http_status": 200,
        "final_url": "https://example.gov/",
        "robots_allowed": True,
        "robots_status": "allowed",
        "js_rendered": False,
        "relevance_score": 42,
        "matched_keywords": ["county", "district"],
        "datasets_found": True,
        "dataset_urls": ["https://example.gov/data.csv"],
        "dataset_formats": ["csv"],
        "crawl_depth_reached": 1,
        "portal_platform": "",
        "error_notes": "",
    }
    base.update(overrides)
    return base


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------------------
# T-90: Fresh run — directory creation and header
# ---------------------------------------------------------------------------

class TestFreshRun:
    def test_creates_output_directory(self, tmp_path):
        out = tmp_path / "output" / "2026-01-01"
        with ReportWriter(out) as w:
            w.open()
        assert out.is_dir()

    def test_creates_results_csv(self, tmp_path):
        out = tmp_path / "2026-01-01"
        with ReportWriter(out) as w:
            w.open()
        assert (out / "results.csv").exists()

    def test_writes_header_row(self, tmp_path):
        out = tmp_path / "run"
        with ReportWriter(out) as w:
            w.open()
        rows = _read_csv(out / "results.csv")
        assert rows == []  # header written but no data rows
        with (out / "results.csv").open() as fh:
            header = fh.readline().strip().split(",")
        assert header == COLUMNS

    def test_open_returns_empty_set(self, tmp_path):
        out = tmp_path / "run"
        with ReportWriter(out) as w:
            seen = w.open()
        assert seen == set()

    def test_idempotent_directory_creation(self, tmp_path):
        out = tmp_path / "run"
        out.mkdir(parents=True)
        with ReportWriter(out) as w:
            w.open()  # should not raise even though dir already exists
        assert (out / "results.csv").exists()


# ---------------------------------------------------------------------------
# T-90: append_row
# ---------------------------------------------------------------------------

class TestAppendRow:
    def test_writes_one_row(self, tmp_path):
        out = tmp_path / "run"
        with ReportWriter(out) as w:
            w.open()
            w.append_row(_full_result())
        rows = _read_csv(out / "results.csv")
        assert len(rows) == 1
        assert rows[0]["url"] == "https://example.gov/"

    def test_writes_multiple_rows(self, tmp_path):
        out = tmp_path / "run"
        with ReportWriter(out) as w:
            w.open()
            w.append_row(_full_result(url="https://a.gov/"))
            w.append_row(_full_result(url="https://b.gov/"))
        rows = _read_csv(out / "results.csv")
        assert [r["url"] for r in rows] == ["https://a.gov/", "https://b.gov/"]

    def test_booleans_serialized_as_lowercase_strings(self, tmp_path):
        out = tmp_path / "run"
        with ReportWriter(out) as w:
            w.open()
            w.append_row(_full_result(active=True, priority=False, js_rendered=False, datasets_found=True))
        row = _read_csv(out / "results.csv")[0]
        assert row["active"] == "true"
        assert row["priority"] == "false"
        assert row["js_rendered"] == "false"
        assert row["datasets_found"] == "true"

    def test_lists_serialized_as_pipe_separated(self, tmp_path):
        out = tmp_path / "run"
        with ReportWriter(out) as w:
            w.open()
            w.append_row(_full_result(
                matched_keywords=["county", "district", "municipality"],
                dataset_urls=["https://a.gov/d.csv", "https://a.gov/e.json"],
                dataset_formats=["csv", "json"],
            ))
        row = _read_csv(out / "results.csv")[0]
        assert row["matched_keywords"] == "county|district|municipality"
        assert row["dataset_urls"] == "https://a.gov/d.csv|https://a.gov/e.json"
        assert row["dataset_formats"] == "csv|json"

    def test_empty_list_serialized_as_empty_string(self, tmp_path):
        out = tmp_path / "run"
        with ReportWriter(out) as w:
            w.open()
            w.append_row(_full_result(matched_keywords=[]))
        row = _read_csv(out / "results.csv")[0]
        assert row["matched_keywords"] == ""

    def test_none_robots_allowed_serialized_as_empty_string(self, tmp_path):
        out = tmp_path / "run"
        with ReportWriter(out) as w:
            w.open()
            w.append_row(_full_result(robots_allowed=None, robots_status="unavailable"))
        row = _read_csv(out / "results.csv")[0]
        assert row["robots_allowed"] == ""
        assert row["robots_status"] == "unavailable"

    def test_all_columns_present_in_row(self, tmp_path):
        out = tmp_path / "run"
        with ReportWriter(out) as w:
            w.open()
            w.append_row(_full_result())
        row = _read_csv(out / "results.csv")[0]
        assert set(row.keys()) == set(COLUMNS)

    def test_raises_if_open_not_called(self, tmp_path):
        w = ReportWriter(tmp_path / "run")
        with pytest.raises(RuntimeError):
            w.append_row(_full_result())

    def test_file_flushed_after_each_row(self, tmp_path):
        out = tmp_path / "run"
        w = ReportWriter(out)
        w.open()
        w.append_row(_full_result(url="https://a.gov/"))
        # File is readable mid-session (flush happened)
        rows = _read_csv(out / "results.csv")
        assert len(rows) == 1
        w.close()


# ---------------------------------------------------------------------------
# T-91: --resume mode
# ---------------------------------------------------------------------------

class TestResumeMode:
    def test_resume_returns_seen_urls(self, tmp_path):
        out = tmp_path / "run"
        # Write initial CSV
        with ReportWriter(out) as w:
            w.open()
            w.append_row(_full_result(url="https://seen.gov/"))

        # Resume
        with ReportWriter(out) as w:
            seen = w.open(resume=True)
        assert "https://seen.gov/" in seen

    def test_resume_does_not_rewrite_header(self, tmp_path):
        out = tmp_path / "run"
        with ReportWriter(out) as w:
            w.open()
            w.append_row(_full_result(url="https://first.gov/"))

        with ReportWriter(out) as w:
            w.open(resume=True)
            w.append_row(_full_result(url="https://second.gov/"))

        rows = _read_csv(out / "results.csv")
        assert len(rows) == 2
        assert rows[0]["url"] == "https://first.gov/"
        assert rows[1]["url"] == "https://second.gov/"

    def test_resume_appends_row_after_existing(self, tmp_path):
        out = tmp_path / "run"
        with ReportWriter(out) as w:
            w.open()
            w.append_row(_full_result(url="https://a.gov/"))

        with ReportWriter(out) as w:
            w.open(resume=True)
            w.append_row(_full_result(url="https://b.gov/"))

        rows = _read_csv(out / "results.csv")
        assert [r["url"] for r in rows] == ["https://a.gov/", "https://b.gov/"]

    def test_resume_with_no_existing_file_acts_as_fresh(self, tmp_path):
        out = tmp_path / "run"
        with ReportWriter(out) as w:
            seen = w.open(resume=True)
        assert seen == set()
        assert (out / "results.csv").exists()

    def test_resume_returns_all_seen_urls_from_file(self, tmp_path):
        out = tmp_path / "run"
        with ReportWriter(out) as w:
            w.open()
            for url in ["https://a.gov/", "https://b.gov/", "https://c.gov/"]:
                w.append_row(_full_result(url=url))

        with ReportWriter(out) as w:
            seen = w.open(resume=True)
        assert seen == {"https://a.gov/", "https://b.gov/", "https://c.gov/"}


# ---------------------------------------------------------------------------
# T-92: collect_seen_urls (--new-only)
# ---------------------------------------------------------------------------

class TestCollectSeenUrls:
    def _write_run(self, output_root: Path, date: str, urls: list[str]):
        run_dir = output_root / date
        with ReportWriter(run_dir) as w:
            w.open()
            for url in urls:
                w.append_row(_full_result(url=url))
        return run_dir

    def test_nonexistent_root_returns_empty_set(self, tmp_path):
        seen = ReportWriter.collect_seen_urls(tmp_path / "missing")
        assert seen == set()

    def test_empty_root_returns_empty_set(self, tmp_path):
        root = tmp_path / "output"
        root.mkdir()
        seen = ReportWriter.collect_seen_urls(root)
        assert seen == set()

    def test_collects_from_single_run(self, tmp_path):
        root = tmp_path / "output"
        self._write_run(root, "2026-01-01", ["https://a.gov/", "https://b.gov/"])
        seen = ReportWriter.collect_seen_urls(root)
        assert seen == {"https://a.gov/", "https://b.gov/"}

    def test_collects_from_multiple_runs(self, tmp_path):
        root = tmp_path / "output"
        self._write_run(root, "2026-01-01", ["https://a.gov/"])
        self._write_run(root, "2026-01-02", ["https://b.gov/"])
        self._write_run(root, "2026-01-03", ["https://c.gov/"])
        seen = ReportWriter.collect_seen_urls(root)
        assert seen == {"https://a.gov/", "https://b.gov/", "https://c.gov/"}

    def test_excludes_specified_directory(self, tmp_path):
        root = tmp_path / "output"
        self._write_run(root, "2026-01-01", ["https://old.gov/"])
        current = self._write_run(root, "2026-01-02", ["https://current.gov/"])
        seen = ReportWriter.collect_seen_urls(root, exclude_dir=current)
        assert "https://old.gov/" in seen
        assert "https://current.gov/" not in seen

    def test_deduplicates_across_runs(self, tmp_path):
        root = tmp_path / "output"
        self._write_run(root, "2026-01-01", ["https://same.gov/"])
        self._write_run(root, "2026-01-02", ["https://same.gov/"])
        seen = ReportWriter.collect_seen_urls(root)
        assert len(seen) == 1
        assert "https://same.gov/" in seen

    def test_skips_subdir_without_results_csv(self, tmp_path):
        root = tmp_path / "output"
        (root / "2026-01-01").mkdir(parents=True)  # dir with no CSV
        self._write_run(root, "2026-01-02", ["https://a.gov/"])
        seen = ReportWriter.collect_seen_urls(root)
        assert seen == {"https://a.gov/"}


# ---------------------------------------------------------------------------
# T-94: make_error_row
# ---------------------------------------------------------------------------

class TestMakeErrorRow:
    def test_active_is_false(self):
        row = ReportWriter.make_error_row("https://err.gov/", False, "connection refused")
        assert row["active"] is False

    def test_http_status_is_zero(self):
        row = ReportWriter.make_error_row("https://err.gov/", False, "timeout")
        assert row["http_status"] == 0

    def test_relevance_score_is_zero(self):
        row = ReportWriter.make_error_row("https://err.gov/", True, "DNS failure")
        assert row["relevance_score"] == 0

    def test_error_notes_contains_message(self):
        row = ReportWriter.make_error_row("https://err.gov/", False, "something went wrong")
        assert row["error_notes"] == "something went wrong"

    def test_url_and_priority_preserved(self):
        row = ReportWriter.make_error_row("https://err.gov/", True, "boom")
        assert row["url"] == "https://err.gov/"
        assert row["priority"] is True

    def test_list_fields_are_empty_lists(self):
        row = ReportWriter.make_error_row("https://err.gov/", False, "oops")
        assert row["matched_keywords"] == []
        assert row["dataset_urls"] == []
        assert row["dataset_formats"] == []

    def test_robots_allowed_is_none(self):
        row = ReportWriter.make_error_row("https://err.gov/", False, "err")
        assert row["robots_allowed"] is None

    def test_error_row_is_writable(self, tmp_path):
        out = tmp_path / "run"
        with ReportWriter(out) as w:
            w.open()
            w.append_row(ReportWriter.make_error_row("https://err.gov/", False, "crash"))
        rows = _read_csv(out / "results.csv")
        assert len(rows) == 1
        assert rows[0]["active"] == "false"
        assert rows[0]["error_notes"] == "crash"
        assert rows[0]["robots_allowed"] == ""


# ---------------------------------------------------------------------------
# _serialize (unit tests for the serialization helper)
# ---------------------------------------------------------------------------

class TestSerialize:
    def test_bool_true_becomes_string_true(self):
        row = _serialize({"active": True})
        assert row["active"] == "true"

    def test_bool_false_becomes_string_false(self):
        row = _serialize({"active": False})
        assert row["active"] == "false"

    def test_none_becomes_empty_string(self):
        row = _serialize({"robots_allowed": None})
        assert row["robots_allowed"] == ""

    def test_list_becomes_pipe_separated(self):
        row = _serialize({"matched_keywords": ["a", "b", "c"]})
        assert row["matched_keywords"] == "a|b|c"

    def test_empty_list_becomes_empty_string(self):
        row = _serialize({"matched_keywords": []})
        assert row["matched_keywords"] == ""

    def test_missing_column_defaults_to_empty_string(self):
        row = _serialize({})
        assert row["error_notes"] == ""

    def test_missing_bool_column_defaults_to_false_not_empty(self):
        row = _serialize({})
        assert row["active"] == "false"
        assert row["priority"] == "false"
        assert row["js_rendered"] == "false"
        assert row["datasets_found"] == "false"

    def test_integer_passed_through(self):
        row = _serialize({"relevance_score": 75})
        assert row["relevance_score"] == 75

    def test_all_columns_present_in_output(self):
        row = _serialize({})
        assert set(row.keys()) == set(COLUMNS)
