"""T-48: ArcGIS Hub open data portal adapter."""
import logging
from portals import score_metadata

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100


class ArcGISHubAdapter:
    """Enumerate all datasets from an ArcGIS Hub portal and score them."""

    def __init__(
        self, base_url: str, effective_keywords: frozenset, http_client
    ) -> None:
        self._base = base_url.rstrip("/")
        self._keywords = effective_keywords
        self._client = http_client

    def run(self) -> dict:
        datasets = self._fetch_all()
        return self._aggregate(datasets)

    def _fetch_all(self) -> list[dict]:
        datasets: list[dict] = []
        page = 1
        while True:
            url = (
                f"{self._base}/api/v3/datasets"
                f"?page[size]={_PAGE_SIZE}&page[number]={page}"
            )
            try:
                resp = self._client.get(url)
                if resp.status_code != 200:
                    logger.warning(
                        "ArcGIS Hub datasets returned HTTP %d at page %d for %s",
                        resp.status_code, page, self._base,
                    )
                    break
                data = resp.json()
                page_data = data.get("data", [])
                datasets.extend(page_data)
                # Pagination: presence of a "next" link in meta signals more pages
                meta = data.get("meta", {}) or {}
                if not meta.get("next"):
                    break
                page += 1
            except Exception as exc:
                logger.warning(
                    "ArcGIS Hub pagination error at page %d for %s: %s",
                    page, self._base, exc,
                )
                break
        return datasets

    def _aggregate(self, datasets: list[dict]) -> dict:
        scored: list[tuple[int, str]] = []
        all_matched: set[str] = set()

        for ds in datasets:
            attrs = ds.get("attributes", {}) or {}
            name = attrs.get("name", "") or ""
            description = attrs.get("description", "") or ""
            tags = " ".join(attrs.get("tags") or [])
            access = attrs.get("access", {}) or {}
            urls = access.get("urls", {}) or {}
            ds_url = urls.get("download", "") or ""

            s, matched = score_metadata(f"{name} {description} {tags}", self._keywords)
            all_matched.update(matched)
            scored.append((s, ds_url))

        scored.sort(key=lambda x: x[0], reverse=True)
        relevant = [u for s, u in scored if s > 0]
        top_urls = [u for _, u in scored[:10]]

        return {
            "portal_dataset_count": len(datasets),
            "portal_relevant_count": len(relevant),
            "top_dataset_urls": top_urls,
            "matched_keywords": sorted(all_matched),
            "relevance_score": scored[0][0] if scored else 0,
        }
