# Plan: state-change events instead of heartbeats and polling

Status: proposed (2026-10-07). No code changes yet.

## Why

The local monitor (Argus) now records health as changes, not samples: a probe
row marks the moment a service's observed state changed. Two kinds of
continuous traffic remain on the journalLinker side:

1. **Polling.** The monitor calls `GET /healthz` on the voice ingest server
   (`scripts/voice_ingest_server.py`) every 10 seconds.
2. **Heartbeats.** Long-running daemons call
   `journal_linker_telemetry.emit_health_probe()` on a timer. For example,
   `feedback_sender.py --daemon` emits `health.probe` every
   `INTENT_FEEDBACK_HEARTBEAT_SEC` (default 300 s).

Neither carries information while nothing changes. Process death is already
caught without them: the monitor watches each daemon's systemd unit over
D-Bus. The useful part, "running but unable to do its job", is only known
inside the service. So it should be reported once, when it happens.

## Contract

Reuse the existing telemetry channel: one line on **stderr**, prefixed
`JOURNAL_LINKER_EVENT=`, emitted via `journal_linker_telemetry._emit`. Add one
helper:

```python
def emit_service_state(service: str, status: str, reason: str, **metrics) -> None:
    """Emit a health transition. Call only when status or reason changes."""
```

producing

```
JOURNAL_LINKER_EVENT={"event":"service.state","service":"voice-ingest","status":"ready","reason":"listening","ts":"2026-10-07T16:15:03-06:00"}
```

- `status`: `ready`, `degraded`, `recovered`, `stopping`, or `error`. The
  monitor treats `error`/`failed` as unhealthy.
- `reason`: short, stable wording per cause, so repeated causes compare equal.
- **Transitions only.** Never on a timer. A small per-process `HealthState`
  (`set(status, reason)` emits only on change) enforces this.

## Emission points

### Voice ingest server (`scripts/voice_ingest_server.py`)

- `ready` after `server_bind` succeeds and the drop dir check passes (next to
  the existing `[ingest] listening on …` line).
- `error` on the existing startup failures (drop dir unset / missing) before
  exiting non-zero.
- `degraded` when uploads start failing on the server side (the `[ingest]
  error:` branch, e.g. drop dir unwritable or disk full). `recovered` on the
  next successful write. Client rejects (4xx) are not health events.
- `stopping` on SIGTERM / KeyboardInterrupt.

### Daemons that heartbeat today (`feedback_sender.py --daemon`, others using `emit_health_probe`)

- Replace the periodic `emit_health_probe` with `HealthState.set(...)` at the
  points where the loop actually learns something: first successful poll
  (`ready`), poll/API failure (`degraded` with the error class), first success
  after failures (`recovered`), shutdown (`stopping`).
- Keep queue/offset numbers as `metrics` on those transition events if they
  are useful for debugging. Don't emit them on a schedule.
- Mark `emit_health_probe` deprecated once no caller remains.

## Monitor-side follow-up (separate repo)

- Add a journald binding for the voice ingest unit so its stderr events are
  ingested (today only its HTTP endpoint and unit state are watched).
- Relax the voice ingest HTTP binding from 10 s to a slow fallback (≥5 min) or
  disable it.
- Have the monitor's evaluation honour the latest `service.state` event, and
  stop applying silence thresholds to services that report by transition.

## Acceptance

- Unit test for `HealthState`: ready → degraded → degraded (same reason) →
  recovered emits exactly three `service.state` lines, and each one parses
  with `parse_event_line`.
- `grep -rn emit_health_probe scripts/` returns nothing once the migration is
  complete.
- After a restart of the voice ingest unit, its journal shows exactly one
  `service.state` `ready` line and no further health lines while idle.
