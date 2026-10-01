#!/bin/zsh
# End-to-end proof of QAJev, one job at a time. Usage: scripts/prove.sh [local|all|projects]
#   local: offline tests, live fixture tests, fixture suite, OpenRouter route, MCP live, fixture project
#   all:   local + public read-only runs (Wikipedia) + the product projects in projects/ (production, read-only)
#   projects: only the product projects (core objectives + a smoke crawl each)
# Shared-machine rules: nice 10, load gate 400/350, pause while a real gate (gate-push.sh / pre-push) runs,
# throwaway profiles under ~/.qajev/tmp, cleanup proof at the end. Stopping this script stops its current job.
set -u
cd "${0:A:h}/.."
SCOPE=${1:-local}
ENVF=${QAJEV_PROOF_ENV_FILE:-$HOME/Development/jev-ultrafast/.env}
OUT=${QAJEV_PROOF_OUT:-$HOME/.qajev/proof-runs}
LOG=$(mktemp -d "${TMPDIR:-/tmp}/qajev-prove-XXXX")
export QAJEV_LOAD_HIGH=${QAJEV_LOAD_HIGH:-400} QAJEV_LOAD_OK=${QAJEV_LOAD_OK:-350} QAJEV_LOAD_WAIT=3600
B=(--ephemeral --headless --env-file "$ENVF" --json --quiet)
JOB="" SERVER=""
cleanup() { [ -n "$JOB" ] && kill -TERM $JOB 2>/dev/null; [ -n "$SERVER" ] && kill $SERVER 2>/dev/null; wait; }
trap 'cleanup; exit 143' TERM INT
trap cleanup EXIT

