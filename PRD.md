# Product Requirements Document: gov-scraper-v2

## 1. Overview

`gov-scraper-v2` is a free-to-run Python crawler that evaluates a curated and continuously growing list of US government-related URLs for their viability as sources of data consistent with the Census of Governments Individual State Descriptions (ISD). New URLs are added to `config/urls.csv` over time as additional sources are identified; the pipeline is designed to accommodate any list size and to process only newly added URLs when the list grows. For each URL, the tool determines whether the site is active, scores its relevance to Census-recognized government entities using state-aware vocabulary, and detects downloadable datasets. Results are written to a single CSV report suitable for analyst review and prioritization.

---

## 2. Background

The US Census Bureau's Census of Governments classifies local government entities into five categories: **County**, **Municipal**, **Township**, **Special District**, and **School District**. The Individual State Descriptions (ISD) document (publication G22-CG-ISD, released April 2024 for reference year 2022) provides a state-by-state reference defining which of these categories exist in each state, how they are legally constituted, and what they are locally called.

A critical challenge is that terminology is not uniform across states. The Census county-equivalent is a "parish" in Louisiana and a "borough" in Alaska. Several states have no functioning township governments. Connecticut abolished county governments in 1960. A scoring system that relies solely on generic keywords will systematically under-score valid sites in states where Census-standard terms do not appear in local usage.

To address this, `gov-scraper-v2` generates a per-state vocabulary file (`config/state_definitions.json`) from the ISD PDF once during setup. This file extends the generic keyword list with state-specific Census terms at score time, ensuring accurate relevance assessment regardless of local terminology.

---

## 3. Goals

- Produce an actionable CSV report for every input URL covering activity status, Census relevance score, and dataset availability.
- Score relevance using state-aware vocabulary so that sites in Louisiana, Alaska, Connecticut, and other terminologically distinct states are assessed accurately.
- Detect downloadable datasets (format and URL) without downloading them.
- Run entirely free using open-source tools — no paid APIs, no commercial services.
- Support resuming interrupted runs without reprocessing completed URLs.
- Support delta runs that process only URLs newly added to `config/urls.csv` since the last completed run.
- Respect each site's `robots.txt` and behave as a polite crawler.

---

## 4. Non-Goals

- **No government unit type classification.** The tool does not attempt to label a site as a "county" or "special district" site.
- **No dataset downloading.** Datasets are detected and logged; their content is never fetched.
- **No data parsing or transformation.** Raw site assessment is the only artifact.
- **No authentication-gated content.** Sites requiring login are flagged and skipped.
- **No paid runtime dependencies.** Every library and service used must be free and open-source.
- **`RESOURCE_NAME` is not used for any inference.** The column is a human label in `config/urls.csv` and is never read by the pipeline.

---

## 5. Stakeholders

**Primary user:** A research analyst running the tool locally to assess which URLs in `config/urls.csv` are likely to contain government unit data consistent with Census of Governments categories.

**Output consumer:** The analyst reads the output CSV to prioritize which sites warrant manual investigation, dataset download, or deeper integration work.

---

## 6. Functional Requirements

### FR-1: Input Ingestion

- Read `config/urls.csv` at startup. The file is a living document; new rows are added over time as additional sources are identified. The pipeline must handle any list size without modification.
- Use the `WEB_ADDRESS`, `PRIORITY_RESOURCE`, and `STATE` columns. Ignore all other columns including `RESOURCE_NAME`.
- `STATE` must be a two-letter USPS abbreviation or `NATIONAL`. Rows with a missing or blank `STATE` value default to `NATIONAL`.
- Deduplicate by `WEB_ADDRESS` at ingestion time (case-insensitive, normalised URL). Log any duplicate entries found and process each unique URL only once per run.
- Skip rows where `WEB_ADDRESS` is empty or blank; log each skipped row.
- Validate that each non-empty `WEB_ADDRESS` is a parseable URL; log and skip malformed entries.

### FR-2: robots.txt Compliance

