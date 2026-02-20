#!/usr/bin/env bash
# Warm up the model runner so the next Scribe call skips initial load.
# Note: `ollama ps` only shows ACTIVE runners.
set -euo pipefail

MODEL="${1:-${SCRIBE_MODEL:-llama3.1:8b}}"
KEEP_ALIVE="${2:-5m}"

# Assert the server exists (macOS app manages it)
curl -fsS http://127.0.0.1:11434/api/tags >/dev/null

# Force a runner to start (build JSON safely)
PAYLOAD="$(python3 - "$MODEL" "$KEEP_ALIVE" <<'PY'
import json
import sys

print(
    json.dumps(
        {
            "model": sys.argv[1],
            "prompt": "warmup",
            "keep_alive": sys.argv[2],
            "stream": False,
        }
    )
)
PY
)"

curl -fsS -X POST http://127.0.0.1:11434/api/generate \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" \
  >/dev/null

echo "Warmed: $MODEL (keep_alive=$KEEP_ALIVE). Run 'ollama ps' to confirm."
