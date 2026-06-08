#!/usr/bin/env python3
"""gov-scraper-v2 entrypoint (Phase 10, T-100–T-103)."""
import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from crawler.dataset_detector import detect_datasets
from crawler.http_client import HttpClient
from crawler.orchestrator import crawl_url, load_urls
from crawler.portal_detector import PortalDetector
from crawler.robots import RobotsChecker
from portals.arcgis_hub import ArcGISHubAdapter
from portals.ckan import CKANAdapter
from portals.socrata import SocrataAdapter
from reporter.writer import ReportWriter
from scorer.keyword_loader import get_effective_keywords
from scorer.scorer import score_page

logger = logging.getLogger(__name__)

_PORTAL_ADAPTERS = {
    "Socrata": SocrataAdapter,
    "CKAN": CKANAdapter,
    "ArcGIS Hub": ArcGISHubAdapter,
}

_DEFAULT_INPUT = "config/urls.csv"
_DEFAULT_OUTPUT_ROOT = "output"


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="gov-scraper-v2: crawl and assess US government URLs for Census relevance",
    )
    p.add_argument(
        "--depth", type=int, default=2,
        help="Maximum crawl depth from seed URL (default: 2)",
    )
    p.add_argument(
        "--delay", type=float, default=2.0,
        help="Minimum seconds between requests to the same domain (default: 2.0)",
    )
    p.add_argument(
        "--output", type=str, default=None,
        help="Output directory path (default: output/<YYYY-MM-DD>/)",
    )
    p.add_argument(
        "--resume", action="store_true",
        help="Resume an interrupted run by skipping URLs already in the current output CSV",
    )
    p.add_argument(
        "--new-only", action="store_true", dest="new_only",
        help="Delta run: skip URLs present in any previous run's output CSV",
    )
    p.add_argument(
        "--input", type=str, default=_DEFAULT_INPUT,
        help=f"Path to input URL CSV (default: {_DEFAULT_INPUT})",
    )
    return p


def main(argv=None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    # T-93: enforce mutual exclusivity
    if args.resume and args.new_only:
        parser.error("--resume and --new-only are mutually exclusive; use one or neither")

    # T-103: configure logging once
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )

    # T-101: startup guard — scorer won't work without state definitions
    state_defs_path = Path("config/state_definitions.json")
    if not state_defs_path.exists():
        print(
            "ERROR: config/state_definitions.json not found.\n"
            "Run the one-time setup script first:\n"
            "  python setup/generate_state_definitions.py --llm gemini\n"
            "  python setup/generate_state_definitions.py --llm ollama",
            file=sys.stderr,
        )
        return 1

    run_date = date.today().isoformat()
    output_dir = Path(args.output) if args.output else Path(_DEFAULT_OUTPUT_ROOT) / run_date

    logger.info("Loading URLs from %s", args.input)
    url_entries = load_urls(args.input)
    logger.info("%d unique URLs loaded", len(url_entries))

    # --new-only: collect the full set of URLs processed in previous runs
    skip_urls: set[str] = set()
    if args.new_only:
        skip_urls = ReportWriter.collect_seen_urls(
            Path(_DEFAULT_OUTPUT_ROOT), exclude_dir=output_dir
        )
        logger.info(
            "--new-only: %d previously processed URLs will be skipped", len(skip_urls)
        )

    processed = 0
    skipped = 0

    with HttpClient(delay=args.delay) as http_client:
        robots_checker = RobotsChecker(http_client)
        portal_detector = PortalDetector(http_client)

        with ReportWriter(output_dir) as writer:
            seen_in_run: set[str] = writer.open(resume=args.resume)

            for entry in url_entries:
                url: str = entry["url"]
                priority: bool = entry["priority"]
                state: str = entry["state"]

                if url in seen_in_run or url in skip_urls:
                    logger.info("Skipping %s (already processed)", url)
                    skipped += 1
                    continue

                logger.info("Processing: %s", url)

                # T-94: per-URL try/except — failures are logged and written, never crash the run
                try:
                    result = _process_url(
                        url=url,
                        priority=priority,
                        state=state,
                        http_client=http_client,
                        robots_checker=robots_checker,
                        portal_detector=portal_detector,
                        depth=args.depth,
                    )
                except Exception as exc:
                    logger.error("Unhandled error for %s: %s", url, exc, exc_info=True)
                    result = ReportWriter.make_error_row(url, priority, str(exc))

                writer.append_row(result)
                processed += 1

    logger.info(
        "Run complete — %d processed, %d skipped. Output: %s",
        processed,
        skipped,
        output_dir / "results.csv",
    )
    return 0


