#!/usr/bin/env python3
"""Food-vlog variant harness for nutrition/wellness + intent gate.

Modes:
  mock  — feed hand-written model JSON (clean + mistaken) through validate_captures,
          plus one end-to-end run_nutrition_stage over a full day note (temp dirs).
          Safe for CI; no Ollama, no writes outside temp dirs.
  live  — call the real Ollama model (call_nutrition_model + call_gate) per variant,
          then validate_captures. Writes nothing; lookups are never called.

Cases live in tests/fixtures/food_vlog_cases.yaml.

Run from a journalLinker checkout:
  .venv/bin/python scripts/food_vlog_harness.py --mode mock
  .venv/bin/python scripts/food_vlog_harness.py --mode live
  .venv/bin/python scripts/food_vlog_harness.py --mode live --json --skip-gate
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
CASES_PATH = REPO / "tests" / "fixtures" / "food_vlog_cases.yaml"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO))
os.environ.setdefault("INTENT_WATER_BOTTLE_ML", "1000")

import nutrition_ledger as nl  # noqa: E402

try:
    import yaml  # type: ignore
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "PyYAML is required for food_vlog_harness. "
        "Install with: uv pip install pyyaml  (or pip install -r requirements-dev.txt)"
    ) from exc


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    variants = doc.get("variants") if isinstance(doc, dict) else None
    if not isinstance(variants, list) or not variants:
        raise SystemExit(f"no variants in {path}")
    return variants


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _match(keyword: str, label: str) -> bool:
    return keyword.lower() in label.lower()


def score(variant_row: dict, captures: list[dict]) -> list[str]:
    exp = variant_row["expect"]
    fails: list[str] = []
    intake_items = [
        (c.get("meal_slot"), item)
        for c in captures
        if c.get("is_intake") and c.get("capture_type") != "water"
        for item in c.get("items") or []
    ]
    for row in exp.get("intake") or []:
        if isinstance(row, dict):
            keyword, slots, qty = row.get("keyword"), row.get("slots"), row.get("qty")
        else:
            keyword, slots, qty = row[0], row[1], row[2]
        hits = [(slot, item) for slot, item in intake_items if _match(keyword, item.get("label", ""))]
        if not hits:
            fails.append(f"missing intake '{keyword}'")
            continue
        if slots and not any(slot in slots for slot, _ in hits):
            fails.append(f"'{keyword}' slot {hits[0][0]} not in {sorted(slots)}")
        if qty is not None:
            qtys = [float(item.get("quantity") or 0) for _, item in hits]
            if not any(abs(q - qty) < 0.01 for q in qtys) or len(hits) > 1:
                fails.append(f"'{keyword}' qty {qtys} != {qty}")
    for keyword in exp.get("not_intake") or []:
        if any(_match(keyword, item.get("label", "")) for _, item in intake_items):
            fails.append(f"'{keyword}' logged as eaten")
    for ctype in ("craving", "want_to_cook", "want_to_eat", "preference"):
        if ctype not in exp:
            continue
        got = [
            item.get("label", "")
            for c in captures if c.get("capture_type") == ctype
            for item in c.get("items") or []
        ]
        for keyword in exp[ctype] or []:
            if not any(_match(keyword, g) for g in got):
                fails.append(f"missing {ctype} '{keyword}'")
        if not exp[ctype] and got:
            fails.append(f"unexpected {ctype} {got}")
    water_caps = [c for c in captures if c.get("capture_type") == "water"]
    if "water_oz" in exp:
        want = exp["water_oz"]
        if want is None and water_caps:
            fails.append(f"unexpected water {water_caps[0].get('water_oz')} oz")
        elif want is not None:
            got = sum(float(c.get("water_oz") or 0) for c in water_caps)
            if len(water_caps) != 1 or abs(got - want) > 0.6:
                fails.append(f"water {round(got, 1)} oz (x{len(water_caps)}) != {round(want, 1)}")
    if exp.get("no_captures") and captures:
        fails.append(f"expected no captures, got {[c['capture_type'] for c in captures]}")
    # Shared context / notes when the clean fixture provided one.
    clean_notes = ""
    clean = variant_row.get("clean") or {}
    for cap in (clean.get("captures") or []):
        note = str(cap.get("notes") or "").strip()
        if note:
            clean_notes = note
            break
    if clean_notes:
        for cap in captures:
            if cap.get("capture_type") == "water":
                continue
            got_note = str(cap.get("notes") or "").strip()
            if clean_notes.lower() not in got_note.lower() and got_note.lower() not in clean_notes.lower():
                fails.append(f"missing context/notes on {cap.get('capture_type')}")
                break
    return fails


def summarize(captures: list[dict]) -> list[str]:
    out = []
    for c in captures:
        if c["capture_type"] == "water":
            out.append(f"water {c.get('water_oz')}oz")
            continue
        items = ", ".join(
            f"{i['label']}×{nl.fmt_qty(i['quantity'])}" for i in c.get("items") or []
        )
        flag = "" if c.get("is_intake") else " (not eaten)"
        note = str(c.get("notes") or "").strip()
        suffix = f" [{note}]" if note else ""
        out.append(f"{c['capture_type']}/{c.get('meal_slot')}: {items}{flag}{suffix}")
    return out


def score_gate(variant_row: dict, intents: list[dict]) -> str:
    want = variant_row["expect"].get("gate")
    raws = [i["intent_raw"] for i in intents]
    if want is None:
        return "info"
    if want == "none":
        return "ok" if not intents else "LEAK"
    hit = all(any(_match(k, r) for r in raws) for k in want)
    return "ok" if hit else "MISS"


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------

def _raw_text(v: dict) -> str:
    note = f"# 2026-10-11\n\n> [!voice] 21:00\n> {v['text']}\n"
    return nl.extract_voice_callouts(note)[0]["raw_text"]


def run_mock(variants: list[dict]) -> dict:
    results = []
    for v in variants:
        raw = _raw_text(v)
        for flavor in ("clean", "mistaken"):
            payload = v.get(flavor)
            if payload is None:
                continue
            captures = nl.validate_captures(json.loads(json.dumps(payload)), raw)
            results.append({
                "id": v["id"],
                "flavor": flavor,
                "fails": score(v, captures),
                "got": summarize(captures),
            })
    return {"variants": results, "e2e": run_e2e(variants)}


def run_e2e(variants: list[dict]) -> dict:
    """Whole day as one note through run_nutrition_stage with clean mocks; run twice."""
    by_text = {_raw_text(v): v for v in variants}
    times = [f"{7 + i // 4:02d}:{(i % 4) * 15:02d}" for i in range(len(variants))]
    body = "\n\n".join(f"> [!voice] {t}\n> {v['text']}" for t, v in zip(times, variants))
    note_text = f"# 2026-10-11\n\n{body}\n"

    def fake_model(system, user, model=None):
        utter = user.split("Utterance:\n", 1)[1].split("\nKnown items:", 1)[0]
        return json.loads(json.dumps(by_text[utter]["clean"]))

    miss = {
        "status": "miss", "query": "", "matched_name": "", "serving_description": "",
        "source_urls": [], "per_serving": None,
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state, journal = root / "state", root / "journal"
        state.mkdir()
        journal.mkdir()
        note = journal / "2026-10-11.md"
        note.write_text(note_text, encoding="utf-8")
        with mock.patch.object(nl, "call_nutrition_model", side_effect=fake_model) as model, \
                mock.patch.object(nl, "lookup_product", return_value=miss):
            first = nl.run_nutrition_stage(note_text, note, "2026-10-11", state, journal_dir=journal)
            second = nl.run_nutrition_stage(note_text, note, "2026-10-11", state, journal_dir=journal)
        notes = sorted((journal / "wellness").glob("*.md"))
        types: dict[str, int] = {}
        for p in notes:
            t = p.read_text(encoding="utf-8").split("type: ", 1)[1].split("\n", 1)[0]
            types[t] = types.get(t, 0) + 1
        ledger_rows = len(nl.load_ledger_rows(state))
        model_calls = model.call_count
    return {
        "callouts": len(variants),
        "first": first,
        "second": second,
        "model_calls": model_calls,
        "notes_by_type": types,
        "ledger_rows": ledger_rows,
    }


def run_live(variants: list[dict], skip_gate: bool) -> dict:
    import process_intents as pi  # noqa: E402

    gate_model = os.getenv("INTENT_GATE_MODEL", pi.DEFAULT_GATE_MODEL)
    model = os.getenv("INTENT_NUTRITION_MODEL", "").strip() or nl.nutrition_model()
    results = []
    for v in variants:
        raw = _raw_text(v)
        row: dict = {"id": v["id"]}
        t0 = time.time()
        try:
            # Live extract only — never write ledger/notes or call web lookup.
            payload = nl.call_nutrition_model(
                nl.NUTRITION_LEDGER_PROMPT,
                nl.build_extract_user("21:00", raw, nl.empty_memory()),
                model=model,
            )
            captures = nl.validate_captures(json.loads(json.dumps(payload)), raw)
            if nl.needs_food_recovery(captures, raw):
                recovery = nl.call_nutrition_model(
                    nl.NUTRITION_RECOVERY_PROMPT,
                    nl.build_extract_user("21:00", raw, nl.empty_memory()),
                    model=model,
                )
                recovered = nl.validate_captures(json.loads(json.dumps(recovery)), raw)
                if any(c.get("capture_type") != "water" and c.get("items") for c in recovered):
                    captures = recovered
            row.update(raw_model=payload, fails=score(v, captures), got=summarize(captures))
        except Exception as exc:  # report, keep going
            row.update(fails=[f"extract error: {exc}"], got=[])
        row["extract_s"] = round(time.time() - t0, 1)
        if not skip_gate:
            try:
                intents = pi.call_gate(raw, gate_model, pi.resolve_gate_style(gate_model))
                row["gate"] = [
                    f"{i['category']}/{i['intent_class']}: {i['intent_raw']}" for i in intents
                ]
                row["gate_verdict"] = score_gate(v, intents)
            except Exception as exc:
                row["gate"], row["gate_verdict"] = [f"error: {exc}"], "ERROR"
        results.append(row)
        status = "PASS" if not row["fails"] else "FAIL"
        print(f"{v['id']}: {status} {row.get('gate_verdict', '')}", file=sys.stderr, flush=True)
    return {"model": model, "gate_model": gate_model, "variants": results}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["mock", "live"], default="mock")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--skip-gate", action="store_true")
    ap.add_argument("--cases", type=Path, default=CASES_PATH)
    args = ap.parse_args()
    variants = load_cases(args.cases)
    out = run_mock(variants) if args.mode == "mock" else run_live(variants, args.skip_gate)
    if args.json:
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return 0
    fails = 0
    total = 0
    for r in out["variants"]:
        status = "PASS" if not r["fails"] else "FAIL"
        total += 1
        if r["fails"]:
            fails += 1
        print(f"{r['id']:<4} {r.get('flavor', 'live'):<9} {status}  {'; '.join(r['fails'])}")
        for line in r["got"]:
            print(f"      {line}")
    if "e2e" in out:
        print("e2e:", json.dumps(out["e2e"]))
    passed = total - fails
    print(f"\n{passed}/{total} passed", file=sys.stderr)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