- Before crawling any path on a domain, fetch and parse `robots.txt` for that domain.
- Honour `Disallow` rules for the configured `User-Agent` (`GovScraper/2.0`).
- **Fail-open policy:** if `robots.txt` cannot be fetched (timeout, 404, network error), log a warning and proceed. Record `robots_status: unavailable` in the output row.
- Cache the parsed result per domain for the lifetime of a single run.

### FR-3: Activity Check

- Issue an HTTP GET to the seed URL, following redirects automatically (httpx default behaviour).
- **Active:** terminal response status is HTTP 200, including cases where a CDN bot-challenge (see FR-6) was bypassed via Playwright and the final rendered status is 200.
- **Inactive:** terminal status is 4xx, 5xx, or any network-level failure (timeout, DNS error, connection refused), after any bot-bypass attempt has been exhausted.
- Record `active: true/false`, `http_status` (terminal code after any bypass), and `final_url` (resolved URL after all redirects).

### FR-4: Polite Crawling

- Default crawl depth: 2 hops from the seed URL, following internal links only (same registered domain).
- Enforce a minimum 2-second delay between consecutive requests to the same domain.
- Maximum 3 retries on HTTP 429 or 503 with exponential backoff (1 s, 2 s, 4 s).
- Do not follow external links during the depth crawl.

### FR-5: State Tagging

Each URL's state tag is read directly from the `STATE` column in `config/urls.csv`. The analyst populates this column when adding new URLs. Valid values:

- **Two-letter USPS abbreviation** (e.g., `MI`, `CA`, `TX`) — URL is scored using `config/keywords.csv` plus the state-specific terms from `config/state_definitions.json`.
- **`NATIONAL`** — URL is not state-specific (federal agencies, national associations, multi-state resources, unresolvable); scored against `config/keywords.csv` only.

A missing or blank `STATE` cell is treated as `NATIONAL`. The state tag is available from ingestion time, before any HTTP request is made, so all output rows carry a state value regardless of whether the URL is active.

### FR-6: JavaScript Detection and Rendering

- Attempt a plain HTTP fetch first (httpx).
- Detect JS-heavy pages by checking: response body contains `<div id="root">` or `<div id="app">` with minimal text content, or `Content-Type` is not `text/html`, or rendered text length is less than 200 characters after stripping tags.
- For detected JS-heavy pages, re-fetch using a headless Playwright browser to obtain the rendered DOM.
- **CDN bot-challenge bypass:** after the plain HTTP fetch, also check for CDN bot-protection signals and retry with Playwright if detected. Supported platforms:
  - **Cloudflare:** `cf-ray` response header or `Server: cloudflare` with HTTP 403; or Cloudflare JS challenge body tokens (`window._cf_chl_opt`, `cf-browser-verification`) on any status.
  - **Akamai:** `Server: AkamaiGHost` with HTTP 403.
  - **Azure WAF:** `Azure WAF JS Challenge` in the response body on any status.
  - Header-based signals are restricted to 403 to avoid retrying legitimate CDN-proxied 200 responses. Body tokens fire on any status to catch JS challenges served as 200.
  - The Playwright result is accepted only if the same bot-challenge signals are absent from the rendered HTML; if the challenge persists, the original 403 is kept, `cdn_blocked=True` is set, and a warning is logged. CDN-blocked URLs receive `relevance_score=null` and a descriptive `error_notes` entry.
  - Politeness: the bypass is consistent with `robots.txt` compliance — the CDN block is a heuristic layer, not the site's stated crawl policy. The bypass only fires after `robots.txt` has already been checked and access is permitted.
- Record `js_rendered: true` in the output row when Playwright was used (covers both JS-heavy and bot-bypass cases).

### FR-7: State-Aware Keyword Relevance Scoring

- **Effective keyword set:** `config/keywords.csv` terms UNION `config/state_definitions.json[state].census_terms` for the tagged state. For `NATIONAL` URLs, use `config/keywords.csv` only.
- **Keyword matching:** case-insensitive, whole-word boundary match.
- **Weighted scoring by content location:**

  | Location | Weight |
  |---|---|
  | Page `<title>` and `<h1>`–`<h3>` headings | 50 pts |
  | Full visible body text | 35 pts |
  | Link anchor text | 15 pts |

