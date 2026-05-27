#!/usr/bin/env python3
"""
generate_state_definitions.py — One-time setup script.

Extracts per-state Census of Governments vocabulary from config/2022ISD.pdf
and writes config/state_definitions.json.

Usage:
    python setup/generate_state_definitions.py --llm gemini   # requires GEMINI_API_KEY
    python setup/generate_state_definitions.py --llm ollama   # requires Ollama running locally
    python setup/generate_state_definitions.py --llm gemini --force  # overwrite without prompt
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
- Include local equivalents (e.g. "parish" for Louisiana county-equivalents, "borough" for Alaska)
- Include all unit type names that differ from or extend Census standard terminology
- Include locally used plural forms when they appear (e.g. "parishes", "boroughs")
- Exclude generic English words ("the", "and", "government", "local", "state")
- Exclude US state names and abbreviations

State section text:
{section_text}"""

# Max section text length sent to LLM (avoid token overflow on large state sections)
MAX_SECTION_CHARS = 4000


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


# ---------------------------------------------------------------------------
# LLM backends
# ---------------------------------------------------------------------------

_GEMINI_MAX_RETRIES = 3


def call_gemini(state_name: str, section_text: str, model: str = "gemini-2.5-flash") -> dict:
    """Call Gemini API and return {"census_terms": [...], "notes": "..."}."""
    try:
        from google import genai
        from google.genai import errors as genai_errors, types as genai_types
    except ImportError:
        log.error("google-genai not installed. Run: pip install google-genai")
        sys.exit(1)

    load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.error("GEMINI_API_KEY environment variable is not set.")
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    prompt = PROMPT_TEMPLATE.format(state_name=state_name, section_text=section_text)

    for attempt in range(1, _GEMINI_MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                            disable=True
                        ),
                    ),
                )
            return _parse_llm_response((response.text or "").strip(), state_name)
        except genai_errors.ClientError as exc:
            if exc.code != 429 or attempt == _GEMINI_MAX_RETRIES:
                raise
            m = re.search(r"retry_delay\s*\{\s*seconds:\s*(\d+)", str(exc))
            wait = int(m.group(1)) + 5 if m else (60 * attempt)
            log.warning(
                "  429 for %s (attempt %d/%d); sleeping %ds...",
                state_name, attempt, _GEMINI_MAX_RETRIES, wait,
            )
            time.sleep(wait)


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

def process_states(
    sections: dict[str, str],
    llm: str,
    ollama_url: str,
    ollama_model: str,
    gemini_model: str = "gemini-2.5-flash",
) -> dict:
    """Run each state section through the selected LLM backend."""
    results: dict = {}
    total = len(sections)

    for i, (abbrev, section_text) in enumerate(sections.items(), 1):
        state_name = ABBREV_TO_NAME.get(abbrev, abbrev)
        log.info(f"[{i}/{total}] {state_name} ({abbrev})...")

        try:
            if llm == "gemini":
                result = call_gemini(state_name, section_text, model=gemini_model)
            else:
                result = call_ollama(state_name, section_text, base_url=ollama_url, model=ollama_model)

            results[abbrev] = result
            log.info(f"  → {len(result['census_terms'])} terms")

        except Exception as exc:
            log.error(f"  Error processing {state_name}: {exc}")
            results[abbrev] = {"census_terms": [], "notes": f"error: {exc}"}

        finally:
            if llm == "gemini":
                # Gemini free tier: ~15 RPM → sleep 4 s between every request (success or failure)
                time.sleep(4)

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
        help="Overwrite output file without prompting.",
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
             "Omit to process all states.",
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

    # T-15: guard against unintentional overwrite
    output_path = Path(args.output)
    if output_path.exists() and not args.force:
        answer = input(f"{output_path} already exists. Overwrite? [y/N] ").strip().lower()
        if answer != "y":
            log.info("Aborted. Pass --force to skip this prompt.")
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
        requested = [s.strip().upper() for s in args.states.split(",")]
        invalid = [s for s in requested if s not in set(STATE_NAMES.values())]
        if invalid:
            log.error("Unknown state abbreviations: %s", invalid)
            sys.exit(1)
        sections = {k: v for k, v in sections.items() if k in requested}
        log.info("Filtering to %d state(s): %s", len(sections), requested)

    # T-12 / T-13: process each section through the LLM
    results = process_states(sections, args.llm, args.ollama_url, args.ollama_model, args.gemini_model)

    # T-14: write output JSON
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, ensure_ascii=False)

    log.info(f"Wrote {len(results)} state entries → {output_path}")

    empty = sorted(k for k, v in results.items() if not v["census_terms"])
    if empty:
        log.warning(f"States with zero census_terms extracted: {empty}")
    else:
        log.info("All states have at least one census_term.")


if __name__ == "__main__":
    main()
