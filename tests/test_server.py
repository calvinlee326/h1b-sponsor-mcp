import json

import pytest

from h1b_sponsor_mcp.server import (
    DATA_FILE,
    get_top_sponsors_by_state,
    normalize,
    search_sponsors,
    state_index,
)

pytestmark = pytest.mark.skipif(
    not DATA_FILE.exists(), reason="USCIS export not downloaded"
)


def test_normalize_collapses_case_and_punctuation():
    assert normalize("Amazon.com Services, LLC") == normalize("AMAZON COM SERVICES LLC")


def test_search_is_punctuation_insensitive():
    results = json.loads(search_sponsors("amazon.com"))["results"]
    assert any(r["employer"].startswith("AMAZON COM SERVICES") for r in results)


def test_search_merges_duplicate_spellings():
    top = json.loads(search_sponsors("amazon.com", limit=1))["results"][0]
    assert top["also_filed_as"], "expected AMAZON.COM variant merged into totals"


def test_search_ranks_exact_match_first():
    top = json.loads(search_sponsors("google llc", limit=1))["results"][0]
    assert top["employer"] == "GOOGLE LLC"


def test_search_rejects_empty_query():
    assert "error" in json.loads(search_sponsors("!!!"))


def test_limit_is_clamped():
    assert json.loads(search_sponsors("inc", limit=999))["returned"] <= 50


def test_state_ranking_is_descending():
    results = json.loads(get_top_sponsors_by_state("CA", limit=10))["results"]
    approvals = [r["approvals"] for r in results]
    assert approvals == sorted(approvals, reverse=True)


def test_state_code_is_case_and_space_insensitive():
    assert json.loads(get_top_sponsors_by_state(" tx "))["state"] == "TX"


def test_unknown_state_returns_valid_options():
    error = json.loads(get_top_sponsors_by_state("California"))
    assert "CA" in error["valid_states"]


def test_state_totals_sum_to_the_national_total():
    """The two indexes must agree: per-state figures partition the national one.

    Guards the bug this design exists to prevent — crediting an employer's
    company-wide total to every state it operates in.
    """
    key = normalize("GOOGLE LLC")
    per_state = sum(
        entry["approvals"]
        for entries in state_index().values()
        for entry in entries
        if normalize(entry["names"].most_common(1)[0][0]) == key
    )
    nationwide = json.loads(search_sponsors("google llc", limit=1))["results"][0]
    assert per_state == nationwide["total_approvals"]
