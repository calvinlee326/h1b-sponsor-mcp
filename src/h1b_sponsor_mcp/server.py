"""H-1B sponsor search MCP server.

An MCP server exposes capabilities to an AI client (Claude Desktop, MCP
Inspector, ...) over JSON-RPC. There are three primitives:

  - tool     : model-controlled action with typed arguments  (~ a POST endpoint)
  - resource : application-controlled read-only data at a URI (~ a GET endpoint)
  - prompt   : user-invoked prompt template                   (~ a saved query)

The client first asks "what do you have?", and the server answers with names,
descriptions and JSON schemas. That discovery step is why type hints and
docstrings below are not decoration: they ARE the API contract the model reads.
"""

import csv
import json
import os
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

# In mcp >= 2.0 the high-level API class is `MCPServer`. It was called
# `FastMCP` (mcp.server.fastmcp) before the 2.0 rename, so older tutorials
# will not import. The low-level protocol API still lives in
# mcp.server.lowlevel.Server; MCPServer is built on top of it and infers tool
# schemas from type hints, the way DRF infers a serializer from a model.
from mcp.server import MCPServer

# Resolve the data file relative to THIS file, never relative to the process
# working directory. Claude Desktop launches the server with cwd="/", so any
# relative path would break there while still working in local tests.
# src/h1b_sponsor_mcp/server.py -> parents[2] is the project root.
DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DATA_FILE = Path(os.environ.get("H1B_DATA_FILE", DATA_DIR / "h1b_employers.csv"))
SAMPLE_DATA_FILE = DATA_DIR / "h1b_employers.sample.csv"

# Column names as they appear in the USCIS "Employer Information" export.
# These differ from the annual bulk export: it uses "Employer" / "State" /
# "Initial Approval", this file uses the longer names below. Naming them once
# here means a future schema change is a one-line edit, not a grep.
EMPLOYER = "Employer (Petitioner) Name"
STATE = "Petitioner State"
CITY = "Petitioner City"
FISCAL_YEAR = "Fiscal Year"

# USCIS splits petitions into six categories rather than the two
# (initial/continuing) that the annual export uses. A company's real
# sponsorship footprint is the sum across all of them.
NEW_EMPLOYMENT_APPROVAL = "New Employment Approval"
CHANGE_OF_EMPLOYER_APPROVAL = "Change of Employer Approval"
CONTINUATION_APPROVAL_COLUMNS = (
    "Continuation Approval",
    "Change with Same Employer Approval",
    "New Concurrent Approval",
    "Amended Approval",
)
APPROVAL_COLUMNS = (
    NEW_EMPLOYMENT_APPROVAL,
    "Continuation Approval",
    "Change with Same Employer Approval",
    "New Concurrent Approval",
    CHANGE_OF_EMPLOYER_APPROVAL,
    "Amended Approval",
)
DENIAL_COLUMNS = (
    "New Employment Denial",
    "Continuation Denial",
    "Change with Same Employer Denial",
    "New Concurrent Denial",
    "Change of Employer Denial",
    "Amended Denial",
)
REQUIRED_COLUMNS = frozenset(
    (EMPLOYER, STATE, CITY, FISCAL_YEAR, *APPROVAL_COLUMNS, *DENIAL_COLUMNS)
)

# `name` and `version` are sent to the client during the initialize handshake
# and are what identifies this server in the Claude Desktop UI.
mcp = MCPServer(name="h1b-sponsor", version="0.1.0")


def parse_int(value: str) -> int:
    """Parse a CSV integer cell, stripping thousands commas and treating junk as 0."""
    # USCIS formats counts >= 1,000 with commas (e.g. "1,269"). Calling
    # .isdigit() directly on the raw string would silently zero out the largest
    # sponsors in the dataset.
    cleaned = value.replace(",", "").strip()
    return int(cleaned) if cleaned.isdigit() else 0


def count(row: dict[str, str], columns: tuple[str, ...]) -> int:
    """Sum petition counts, treating blank or malformed cells as zero."""
    # Every CSV cell is untrusted input from outside the system, exactly like a
    # query param. `parse_int` filters blanks and junk without try/except and
    # without letting one bad row crash a whole request.
    return sum(parse_int(row.get(c, "")) for c in columns)


def approval_rate(approvals: int, denials: int) -> float | None:
    """Share of decided petitions that were approved, or None if none decided."""
    decided = approvals + denials
    return round(approvals / decided, 4) if decided else None