summ() { python3 -c '
import json, sys
d = json.load(sys.stdin)
if "error" in d: print("  ERROR", d["error"]); sys.exit()
c = d["cost"]
print("  gate", d["gate"], "| $%.4f" % c["usd"], c["calls"], "| %ss" % round(d["seconds"]), "| jev", (d.get("models") or {}).get("jev"), "| findings", len(d["findings"]))
for s in d["scenarios"]:
    j = s.get("jev") or {}
    print("   ", s["outcome"].ljust(10), s["name"][:48].ljust(48), "%6ss" % s.get("seconds"), "act", j.get("actions"), "$%s" % s.get("cost_usd"), "|", (s.get("reason") or "")[:150])
for f in d["findings"][:10]:
    print("    finding", f["severity"], f["kind"], "|", (f.get("detail") or "")[:100], "|", f["scenario"][:30])
print("  report", d["run_dir"] + "/report.md")' 2>&1 || echo "  (no report: see $LOG/job.err)"; }

gate_running() {  # the gate script or hook itself, not a shell that only mentions it while waiting
  ps -axo command= | awk '{ for (i = 1; i <= 3 && i <= NF; i++) if ($i ~ /(gate-push\.sh|hooks\/pre-push)$/) { found = 1 } } END { exit !found }'
}
ready() { while gate_running; do echo "  gate running; waiting"; sleep 20; done; echo "  $(date +%T) load $(sysctl -n vm.loadavg)"; }
run() { nice -n 10 "$@" > "$LOG/job.json" 2> "$LOG/job.err" & JOB=$!; wait $JOB; JOB=""; summ < "$LOG/job.json"; }
fed() { curl -s http://127.0.0.1:8765/api/feedback | python3 -c 'import json,sys; print([i["email"] for i in json.load(sys.stdin)["items"]])'; }

if [ "$SCOPE" != projects ]; then
echo "== lint, types, offline tests"; uv run ruff check . | tail -1; uv run basedpyright qajev 2>&1 | tail -1
nice -n 10 uv run pytest -q 2>&1 | tail -1
.venv/bin/python tests/fixtures/serve.py 8765 >/dev/null 2>&1 & SERVER=$!
sleep 1; curl -s -o /dev/null -w "  fixture server %{http_code}\n" http://127.0.0.1:8765/

echo "== live fixture tests (guard, deaf, read-only, smoke, one Jev check)"; ready
QAJEV_LIVE=1 QAJEV_LIVE_JEV=1 QAJEV_TEST_ENV_FILE="$ENVF" nice -n 10 uv run pytest -q tests/test_live.py 2>&1 | tail -1

echo "== fixture suite: typing, mutate + server side effect, chain, phone, persona"; ready
run .venv/bin/python -m qajev run examples/fixture.yaml --out "$OUT" "${B[@]}"
echo "  server saw: $(fed)"

echo "== OpenRouter route: decisions and typing through one OpenRouter key"; ready
run env -u OPENROUTER_API_KEY .venv/bin/python -m qajev check http://127.0.0.1:8765/feedback.html --mode mutate \
  --jev-provider openrouter --max-actions 10 --cost-cap 0.05 --out "$OUT" \
  --goal 'Send feedback as Grace Hopper, email grace@example.test, with the message "Found a bug on the pricing page." Stop when a thank-you message is visible.' \
  --expect-text "Thanks, Grace Hopper" --fetch "/api/feedback=200" "${B[@]}"
echo "  server saw: $(fed)"

echo "== MCP live: qa_check with Jev, qa_smoke, browser lifecycle"; ready
nice -n 10 .venv/bin/python scripts/mcp_live.py "$ENVF" > "$LOG/mcp.log" 2>&1 & JOB=$!; wait $JOB; JOB=""
grep -v "^\s*$" "$LOG/mcp.log" | tail -8

echo "== fixture project (in-repo .qajev, local, mutate on loopback)"; ready
run .venv/bin/python -m qajev run --project examples/fixture-project --suite core "${B[@]}"

fi

if [ "$SCOPE" = all ]; then
  echo "== Wikipedia Jev goal (read-only)"; ready
  run .venv/bin/python -m qajev check https://en.wikipedia.org/wiki/Main_Page --max-actions 10 --cost-cap 0.10 --out "$OUT" \
    --goal "Find the Wikipedia article about Gödel's incompleteness theorems. Stop when that article is open." \
    --expect-url-regex "incompleteness_theorems" --expect-text "incompleteness theorems" "${B[@]}"
  echo "== Wikipedia smoke: 10 pages, robots.txt, 1 page/s, no model"; ready
  run .venv/bin/python -m qajev smoke https://en.wikipedia.org/wiki/Software_testing --max-pages 10 --out "$OUT" "${B[@]}"
fi

if [ "$SCOPE" = all ] || [ "$SCOPE" = projects ]; then
  for P in projects/*.toml(N); do
    P=${P:t:r}
    echo "== project $P: core objectives on production (read-only)"; ready
    run .venv/bin/python -m qajev run --project $P --suite core "${B[@]}"
    echo "== project $P: smoke, 10 pages, robots.txt, 1 page/s, link check"; ready
    run .venv/bin/python -m qajev smoke --project $P --max-pages 10 --check-links "${B[@]}"
  done
fi

echo "== index"; .venv/bin/python -m qajev reports --limit 12
echo "== cleanup $(date +%T)"
.venv/bin/python -m qajev browser reap
left=$(ps -axo pid,command | grep -E "qajev-[a-z-]+-[0-9]+-|qajev/.venv/bin/python -m browser_harness" | grep -v -E "grep|--type=")
[ -z "$left" ] && echo "  no QAJev Chrome or daemon left" || echo "  LEFT RUNNING:\n$left"
stray=(~/.qajev/tmp/qajev-*(N)); (( ${#stray} )) && echo "  TEMP PROFILES LEFT: $stray" || echo "  no temp profiles"
lsof -nP -iTCP:9350-9399 -sTCP:LISTEN >/dev/null 2>&1 && echo "  PORT LISTENING" || echo "  ports 9350-9399 free"
echo "== done $(date +%T)"