- **Score formula:** `relevance_score = min(100, round(weighted_hits / normalization_factor * 100))`
  where `normalization_factor = base_keyword_count()` — fixed at the count of terms in `config/keywords.csv`. State-specific terms extend the numerator (more keywords can match) without inflating the denominator, so state-tagged URLs are not penalized for having a larger effective keyword set.
- URL and domain text do not contribute to the score.
- Record `relevance_score` (0–100) and `matched_keywords` (pipe-separated list of matched terms).

### FR-8: Downloadable Dataset Detection

Scan all pages visited during the crawl (up to depth 2) for links to downloadable files.

- **Detected formats:** `.csv`, `.xlsx`, `.xls`, `.json`, `.xml` (machine-readable); `.pdf` (flagged separately).
- **Detection signals:**
  - Link `href` ends with a detected extension.
  - Link `href` contains a detected extension before a query string (e.g., `download.php?file=data.csv`).
  - HTTP `Content-Disposition: attachment` header on a followed link.
- **Ranking:** all candidates are scored. Score = format tier (CSV/JSON/XLSX=3, XLS/XML=2, PDF/unknown=1) + Census keyword match in link anchor text (+2) + crawl depth proximity (seed page=+2, depth-1=+1, depth-2=+0). Candidates are sorted descending by score, most-relevant first.
- **No dataset URL is dropped.** The complete ranked list is written, one row per URL, to a normalized companion CSV `output/<date>/dataset_urls.csv` (`url, dataset_url, format, rank`), keyed back to `results.csv` by `url`. One URL per row means this file has no cell-size limit.
- Record `datasets_found: true/false`, `dataset_urls` (pipe-separated, ranked, **char-capped** at 32,000 chars to keep `results.csv` under the 32,767-char spreadsheet cell limit), `dataset_formats` (pipe-separated, deduplicated, all detected formats), `dataset_urls_total` (count of all detected URLs), and `dataset_urls_omitted` (count dropped from the `dataset_urls` cell to stay under the char budget — these remain in the companion CSV).
- PDF links are included with format `pdf`; they are not combined with machine-readable format flags.
- When the ranked `dataset_urls` cell is char-capped, the lowest-scoring URLs are the ones omitted from the cell (never from the companion CSV).

### FR-9: Priority URL Handling

- URLs where `PRIORITY_RESOURCE` is `YES` (case-insensitive) are sorted to the front of the processing queue before the run begins.
- Record `priority: true/false` in the output row.

### FR-10: Crash-Safe Incremental Output and Delta Runs

- Append each completed URL's result row to the output CSV immediately after processing, before starting the next URL.
- On restart with `--resume`, read the current run's output CSV and skip any URL already present in it. This recovers from crashes mid-run without reprocessing completed URLs.
- On invocation with `--new-only`, collect all `WEB_ADDRESS` values present across every previous output CSV in the `output/` directory and skip any URL found there. This enables efficient delta runs: when new rows are added to `config/urls.csv`, only the genuinely new URLs are processed.
- The output CSV header is written once at the start of a fresh run; it is not re-written on resume or delta runs.
- `--resume` and `--new-only` are mutually exclusive flags.

### FR-11: One-Time Setup — Generate `config/state_definitions.json`

- Provided as `setup/generate_state_definitions.py`.
- Accepts the ISD PDF path and an LLM backend flag (`--llm gemini` or `--llm ollama`).
- Extracts per-state Census terminology from the PDF and writes `config/state_definitions.json`.
- Must be run once before the first crawl and re-run when a new ISD edition is published (approximately every 5 years).
- The generated file is committed to the repository so subsequent users do not need an LLM to run the crawler.
- **Auto-detect mode** (when `--states` is omitted): compares `config/state_abbrev.json` against the existing output to identify states that are absent, have empty `census_terms`, or have error notes, then prompts for confirmation before processing. Stops after `--max-requests` API calls (default 20, matching the Gemini free-tier RPD cap). Retries transient 429 and 503 errors up to 3 times; each retry counts against the daily limit. Results are always merged into the existing file — prior entries are never overwritten. A post-run summary logs how many states remain.
- **Manual mode** (`--states XX,YY`): processes exactly the specified states with no request cap; results are merged into the existing file.