@lru_cache(maxsize=1)
def load_rows() -> tuple[dict[str, str], ...]:
    """Parse the USCIS export once and cache it for the process lifetime.

    The file is UTF-16 and tab-delimited despite its .csv name, and its header
    has trailing whitespace, so both are normalised here rather than at every
    call site.
    """
    # An MCP stdio server is a long-lived process, so lru_cache turns this into
    # a lazy singleton: the ~0.4s parse happens on first use, then every tool
    # reuses the same tuple. Returning a tuple (not a list) keeps it hashable
    # and signals that callers must not mutate the shared dataset.
    #
    # Stream with encoding="utf-16" and newline="" rather than reading the
    # entire 41 MB file into memory at once: Python's UTF-16 codec strips the
    # BOM automatically and csv.reader handles any embedded newlines safely.
    with DATA_FILE.open(encoding="utf-16", newline="") as f:
        reader = csv.reader(f, delimiter="\t")

        # Strip the header ONCE. The real file ships "Fiscal Year   " with three
        # trailing spaces; without this every lookup raises KeyError.
        try:
            header = [column.strip() for column in next(reader)]
        except StopIteration as exc:
            raise ValueError(f"Data file {DATA_FILE} is empty") from exc

        missing = REQUIRED_COLUMNS.difference(header)
        if missing:
            raise ValueError(
                f"Data file {DATA_FILE} is missing expected columns: {sorted(missing)}. "
                "Ensure you downloaded the USCIS H-1B Employer Data Hub export "
                "('Employer Information.csv'), not the annual bulk export."
            )

        # csv.DictReader would rebuild the key strings for all 20 columns on every
        # one of the ~174k rows (~3.5M redundant string objects). dict(zip(...))
        # reuses the single `header` list, so all rows share the same key objects.
        parsed: list[dict[str, str]] = []
        for values in reader:
            row = dict(zip(header, (v.strip() for v in values)))
            # ~20 rows in the real export have no employer name; drop them at load
            # time so no downstream tool has to special-case them.
            if not row.get(EMPLOYER):
                continue
            if row.get(STATE):
                row[STATE] = row[STATE].upper()
            parsed.append(row)

    return tuple(parsed)


def clear_caches() -> None:
    """Clear all cached dataset indexes (useful in tests when swapping DATA_FILE)."""
    load_rows.cache_clear()
    available_fiscal_years.cache_clear()
    employer_index.cache_clear()
    state_index.cache_clear()
    _summary_payload.cache_clear()


@lru_cache(maxsize=1)
def available_fiscal_years() -> tuple[str, ...]:
    """Return sorted distinct fiscal years present in the dataset."""
    return tuple(sorted({r[FISCAL_YEAR] for r in load_rows() if r.get(FISCAL_YEAR)}))


# Compiled once at import, not per call: this runs against ~100k employer names
# on every search.
NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")

MAX_RESULTS = 50


def normalize(name: str) -> str:
    """Reduce a company name to a comparable key.

    Employer names are entered by hand, so the same company appears as
    "AMAZON COM SERVICES LLC" while a user types "Amazon.com". Dropping case and
    every non-alphanumeric character collapses both to "amazoncomservices..." /
    "amazoncom", making one a substring of the other.

    Known ceiling: exact key equality still splits entities that differ by a
    legal suffix, so "COGNIZANT ... US CORP" and "COGNIZANT ... US" remain two
    entries. Stripping CORP/INC/LLC would merge them but also merge genuinely
    distinct subsidiaries; if that trade becomes worth making, do it here.
    """
    return NON_ALPHANUMERIC.sub("", name.lower())


