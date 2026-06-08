## Implementation status

| Phase | Description | Status |
|---|---|---|
| 0 | Scaffolding (`requirements.txt`, `__init__.py` files) | Done |
| 1 | `setup/generate_state_definitions.py` + `config/state_definitions.json` | Done |
| 2 | `crawler/http_client.py`, `crawler/robots.py` | Done |
| 3 | State tagging via `STATE` column in `urls.csv` (replaces auto-detection; `crawler/state_tagger.py` removed) | Done |
| 4 | Page fetcher + JS detection + CDN bot-bypass + `crawler/playwright_client.py` | Done |
| 4B | Open data portal detection (`crawler/portal_detector.py`, `portals/`) | Done |
| 5 | Depth crawler with `prefetched_seed` optimisation (`crawler/orchestrator.py`) | Done |
| 6 | Dataset detector (`crawler/dataset_detector.py`) | Done |
| 7 | Relevance scorer (`scorer/keyword_loader.py`, `scorer/scorer.py`) | Done |
| 8 | Input ingestion + priority queue (extends `crawler/orchestrator.py`) | Done |
| 9 | Output writer + run modes (`reporter/writer.py`) | Done |
| 10 | `run.py` entrypoint | Done |

**Current state:** all phases complete. The full pipeline is wired in `run.py` and the test suite passes (392 unit tests, 18 integration tests skipped by default). Remaining open items are Phase 11 validation tasks T-110 and T-117 — T-110 requires completing the ISD setup script for all 51 states; T-117 requires a live integration run against known portal URLs.

### Phase 2 implementation notes

**`crawler/http_client.py` — `HttpClient`**
- `get(url)` enforces per-domain rate limiting (keyed on registered domain via `tldextract`), retries on HTTP 429/503 with 1 s/2 s/4 s backoff (up to 3 retries), and sets 10 s connect / 30 s read timeouts.
- Rate limit measures from request *start*, not response receipt — the gap between sends is at least `delay` seconds.
- Network errors (`ConnectError`, `TimeoutException`, etc.) propagate immediately; only 429/503 trigger retries.
- Supports the context manager protocol (`with HttpClient() as c:`).

**`crawler/robots.py` — `RobotsChecker`**
- Takes the shared `HttpClient` instance as a dependency so robots.txt fetches count against the domain's rate limit.
- Caches parsed `RobotFileParser` per netloc for the run lifetime. A failed or missing robots.txt is cached as `None` — no refetch on subsequent calls to the same host.
- `is_allowed(url) -> (bool, str)` returns status `"allowed"`, `"disallowed"`, or `"unavailable"` (fail-open on any fetch error including 404).
- Passes agent name `"GovScraper"` to `RobotFileParser.can_fetch()`, which matches both `User-agent: GovScraper` and `User-agent: *` rules with GovScraper-specific rules taking precedence.

### Known site-specific behaviour

**`catalog.data.gov`** — As of May 2026, catalog.data.gov has migrated away from a standard CKAN deployment to a custom Next.js SPA. All CKAN API endpoints (`/api/3/action/*`) return 404. The only remaining CKAN passive signal is internal `/dataset` links in the HTML, which the portal detector correctly ignores (too generic). When the crawler processes this URL, passive detection will not fire and it will be handled as a plain site via the standard depth crawler.

**`michigan.gov`** — Sits behind Akamai CDN with bot detection enabled. Plain HTTP requests using `GovScraper/2.0` receive a 403 regardless of Accept headers; only real Chromium (Playwright) gets through. The site's `robots.txt` (accessible at `www.michigan.gov/robots.txt`) permits `*` for most paths — the block is at the CDN layer, not the application layer. See Phase 4 notes on Playwright fallback for the recommended fix.

---

### Phase 4 implementation notes

**`crawler/http_client.py` — `fetch_page(url)`**
- `_is_js_heavy(html, content_type)` flags a page when: content-type is non-text/html; or visible text (after BS4 tag-strip) is < 200 chars; or the page has a `<div id="root|app">` with < 400 visible chars. The double-threshold for the SPA-root check lets pages that are partially rendered but still legible avoid a costly Playwright round-trip.
- `fetch_page()` guards the Playwright fallback behind a second check: only fires Playwright when content-type is text/html (or absent). Non-HTML responses (JSON, PDF, etc.) are returned as-is — Playwright cannot help there.
- Playwright failure is caught and logged; the method falls back to plain HTML and sets `js_rendered=False`.

