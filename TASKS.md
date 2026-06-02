# TASKS.md — gov-scraper-v2 MVP Build Plan

Tasks are ordered by dependency. Each section is a logical build phase; tasks within a phase can largely be parallelized unless noted.

---

## Phase 0 — Project Scaffolding

- [x] **T-00** Create `requirements.txt` with pinned dependencies: `httpx`, `playwright`, `beautifulsoup4`, `lxml`, `tldextract`, `urllib3`, `unicodedata2`
- [x] **T-01** Add `output/` to `.gitignore` (prevent run artifacts from being committed)
- [x] **T-02** Create empty `__init__.py` files in `crawler/`, `scorer/`, `reporter/`, and `setup/` to make them importable packages
- [x] **T-03** Add `playwright install chromium` step to project setup documentation (Playwright requires a browser binary download)

---

## Phase 1 — One-Time Setup Script

> Produces `config/state_definitions.json`, which must exist before the scorer can run.

- [x] **T-10** `setup/generate_state_definitions.py` — CLI entrypoint accepting `--llm gemini|ollama` and `--pdf config/2022ISD.pdf`
- [x] **T-11** PDF text extraction: use `pdfplumber` or `pypdf` to extract text page-by-page from `config/2022ISD.pdf`; split into per-state sections by detecting state header patterns
- [x] **T-12** Gemini backend: prompt Gemini 2.5 Flash (free tier) with each state section; parse response to extract `census_terms` list and `notes` string
- [x] **T-13** Ollama backend: same prompt structure as T-12, directed at local Ollama REST API (`http://localhost:11434`)
- [x] **T-14** Output writer: merge per-state results into `{ "XX": { "census_terms": [...], "notes": "..." } }` schema and write to `config/state_definitions.json`
- [x] **T-15** Guard: if `config/state_definitions.json` already exists, prompt user before overwriting (or require `--force`)

---

## Phase 2 — HTTP Client and robots.txt

> Low-level network layer. All other crawler components depend on this.

- [x] **T-20** `crawler/http_client.py` — `httpx.Client` wrapper with:
  - `User-Agent: GovScraper/2.0 (contact: andihalim00@gmail.com)` on every request
  - Per-domain rate limiting: enforce minimum 2-second gap between consecutive requests to the same registered domain
  - Automatic redirect following (httpx default)
  - Retry logic: up to 3 retries on HTTP 429 or 503 with exponential backoff (1 s, 2 s, 4 s)
  - Configurable `--delay` override passed in at construction time
  - Network-level timeout (10 s connect, 30 s read)

- [x] **T-21** `crawler/robots.py` — `RobotsChecker` class:
  - Fetch and parse `robots.txt` for a domain before the first request
  - Cache parsed result per registered domain for the lifetime of the run
  - Return `(allowed: bool, status: str)` where `status` is `"allowed"`, `"disallowed"`, or `"unavailable"`
  - Fail-open: if fetch fails for any reason, log a warning and return `(True, "unavailable")`
  - Respect `Disallow` rules for `GovScraper` and the wildcard `*` agent

---

## Phase 3 — State Tagger

> Tags each URL with a two-letter state code, `FEDERAL`, or `NATIONAL` before scoring.

- [x] **T-30** `crawler/state_tagger.py` — `StateTagger` class implementing the six-priority resolution chain (PRD §8):
  1. `*.state.XX.us` TLD pattern → extract `XX`
  2. Two-letter subdomain before `.gov` (e.g., `sco.ca.gov` → `CA`, `auditor.mo.gov` → `MO`)
  3. Full state name in registered domain (e.g., `michigan.gov` → `MI`)
  4. Page content fallback: scan `<title>` and first `<h1>` for state name or abbreviation
  5. Domain in hardcoded federal list → `FEDERAL`
  6. Unresolved → `NATIONAL`
- [x] **T-31** Build the canonical state name↔abbreviation lookup (all 50 states + DC)
- [x] **T-32** Build the known federal domain list: `hud.gov`, `epa.gov`, `census.gov`, `usda.gov`, `faa.gov`, `usa.gov`, `data.gov` (extensible list)

---

## Phase 4 — Page Fetcher and JS Detection

> Retrieves rendered page content; Playwright fallback for JS-heavy pages.