### FR-12: Open Data Portal Detection

After the initial page fetch (FR-6), apply a two-pass detection process before the depth crawler and scorer run. If a known open data platform is identified, record the platform, set `relevance_score` to null, and skip the depth crawl and scorer. Portal dataset enumeration via API is future work.

**Pass 1 — Passive detection (zero extra requests)**

Scan the HTML and response headers already fetched for platform-specific signatures:

| Platform | Passive signals |
|---|---|
| Socrata / Tyler Technologies | Footer text contains "Powered by Socrata" or "Powered by Tyler Data & Insights"; response header `X-Socrata-RequestId` present; scripts loaded from `*.socrata.com` or `*.tylertech.com`; CSS classes prefixed `socrata-` or `soda-`; meta tag `<meta name="soda-host">` present |
| CKAN | `<meta name="generator" content="ckan">` in page `<head>`; `<body class="ckan-*">` or `<html id="ckan-*">`; inline JS contains `ckan.module(`; internal links to `/dataset` path |
| ArcGIS Hub | Custom web components `<hub-hero>`, `<hub-gallery>`, or any `<arcgis-hub-*>` element in the DOM; scripts loaded from `js.arcgis.com` or `cdn.arcgis.com`; `<meta property="og:site_name" content="ArcGIS Hub">`; domain matches `*.hub.arcgis.com` or `*.opendata.arcgis.com` |

**Pass 2 — Active API probe (one extra request, only if Pass 1 is inconclusive)**

Issue a single GET to the platform's canonical status or catalog endpoint:

| Platform | Probe URL | Success condition |
|---|---|---|
| Socrata | `{base_url}/api/catalog/v1?limit=1` | HTTP 200 + JSON body contains `"results"` array |
| CKAN | `{base_url}/api/3/action/site_read` | HTTP 200 + JSON body contains `"success": true` |
| ArcGIS Hub | `{base_url}/api/v3/datasets?page[size]=1` | HTTP 200 + JSON body contains `"data"` array |

If passive signals point to multiple platforms (rare), the active API probe result takes precedence.

**Routing:**
- Portal detected → record `portal_platform`; set `relevance_score` to null; skip depth crawl and scorer.
- No portal detected → proceed with the standard depth crawler and scorer.

**Known site-specific behaviour:**
- `catalog.data.gov` — as of 2026, this site has migrated from CKAN to a custom Next.js SPA. All CKAN API endpoints (`/api/3/action/*`) return 404 and passive signals are absent. The portal detector correctly falls through; the site is processed as a plain page via the standard depth crawler.

**Output:** record `portal_platform` (`Socrata`, `CKAN`, `ArcGIS Hub`, or empty string). `relevance_score` is null for detected portal URLs.

---

## 7. Scoring Methodology

### Effective keyword set

```
effective_keywords = set(keywords.csv) | set(state_definitions[state].census_terms)
```

For `NATIONAL` URLs: `effective_keywords = set(keywords.csv)`.

### Normalization factor

`normalization_factor = base_keyword_count()`

Fixed at the number of terms in `config/keywords.csv`. State-specific terms extend the effective keyword set (increasing the numerator when they match) without inflating the denominator, so URLs in states with rich Census vocabulary are not penalized relative to NATIONAL URLs.

### Weighted hit calculation

For each keyword `k` in `effective_keywords`:
- Award 0.50 pts if `k` appears in title or any H1–H3 heading (capped at 1 occurrence per keyword per location tier).
- Award 0.35 pts if `k` appears in body text.
- Award 0.15 pts if `k` appears in any link anchor text.
- Multiple location matches for the same keyword are additive (max 1.00 pt per keyword).