**`crawler/playwright_client.py`**
- `fetch_rendered(url)` is a thin synchronous wrapper over `asyncio.run(_render_async(url))`. Each call launches a fresh Chromium context and shuts it down on exit — no persistent browser session across URLs (T-42 spec).
- The Playwright browser context uses the same `User-Agent` string as `HttpClient`.

**`crawler/http_client.py` — `_is_bot_challenge()` and bot-bypass retry**
- `_is_bot_challenge(html, status_code, headers)` detects CDN bot-protection challenges. It fires on: (a) Cloudflare `cf-ray` header or `Server: cloudflare` with HTTP 403; (b) Cloudflare JS challenge body tokens (`window._cf_chl_opt`, `cf-browser-verification`) on any status; (c) Akamai `Server: AkamaiGHost` with HTTP 403. Header-based signals are restricted to 403 to avoid retrying legitimate Cloudflare-proxied 200 responses.
- `fetch_page()` tries Playwright when `_is_bot_challenge()` returns True, as a second fallback path distinct from the JS-heavy fallback. A `_playwright_tried` local flag prevents a double Playwright attempt when a page matches both conditions (e.g., a sparse Cloudflare challenge page also flagged by `_is_js_heavy()`).
- For bot-bypass retries, the Playwright result is accepted only if `_is_bot_challenge(pw_html, 200, {})` is False — i.e., Playwright actually broke through. If the challenge persists in the rendered HTML, the original status and HTML are kept and a warning is logged.
- If Playwright bypasses successfully, `http_status` is updated to 200 and `js_rendered` is set to True.
- **Azure WAF** (`_AZURE_WAF_BODY_RE`): detects `"Azure WAF JS Challenge"` in the response body on any status. In practice Playwright cannot bypass Azure WAF's JS challenge (unlike Cloudflare's), so the bypass attempt is logged as a warning and the original 403 is kept. `floridajobs.org` exhibits this behaviour.
- Politeness: this is consistent with `robots.txt` compliance — the bypass only fires when the site's stated `robots.txt` policy permits crawling; the CDN block is a heuristic layer, not the site's crawl policy. See discussion in NOTES.md under "Known site-specific behaviour" for the `michigan.gov` Akamai case.

**`crawler/orchestrator.py` — `crawl_url()` `prefetched_seed` parameter**
- `crawl_url()` accepts an optional `prefetched_seed=(html, final_url, http_status, js_rendered)` tuple. When provided, the seed URL is not re-fetched; its result is used directly as the first `pages` entry and child links are enqueued at hop 1. This eliminates the duplicate seed fetch that previously occurred between `run.py`'s activity check (step 2) and the depth crawler (step 6).
- Without this fix, bot-bypassed URLs scored 0: Playwright succeeded in the activity check setting `active=True`, but the depth crawler's duplicate fetch would also trigger Playwright, and the second call often returned sparse content, leaving the scorer with no useful HTML.
- `run.py` always passes `prefetched_seed=(html, final_url, http_status, js_rendered)` to `crawl_url()`.

**`crawler/portal_detector.py` — `PortalDetector`**
- `detect(html, headers, base_url)` runs a two-pass strategy: passive scan first (zero extra requests), active API probe only when passive is ambiguous (0 or ≥2 candidates).
- All per-platform signal strings are module-level constants (T-44) — no literal strings in detection logic.
- Socrata footer-text check uses `html.unescape()` before comparison so `&amp;` in the raw HTML matches the `&` in the constant string.
- CKAN `/dataset` link check uses a regex (`href="…/dataset[/"'?#]`) rather than a bare substring to reduce false positives.

**`portals/` adapters (T-45–T-48)**
- `portals/__init__.py` exposes `score_metadata(text, keywords)` — the shared scoring function for all three adapters. Normalizes via NFC + diacritic stripping before whole-word regex match. Returns `(score 0–100, sorted matched keyword list)`.
- All three adapters (`SocrataAdapter`, `CKANAdapter`, `ArcGISHubAdapter`) implement `run() -> dict` returning the shared contract from PRD §12. Pagination stops as soon as a page smaller than `_PAGE_SIZE` (100) is returned, or when no `meta.next` link is present (ArcGIS Hub).
- Errors during pagination are caught, logged, and stop further requests — partial results are still aggregated and returned.