def _process_url(
    url: str,
    priority: bool,
    state: str,
    http_client: HttpClient,
    robots_checker: RobotsChecker,
    portal_detector: PortalDetector,
    depth: int,
) -> dict:
    """Execute the full per-URL pipeline.

    Returns a fully-populated result dict ready for ReportWriter.append_row().
    Raises on unrecoverable errors — the caller (main loop) wraps this in try/except.
    """
    result: dict = {
        "url": url,
        "priority": priority,
        "state": state,
        "active": False,
        "http_status": 0,
        "final_url": url,
        "robots_allowed": None,
        "robots_status": "unavailable",
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
        "error_notes": "",
    }

    # Step 1: robots.txt check
    robots_allowed, robots_status = robots_checker.is_allowed(url)
    result["robots_allowed"] = robots_allowed
    result["robots_status"] = robots_status

    if not robots_allowed:
        logger.warning("robots.txt disallows: %s", url)
        result["error_notes"] = "Disallowed by robots.txt"
        return result

    # Step 2: fetch seed URL
    try:
        html, final_url, http_status, js_rendered = http_client.fetch_page(url)
    except Exception as exc:
        logger.warning("Network error fetching %s: %s", url, exc)
        result["error_notes"] = f"Network error: {exc}"
        return result

    result["final_url"] = final_url
    result["http_status"] = http_status
    result["js_rendered"] = js_rendered
    result["active"] = http_status == 200

    # Step 3: inactive URLs — write minimal row and continue
    if not result["active"]:
        return result

    # Step 4: resolve effective keywords from the manually-tagged state
    effective_keywords = get_effective_keywords(state)

    # Step 5 (T-49): portal detection and routing
    # last_response_headers is set by fetch_page(); use empty dict if not available (e.g. in tests)
    headers = getattr(http_client, "last_response_headers", {})
    platform, _method = portal_detector.detect(html, headers, final_url)
    result["portal_platform"] = platform or ""

    if platform:
        logger.info("Portal detected (%s) for %s — using API adapter", platform, url)
        adapter_cls = _PORTAL_ADAPTERS[platform]
        adapter = adapter_cls(final_url, effective_keywords, http_client)
        portal_result = adapter.run()
        result["portal_dataset_count"] = portal_result.get("portal_dataset_count", 0)
        result["portal_relevant_count"] = portal_result.get("portal_relevant_count", 0)
        result["top_dataset_urls"] = portal_result.get("top_dataset_urls", [])
        result["matched_keywords"] = portal_result.get("matched_keywords", [])
        result["relevance_score"] = portal_result.get("relevance_score", 0)
        return result

    # Step 6: depth crawl (no portal detected)
    pages, crawl_depth_reached = crawl_url(url, http_client, depth=depth)
    result["crawl_depth_reached"] = crawl_depth_reached

    # Step 7: dataset detection
    found, dataset_urls, dataset_formats = detect_datasets(pages, http_client)
    result["datasets_found"] = found
    result["dataset_urls"] = dataset_urls
    result["dataset_formats"] = dataset_formats

    # Step 8: relevance scoring
    score_result = score_page(pages, effective_keywords)
    result["relevance_score"] = score_result["relevance_score"]
    result["matched_keywords"] = score_result["matched_keywords"]

    return result


if __name__ == "__main__":
    sys.exit(main())
