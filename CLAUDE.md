# gov-scraper-v2

## What this project does

`gov-scraper-v2` crawls a curated and continuously growing list of US government-related URLs (from `config/urls.csv`) to assess each site's viability as a source of data consistent with the Census of Governments Individual State Descriptions. New URLs are added to the list over time; the pipeline handles any list size and supports delta runs that process only newly added URLs. For every URL it reports: whether the site is active, a 0–100 relevance score against Census vocabulary, and any downloadable datasets detected. Output is a single CSV. The tool must run entirely free — no paid APIs or services.

See [PRD.md](PRD.md) for full requirements. See [TASKS.md](TASKS.md) for full breakdown of individual steps. See [NOTES.md](NOTES.md) for additional information regarding implementation notes for each phase.

---

## Config files

| File | Purpose |
|---|---|
| `config/urls.csv` | Seed URLs — a living document; new rows are appended over time. `WEB_ADDRESS`, `PRIORITY_RESOURCE`, and `STATE` columns are used by the pipeline. `RESOURCE_NAME` is a human label — **never use it for inference**. When adding new rows, populate `STATE` with a two-letter USPS abbreviation or `NATIONAL`. |
| `config/keywords.csv` | Base vocabulary for relevance scoring. Used for all URLs. |
| `config/state_definitions.json` | Per-state Census vocabulary generated from the ISD PDF. Used to extend `keywords.csv` for state-tagged URLs. **Do not hand-edit.** |
| `config/state_abbrev.json` | Ordered list of all 51 state/DC abbreviations. Used by the setup script to track completion progress. |
| `config/2022ISD.pdf` | Source document for `state_definitions.json`. Census Bureau publication G22-CG-ISD (339 pages, April 2024, reference year 2022). |

---

## Setup (run once)

```bash
pip install -r requirements.txt
playwright install chromium              # downloads the Playwright browser binary

python setup/generate_state_definitions.py --llm gemini   # requires GEMINI_API_KEY in .env
# or
python setup/generate_state_definitions.py --llm ollama   # requires Ollama running locally
```

**Gemini API key:** create a `.env` file in the project root with `GEMINI_API_KEY=<your key>`. The script loads it automatically via `python-dotenv`. `.env` is gitignored.

**Gemini SDK:** uses `google-genai` (the current unified SDK, `from google import genai`). The deprecated `google-generativeai` package is not used.

**Additional flags for `generate_state_definitions.py`:**

| Flag | Default | Purpose |
|---|---|---|
| `--pdf PATH` | `config/2022ISD.pdf` | Path to the ISD PDF |
| `--output PATH` | `config/state_definitions.json` | Output path |
| `--force` | off | Skip the confirmation prompt |
| `--gemini-model NAME` | `gemini-3.5-flash` | Gemini model to use (Gemini backend only) |
| `--states XX,YY` | *(auto-detect)* | Comma-separated abbreviations to process manually. Results are **merged** into existing output — unlisted states are preserved. |
| `--max-requests N` | `20` | Max Gemini API calls per run when `--states` is omitted (each retry counts). Matches the free-tier RPD cap. |
| `--ollama-url URL` | `http://localhost:11434` | Ollama API base URL |
| `--ollama-model NAME` | `llama3.2` | Ollama model to use |

**PDF library:** the script uses `pdfplumber` (not `pypdf`) for page-by-page text extraction.

**When `--states` is omitted**, the script automatically determines which states still need processing by comparing `config/state_abbrev.json` against the current `config/state_definitions.json`. It considers a state "remaining" if it is absent, has empty `census_terms`, or has an error/parse-error note. It then prompts for confirmation and stops after `--max-requests` API calls (default 20). Just run the same command each day until complete:

```bash
# Each day — script picks up where it left off, stops at 20 requests
python setup/generate_state_definitions.py --llm gemini
```

The script sleeps 10 seconds between every request and retries up to 3 times on 429/503 errors (each retry counts against the daily limit). At the end of each run it logs how many states are still remaining.

**When `--states` is provided**, the script processes exactly those states with no request cap — useful for retrying a specific failed state (e.g. `--states AK`) or overriding the auto-detect order. Results are always merged into the existing file.

`config/state_definitions.json` has already been partially generated. Re-run the script on successive days until all 51 states are complete. Re-run from scratch only when a new ISD edition is published (approximately every 5 years).

---

## Running the crawler

```bash
python run.py                        # fresh run, depth=2, 2s delay
python run.py --resume               # crash recovery: skip URLs already in this run's output
python run.py --new-only             # delta run: skip URLs present in any previous run's output
python run.py --depth 1 --delay 3   # override defaults
```

Output is written incrementally to `output/<YYYY-MM-DD>/results.csv`. Each dated directory is a self-contained run. `--new-only` reads all previous output directories to determine which URLs are genuinely new.

