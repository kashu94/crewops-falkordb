#!/bin/sh
# Start FalkorDB in the background (in memory only), then the app.
# The app loads the seed data when the graph is empty, so every restart starts clean.
set -e
redis-server --loadmodule /var/lib/falkordb/bin/falkordb.so \
  --dir /tmp --save "" --appendonly no --daemonize yes
until redis-cli ping >/dev/null 2>&1; do sleep 0.2; done
exec /app/.venv/bin/crewops serve --host 0.0.0.0 --port "${PORT:-8000}"
