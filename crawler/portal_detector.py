"""T-43/T-44: Two-pass open data portal detection (passive signals + active API probe)."""
import re
import logging

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# T-44: Passive signal constants — defined here, not inlined in logic
# -----------------------------------------------------------------------

SOCRATA_FOOTER_TEXTS = ("Powered by Socrata", "Powered by Tyler Data & Insights")
SOCRATA_HEADER_KEYS = ("x-socrata-requestid",)          # stored lower-case for comparison
SOCRATA_SCRIPT_DOMAINS = ("socrata.com", "tylertech.com")
SOCRATA_CSS_PREFIXES = ("socrata-", "soda-")
SOCRATA_META_NAMES = ("soda-host",)

CKAN_META_GENERATOR_RE = re.compile(
    r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']ckan',
    re.IGNORECASE,
)
CKAN_META_GENERATOR_REV_RE = re.compile(
    r'<meta[^>]+content=["\']ckan[^"\']*["\'][^>]+name=["\']generator["\']',
    re.IGNORECASE,
)
CKAN_BODY_CLASS_RE = re.compile(r'<body[^>]+class=["\'][^"\']*ckan-', re.IGNORECASE)
CKAN_HTML_ID_RE = re.compile(r'<html[^>]+id=["\'][^"\']*ckan-', re.IGNORECASE)
CKAN_JS_SNIPPET = "ckan.module("
CKAN_DATASET_LINK_RE = re.compile(r'href=["\'](?:https?://[^"\']*)?/dataset[/"\'?#]', re.IGNORECASE)

ARCGIS_DOMAINS = (".hub.arcgis.com", ".opendata.arcgis.com")
ARCGIS_COMPONENT_TAGS = ("hub-hero", "hub-gallery")
ARCGIS_COMPONENT_PREFIX = "arcgis-hub-"
ARCGIS_SCRIPT_DOMAINS = ("js.arcgis.com", "cdn.arcgis.com")
ARCGIS_OG_SITE_NAME_RE = re.compile(
    r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']ArcGIS Hub["\']',
    re.IGNORECASE,
)
ARCGIS_OG_SITE_NAME_REV_RE = re.compile(
    r'<meta[^>]+content=["\']ArcGIS Hub["\'][^>]+property=["\']og:site_name["\']',
    re.IGNORECASE,
)

# Active probe endpoints (appended to base_url)
_PROBE_PATHS = {
    "Socrata": "/api/catalog/v1?limit=1",
    "CKAN": "/api/3/action/site_read",
    "ArcGIS Hub": "/api/v3/datasets?page[size]=1",
}

_PROBE_SUCCESS = {
    "Socrata": lambda d: isinstance(d.get("results"), list),
    "CKAN": lambda d: d.get("success") is True,
    "ArcGIS Hub": lambda d: isinstance(d.get("data"), list),
}


class PortalDetector:
    """T-43: Identifies whether a URL belongs to a known open data platform.

    detect() runs a two-pass strategy:
      Pass 1 — passive: scan already-fetched HTML and response headers.
      Pass 2 — active probe: fire one GET per candidate platform if Pass 1
               is inconclusive (zero or multiple candidates).
    """

    def __init__(self, http_client) -> None:
        self._client = http_client

    def detect(
        self, html: str, headers: dict, base_url: str
    ) -> tuple[str | None, str]:
        """Returns (platform, method).

        platform: "Socrata", "CKAN", "ArcGIS Hub", or None
        method:   "passive", "probe", or "none"
        """
        candidates = self._passive_detect(html, headers, base_url)

        if len(candidates) == 1:
            logger.debug("Portal detected passively: %s for %s", candidates[0], base_url)
            return candidates[0], "passive"

        if not candidates:
            return None, "none"

        # Multiple candidates — active probe decides
        logger.debug(
            "Multiple portal candidates %s for %s; firing active probe",
            candidates,
            base_url,
        )
        platform = self._active_probe(candidates, base_url)
        if platform:
            logger.debug("Active probe confirmed: %s for %s", platform, base_url)
            return platform, "probe"
        return None, "none"

    # ------------------------------------------------------------------
    # Pass 1: passive detection
    # ------------------------------------------------------------------

    def _passive_detect(
        self, html: str, headers: dict, base_url: str
    ) -> list[str]:
        headers_lc = {k.lower(): v for k, v in headers.items()}
        found: list[str] = []
        if self._check_socrata(html, headers_lc):
            found.append("Socrata")
        if self._check_ckan(html):
            found.append("CKAN")
        if self._check_arcgis(html, base_url):
            found.append("ArcGIS Hub")
        return found

    def _check_socrata(self, html: str, headers_lc: dict) -> bool:
        import html as html_lib
        for key in SOCRATA_HEADER_KEYS:
            if key in headers_lc:
                return True
        # Unescape HTML entities so "&amp;" matches "&" in footer text search
        html_lc = html_lib.unescape(html).lower()
        for text in SOCRATA_FOOTER_TEXTS:
            if text.lower() in html_lc:
                return True
        for domain in SOCRATA_SCRIPT_DOMAINS:
            if domain in html_lc:
                return True
        for prefix in SOCRATA_CSS_PREFIXES:
            if f'class="{prefix}' in html_lc or f"class='{prefix}" in html_lc:
                return True
        for name in SOCRATA_META_NAMES:
            if f'name="{name}"' in html_lc or f"name='{name}'" in html_lc:
                return True
        return False

    def _check_ckan(self, html: str) -> bool:
        if CKAN_META_GENERATOR_RE.search(html):
            return True
        if CKAN_META_GENERATOR_REV_RE.search(html):
            return True
        if CKAN_BODY_CLASS_RE.search(html):
            return True
        if CKAN_HTML_ID_RE.search(html):
            return True
        if CKAN_JS_SNIPPET in html:
            return True
        if CKAN_DATASET_LINK_RE.search(html):
            return True
        return False

    def _check_arcgis(self, html: str, base_url: str) -> bool:
        for domain in ARCGIS_DOMAINS:
            if domain in base_url.lower():
                return True
        html_lc = html.lower()
        for tag in ARCGIS_COMPONENT_TAGS:
            if f"<{tag}" in html_lc:
                return True
        if f"<{ARCGIS_COMPONENT_PREFIX}" in html_lc:
            return True
        for domain in ARCGIS_SCRIPT_DOMAINS:
            if domain in html_lc:
                return True
        if ARCGIS_OG_SITE_NAME_RE.search(html):
            return True
        if ARCGIS_OG_SITE_NAME_REV_RE.search(html):
            return True
        return False

    # ------------------------------------------------------------------
    # Pass 2: active probe
    # ------------------------------------------------------------------

    def _active_probe(self, candidates: list[str], base_url: str) -> str | None:
        base = base_url.rstrip("/")
        for platform in candidates:
            probe_url = base + _PROBE_PATHS[platform]
            try:
                resp = self._client.get(probe_url)
                if resp.status_code == 200:
                    data = resp.json()
                    if _PROBE_SUCCESS[platform](data):
                        return platform
            except Exception as exc:
                logger.debug(
                    "Active probe failed for %s at %s: %s", platform, probe_url, exc
                )
        return None
