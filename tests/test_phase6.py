"""Unit tests for Phase 6 (T-60–T-61): Dataset Detector."""
from unittest.mock import MagicMock

import httpx
import pytest

from crawler.dataset_detector import (
    _check_content_disposition,
    _extract_format_from_url,
    detect_datasets,
)
from crawler.http_client import HttpClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_head_response(status: int = 200, cd_header: str | None = None) -> httpx.Response:
    headers: dict[str, str] = {}
    if cd_header:
        headers["content-disposition"] = cd_header
    resp = httpx.Response(status, headers=httpx.Headers(headers))
    resp.request = httpx.Request("HEAD", "http://example.gov/")
    return resp


def _page(url: str, html: str, status: int = 200) -> tuple:
    return (url, html, status, False)


# ---------------------------------------------------------------------------
# _extract_format_from_url (T-60)
# ---------------------------------------------------------------------------

class TestExtractFormatFromUrl:
    def test_csv(self):
        assert _extract_format_from_url("https://example.gov/data.csv") == "csv"

    def test_xlsx(self):
        assert _extract_format_from_url("https://example.gov/report.xlsx") == "xlsx"

    def test_xls(self):
        assert _extract_format_from_url("https://example.gov/report.xls") == "xls"

    def test_json(self):
        assert _extract_format_from_url("https://example.gov/api/data.json") == "json"

    def test_xml(self):
        assert _extract_format_from_url("https://example.gov/feed.xml") == "xml"

    def test_pdf(self):
        assert _extract_format_from_url("https://example.gov/annual.pdf") == "pdf"

    def test_extension_before_query_string(self):
        assert _extract_format_from_url("https://example.gov/data.csv?v=2") == "csv"

    def test_extension_in_query_string(self):
        assert _extract_format_from_url("https://example.gov/dl?file=report.xlsx") == "xlsx"

    def test_case_insensitive(self):
        assert _extract_format_from_url("https://example.gov/DATA.CSV") == "csv"
        assert _extract_format_from_url("https://example.gov/Report.PDF") == "pdf"

    def test_no_extension_returns_none(self):
        assert _extract_format_from_url("https://example.gov/about") is None

    def test_html_extension_returns_none(self):
        assert _extract_format_from_url("https://example.gov/page.html") is None

    def test_php_without_dataset_ext_returns_none(self):
        assert _extract_format_from_url("https://example.gov/download.php") is None

    def test_path_containing_csv_as_word_not_matched(self):
        # "/get-csv/" has no dot before csv — should not match
        assert _extract_format_from_url("https://example.gov/get-csv/page") is None

    def test_extension_with_fragment(self):
        assert _extract_format_from_url("https://example.gov/data.json#section") == "json"

    def test_compound_extension_csv_gz_returns_none(self):
        assert _extract_format_from_url("https://example.gov/archive.csv.gz") is None

    def test_compound_extension_json_br_returns_none(self):
        assert _extract_format_from_url("https://example.gov/data.json.br") is None

    def test_compound_extension_xlsx_zip_returns_none(self):
        assert _extract_format_from_url("https://example.gov/report.xlsx.zip") is None


# ---------------------------------------------------------------------------
# _check_content_disposition (T-61)
# ---------------------------------------------------------------------------