- [x] **T-40** `crawler/http_client.py` (extend T-20) — `fetch_page(url)` method: issues GET, returns `(html: str, final_url: str, http_status: int, js_rendered: bool)`
- [x] **T-41** JS-heavy detection logic: flag a page as JS-heavy if any of:
  - Body contains `<div id="root">` or `<div id="app">` with fewer than 200 characters of visible text after tag stripping
  - `Content-Type` is not `text/html`
  - Stripped text length < 200 characters
- [x] **T-42** `crawler/playwright_client.py` — async Playwright wrapper:
  - Launch headless Chromium
  - Navigate to URL, wait for `networkidle`
  - Return rendered `document.body.innerHTML`
  - Shut down browser context after each URL (no persistent browser session across URLs)

---

## Phase 4B — Open Data Portal Detection

> Identifies whether a seed URL is a known open data platform and routes it to a platform-specific API adapter rather than the generic depth crawler. Runs after the initial page fetch (Phase 4) and before the depth crawler (Phase 5).

- [x] **T-43** `crawler/portal_detector.py` — `PortalDetector` class:
  - `detect(html: str, headers: dict, base_url: str) -> tuple[str | None, str]`
  - Returns `(platform, method)` where `platform` is `"Socrata"`, `"CKAN"`, `"ArcGIS Hub"`, or `None`; `method` is `"passive"`, `"probe"`, or `"none"`
  - Pass 1: scan HTML + response headers for per-platform passive signals (PRD FR-12)
  - Pass 2: if Pass 1 is inconclusive, fire a single GET probe per candidate platform using the rate-limited client from T-20; use probe endpoints from PRD FR-12 table
  - If passive signals match multiple platforms, active probe result takes precedence

- [x] **T-44** Passive signal constants: define per-platform HTML and header signatures as module-level constants in `portal_detector.py`; do not hardcode literal strings inline in detection logic

- [x] **T-45** `portals/` package scaffolding: `portals/__init__.py`

- [x] **T-46** `portals/socrata.py` — `SocrataAdapter(base_url, effective_keywords, http_client)`:
  - Paginate `GET /api/catalog/v1?limit=100&offset=N` until all datasets retrieved
  - Per dataset: extract `resource.name`, `resource.description`, `classification.domain_tags`, `resource.type`, `permalink`
  - Score each dataset's concatenated metadata (title + description + tags) using the same weighted matcher as T-71
  - Return the shared adapter contract dict (PRD §12)

- [x] **T-47** `portals/ckan.py` — `CKANAdapter(base_url, effective_keywords, http_client)`:
  - Paginate `GET /api/3/action/package_search?rows=100&start=N` to retrieve all metadata in batches
  - Per dataset: extract `title`, `notes`, `tags[].name`, `resources[].format`, `resources[].url`
  - Score metadata; return adapter contract dict

- [x] **T-48** `portals/arcgis_hub.py` — `ArcGISHubAdapter(base_url, effective_keywords, http_client)`:
  - Paginate `GET /api/v3/datasets?page[size]=100&page[number]=N` until complete
  - Per dataset: extract `attributes.name`, `attributes.description`, `attributes.tags`, `attributes.access.urls.download`
  - Score metadata; return adapter contract dict

- [ ] **T-49** Portal routing in `crawler/orchestrator.py`: after initial page fetch, call `PortalDetector.detect()`; if platform is not `None`, call the matching adapter and skip the depth crawl; if `None`, proceed normally

---

## Phase 5 — Depth Crawler

> Follows internal links up to `--depth` hops to discover more pages and dataset links.

- [x] **T-50** `crawler/orchestrator.py` — `crawl_url(seed_url, depth)` function:
  - Fetch seed URL via T-40/T-42
  - Parse all `<a href>` links; filter to same registered domain only (use `tldextract`)
  - BFS up to `depth` hops; do not revisit already-seen URLs within the same seed's crawl
  - Apply per-domain rate limiting (T-20) between each hop
  - Return list of `(url, html, http_status, js_rendered)` tuples for all pages visited
- [x] **T-51** Skip external links during BFS (different registered domain = external)
- [x] **T-52** Track `crawl_depth_reached`: the deepest hop level successfully fetched (0 if seed itself failed)