### Phase 5 implementation notes

**`crawler/orchestrator.py` — `crawl_url(seed_url, http_client, depth)`**
- BFS over `(url, hop_depth)` pairs using `collections.deque`. Visited URLs are tracked in a set keyed on the normalized URL string (fragment stripped) to prevent re-fetching.
- `_registered_domain()` uses `tldextract` to extract the `domain.suffix` pair — same logic as `HttpClient._registered_domain()`. This means subdomains of the same site (e.g. `data.michigan.gov` and `www.michigan.gov`) are treated as internal links, matching the PRD intent.
- `_extract_links()` resolves relative hrefs via `urllib.parse.urljoin` against `final_url` (not `seed_url`) so redirected pages expand links correctly. Fragments are stripped; `javascript:`, `mailto:`, `tel:`, and bare `#` hrefs are discarded.
- Rate limiting is already handled by `HttpClient.get()` — no extra delay logic is needed in the BFS loop.
- `crawl_depth_reached` is initialized to 0 and only advances when a page at hop > current max returns HTTP 200. A seed failure (non-200 or exception) leaves it at 0, same as a seed-only successful crawl — both are valid "depth 0" states per the output spec.
- Network errors during hop fetches are caught, logged, and appended as `(url, "", 0, False)` so the pages list always has one entry per attempted URL; the caller can inspect `http_status == 0` to distinguish network errors from server errors.

### Phase 6 implementation notes

**`crawler/dataset_detector.py` — `detect_datasets(pages, http_client=None)`**
- Accepts `list[PageResult]` (same tuple type returned by `crawl_url`). Pages with non-200 status or empty HTML are silently skipped.
- `_EXT_RE` uses a negative-alphanumeric lookahead `(?=[^a-zA-Z0-9]|$)` rather than a fixed set like `[?&#]` so it correctly matches extensions inside quoted `Content-Disposition` filenames (e.g. `filename="data.csv"`) and in query values (e.g. `?file=report.xlsx`).
- Extension detection checks the URL path component first, then the query string — this catches both `/files/data.csv?v=2` and `/dl?file=report.csv`.
- HEAD probes (`_check_content_disposition`) fire only for URLs whose path matches `_DOWNLOAD_PATH_RE` (contains `/download`, `/export`, `/getfile`, etc.) to avoid hammering servers with HEAD requests for every ordinary link. Rate limiting is handled by `HttpClient.head()`.
- When `Content-Disposition: attachment` is present but no recognised extension appears in the filename, the URL is added to `dataset_urls` but contributes nothing to `dataset_formats` (empty-string sentinel from `_check_content_disposition`).
- `HttpClient.head()` was added (T-61): same rate-limiting as `get()`, no retry logic, no body download.

---

### Phase 3 implementation notes

**`crawler/state_tagger.py` — `StateTagger`**
- `tag(url, html="") -> str` resolves the six-priority chain from PRD §8; returns a two-letter state code or `"NATIONAL"`.
- **Priority 1** (`*.state.XX.us`): regex on hostname — `\.state\.([a-z]{2})\.us$`.
- **Priority 2** (`XX.gov` subdomain): checks `tldextract` `domain` field is exactly two letters and matches a state abbreviation. Known federal two-letter domains (e.g. `va.gov`) are explicitly excluded before this check fires.
- **Priority 3** (state name in domain): iterates all state names longest-first, compresses spaces (`"new mexico"` → `"newmexico"`), and checks for substring presence in the registered domain. This catches embedded names like `oregoncounties.org` → `OR` and `portsoflouisiana.org` → `LA`. Abbreviation-only domains (e.g. `alconservationdistricts.gov`) are not matched here and fall through to Priority 4.
- **Priority 4** (page content): scans `<title>` and first `<h1>` using BeautifulSoup; tries full state names first (longest-first), then abbreviations against original-case text to reduce false positives on common English words (`or`, `in`, `me`). Skipped if `html` is empty.
- **Priority 5** (federal domain list): checks registered domain against `FEDERAL_DOMAINS` — `hud.gov`, `epa.gov`, `census.gov`, `usda.gov`, `faa.gov`, `usa.gov`, `data.gov`, `va.gov`.
- **Priority 6**: returns `"NATIONAL"`.
- `STATE_NAME_TO_ABBREV`, `ABBREV_TO_STATE_NAME`, `STATE_ABBREVS`, and `FEDERAL_DOMAINS` are module-level constants available for import by other components.

