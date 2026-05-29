"""T-46: Socrata / Tyler Technologies open data portal adapter."""
import logging
from portals import score_metadata

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100


class SocrataAdapter:
    """Enumerate all datasets from a Socrata/Tyler portal and score them."""

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
        offset = 0
        while True:
            url = f"{self._base}/api/catalog/v1?limit={_PAGE_SIZE}&offset={offset}"
            try:
                resp = self._client.get(url)
                if resp.status_code != 200:
                    logger.warning(
                        "Socrata catalog returned HTTP %d at offset %d for %s",
                        resp.status_code, offset, self._base,
                    )
                    break
                data = resp.json()
                page = data.get("results", [])
                datasets.extend(page)
                if len(page) < _PAGE_SIZE:
                    break
                offset += _PAGE_SIZE
            except Exception as exc:
                logger.warning(
                    "Socrata pagination error at offset %d for %s: %s",
                    offset, self._base, exc,
                )
                break
        return datasets

    def _aggregate(self, datasets: list[dict]) -> dict:
        scored: list[tuple[int, str]] = []
        all_matched: set[str] = set()

        for ds in datasets:
            resource = ds.get("resource", {})
            classification = ds.get("classification", {})

            title = resource.get("name", "") or ""
            description = resource.get("description", "") or ""
            tags = " ".join(classification.get("domain_tags", []))
            permalink = ds.get("permalink", "")

            s, matched = score_metadata(f"{title} {description} {tags}", self._keywords)
            all_matched.update(matched)
            scored.append((s, permalink))

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
