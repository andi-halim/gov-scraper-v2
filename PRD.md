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
- Use only the `WEB_ADDRESS` and `PRIORITY_RESOURCE` columns. Ignore all other columns including `RESOURCE_NAME`.
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
- **Active:** terminal response status is HTTP 200.
- **Inactive:** terminal status is 4xx, 5xx, or any network-level failure (timeout, DNS error, connection refused).
- Record `active: true/false`, `http_status` (terminal code), and `final_url` (resolved URL after all redirects).

### FR-4: Polite Crawling

- Default crawl depth: 2 hops from the seed URL, following internal links only (same registered domain).
- Enforce a minimum 2-second delay between consecutive requests to the same domain.
- Maximum 3 retries on HTTP 429 or 503 with exponential backoff (1 s, 2 s, 4 s).
- Do not follow external links during the depth crawl.

### FR-5: State Tagging

Assign a US state (two-letter USPS abbreviation) or a special tag to each URL before scoring, using the following priority order:

1. **URL pattern — `*.state.XX.us`**: extract `XX` directly.
2. **URL pattern — `*.XX.gov` subdomain**: extract two-letter prefix before `.gov` (e.g., `sco.ca.gov` → `CA`).
3. **URL pattern — full state name in registered domain**: match against canonical state name list (e.g., `michigan.gov` → `MI`).
4. **Page content fallback**: fetch page, scan `<title>` and first `<h1>` for a US state name or abbreviation.
5. **FEDERAL**: domain matches a known federal agency list (e.g., `hud.gov`, `epa.gov`, `census.gov`, `usda.gov`, `faa.gov`).
6. **NATIONAL**: none of the above resolved a state.

`FEDERAL` and `NATIONAL` URLs are scored against `config/keywords.csv` only (no state expansion).

### FR-6: JavaScript Detection and Rendering

- Attempt a plain HTTP fetch first (httpx).
- Detect JS-heavy pages by checking: response body contains `<div id="root">` or `<div id="app">` with minimal text content, or `Content-Type` is not `text/html`, or rendered text length is less than 200 characters after stripping tags.
- For detected JS-heavy pages, re-fetch using a headless Playwright browser to obtain the rendered DOM.
- Record `js_rendered: true` in the output row when Playwright was used.

### FR-7: State-Aware Keyword Relevance Scoring

- **Effective keyword set:** `config/keywords.csv` terms UNION `config/state_definitions.json[state].census_terms` for the tagged state. For `FEDERAL` and `NATIONAL` URLs, use `config/keywords.csv` only.
- **Keyword matching:** case-insensitive, whole-word boundary match.
- **Weighted scoring by content location:**

  | Location | Weight |
  |---|---|
  | Page `<title>` and `<h1>`–`<h3>` headings | 50 pts |
  | Full visible body text | 35 pts |
  | Link anchor text | 15 pts |

- **Score formula:** `relevance_score = min(100, round(weighted_hits / normalization_factor * 100))`
  where `normalization_factor` scales relative to the total number of unique keywords in the effective set.
- URL and domain text do not contribute to the score.
- Record `relevance_score` (0–100) and `matched_keywords` (pipe-separated list of matched terms).

### FR-8: Downloadable Dataset Detection

Scan all pages visited during the crawl (up to depth 2) for links to downloadable files.

- **Detected formats:** `.csv`, `.xlsx`, `.xls`, `.json`, `.xml` (machine-readable); `.pdf` (flagged separately).
- **Detection signals:**
  - Link `href` ends with a detected extension.
  - Link `href` contains a detected extension before a query string (e.g., `download.php?file=data.csv`).
  - HTTP `Content-Disposition: attachment` header on a followed link.
- Record `datasets_found: true/false`, `dataset_urls` (pipe-separated), `dataset_formats` (pipe-separated, deduplicated).
- PDF links are included in `dataset_urls` with format `pdf`; they are not combined with machine-readable format flags.

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

### FR-12: Open Data Portal Detection and Routing

After the initial page fetch (FR-6), apply a two-pass detection process before the depth crawler and scorer run. If a known open data platform is identified, route the URL to a platform-specific API adapter and skip the standard depth crawler.

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
- Portal detected → skip depth crawler; call the appropriate adapter (see §12).
- No portal detected → proceed with the standard depth crawler and scorer.

**Output:** record `portal_platform` (`Socrata`, `CKAN`, `ArcGIS Hub`, or empty string), `portal_dataset_count` (total datasets in catalog), `portal_relevant_count` (datasets with score > 0), and `top_dataset_urls` (pipe-separated, up to 10, sorted by relevance score descending).

---

## 7. Scoring Methodology

### Effective keyword set

```
effective_keywords = set(keywords.csv) | set(state_definitions[state].census_terms)
```

For `FEDERAL` / `NATIONAL` URLs: `effective_keywords = set(keywords.csv)`.

### Normalization factor

`normalization_factor = len(effective_keywords)`

This ensures that a state with more Census terms (e.g., a state with rich special-district vocabulary) does not produce artificially inflated scores relative to a state with fewer terms.

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

## 8. State Tagging Logic

Resolution is attempted in order; the first match wins.