---

## Phase 6 — Dataset Detector

> Scans collected page HTML for downloadable file links.

- [x] **T-60** `crawler/dataset_detector.py` — `detect_datasets(pages: list[html])` function:
  - Scan all `<a href>` values across all crawled pages
  - Detect by: href ends with `.csv`, `.xlsx`, `.xls`, `.json`, `.xml`, `.pdf`; or extension appears before a `?` query string; or `Content-Disposition: attachment` header (requires HEAD request on ambiguous URLs)
  - Return `(found: bool, urls: list[str], formats: list[str])` — formats deduplicated, PDFs tracked separately but included in `dataset_urls`
- [x] **T-61** HEAD request helper for `Content-Disposition` check — reuse the rate-limited client from T-20; do not download response body

---

## Phase 7 — Relevance Scorer

> Produces 0–100 Census relevance score using weighted keyword matching.

- [ ] **T-70** `scorer/keyword_loader.py`:
  - Load `config/keywords.csv` (single `Keywords` column) into a set
  - Load `config/state_definitions.json` and extract `census_terms` for a given state
  - `get_effective_keywords(state: str) -> frozenset[str]`: returns union of base keywords + state terms; for `FEDERAL`/`NATIONAL`, returns base keywords only
- [ ] **T-71** `scorer/scorer.py` — `score_page(pages: list[html], effective_keywords: frozenset, state: str) -> dict`:
  - For each page, parse with BeautifulSoup; extract title/H1–H3 text, body text, and anchor text as separate text pools
  - Apply Unicode NFC normalization and diacritic stripping before matching
  - Whole-word boundary match (`\b`) case-insensitively
  - Weighted scoring per PRD §7: heading hits × 0.50, body hits × 0.35, anchor hits × 0.15 (per keyword, additive across tiers, max 1.00 pt per keyword)
  - `normalization_factor = len(effective_keywords)`
  - `relevance_score = min(100, round(weighted_hits / normalization_factor * 100))`
  - Return `{ "relevance_score": int, "matched_keywords": list[str] }`
- [ ] **T-72** URL and domain text must be explicitly excluded from all three scoring text pools

---

## Phase 8 — Input Ingestion and Priority Queue

> Reads `config/urls.csv` and prepares the ordered processing queue.

- [ ] **T-80** `crawler/orchestrator.py` — `load_urls(csv_path: str) -> list[dict]`:
  - Read `WEB_ADDRESS` and `PRIORITY_RESOURCE` columns only; ignore all others including `RESOURCE_NAME`
  - Skip rows where `WEB_ADDRESS` is empty/blank; log each skip
  - Validate each URL is parseable (use `urllib.parse.urlparse`); log and skip malformed entries
  - Deduplicate by normalized URL (lowercase scheme+host+path); log duplicates
- [ ] **T-81** Sort queue: `PRIORITY_RESOURCE == "YES"` (case-insensitive) rows first, preserving relative order within each group; set `priority: true/false` on each row

---

## Phase 9 — Output Writer and Run Modes

> Incremental CSV output + crash recovery and delta run support.

- [ ] **T-90** `reporter/writer.py` — `ReportWriter` class:
  - Create `output/<YYYY-MM-DD>/` directory at run start
  - Write CSV header once (see PRD §12 column list) on fresh run; never re-write on resume
  - `append_row(result: dict)`: serialize one result dict to CSV and flush immediately after each URL
