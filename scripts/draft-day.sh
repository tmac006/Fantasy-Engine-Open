#!/usr/bin/env bash
# Bring the whole draft assistant up with one command.
#
#   ./scripts/draft-day.sh
#
# Applies migrations, rebuilds the extension, starts the API, and reports how
# fresh the market data is. The API catches up on any stale ingest itself on
# startup, so the numbers are current before the first pick.
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"

PORT="${PORT:-8000}"

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }
warn() { printf '\033[33m%s\033[0m\n' "$1"; }

for tool in uv node; do
  command -v "$tool" >/dev/null || { warn "missing $tool (expected in ~/.local/bin)"; exit 1; }
done

say "1/4  Dependencies"
uv sync --quiet
(cd extension && npm install --silent --no-fund --no-audit)
(cd web && npm install --silent --no-fund --no-audit)

say "2/4  Database"
uv run alembic upgrade head 2>&1 | tail -1

say "3/4  Front ends"
(cd extension && npm run --silent build)
echo "extension: load unpacked from $(pwd)/extension"
(cd web && npx --no-install ng build --configuration production >/dev/null)

say "4/4  API on :$PORT"
uv run uvicorn api.main:app --port "$PORT" &
API_PID=$!
trap 'kill $API_PID 2>/dev/null || true' EXIT INT TERM

# Wait for the API, then report data freshness. Ingest catch-up runs in the
# background on startup, so a stale-looking first read is normal.
for _ in $(seq 1 60); do
  if curl -sf "http://localhost:$PORT/healthz" >/dev/null 2>&1; then break; fi
  sleep 1
done

say "Market data freshness"
curl -sf "http://localhost:$PORT/admin/ingest-status" 2>/dev/null | uv run python -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print("  (could not read ingest status)"); sys.exit()
for job in data["jobs"]:
    name = job["job"]
    age = job["age_hours"]
    if age is None:
        state, note = "NEVER RUN", "refreshing now"
    elif job["due"]:
        state, note = "STALE", "%.0fh old, refreshing now" % age
    else:
        state, note = "ok", "%.1fh old" % age
    print("  %-12s %-10s %s" % (name, state, note))
' || echo "  (status unavailable)"

cat <<EOF

Ready.
  Draft day  - open your draft room; the side panel auto-detects Sleeper and ESPN.
  In season  - http://localhost:$PORT/app/  (news, leagues, data freshness)

Press Ctrl-C to stop.
EOF

wait $API_PID
