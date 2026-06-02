"""Incremental CSV output writer (Phase 9, T-90–T-94)."""
import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

COLUMNS = [
    "url", "priority", "state", "active", "http_status", "final_url",
    "robots_allowed", "robots_status", "js_rendered", "relevance_score",
    "matched_keywords", "datasets_found", "dataset_urls", "dataset_formats",
    "crawl_depth_reached", "portal_platform", "portal_dataset_count",
    "portal_relevant_count", "top_dataset_urls", "error_notes",
]


class ReportWriter:
    """Incremental CSV writer for crawl results.

    Fresh run:
        with ReportWriter(output_dir) as w:
            seen = w.open()           # writes header; seen == set()
            w.append_row(result)

    Resume (--resume):
        with ReportWriter(output_dir) as w:
            seen = w.open(resume=True)  # reads existing file; no header written
            # skip URLs in seen, then call append_row for new URLs

    Delta run (--new-only): caller uses collect_seen_urls() before constructing writer.
    """

    COLUMNS = COLUMNS

    def __init__(self, output_dir: "Path | str"):
        self.output_dir = Path(output_dir)
        self.csv_path = self.output_dir / "results.csv"
        self._fh = None
        self._writer = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self, resume: bool = False) -> set:
        """Open the CSV for writing. Returns the set of already-written URLs.

        Fresh run: creates directory, writes header row, returns empty set.
        Resume: reads existing CSV for seen URLs, opens in append mode (no
        header), returns seen set. Falls back to fresh run if no CSV exists yet.
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        seen: set[str] = set()

        if resume and self.csv_path.exists():
            seen = _read_seen_urls(self.csv_path)
            self._fh = self.csv_path.open("a", newline="", encoding="utf-8")
            logger.info(
                "Resuming — %d URLs already written, appending to %s",
                len(seen), self.csv_path,
            )
        else:
            self._fh = self.csv_path.open("w", newline="", encoding="utf-8")
            self._writer = csv.DictWriter(
                self._fh, fieldnames=COLUMNS, extrasaction="ignore"
            )
            self._writer.writeheader()
            self._fh.flush()
            if resume:
                logger.info(
                    "Resume requested but no existing CSV found; starting fresh at %s",
                    self.csv_path,
                )
            else:
                logger.info("Fresh run — writing to %s", self.csv_path)

        if self._writer is None:
            self._writer = csv.DictWriter(
                self._fh, fieldnames=COLUMNS, extrasaction="ignore"
            )

        return seen

    def append_row(self, result: dict) -> None:
        """Serialize result dict to one CSV row and flush immediately."""
        if self._writer is None:
            raise RuntimeError("Call open() before append_row()")
        self._writer.writerow(_serialize(result))
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._writer = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ------------------------------------------------------------------
    # Class-level helpers for run modes
    # ------------------------------------------------------------------

    @staticmethod
    def collect_seen_urls(
        output_root: "Path | str",
        exclude_dir: "Path | str | None" = None,
    ) -> set:
        """Aggregate every URL from all dated subdirectories under output_root.

        Skips exclude_dir (typically the current run's directory).
        Returns an empty set when output_root does not exist.
        Used to implement --new-only delta-run mode.
        """
        output_root = Path(output_root)
        if not output_root.exists():
            return set()

        exclude = Path(exclude_dir).resolve() if exclude_dir else None
        seen: set[str] = set()

        for subdir in sorted(output_root.iterdir()):
            if not subdir.is_dir():
                continue
            if exclude is not None and subdir.resolve() == exclude:
                continue
            csv_path = subdir / "results.csv"
            if not csv_path.exists():
                continue
            batch = _read_seen_urls(csv_path)
            seen.update(batch)
            logger.debug("Collected %d URLs from %s", len(batch), csv_path)

        return seen

    @staticmethod
    def make_error_row(url: str, priority: bool, error: str) -> dict:
        """Return a minimal result dict for a URL that raised an uncaught exception.

        Sets active=False, http_status=0, relevance_score=0; all list fields empty.
        The caller writes this via append_row() and continues to the next URL.
        """
        return {
            "url": url,
            "priority": priority,
            "state": "",
            "active": False,
            "http_status": 0,
            "final_url": "",
            "robots_allowed": None,
            "robots_status": "",
            "js_rendered": False,
            "relevance_score": 0,
            "matched_keywords": [],
            "datasets_found": False,
            "dataset_urls": [],
            "dataset_formats": [],
            "crawl_depth_reached": 0,
            "portal_platform": "",
            "portal_dataset_count": 0,
            "portal_relevant_count": 0,
            "top_dataset_urls": [],
            "error_notes": error,
        }


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _read_seen_urls(csv_path: Path) -> set:
    """Return the set of 'url' values from an existing results CSV."""
    seen: set[str] = set()
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            url = (row.get("url") or "").strip()
            if url:
                seen.add(url)
    return seen


_BOOL_COLUMNS = frozenset({
    "priority", "active", "js_rendered", "datasets_found",
})


def _serialize(result: dict) -> dict:
    """Convert a result dict to CSV-safe string values.

    Rules:
    - None  → "" (handles nullable robots_allowed)
    - bool  → "true" / "false"
    - list  → pipe-joined string
    - other → unchanged (str/int are written as-is by csv.DictWriter)

    Boolean columns missing from result default to "false" rather than "".
    """
    row: dict = {}
    for col in COLUMNS:
        if col in _BOOL_COLUMNS:
            val = result.get(col, False)
        else:
            val = result.get(col, "")
        if val is None:
            row[col] = ""
        elif isinstance(val, bool):
            row[col] = "true" if val else "false"
        elif isinstance(val, list):
            row[col] = "|".join(str(v) for v in val)
        else:
            row[col] = val
    return row