---

## Running tests

```bash
python -m pytest tests/                          # unit tests only (fast, no network)
RUN_INTEGRATION_TESTS=1 python -m pytest tests/test_integration_urls.py -v
```

Integration tests hit four live URLs (census.gov, data.cityofchicago.org, catalog.data.gov, opendata.dc.gov) and are skipped by default.

---

## Running the Streamlit UI

```bash
streamlit run app.py
```

`app.py` provides a two-page browser interface to explore and extend pipeline results without using the terminal. Requires `streamlit>=1.35.0` and `pandas>=2.0.0` (both in `requirements.txt`).

**Page 1 — Explorer:** Loads the most recently dated `output/<YYYY-MM-DD>/results.csv` into an interactive table. Sidebar filters: state (multiselect), status radio (`Active` / `All` / `Inactive` / `Errors & blocked`, default `Active`), a "null scores only" toggle that disables the score slider, relevance score range slider, keyword search (multiselect against `matched_keywords`, options drawn from `keywords.csv` + `state_definitions.json` census terms, same widget pattern as the Map tab), and portal platform multiselect. Default sort is descending score then descending dataset count. Selecting a row opens a drill-down panel with four metric tiles, an expander (`expanded=True` by default, still collapsible) showing matched keywords as a wrapping row of colored `st.badge` chips (`_render_keyword_chips()`), and the **complete** `dataset_urls` list as a clickable link table — loaded from the companion `dataset_urls.csv` via `_load_companion_datasets()` (falling back to the char-capped `results.csv` cell for older runs without a companion file).

**Page 2 — Scraper:** Accepts a URL, state, priority flag, crawl depth, and max-pages cap. Calls `run._process_url()` directly (no subprocess) and streams live log output from the `run`, `crawler`, and `scorer` loggers via a `st.status` container. Results display as metric tiles, the same auto-expanded matched-keywords badge-chip expander, and a dataset URL table. A "Add to most recent results.csv" button appends the row using `ReportWriter` in resume mode and disables after a successful write.

---

## The Census of Governments ISD — what you need to know

The **Individual State Descriptions** is the Census Bureau's reference document defining what counts as each of the five local government unit types in every US state:

> County · Municipal · Township · Special District · School District

Each state section describes which types exist in that state, what they are locally called, how they are formed, and how many exist.

**The key design implication:** terminology is not uniform across states. Several states use legally distinct names for the same Census category — for example, the county-equivalent is called a "parish" in Louisiana and a "borough" in Alaska, while some states have no functioning township governments at all. A generic keyword list without state context will systematically mis-score valid sites in these states.

`config/state_definitions.json` holds the authoritative per-state vocabulary derived from the ISD. The scorer uses it to extend the base keyword set for each state-tagged URL. This is vocabulary expansion for scoring purposes only — the tool does not classify or label sites by government unit type.

---

## Key architectural decisions

