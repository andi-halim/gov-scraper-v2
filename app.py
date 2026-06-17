"""Streamlit UI for gov-scraper-v2.

Page 1 - Explorer: filter, sort, and drill into results.csv
Page 2 - Scraper:  run the full pipeline against a pasted URL
"""
import csv
import hashlib
import json
import logging
import sys
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import plotly.express as px
import streamlit as st

# Ensure project root is importable so pipeline modules resolve correctly
_ROOT = Path(__file__).parent.resolve()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Suppress verbose pipeline logging at app startup; re-enabled during scrapes
logging.basicConfig(level=logging.WARNING)
for _noisy in ("crawler", "scorer", "reporter", "playwright"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

_OUTPUT_ROOT = _ROOT / "output"
_STATE_ABBREV_PATH = _ROOT / "config" / "state_abbrev.json"

# Loggers to activate during on-demand scrapes for live output
_SCRAPER_LOG_NAMES = ["run", "crawler", "scorer"]

_HTTP_STATUS_LABELS: dict[int, str] = {
    0:   "Network failure — timeout, DNS error, or connection refused",
    200: "OK — page loaded successfully",
    301: "Moved Permanently — redirect followed automatically",
    302: "Found — redirect followed automatically",
    400: "Bad Request — the server couldn't understand the request",
    401: "Unauthorized — login required to access this page",
    403: "Forbidden — access denied (may be a CDN or firewall block)",
    404: "Not Found — the page doesn't exist at this URL",
    429: "Too Many Requests — the server is rate-limiting this crawler",
    500: "Internal Server Error — the server encountered an unexpected error",
    503: "Service Unavailable — the server is temporarily down or overloaded",
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def _load_most_recent_csv() -> tuple["pd.DataFrame | None", "Path | None"]:
    """Return (DataFrame, directory Path) for the most recently dated output dir."""
    if not _OUTPUT_ROOT.exists():
        return None, None
    dirs = sorted(
        d for d in _OUTPUT_ROOT.iterdir()
        if d.is_dir() and (d / "results.csv").exists()
    )
    if not dirs:
        return None, None
    most_recent = dirs[-1]
    df = pd.read_csv(most_recent / "results.csv", dtype=str, keep_default_na=False)
    return df, most_recent


@st.cache_data(ttl=60)
def _load_companion_datasets(csv_dir: "Path | None") -> dict:
    """Map each seed URL to its complete, rank-ordered dataset URL list.

    Reads the normalized companion CSV (dataset_urls.csv) written alongside results.csv.
    results.csv only carries a char-capped subset in its `dataset_urls` cell; the
    companion holds every detected URL. Returns an empty dict for older runs that
    predate the companion file.
    """
    if csv_dir is None:
        return {}
    path = Path(csv_dir) / "dataset_urls.csv"
    if not path.exists():
        return {}
    comp = pd.read_csv(path, dtype=str, keep_default_na=False)
    if comp.empty or "url" not in comp.columns or "dataset_url" not in comp.columns:
        return {}
    if "rank" in comp.columns:
        comp = comp.assign(_rank=pd.to_numeric(comp["rank"], errors="coerce")).sort_values("_rank")
    out: dict = {}
    for seed, dataset_url in zip(comp["url"], comp["dataset_url"]):
        if dataset_url.strip():
            out.setdefault(seed, []).append(dataset_url.strip())
    return out


@st.cache_data
def _load_all_keywords() -> list[str]:
    """Sorted deduplicated keyword list from keywords.csv + state_definitions.json."""
    terms: set[str] = set()
    kw_path = _ROOT / "config" / "keywords.csv"
    if kw_path.exists():
        with kw_path.open(encoding="utf-8", newline="") as f:
            for row in csv.reader(f):
                if row and row[0].strip():
                    terms.add(row[0].strip().lower())
    sd_path = _ROOT / "config" / "state_definitions.json"
    if sd_path.exists():
        sd = json.loads(sd_path.read_text(encoding="utf-8"))
        for entry in sd.values():
            for t in entry.get("census_terms", []):
                if t and t.strip():
                    terms.add(t.strip().lower())
    return sorted(terms, key=str.lower)


@st.cache_data
def _load_state_options() -> list[str]:
    """NATIONAL + all state abbreviations from config/state_abbrev.json."""
    base = ["NATIONAL"]
    if _STATE_ABBREV_PATH.exists():
        abbrevs: list[str] = json.loads(_STATE_ABBREV_PATH.read_text(encoding="utf-8"))
        return base + sorted(abbrevs)
    return base


def _infer_format(url: str) -> str:
    path = urlparse(url).path.lower().split("?")[0]
    for ext, fmt in [
        (".csv", "csv"), (".xlsx", "xlsx"), (".xls", "xls"),
        (".json", "json"), (".xml", "xml"), (".pdf", "pdf"),
    ]:
        if path.endswith(ext):
            return fmt
    return "?"


def _http_status_help(code) -> str:
    try:
        return _HTTP_STATUS_LABELS.get(int(code), f"HTTP {code}")
    except (TypeError, ValueError):
        return ""


def _render_dataset_urls(raw: str) -> None:
    """Display a pipe-separated dataset_urls string as a formatted table."""
    urls = [u.strip() for u in (raw or "").split("|") if u.strip()]
    if not urls:
        st.write("No dataset URLs detected.")
        return
    rows = [{"format": _infer_format(u), "url": u} for u in urls]
    st.dataframe(
        pd.DataFrame(rows),
        column_config={
            "format": st.column_config.TextColumn("Format", width="small"),
            "url": st.column_config.LinkColumn("URL"),
        },
        width='stretch',
        hide_index=True,
    )


_BADGE_COLORS = ["red", "orange", "yellow", "blue", "green", "violet", "gray"]


def _chip_color(keyword: str) -> str:
    """Deterministic badge color per keyword (stable across runs/PYTHONHASHSEED)."""
    digest = hashlib.md5(keyword.lower().encode("utf-8")).digest()
    return _BADGE_COLORS[digest[0] % len(_BADGE_COLORS)]


def _render_keyword_chips(keywords: list[str]) -> None:
    """Render matched keywords as a wrapping row of colored badge chips."""
    if not keywords:
        st.caption("No keywords matched.")
        return
    with st.container(horizontal=True):
        for kw in sorted(keywords, key=str.lower):
            st.badge(kw, color=_chip_color(kw))


def _render_result_metrics(result: dict) -> None:
    """Render the four top-level metric tiles for a result dict."""
    col_a, col_b, col_c, col_d = st.columns(4)
    active_val = result.get("active")
    col_a.metric("Active", "yes" if active_val in (True, "true") else "no")
    http_code = result.get("http_status", 0)
    col_b.metric("HTTP status", http_code, help=_http_status_help(http_code))
    score = result.get("relevance_score")
    col_c.metric("Relevance score", score if score is not None else "—")
    col_d.metric("Crawl depth", result.get("crawl_depth_reached", 0))


def _result_dataset_str(result: dict) -> str:
    """Normalise dataset URLs to a pipe string, preferring the full uncapped list.

    A fresh in-process scrape carries the complete ranked list in `dataset_links` as
    (url, format) tuples; fall back to the char-capped `dataset_urls` cell (list or pipe
    string) for CSV-sourced rows.
    """
    links = result.get("dataset_links")
    if links:
        return "|".join(url for url, _fmt in links)
    val = result.get("dataset_urls", [])
    if isinstance(val, list):
        return "|".join(val)
    return str(val) if val else ""


class _UILogHandler(logging.Handler):
    """Logging handler that streams pipeline output to a Streamlit placeholder."""

    def __init__(self, placeholder):
        super().__init__(level=logging.INFO)
        self._placeholder = placeholder
        self._lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        name = record.name.split(".")[-1]
        self._lines.append(f"[{name}] {record.getMessage()}")
        self._placeholder.code("\n".join(self._lines[-25:]), language=None)


# ---------------------------------------------------------------------------
# Page 1 — Explorer
# ---------------------------------------------------------------------------

def page_explorer() -> None:
    st.title("State Resources Explorer")

    col_refresh, col_info = st.columns([1, 8])
    with col_refresh:
        if st.button("Refresh"):
            _load_most_recent_csv.clear()
            st.rerun()

    df, csv_dir = _load_most_recent_csv()

    if df is None:
        st.warning(
            "No results found. Run `python run.py` first, "
            "or check that the `output/` directory exists."
        )
        return

    assert csv_dir is not None  # guarded by df is None check above
    with col_info:
        st.caption(f"Loaded: `{csv_dir / 'results.csv'}` — {len(df):,} rows")

    # ---- sidebar filters ----
    with st.sidebar:
        st.header("Filters")

        states = sorted(df["state"].dropna().unique().tolist())
        sel_states = st.multiselect("State", states)

        status_filter = st.radio(
            "Status",
            ["Active", "All", "Inactive", "Errors & blocked"],
            index=0,
            horizontal=True,
            help=(
                "Active: crawled successfully | "
                "Inactive: got a response but not HTTP 200 | "
                "Errors & blocked: network failure, CDN block, SSL error, etc."
            ),
        )

        null_score_only = st.checkbox("Unscored only (null relevance score)")
        score_range = st.slider(
            "Relevance score", 0, 100, (0, 100), disabled=null_score_only
        )

        all_keywords = _load_all_keywords()
        sel_keywords = st.multiselect(
            "Keyword search",
            options=all_keywords,
            placeholder="Search keywords…",
            help="Select one or more keywords — rows matching ANY selected term are shown.",
        )

        # Portal platform last (feedback #6)
        portals_raw = sorted(set(df["portal_platform"].dropna().tolist()))
        portals_with_none = (
            (["(none)"] if "" in portals_raw else []) + [p for p in portals_raw if p]
        )
        sel_portals = st.multiselect("Portal platform", portals_with_none)

    # ---- apply filters ----
    mask = pd.Series([True] * len(df), index=df.index)

    if sel_states:
        mask &= df["state"].isin(sel_states)

    has_error = df["error_notes"].str.strip() != ""
    if status_filter == "Active":
        mask &= (df["active"] == "true") & ~has_error
    elif status_filter == "Inactive":
        mask &= (df["active"] == "false") & ~has_error
    elif status_filter == "Errors & blocked":
        mask &= has_error

    numeric_score = pd.to_numeric(df["relevance_score"], errors="coerce")
    if null_score_only:
        mask &= numeric_score.isna()
    elif score_range != (0, 100):
        lo, hi = score_range
        if lo == 0:
            score_mask = numeric_score.isna() | ((numeric_score >= lo) & (numeric_score <= hi))
        else:
            score_mask = (numeric_score >= lo) & (numeric_score <= hi)
        mask &= score_mask

    if sel_portals:
        include_none = "(none)" in sel_portals
        real_portals = [p for p in sel_portals if p != "(none)"]
        if include_none and real_portals:
            portal_mask = (df["portal_platform"] == "") | df["portal_platform"].isin(real_portals)
        elif include_none:
            portal_mask = df["portal_platform"] == ""
        else:
            portal_mask = df["portal_platform"].isin(real_portals)
        mask &= portal_mask

    if sel_keywords:
        lower_terms = {t.lower() for t in sel_keywords}
        def _any_match(cell: str) -> bool:
            parts = {p.strip().lower() for p in cell.split("|") if p.strip()}
            return bool(parts & lower_terms)
        mask &= df["matched_keywords"].apply(_any_match)

    filtered = df[mask].copy()

    # Default sort: score desc (nulls last), then dataset count desc
    filtered["_score_sort"] = pd.to_numeric(filtered["relevance_score"], errors="coerce")
    if "dataset_urls_total" in filtered.columns:
        # Accurate full count from the companion-backed column (cell is char-capped).
        filtered["_dataset_sort"] = pd.to_numeric(
            filtered["dataset_urls_total"], errors="coerce"
        ).fillna(0)
    else:
        filtered["_dataset_sort"] = (
            filtered["dataset_urls"]
            .str.split("|")
            .apply(lambda parts: sum(1 for p in parts if p.strip()) if isinstance(parts, list) else 0)
        )
    filtered = filtered.sort_values(
        ["_score_sort", "_dataset_sort"], ascending=[False, False], na_position="last"
    ).drop(columns=["_score_sort", "_dataset_sort"])

    # ---- build display DataFrame ----
    display_cols = [
        "url", "priority", "state", "active", "relevance_score",
        "datasets_found", "dataset_formats", "dataset_urls", "matched_keywords",
        "crawl_depth_reached", "portal_platform", "error_notes",
    ]
    display_df = filtered[display_cols].copy().reset_index(drop=True)
    display_df["relevance_score"] = pd.to_numeric(display_df["relevance_score"], errors="coerce")
    for _bool_col in ("priority", "active", "datasets_found"):
        display_df[_bool_col] = display_df[_bool_col].str.lower() == "true"

    st.write(f"**{len(filtered):,}** of {len(df):,} rows shown")

    selection = st.dataframe(
        display_df,
        width="stretch",
        height=388,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "url": st.column_config.LinkColumn("URL"),
            "priority": st.column_config.CheckboxColumn("Priority", width="small"),
            "state": st.column_config.TextColumn("State", width="small"),
            "active": st.column_config.CheckboxColumn("Active", width="small"),
            "relevance_score": st.column_config.NumberColumn("Score", format="%d"),
            "datasets_found": st.column_config.CheckboxColumn("Datasets?", width="small"),
            "dataset_formats": st.column_config.TextColumn("Formats"),
            "dataset_urls": st.column_config.TextColumn("Dataset URLs"),
            "matched_keywords": st.column_config.TextColumn("Matched Keywords"),
            "crawl_depth_reached": st.column_config.TextColumn("Depth", width="small"),
            "portal_platform": st.column_config.TextColumn("Portal"),
            "error_notes": st.column_config.TextColumn("Error Notes", width="large"),
        },
    )

    # ---- drill-down panel ----
    rows_selected = selection.selection["rows"]
    if not rows_selected:
        return

    idx = rows_selected[0]
    original_idx = filtered.index[idx]
    row_dict: dict = df.loc[original_idx].to_dict()

    st.divider()
    st.subheader("Row detail")

    _render_result_metrics(row_dict)

    final_url = str(row_dict.get("final_url", ""))
    if final_url and final_url != str(row_dict.get("url", "")):
        st.caption(f"Resolved URL: {final_url}")
    if str(row_dict.get("portal_platform", "")):
        st.info(f"Open data portal: **{row_dict['portal_platform']}**")
    if str(row_dict.get("error_notes", "")):
        st.warning(f"Error: {row_dict['error_notes']}")

    kws = str(row_dict.get("matched_keywords", ""))
    tags = [k.strip() for k in kws.split("|") if k.strip()]
    with st.expander("Matched keywords", expanded=True):
        _render_keyword_chips(tags)

    # Prefer the complete list from the companion CSV; fall back to the capped cell.
    companion = _load_companion_datasets(csv_dir)
    full_list = companion.get(str(row_dict.get("url", "")))
    if full_list:
        raw_datasets = "|".join(full_list)
        n_datasets = len(full_list)
    else:
        raw_datasets = str(row_dict.get("dataset_urls", ""))
        n_datasets = len([u for u in raw_datasets.split("|") if u.strip()])
    with st.expander(f"Dataset URLs ({n_datasets} found)", expanded=n_datasets > 0):
        _render_dataset_urls(raw_datasets)


# ---------------------------------------------------------------------------
# Page 2 — On-demand Scraper
# ---------------------------------------------------------------------------

def page_scraper() -> None:
    st.title("On-demand Scraper")
    st.caption(
        "Paste any URL to run the full gov-scraper-v2 pipeline. "
        "Results can be added to the most recent `results.csv`."
    )

    state_options = _load_state_options()

    with st.form("scraper_form"):
        url_input = st.text_input("URL", placeholder="https://www.example.gov/")
        col1, col2 = st.columns(2)
        with col1:
            state_input = st.selectbox("State tag", state_options, index=0)
            priority_input = st.checkbox("Priority resource", value=False)
        with col2:
            depth_input = st.slider("Crawl depth", min_value=1, max_value=2, value=2)
            max_pages_input = st.number_input(
                "Max pages",
                min_value=5, max_value=75, value=50, step=5,
                help="Lower = faster but may miss dataset URLs. 25 caps worst-case at ~50 seconds.",
            )
        submitted = st.form_submit_button("Run Scraper")

    if submitted:
        if not url_input.strip():
            st.error("Please enter a URL.")
            return

        st.session_state.pop("scrape_result", None)
        st.session_state.pop("result_saved", None)

        try:
            from run import _process_url
            from crawler.http_client import HttpClient
            from crawler.robots import RobotsChecker
            from crawler.portal_detector import PortalDetector
        except ImportError as exc:
            st.error(
                f"Could not import pipeline modules: {exc}\n\n"
                "Make sure you launch Streamlit from the project root: "
                "`streamlit run app.py`"
            )
            return

        # Live log output via st.status (feedback #3)
        with st.status("Crawling…", expanded=True) as status:
            log_placeholder = st.empty()
            handler = _UILogHandler(log_placeholder)

            saved_levels: dict[str, int] = {}
            for name in _SCRAPER_LOG_NAMES:
                lgr = logging.getLogger(name)
                saved_levels[name] = lgr.level
                lgr.setLevel(logging.INFO)
                lgr.addHandler(handler)

            try:
                with HttpClient(delay=2.0) as http_client:
                    robots = RobotsChecker(http_client)
                    portal = PortalDetector(http_client)
                    result = _process_url(
                        url=url_input.strip(),
                        priority=priority_input,
                        state=state_input,
                        http_client=http_client,
                        robots_checker=robots,
                        portal_detector=portal,
                        depth=depth_input,
                        max_pages=int(max_pages_input),
                    )
                st.session_state["scrape_result"] = result
                status.update(label="Crawl complete", state="complete", expanded=False)
            except Exception as exc:
                status.update(label=f"Crawl failed: {exc}", state="error")
                st.error(f"Scrape failed: {exc}")
                return
            finally:
                for name in _SCRAPER_LOG_NAMES:
                    lgr = logging.getLogger(name)
                    lgr.removeHandler(handler)
                    lgr.setLevel(saved_levels[name])

    result = st.session_state.get("scrape_result")
    if result is None:
        return

    # ---- result display ----
    st.divider()
    st.subheader("Result")

    _render_result_metrics(result)

    # HTTP status plain-English explanation (feedback #2)
    http_code = result.get("http_status", 0)
    explanation = _http_status_help(http_code)
    if explanation:
        st.caption(f"**HTTP {http_code}:** {explanation}")

    final_url = result.get("final_url", "")
    if final_url and final_url != result.get("url", ""):
        st.caption(f"Resolved URL: {final_url}")

    if result.get("portal_platform"):
        st.info(
            f"Open data portal detected: **{result['portal_platform']}** "
            "— depth crawl and scoring were skipped."
        )
    if result.get("error_notes"):
        st.warning(f"Error: {result['error_notes']}")

    kws = result.get("matched_keywords", [])
    kws_list = (
        kws if isinstance(kws, list)
        else [k.strip() for k in str(kws).split("|") if k.strip()]
    )
    with st.expander("Matched keywords", expanded=True):
        _render_keyword_chips(kws_list)

    dataset_str = _result_dataset_str(result)
    n_datasets = len([u for u in dataset_str.split("|") if u.strip()])
    with st.expander(f"Dataset URLs ({n_datasets} found)", expanded=n_datasets > 0):
        _render_dataset_urls(dataset_str)

    # ---- save to CSV ----
    st.divider()
    if st.session_state.get("result_saved"):
        _, csv_dir = _load_most_recent_csv()
        saved_path = (csv_dir / "results.csv") if csv_dir else "results.csv"
        st.success(f"Added to `{saved_path}`")
    else:
        if st.button("Add to most recent CSV"):
            _, csv_dir = _load_most_recent_csv()
            if csv_dir is None:
                st.error(
                    "No existing results.csv found. "
                    "Run `python run.py` at least once to create the output directory."
                )
            else:
                try:
                    from reporter.writer import ReportWriter
                    with ReportWriter(csv_dir) as writer:
                        writer.open(resume=True)
                        writer.append_row(result)
                    st.session_state["result_saved"] = True
                    _load_most_recent_csv.clear()
                    st.rerun()
                except Exception as exc:
                    st.error(f"Failed to save: {exc}")


# ---------------------------------------------------------------------------
# Page 3 — State Map
# ---------------------------------------------------------------------------

_COLOR_METRICS = {
    "Active URLs with datasets": "active_with_datasets",
    "Avg relevance score": "avg_score",
    "Total active URLs": "active_count",
}

_NON_STATE_TAGS = {"NATIONAL"}


def page_map() -> None:
    st.title("State Map")

    df, csv_dir = _load_most_recent_csv()

    if df is None:
        st.warning(
            "No results found. Run `python run.py` first, "
            "or check that the `output/` directory exists."
        )
        return

    assert csv_dir is not None
    st.caption(f"Loaded: `{csv_dir / 'results.csv'}` — {len(df):,} rows")

    # ---- sidebar controls ----
    with st.sidebar:
        st.header("Map filters")

        all_keywords = _load_all_keywords()
        sel_keywords = st.multiselect(
            "Keyword filter",
            options=all_keywords,
            placeholder="Search keywords…",
            help="Select one or more keywords — only URLs matching ANY selected term count toward state stats.",
        )

        color_label = st.selectbox("Color states by", list(_COLOR_METRICS.keys()), index=0)
        color_col = _COLOR_METRICS[color_label]

        non_state = df[df["state"].isin(_NON_STATE_TAGS)]
        if len(non_state):
            st.caption(
                f"{len(non_state):,} NATIONAL URL(s) excluded from map."
            )

    # ---- filter to state-tagged rows ----
    state_df = df[~df["state"].isin(_NON_STATE_TAGS)].copy()

    # ---- apply keyword filter ----
    if sel_keywords:
        lower_terms = {t.lower() for t in sel_keywords}
        def _any_match(cell: str) -> bool:
            parts = {p.strip().lower() for p in cell.split("|") if p.strip()}
            return bool(parts & lower_terms)
        kw_mask = state_df["matched_keywords"].apply(_any_match)
        state_df = state_df[kw_mask].copy()

    if state_df.empty:
        st.info("No state-tagged rows match the selected keywords. Try removing some selections.")
        return

    # ---- aggregate per state ----
    numeric_score = pd.to_numeric(state_df["relevance_score"], errors="coerce")
    state_df = state_df.copy()
    state_df["_active"] = state_df["active"].str.lower() == "true"
    state_df["_datasets"] = state_df["datasets_found"].str.lower() == "true"
    state_df["_priority"] = state_df["priority"].str.lower() == "true"
    state_df["_score"] = numeric_score

    agg = state_df.groupby("state").agg(
        active_with_datasets=("_active", lambda s: int((s & state_df.loc[s.index, "_datasets"]).sum())),
        active_count=("_active", "sum"),
        total_urls=("url", "count"),
        priority_count=("_priority", "sum"),
        avg_score=("_score", lambda s: round(s.dropna().mean(), 1) if s.dropna().size else None),
    ).reset_index()

    agg["active_count"] = agg["active_count"].astype(int)
    agg["priority_count"] = agg["priority_count"].astype(int)
    agg["has_priority"] = agg["priority_count"].apply(lambda n: "Yes" if n > 0 else "No")
    agg["avg_score_display"] = agg["avg_score"].apply(
        lambda v: str(v) if v is not None and not pd.isna(v) else "—"
    )

    # ---- build figure ----
    color_range = (
        [0, max(agg[color_col].fillna(0).max(), 1)]
        if color_col != "avg_score"
        else [0, 100]
    )

    fig = px.choropleth(
        agg,
        locations="state",
        locationmode="USA-states",
        color=color_col,
        scope="usa",
        color_continuous_scale="Blues",
        range_color=color_range,
        labels={color_col: color_label},
        custom_data=["state", "active_with_datasets", "has_priority",
                     "priority_count", "avg_score_display", "active_count", "total_urls"],
    )

    fig.update_traces(
        hovertemplate=(
            "<b>%{customdata[0]}</b><br>"
            "Active URLs with datasets: %{customdata[1]}<br>"
            "Priority URL: %{customdata[2]} (%{customdata[3]})<br>"
            "Avg relevance score: %{customdata[4]}<br>"
            "Total active URLs: %{customdata[5]}<br>"
            "Total URLs in state: %{customdata[6]}"
            "<extra></extra>"
        )
    )

    fig.update_layout(
        margin={"r": 0, "t": 0, "l": 0, "b": 0},
        coloraxis_colorbar={"title": color_label},
        geo={"bgcolor": "rgba(0,0,0,0)"},
    )

    st.plotly_chart(fig, width='stretch')

    # ---- summary table below map ----
    with st.expander("State summary table", expanded=False):
        display_agg = agg[
            ["state", "active_with_datasets", "active_count", "total_urls",
             "has_priority", "priority_count", "avg_score_display"]
        ].copy()
        display_agg.columns = [
            "State", "Active w/ Datasets", "Active URLs",
            "Total URLs", "Has Priority", "Priority Count", "Avg Score",
        ]
        st.dataframe(
            display_agg.sort_values("Active w/ Datasets", ascending=False).reset_index(drop=True),
            hide_index=True,
            width='stretch',
        )


# ---------------------------------------------------------------------------
# App entry point
# ---------------------------------------------------------------------------

def _main() -> None:
    st.set_page_config(
        page_title="gov-scraper-v2",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    with st.sidebar:
        page = st.radio("Navigation", ["Explorer", "Scraper", "Map"], label_visibility="collapsed")
        st.divider()

    if page == "Explorer":
        page_explorer()
    elif page == "Scraper":
        page_scraper()
    else:
        page_map()


# `streamlit run app.py` executes this module as the main script (__name__ == "__main__"),
# so the UI still launches normally. Guarding the call also lets the module be imported by
# tests/tools (e.g. to exercise the helper functions) without rendering the whole app.
if __name__ == "__main__":
    _main()