@lru_cache(maxsize=8)
def employer_index(fiscal_year: str | None = None) -> tuple[dict, ...]:
    """Aggregate the row-level export into one entry per employer.

    The export has one row per employer PER location PER fiscal year, so a
    company's true totals only exist after summing its rows. Building this index
    once at first use keeps every later search to a single linear scan, instead
    of re-aggregating 174k rows per query.

    Entries are keyed by the normalised name, not the raw one, because USCIS
    holds the same company under several spellings — "AMAZON COM SERVICES LLC"
    and "AMAZON.COM SERVICES LLC" are separate rows that must be summed to
    answer "how many people does Amazon sponsor". The trade-off is that two
    genuinely different companies whose names differ only in punctuation would
    be merged; Tax ID cannot disambiguate them because it is masked to 4 digits.
    """
    totals: dict[str, dict] = {}
    for row in load_rows():
        fy = row[FISCAL_YEAR]
        if fiscal_year is not None and fy != fiscal_year:
            continue
        name = row[EMPLOYER]
        # Precomputed here so normalize() never runs inside the search loop.
        key = normalize(name)
        entry = totals.get(key)
        if entry is None:
            entry = totals[key] = {
                # Spellings are counted, not just collected, so the most
                # frequently filed one becomes the display name.
                "names": Counter(),
                "search_key": key,
                "approvals": 0,
                "denials": 0,
                "new_employment_approvals": 0,
                "change_of_employer_approvals": 0,
                "continuation_approvals": 0,
                "states": set(),
                "cities": set(),
                "fiscal_years": set(),
                "by_fiscal_year": {},
            }
        approvals = count(row, APPROVAL_COLUMNS)
        denials = count(row, DENIAL_COLUMNS)
        entry["names"][name] += 1
        entry["approvals"] += approvals
        entry["denials"] += denials
        entry["new_employment_approvals"] += parse_int(
            row.get(NEW_EMPLOYMENT_APPROVAL, "")
        )
        entry["change_of_employer_approvals"] += parse_int(
            row.get(CHANGE_OF_EMPLOYER_APPROVAL, "")
        )
        entry["continuation_approvals"] += count(row, CONTINUATION_APPROVAL_COLUMNS)
        entry["fiscal_years"].add(fy)
        fy_bucket = entry["by_fiscal_year"].get(fy)
        if fy_bucket is None:
            fy_bucket = entry["by_fiscal_year"][fy] = {"approvals": 0, "denials": 0}
        fy_bucket["approvals"] += approvals
        fy_bucket["denials"] += denials
        if row[STATE]:
            entry["states"].add(row[STATE])
        if row[CITY]:
            entry["cities"].add(row[CITY])
    return tuple(totals.values())


def as_result(entry: dict) -> dict:
    """Project an index entry into a JSON-serialisable result."""
    display, *variants = [name for name, _ in entry["names"].most_common()]
    by_year = {
        fy: {
            "approvals": counts["approvals"],
            "denials": counts["denials"],
            "approval_rate": approval_rate(counts["approvals"], counts["denials"]),
        }
        for fy, counts in sorted(entry["by_fiscal_year"].items())
    }
    return {
        "employer": display,
        # Surfaced so the caller can see the merge happened rather than wonder
        # why the total is higher than the figure on any single USCIS row.
        "also_filed_as": variants,
        "total_approvals": entry["approvals"],
        "total_denials": entry["denials"],
        # A company with 1 approval and 0 denials is not "better" than one with
        # 5000 approvals; the model needs the raw counts to judge, so the rate is
        # reported alongside them rather than instead of them.
        "approval_rate": approval_rate(entry["approvals"], entry["denials"]),
        "new_employment_approvals": entry["new_employment_approvals"],
        "change_of_employer_approvals": entry["change_of_employer_approvals"],
        "continuation_approvals": entry["continuation_approvals"],
        "states": sorted(entry["states"]),
        "cities": sorted(entry["cities"]),
        "fiscal_years": sorted(entry["fiscal_years"]),
        "by_fiscal_year": by_year,
    }


def _missing_file_error() -> str:
    return json.dumps(
        {
            "error": f"H-1B dataset file not found at {DATA_FILE}",
            "hint": (
                "Download 'Employer Information.csv' from the USCIS H-1B Employer "
                "Data Hub and move it to data/h1b_employers.csv, or set the "
                "H1B_DATA_FILE environment variable."
            ),
        },
        indent=2,
    )