```
weighted_hits = sum(location_score per keyword)
relevance_score = min(100, round(weighted_hits / normalization_factor * 100))
```

### Keyword normalisation

- Strip diacritics and normalise Unicode to NFC before matching.
- Match on whole-word boundaries (regex `\b`).
- Case-insensitive.

---

## 8. State Tagging

State tags are maintained manually in the `STATE` column of `config/urls.csv`. The analyst assigns the correct value when adding a new URL. The pipeline reads the column at ingestion and carries the value through to the output row unchanged.

| Value | Meaning | Scoring behaviour |
|---|---|---|
| Two-letter abbreviation (e.g. `MI`) | State-specific URL | `keywords.csv` + `state_definitions.json[state].census_terms` |
| `NATIONAL` | Non-state-specific or unknown (including federal agencies) | `keywords.csv` only |

When the `STATE` column is absent or blank, the row defaults to `NATIONAL`.

---

## 9. `config/state_definitions.json` Generation

**Script:** `setup/generate_state_definitions.py`

**Inputs:**
- `config/2022ISD.pdf` — Census Bureau ISD, 339 pages, one section per state/DC.
- `config/state_abbrev.json` — ordered list of all 51 state/DC abbreviations; used to track completion progress.
- `--llm gemini` (Gemini 2.5 Flash free tier, requires `GEMINI_API_KEY`) or `--llm ollama` (local model, requires Ollama running).

**Key flags:**

| Flag | Default | Description |
|---|---|---|
| `--states XX,YY` | *(auto-detect)* | Process specific states only; merge into existing output |
| `--max-requests N` | `20` | Max API calls per auto-detect run (retries count); matches free-tier RPD cap |
| `--force` | off | Skip confirmation prompt |

**Output schema:**
```json
{
  "LA": {
    "census_terms": ["parish", "police jury", "ward", "parish school district"],
    "notes": "Parish = county equivalent. No township governments."
  },
  "AK": {
    "census_terms": ["borough", "first-class borough", "second-class borough", "home-rule borough", "census area"],
    "notes": "Borough = county equivalent. No townships. No independent school districts."
  }
}
```

**Purpose:** vocabulary expansion for the relevance scorer only. Not used for unit type labeling.

**Lifecycle:** commit the generated file to the repository. Re-run the script when a new ISD edition is published. Do not hand-edit the file.

---

## 10. Non-Functional Requirements

**NFR-1: Zero paid runtime dependencies.**
All libraries used during a crawl run must be free and open-source. LLMs are only required for the one-time setup script.

**NFR-2: Polite scraping.**
Every request must include `User-Agent: GovScraper/2.0 (contact: <CONTACT_EMAIL>)`. Per-domain rate limiting and `robots.txt` compliance are mandatory.

**NFR-3: Graceful error handling.**
A failure on any individual URL must be caught, logged, and recorded in `error_notes` in the output row. The run must continue to the next URL.

**NFR-4: Deterministic output.**
Given the same input CSV and the same live state of each website, two runs must produce the same output CSV.

---

## 11. Architecture

### Pipeline (per crawl run)

```
config/urls.csv  (STATE column pre-populated by analyst)
       |
  [Orchestrator / load_urls]  <-- reads url, priority, state per row
       |
  [Priority Queue]  <-- PRIORITY_RESOURCE=YES sorted first
       |
  per URL:
  +-----------------------------------------+
  | state = entry["state"]                  |
  | effective_keywords = get_keywords(state)|
  |      |                                  |
  | [RobotsChecker]                         |
  |      |                                  |
  | [ActivityChecker]                       |
  |   httpx GET → CDN bypass? → Playwright  |
  |   (Cloudflare / Akamai / Azure WAF)     |
  |      |                                  |
  | [PortalDetector]  (passive → API probe) |
  |      |                                  |
  |  if portal: record platform,            |
  |             score=null, skip crawl      |
  |  else:      [Crawler]  depth=2          |
  |    prefetched_seed avoids re-fetch      |
  |    child hops via httpx + Playwright    |
  | [Scorer]   (keyword + state vocab)      |
  |      |                                  |
  | [DatasetDetector]                       |
  +-----------------------------------------+
       |
  [ReportWriter]  <-- incremental CSV append
       |
  output/<run-date>/results.csv
```

