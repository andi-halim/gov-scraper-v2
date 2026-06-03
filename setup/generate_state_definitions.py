#!/usr/bin/env python3
"""
generate_state_definitions.py — One-time setup script.

Extracts per-state Census of Governments vocabulary from config/2022ISD.pdf
and writes config/state_definitions.json.

Usage:
    python setup/generate_state_definitions.py --llm gemini   # auto-detects remaining states; prompts before starting
    python setup/generate_state_definitions.py --llm ollama   # requires Ollama running locally
    python setup/generate_state_definitions.py --llm gemini --force      # skip confirmation prompt
    python setup/generate_state_definitions.py --llm gemini --states AK  # process specific states only
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from dotenv import load_dotenv
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Canonical state name → two-letter USPS abbreviation
STATE_NAMES: dict[str, str] = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME",
    "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH",
    "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}

# Reverse lookup: abbreviation → full name
ABBREV_TO_NAME: dict[str, str] = {v: k for k, v in STATE_NAMES.items()}

PROMPT_TEMPLATE = """You are analyzing a section of the US Census Bureau's Individual State Descriptions (ISD) for {state_name}.

Your task: extract Census of Governments vocabulary specific to this state — the local names, equivalents, and terminology used for government unit types (county-equivalents, municipalities, townships, special districts, school districts).

Return ONLY valid JSON with no markdown fences:
{{
  "census_terms": ["term1", "term2"],
  "notes": "1-3 sentence summary of key government unit type facts for this state"
}}

Rules for census_terms:
- Lowercase strings only
- Singular form only — use "parish" not "parishes", "borough" not "boroughs", "district" not "districts"
- Include local equivalents (e.g. "parish" for Louisiana county-equivalents, "borough" for Alaska)
- Include all unit type names that differ from or extend Census standard terminology
- Exclude generic English words ("the", "and", "government", "local", "state")
- Exclude US state names and abbreviations

