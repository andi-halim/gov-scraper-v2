"""T-47: CKAN open data portal adapter."""
import logging
from portals import score_metadata

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100


class CKANAdapter:
    """Enumerate all datasets from a CKAN portal and score them."""

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
        start = 0
        while True:
            url = (
                f"{self._base}/api/3/action/package_search"
                f"?rows={_PAGE_SIZE}&start={start}"
            )
            try:
                resp = self._client.get(url)
                if resp.status_code != 200:
                    logger.warning(
                        "CKAN package_search returned HTTP %d at start %d for %s",
                        resp.status_code, start, self._base,
                    )
                    break
                data = resp.json()
                if not data.get("success"):
                    logger.warning("CKAN package_search success=false for %s", self._base)
                    break
                page = data.get("result", {}).get("results", [])
                datasets.extend(page)
                if len(page) < _PAGE_SIZE:
                    break
                start += _PAGE_SIZE
            except Exception as exc:
                logger.warning(
                    "CKAN pagination error at start %d for %s: %s",
                    start, self._base, exc,
                )
                break
        return datasets

    def _aggregate(self, datasets: list[dict]) -> dict:
        scored: list[tuple[int, str]] = []
        all_matched: set[str] = set()

        for ds in datasets:
            title = ds.get("title", "") or ""
            notes = ds.get("notes", "") or ""
            tags = " ".join(t.get("name", "") for t in (ds.get("tags") or []))
            resources = ds.get("resources") or []
            ds_url = resources[0].get("url", "") if resources else ""

            s, matched = score_metadata(f"{title} {notes} {tags}", self._keywords)
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
