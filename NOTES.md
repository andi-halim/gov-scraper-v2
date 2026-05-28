## Implementation status

| Phase | Description | Status |
|---|---|---|
| 0 | Scaffolding (`requirements.txt`, `__init__.py` files) | Done |
| 1 | `setup/generate_state_definitions.py` + `config/state_definitions.json` | Done |
| 2 | `crawler/http_client.py`, `crawler/robots.py` | Done |
| 3 | `crawler/state_tagger.py` | Done |
| 4 | Page fetcher + JS detection + `crawler/playwright_client.py` | Not started |
| 4B | Open data portal detection (`crawler/portal_detector.py`, `portals/`) | Not started |
| 5 | Depth crawler (`crawler/orchestrator.py`) | Not started |
| 6 | Dataset detector (`crawler/dataset_detector.py`) | Not started |
| 7 | Relevance scorer (`scorer/keyword_loader.py`, `scorer/scorer.py`) | Not started |
| 8 | Input ingestion + priority queue (extends `crawler/orchestrator.py`) | Not started |
| 9 | Output writer + run modes (`reporter/writer.py`) | Not started |
| 10 | `run.py` entrypoint | Not started |

**Current state:** the one-time PDF setup script, HTTP/robots layer, and state tagger exist. `run.py` does not exist yet — the "Running the crawler" commands above will fail until Phase 10 is complete.

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

### Phase 3 implementation notes

**`crawler/state_tagger.py` — `StateTagger`**
- `tag(url, html="") -> str` resolves the six-priority chain from PRD §8; returns a two-letter state code, `"FEDERAL"`, or `"NATIONAL"`.
- **Priority 1** (`*.state.XX.us`): regex on hostname — `\.state\.([a-z]{2})\.us$`.
- **Priority 2** (`XX.gov` subdomain): checks `tldextract` `domain` field is exactly two letters and matches a state abbreviation. Known federal two-letter domains (e.g. `va.gov`) are explicitly excluded before this check fires.
- **Priority 3** (state name in domain): iterates all state names longest-first, compresses spaces (`"new mexico"` → `"newmexico"`), and checks for substring presence in the registered domain. This catches embedded names like `oregoncounties.org` → `OR` and `portsoflouisiana.org` → `LA`. Abbreviation-only domains (e.g. `alconservationdistricts.gov`) are not matched here and fall through to Priority 4.
- **Priority 4** (page content): scans `<title>` and first `<h1>` using BeautifulSoup; tries full state names first (longest-first), then abbreviations against original-case text to reduce false positives on common English words (`or`, `in`, `me`). Skipped if `html` is empty.
- **Priority 5** (federal domain list): checks registered domain against `FEDERAL_DOMAINS` — `hud.gov`, `epa.gov`, `census.gov`, `usda.gov`, `faa.gov`, `usa.gov`, `data.gov`, `va.gov`.
- **Priority 6**: returns `"NATIONAL"`.
- `STATE_NAME_TO_ABBREV`, `ABBREV_TO_STATE_NAME`, `STATE_ABBREVS`, and `FEDERAL_DOMAINS` are module-level constants available for import by other components.
