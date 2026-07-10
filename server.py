#!/usr/bin/env python3
"""ledger-mcp — a Model Context Protocol server exposing a Casper verdict ledger.

Serves `.claude/verdicts.log` (the append-only judgment ledger — see the verdict
grammar v1) to any MCP client over stdio, as a structured RESOURCE plus three
query TOOLS. Your correction history becomes a live, queryable context source for
any model.

Design: hand-rolled JSON-RPC 2.0 over a stdio loop. NO SDK dependency — runs on
a bare `python3` (>=3.8), matching Casper's dependency-free ethos. The ledger is
only ever READ; nothing is written and nothing leaves the machine.

MCP methods implemented:
  initialize, notifications/initialized, ping,
  tools/list, tools/call,
  resources/list, resources/read

Tools:
  query_verdicts  — filter the ledger by type / since / commit
  ledger_stats    — counts by type, gate-override rate, days since last override
  overrode_then_broke — gate overrides whose commit was later reverted/fixed

Usage:
  python3 server.py                 # serve, reading ./.claude/verdicts.log
  LEDGER_PATH=/path/to/verdicts.log python3 server.py
  python3 server.py --ledger /path/to/verdicts.log

MIT (c) 2026 Ronnie Pinnell.
"""
import collections
import datetime
import json
import os
import re
import subprocess
import sys

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "ledger-mcp"
SERVER_VERSION = "0.1.0"
LEDGER_URI = "ledger://verdicts"

# ---------------------------------------------------------------------------
# Ledger parsing — the verdict grammar v1 (backward compatible with v0).
# Ported from verdict-viewer/verdicts.py so this server stays single-file and
# dependency-free. The ledger is a grep-able flat file; we only READ it.
# ---------------------------------------------------------------------------

FIX_RE = re.compile(r"\b(revert|reverts|reverted|fix|fixes|fixed|hotfix|"
                    r"rollback|roll back|backout|back out|undo)\b", re.I)


def default_ledger_path():
    """Resolve the ledger path from CLI arg, env, or the conventional default."""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--ledger" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--ledger="):
            return a.split("=", 1)[1]
    return os.environ.get("LEDGER_PATH", ".claude/verdicts.log")


def parse(path):
    """Yield dicts for each verdict line. Comments/blank lines skipped; malformed
    lines (<3 pipe-fields) preserved with type MALFORMED, never dropped."""
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            parts = [p.strip() for p in raw.split("|")]
            if len(parts) < 3:
                rows.append({"date": "", "type": "MALFORMED", "verdict": raw,
                             "detail": "", "commit": "", "by": "", "raw": raw})
                continue
            by = ""
            detail = parts[3:]
            if detail and detail[-1].lower().startswith("by:"):
                by = detail[-1][3:].strip()
                detail = detail[:-1]
            commit = ""
            kept = []
            for d in detail:
                if d.lower().startswith("commit:"):
                    commit = d.split(":", 1)[1].strip()
                else:
                    kept.append(d)
            rows.append({"date": parts[0], "type": parts[1].upper(),
                         "verdict": parts[2], "detail": " | ".join(kept),
                         "commit": commit, "by": by, "raw": raw})
    return rows


def ledger_grammar(path):
    """Return 'v1' if the file opens with a `# grammar: v1` header, else 'v0'."""
    if not os.path.exists(path):
        return "unknown"
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            s = raw.strip()
            if not s:
                continue
            m = re.match(r"#\s*grammar:\s*(v\d+)", s, re.I)
            return m.group(1).lower() if m else "v0"
    return "v0"


# ---------------------------------------------------------------------------
# Tool implementations. Each returns a JSON-serializable dict.
# ---------------------------------------------------------------------------

def tool_query_verdicts(rows, args):
    """Filter by type / since (YYYY-MM-DD) / commit (prefix match)."""
    typ = (args.get("type") or "").upper().strip()
    since = (args.get("since") or "").strip()
    commit = (args.get("commit") or "").strip()
    limit = args.get("limit")
    out = []
    for r in rows:
        if typ and r["type"] != typ:
            continue
        if since and (not r["date"] or r["date"][:10] < since):
            continue
        if commit:
            c = r.get("commit") or ""
            if not c or not (c.startswith(commit) or commit.startswith(c)):
                continue
        out.append(r)
    if isinstance(limit, int) and limit >= 0:
        out = out[-limit:]
    return {"count": len(out), "verdicts": out}