# A tool, not a resource: it takes an argument and the model decides both when to
# call it and what to pass. The type hints below become the JSON schema the model
# sees, and this docstring is the only instruction it gets about when to use it —
# so it is written for the model, not for a human maintainer.
@mcp.tool()
def search_sponsors(
    company_name: str, limit: int = 10, fiscal_year: str | None = None
) -> str:
    """Find H-1B sponsoring employers whose name contains the given text.

    Matching ignores case and punctuation, so "amazon.com" matches
    "AMAZON COM SERVICES LLC". Results are aggregated across all of an
    employer's offices and ranked by relevance then total approvals.

    Args:
        company_name: Full or partial company name, e.g. "google" or "infosys".
        limit: Maximum employers to return (1-50).
        fiscal_year: Optional four-digit fiscal year filter (e.g. "2026").
            Omit to aggregate across all fiscal years in the dataset.
    """
    # Validate at the boundary: `company_name` comes from the model, which can
    # send anything. An all-punctuation query would normalise to "" and match
    # every employer, so it is rejected rather than silently returning the
    # whole dataset.
    query = normalize(company_name)
    if not query:
        return json.dumps(
            {"error": "company_name must contain at least one letter or digit"}
        )
    if not DATA_FILE.exists():
        return _missing_file_error()

    fy_filter = str(fiscal_year).strip() if fiscal_year is not None else None
    if fy_filter:
        valid_fys = available_fiscal_years()
        if fy_filter not in valid_fys:
            return json.dumps(
                {
                    "error": f"unknown fiscal_year {fiscal_year!r}",
                    "valid_fiscal_years": list(valid_fys),
                }
            )
    else:
        fy_filter = None

    limit = max(1, min(limit, MAX_RESULTS))

    matches = [e for e in employer_index(fy_filter) if query in e["search_key"]]

    # Relevance before popularity: an exact name match outranks a prefix match,
    # which outranks an incidental substring; ties break on sponsorship volume.
    matches.sort(
        key=lambda e: (
            e["search_key"] != query,
            not e["search_key"].startswith(query),
            -e["approvals"],
        )
    )

    # Report the true total separately from the truncated list, so the model can
    # tell "these are all of them" from "there are more".
    payload: dict = {
        "query": company_name,
        "total_matches": len(matches),
        "returned": min(len(matches), limit),
        "results": [as_result(e) for e in matches[:limit]],
    }
    if fy_filter is not None:
        payload["fiscal_year"] = fy_filter
    return json.dumps(payload, indent=2)


@lru_cache(maxsize=8)
def state_index(fiscal_year: str | None = None) -> dict[str, tuple[dict, ...]]:
    """Aggregate by (employer, state), grouped by state and pre-ranked.

    Deliberately a second index rather than a reshaping of employer_index():
    that one sums a company across all its offices, which is the right answer
    for "how many people does Google sponsor" and the wrong one for "who
    sponsors most in Texas" — Google's national total must not be credited to
    every state it appears in.

    Sorting happens here, once, because the ranking never changes between
    requests. The tool itself is then just a slice.
    """
    totals: dict[tuple[str, str], dict] = {}
    for row in load_rows():
        fy = row[FISCAL_YEAR]
        if fiscal_year is not None and fy != fiscal_year:
            continue
        state = row[STATE]
        # Rows without a state cannot be attributed to one, so they are dropped
        # rather than bucketed under "".
        if not state:
            continue
        key = (state, normalize(row[EMPLOYER]))
        entry = totals.get(key)
        if entry is None:
            entry = totals[key] = {
                "names": Counter(),
                "approvals": 0,
                "denials": 0,
                "new_employment_approvals": 0,
                "change_of_employer_approvals": 0,
                "continuation_approvals": 0,
                "cities": set(),
                "fiscal_years": set(),
            }
        entry["names"][row[EMPLOYER]] += 1
        entry["approvals"] += count(row, APPROVAL_COLUMNS)
        entry["denials"] += count(row, DENIAL_COLUMNS)
        entry["new_employment_approvals"] += parse_int(
            row.get(NEW_EMPLOYMENT_APPROVAL, "")
        )
        entry["change_of_employer_approvals"] += parse_int(
            row.get(CHANGE_OF_EMPLOYER_APPROVAL, "")
        )
        entry["continuation_approvals"] += count(row, CONTINUATION_APPROVAL_COLUMNS)
        entry["fiscal_years"].add(fy)
        if row[CITY]:
            entry["cities"].add(row[CITY])

    by_state: dict[str, list[dict]] = {}
    for (state, _), entry in totals.items():
        by_state.setdefault(state, []).append(entry)
    return {
        state: tuple(sorted(entries, key=lambda e: -e["approvals"]))
        for state, entries in by_state.items()
    }