- [ ] **T-91** `--resume` mode: at startup, read the current run's output CSV (same date directory); collect all `url` values already present; skip those in the queue
- [ ] **T-92** `--new-only` mode: at startup, scan all subdirectories under `output/` (excluding the current run's); collect all `url` values ever seen; skip those in the queue
- [ ] **T-93** Enforce mutual exclusivity of `--resume` and `--new-only` at CLI parse time (exit with error if both are passed)
- [ ] **T-94** Error handling: wrap each URL's full pipeline execution in a try/except; on any uncaught exception, write a result row with `active: false`, `relevance_score: 0`, and the exception message in `error_notes`; continue to the next URL

---

## Phase 10 — Entrypoint (`run.py`)

> Top-level CLI that wires all components together.

- [ ] **T-100** `run.py` — `argparse` CLI with flags matching PRD §13:
  - `--depth` (default 2)
  - `--delay` (default 2.0)
  - `--output` (default `output/<YYYY-MM-DD>/`)
  - `--resume`
  - `--new-only`
  - `--input` (default `config/urls.csv`)
- [ ] **T-101** Startup check: if `config/state_definitions.json` does not exist, print a clear error message pointing to the setup script and exit
- [ ] **T-102** Main loop: for each URL in the ordered queue:
  1. Check `RobotsChecker` → record `robots_allowed`, `robots_status`
  2. Fetch seed URL → record `active`, `http_status`, `final_url`, `js_rendered`
  3. If inactive (non-200 or network error), write row immediately and continue
  4. Run `PortalDetector` → record `portal_platform`
  5. If portal detected: call the matching adapter → record `portal_dataset_count`, `portal_relevant_count`, `top_dataset_urls`, `relevance_score`, `matched_keywords`; skip to step 10
  6. Run `StateTagger` → record `state`
  7. Run depth crawler → collect all visited pages
  8. Run `DatasetDetector` → record `datasets_found`, `dataset_urls`, `dataset_formats`
  9. Run `Scorer` → record `relevance_score`, `matched_keywords`
  10. Record `crawl_depth_reached`
  11. `ReportWriter.append_row(result)`
- [ ] **T-103** Logging: use Python `logging` module; emit `INFO` for each URL processed, `WARNING` for robots unavailable and duplicates/skips, `ERROR` for per-URL failures

---

## Phase 11 — Validation and Smoke Testing

> Manual and scripted checks that the pipeline produces correct output before handing off.

- [ ] **T-110** Run the setup script against `config/2022ISD.pdf` with either backend; verify `config/state_definitions.json` contains entries for all 50 states + DC with non-empty `census_terms`
- [ ] **T-111** Smoke test: run `python run.py --input config/urls.csv --depth 1` against a 5-URL subset; verify `output/<date>/results.csv` has correct columns and one row per URL
- [ ] **T-112** Verify state tagging: confirm a `*.state.tx.us` URL → `TX`, a `sco.ca.gov` URL → `CA`, a `census.gov` URL → `FEDERAL`, and an untaggable URL → `NATIONAL`
- [ ] **T-113** Verify scorer: a page with Census keywords in its `<h1>` should score higher than an identical page with keywords only in body text
- [ ] **T-114** Verify dataset detection: a page containing `<a href="data.csv">` should produce `datasets_found: true`, `dataset_formats: csv`
- [ ] **T-115** Verify `--resume`: kill the process mid-run; restart with `--resume`; confirm no URL is processed twice in the output CSV
- [ ] **T-116** Verify `--new-only`: add a new URL to `config/urls.csv`; run with `--new-only`; confirm only the new URL is processed
- [ ] **T-117** Verify portal detection: confirm a known Socrata portal URL produces `portal_platform: Socrata` and non-zero `portal_dataset_count`; confirm a CKAN portal produces `portal_platform: CKAN`; confirm a non-portal URL produces an empty `portal_platform`

---

## Output column reference (PRD §12)

| Column | Notes |
|---|---|
| `url` | Seed URL |
| `priority` | boolean |
| `state` | Two-letter code, `FEDERAL`, or `NATIONAL` |
| `active` | boolean |
| `http_status` | integer (0 for network failure) |
| `final_url` | Resolved URL after redirects |
| `robots_allowed` | boolean or null |
| `robots_status` | `allowed` / `disallowed` / `unavailable` |
| `js_rendered` | boolean |
| `relevance_score` | 0–100 |
| `matched_keywords` | pipe-separated |
| `datasets_found` | boolean |
| `dataset_urls` | pipe-separated |
| `dataset_formats` | pipe-separated, deduplicated |
| `crawl_depth_reached` | 0–2 |
| `portal_platform` | `Socrata`, `CKAN`, `ArcGIS Hub`, or empty |
| `portal_dataset_count` | integer; 0 for non-portal URLs |
| `portal_relevant_count` | integer; 0 for non-portal URLs |
| `top_dataset_urls` | pipe-separated; empty for non-portal URLs |
| `error_notes` | empty string if no errors |