---

### Phase 7 implementation notes

**`scorer/keyword_loader.py`**
- File-reading logic is split into `_load_keywords(path)` and `_load_state_defs(path)` — pure functions that accept a `Path` and are directly testable with `tmp_path` fixtures. The `lru_cache`'d wrappers `_base_keywords()` and `_state_defs()` are one-liners that call them with the real config paths; they are not tested directly.
- `config/keywords.csv` is a headerless single-column file — the loader uses `csv.reader` and reads `row[0]`, not `csv.DictReader`. Do not add a header row to the CSV.
- `get_effective_keywords(state)` returns a `frozenset` (hashable) so it can be used directly as a cache key downstream. `FEDERAL` and `NATIONAL` states receive base keywords only.

**`scorer/scorer.py` — `score_page(pages, effective_keywords)`**
- Text pools are **independent**: `<h1>`–`<h3>` tags are extracted into the heading pool and then `decompose()`'d from the tree before `body.get_text()` runs, so heading text does not also score in the body pool. `<title>` is in `<head>` and is naturally excluded from body. Anchor text remains part of the body pool (it is visible body text) and also scores independently in the anchor pool.
- Effective tier weights per keyword: heading-only = 0.50, body-only = 0.35, anchor-only = 0.15, heading+body = 0.85, all three = 1.00.
- Regex patterns are precompiled once per unique `effective_keywords` frozenset via `@lru_cache` on `_compile_patterns(keywords)`. Since `get_effective_keywords` is also cached, patterns are compiled at most once per state per process lifetime.
- URL strings (`https?://...`, `www....`) are stripped from all three pools before scoring (T-72). `href` attribute values are excluded naturally by BeautifulSoup's `get_text()`.
- Text normalization (NFC + diacritic strip + lowercase) is imported from `utils.normalize_text` — shared with `portals/__init__.py`.

**`utils.py`**
- Added at the project root to hold `normalize_text(text) -> str`, shared between `scorer/scorer.py` and `portals/__init__.py`. Both previously had identical inline implementations.

---

### Phase 8 implementation notes

**`crawler/orchestrator.py` — `load_urls(csv_path)`**
- Uses `csv.DictReader`; reads only `WEB_ADDRESS` and `PRIORITY_RESOURCE` columns. All other columns (including `RESOURCE_NAME`) are ignored entirely.
- Blank/whitespace-only `WEB_ADDRESS` values are skipped with a `WARNING` log. Malformed entries (missing scheme or netloc per `urlparse`) are also skipped with a `WARNING` log. Validation is a direct `if not parsed.scheme or not parsed.netloc` guard — `urlparse` never raises, so no try/except is needed.
- Deduplication key is `scheme.lower() + netloc.lower() + path.lower()` — query strings and fragments are excluded so `example.gov/page?v=1` and `example.gov/page?v=2` are treated as the same URL. An empty path (root URL with no trailing slash) is normalized to `"/"` so `https://example.gov` and `https://example.gov/` are treated as duplicates. First occurrence is kept; subsequent duplicates are logged as `WARNING`.
- Priority sort uses Python's stable `list.sort(key=...)` so relative CSV order is preserved within each group (`priority=True` first, `priority=False` second).
- `_normalize_url_for_dedup()` is a module-level helper (not exported) that encapsulates the normalization logic.

---

### Phase 9 implementation notes

