#!/usr/bin/env bash
# Ensure Ollama is reachable before journalLinker / pushwatcher jobs that need it.
# Safe to source: uses return, not exit (exit in a sourced script kills the parent).
set -euo pipefail

ensure_ollama() {
  local model="${SCRIBE_MODEL:-llama3.1:8b}"
  local url

  _export_ollama() {
    export OLLAMA_URL="$1"
    export OLLAMA_HOST="$1"
  }

  _ollama_has_model() {
    local base_url="$1"
    curl -fsS --max-time 3 "${base_url}/api/tags" | python3 -c "
import json, sys
model = sys.argv[1]
names = [m.get('name', '') for m in json.load(sys.stdin).get('models', [])]
base = model.split(':', 1)[0]
ok = any(n == model or n.split(':', 1)[0] == base for n in names)
sys.exit(0 if ok else 1)
" "$model" 2>/dev/null
  }

  for url in "${OLLAMA_URL:-http://127.0.0.1:11434}" "http://127.0.0.1:11435"; do
    if _ollama_has_model "$url"; then
      _export_ollama "$url"
      return 0
    fi
  done

  if command -v pc-stacks >/dev/null 2>&1; then
    pc-stacks up palindrome 2>/dev/null || true
    if _ollama_has_model "http://127.0.0.1:11435"; then
      _export_ollama "http://127.0.0.1:11435"
      return 0
    fi
  fi

  if systemctl is-active ollama.service >/dev/null 2>&1 || sudo systemctl start ollama.service 2>/dev/null; then
    local _i
    for _i in $(seq 1 30); do
      if _ollama_has_model "${OLLAMA_URL:-http://127.0.0.1:11434}"; then
        _export_ollama "${OLLAMA_URL:-http://127.0.0.1:11434}"
        return 0
      fi
      sleep 2
    done
  fi

  echo "ensure-ollama: ${model} not available on Ollama (tried :11434 and :11435)" >&2
  return 1
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  ensure_ollama
  exit $?
fi

ensure_ollama