def tool_ledger_stats(rows):
    """Counts by verdict type, gate-override rate, days since last override."""
    by_type = collections.Counter(r["type"] for r in rows)
    gates = [r for r in rows if r["type"] == "GATE"]
    overrides = [r for r in gates if "OVERRIDE" in r["raw"].upper()]
    tripped = [r for r in gates if "TRIPPED" in r["raw"].upper()]
    accepted = [r for r in rows
                if r["type"] == "PREMORTEM" and "accept" in r["raw"].lower()]
    one_way = [r for r in rows
               if r["type"] == "DOOR" and "one-way" in r["raw"].lower()]

    override_rate = (len(overrides) / len(gates)) if gates else 0.0

    days_since = None
    last_date = ""
    dated = [r["date"][:10] for r in overrides if r["date"]]
    if dated:
        last_date = max(dated)
        try:
            d = datetime.date.fromisoformat(last_date)
            days_since = (datetime.date.today() - d).days
        except ValueError:
            days_since = None

    # gates overridden >= 3x are suspicious (threshold wrong or gate gamed).
    suspicious = [{"gate": g, "count": c}
                  for g, c in collections.Counter(
                      r["verdict"] for r in overrides).items() if c >= 3]

    return {
        "total_verdicts": len(rows),
        "by_type": dict(by_type.most_common()),
        "gate_total": len(gates),
        "gate_tripped": len(tripped),
        "gate_overrides": len(overrides),
        "gate_override_rate": round(override_rate, 4),
        "days_since_last_override": days_since,
        "last_override_date": last_date or None,
        "premortem_accepted_risks": len(accepted),
        "one_way_doors": len(one_way),
        "suspicious_gates": suspicious,
    }


def _git(args, cwd=None):
    try:
        r = subprocess.run(["git"] + args, capture_output=True, text=True,
                           cwd=cwd)
    except (OSError, FileNotFoundError):
        return None
    return r.stdout if r.returncode == 0 else None


def tool_overrode_then_broke(rows, cwd=None):
    """Gate overrides whose judged commit was later reverted/fixed.

    Reuses verdict-viewer's correlation logic: for each commit-linked GATE
    OVERRIDE, ask git which files the commit touched, then scan <sha>..HEAD for
    later commits touching the SAME files with a revert/fix-like subject.
    Degrades to a clear status when git or commit linkage is missing (never a
    silent empty result)."""
    overrides = [r for r in rows
                 if r["type"] == "GATE" and "OVERRIDE" in r["raw"].upper()]
    linked = [r for r in overrides
              if r.get("commit") and r["commit"].lower() != "none"]
    if not overrides:
        return {"status": "no_overrides",
                "message": "No GATE OVERRIDE verdicts in the ledger — nothing "
                           "to correlate.", "findings": []}
    if _git(["rev-parse", "--git-dir"], cwd=cwd) is None:
        return {"status": "insufficient_history",
                "message": "Not a git repository (or git unavailable) — cannot "
                           "correlate overrides against later reverts/fixes.",
                "findings": []}
    if not linked:
        return {"status": "insufficient_history",
                "message": f"{len(overrides)} gate override(s) found, but none "
                           "carry a `commit:` sha (all v0/legacy or commit: "
                           "none). Log overrides with grammar-v1 commit linkage "
                           "to enable this query.",
                "findings": []}
    findings = []
    for r in linked:
        sha = r["commit"]
        files_out = _git(["show", "--name-only", "--pretty=format:", sha],
                         cwd=cwd)
        if files_out is None:
            findings.append({"date": r["date"], "commit": sha,
                             "status": "commit_not_in_repo",
                             "reverted_or_fixed_by": []})
            continue
        files = [f for f in files_out.splitlines() if f.strip()]
        if not files:
            continue
        log_out = _git(["log", "--oneline", f"{sha}..HEAD", "--"] + files,
                       cwd=cwd)
        followups = [ln for ln in (log_out or "").splitlines()
                     if ln.strip() and FIX_RE.search(ln)]
        if followups:
            findings.append({
                "date": r["date"], "verdict": r["verdict"], "commit": sha,
                "by": r["by"], "files_touched": len(files),
                "reverted_or_fixed_by": followups,
            })
    if not findings:
        return {"status": "held_up",
                "message": f"{len(linked)} commit-linked override(s) checked; "
                           "none of their files were later reverted or fixed. "
                           "Overrides held up.", "findings": []}
    return {"status": "found", "message": f"{len(findings)} override(s) "
            "correlated with later reverts/fixes.", "findings": findings}


