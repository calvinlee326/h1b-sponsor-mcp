# H-1B Sponsor Search — MCP Server

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant query
USCIS H-1B Employer Data Hub records: search a company's sponsorship history,
rank the largest sponsors in a given US state, and read a summary of the
dataset.

Built with the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
(`mcp >= 2.0`) and the Python standard library — no pandas, no fuzzy-matching
dependency.

## Capabilities

| Kind | Name | Purpose |
| --- | --- | --- |
| Tool | `search_sponsors(company_name, limit=10)` | Find employers by full or partial name. Case- and punctuation-insensitive, so `amazon.com` matches `AMAZON COM SERVICES LLC`. Totals are aggregated across all of an employer's offices. |
| Tool | `get_top_sponsors_by_state(state, limit=10)` | Rank the largest sponsors in one state by approved petitions, counting only petitions filed from offices in that state. |
| Resource | `h1b://summary` | Dataset coverage, unique employer count, national approval/denial totals and approval rate. |

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- Node.js 18+ — only if you want to run the MCP Inspector

## Setup

```bash
git clone <your-repo-url>
cd h1b-sponsor-mcp
uv sync
```

`uv sync` creates `.venv` and installs the exact versions pinned in `uv.lock`.

## Getting the data

The USCIS export is not redistributed in this repository. Download it yourself:

1. Open the [H-1B Employer Data Hub](https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub).
2. Run a query (leave the filters empty for the full dataset) and export the
   results. You will get a file named `Employer Information.csv`.
3. Move it into place:

   ```bash
   mv ~/Downloads/"Employer Information.csv" data/h1b_employers.csv
   ```

**uscis.gov blocks non-browser clients.** `curl`, `wget` and scripted fetches
return HTTP 403 regardless of User-Agent, so this step has to be done in a real
browser.

### About the file format

Despite the `.csv` extension, the export is **UTF-16 encoded and tab-delimited**.
Opening it with `open(path)` or parsing it with a comma delimiter will not work.
The loader in `server.py` handles both.

Expected columns:

```
Line by line, Fiscal Year, Employer (Petitioner) Name, Tax ID,
Industry (NAICS) Code, Petitioner City, Petitioner State, Petitioner Zip Code,
New Employment Approval,            New Employment Denial,
Continuation Approval,              Continuation Denial,
Change with Same Employer Approval, Change with Same Employer Denial,
New Concurrent Approval,            New Concurrent Denial,
Change of Employer Approval,        Change of Employer Denial,
Amended Approval,                   Amended Denial
```

USCIS splits petitions into six approval/denial categories. This differs from
the *annual bulk export* also published by USCIS, which uses `Employer`,
`State`, `Initial Approval` and `Continuing Approval` instead — that file is
**not** compatible with this server without editing the column constants at the
top of `server.py`.

A 40-row sample in the identical format is committed at
`data/h1b_employers.sample.csv`, so the repository is inspectable without the
41 MB download.

## Use with Claude Desktop

Add the server to `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "h1b-sponsor": {
      "command": "/absolute/path/to/uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/h1b-sponsor-mcp",
        "h1b-sponsor-mcp"
      ]
    }
  }
}
```

Then **fully quit Claude Desktop and reopen it** (Cmd-Q on macOS — closing the
window is not enough).

Both paths must be absolute; find your `uv` with `which uv`. `--directory` is
required because Claude Desktop launches servers with the working directory set
to `/`, so `uv` would otherwise never find this project. `h1b-sponsor-mcp` is
the console script declared in `pyproject.toml`, which survives moves better
than a hardcoded file path.

Once connected, ask things like:

- *How many H-1B petitions has Amazon sponsored?*
- *Who are the top 5 H-1B sponsors in Washington state?*
- *What's the overall H-1B approval rate in this dataset?*

### Troubleshooting

Test the server directly, bypassing Claude Desktop:

```bash
cd / && echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
  | /absolute/path/to/uv run --directory /absolute/path/to/h1b-sponsor-mcp h1b-sponsor-mcp
```

A JSON-RPC response means the server is fine and the problem is in the client
config. A traceback means the problem is in the server. Run it from `/` — testing
from inside the project directory can succeed even when the config is wrong,
because `uv` finds `pyproject.toml` by walking up from the working directory.

Client logs are at `~/Library/Logs/Claude/mcp-server-h1b-sponsor.log`.

Because the transport is stdio, **anything written to stdout corrupts the
protocol stream**. Use `logging` (stderr) when debugging, never `print()`.

## Use with MCP Inspector

```bash
uv run mcp dev src/h1b_sponsor_mcp/server.py --with-editable .
```

This opens a browser UI where you can list tools, inspect the JSON schemas
generated from the Python type hints, and call tools interactively. The URL
printed in the terminal includes an auth token and will not connect without it.
`--with-editable .` installs this project into the Inspector's environment.

`mcp dev` currently pulls Inspector v1, which is deprecated. For the current
version, skip `mcp dev` and point Inspector at the same command Claude Desktop
uses:

```bash
npx @modelcontextprotocol/inspector@latest \
  uv run --directory "$PWD" h1b-sponsor-mcp
```

## Testing

```bash
uv run pytest
```

Tests run against the real USCIS export and skip automatically if
`data/h1b_employers.csv` is absent.

## How it works

`server.py` builds two cached indexes on first use, both derived from a single
parse of the export:

- `employer_index()` — one entry per employer, summed across every office and
  fiscal year. Answers "how many people does Google sponsor" (3,629).
- `state_index()` — one entry per (employer, state), grouped by state and
  pre-sorted by approvals. Answers "who sponsors most in Texas" without
  crediting a national employer's company-wide total to every state it appears
  in (Google is 3,613 in CA and 16 in TX).

Both use `functools.lru_cache`, so the parse and aggregation happen on the first
tool call rather than at import. This keeps the MCP initialize handshake
instant, which matters because clients can time out during startup. A test
asserts the two indexes agree: per-state figures must sum to the national total.

Employer names are matched on a normalized key (lowercased, non-alphanumeric
characters removed). This also merges duplicate spellings in the source data —
`AMAZON COM SERVICES LLC` and `AMAZON.COM SERVICES LLC` are separate USCIS rows
for one company. Roughly 6,000 such duplicates collapse this way, and any merged
spellings are reported in an `also_filed_as` field.

## Known limitations

- **Legal suffixes are not stripped.** `COGNIZANT ... US CORP` and
  `COGNIZANT ... US` remain separate entries, so some rankings understate a
  company. Stripping `CORP`/`INC`/`LLC` would fix it but would also merge
  genuinely distinct subsidiaries. Tax ID cannot disambiguate them because the
  export masks it to four digits.
- **Data is loaded once per process.** After replacing the CSV, restart the
  client to pick up the new file.
- **The config uses absolute paths.** Moving or renaming the project directory
  breaks the Claude Desktop entry until the config is updated.

## Data source

[USCIS H-1B Employer Data Hub](https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub)
— public US government data. Approval and denial counts reflect petitions
processed by USCIS, not individual workers; one employer may file several
petitions for the same person across categories.
