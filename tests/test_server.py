import json
from pathlib import Path

import pytest

import h1b_sponsor_mcp.server as server
from h1b_sponsor_mcp.server import (
    DATA_FILE,
    SAMPLE_DATA_FILE,
    clear_caches,
    employer_index,
    get_summary,
    get_top_sponsors_by_state,
    normalize,
    parse_int,
    search_sponsors,
    state_index,
)

requires_full_dataset = pytest.mark.skipif(
    not DATA_FILE.exists(), reason="USCIS export not downloaded"
)


@pytest.fixture()
def use_sample_data(monkeypatch: pytest.MonkeyPatch):
    """Point the server at the committed 40-row sample CSV and clear caches."""
    monkeypatch.setattr(server, "DATA_FILE", SAMPLE_DATA_FILE)
    clear_caches()
    yield
    clear_caches()


def test_normalize_collapses_case_and_punctuation():
    assert normalize("Amazon.com Services, LLC") == normalize("AMAZON COM SERVICES LLC")


def test_parse_int_handles_commas_and_junk():
    assert parse_int("1,269") == 1269
    assert parse_int(" 3,281 ") == 3281
    assert parse_int("42") == 42
    assert parse_int("") == 0
    assert parse_int("N/A") == 0


def test_sample_dataset_search_and_summary(use_sample_data: None):
    summary = json.loads(get_summary())
    assert summary["rows"] == 40
    assert summary["unique_employers"] == len(employer_index())
    assert summary["fiscal_years"] == ["2026"]

    res = json.loads(search_sponsors("3r info"))
    assert res["total_matches"] == 1
    top = res["results"][0]
    assert top["employer"] == "3R INFO LLC"
    assert top["total_approvals"] == 6
    assert (
        top["new_employment_approvals"]
        + top["change_of_employer_approvals"]
        + top["continuation_approvals"]
        == top["total_approvals"]
    )
    assert top["by_fiscal_year"]["2026"]["approvals"] == 6


def test_missing_data_file_returns_structured_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(server, "DATA_FILE", tmp_path / "missing.csv")
    clear_caches()
    try:
        for output in (
            search_sponsors("google"),
            get_top_sponsors_by_state("CA"),
            get_summary(),
        ):
            payload = json.loads(output)
            assert "error" in payload
            assert "hint" in payload
    finally:
        clear_caches()


def test_invalid_csv_headers_raise_value_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    bad_csv = tmp_path / "wrong_schema.csv"
    bad_csv.write_text("Employer\tState\tInitial Approval\nFoo\tCA\t1\n", encoding="utf-16")
    monkeypatch.setattr(server, "DATA_FILE", bad_csv)
    clear_caches()
    try:
        with pytest.raises(ValueError, match="missing expected columns"):
            server.load_rows()
    finally:
        clear_caches()


@requires_full_dataset
def test_search_is_punctuation_insensitive():
    results = json.loads(search_sponsors("amazon.com"))["results"]
    assert any(r["employer"].startswith("AMAZON COM SERVICES") for r in results)


@requires_full_dataset
def test_search_merges_duplicate_spellings():
    top = json.loads(search_sponsors("amazon.com", limit=1))["results"][0]
    assert top["also_filed_as"], "expected AMAZON.COM variant merged into totals"


@requires_full_dataset
def test_search_ranks_exact_match_first():
    top = json.loads(search_sponsors("google llc", limit=1))["results"][0]
    assert top["employer"] == "GOOGLE LLC"
    # Verify comma-formatted thousands cells (e.g. "1,269", "3,281") are counted
    assert top["total_approvals"] == 14096
    assert (
        top["new_employment_approvals"]
        + top["change_of_employer_approvals"]
        + top["continuation_approvals"]
        == top["total_approvals"]
    )
    assert (
        sum(fy["approvals"] for fy in top["by_fiscal_year"].values())
        == top["total_approvals"]
    )


@requires_full_dataset
def test_search_filters_by_fiscal_year():
    all_years = json.loads(search_sponsors("google llc", limit=1))["results"][0]
    fy_2026 = json.loads(
        search_sponsors("google llc", limit=1, fiscal_year="2026")
    )["results"][0]
    assert fy_2026["fiscal_years"] == ["2026"]
    assert fy_2026["total_approvals"] == all_years["by_fiscal_year"]["2026"]["approvals"]

    bad_fy = json.loads(search_sponsors("google llc", fiscal_year="1999"))
    assert "error" in bad_fy
    assert "2026" in bad_fy["valid_fiscal_years"]


@requires_full_dataset
def test_search_rejects_empty_query():
    assert "error" in json.loads(search_sponsors("!!!"))


@requires_full_dataset
def test_limit_is_clamped():
    assert json.loads(search_sponsors("inc", limit=999))["returned"] <= 50


@requires_full_dataset
def test_state_ranking_is_descending():
    results = json.loads(get_top_sponsors_by_state("CA", limit=10))["results"]
    approvals = [r["approvals"] for r in results]
    assert approvals == sorted(approvals, reverse=True)


@requires_full_dataset
def test_state_code_is_case_and_space_insensitive():
    assert json.loads(get_top_sponsors_by_state(" tx "))["state"] == "TX"


@requires_full_dataset
def test_unknown_state_returns_valid_options():
    error = json.loads(get_top_sponsors_by_state("California"))
    assert "CA" in error["valid_states"]


@requires_full_dataset
def test_summary_matches_employer_index():
    summary = json.loads(get_summary())
    assert summary["unique_employers"] == len(employer_index())
    assert summary["raw_employer_names"] >= summary["unique_employers"]


@requires_full_dataset
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