# ---------------------------------------------------------------------------
# MCP tool + resource catalogs.
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "query_verdicts",
        "description": "Query the Casper verdict ledger, filtered by verdict "
                       "type, date, and/or commit sha. Returns structured "
                       "verdict rows (date, type, verdict, detail, commit, by).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string",
                         "description": "Verdict type filter: DOOR, GATE, "
                                        "PREMORTEM, REFUTE, DRIFT, ALTITUDE."},
                "since": {"type": "string",
                          "description": "Only verdicts on/after this ISO date "
                                         "(YYYY-MM-DD)."},
                "commit": {"type": "string",
                           "description": "Commit sha (prefix) the verdict "
                                          "judges."},
                "limit": {"type": "integer",
                          "description": "Return only the last N matches."},
            },
        },
    },
    {
        "name": "ledger_stats",
        "description": "Summary statistics for the ledger: counts by verdict "
                       "type, gate override rate, days since last gate "
                       "override, accepted risks, one-way doors, and gates "
                       "overridden suspiciously often.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "overrode_then_broke",
        "description": "Correlate GATE OVERRIDE verdicts against git history: "
                       "surface overrides whose judged commit was later "
                       "reverted or fixed. Requires a git repo and commit-linked "
                       "(grammar v1) overrides; degrades to a clear status "
                       "otherwise.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 dispatch.
# ---------------------------------------------------------------------------

class LedgerServer:
    def __init__(self, ledger_path):
        self.ledger_path = ledger_path

    def rows(self):
        return parse(self.ledger_path)

    # -- resource payload ---------------------------------------------------
    def resource_text(self):
        rows = self.rows()
        payload = {
            "uri": LEDGER_URI,
            "path": self.ledger_path,
            "grammar": ledger_grammar(self.ledger_path),
            "count": len(rows),
            "verdicts": rows,
        }
        return json.dumps(payload, indent=2)

    # -- method handlers ----------------------------------------------------
    def handle(self, method, params):
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "resources/list":
            return {"resources": [{
                "uri": LEDGER_URI,
                "name": "Casper verdict ledger",
                "description": "The project's append-only judgment ledger "
                               "(.claude/verdicts.log) as structured JSON.",
                "mimeType": "application/json",
            }]}
        if method == "resources/read":
            uri = (params or {}).get("uri")
            if uri != LEDGER_URI:
                raise RpcError(-32602, f"unknown resource uri: {uri!r}")
            return {"contents": [{
                "uri": LEDGER_URI,
                "mimeType": "application/json",
                "text": self.resource_text(),
            }]}
        if method == "tools/call":
            return self.call_tool(params or {})
        raise RpcError(-32601, f"method not found: {method}")

    def call_tool(self, params):
        name = params.get("name")
        args = params.get("arguments") or {}
        rows = self.rows()
        if name == "query_verdicts":
            result = tool_query_verdicts(rows, args)
        elif name == "ledger_stats":
            result = tool_ledger_stats(rows)
        elif name == "overrode_then_broke":
            cwd = os.path.dirname(os.path.abspath(self.ledger_path)) or None
            result = tool_overrode_then_broke(rows, cwd=cwd)
        else:
            raise RpcError(-32602, f"unknown tool: {name!r}")
        return {"content": [{"type": "text",
                             "text": json.dumps(result, indent=2)}],
                "isError": False}


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def serve(ledger_path, stdin=None, stdout=None):
    """Blocking newline-delimited JSON-RPC loop over stdio."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = LedgerServer(ledger_path)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _write(stdout, {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700,
                                      "message": "parse error"}})
            continue
        rid = req.get("id")
        method = req.get("method", "")
        # Notifications (no id) get no response.
        is_notification = "id" not in req
        try:
            result = server.handle(method, req.get("params"))
        except RpcError as e:
            if not is_notification:
                _write(stdout, {"jsonrpc": "2.0", "id": rid,
                                "error": {"code": e.code, "message": e.message}})
            continue
        except Exception as e:  # noqa: BLE001 — report, don't crash the loop.
            if not is_notification:
                _write(stdout, {"jsonrpc": "2.0", "id": rid,
                                "error": {"code": -32603,
                                          "message": f"internal error: {e}"}})
            continue
        if not is_notification:
            _write(stdout, {"jsonrpc": "2.0", "id": rid, "result": result})


def _write(stdout, obj):
    stdout.write(json.dumps(obj) + "\n")
    stdout.flush()


if __name__ == "__main__":
    serve(default_ledger_path())