@mcp.tool()
def get_top_sponsors_by_state(
    state: str, limit: int = 10, fiscal_year: str | None = None
) -> str:
    """Rank the largest H-1B sponsors in one US state by approved petitions.

    Counts only petitions filed from offices in that state, so a national
    employer appears with its local figures rather than its company-wide total.

    Args:
        state: Two-letter USPS code, e.g. "CA", "TX", "NY". Territories such as
            "PR" and "GU" are also present.
        limit: Maximum employers to return (1-50).
        fiscal_year: Optional four-digit fiscal year filter (e.g. "2026").
            Omit to aggregate across all fiscal years in the dataset.
    """
    if not DATA_FILE.exists():
        return _missing_file_error()

    fy_filter = str(fiscal_year).strip() if fiscal_year is not None else None
    if fy_filter:
        valid_fys = available_fiscal_years()
        if fy_filter not in valid_fys:
            return json.dumps(
                {
                    "error": f"unknown fiscal_year {fiscal_year!r}",
                    "valid_fiscal_years": list(valid_fys),
                }
            )
    else:
        fy_filter = None

    index = state_index(fy_filter)
    # The model may send "california", " ca " or a full name. Normalising the
    # cheap cases costs one line; anything still unknown gets the valid codes
    # back so the model can correct itself instead of guessing again.
    code = state.strip().upper()
    if code not in index:
        all_states = sorted(state_index(None))
        return json.dumps(
            {"error": f"unknown state {state!r}", "valid_states": all_states}
        )
    limit = max(1, min(limit, MAX_RESULTS))

    entries = index[code]
    results = []
    for rank, entry in enumerate(entries[:limit], start=1):
        display, *variants = [name for name, _ in entry["names"].most_common()]
        results.append(
            {
                "rank": rank,
                "employer": display,
                "also_filed_as": variants,
                "approvals": entry["approvals"],
                "denials": entry["denials"],
                "approval_rate": approval_rate(entry["approvals"], entry["denials"]),
                "new_employment_approvals": entry["new_employment_approvals"],
                "change_of_employer_approvals": entry["change_of_employer_approvals"],
                "continuation_approvals": entry["continuation_approvals"],
                "cities": sorted(entry["cities"]),
                "fiscal_years": sorted(entry["fiscal_years"]),
            }
        )

    payload: dict = {
        "state": code,
        "employers_in_state": len(entries),
        "returned": min(len(entries), limit),
        "results": results,
    }
    if fy_filter is not None:
        payload["fiscal_year"] = fy_filter
    return json.dumps(payload, indent=2)


@lru_cache(maxsize=1)
def _summary_payload() -> str:
    rows = load_rows()
    approvals = sum(count(r, APPROVAL_COLUMNS) for r in rows)
    denials = sum(count(r, DENIAL_COLUMNS) for r in rows)
    states = Counter(r[STATE] for r in rows if r[STATE])
    return json.dumps(
        {
            "rows": len(rows),
            "fiscal_years": sorted({r[FISCAL_YEAR] for r in rows}),
            # Matches employer_index() by counting normalised employer keys,
            # while also reporting the raw unmerged spelling count.
            "unique_employers": len(employer_index()),
            "raw_employer_names": len({r[EMPLOYER] for r in rows}),
            "total_approvals": approvals,
            "total_denials": denials,
            "approval_rate": approval_rate(approvals, denials),
            # Above 50: the data includes DC and territories such as PR and GU.
            "states_covered": len(states),
            "top_states_by_filings": dict(states.most_common(5)),
        },
        indent=2,
    )


# A resource, not a tool: it takes no arguments, has no side effects, and the
# model does not decide anything to call it. "h1b://" is a private URI scheme —
# it only has to be unique within this server. mime_type tells the client how
# to render what the function returns.
@mcp.resource("h1b://summary", mime_type="application/json")
def get_summary() -> str:
    """Overall shape of the H-1B dataset: coverage, totals and approval rate."""
    # This docstring is what the client displays as the resource description,
    # so it is written for a reader deciding whether to attach this as context.
    if not DATA_FILE.exists():
        return _missing_file_error()
    return _summary_payload()


def main() -> None:
    # Defaults to stdio transport: the client launches this file as a subprocess
    # and speaks JSON-RPC over stdin/stdout. That is why anything printed to
    # stdout corrupts the protocol stream — use logging (stderr) to debug, never
    # print(). `transport="streamable-http"` is the remote alternative.
    mcp.run()


if __name__ == "__main__":
    main()
