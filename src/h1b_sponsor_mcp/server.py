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
DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "h1b_employers.csv"

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
APPROVAL_COLUMNS = (
    "New Employment Approval",
    "Continuation Approval",
    "Change with Same Employer Approval",
    "New Concurrent Approval",
    "Change of Employer Approval",
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

# `name` and `version` are sent to the client during the initialize handshake
# and are what identifies this server in the Claude Desktop UI.
mcp = MCPServer(name="h1b-sponsor", version="0.1.0")


def count(row: dict[str, str], columns: tuple[str, ...]) -> int:
    """Sum petition counts, treating blank or malformed cells as zero."""
    # Every CSV cell is untrusted input from outside the system, exactly like a
    # query param. `.isdigit()` filters blanks and junk without try/except and
    # without letting one bad row crash a whole request.
    return sum(int(row[c]) for c in columns if row[c].isdigit())


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
    # read_bytes().decode("utf-16") rather than open(): the file has a UTF-16
    # BOM, so opening it as text would yield NUL-separated garbage.
    lines = DATA_FILE.read_bytes().decode("utf-16").splitlines()
    reader = csv.reader(lines, delimiter="\t")

    # Strip the header ONCE. The real file ships "Fiscal Year   " with three
    # trailing spaces; without this every lookup raises KeyError.
    header = [column.strip() for column in next(reader)]

    # csv.DictReader would rebuild the key strings for all 20 columns on every
    # one of the ~174k rows (~3.5M redundant string objects). dict(zip(...))
    # reuses the single `header` list, so all rows share the same key objects.
    rows = (dict(zip(header, (v.strip() for v in values))) for values in reader)

    # ~20 rows in the real export have no employer name; drop them at load time
    # so no downstream tool has to special-case them.
    return tuple(row for row in rows if row.get(EMPLOYER))


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


@lru_cache(maxsize=1)
def employer_index() -> tuple[dict, ...]:
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
                "states": set(),
                "cities": set(),
                "fiscal_years": set(),
            }
        entry["names"][name] += 1
        entry["approvals"] += count(row, APPROVAL_COLUMNS)
        entry["denials"] += count(row, DENIAL_COLUMNS)
        entry["fiscal_years"].add(row[FISCAL_YEAR])
        if row[STATE]:
            entry["states"].add(row[STATE])
        if row[CITY]:
            entry["cities"].add(row[CITY])
    return tuple(totals.values())


def as_result(entry: dict) -> dict:
    """Project an index entry into a JSON-serialisable result."""
    display, *variants = [name for name, _ in entry["names"].most_common()]
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
        "states": sorted(entry["states"]),
        "cities": sorted(entry["cities"]),
        "fiscal_years": sorted(entry["fiscal_years"]),
    }


# A tool, not a resource: it takes an argument and the model decides both when to
# call it and what to pass. The type hints below become the JSON schema the model
# sees, and this docstring is the only instruction it gets about when to use it —
# so it is written for the model, not for a human maintainer.
@mcp.tool()
def search_sponsors(company_name: str, limit: int = 10) -> str:
    """Find H-1B sponsoring employers whose name contains the given text.

    Matching ignores case and punctuation, so "amazon.com" matches
    "AMAZON COM SERVICES LLC". Results cover fiscal years 2024-2026 and are
    aggregated across all of an employer's offices, ranked by total approvals.

    Args:
        company_name: Full or partial company name, e.g. "google" or "infosys".
        limit: Maximum employers to return (1-50).
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
    limit = max(1, min(limit, MAX_RESULTS))

    matches = [e for e in employer_index() if query in e["search_key"]]

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
    return json.dumps(
        {
            "query": company_name,
            "total_matches": len(matches),
            "returned": min(len(matches), limit),
            "results": [as_result(e) for e in matches[:limit]],
        },
        indent=2,
    )


@lru_cache(maxsize=1)
def state_index() -> dict[str, tuple[dict, ...]]:
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
                "cities": set(),
                "fiscal_years": set(),
            }
        entry["names"][row[EMPLOYER]] += 1
        entry["approvals"] += count(row, APPROVAL_COLUMNS)
        entry["denials"] += count(row, DENIAL_COLUMNS)
        entry["fiscal_years"].add(row[FISCAL_YEAR])
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
def get_top_sponsors_by_state(state: str, limit: int = 10) -> str:
    """Rank the largest H-1B sponsors in one US state by approved petitions.

    Counts only petitions filed from offices in that state, so a national
    employer appears with its local figures rather than its company-wide total.
    Covers fiscal years 2024-2026.

    Args:
        state: Two-letter USPS code, e.g. "CA", "TX", "NY". Territories such as
            "PR" and "GU" are also present.
        limit: Maximum employers to return (1-50).
    """
    index = state_index()
    # The model may send "california", " ca " or a full name. Normalising the
    # cheap cases costs one line; anything still unknown gets the valid codes
    # back so the model can correct itself instead of guessing again.
    code = state.strip().upper()
    if code not in index:
        return json.dumps(
            {"error": f"unknown state {state!r}", "valid_states": sorted(index)}
        )
    limit = max(1, min(limit, MAX_RESULTS))

    entries = index[code]
    return json.dumps(
        {
            "state": code,
            "employers_in_state": len(entries),
            "returned": min(len(entries), limit),
            "results": [
                {
                    "rank": rank,
                    "employer": entry["names"].most_common(1)[0][0],
                    "approvals": entry["approvals"],
                    "denials": entry["denials"],
                    "approval_rate": approval_rate(
                        entry["approvals"], entry["denials"]
                    ),
                    "cities": sorted(entry["cities"]),
                    "fiscal_years": sorted(entry["fiscal_years"]),
                }
                for rank, entry in enumerate(entries[:limit], start=1)
            ],
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
    rows = load_rows()
    approvals = sum(count(r, APPROVAL_COLUMNS) for r in rows)
    denials = sum(count(r, DENIAL_COLUMNS) for r in rows)
    states = Counter(r[STATE] for r in rows if r[STATE])
    return json.dumps(
        {
            "rows": len(rows),
            "fiscal_years": sorted({r[FISCAL_YEAR] for r in rows}),
            # Far fewer than `rows`: the export has one row per employer PER
            # location PER fiscal year, so any per-company figure must aggregate.
            "unique_employers": len({r[EMPLOYER] for r in rows}),
            "total_approvals": approvals,
            "total_denials": denials,
            "approval_rate": approval_rate(approvals, denials),
            # Above 50: the data includes DC and territories such as PR and GU.
            "states_covered": len(states),
            "top_states_by_filings": dict(states.most_common(5)),
        },
        indent=2,
    )


def main() -> None:
    # Defaults to stdio transport: the client launches this file as a subprocess
    # and speaks JSON-RPC over stdin/stdout. That is why anything printed to
    # stdout corrupts the protocol stream — use logging (stderr) to debug, never
    # print(). `transport="streamable-http"` is the remote alternative.
    mcp.run()


if __name__ == "__main__":
    main()
