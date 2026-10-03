from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.retention_cleanup import plan_log_deletes, run_cleanup


def _old(path: Path, days: int) -> None:
    when = datetime.now(timezone.utc) - timedelta(days=days)
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def test_plan_deletes_only_generated_old_logs(tmp_path: Path):
    old_log = tmp_path / "scribe-20260101-000000-1.log"
    old_log.write_text("old", encoding="utf-8")
    _old(old_log, 45)
    manual = tmp_path / "manual-notes.log"
    manual.write_text("manual", encoding="utf-8")
    _old(manual, 45)
    recent = tmp_path / "scribe-20260201-000000-1.log"
    recent.write_text("recent", encoding="utf-8")

    planned = plan_log_deletes(tmp_path, log_days=30, keep_newest=0)
    assert [item.path for item in planned] == [old_log]


def test_cleanup_preserves_latest_symlink_target(tmp_path: Path):
    old_log = tmp_path / "voice-20260101-000000-1.log"
    old_log.write_text("old", encoding="utf-8")
    _old(old_log, 45)
    (tmp_path / "voice-latest.log").symlink_to(old_log)

    result = run_cleanup(
        state_dir=tmp_path,
        apply=True,
        log_days=30,
        keep_newest=0,
        prune_intents=False,
    )
    assert result["planned"] == []
    assert old_log.exists()
