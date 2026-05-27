# gov-scraper-v2

## What this project does

`gov-scraper-v2` crawls a curated and continuously growing list of US government-related URLs (from `config/urls.csv`) to assess each site's viability as a source of data consistent with the Census of Governments Individual State Descriptions. New URLs are added to the list over time; the pipeline handles any list size and supports delta runs that process only newly added URLs. For every URL it reports: whether the site is active, a 0–100 relevance score against Census vocabulary, and any downloadable datasets detected. Output is a single CSV. The tool must run entirely free — no paid APIs or services.

See [PRD.md](PRD.md) for full requirements.

---

## Config files

| File | Purpose |
|---|---|
| `config/urls.csv` | Seed URLs — a living document; new rows are appended over time. Only `WEB_ADDRESS` and `PRIORITY_RESOURCE` columns are used by the pipeline. `RESOURCE_NAME` is a human label — **never use it for inference**. |
| `config/keywords.csv` | Base vocabulary for relevance scoring. Used for all URLs. |
| `config/state_definitions.json` | Per-state Census vocabulary generated from the ISD PDF. Used to extend `keywords.csv` for state-tagged URLs. **Do not hand-edit.** |
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
| `--force` | off | Overwrite existing output without prompting |
| `--gemini-model NAME` | `gemini-2.5-flash` | Gemini model to use (Gemini backend only) |
| `--states XX,YY` | all states | Comma-separated abbreviations to process; useful for testing (e.g. `AL,CA,TX`) |
| `--ollama-url URL` | `http://localhost:11434` | Ollama API base URL |
| `--ollama-model NAME` | `llama3.2` | Ollama model to use |

**PDF library:** the script uses `pdfplumber` (not `pypdf`) for page-by-page text extraction.

**Rate limits:** the Gemini free tier is ~15 RPM. The script sleeps 4 seconds between every request (success or failure) and retries up to 3 times on 429 errors, respecting the `retry_delay` hint in the response. If the daily quota is exhausted, wait until midnight UTC and re-run with `--force`.

**Testing with a subset of states:** use `--states` to limit processing to specific states. A full run takes ~4 minutes (51 states × 4 s sleep) and consumes the daily free-tier quota. Use a small subset to verify output format before committing to a full run:

```bash
python setup/generate_state_definitions.py --llm gemini --states LA,AK --force
```

Only the listed abbreviations will be written to the output JSON. Any other states already in the file are overwritten (the output is always a complete replacement, not a merge).

`config/state_definitions.json` has already been generated and committed. Re-run only when a new ISD edition is published (approximately every 5 years).

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
- **State is auto-tagged from URL patterns first** (`*.state.XX.us`, `*.XX.gov` subdomain, full state name in domain), falling back to page `<title>`/H1 content. Unresolvable URLs are tagged `NATIONAL`; known federal agency domains are tagged `FEDERAL`.
- **Scorer is pluggable.** v1 uses keyword matching only. Future modes add sentence transformers (Mode 2), local Ollama LLM (Mode 3), or Gemini free tier (Mode 4) without changing the pipeline.
- **Output is written incrementally** (one CSV row appended per completed URL) so runs are crash-safe and resumable via `--resume`. Use `--new-only` for delta runs when new URLs are added to `urls.csv`.
- **robots.txt is fail-open.** If `robots.txt` is unreachable, a warning is logged and the crawl proceeds.

---

## Implementation status

| Phase | Description | Status |
|---|---|---|
| 0 | Scaffolding (`requirements.txt`, `__init__.py` files) | Done |
| 1 | `setup/generate_state_definitions.py` + `config/state_definitions.json` | Done |
| 2 | `crawler/http_client.py`, `crawler/robots.py` | Not started |
| 3 | `crawler/state_tagger.py` | Not started |
| 4 | Page fetcher + JS detection + `crawler/playwright_client.py` | Not started |
| 5 | Depth crawler (`crawler/orchestrator.py`) | Not started |
| 6 | Dataset detector (`crawler/dataset_detector.py`) | Not started |
| 7 | Relevance scorer (`scorer/keyword_loader.py`, `scorer/scorer.py`) | Not started |
| 8 | Input ingestion + priority queue (extends `crawler/orchestrator.py`) | Not started |
| 9 | Output writer + run modes (`reporter/writer.py`) | Not started |
| 10 | `run.py` entrypoint | Not started |

**Current state:** only the one-time PDF setup script exists. `run.py` does not exist yet — the "Running the crawler" commands above will fail until Phase 10 is complete.