### One-time setup

```
config/2022ISD.pdf
       |
  setup/generate_state_definitions.py  (LLM-assisted)
       |
  config/state_definitions.json  (committed to repo)
```

### Suggested file layout

```
gov-scraper-v2/
  config/
    urls.csv
    keywords.csv
    state_definitions.json        # generated; committed
    state_abbrev.json             # ordered list of 51 abbreviations; committed
    2022ISD.pdf
  crawler/
    orchestrator.py
    robots.py
    http_client.py                # httpx wrapper with rate limiting
    playwright_client.py
    dataset_detector.py
    portal_detector.py
  scorer/
    scorer.py
    keyword_loader.py
  reporter/
    writer.py
  setup/
    generate_state_definitions.py
  tests/
    test_phase2.py … test_phase10.py  # test_phase3.py removed (StateTagger deleted)
    test_integration_urls.py          # skipped by default; set RUN_INTEGRATION_TESTS=1
  output/                         # gitignored
  page_result.py                  # PageResult NamedTuple shared across packages
  utils.py                        # normalize_text() shared text helper
  run.py                          # entrypoint
```

---

## 12. Portal Detection (Future: Adapter Enumeration)

Portal detection (FR-12) identifies whether a URL is a known open data platform. Detection is the only behavior in the current implementation. When a portal is detected, `portal_platform` is recorded and `relevance_score` is left null — the depth crawler and scorer are skipped.

Platform-specific adapter enumeration (paginating the dataset catalog via API and scoring individual dataset metadata) is scoped out for a future iteration. The detection signals and API endpoints documented below serve as the specification for that work when it is implemented:

| Platform | Detection | Future API endpoint |
|---|---|---|
| Socrata / Tyler Technologies | Footer text, `X-Socrata-RequestId` header, scripts from `*.socrata.com` / `*.tylertech.com` | `GET /api/catalog/v1?limit=100&offset=N` |
| CKAN | `<meta name="generator" content="ckan">`, body class `ckan-*`, inline `ckan.module(` JS | `GET /api/3/action/package_search?rows=100&start=N` |
| ArcGIS Hub | Custom elements `<hub-hero>` / `<hub-gallery>`, scripts from `js.arcgis.com`, domain `*.hub.arcgis.com` | `GET /api/v3/datasets?page[size]=100&page[number]=N` |

---

## 13. Output Report Specification

**File:** `output/<YYYY-MM-DD>/results.csv`  
**One row per input URL** (including skipped/inactive URLs).

**Companion file:** `output/<YYYY-MM-DD>/dataset_urls.csv` — normalized table holding the *complete* set of detected dataset URLs, one row per URL (`url, dataset_url, format, rank`). `url` is the foreign key back to `results.csv`. Written incrementally alongside `results.csv`. This is the authoritative full list; `results.csv.dataset_urls` is a char-capped convenience subset.

| Column | Type | Description |
|---|---|---|
| `url` | string | Seed URL from `WEB_ADDRESS` column |
| `priority` | boolean | `true` if `PRIORITY_RESOURCE` was `YES` |
| `state` | string | Two-letter state code or `NATIONAL` |
| `active` | boolean | `true` if terminal HTTP status was 200 |
| `http_status` | integer | Terminal HTTP response code (or 0 for network failure) |
| `final_url` | string | Resolved URL after redirect chain |
| `robots_allowed` | boolean | `true` if crawl was permitted by robots.txt; `null` if unavailable |
| `robots_status` | string | `allowed`, `disallowed`, or `unavailable` |
| `js_rendered` | boolean | `true` if Playwright was used to render the page |
| `relevance_score` | integer | 0–100 Census relevance score; null for detected portals, CDN-blocked URLs, and network errors (cases where scoring was impossible) |
| `matched_keywords` | string | Pipe-separated list of matched keywords |
| `datasets_found` | boolean | `true` if at least one downloadable file was detected |
| `dataset_urls` | string | Pipe-separated list of detected dataset URLs, ranked, char-capped at 32,000 chars for spreadsheet safety. The complete list lives in the companion `dataset_urls.csv`. |
| `dataset_formats` | string | Pipe-separated deduplicated format list of all detected formats (e.g., `csv\|xlsx\|pdf`) |
| `dataset_urls_total` | integer | Count of all detected dataset URLs (= number of rows for this `url` in the companion CSV) |
| `dataset_urls_omitted` | integer | Count of URLs dropped from the `dataset_urls` cell to stay under the char budget; `0` when all fit. Omitted URLs are still in the companion CSV. |
| `crawl_depth_reached` | integer | Deepest hop level successfully crawled (0–2) |
| `portal_platform` | string | `Socrata`, `CKAN`, `ArcGIS Hub`, or empty string if not a portal |
| `error_notes` | string | Description of any errors encountered; empty if none |