**`reporter/writer.py` — `ReportWriter`**
- `open(resume=False)` handles both fresh and resume modes. Fresh run: creates the directory, writes the CSV header, returns `set()`. Resume: reads the existing `results.csv` to collect seen URLs (keyed on the `url` column), then opens in append mode — no header written. If `resume=True` but no CSV exists yet, falls back to fresh run behaviour silently.
- `append_row(result)` calls a module-level `_serialize(result)` helper before handing off to `csv.DictWriter`. `extrasaction="ignore"` means extra keys in the result dict are harmlessly dropped.
- `_serialize` rules: `None` → `""` (handles nullable `robots_allowed`); `bool` → `"true"` / `"false"` (lowercase, not Python's `True`/`False`); `list` → pipe-joined string; all other types passed through unchanged.
- `collect_seen_urls(output_root, exclude_dir)` is a `@staticmethod` for `--new-only` mode. It scans all immediate subdirectories of `output_root`, skipping `exclude_dir` (the current run's dated directory), and unions all `url` values from any `results.csv` found. Returns `set()` if `output_root` does not exist.
- `make_error_row(url, priority, error)` is a `@staticmethod` that returns a fully-populated result dict with `active=False`, `http_status=0`, `relevance_score=0`, `robots_allowed=None`, empty lists for all list fields, and the exception message in `error_notes`. The caller passes this dict directly to `append_row()`.
- Missing boolean columns in a result dict passed to `_serialize` default to `"false"` (not `""`) via `_BOOL_COLUMNS`, ensuring the CSV is never silently missing a boolean value.
- T-93 (mutual exclusivity of `--resume` / `--new-only`) and T-94 (per-URL try/except wrapping) are both enforced in `run.py` — `ReportWriter` provides the tools (`make_error_row`, `open(resume=True)`, `collect_seen_urls`) but does not orchestrate them.

---

### Phase 3 unit tests (T-112)

`tests/test_phase3.py` — 64 unit tests covering all six resolution priorities:

| Class | What it covers |
|---|---|
| `TestConstants` | 51-entry `STATE_NAME_TO_ABBREV`, `FEDERAL_DOMAINS` membership |
| `TestPriority1StateUs` | `*.state.XX.us` → state code; invalid codes fall through; beats P3 |
| `TestPriority2TwoLetterGov` | `sco.ca.gov` → CA, `auditor.mo.gov` → MO; `va.gov` excluded as federal |
| `TestPriority3StateName` | Full name in domain; multi-word compression; longest-first ordering |
| `TestPriority4Content` | State name in `<title>`/`<h1>`; case-sensitivity for abbreviations; empty HTML skips P4 |
| `TestPriority5Federal` | All 8 FEDERAL_DOMAINS; subdomain routing |
| `TestPriority6National` | Unresolvable URLs → NATIONAL |
| `TestPriorityOrdering` | Each priority explicitly beats the one below it |

---

### Phase 11 smoke run — 2026-06-03

**Command:** `python run.py --input /tmp/urls_filtered.csv --depth 1 --delay 1.0`

**Filter:** Only URLs whose URL-pattern state tag is one of the 26 states currently in `state_definitions.json` (AK, AL, AR, AZ, CA, CO, CT, DC, DE, FL, GA, HI, IA, ID, IL, IN, KS, KY, LA, MA, MD, ME, MI, MN, MO, MS), plus FEDERAL and NATIONAL.

**Input:** 263 rows from `config/urls.csv` passed the filter; after deduplication and malformed-URL rejection, **248 unique URLs** were queued.

**Malformed entries caught at startup (logged as WARNING, skipped):**
- Missing scheme: `www.al1call.com/membership_list.doc`, `www.apers.org`, `www.insurance.arkansas.gov`, `www.klc.org`, `www.dlg.ky.gov`, `www.mmlonline.com`, `www.mocities.com`, `oa.mo.gov`, `www.ded.mo.gov`, `www.sdtownships.com`, `oversightboard.pr.gov`
- Non-URL value in WEB_ADDRESS: `Arizona Fire Insurance Premium Tax Refund | Department of Forestry and Fire Management`, `rclark@mdcounties.org` (email address)
- Duplicate: `http://portal.hud.gov/hudportal/HUD?...` (second occurrence), `http://www.mass.gov/?...` (duplicate query-string variant)

**Output:** `output/2026-06-03/results.csv` — written incrementally, one row flushed per completed URL.

**States excluded from this run** (not yet in `state_definitions.json`): MT, NC, ND, NE, NH, NJ, NM, NV, NY, OH, OK, OR, PA, RI, SC, SD, TN, TX, UT, VA, VT, WA, WI, WV, WY (69 URLs excluded).

---

### Phase 10 implementation notes

**`PageResult` NamedTuple upgrade**
`PageResult` is currently a bare tuple alias defined in `crawler/orchestrator.py` and imported by `scorer/scorer.py`. Upgrade it to a `NamedTuple` in a new `types.py` at the project root so fields are accessible by name (`page.url`, `page.html`, etc.) rather than by index. Both `crawler/orchestrator.py` and `scorer/scorer.py` should import from `types.py`. Update all construction sites: `orchestrator.py`, `dataset_detector.py`, and any test files that build `PageResult` tuples inline.
