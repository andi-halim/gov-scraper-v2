"""Tests for Phase 10: run.py entrypoint (T-49, T-93, T-94, T-100–T-103)."""
import csv
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import run as run_module
from run import _build_arg_parser, _process_url, main
from reporter.writer import COLUMNS, ReportWriter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RICH_HTML = "<html><head><title>State Finance</title></head><body>" + "word " * 60 + "</body></html>"


def _make_http_client(
    html=_RICH_HTML,
    final_url="https://example.gov/",
    http_status=200,
    js_rendered=False,
    response_headers=None,
):
    client = MagicMock()
    client.last_response_headers = response_headers or {}
    client.fetch_page.return_value = (html, final_url, http_status, js_rendered, False)
    return client


def _make_robots(allowed=True, status="allowed"):
    checker = MagicMock()
    checker.is_allowed.return_value = (allowed, status)
    return checker


def _make_portal_detector(platform=None, method="none"):
    det = MagicMock()
    det.detect.return_value = (platform, method)
    return det


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _score_result(**overrides) -> dict:
    base = {"relevance_score": 10, "matched_keywords": ["county"]}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# T-100: argparse defaults and overrides
# ---------------------------------------------------------------------------

class TestArgParse:
    def test_depth_default(self):
        args = _build_arg_parser().parse_args([])
        assert args.depth == 2

    def test_delay_default(self):
        args = _build_arg_parser().parse_args([])
        assert args.delay == 2.0

    def test_output_default_none(self):
        args = _build_arg_parser().parse_args([])
        assert args.output is None

    def test_input_default(self):
        args = _build_arg_parser().parse_args([])
        assert args.input == "config/urls.csv"

    def test_resume_default_false(self):
        args = _build_arg_parser().parse_args([])
        assert args.resume is False

    def test_new_only_default_false(self):
        args = _build_arg_parser().parse_args([])
        assert args.new_only is False

    def test_depth_override(self):
        args = _build_arg_parser().parse_args(["--depth", "3"])
        assert args.depth == 3

    def test_delay_override(self):
        args = _build_arg_parser().parse_args(["--delay", "5.0"])
        assert args.delay == 5.0

    def test_output_override(self):
        args = _build_arg_parser().parse_args(["--output", "/tmp/out"])
        assert args.output == "/tmp/out"

    def test_input_override(self):
        args = _build_arg_parser().parse_args(["--input", "custom.csv"])
        assert args.input == "custom.csv"

    def test_resume_flag(self):
        args = _build_arg_parser().parse_args(["--resume"])
        assert args.resume is True

    def test_new_only_flag(self):
        args = _build_arg_parser().parse_args(["--new-only"])
        assert args.new_only is True


# ---------------------------------------------------------------------------
# T-93: --resume / --new-only mutual exclusivity
# ---------------------------------------------------------------------------

class TestMutualExclusivity:
    def test_both_flags_cause_system_exit(self):
        """parser.error() raises SystemExit; assert the process would abort."""
        with pytest.raises(SystemExit) as exc_info:
            main(["--resume", "--new-only"])
        assert exc_info.value.code != 0

    def test_resume_alone_is_parsed_ok(self):
        args = _build_arg_parser().parse_args(["--resume"])
        assert args.resume
        assert not args.new_only

    def test_new_only_alone_is_parsed_ok(self):
        args = _build_arg_parser().parse_args(["--new-only"])
        assert args.new_only
        assert not args.resume


# ---------------------------------------------------------------------------
# T-101: startup check for config/state_definitions.json
# ---------------------------------------------------------------------------

class TestStartupCheck:
    def test_returns_1_when_state_defs_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config").mkdir()
        urls_csv = tmp_path / "config" / "urls.csv"
        urls_csv.write_text("WEB_ADDRESS,PRIORITY_RESOURCE\n")
        result = main(["--input", str(urls_csv)])
        assert result == 1

    def test_prints_error_message_when_state_defs_missing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config").mkdir()
        urls_csv = tmp_path / "config" / "urls.csv"
        urls_csv.write_text("WEB_ADDRESS,PRIORITY_RESOURCE\n")
        main(["--input", str(urls_csv)])
        captured = capsys.readouterr()
        assert "state_definitions.json" in captured.err
        assert "setup" in captured.err