`RESOURCE_NAME` is intentionally excluded from the output.

---

## 14. Configuration

### `config/urls.csv`

This is a living document. New rows are appended as additional government sources are identified. The pipeline imposes no limit on list size and requires no code changes when new URLs are added.

| Column | Used by pipeline | Description |
|---|---|---|
| `RESOURCE_NAME` | No | Human label; ignored by all pipeline components |
| `WEB_ADDRESS` | Yes | Seed URL to crawl |
| `PRIORITY_RESOURCE` | Yes | `YES` to sort URL to front of queue; any other value treated as non-priority |
| `STATE` | Yes | Manually assigned state tag: two-letter USPS abbreviation or `NATIONAL`. Missing or blank defaults to `NATIONAL`. Fill this column when adding new rows. |

### `config/keywords.csv`

Single column `Keywords`. One keyword or phrase per row. Used as the base vocabulary for all URLs regardless of state.

### `config/state_definitions.json`

Generated by `setup/generate_state_definitions.py`. Schema:
```json
{ "<STATE_ABBREV>": { "census_terms": ["...", "..."], "notes": "..." } }
```
Do not hand-edit.

### `config/state_abbrev.json`

Ordered JSON array of all 51 state/DC two-letter abbreviations. Used by the setup script to determine processing order and track completion progress. Do not edit.

### Runtime flags (`run.py`)

| Flag | Default | Description |
|---|---|---|
| `--depth` | `2` | Maximum crawl depth from seed URL |
| `--delay` | `2.0` | Minimum seconds between requests to the same domain |
| `--output` | `output/<YYYY-MM-DD>/` | Directory for results CSV |
| `--resume` | off | Skip URLs already present in the current run's output CSV (crash recovery) |
| `--new-only` | off | Skip URLs present in any previous run's output CSV (delta run for newly added URLs) |
| `--input` | `config/urls.csv` | Path to input URL list; can point to any CSV matching the schema |

---

## 15. Future Enhancements

The scorer is designed as a pluggable interface. The following modes can be added without changing the pipeline:

| Mode | Scorer | Cost | Notes |
|---|---|---|---|
| **1 (v1, default)** | Keyword matching + state vocab | Free, zero deps | Rule-based; fully deterministic |
| **2** | Sentence transformers (`all-MiniLM-L6-v2`) | Free, local | Semantic similarity; ~80 MB model; CPU-friendly |
| **3** | Ollama local LLM (e.g., Llama 3.2 3B) | Free, local | Full-page semantic assessment; requires ~4 GB RAM |
| **4** | Gemini 2.0 Flash free tier | Free, API key required | Highest quality; 15 RPM / 1500 req/day limit |

Additional enhancements:
- **Portal adapter enumeration**: implement Socrata, CKAN, and ArcGIS Hub adapters (see §12) to paginate each portal's dataset catalog, score individual dataset metadata against Census vocabulary, and populate `portal_dataset_count`, `portal_relevant_count`, and `top_dataset_urls` output columns.
- **Incremental re-crawl**: on re-run, skip URLs whose `final_url` and `http_status` match the previous run's output (no site state change detected).
- **Configurable crawl depth**: expose `--depth` beyond 2 for deeper dataset discovery.
- **Structured dataset metadata**: where possible, attempt a HEAD request on detected dataset URLs to capture `Content-Length` and `Last-Modified`.

