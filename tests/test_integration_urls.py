"""Integration tests against a small diverse subset of real government URLs.

These tests make live network requests and are skipped by default.
Run with:  RUN_INTEGRATION_TESTS=1 python -m pytest tests/test_integration_urls.py -v

Covered URL types:
  1. https://michigan.gov         — plain state .gov page  (expect state=MI)
  2. https://data.cityofchicago.org — Socrata portal       (expect Socrata detection)
  3. https://catalog.data.gov     — CKAN portal (federal)  (expect CKAN detection)
  4. https://census.gov           — federal agency site    (expect state=FEDERAL)
  5. https://opendata.dc.gov      — ArcGIS Hub             (expect ArcGIS Hub detection)
"""
import os
import pytest
from unittest.mock import MagicMock

import httpx

SKIP_REASON = "Set RUN_INTEGRATION_TESTS=1 to enable live URL tests"
SKIP = os.getenv("RUN_INTEGRATION_TESTS", "") not in ("1", "true", "yes")

# Shared HTTP client config for integration tests
_TIMEOUT = httpx.Timeout(10.0, read=30.0)
_HEADERS = {"User-Agent": "GovScraper/2.0 (contact: andihalim00@gmail.com)"}


def _get(url: str) -> httpx.Response:
    with httpx.Client(headers=_HEADERS, follow_redirects=True, timeout=_TIMEOUT) as client:
        return client.get(url)


@pytest.mark.skipif(SKIP, reason=SKIP_REASON)
class TestMichiganGov:
    """michigan.gov — plain state government homepage."""

    URL = "https://michigan.gov"

    def setup_method(self):
        self.resp = _get(self.URL)

    def test_page_is_active(self):
        assert self.resp.status_code == 200

    def test_page_has_substantial_content(self):
        from crawler.http_client import _visible_text
        visible = _visible_text(self.resp.text)
        assert len(visible) > 200

    def test_html_content_type(self):
        ct = self.resp.headers.get("content-type", "")
        assert "text/html" in ct


@pytest.mark.skipif(SKIP, reason=SKIP_REASON)
class TestCensusGovFederal:
    """census.gov — federal agency; should tag as FEDERAL."""

    URL = "https://census.gov"

    def setup_method(self):
        self.resp = _get(self.URL)

    def test_page_is_active(self):
        assert self.resp.status_code == 200

    def test_not_detected_as_portal(self):
        from crawler.portal_detector import PortalDetector
        det = PortalDetector(MagicMock())
        platform, _ = det.detect(self.resp.text, dict(self.resp.headers), self.URL)
        assert platform is None


@pytest.mark.skipif(SKIP, reason=SKIP_REASON)
class TestChicagoSocrataPortal:
    """data.cityofchicago.org — known Socrata open data portal."""

    URL = "https://data.cityofchicago.org"

    def setup_method(self):
        self.resp = _get(self.URL)

    def test_page_is_active(self):
        assert self.resp.status_code == 200

    def test_detected_as_socrata(self):
        from crawler.portal_detector import PortalDetector
        det = PortalDetector(MagicMock())
        platform, method = det.detect(self.resp.text, dict(self.resp.headers), self.URL)
        assert platform == "Socrata"

    def test_catalog_api_reachable(self):
        resp = _get(f"{self.URL}/api/catalog/v1?limit=1")
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data

    def test_socrata_adapter_returns_datasets(self):
        from crawler.http_client import HttpClient
        from portals.socrata import SocrataAdapter
        with HttpClient(delay=0) as client:
            adapter = SocrataAdapter(self.URL, frozenset({"county", "budget", "district"}), client)
            result = adapter.run()
        assert result["portal_dataset_count"] > 0
        assert isinstance(result["portal_relevant_count"], int)
        assert isinstance(result["top_dataset_urls"], list)


@pytest.mark.skipif(SKIP, reason=SKIP_REASON)
class TestDemoCKAN:
    """demo.ckan.org — CKAN project's official public demo instance."""

    URL = "https://demo.ckan.org"

    def setup_method(self):
        self.resp = _get(self.URL)

    def test_page_is_active(self):
        assert self.resp.status_code == 200

    def test_detected_as_ckan(self):
        from crawler.portal_detector import PortalDetector
        det = PortalDetector(MagicMock())
        platform, method = det.detect(self.resp.text, dict(self.resp.headers), self.URL)
        assert platform == "CKAN"

    def test_ckan_status_show_api_reachable(self):
        resp = _get(f"{self.URL}/api/3/action/status_show")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("success")

    def test_ckan_adapter_returns_datasets(self):
        from crawler.http_client import HttpClient
        from portals.ckan import CKANAdapter
        with HttpClient(delay=0) as client:
            adapter = CKANAdapter(self.URL, frozenset({"country", "population", "data"}), client)
            result = adapter.run()
        assert result["portal_dataset_count"] > 0


@pytest.mark.skipif(SKIP, reason=SKIP_REASON)
class TestDCOpenDataArcGISHub:
    """opendata.dc.gov — DC's ArcGIS Hub open data portal."""

    URL = "https://opendata.dc.gov"

    def setup_method(self):
        self.resp = _get(self.URL)

    def test_page_is_active(self):
        assert self.resp.status_code == 200

    def test_detected_as_arcgis_hub(self):
        from crawler.portal_detector import PortalDetector
        det = PortalDetector(MagicMock())
        final_url = str(self.resp.url)
        platform, method = det.detect(self.resp.text, dict(self.resp.headers), final_url)
        assert platform == "ArcGIS Hub"

