import argparse
import json
import os
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path


DATE_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
DATE_TERM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TEMPORAL_WINDOW_DAYS = 90
DEFAULT_MIN_COOCCURRENCE = 3


def load_local_env(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
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


def parse_cli(argv: list[str] | None = None) -> argparse.Namespace:
    load_local_env(Path(__file__).with_name(".env"))
    parser = argparse.ArgumentParser(description="Generate vault relationship map from journal wikilinks.")
    parser.add_argument("--journal-dir", default=os.getenv("SCRIBE_JOURNAL_DIR"))
    parser.add_argument("--learning-file", default=str(Path(__file__).with_name("scribe_learning.json")))
    parser.add_argument("--output-dir", default=None, help="Defaults to <journal_dir>/Insights/")
    parser.add_argument("--min-cooccurrence", type=int, default=DEFAULT_MIN_COOCCURRENCE)
    return parser.parse_args(argv)


def _extract_links_from_text(text: str) -> list[str]:
    links = []
    for raw in WIKILINK_RE.findall(text):
        target = raw.split("|", 1)[0].split("#", 1)[0].strip().lower()
        if not target or DATE_TERM_RE.fullmatch(target):
            continue
        links.append(target)
    return links


def scan_journal_links(journal_dir: Path) -> dict[str, list[str]]:
    """Return {date_str: [term, ...]} for all YYYY-MM-DD.md files."""
    result: dict[str, list[str]] = {}
    for path in journal_dir.iterdir():
        if not DATE_FILE_RE.fullmatch(path.name):
            continue
        date_str = path.stem
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        links = _extract_links_from_text(text)
        if links:
            result[date_str] = links
    return result


def build_cooccurrence(
    daily_links: dict[str, list[str]], min_cooccurrence: int = DEFAULT_MIN_COOCCURRENCE
) -> dict[tuple[str, str], int]:
    """Count how many notes each pair of terms co-appears in."""
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for terms in daily_links.values():
        unique = sorted(set(terms))
        for i in range(len(unique)):
            for j in range(i + 1, len(unique)):
                counts[(unique[i], unique[j])] += 1
    return {pair: count for pair, count in counts.items() if count >= min_cooccurrence}


def cluster_terms(cooccurrence: dict[tuple[str, str], int]) -> list[list[str]]:
    """Union-find connected components from co-occurrence pairs."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for a, b in cooccurrence:
        if a not in parent:
            parent[a] = a
        if b not in parent:
            parent[b] = b
        union(a, b)

    groups: dict[str, list[str]] = defaultdict(list)
    for term in parent:
        groups[find(term)].append(term)

    return [sorted(members) for members in groups.values() if len(members) >= 2]


def load_memory_signals(learning_file: str) -> dict[str, dict]:
    """Load term_memory from scribe_learning.json; returns {} on any failure."""
    path = Path(learning_file)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tm = data.get("term_memory", {})
        if isinstance(tm, dict):
            return {k.lower(): v for k, v in tm.items() if isinstance(v, dict)}
    except Exception:
        pass
    return {}


def _term_strength(term: str, memory_signals: dict[str, dict]) -> float:
    """0–1 strength from success/failure ratio in term_memory."""
    mem = memory_signals.get(term)
    if not mem:
        return 0.0
    success = int(mem.get("success", 0))
    failure = int(mem.get("failure", 0))
    total = success + failure
    if total == 0:
        return 0.0
    return round(success / total, 2)


def _term_last_active(term: str, daily_links: dict[str, list[str]]) -> str:
    """Return the most recent date_str where term appears, or ''."""
    dates = [d for d, links in daily_links.items() if term in links]
    return max(dates) if dates else ""


def _top_pairs(cooccurrence: dict[tuple[str, str], int], n: int = 10) -> list[tuple[tuple[str, str], int]]:
    return sorted(cooccurrence.items(), key=lambda x: -x[1])[:n]


def _detect_temporal_peaks(
    daily_links: dict[str, list[str]], cutoff_days: int = TEMPORAL_WINDOW_DAYS
) -> list[dict]:
    """Find terms with notable recent activity vs their baseline."""
    today = date.today()
    cutoff = today.replace(year=today.year - 1) if cutoff_days >= 365 else (
        datetime.strptime(
            (today - __import__("datetime").timedelta(days=cutoff_days)).isoformat(), "%Y-%m-%d"
        ).date()
    )

    term_dates: dict[str, list[date]] = defaultdict(list)
    for date_str, links in daily_links.items():
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        for term in links:
            term_dates[term].append(d)

    peaks = []
    for term, dates in term_dates.items():
        recent = [d for d in dates if d >= cutoff]
        if len(recent) < 2:
            continue
        recent_count = len(recent)
        total_count = len(dates)
        if total_count == 0:
            continue
        recency_ratio = recent_count / total_count
        if recency_ratio < 0.5 or recent_count < 2:
            continue
        last = max(dates)
        # Simple trend: is the term still being used in last 30 days?
        very_recent = [d for d in recent if (today - d).days <= 30]
        trend = "rising" if very_recent else "cooling"
        # Approximate ISO week of peak
        peak_week = max(recent).isocalendar()
        peaks.append({
            "term": term,
            "trend": trend,
            "peak_week": f"{peak_week[0]}-W{peak_week[1]:02d}",
            "recent_count": recent_count,
        })
    peaks.sort(key=lambda x: (-x["recent_count"], x["term"]))
    return peaks[:8]


def render_vault_map(
    clusters: list[list[str]],
    cooccurrence: dict[tuple[str, str], int],
    memory_signals: dict[str, dict],
    daily_links: dict[str, list[str]],
    today_str: str,
) -> str:
    entity_count = len({term for pair in cooccurrence for term in pair})
    lines = [
        "# Vault Relationship Map",
        f"_Last updated: {today_str} | {entity_count} tracked entities_",
        "",
    ]

    # Strong connections
    top_pairs = _top_pairs(cooccurrence, n=10)
    if top_pairs:
        lines.append("## Strong Connections")
        for (a, b), count in top_pairs:
            lines.append(f"- [[{a}]] ↔ [[{b}]] ({count} notes)")
        lines.append("")

    # Clusters
    if clusters:
        lines.append("## Clusters")
        for members in sorted(clusters, key=lambda c: -len(c)):
            # Label by highest-strength term; fall back to alphabetically first
            def sort_key(t: str) -> tuple:
                return (-_term_strength(t, memory_signals), t)
            label_term = min(members, key=sort_key)
            formatted = ", ".join(f"[[{m}]]" for m in members)
            lines.append(f"- **{label_term}-cluster:** {formatted}")
        lines.append("")

    # Temporal peaks
    peaks = _detect_temporal_peaks(daily_links)
    if peaks:
        lines.append(f"## Temporal Peaks (last {TEMPORAL_WINDOW_DAYS} days)")
        for peak in peaks:
            lines.append(f"- [[{peak['term']}]] — peaked {peak['peak_week']}, {peak['trend']}")
        lines.append("")

    return "\n".join(lines)


def render_vault_json(
    clusters: list[list[str]],
    cooccurrence: dict[tuple[str, str], int],
    memory_signals: dict[str, dict],
    daily_links: dict[str, list[str]],
    today_str: str,
) -> dict:
    # Build per-term co-occurrence list
    term_cooccurs: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for (a, b), count in cooccurrence.items():
        term_cooccurs[a].append((b, count))
        term_cooccurs[b].append((a, count))

    all_terms = {term for pair in cooccurrence for term in pair}
    entities: dict[str, dict] = {}
    for term in sorted(all_terms):
        co_list = sorted(term_cooccurs[term], key=lambda x: -x[1])
        entities[term] = {
            "co_occurs_with": [[t, c] for t, c in co_list],
            "strength": _term_strength(term, memory_signals),
            "last_active": _term_last_active(term, daily_links),
        }

    return {
        "generated": today_str,
        "source_notes": len(daily_links),
        "entities": entities,
    }


def write_outputs(output_dir: Path, md_str: str, json_dict: dict) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / "vault_map.md"
    json_path = output_dir / "vault_relationships.json"
    md_path.write_text(md_str, encoding="utf-8")
    json_path.write_text(json.dumps(json_dict, indent=2, ensure_ascii=False), encoding="utf-8")
    return md_path, json_path


def build_vault_map(
    journal_dir: str,
    learning_file: str,
    output_dir: str | None = None,
    min_cooccurrence: int = DEFAULT_MIN_COOCCURRENCE,
) -> tuple[Path, Path]:
    journal_path = Path(journal_dir)
    resolved_output_dir = Path(output_dir) if output_dir else journal_path / "Insights"
    today_str = date.today().isoformat()

    daily_links = scan_journal_links(journal_path)
    cooccurrence = build_cooccurrence(daily_links, min_cooccurrence)
    clusters = cluster_terms(cooccurrence)
    memory_signals = load_memory_signals(learning_file)

    md_str = render_vault_map(clusters, cooccurrence, memory_signals, daily_links, today_str)
    json_dict = render_vault_json(clusters, cooccurrence, memory_signals, daily_links, today_str)

    return write_outputs(resolved_output_dir, md_str, json_dict)


def main() -> int:
    args = parse_cli()

    if not args.journal_dir:
        print("Error: journal directory is required. Use --journal-dir or SCRIBE_JOURNAL_DIR.")
        return 2

    try:
        md_path, json_path = build_vault_map(
            journal_dir=args.journal_dir,
            learning_file=args.learning_file,
            output_dir=args.output_dir,
            min_cooccurrence=args.min_cooccurrence,
        )
    except Exception as exc:
        print(f"Error: {exc}")
        return 1

    print(f"[vault_mapper] wrote={md_path}")
    print(f"[vault_mapper] wrote={json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
