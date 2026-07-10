# ledger-mcp

**A Model Context Protocol server that exposes your Casper verdict ledger
(`.claude/verdicts.log`) as a live, queryable context source for any model.**

The Casper verdict ledger is an append-only flat file of *judgment verdicts* —
every gate you tripped, every override a human signed, every claim that survived
(or broke under) refutation, every one-way door you walked through. It is the
[verdict grammar v1](https://github.com/ronniepinnell/verdict-grammar): one
`grep`-able line per verdict, forever.

`ledger-mcp` serves that file over the Model Context Protocol so any MCP client
— Claude Code, or any other model host — can *read and query your own
correction history at reasoning time.*

## Why ledger-as-MCP

Every coding agent reinvents "did this actually work?" from scratch and then
throws the answer away. Casper keeps the answer — as a verdict ledger. But a
ledger sitting in a file only helps if the model can *reach* it mid-task.

MCP is the bridge. Point any model at this server and its own history stops
being a static log and becomes **retrievable context**:

- *"Have we overridden this gate before? Did it bite us?"* → `overrode_then_broke`
- *"What did we already refute about search/unicode?"* → `query_verdicts type=REFUTE`
- *"Is this gate an override magnet we should re-threshold?"* → `ledger_stats`

That is the on-ramp. Once your ledger is an MCP resource, the same interface
scales to **hosted cross-repo calibration** — pooling verdict ledgers across
projects so a model can learn from corrections it never personally made. This
server is the local, private, single-repo foothold for that.

## Privacy

**It reads a LOCAL file and sends nothing anywhere.** No network, no telemetry,
no writes — the server only ever *reads* `.claude/verdicts.log` and answers the
MCP client on stdio. Nothing about your ledger leaves the machine.

## Install

Zero dependencies. Just `python3` (>= 3.8). No SDK, no `pip install` — matching
Casper's dependency-free ethos. Copy `server.py` anywhere and run it.

### Add to Claude Code (`.mcp.json`)

```json
{
  "mcpServers": {
    "ledger": {
      "command": "python3",
      "args": ["/absolute/path/to/ledger-mcp/server.py"],
      "env": { "LEDGER_PATH": ".claude/verdicts.log" }
    }
  }
}
```

Or via the CLI:

```sh
claude mcp add ledger -- python3 /absolute/path/to/ledger-mcp/server.py
```

The ledger path resolves from (in order): `--ledger <path>` CLI arg,
`LEDGER_PATH` env var, or the default `./.claude/verdicts.log`.

## MCP surface

Transport: **stdio**, newline-delimited JSON-RPC 2.0.

Methods: `initialize`, `notifications/initialized`, `ping`, `tools/list`,
`tools/call`, `resources/list`, `resources/read`.

### Resource

| URI | Description |
|---|---|
| `ledger://verdicts` | The whole ledger as structured JSON: `{path, grammar, count, verdicts[]}`, each verdict `{date, type, verdict, detail, commit, by, raw}`. |

### Tools

| Tool | Arguments | Returns |
|---|---|---|
| `query_verdicts` | `type`, `since` (YYYY-MM-DD), `commit` (sha prefix), `limit` — all optional | matching verdict rows |
| `ledger_stats` | — | counts by type, gate override rate, days since last override, accepted risks, one-way doors, suspicious (>=3x overridden) gates |
| `overrode_then_broke` | — | gate overrides whose judged commit was later reverted/fixed (needs a git repo + grammar-v1 commit linkage; degrades to a clear `status` otherwise) |

## Test

```sh
test/run-tests.sh
```

Starts the server, runs a full JSON-RPC session (initialize → tools/list →
resources/read → tool calls) against a synthetic ledger fixture, and asserts on
every response.

## License

MIT © 2026 Ronnie Pinnell. See [LICENSE](./LICENSE).