# ---------------------------------------------------------------------------
# T-94: per-URL error handling in main()
# ---------------------------------------------------------------------------

class TestPerUrlErrorHandling:
    def _run_with_error_url(self, tmp_path, monkeypatch):
        """Runs main() with two URLs: one raises, one succeeds. Returns output rows."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config").mkdir()
        # Minimal state_definitions.json so startup check passes
        (tmp_path / "config" / "state_definitions.json").write_text("{}")
        (tmp_path / "config" / "keywords.csv").write_text("county\n")
        urls_csv = tmp_path / "config" / "urls.csv"
        urls_csv.write_text(
            "WEB_ADDRESS,PRIORITY_RESOURCE\n"
            "https://fail.gov/,NO\n"
            "https://ok.gov/,NO\n"
        )
        output_dir = tmp_path / "output" / "2026-01-01"

        with patch("run._process_url") as mock_proc:
            def side_effect(url, **kwargs):
                if "fail" in url:
                    raise RuntimeError("simulated crash")
                return {
                    "url": url, "priority": False, "state": "NATIONAL",
                    "active": True, "http_status": 200, "final_url": url,
                    "robots_allowed": True, "robots_status": "allowed",
                    "js_rendered": False, "relevance_score": 0,
                    "matched_keywords": [], "datasets_found": False,
                    "dataset_urls": [], "dataset_formats": [],
                    "crawl_depth_reached": 0, "portal_platform": "",
                    "error_notes": "",
                }
            mock_proc.side_effect = side_effect

            with patch("run.HttpClient") as MockClient, \
                 patch("run.RobotsChecker"), \
                 patch("run.PortalDetector"), \
                 patch("run.PortalDetector"):
                MockClient.return_value.__enter__ = lambda s: MockClient.return_value
                MockClient.return_value.__exit__ = MagicMock(return_value=False)
                main(["--input", str(urls_csv), "--output", str(output_dir)])

        return _read_csv(output_dir / "results.csv")

    def test_error_url_writes_error_row(self, tmp_path, monkeypatch):
        rows = self._run_with_error_url(tmp_path, monkeypatch)
        fail_row = next(r for r in rows if "fail.gov" in r["url"])
        assert fail_row["active"] == "false"
        assert "simulated crash" in fail_row["error_notes"]

    def test_run_continues_after_error(self, tmp_path, monkeypatch):
        rows = self._run_with_error_url(tmp_path, monkeypatch)
        assert len(rows) == 2
        ok_row = next(r for r in rows if "ok.gov" in r["url"])
        assert ok_row["active"] == "true"

    def test_both_urls_present_in_output(self, tmp_path, monkeypatch):
        rows = self._run_with_error_url(tmp_path, monkeypatch)
        urls = {r["url"] for r in rows}
        assert "https://fail.gov/" in urls
        assert "https://ok.gov/" in urls


# ---------------------------------------------------------------------------
# _process_url: robots check
# ---------------------------------------------------------------------------

class TestProcessUrlRobots:
    def _call(self, allowed, status, url="https://example.gov/"):
        client = _make_http_client()
        robots = _make_robots(allowed, status)
        portal = _make_portal_detector()
        return _process_url(
            url=url, priority=False, state="NATIONAL",
            http_client=client, robots_checker=robots,
            portal_detector=portal, depth=2,
        )

    def test_allowed_recorded_true(self):
        result = self._call(True, "allowed")
        assert result["robots_allowed"] is True

    def test_disallowed_returns_early(self):
        result = self._call(False, "disallowed")
        assert result["active"] is False
        assert "robots" in result["error_notes"].lower()

    def test_disallowed_does_not_fetch(self):
        client = _make_http_client()
        robots = _make_robots(False, "disallowed")
        _process_url(
            url="https://example.gov/", priority=False, state="NATIONAL",
            http_client=client, robots_checker=robots,
            portal_detector=_make_portal_detector(), depth=2,
        )
        client.fetch_page.assert_not_called()

    def test_unavailable_recorded(self):
        result = self._call(True, "unavailable")
        assert result["robots_status"] == "unavailable"


# ---------------------------------------------------------------------------
# _process_url: network errors
# ---------------------------------------------------------------------------

class TestProcessUrlNetworkError:
    def test_network_error_sets_active_false(self):
        client = MagicMock()
        client.fetch_page.side_effect = OSError("connection refused")
        result = _process_url(
            url="https://example.gov/", priority=False, state="NATIONAL",
            http_client=client, robots_checker=_make_robots(),
            portal_detector=_make_portal_detector(), depth=2,
        )
        assert result["active"] is False

    def test_network_error_recorded_in_error_notes(self):
        client = MagicMock()
        client.fetch_page.side_effect = OSError("DNS lookup failed")
        result = _process_url(
            url="https://example.gov/", priority=False, state="NATIONAL",
            http_client=client, robots_checker=_make_robots(),
            portal_detector=_make_portal_detector(), depth=2,
        )
        assert "DNS lookup failed" in result["error_notes"]

    def test_network_error_http_status_zero(self):
        client = MagicMock()
        client.fetch_page.side_effect = ConnectionError("timeout")
        result = _process_url(
            url="https://example.gov/", priority=False, state="NATIONAL",
            http_client=client, robots_checker=_make_robots(),
            portal_detector=_make_portal_detector(), depth=2,
        )
        assert result["http_status"] == 0


# ---------------------------------------------------------------------------
# _process_url: inactive (non-200) URLs
# ---------------------------------------------------------------------------

class TestProcessUrlInactive:
    def _call_with_status(self, status):
        client = _make_http_client(html="<html><body>Error</body></html>",
                                    http_status=status)
        return _process_url(
            url="https://example.gov/", priority=False, state="NATIONAL",
            http_client=client, robots_checker=_make_robots(),
            portal_detector=_make_portal_detector(), depth=2,
        )

    def test_404_sets_active_false(self):
        result = self._call_with_status(404)
        assert result["active"] is False
        assert result["http_status"] == 404

    def test_500_sets_active_false(self):
        result = self._call_with_status(500)
        assert result["active"] is False

    def test_inactive_does_not_crawl(self):
        client = _make_http_client(http_status=404)
        portal = _make_portal_detector()
        _process_url(
            url="https://example.gov/", priority=False, state="NATIONAL",
            http_client=client, robots_checker=_make_robots(),
            portal_detector=portal, depth=2,
        )
        portal.detect.assert_not_called()

    def test_inactive_no_error_notes(self):
        result = self._call_with_status(404)
        assert result["error_notes"] == ""


# ---------------------------------------------------------------------------
# T-49: portal detection and routing
# ---------------------------------------------------------------------------

class TestPortalRouting:
    def _call_with_portal(self, platform):
        client = _make_http_client()
        portal = _make_portal_detector(platform=platform, method="passive")

        with patch("run.crawl_url") as mock_crawl, \
             patch("run.detect_datasets") as mock_detect, \
             patch("run.score_page") as mock_score, \
             patch("run.get_effective_keywords", return_value=frozenset({"county"})):
            result = _process_url(
                url="https://data.example.gov/", priority=False, state="NATIONAL",
                http_client=client, robots_checker=_make_robots(),
                portal_detector=portal, depth=2,
            )
        return result, mock_crawl, mock_detect, mock_score

    def test_socrata_platform_recorded(self):
        result, _, _, _ = self._call_with_portal("Socrata")
        assert result["portal_platform"] == "Socrata"

    def test_ckan_platform_recorded(self):
        result, _, _, _ = self._call_with_portal("CKAN")
        assert result["portal_platform"] == "CKAN"

    def test_arcgis_hub_platform_recorded(self):
        result, _, _, _ = self._call_with_portal("ArcGIS Hub")
        assert result["portal_platform"] == "ArcGIS Hub"

    def test_portal_relevance_score_is_null(self):
        result, _, _, _ = self._call_with_portal("Socrata")
        assert result["relevance_score"] is None

    def test_portal_skips_depth_crawl(self):
        _, mock_crawl, _, _ = self._call_with_portal("Socrata")
        mock_crawl.assert_not_called()

    def test_portal_skips_dataset_detector(self):
        _, _, mock_detect, _ = self._call_with_portal("Socrata")
        mock_detect.assert_not_called()

    def test_portal_skips_scorer(self):
        _, _, _, mock_score = self._call_with_portal("Socrata")
        mock_score.assert_not_called()

    def test_no_portal_triggers_depth_crawl(self):
        client = _make_http_client()
        portal = _make_portal_detector(platform=None)

        with patch("run.crawl_url", return_value=([], 0, {})) as mock_crawl, \
             patch("run.detect_datasets", return_value=(False, [], [])), \
             patch("run.score_page", return_value=_score_result()), \
             patch("run.get_effective_keywords", return_value=frozenset({"county"})):
            _process_url(
                url="https://example.gov/", priority=False, state="NATIONAL",
                http_client=client, robots_checker=_make_robots(),
                portal_detector=portal, depth=2,
            )
        mock_crawl.assert_called_once()


# ---------------------------------------------------------------------------
# _process_url: standard (non-portal) pipeline
# ---------------------------------------------------------------------------

class TestProcessUrlStandardPipeline:
    def _call(self, url="https://example.gov/", state="NATIONAL",
               crawl_pages=None, crawl_depth=0,
               datasets=(False, [], []),
               score=None):
        client = _make_http_client()
        portal = _make_portal_detector(platform=None)
        score = score or _score_result()
        crawl_pages = crawl_pages or []

        with patch("run.crawl_url", return_value=(crawl_pages, crawl_depth, {})), \
             patch("run.detect_datasets", return_value=datasets), \
             patch("run.score_page", return_value=score), \
             patch("run.get_effective_keywords", return_value=frozenset({"county"})):
            return _process_url(
                url=url, priority=False, state=state,
                http_client=client, robots_checker=_make_robots(),
                portal_detector=portal, depth=2,
            )

    def test_active_true_on_200(self):
        result = self._call()
        assert result["active"] is True
        assert result["http_status"] == 200

    def test_state_recorded(self):
        result = self._call(state="TX")
        assert result["state"] == "TX"

    def test_crawl_depth_recorded(self):
        result = self._call(crawl_depth=2)
        assert result["crawl_depth_reached"] == 2

    def test_datasets_found_true(self):
        result = self._call(datasets=(True, ["https://e.gov/data.csv"], ["csv"]))
        assert result["datasets_found"] is True
        assert "https://e.gov/data.csv" in result["dataset_urls"]

    def test_datasets_found_false(self):
        result = self._call(datasets=(False, [], []))
        assert result["datasets_found"] is False

    def test_relevance_score_recorded(self):
        result = self._call(score=_score_result(relevance_score=75))
        assert result["relevance_score"] == 75

    def test_matched_keywords_recorded(self):
        result = self._call(score=_score_result(matched_keywords=["county", "township"]))
        assert "county" in result["matched_keywords"]
        assert "township" in result["matched_keywords"]

    def test_portal_platform_empty_for_non_portal(self):
        result = self._call()
        assert result["portal_platform"] == ""

    def test_js_rendered_flag_propagated(self):
        client = _make_http_client(js_rendered=True)
        portal = _make_portal_detector()
        with patch("run.crawl_url", return_value=([], 0, {})), \
             patch("run.detect_datasets", return_value=(False, [], [])), \
             patch("run.score_page", return_value=_score_result()), \
             patch("run.get_effective_keywords", return_value=frozenset()):
            result = _process_url(
                url="https://example.gov/", priority=False, state="NATIONAL",
                http_client=client, robots_checker=_make_robots(),
                portal_detector=portal, depth=2,
            )
        assert result["js_rendered"] is True

    def test_prefetched_seed_passed_to_crawl_url(self):
        client = _make_http_client(html=_RICH_HTML, final_url="https://example.gov/",
                                   http_status=200, js_rendered=True)
        portal = _make_portal_detector(platform=None)
        with patch("run.crawl_url", return_value=([], 0, {})) as mock_crawl, \
             patch("run.detect_datasets", return_value=(False, [], [])), \
             patch("run.score_page", return_value=_score_result()), \
             patch("run.get_effective_keywords", return_value=frozenset()):
            _process_url(
                url="https://example.gov/", priority=False, state="NATIONAL",
                http_client=client, robots_checker=_make_robots(),
                portal_detector=portal, depth=2,
            )
        _, kwargs = mock_crawl.call_args
        assert kwargs.get("prefetched_seed") == (_RICH_HTML, "https://example.gov/", 200, True)

    def test_priority_flag_preserved(self):
        client = _make_http_client()
        with patch("run.crawl_url", return_value=([], 0, {})), \
             patch("run.detect_datasets", return_value=(False, [], [])), \
             patch("run.score_page", return_value=_score_result()), \
             patch("run.get_effective_keywords", return_value=frozenset()):
            result = _process_url(
                url="https://example.gov/", priority=True, state="NATIONAL",
                http_client=client, robots_checker=_make_robots(),
                portal_detector=_make_portal_detector(), depth=2,
            )
        assert result["priority"] is True


# ---------------------------------------------------------------------------
# main(): skip logic
# ---------------------------------------------------------------------------

class TestMainSkipLogic:
    def _setup_env(self, tmp_path, monkeypatch, urls):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "state_definitions.json").write_text("{}")
        (config_dir / "keywords.csv").write_text("county\n")
        urls_csv = config_dir / "urls.csv"
        lines = ["WEB_ADDRESS,PRIORITY_RESOURCE,STATE"] + [f"{u},NO,NATIONAL" for u in urls]
        urls_csv.write_text("\n".join(lines) + "\n")
        return urls_csv

    def test_resume_skips_already_written_url(self, tmp_path, monkeypatch):
        urls = ["https://a.gov/", "https://b.gov/"]
        urls_csv = self._setup_env(tmp_path, monkeypatch, urls)
        output_dir = tmp_path / "output" / "2026-01-01"

        # First run writes https://a.gov/
        a_result = {
            "url": "https://a.gov/", "priority": False, "state": "NATIONAL",
            "active": True, "http_status": 200, "final_url": "https://a.gov/",
            "robots_allowed": True, "robots_status": "allowed", "js_rendered": False,
            "relevance_score": 0, "matched_keywords": [], "datasets_found": False,
            "dataset_urls": [], "dataset_formats": [], "crawl_depth_reached": 0,
            "portal_platform": "", "error_notes": "",
        }
        output_dir.mkdir(parents=True)
        with ReportWriter(output_dir) as w:
            w.open()
            w.append_row(a_result)

        call_log = []
        def fake_process(url, **kwargs):
            call_log.append(url)
            return {**a_result, "url": url}

        with patch("run._process_url", side_effect=fake_process), \
             patch("run.HttpClient") as MockClient, \
             patch("run.RobotsChecker"), \
             patch("run.PortalDetector"):
            MockClient.return_value.__enter__ = lambda s: MockClient.return_value
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            main(["--input", str(urls_csv), "--output", str(output_dir), "--resume"])

        assert "https://a.gov/" not in call_log
        assert "https://b.gov/" in call_log

    def test_new_only_skips_previously_seen_url(self, tmp_path, monkeypatch):
        urls = ["https://old.gov/", "https://new.gov/"]
        urls_csv = self._setup_env(tmp_path, monkeypatch, urls)
        output_dir = tmp_path / "output" / "2026-01-02"

        # Write https://old.gov/ as a previous run
        prev_dir = tmp_path / "output" / "2026-01-01"
        old_result = {
            "url": "https://old.gov/", "priority": False, "state": "NATIONAL",
            "active": True, "http_status": 200, "final_url": "https://old.gov/",
            "robots_allowed": True, "robots_status": "allowed", "js_rendered": False,
            "relevance_score": 0, "matched_keywords": [], "datasets_found": False,
            "dataset_urls": [], "dataset_formats": [], "crawl_depth_reached": 0,
            "portal_platform": "", "error_notes": "",
        }
        with ReportWriter(prev_dir) as w:
            w.open()
            w.append_row(old_result)

        call_log = []
        def fake_process(url, **kwargs):
            call_log.append(url)
            return {**old_result, "url": url}

        with patch("run._process_url", side_effect=fake_process), \
             patch("run.HttpClient") as MockClient, \
             patch("run.RobotsChecker"), \
             patch("run.PortalDetector"):
            MockClient.return_value.__enter__ = lambda s: MockClient.return_value
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            main(["--input", str(urls_csv), "--output", str(output_dir), "--new-only"])

        assert "https://old.gov/" not in call_log
        assert "https://new.gov/" in call_log


# ---------------------------------------------------------------------------
# main(): output CSV integrity
# ---------------------------------------------------------------------------

class TestMainOutputCSV:
    def _run_main(self, tmp_path, monkeypatch, url_count=1):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "state_definitions.json").write_text("{}")
        (config_dir / "keywords.csv").write_text("county\n")
        urls_csv = config_dir / "urls.csv"
        urls = [f"https://example{i}.gov/" for i in range(url_count)]
        lines = ["WEB_ADDRESS,PRIORITY_RESOURCE,STATE"] + [f"{u},NO,NATIONAL" for u in urls]
        urls_csv.write_text("\n".join(lines) + "\n")
        output_dir = tmp_path / "output" / "2026-01-01"

        def fake_process(url, **kwargs):
            return {
                "url": url, "priority": False, "state": "NATIONAL",
                "active": True, "http_status": 200, "final_url": url,
                "robots_allowed": True, "robots_status": "allowed", "js_rendered": False,
                "relevance_score": 5, "matched_keywords": ["county"],
                "datasets_found": False, "dataset_urls": [], "dataset_formats": [],
                "crawl_depth_reached": 0, "portal_platform": "", "error_notes": "",
            }

        with patch("run._process_url", side_effect=fake_process), \
             patch("run.HttpClient") as MockClient, \
             patch("run.RobotsChecker"), \
             patch("run.PortalDetector"):
            MockClient.return_value.__enter__ = lambda s: MockClient.return_value
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            rc = main(["--input", str(urls_csv), "--output", str(output_dir)])

        return rc, output_dir / "results.csv"

    def test_returns_zero_on_success(self, tmp_path, monkeypatch):
        rc, _ = self._run_main(tmp_path, monkeypatch)
        assert rc == 0

    def test_csv_has_correct_columns(self, tmp_path, monkeypatch):
        _, csv_path = self._run_main(tmp_path, monkeypatch)
        rows = _read_csv(csv_path)
        assert set(rows[0].keys()) == set(COLUMNS)

    def test_one_row_per_url(self, tmp_path, monkeypatch):
        _, csv_path = self._run_main(tmp_path, monkeypatch, url_count=3)
        rows = _read_csv(csv_path)
        assert len(rows) == 3

    def test_output_csv_created(self, tmp_path, monkeypatch):
        _, csv_path = self._run_main(tmp_path, monkeypatch)
        assert csv_path.exists()