- **`RESOURCE_NAME` is never used for inference.** It is a manually maintained human label. Any logic that reads it would require ongoing maintenance and is explicitly out of scope.
- **URL/domain text does not contribute to the relevance score.** Score is derived from page title, headings, body text, and link anchor text only.
- **Government unit type classification is not in scope.** The tool answers "is this site Census-relevant?" not "what type of unit is this site?"
- **State is manually assigned via the `STATE` column in `config/urls.csv`.** Valid values are two-letter USPS abbreviations or `NATIONAL` (including federal agency URLs). The pipeline reads this column at ingestion time; rows with a missing `STATE` cell default to `NATIONAL`. There is no automatic state detection — the analyst fills in the column when adding new URLs.
- **Scorer is pluggable.** v1 uses keyword matching only. Future modes add sentence transformers (Mode 2), local Ollama LLM (Mode 3), or Gemini free tier (Mode 4) without changing the pipeline.
- **Output is written incrementally** (one CSV row appended per completed URL) so runs are crash-safe and resumable via `--resume`. Use `--new-only` for delta runs when new URLs are added to `urls.csv`.
- **robots.txt is fail-open.** If `robots.txt` is unreachable, a warning is logged and the crawl proceeds.
- **Open data portal detection uses a two-pass approach.** After the initial page fetch, passive HTML and header signals are checked first (zero extra requests). An active API probe fires only when passive detection is inconclusive. When a portal (Socrata/Tyler Technologies, CKAN, ArcGIS Hub) is detected, `portal_platform` is recorded, `relevance_score` is set to null, and the depth crawl and scorer are skipped. Portal adapter enumeration (dataset catalog via API) is future work.
- **JS-heavy pages are re-fetched with Playwright.** `crawler/http_client.py::_is_js_heavy()` flags a page when visible text < 200 chars or content-type is non-HTML; `fetch_page()` then calls `crawler/playwright_client.py::fetch_rendered()` for a headless Chromium render. Playwright failures fall back to plain HTML silently.
- **CDN bot-challenge bypass is a second Playwright path in `fetch_page()`.** `_is_bot_challenge(html, status, headers)` detects Cloudflare (cf-ray/Server header + 403, or body tokens), Akamai (AkamaiGHost + 403), and Azure WAF (body token) and retries with Playwright. The Playwright result is only accepted if the challenge signals are absent from the rendered HTML — if the challenge persists, the original status is kept and `fetch_page()` returns `cdn_blocked=True` as the 5th tuple element. CDN-blocked URLs receive `relevance_score=null` and a descriptive `error_notes` entry. Header-based signals are gated to 403 to avoid retrying legitimate CDN-proxied 200 responses. This is consistent with `robots.txt` compliance: the bypass fires only after `robots.txt` allows crawling; CDN blocks are heuristic layers, not the site's stated policy.
- **`relevance_score` is null (not 0) for any URL where scoring was impossible.** This covers: detected open data portals (depth crawl skipped), CDN-blocked URLs (page content unavailable), and network errors (fetch failed entirely). A score of 0 means the page was reachable but matched no keywords; null means no attempt was made.
- **`crawl_url()` accepts a `prefetched_seed` tuple** `(html, final_url, http_status, js_rendered, cdn_blocked)` to avoid re-fetching the seed URL. `run.py` always passes the result of its activity-check `fetch_page()` call as the prefetched seed. Without this, bot-bypassed URLs (where Playwright ran during the activity check) would re-trigger Playwright inside the depth crawler, and the second attempt often failed — causing the scorer to run on empty HTML and produce a score of 0. `crawl_url()` returns a 3-tuple `(pages, crawl_depth_reached, page_depths)` where `page_depths` maps each fetched page URL to its hop depth (seed=0).
- **`utils.py` holds shared text utilities.** `normalize_text(text) -> str` (NFC + diacritic strip + lowercase) is defined here and imported by `scorer/scorer.py`. Add any future cross-package text helpers here.
- **Scoring normalization uses `base_keyword_count()` as the denominator.** The normalization factor is fixed at the number of terms in `config/keywords.csv` (not `len(effective_keywords)`). State-specific terms extend the numerator (more matches possible) without inflating the denominator, so state-tagged URLs are not penalized for having a larger effective keyword set.
- **`dataset_urls` is ranked by relevance; the full list is never dropped.** Each candidate is scored: format tier (CSV/JSON/XLSX=3, XLS/XML=2, PDF=1) + Census keyword match in link anchor text (+2) + crawl depth proximity (seed=+2, depth-1=+1, depth-2=+0). `detect_datasets()` (receiving `effective_keywords` and `page_depths` from the caller) returns a 4-tuple `(found, dataset_urls, dataset_formats, dataset_links)`; `dataset_links` is the **complete uncapped ranked list** of `(url, format)` records. Every detected URL is written, one row per URL, to a normalized companion CSV `output/<date>/dataset_urls.csv` (columns `url, dataset_url, format, rank`) — the authoritative full set, keyed back to `results.csv` by `url`. `ReportWriter` opens and appends this companion in lockstep with `results.csv` (incl. on `--resume`). The per-row `format` comes straight from `dataset_links`, so a format resolved only via a Content-Disposition HEAD probe (i.e. not present in the URL) is preserved in the companion; it is blank only when genuinely unknown.
- **The `dataset_urls` cell in `results.csv` is char-capped, not count-capped.** `run.py::_cap_urls_by_chars()` keeps ranked URLs (most relevant first) until the pipe-joined string would exceed `_DATASET_CELL_CHAR_BUDGET = 32000` — under the Excel/Google Sheets 32,767-char cell limit so `results.csv` stays spreadsheet-safe. Two columns record the outcome: `dataset_urls_total` (all detected) and `dataset_urls_omitted` (dropped from the cell only — still present in the companion CSV). The char cap also keeps every cell well under Python's `csv` reader field-size limit (131,072 bytes), so `--resume`/`--new-only` reads of `results.csv` never hit it. `dataset_formats` reflects all detected formats.
- **Scoring text pools are independent.** `<h1>`–`<h3>` text contributes only to the heading pool (decomposed before body extraction). Anchor text contributes to both body and anchor pools. `<title>` is in `<head>` and is naturally excluded from body. Effective weights per keyword: heading-only 0.50, body-only 0.35, anchor-only 0.15, max 1.00 when all three match.
- **`config/keywords.csv` is a headerless single-column file.** One keyword or phrase per row, no header row. The loader uses `csv.reader` and reads `row[0]`. Do not add a header row.

