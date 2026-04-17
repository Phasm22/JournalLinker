"""JournalLinker environment bootstrap.

Historically, JournalLinker used a repo-local `.env` next to the top-level scripts.
That pattern is convenient for development but a poor default for secrets.

Bootstrap precedence (never overwrites existing os.environ values):
1) `JOURNAL_LINKER_ENV_FILE` if set **and the path exists**
2) `XDG_CONFIG_HOME/journal-linker/journal-linker.env` (fallback: `~/.config/journal-linker/journal-linker.env`)
   - Compatibility: if that file is missing, also try `.../journal-linker/env` (older filename used by earlier systemd/docs)
3) Optional legacy: `<repo_root>/.env` ONLY if `JOURNAL_LINKER_DOTENV=1`

This module is intentionally tiny and stdlib-only so every entrypoint can import it safely.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_key_value_file(path: Path) -> int:
    """Load KEY=VAL pairs into os.environ. Returns number of vars set."""
    if not path.exists():
        return 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return 0

    set_count = 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and ((value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")):
            value = value[1:-1]
        os.environ[key] = value
        set_count += 1
    return set_count


def bootstrap_journal_linker_env(*, repo_root: Path) -> None:
    """Populate os.environ from safer defaults; idempotent."""
    if os.environ.get("JOURNAL_LINKER_ENV_BOOTSTRAPPED") == "1":
        return

    explicit = (os.environ.get("JOURNAL_LINKER_ENV_FILE") or "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if p.exists():
            load_key_value_file(p)
            os.environ["JOURNAL_LINKER_ENV_BOOTSTRAPPED"] = "1"
            return

    xdg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
    cfg_home = Path(xdg).expanduser() if xdg else (Path.home() / ".config")
    user_env = (cfg_home / "journal-linker" / "journal-linker.env").expanduser()
    legacy_user_env = (cfg_home / "journal-linker" / "env").expanduser()
    if user_env.exists():
        load_key_value_file(user_env)
        os.environ["JOURNAL_LINKER_ENV_BOOTSTRAPPED"] = "1"
        return
    if legacy_user_env.exists():
        load_key_value_file(legacy_user_env)
        os.environ["JOURNAL_LINKER_ENV_BOOTSTRAPPED"] = "1"
        return

    dotenv_flag = (os.environ.get("JOURNAL_LINKER_DOTENV") or "").strip().lower()
    if dotenv_flag in {"1", "true", "yes", "on"}:
        legacy = (repo_root / ".env").expanduser()
        load_key_value_file(legacy)

    os.environ["JOURNAL_LINKER_ENV_BOOTSTRAPPED"] = "1"