State section text:
{section_text}"""

# Max section text length sent to LLM.
# Gemini 2.5 Flash context window is 1M tokens; 12000 chars (~3000 tokens) is safe
# and avoids truncating relevant state-specific vocabulary from longer sections.
MAX_SECTION_CHARS = 12000


def extract_state_sections(pdf_path: str) -> dict[str, str]:
    """Return dict of state_abbrev → raw section text extracted from the ISD PDF."""
    try:
        import pdfplumber
    except ImportError:
        log.error("pdfplumber not installed. Run: pip install pdfplumber")
        sys.exit(1)

    log.info(f"Opening PDF: {pdf_path}")
    full_text_lines: list[str] = []

    with pdfplumber.open(pdf_path) as pdf:
        log.info(f"PDF has {len(pdf.pages)} pages — extracting text...")
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            full_text_lines.extend(text.split("\n"))
            if (i + 1) % 50 == 0:
                log.info(f"  {i + 1}/{len(pdf.pages)} pages processed")

    log.info("Splitting into per-state sections...")
    return _split_into_state_sections(full_text_lines)


def _split_into_state_sections(lines: list[str]) -> dict[str, str]:
    """Scan lines for state-name headers and collect text until the next header."""
    # Build set of uppercase state names for fast lookup
    upper_to_abbrev: dict[str, str] = {
        name.upper(): abbrev for name, abbrev in STATE_NAMES.items()
    }
    # Also match "STATE OF <NAME>" variants
    state_of_upper: dict[str, str] = {
        f"STATE OF {name.upper()}": abbrev for name, abbrev in STATE_NAMES.items()
    }
    upper_to_abbrev.update(state_of_upper)

    state_positions: list[tuple[int, str]] = []  # (line_index, abbrev)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        candidate = stripped.upper()
        if candidate in upper_to_abbrev:
            abbrev = upper_to_abbrev[candidate]
            # Avoid re-adding duplicates (keep first occurrence of each state)
            if not any(a == abbrev for _, a in state_positions):
                state_positions.append((i, abbrev))

    if not state_positions:
        log.warning("Exact state-header match found nothing; trying partial-line match.")
        state_positions = _fallback_section_detection(lines)

    log.info(f"Located {len(state_positions)} state section headers")

    sections: dict[str, str] = {}
    for idx, (line_i, abbrev) in enumerate(state_positions):
        end = state_positions[idx + 1][0] if idx + 1 < len(state_positions) else len(lines)
        text = "\n".join(lines[line_i:end]).strip()
        if len(text) > MAX_SECTION_CHARS:
            text = text[:MAX_SECTION_CHARS] + "\n[...truncated...]"
        sections[abbrev] = text

    return sections


def _fallback_section_detection(lines: list[str]) -> list[tuple[int, str]]:
    """Looser heuristic: short lines containing exactly a state name."""
    positions: list[tuple[int, str]] = []
    seen: set[str] = set()

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not 3 <= len(stripped) <= 40:
            continue
        for name, abbrev in STATE_NAMES.items():
            if abbrev in seen:
                continue
            if re.fullmatch(re.escape(name), stripped, re.IGNORECASE):
                positions.append((i, abbrev))
                seen.add(abbrev)
                break

    return positions


def _load_remaining_states(output_path: Path, abbrev_path: Path) -> list[str]:
    """Return abbreviations that still need processing, in state_abbrev.json order.

    A state is considered done when it has non-empty census_terms and notes that do
    not start with 'error:' or 'parse_error:'. Both absent and errored states are
    returned as remaining.
    """
    if not abbrev_path.exists():
        log.error("state_abbrev.json not found: %s", abbrev_path)
        sys.exit(1)
    with open(abbrev_path, encoding="utf-8") as fh:
        all_abbrevs: list[str] = json.load(fh)

    existing: dict = {}
    if output_path.exists():
        with open(output_path, encoding="utf-8") as fh:
            existing = json.load(fh)

    remaining = []
    for abbrev in all_abbrevs:
        entry = existing.get(abbrev)
        if entry is None:
            remaining.append(abbrev)
        elif not entry.get("census_terms"):
            remaining.append(abbrev)
        elif entry.get("notes", "").startswith(("error:", "parse_error:")):
            remaining.append(abbrev)
    return remaining


# ---------------------------------------------------------------------------
# LLM backends
# ---------------------------------------------------------------------------

_GEMINI_MAX_RETRIES = 3

# Reusable Gemini client — created once when first needed so we don't pay
# connection overhead on every state call.
_gemini_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        try:
            from google import genai
        except ImportError:
            log.error("google-genai not installed. Run: pip install google-genai")
            sys.exit(1)
        load_dotenv()
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            log.error("GEMINI_API_KEY environment variable is not set.")
            sys.exit(1)
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


def call_gemini(
    state_name: str,
    section_text: str,
    model: str = "gemini-2.5-flash",
    request_counter: Optional[list[int]] = None,
) -> tuple[dict, int]:
    """Call Gemini API and return ({"census_terms": [...], "notes": "..."}, attempts_used)."""
    try:
        from google.genai import errors as genai_errors, types as genai_types
    except ImportError:
        log.error("google-genai not installed. Run: pip install google-genai")
        sys.exit(1)

    client = _get_gemini_client()
    prompt = PROMPT_TEMPLATE.format(state_name=state_name, section_text=section_text)

    for attempt in range(1, _GEMINI_MAX_RETRIES + 1):
        if request_counter is not None:
            request_counter[0] += 1
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                    # Disable thinking — this is a structured extraction task that
                    # does not need chain-of-thought. Thinking is on by default for
                    # gemini-2.5-flash and adds significant compute overhead, causing
                    # 503 "high demand" errors on the free tier.
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            return _parse_llm_response((response.text or "").strip(), state_name), attempt
        except genai_errors.APIError as exc:
            retryable = exc.code in (429, 503)
            if not retryable or attempt == _GEMINI_MAX_RETRIES:
                raise
            m = re.search(r"retry_delay\s*\{\s*seconds:\s*(\d+)", str(exc))
            wait = int(m.group(1)) + 5 if m else (60 * attempt)
            log.warning(
                "  %d for %s (attempt %d/%d); sleeping %ds...",
                exc.code, state_name, attempt, _GEMINI_MAX_RETRIES, wait,
            )
            time.sleep(wait)

    # Unreachable: the final retry always raises; this satisfies the type checker.
    raise RuntimeError(f"call_gemini exhausted all retries for {state_name}")


def call_ollama(
    state_name: str,
    section_text: str,
    base_url: str = "http://localhost:11434",
    model: str = "llama3.2",
) -> dict:
    """Call local Ollama REST API and return {"census_terms": [...], "notes": "..."}."""
    import urllib.request
    import urllib.error

    prompt = PROMPT_TEMPLATE.format(state_name=state_name, section_text=section_text)
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
    }).encode()

    req = urllib.request.Request(
        f"{base_url}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama request failed ({base_url}): {exc}") from exc

    raw = data.get("response", "").strip()
    return _parse_llm_response(raw, state_name)


def _parse_llm_response(raw: str, state_name: str) -> dict:
    """Parse LLM JSON output; return safe defaults on failure."""
    # Strip markdown code fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE).strip()

    try:
        parsed = json.loads(cleaned)
        terms = [str(t).lower().strip() for t in parsed.get("census_terms", []) if t]
        notes = str(parsed.get("notes", "")).strip()
        return {"census_terms": terms, "notes": notes}
    except json.JSONDecodeError:
        log.warning(f"  JSON parse failed for {state_name}. Raw snippet: {raw[:120]!r}")
        return {"census_terms": [], "notes": f"parse_error: {raw[:80]}"}


# ---------------------------------------------------------------------------
# Main processing loop
# ---------------------------------------------------------------------------

def _write_result_immediately(output_path: Path, abbrev: str, result: dict) -> None:
    """Merge one state entry into output_path and flush to disk immediately."""
    existing: dict = {}
    if output_path.exists():
        with open(output_path, encoding="utf-8") as fh:
            existing = json.load(fh)
    existing[abbrev] = result
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, indent=2, ensure_ascii=False)
    log.info("  Persisted %s immediately to %s", abbrev, output_path)


def process_states(
    sections: dict[str, str],
    llm: str,
    ollama_url: str,
    ollama_model: str,
    gemini_model: str = "gemini-2.5-flash",
    max_requests: int = 0,
    request_counter: Optional[list[int]] = None,
    output_path: Optional[Path] = None,
) -> dict:
    """Run each state section through the selected LLM backend."""
    results: dict = {}
    total = len(sections)

    for i, (abbrev, section_text) in enumerate(sections.items(), 1):
        if max_requests and request_counter is not None and request_counter[0] >= max_requests:
            log.warning(
                "Daily request limit (%d) reached after %d state(s). "
                "Run again tomorrow to continue.",
                max_requests, i - 1,
            )
            break

        state_name = ABBREV_TO_NAME.get(abbrev, abbrev)
        log.info(f"[{i}/{total}] {state_name} ({abbrev})...")

        try:
            if llm == "gemini":
                result, attempts = call_gemini(
                    state_name, section_text,
                    model=gemini_model,
                    request_counter=request_counter,
                )
                results[abbrev] = result
                log.info(f"  → {len(result['census_terms'])} terms")
                if attempts == _GEMINI_MAX_RETRIES and output_path is not None:
                    _write_result_immediately(output_path, abbrev, result)
            else:
                result = call_ollama(state_name, section_text, base_url=ollama_url, model=ollama_model)
                results[abbrev] = result
                log.info(f"  → {len(result['census_terms'])} terms")

        except Exception as exc:
            log.error(f"  Error processing {state_name}: {exc}")
            error_result = {"census_terms": [], "notes": f"error: {exc}"}
            results[abbrev] = error_result
            # All retries were consumed before raising — persist immediately so
            # the error entry survives any subsequent crash or quota cutoff.
            if llm == "gemini" and output_path is not None:
                _write_result_immediately(output_path, abbrev, error_result)

        finally:
            if llm == "gemini":
                # Gemini free tier: ~15 RPM → sleep 10s between every request (success or failure)
                time.sleep(10)

    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate config/state_definitions.json from the Census ISD PDF."
    )
    parser.add_argument(
        "--llm",
        choices=["gemini", "ollama"],
        required=True,
        help="LLM backend to use.",
    )
    parser.add_argument(
        "--pdf",
        default="config/2022ISD.pdf",
        metavar="PATH",
        help="Path to the ISD PDF (default: config/2022ISD.pdf).",
    )
    parser.add_argument(
        "--output",
        default="config/state_definitions.json",
        metavar="PATH",
        help="Output path (default: config/state_definitions.json).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip the confirmation prompt.",
    )
    parser.add_argument(
        "--gemini-model",
        default="gemini-2.5-flash",
        help="Gemini model name (default: gemini-2.5-flash).",
    )
    parser.add_argument(
        "--states",
        metavar="XX,YY",
        help="Comma-separated state abbreviations to process (e.g. AL,CA,TX). "
             "Results are merged into existing output. When omitted, the script "
             "auto-detects remaining states and caps at --max-requests per run.",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=20,
        metavar="N",
        help="Maximum Gemini API calls when --states is omitted "
             "(default: 20, the free-tier RPD cap). Each retry attempt counts.",
    )
    parser.add_argument(
        "--ollama-url",
        default="http://localhost:11434",
        help="Ollama base URL (default: http://localhost:11434).",
    )
    parser.add_argument(
        "--ollama-model",
        default="llama3.2",
        help="Ollama model name (default: llama3.2).",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    abbrev_path = Path("config/state_abbrev.json")
    remaining: list[str] = []

    # --- Auto-detect mode (no --states): show progress summary and confirm ---
    if not args.states:
        remaining = _load_remaining_states(output_path, abbrev_path)
        with open(abbrev_path, encoding="utf-8") as fh:
            total_states = len(json.load(fh))
        done_count = total_states - len(remaining)

        if not remaining:
            log.info("All %d states are already complete. Nothing to do.", total_states)
            sys.exit(0)

        to_process = min(len(remaining), args.max_requests)
        print(f"\nProgress: {done_count}/{total_states} done, {len(remaining)} remaining.")
        print(
            f"Will process up to {to_process} state(s) this run "
            f"(--max-requests to change; each retry also counts against the limit)."
        )

        if not args.force:
            answer = input("Continue? [y/N] ").strip().lower()
            if answer != "y":
                log.info("Aborted.")
                sys.exit(0)

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        log.error(f"PDF not found: {pdf_path}")
        sys.exit(1)

    # T-11: extract per-state sections from PDF
    sections = extract_state_sections(str(pdf_path))
    if not sections:
        log.error("No state sections found in the PDF. Check the PDF format.")
        sys.exit(1)

    missing = sorted(set(STATE_NAMES.values()) - set(sections.keys()))
    if missing:
        log.warning(f"No section found for: {missing}")

    if args.states:
        # Manual mode: process exactly the requested states; no request cap.
        requested = [s.strip().upper() for s in args.states.split(",")]
        invalid = [s for s in requested if s not in set(STATE_NAMES.values())]
        if invalid:
            log.error("Unknown state abbreviations: %s", invalid)
            sys.exit(1)
        sections = {k: v for k, v in sections.items() if k in requested}
        log.info("Filtering to %d state(s): %s", len(sections), requested)
        request_counter: Optional[list[int]] = None
        effective_max = 0
    else:
        # Auto mode: filter to remaining states, capped at max_requests.
        available = [s for s in remaining if s in sections]
        no_section = [s for s in remaining if s not in sections]
        if no_section:
            log.warning("No PDF section found for: %s (skipping)", no_section)
        to_process_list = available[:args.max_requests]
        sections = {k: v for k, v in sections.items() if k in set(to_process_list)}
        log.info("Processing %d remaining state(s): %s", len(sections), to_process_list)
        request_counter = [0]
        effective_max = args.max_requests

    # T-12 / T-13: process each section through the LLM
    output_path.parent.mkdir(parents=True, exist_ok=True)
    results = process_states(
        sections, args.llm, args.ollama_url, args.ollama_model, args.gemini_model,
        max_requests=effective_max,
        request_counter=request_counter,
        output_path=output_path,
    )

    # T-14: always merge into existing output to preserve prior results
    existing: dict = {}
    if output_path.exists():
        with open(output_path, "r", encoding="utf-8") as fh:
            existing = json.load(fh)
    existing.update(results)
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, indent=2, ensure_ascii=False)

    log.info("Wrote %d state entries → %s", len(existing), output_path)

    empty = sorted(k for k, v in existing.items() if not v["census_terms"])
    if empty:
        log.warning("States with zero census_terms extracted: %s", empty)

    # Post-run summary for auto mode
    if not args.states:
        still_remaining = _load_remaining_states(output_path, abbrev_path)
        if still_remaining:
            log.info(
                "%d state(s) still remaining: %s. Run again tomorrow to continue.",
                len(still_remaining), still_remaining,
            )
        else:
            log.info("All states complete!")
        if request_counter is not None:
            log.info("Total Gemini API calls this run (including retries): %d", request_counter[0])


if __name__ == "__main__":
    main()
