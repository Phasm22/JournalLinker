#!/bin/bash
# Set SCRIBE_JOURNAL_DIR in a LaunchAgent plist without PlistBuddy quoting issues
# (paths with apostrophes, e.g. "TJ's Think Tank", are safe).
#
# Usage:
#   ./scripts/patch_launchagent_journal.sh "/path/to/journal" [path/to/agent.plist]
# Default plist: ~/Library/LaunchAgents/com.journal-linker.scribe.plist

set -euo pipefail

JOURNAL="${1:?Pass the journal directory path as the first argument.}"
PLIST="${2:-$HOME/Library/LaunchAgents/com.journal-linker.scribe.plist}"

if [[ ! -f "$PLIST" ]]; then
  echo "Missing plist: $PLIST" >&2
  echo "Copy launchd/Scribe.example.plist there first." >&2
  exit 1
fi

export SCRIBE_JOURNAL_DIR="$JOURNAL"
export PATCH_PLIST="$PLIST"

python3 <<'PY'
import os
import plistlib

path = os.environ["PATCH_PLIST"]
journal = os.environ["SCRIBE_JOURNAL_DIR"]

with open(path, "rb") as f:
    data = plistlib.load(f)

env = data.setdefault("EnvironmentVariables", {})
if not isinstance(env, dict):
    env = {}
    data["EnvironmentVariables"] = env

env["SCRIBE_JOURNAL_DIR"] = journal

with open(path, "wb") as f:
    try:
        plistlib.dump(data, f, fmt=plistlib.FMT_XML)
    except TypeError:
        plistlib.dump(data, f)

print("Updated", path)
print("SCRIBE_JOURNAL_DIR =", journal)
PY
