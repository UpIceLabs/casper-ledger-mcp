#!/usr/bin/env bash
# Test matrix for ledger-mcp/server.py. Plain bash + python3, zero deps.
# Drives the server over stdio with a real JSON-RPC session and asserts on the
# responses with a tiny python jq-substitute.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SRV="$HERE/../server.py"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
LOG="$TMP/verdicts.log"

# Synthetic v1 ledger fixture.
cat > "$LOG" <<'EOF'
# grammar: v1
2026-07-09 | DOOR | one-way | locked: uuid pks | commit: none | by: operator
2026-07-09 | GATE | TRIPPED p95=340ms>200ms | on-fail: STOP executed | commit: 1a2b3c4 | by: claude
2026-07-10 | GATE | OVERRIDE 200->400ms | reason: cold cache accepted | commit: 9f8e7d6 | by: operator
2026-07-11 | REFUTE | REFUTED "search works" | broke on: unicode names | commit: 5e6f7a8 | by: claude
2026-07-12 | PREMORTEM | risk accepted: no rollback plan | commit: none | by: operator
EOF

# A JSON-RPC session: initialize, initialized notification, tools/list,
# resources/list, resources/read, and three tools/call requests.
REQ="$TMP/session.jsonl"
cat > "$REQ" <<EOF
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"resources/list"}
{"jsonrpc":"2.0","id":4,"method":"resources/read","params":{"uri":"ledger://verdicts"}}
{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"query_verdicts","arguments":{"type":"GATE"}}}
{"jsonrpc":"2.0","id":6,"method":"ledger_stats_bad"}
{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"ledger_stats","arguments":{}}}
{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"overrode_then_broke","arguments":{}}}
EOF

OUT="$TMP/out.jsonl"
LEDGER_PATH="$LOG" python3 "$SRV" < "$REQ" > "$OUT"

pass=0; failc=0
ok()  { pass=$((pass+1)); }
bad() { failc=$((failc+1)); echo "FAIL: $1"; }

# Assert helper: python reads out.jsonl, finds response by id, evals an expr
# over `r` (the response dict). Prints TRUE/FALSE.
assert() { # id  python-expr  label
  local got
  got="$(python3 - "$OUT" "$1" "$2" <<'PY'
import json, sys
path, rid, expr = sys.argv[1], int(sys.argv[2]), sys.argv[3]
r = None
for line in open(path):
    line = line.strip()
    if not line:
        continue
    o = json.loads(line)
    if o.get("id") == rid:
        r = o
        break
print("TRUE" if (r is not None and eval(expr)) else "FALSE")
PY
)"
  [ "$got" = "TRUE" ] && ok || bad "$3"
}

# We got a well-formed line per non-notification request (8 of them).
[ "$(grep -c . "$OUT")" = 8 ] && ok || bad "one response per request (notification silent)"

# initialize handshake
assert 1 'r["result"]["protocolVersion"]=="2024-11-05"' "initialize protocolVersion"
assert 1 'r["result"]["serverInfo"]["name"]=="ledger-mcp"' "initialize serverInfo name"
assert 1 '"tools" in r["result"]["capabilities"] and "resources" in r["result"]["capabilities"]' "initialize advertises tools+resources"

# tools/list
assert 2 'sorted(t["name"] for t in r["result"]["tools"])==["ledger_stats","overrode_then_broke","query_verdicts"]' "tools/list lists 3 tools"
assert 2 'all("inputSchema" in t for t in r["result"]["tools"])' "each tool has inputSchema"

# resources/list + read
assert 3 'r["result"]["resources"][0]["uri"]=="ledger://verdicts"' "resources/list uri"
assert 4 'json.loads(r["result"]["contents"][0]["text"])["count"]==5' "resources/read returns 5 verdicts"
assert 4 'json.loads(r["result"]["contents"][0]["text"])["grammar"]=="v1"' "resources/read detects grammar v1"

# query_verdicts filter by type
assert 5 'json.loads(r["result"]["content"][0]["text"])["count"]==2' "query_verdicts GATE filter -> 2"
assert 5 'all(v["type"]=="GATE" for v in json.loads(r["result"]["content"][0]["text"])["verdicts"])' "query_verdicts all GATE"

# unknown method -> JSON-RPC error, loop survives
assert 6 'r.get("error",{}).get("code")==-32601' "unknown method -> -32601"

# ledger_stats
assert 7 'json.loads(r["result"]["content"][0]["text"])["gate_overrides"]==1' "ledger_stats gate_overrides==1"
assert 7 'json.loads(r["result"]["content"][0]["text"])["total_verdicts"]==5' "ledger_stats total==5"
assert 7 'json.loads(r["result"]["content"][0]["text"])["one_way_doors"]==1' "ledger_stats one_way_doors==1"

# overrode_then_broke degrades gracefully (fixture dir is not a git repo)
assert 8 'json.loads(r["result"]["content"][0]["text"])["status"] in ("insufficient_history","held_up","found","no_overrides")' "overrode_then_broke returns a known status"

echo "ledger-mcp matrix: $pass passed, $failc failed"
exit "$((failc > 0 ? 1 : 0))"