class TestCheckContentDisposition:
    def _client(self, cd_header: str | None) -> MagicMock:
        mock = MagicMock()
        mock.head.return_value = _make_head_response(200, cd_header)
        return mock

    def test_attachment_with_csv_filename(self):
        result = _check_content_disposition("https://example.gov/dl", self._client('attachment; filename="data.csv"'))
        assert result == "csv"

    def test_attachment_with_xlsx_filename(self):
        result = _check_content_disposition("https://example.gov/dl", self._client('attachment; filename="report.xlsx"'))
        assert result == "xlsx"

    def test_attachment_without_extension_returns_empty_string(self):
        result = _check_content_disposition("https://example.gov/dl", self._client("attachment"))
        assert result == ""

    def test_no_content_disposition_returns_none(self):
        result = _check_content_disposition("https://example.gov/dl", self._client(None))
        assert result is None

    def test_inline_disposition_returns_none(self):
        result = _check_content_disposition("https://example.gov/dl", self._client("inline; filename=report.pdf"))
        assert result is None

    def test_request_failure_returns_none(self, caplog):
        import logging
        mock = MagicMock()
        mock.head.side_effect = ConnectionError("refused")
        with caplog.at_level(logging.WARNING, logger="crawler.dataset_detector"):
            result = _check_content_disposition("https://example.gov/dl", mock)
        assert result is None
        assert any("HEAD request failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# detect_datasets — basic behaviour (T-60)
# ---------------------------------------------------------------------------

class TestDetectDatasetsBasic:
    def test_empty_pages_list(self):
        found, urls, fmts = detect_datasets([])
        assert not found
        assert urls == []
        assert fmts == []

    def test_no_dataset_links(self):
        page = _page("https://example.gov/", "<html><body><a href='/about'>About</a></body></html>")
        found, urls, fmts = detect_datasets([page])
        assert not found
        assert urls == []
        assert fmts == []

    def test_csv_link_detected(self):
        page = _page("https://example.gov/", '<a href="data.csv">Get Data</a>')
        found, urls, fmts = detect_datasets([page])
        assert found
        assert any("data.csv" in u for u in urls)
        assert "csv" in fmts

    def test_pdf_link_included(self):
        page = _page("https://example.gov/", '<a href="annual.pdf">Report</a>')
        found, urls, fmts = detect_datasets([page])
        assert found
        assert "pdf" in fmts

    def test_json_link_detected(self):
        page = _page("https://example.gov/", '<a href="/api/data.json">JSON</a>')
        found, urls, fmts = detect_datasets([page])
        assert found
        assert "json" in fmts

    def test_xml_link_detected(self):
        page = _page("https://example.gov/", '<a href="/feeds/data.xml">XML</a>')
        found, urls, fmts = detect_datasets([page])
        assert found
        assert "xml" in fmts

    def test_xls_link_detected(self):
        page = _page("https://example.gov/", '<a href="/files/old.xls">XLS</a>')
        found, urls, fmts = detect_datasets([page])
        assert found
        assert "xls" in fmts

    def test_multiple_formats_deduplicated(self):
        html = '<a href="a.csv">A</a><a href="b.csv">B</a><a href="c.xlsx">C</a>'
        page = _page("https://example.gov/", html)
        _, urls, fmts = detect_datasets([page])
        assert len(urls) == 3
        assert fmts.count("csv") == 1
        assert "xlsx" in fmts

    def test_formats_sorted(self):
        html = '<a href="d.xlsx">X</a><a href="e.csv">C</a><a href="f.json">J</a>'
        page = _page("https://example.gov/", html)
        _, _, fmts = detect_datasets([page])
        assert fmts == sorted(fmts)

    def test_non_200_page_skipped(self):
        page = _page("https://example.gov/", '<a href="data.csv">Get</a>', status=404)
        found, _, _ = detect_datasets([page])
        assert not found

    def test_zero_status_page_skipped(self):
        page = ("https://example.gov/", '<a href="data.csv">Get</a>', 0, False)
        found, _, _ = detect_datasets([page])
        assert not found

    def test_empty_html_page_skipped(self):
        page = ("https://example.gov/", "", 200, False)
        found, _, _ = detect_datasets([page])
        assert not found

    def test_skips_javascript_href(self):
        page = _page("https://example.gov/", '<a href="javascript:download()">DL</a>')
        found, _, _ = detect_datasets([page])
        assert not found

    def test_skips_mailto_href(self):
        page = _page("https://example.gov/", '<a href="mailto:info@gov.gov">Email</a>')
        found, _, _ = detect_datasets([page])
        assert not found

    def test_multiple_pages_all_scanned(self):
        p1 = _page("https://example.gov/", '<a href="a.csv">CSV</a>')
        p2 = _page("https://example.gov/page2", '<a href="b.xlsx">XLSX</a>')
        found, urls, fmts = detect_datasets([p1, p2])
        assert found
        assert len(urls) == 2
        assert "csv" in fmts and "xlsx" in fmts


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------

class TestDetectDatasetsUrlResolution:
    def test_relative_href_resolved(self):
        page = _page("https://example.gov/section/", '<a href="../data.csv">CSV</a>')
        _, urls, _ = detect_datasets([page])
        assert "https://example.gov/data.csv" in urls

    def test_absolute_href_preserved(self):
        page = _page("https://example.gov/", '<a href="https://data.state.gov/export.json">JSON</a>')
        _, urls, _ = detect_datasets([page])
        assert "https://data.state.gov/export.json" in urls

    def test_root_relative_href_resolved(self):
        page = _page("https://example.gov/section/", '<a href="/files/data.csv">CSV</a>')
        _, urls, _ = detect_datasets([page])
        assert "https://example.gov/files/data.csv" in urls


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDetectDatasetsDeduplication:
    def test_same_url_across_pages_counted_once(self):
        html = '<a href="report.csv">CSV</a>'
        pages = [
            _page("https://example.gov/", html),
            _page("https://example.gov/page2", html),
        ]
        _, urls, _ = detect_datasets(pages)
        assert urls.count("https://example.gov/report.csv") == 1

    def test_duplicate_link_on_same_page(self):
        page = _page("https://example.gov/", '<a href="d.csv">A</a><a href="d.csv">B</a>')
        _, urls, _ = detect_datasets([page])
        assert urls.count("https://example.gov/d.csv") == 1

    def test_same_url_different_fragments_counted_once(self):
        html = '<a href="data.json#section1">A</a><a href="data.json#section2">B</a>'
        page = _page("https://example.gov/", html)
        _, urls, _ = detect_datasets([page])
        assert urls.count("https://example.gov/data.json") == 1

    def test_fragment_stripped_from_returned_url(self):
        page = _page("https://example.gov/", '<a href="report.csv#top">Download</a>')
        _, urls, _ = detect_datasets([page])
        assert urls == ["https://example.gov/report.csv"]


# ---------------------------------------------------------------------------
# Query-string / extension-before-? detection
# ---------------------------------------------------------------------------

class TestDetectDatasetsQueryString:
    def test_extension_before_query_string(self):
        page = _page("https://example.gov/", '<a href="/files/data.csv?v=2">DL</a>')
        found, _, fmts = detect_datasets([page])
        assert found
        assert "csv" in fmts

    def test_extension_in_query_string(self):
        page = _page("https://example.gov/", '<a href="/dl?file=report.xlsx">DL</a>')
        found, _, fmts = detect_datasets([page])
        assert found
        assert "xlsx" in fmts


# ---------------------------------------------------------------------------
# Content-Disposition HEAD probe (T-61)
# ---------------------------------------------------------------------------

class TestDetectDatasetsContentDisposition:
    def _client_with_cd(self, cd_header: str) -> MagicMock:
        mock = MagicMock()
        mock.head.return_value = _make_head_response(200, cd_header)
        return mock

    def test_head_probe_fires_for_download_path(self):
        page = _page("https://example.gov/", '<a href="/download/budget">Get</a>')
        client = self._client_with_cd('attachment; filename="budget.csv"')
        found, _, fmts = detect_datasets([page], http_client=client)
        assert found
        assert "csv" in fmts

    def test_head_probe_fires_for_export_path(self):
        page = _page("https://example.gov/", '<a href="/export/data">Export</a>')
        client = self._client_with_cd('attachment; filename="data.json"')
        found, _, fmts = detect_datasets([page], http_client=client)
        assert found
        assert "json" in fmts

    def test_head_probe_not_fired_when_no_client(self):
        page = _page("https://example.gov/", '<a href="/download/budget">Get</a>')
        found, _, _ = detect_datasets([page], http_client=None)
        assert not found

    def test_no_head_probe_for_ordinary_path(self):
        page = _page("https://example.gov/", '<a href="/about/staff">Staff</a>')
        mock = MagicMock()
        detect_datasets([page], http_client=mock)
        mock.head.assert_not_called()

    def test_attachment_without_extension_url_included_format_excluded(self):
        page = _page("https://example.gov/", '<a href="/download/data">Export</a>')
        client = self._client_with_cd("attachment")
        found, urls, fmts = detect_datasets([page], http_client=client)
        assert found
        assert any("download/data" in u for u in urls)
        assert fmts == []

    def test_no_content_disposition_download_path_not_included(self):
        page = _page("https://example.gov/", '<a href="/download/page">DL</a>')
        mock = MagicMock()
        mock.head.return_value = _make_head_response(200)  # no CD header
        found, _, _ = detect_datasets([page], http_client=mock)
        assert not found

    @pytest.mark.parametrize("path,label", [
        ("/dl/budget", "dl"),
        ("/files/report", "files"),
        ("/file/data", "file"),
        ("/serve/dataset", "serve"),
        ("/attachment/doc", "attachment"),
        ("/attachments/doc", "attachments"),
        ("/document/brief", "document"),
        ("/documents/annual-report", "documents"),
        ("/downloads/archive", "downloads"),
        ("/exports/full", "exports"),
    ])
    def test_head_probe_fires_for_expanded_paths(self, path, label):
        page = _page("https://example.gov/", f'<a href="{path}">Get</a>')
        mock = MagicMock()
        mock.head.return_value = _make_head_response(200, f'attachment; filename="data.csv"')
        found, _, fmts = detect_datasets([page], http_client=mock)
        assert found, f"Expected dataset detected for path pattern '{label}'"
        assert "csv" in fmts


# ---------------------------------------------------------------------------
# HttpClient.head() (T-61)
# ---------------------------------------------------------------------------

class TestHttpClientHead:
    def test_head_method_issues_head_request(self):
        client = HttpClient(delay=0)
        mock_resp = httpx.Response(200, headers=httpx.Headers({"content-disposition": "attachment"}))
        mock_resp.request = httpx.Request("HEAD", "https://example.gov/file")
        client._client.head = MagicMock(return_value=mock_resp)
        resp = client.head("https://example.gov/file")
        client._client.head.assert_called_once()
        assert resp.status_code == 200

    def test_head_respects_rate_limiting(self):
        import time
        client = HttpClient(delay=0.05)
        mock_resp = httpx.Response(200)
        mock_resp.request = httpx.Request("HEAD", "https://example.gov/")
        client._client.head = MagicMock(return_value=mock_resp)
        t0 = time.monotonic()
        client.head("https://example.gov/a")
        client.head("https://example.gov/b")
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.04  # rate limit respected