---

## 16. Out of Scope

- Government unit type classification (County / Municipality / Township / etc.)
- Downloading datasets or storing dataset content
- Parsing, transforming, or ingesting data from detected datasets
- Handling authentication-gated or login-required pages
- Any paid API, hosted service, or commercial dependency
- Using `RESOURCE_NAME` for any automated inference or classification
- Crawling URLs not present in `config/urls.csv`
- Modifying or enriching `config/urls.csv` as a pipeline output

---

## 17. Streamlit UI

### Overview

`app.py` is a local Streamlit prototype that wraps the pipeline in a browser-based GUI. It has no effect on CLI operation and requires no changes to any pipeline module.

### Entry point

```bash
streamlit run app.py
```

Requires `streamlit>=1.35.0` and `pandas>=2.0.0` (both included in `requirements.txt`).

### Page 1 — Explorer

Loads the most recently dated `output/<YYYY-MM-DD>/results.csv` and renders it as an interactive, filterable, sortable table.

**Sidebar filters (applied top-to-bottom):**

| Filter | Type | Behavior |
|---|---|---|
| State | Multiselect | Restricts rows to selected state(s). No selection = all states. |
| Status | Radio (horizontal) | `Active` (default): `active=true` and no `error_notes`. `Inactive`: `active=false` and no `error_notes`. `Errors & blocked`: any row with a non-empty `error_notes`. `All`: no status filter. |
| Unscored only | Checkbox | When checked, shows only rows with null `relevance_score`; disables the score slider. |
| Score range | Slider (0–100) | Filters by `relevance_score`. Rows with null scores are excluded when the slider departs from the full 0–100 range and "unscored only" is unchecked. |
| Keyword search | Multiselect | Options drawn from `keywords.csv` + all `state_definitions.json` census terms; OR logic against the `matched_keywords` column (case-insensitive). |
| Portal platform | Multiselect | Restricts to rows matching the selected portal platform(s). |

**Default sort:** Descending `relevance_score` (nulls last), then descending dataset count derived from `dataset_urls`.

**Table columns:** URL, Priority, State, Active, Score, Datasets?, Formats, Dataset URLs, Matched Keywords, Depth, Portal, Error Notes. Boolean columns (`priority`, `active`, `datasets_found`) render as checkboxes; `relevance_score` renders as an integer.

**Drill-down panel:** Selecting a row expands a panel below the table showing four metric tiles (Active, HTTP Status, Score, Crawl Depth), a "Matched keywords" expander (open by default, still collapsible) showing matched keywords as a wrapping row of colored badge chips (one per keyword, deterministic color per keyword), and the full `dataset_urls` list as a clickable link table with inferred format badges.

### Page 2 — Scraper

Accepts a URL and crawl parameters, runs the full pipeline in-process, and optionally appends the result to the most recent CSV.

**Inputs:**
- URL to scrape (text)
- State (selectbox: NATIONAL + sorted state abbreviations)
- Priority resource (checkbox, default off)
- Crawl depth (slider 1–2, default 2)
- Max pages per crawl (number input 5–75, default 25)

**Execution:** Calls `run._process_url()` directly (imports the function from `run.py`; no subprocess). A `st.status` container streams live log lines from the `run`, `crawler`, and `scorer` loggers during the crawl via a custom `logging.Handler`.

**Result display:** Four metric tiles (Active, HTTP Status, Score, Crawl Depth), a "Matched keywords" expander (open by default, still collapsible) rendering `matched_keywords` as colored badge chips (same component as the Explorer drill-down), `dataset_urls` clickable link table, `error_notes` warning banner, and `portal_platform` info banner.

**Save:** "Add to most recent results.csv" appends the result row using `ReportWriter(resume=True)`. The button disables after a successful write to prevent duplicate rows.
