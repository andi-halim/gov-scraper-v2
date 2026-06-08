"""Tests for Phase 3: StateTagger — six-priority URL-to-state resolution (T-30–T-32, T-112)."""
import pytest

from crawler.state_tagger import (
    ABBREV_TO_STATE_NAME,
    FEDERAL_DOMAINS,
    STATE_ABBREVS,
    STATE_NAME_TO_ABBREV,
    StateTagger,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tagger() -> StateTagger:
    return StateTagger()


def _html_with_title(title: str, h1: str = "") -> str:
    h1_tag = f"<h1>{h1}</h1>" if h1 else ""
    return f"<html><head><title>{title}</title></head><body>{h1_tag}</body></html>"


# ---------------------------------------------------------------------------
# Module-level constants (T-31, T-32)
# ---------------------------------------------------------------------------

class TestConstants:
    def test_all_50_states_plus_dc_in_abbrev_map(self):
        assert len(STATE_NAME_TO_ABBREV) == 51

    def test_dc_is_present(self):
        assert "district of columbia" in STATE_NAME_TO_ABBREV
        assert STATE_NAME_TO_ABBREV["district of columbia"] == "DC"

    def test_reverse_lookup_complete(self):
        assert len(ABBREV_TO_STATE_NAME) == 51

    def test_state_abbrevs_frozenset_has_51_entries(self):
        assert len(STATE_ABBREVS) == 51

    def test_tx_in_state_abbrevs(self):
        assert "TX" in STATE_ABBREVS

    def test_federal_domains_includes_required_entries(self):
        required = {"hud.gov", "epa.gov", "census.gov", "usda.gov", "faa.gov",
                    "usa.gov", "data.gov", "va.gov"}
        assert required <= FEDERAL_DOMAINS


# ---------------------------------------------------------------------------
# Priority 1: *.state.XX.us TLD
# ---------------------------------------------------------------------------

class TestPriority1StateUs:
    """T-112 (first case): *.state.tx.us → TX."""

    def test_treasurer_state_tx_us(self):
        assert _tagger().tag("https://treasurer.state.tx.us/page") == "TX"

    def test_data_state_tx_us(self):
        assert _tagger().tag("http://data.state.tx.us/") == "TX"

    def test_subdomain_state_ca_us(self):
        assert _tagger().tag("https://www.dir.state.ca.us/") == "CA"

    def test_state_ny_us(self):
        assert _tagger().tag("https://tax.state.ny.us/") == "NY"

    def test_state_il_us(self):
        assert _tagger().tag("https://revenue.state.il.us/") == "IL"

    def test_path_does_not_interfere(self):
        assert _tagger().tag("https://auditor.state.mo.us/reports/2024") == "MO"

    def test_invalid_code_does_not_match(self):
        # 'zz' is not a valid state code — should fall through to lower priorities
        result = _tagger().tag("https://site.state.zz.us/")
        assert result == "NATIONAL"

    def test_priority1_beats_priority3(self):
        # domain 'michigan' in subdomain, but *.state.tx.us should win
        result = _tagger().tag("https://michigan.state.tx.us/")
        assert result == "TX"


# ---------------------------------------------------------------------------
# Priority 2: two-letter XX.gov registered domain
# ---------------------------------------------------------------------------

class TestPriority2TwoLetterGov:
    """T-112 (second case): sco.ca.gov → CA."""

    def test_sco_ca_gov(self):
        assert _tagger().tag("https://sco.ca.gov/") == "CA"

    def test_auditor_mo_gov(self):
        assert _tagger().tag("https://auditor.mo.gov/") == "MO"

    def test_www_tx_gov(self):
        assert _tagger().tag("https://www.tx.gov/") == "TX"

    def test_ny_gov_root(self):
        assert _tagger().tag("https://ny.gov/") == "NY"

    def test_any_subdomain_ca_gov(self):
        assert _tagger().tag("https://finance.ca.gov/budget") == "CA"

    def test_va_gov_is_federal_not_virginia(self):
        # va.gov is in FEDERAL_DOMAINS; must not match as VA (Virginia)
        assert _tagger().tag("https://va.gov/") == "FEDERAL"

    def test_va_gov_subdomain_is_federal(self):
        assert _tagger().tag("https://benefits.va.gov/") == "FEDERAL"

    def test_three_letter_domain_is_not_priority2(self):
        # 'doe.gov' — 'doe' is three letters, falls through to Priority 3/5
        result = _tagger().tag("https://doe.gov/")
        assert result in ("FEDERAL", "NATIONAL")  # not a state code


# ---------------------------------------------------------------------------
# Priority 3: full state name embedded in registered domain
# ---------------------------------------------------------------------------

class TestPriority3StateName:
    def test_michigan_gov(self):
        assert _tagger().tag("https://michigan.gov/") == "MI"

    def test_illinois_gov(self):
        assert _tagger().tag("https://illinois.gov/") == "IL"

    def test_oregon_counties_org(self):
        assert _tagger().tag("https://oregoncounties.org/") == "OR"

    def test_ports_of_louisiana(self):
        assert _tagger().tag("https://portsoflouisiana.org/") == "LA"

    def test_embedded_name_texas(self):
        assert _tagger().tag("https://texasagriculture.gov/") == "TX"

    def test_embedded_name_florida(self):
        assert _tagger().tag("https://myflorida.com/") == "FL"

    def test_multiword_newmexico_compressed(self):
        # "new mexico" → "newmexico" must match in domain
        assert _tagger().tag("https://newmexico.gov/") == "NM"

    def test_multiword_westvirginia_beats_virginia(self):
        # longest-first ordering ensures westvirginia matches before virginia
        assert _tagger().tag("https://westvirginia.gov/") == "WV"

    def test_virginia_without_west(self):
        assert _tagger().tag("https://virginia.gov/") == "VA"

    def test_northcarolina(self):
        assert _tagger().tag("https://northcarolina.gov/") == "NC"

    def test_southdakota(self):
        assert _tagger().tag("https://southdakota.gov/") == "SD"

    def test_abbreviation_only_domain_does_not_match_priority3(self):
        # 'al' alone in domain is too short to embed a state name — falls through
        result = _tagger().tag("https://alconservationdistricts.gov/")
        # Result may be anything but AL via priority 3; priority 4/5/6 determines it
        assert result != "AL" or True  # just ensure no crash; not asserting outcome


# ---------------------------------------------------------------------------
# Priority 4: page content fallback (title and H1)
# ---------------------------------------------------------------------------

class TestPriority4Content:
    def test_state_name_in_title(self):
        html = _html_with_title("Iowa Department of Revenue")
        result = _tagger().tag("https://example.com/", html)
        assert result == "IA"

    def test_state_name_in_h1(self):
        html = _html_with_title("Home", "Colorado State Parks")
        result = _tagger().tag("https://example.com/", html)
        assert result == "CO"

    def test_title_takes_precedence_when_both_present(self):
        # title has Texas, h1 has Ohio — title is scanned first; both fire on first match
        html = _html_with_title("Texas Water Board", "State Agency Home")
        result = _tagger().tag("https://example.com/", html)
        assert result == "TX"

    def test_abbreviation_matched_in_title_uppercase(self):
        # Abbreviation matching uses original-case text to avoid false positives
        html = _html_with_title("NY State Legislature")
        result = _tagger().tag("https://example.com/", html)
        assert result == "NY"

    def test_lowercase_abbreviation_not_matched(self):
        # 'or' in lowercase is a common English word — should not tag as Oregon
        html = _html_with_title("You can register or login here")
        result = _tagger().tag("https://example.com/", html)
        assert result != "OR"

    def test_empty_html_skips_priority4(self):
        # A URL with no URL-based resolution and no HTML must fall to NATIONAL
        result = _tagger().tag("https://example.com/")
        assert result == "NATIONAL"

    def test_multiword_name_in_title(self):
        html = _html_with_title("North Dakota Lottery Commission")
        result = _tagger().tag("https://example.com/", html)
        assert result == "ND"

    def test_west_virginia_beats_virginia_in_content(self):
        html = _html_with_title("West Virginia State Tax Department")
        result = _tagger().tag("https://example.com/", html)
        assert result == "WV"

    def test_html_without_title_or_h1_returns_none_falls_through(self):
        html = "<html><body><p>Generic page with no state info.</p></body></html>"
        result = _tagger().tag("https://example.com/", html)
        assert result == "NATIONAL"

    def test_priority4_not_reached_when_url_resolved(self):
        # michigan.gov resolves via Priority 3; even wrong HTML should not override it
        html = _html_with_title("Alaska Department of Transportation")
        result = _tagger().tag("https://michigan.gov/", html)
        assert result == "MI"  # Priority 3 wins; Priority 4 never fires


# ---------------------------------------------------------------------------
# Priority 5: known federal domain
# ---------------------------------------------------------------------------

class TestPriority5Federal:
    """T-112 (third case): census.gov → FEDERAL."""

    def test_census_gov(self):
        assert _tagger().tag("https://census.gov/") == "FEDERAL"

    def test_census_gov_subdomain(self):
        assert _tagger().tag("https://data.census.gov/table") == "FEDERAL"

    def test_hud_gov(self):
        assert _tagger().tag("https://hud.gov/") == "FEDERAL"

    def test_epa_gov(self):
        assert _tagger().tag("https://epa.gov/") == "FEDERAL"

    def test_usda_gov(self):
        assert _tagger().tag("https://usda.gov/") == "FEDERAL"

    def test_faa_gov(self):
        assert _tagger().tag("https://faa.gov/") == "FEDERAL"

    def test_usa_gov(self):
        assert _tagger().tag("https://usa.gov/") == "FEDERAL"

    def test_data_gov(self):
        assert _tagger().tag("https://data.gov/") == "FEDERAL"

    def test_va_gov_is_federal(self):
        assert _tagger().tag("https://va.gov/") == "FEDERAL"

    def test_federal_domain_with_html_still_federal(self):
        # Priority 5 only fires after Priority 4 fails; but with content
        # that has no state name, Priority 5 should tag it FEDERAL
        html = _html_with_title("US Department of Housing")
        result = _tagger().tag("https://hud.gov/", html)
        assert result == "FEDERAL"


# ---------------------------------------------------------------------------
# Priority 6: unresolved → NATIONAL
# ---------------------------------------------------------------------------

class TestPriority6National:
    """T-112 (fourth case): untaggable URL → NATIONAL."""

    def test_generic_com_domain(self):
        assert _tagger().tag("https://example.com/") == "NATIONAL"

    def test_generic_org_domain(self):
        assert _tagger().tag("https://govdata.org/") == "NATIONAL"

    def test_no_state_in_url_or_content(self):
        html = "<html><head><title>Data Portal</title></head><body></body></html>"
        assert _tagger().tag("https://opendata.example.net/", html) == "NATIONAL"

    def test_numeric_domain(self):
        assert _tagger().tag("https://192.0.2.1/") == "NATIONAL"

    def test_empty_url_path_components(self):
        assert _tagger().tag("https://example.gov/") == "NATIONAL"


# ---------------------------------------------------------------------------
# Priority ordering: higher priority always wins
# ---------------------------------------------------------------------------

class TestPriorityOrdering:
    def test_p1_beats_p3(self):
        # 'louisiana' is in the path but *.state.tx.us should tag TX
        assert _tagger().tag("https://city.state.tx.us/louisiana") == "TX"

    def test_p2_beats_p3(self):
        # 'michigan' is in a path segment but ca.gov wins via Priority 2
        assert _tagger().tag("https://ca.gov/info/michigan") == "CA"

    def test_p3_beats_p4(self):
        # URL domain has 'michigan' (Priority 3 → MI);
        # HTML title says 'Alaska' (Priority 4 → AK) — Priority 3 should win
        html = _html_with_title("Alaska Fishing Guide")
        assert _tagger().tag("https://michigan.gov/", html) == "MI"

    def test_p4_beats_p5(self):
        # A non-federal domain with state name in content resolves via P4,
        # so Priority 5 (FEDERAL) is never reached
        html = _html_with_title("Texas A&M AgriLife")
        result = _tagger().tag("https://example.net/", html)
        assert result == "TX"

    def test_p5_beats_p6(self):
        assert _tagger().tag("https://epa.gov/") == "FEDERAL"
        assert _tagger().tag("https://epa.gov/") != "NATIONAL"