| Priority | Pattern | Example |
|---|---|---|
| 1 | `*.state.XX.us` TLD | `treasurer.state.tx.us` → `TX` |
| 2 | Two-letter subdomain before `.gov` | `sco.ca.gov` → `CA`, `auditor.mo.gov` → `MO` |
| 3 | Full state name in registered domain | `michigan.gov` → `MI`, `illinois.gov` → `IL` |
| 4 | State name/abbrev in page `<title>` or `<h1>` | page title "Iowa Department of Management" → `IA` |
| 5 | Domain in known federal list | `hud.gov` → `FEDERAL` |
| 6 | Unresolved | → `NATIONAL` |

Known federal domains include at minimum: `hud.gov`, `epa.gov`, `census.gov`, `usda.gov`, `faa.gov`, `usa.gov`, `data.gov`.

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
config/urls.csv
       |
  [Orchestrator]
       |
  [State Tagger]  <-- URL patterns + page content fallback
       |
  [Priority Queue]  <-- PRIORITY_RESOURCE=YES sorted first
       |
  per URL:
  +-----------------------------------------+
  | [RobotsChecker]                         |
  |      |                                  |
  | [ActivityChecker]  (HTTP GET + redirect)|
  |      |                                  |
  | [PortalDetector]  (passive → API probe) |
  |      |                                  |
  |  if portal: [PortalAdapter] (API-first) |
  |  else:      [Crawler]  depth=2          |
  |               (httpx + Playwright)      |
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
    state_tagger.py
    dataset_detector.py
    portal_detector.py
  portals/
    __init__.py
    socrata.py
    ckan.py
    arcgis_hub.py
  scorer/
    scorer.py
    keyword_loader.py
  reporter/
    writer.py
  setup/
    generate_state_definitions.py
  output/                         # gitignored
  run.py                          # entrypoint
```

---

## 12. Portal Detection Adapters

Each adapter enumerates all datasets in a detected portal's catalog via API, scores each dataset's metadata against the effective keyword set, and returns an aggregated result for the output row.

### Shared adapter contract

Each adapter receives `(base_url: str, effective_keywords: frozenset, http_client)` and returns:

```python
{
    "portal_dataset_count": int,
    "portal_relevant_count": int,    # datasets with relevance_score > 0
    "top_dataset_urls": list[str],   # up to 10, sorted by relevance score desc
    "matched_keywords": list[str],   # union of all matched keywords across datasets
    "relevance_score": int           # max relevance score across all datasets
}
```

Metadata scoring: concatenate each dataset's title, description, and tags into a single text block; run the same weighted keyword matcher from FR-7; record per-dataset score. URL and domain text are excluded from scoring (consistent with FR-7).

### Socrata / Tyler Technologies adapter

- Endpoint: `GET /api/catalog/v1?limit=100&offset=N` — paginate until all datasets are retrieved.
- Per dataset: extract `resource.name`, `resource.description`, `classification.domain_tags`, `resource.type`, `permalink`.
- Apply the same per-domain delay as FR-4 between paginated requests.

### CKAN adapter

- Endpoint: `GET /api/3/action/package_search?rows=100&start=N` — retrieve all dataset metadata in batches (more efficient than per-ID `package_show` lookup).
- Per dataset: extract `title`, `notes`, `tags[].name`, `resources[].format`, `resources[].url`.

### ArcGIS Hub adapter

- Endpoint: `GET /api/v3/datasets?page[size]=100&page[number]=N` — paginate until complete.
- Per dataset: extract `attributes.name`, `attributes.description`, `attributes.tags`, `attributes.access.urls.download`.

---

## 13. Output Report Specification

**File:** `output/<YYYY-MM-DD>/results.csv`  
**One row per input URL** (including skipped/inactive URLs).

| Column | Type | Description |
|---|---|---|
| `url` | string | Seed URL from `WEB_ADDRESS` column |
| `priority` | boolean | `true` if `PRIORITY_RESOURCE` was `YES` |
| `state` | string | Two-letter state code, `FEDERAL`, or `NATIONAL` |
| `active` | boolean | `true` if terminal HTTP status was 200 |
| `http_status` | integer | Terminal HTTP response code (or 0 for network failure) |
| `final_url` | string | Resolved URL after redirect chain |
| `robots_allowed` | boolean | `true` if crawl was permitted by robots.txt; `null` if unavailable |
| `robots_status` | string | `allowed`, `disallowed`, or `unavailable` |
| `js_rendered` | boolean | `true` if Playwright was used to render the page |
| `relevance_score` | integer | 0–100 Census relevance score |
| `matched_keywords` | string | Pipe-separated list of matched keywords |
| `datasets_found` | boolean | `true` if at least one downloadable file was detected |
| `dataset_urls` | string | Pipe-separated list of detected dataset URLs |
| `dataset_formats` | string | Pipe-separated deduplicated format list (e.g., `csv\|xlsx\|pdf`) |
| `crawl_depth_reached` | integer | Deepest hop level successfully crawled (0–2) |
| `portal_platform` | string | `Socrata`, `CKAN`, `ArcGIS Hub`, or empty string if not a portal |
| `portal_dataset_count` | integer | Total datasets enumerated via portal API; 0 for non-portal URLs |
| `portal_relevant_count` | integer | Datasets with relevance score > 0; 0 for non-portal URLs |
| `top_dataset_urls` | string | Pipe-separated list of up to 10 dataset URLs sorted by score; empty for non-portal URLs |
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
