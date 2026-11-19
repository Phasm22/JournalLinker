#!/usr/bin/env bash
# Ensure Ollama is reachable before journalLinker / pushwatcher jobs that need it.
# Safe to source: uses return, not exit (exit in a sourced script kills the parent).
#
# There is one Ollama on this host: the user unit ollama.service on
# 127.0.0.1:11434 (~/bin/ai-core). Systemd units get OLLAMA_URL from
# ~/bin/ai-core/ollama.env; if the server is down, start it — no fallbacks.
set -euo pipefail

ensure_ollama() {
  local model="${SCRIBE_MODEL:-llama3.1:8b}"
  local url="${OLLAMA_URL:-http://127.0.0.1:11434}"

  _ollama_has_model() {
    curl -fsS --max-time 3 "${url}/api/tags" | python3 -c "
import json, sys
model = sys.argv[1]
names = [m.get('name', '') for m in json.load(sys.stdin).get('models', [])]
base = model.split(':', 1)[0]
ok = any(n == model or n.split(':', 1)[0] == base for n in names)
sys.exit(0 if ok else 1)
" "$model" 2>/dev/null
  }

  if ! curl -fsS --max-time 3 "${url}/api/version" >/dev/null 2>&1; then
    # ExecStartPost in ollama.service blocks until the API answers.
    systemctl --user start ollama.service 2>/dev/null || true
  fi

  if _ollama_has_model; then
    export OLLAMA_URL="$url"
    export OLLAMA_HOST="$url"
    return 0
  fi

  echo "ensure-ollama: ${model} not available on ${url} (see ~/bin/ai-core/models.txt)" >&2
  return 1
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  ensure_ollama
  exit $?
fi

ensure_ollama
