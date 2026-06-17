"""Incremental CSV output writer (Phase 9, T-90–T-94)."""
import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

COLUMNS = [
    "url", "priority", "state", "active", "http_status", "final_url",
    "robots_allowed", "robots_status", "js_rendered", "relevance_score",
    "matched_keywords", "datasets_found", "dataset_urls", "dataset_formats",
    "dataset_urls_total", "dataset_urls_omitted",
    "crawl_depth_reached", "portal_platform", "error_notes",
]

# Normalized companion table: one row per detected dataset URL (the complete,
# uncapped set). results.csv keeps a char-capped `dataset_urls` cell; this file holds
# everything, keyed back to results.csv by `url`. One URL per row → no cell-size limit.
COMPANION_FILENAME = "dataset_urls.csv"
COMPANION_COLUMNS = ["url", "dataset_url", "format", "rank"]


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
        self.companion_path = self.output_dir / COMPANION_FILENAME
        self._fh = None
        self._writer = None
        self._companion_fh = None
        self._companion_writer = None

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

        self._open_companion(resume=resume)
        return seen

    def _open_companion(self, resume: bool) -> None:
        """Open the normalized companion CSV in the same mode as the main file.

        Fresh run (or resume with no existing companion): write the header.
        Resume with an existing companion: append, no header.
        """
        if resume and self.companion_path.exists():
            self._companion_fh = self.companion_path.open("a", newline="", encoding="utf-8")
            self._companion_writer = csv.DictWriter(
                self._companion_fh, fieldnames=COMPANION_COLUMNS, extrasaction="ignore"
            )
        else:
            self._companion_fh = self.companion_path.open("w", newline="", encoding="utf-8")
            self._companion_writer = csv.DictWriter(
                self._companion_fh, fieldnames=COMPANION_COLUMNS, extrasaction="ignore"
            )
            self._companion_writer.writeheader()
            self._companion_fh.flush()

    def append_row(self, result: dict) -> None:
        """Serialize result dict to one CSV row, then expand its full dataset list.

        The main results.csv row is written and flushed first (preserving crash-safe
        resume semantics keyed on results.csv), then every URL in `dataset_urls_all`
        is appended to the companion CSV.
        """
        if self._writer is None:
            raise RuntimeError("Call open() before append_row()")
        self._writer.writerow(_serialize(result))
        self._fh.flush()
        self._append_companion_rows(result)

    def _append_companion_rows(self, result: dict) -> None:
        """Write one companion row per detected dataset URL for this result.

        `dataset_links` is the full ranked list of (url, format) tuples from
        detect_datasets(); `format` carries the per-URL format, including formats
        resolved only via a Content-Disposition HEAD probe (blank when unknown).
        """
        if self._companion_writer is None:
            return
        seed_url = result.get("url", "")
        dataset_links = result.get("dataset_links") or []
        if not dataset_links:
            return
        for rank, (dataset_url, fmt) in enumerate(dataset_links, start=1):
            self._companion_writer.writerow({
                "url": seed_url,
                "dataset_url": dataset_url,
                "format": fmt,
                "rank": rank,
            })
        self._companion_fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._writer = None
        if self._companion_fh is not None:
            self._companion_fh.close()
            self._companion_fh = None
            self._companion_writer = None

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
            "relevance_score": None,
            "matched_keywords": [],
            "datasets_found": False,
            "dataset_urls": [],
            "dataset_formats": [],
            "dataset_urls_total": 0,
            "dataset_urls_omitted": 0,
            "dataset_links": [],
            "crawl_depth_reached": 0,
            "portal_platform": "",
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
