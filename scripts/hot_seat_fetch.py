#!/usr/bin/env python3
"""hot_seat_fetch.py — Fetch a company's latest 10-K from SEC EDGAR into hot_seat.

Single source of truth for the "pull a filing" route. Both the interactive
`/hot-seat` skill and the automated journal command pipeline call this.

Resolves a spoken/typed company reference ("Ford", "Ford Motor", "F") to a
ticker + CIK, finds the most recent 10-K, and downloads the primary document
into ~/Documents/hot_seat/TICKER_10K_FYYYYY/. The hot_seat daemon indexes it
(60s/30s debounce); pass --index to also kick an llmLibrarian add_silo over MCP.

Usage:
    python3 scripts/hot_seat_fetch.py "Ford"
    python3 scripts/hot_seat_fetch.py "Ford" --dry-run
    python3 scripts/hot_seat_fetch.py AAPL --json
    python3 scripts/hot_seat_fetch.py "microsoft" --hot-seat-dir /tmp/hs

Env vars:
    SEC_EDGAR_USER_AGENT   User-Agent for SEC requests (required by SEC).
                           Default: "TJResearch/1.0 tj@example.com".
    HOT_SEAT_DIR           Download target (default: ~/Documents/hot_seat).
    HOT_SEAT_TICKER_RESOLVER  openai|local|auto (default: auto — OpenAI first,
                           local EDGAR name-match fallback).
    HOT_SEAT_ALIAS_FILE    Path to a personal ticker-alias JSON override
                           (default: scripts/ticker_aliases.json in-repo).
    INTENT_ROUTING_MODEL   OpenAI model for name->ticker (default: gpt-4o-mini).
    OPENAI_API_KEY         Enables the OpenAI resolver.

Exit codes:
    0  success (downloaded, or already present)
    2  could not resolve ticker
    3  no 10-K found for issuer
    4  download / network failure
"""

import argparse
import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from text_normalize import normalize_command_text

DEFAULT_USER_AGENT = "TJResearch/1.0 tj@example.com"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
ARCHIVE_DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{doc}"

EXIT_OK = 0
EXIT_NO_TICKER = 2
EXIT_NO_FILING = 3
EXIT_DOWNLOAD = 4

CACHE_TTL_SECONDS = 24 * 3600


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _user_agent() -> str:
    return os.getenv("SEC_EDGAR_USER_AGENT", "").strip() or DEFAULT_USER_AGENT


def _http_get(url: str, *, timeout: float = 20.0, retries: int = 3) -> bytes:
    last_exc: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={
            "User-Agent": _user_agent(),
            "Accept-Encoding": "gzip, deflate",
            "Accept": "*/*",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                encoding = (resp.headers.get("Content-Encoding") or "").lower()
                if "gzip" in encoding:
                    import gzip
                    data = gzip.decompress(data)
                elif "deflate" in encoding:
                    import zlib
                    data = zlib.decompress(data)
                return data
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last_exc = exc
            # SEC rate-limits ~10 req/s; be polite between retries.
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url}: {last_exc}")


def _http_get_json(url: str, **kw) -> object:
    return json.loads(_http_get(url, **kw).decode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# Company tickers (cached)
# ---------------------------------------------------------------------------

def _cache_dir() -> Path:
    raw = os.getenv("HOT_SEAT_CACHE_DIR", "").strip()
    d = Path(raw).expanduser() if raw else (
        Path.home() / ".local" / "state" / "journal-linker" / "hot_seat"
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_company_tickers(*, force_refresh: bool = False) -> list[dict]:
    """Return EDGAR ticker table as a list of {cik_str, ticker, title}."""
    cache = _cache_dir() / "company_tickers.json"
    fresh = (
        cache.exists()
        and (time.time() - cache.stat().st_mtime) < CACHE_TTL_SECONDS
    )
    if fresh and not force_refresh:
        try:
            raw = json.loads(cache.read_text(encoding="utf-8"))
            return _normalize_ticker_table(raw)
        except Exception:
            pass
    raw = _http_get_json(COMPANY_TICKERS_URL)
    try:
        cache.write_text(json.dumps(raw), encoding="utf-8")
    except Exception:
        pass
    return _normalize_ticker_table(raw)


def _normalize_ticker_table(raw: object) -> list[dict]:
    rows: list[dict] = []
    values = raw.values() if isinstance(raw, dict) else (raw or [])
    for row in values:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker", "")).strip()
        title = str(row.get("title", "")).strip()
        cik = row.get("cik_str", row.get("cik"))
        if not ticker or cik is None:
            continue
        rows.append({"ticker": ticker.upper(), "title": title, "cik_str": int(cik)})
    return rows


# ---------------------------------------------------------------------------
# Ticker resolution
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    text = re.sub(r"[^\w\s]+", " ", str(text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def local_resolve_ticker(query: str, tickers: list[dict]) -> dict | None:
    """Score EDGAR titles/tickers against a query. Company name beats ticker
    collisions unless the query is clearly an explicit ticker (uppercase, <=5).
    """
    q_raw = str(query or "").strip()
    ql = _norm(q_raw)
    if not ql:
        return None
    q_tokens = ql.split()
    q_is_tickerish = q_raw.isupper() and 1 <= len(q_raw) <= 5

    best: dict | None = None
    best_score = 0
    for row in tickers:
        ticker = row["ticker"]
        title = row["title"]
        tnorm = _norm(title)
        tl = ticker.lower()
        score = 0
        if ql == tl:
            score = 100 if q_is_tickerish else 55
        if tnorm == ql:
            score = max(score, 95)
        elif tnorm.startswith(ql + " "):
            score = max(score, 85)
        else:
            t_tokens = tnorm.split()
            if q_tokens and all(t in t_tokens for t in q_tokens):
                score = max(score, 80 if t_tokens[: len(q_tokens)] == q_tokens else 70)
        if score > best_score or (
            score == best_score
            and best is not None
            and len(title) < len(best["title"])
        ):
            best = {"ticker": ticker, "cik_str": row["cik_str"], "title": title}
            best_score = score
    if best is None or best_score <= 0:
        return None
    best["score"] = best_score
    best["source"] = "edgar_local"
    return best


# Fuzzy-match thresholds are length-aware: a single-letter transposition in a
# short ticker already swings difflib's ratio a lot (measured: "OCM" vs the
# real "OMC" = 0.667), so ticker-vs-ticker comparisons use a low bar. Longer
# title comparisons keep a high bar since more characters make coincidental
# similarity far less likely.
_FUZZY_TICKER_THRESHOLD = 0.6
_FUZZY_TITLE_THRESHOLD = 0.8


def fuzzy_resolve_ticker(query: str, tickers: list[dict]) -> dict | None:
    """Last-resort similarity match for near-misses exact/token/prefix
    scoring in local_resolve_ticker can't catch — e.g. a short ticker heard
    as a letter transposition ("OCM" spoken/heard for the real "OMC").

    Compares against both raw ticker symbols (what actually catches short
    transpositions — token-subset scoring in local_resolve_ticker doesn't
    help for single short tokens) and normalized titles (for longer company
    names). Returns a low_confidence result so callers can flag it.
    """
    q_raw = str(query or "").strip()
    ql = _norm(q_raw)
    if not ql:
        return None

    best: dict | None = None
    best_ratio = 0.0
    for row in tickers:
        ticker = row["ticker"]
        title = row["title"]
        tnorm = _norm(title)

        ticker_ratio = difflib.SequenceMatcher(None, q_raw.upper(), ticker).ratio()
        if ticker_ratio >= _FUZZY_TICKER_THRESHOLD and ticker_ratio > best_ratio:
            best, best_ratio = row, ticker_ratio

        title_ratio = difflib.SequenceMatcher(None, ql, tnorm).ratio()
        if title_ratio >= _FUZZY_TITLE_THRESHOLD and title_ratio > best_ratio:
            best, best_ratio = row, title_ratio

    if best is None:
        return None
    return {
        "ticker": best["ticker"],
        "cik_str": best["cik_str"],
        "title": best["title"],
        "source": "fuzzy",
        "score": round(best_ratio, 3),
        "low_confidence": True,
    }


def openai_resolve_ticker(query: str) -> str | None:
    """Ask OpenAI for the most likely ticker. Returns a ticker or None."""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        import openai  # type: ignore
    except ImportError:
        return None
    model = os.getenv("INTENT_ROUTING_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
    prompt = (
        "You map a spoken or typed company reference to its US-listed stock "
        "ticker for an SEC EDGAR lookup. Prefer the largest, most well-known "
        "issuer for ambiguous common names (e.g. 'Ford' -> F, the automaker). "
        "Respond with JSON only: {\"ticker\": \"<SYMBOL>\", \"company\": \"<name>\"}. "
        "If you cannot determine it, use an empty ticker string.\n\n"
        f"Reference: {query!r}"
    )
    try:
        client = openai.OpenAI(api_key=api_key)
        completion = client.chat.completions.create(
            model=model,
            max_tokens=60,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        raw = completion.choices[0].message.content or "{}"
        data = json.loads(raw)
        ticker = str(data.get("ticker", "")).strip().upper()
        return ticker or None
    except Exception:
        return None


_DEFAULT_ALIAS_FILE = Path(__file__).resolve().parent / "ticker_aliases.json"


def load_ticker_aliases() -> dict[str, str]:
    """Load the ticker-alias override table: uppercase query -> canonical ticker.

    A manually curated correction list for known misheard-transcription
    cases (e.g. "OCM" -> "OMC" for Omnicom), not a general alias/nickname
    dictionary — that's what fuzzy matching and OpenAI resolution are for.
    Re-read on every call (the file is tiny; no TTL/caching needed).
    """
    raw_path = os.getenv("HOT_SEAT_ALIAS_FILE", "").strip()
    path = Path(raw_path).expanduser() if raw_path else _DEFAULT_ALIAS_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    aliases: dict[str, str] = {}
    for key, value in data.items():
        ticker = value.get("ticker") if isinstance(value, dict) else value
        if ticker:
            aliases[str(key).strip().upper()] = str(ticker).strip().upper()
    return aliases


def resolve_ticker(query: str, *, tickers: list[dict] | None = None,
                   mode: str = "auto") -> dict | None:
    """Resolve a company reference to {ticker, cik_str, title, source}."""
    query = normalize_command_text(str(query or ""))
    if tickers is None:
        tickers = load_company_tickers()
    by_ticker = {row["ticker"]: row for row in tickers}

    aliases = load_ticker_aliases()
    alias_ticker = aliases.get(query.strip().upper())
    if alias_ticker and alias_ticker in by_ticker:
        row = by_ticker[alias_ticker]
        return {
            "ticker": row["ticker"],
            "cik_str": row["cik_str"],
            "title": row["title"],
            "source": "alias",
        }

    if mode in ("auto", "openai"):
        oai = openai_resolve_ticker(query)
        if oai and oai in by_ticker:
            row = by_ticker[oai]
            return {
                "ticker": row["ticker"],
                "cik_str": row["cik_str"],
                "title": row["title"],
                "source": "openai",
            }
        if mode == "openai":
            return None

    return local_resolve_ticker(query, tickers) or fuzzy_resolve_ticker(query, tickers)


# ---------------------------------------------------------------------------
# Filing resolution
# ---------------------------------------------------------------------------

def resolve_latest_filing(cik: int, form: str = "10-K") -> dict | None:
    """Return the most recent filing of `form` for a CIK, or None."""
    cik10 = f"{int(cik):010d}"
    data = _http_get_json(SUBMISSIONS_URL.format(cik10=cik10))
    recent = (data.get("filings", {}) or {}).get("recent", {}) or {}
    forms = recent.get("form", []) or []
    accns = recent.get("accessionNumber", []) or []
    docs = recent.get("primaryDocument", []) or []
    filing_dates = recent.get("filingDate", []) or []
    report_dates = recent.get("reportDate", []) or []
    for i, f in enumerate(forms):
        if str(f).strip().upper() != form.upper():
            continue
        accn = accns[i] if i < len(accns) else ""
        report_date = report_dates[i] if i < len(report_dates) else ""
        filing_date = filing_dates[i] if i < len(filing_dates) else ""
        fy = (report_date[:4] or filing_date[:4]) if (report_date or filing_date) else ""
        return {
            "accession": accn,
            "accession_nodash": accn.replace("-", ""),
            "primary_doc": docs[i] if i < len(docs) else "",
            "filing_date": filing_date,
            "report_date": report_date,
            "fy": fy,
            "form": form.upper(),
        }
    return None


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def hot_seat_dir() -> Path:
    raw = os.getenv("HOT_SEAT_DIR", "").strip()
    d = Path(raw).expanduser() if raw else (Path.home() / "Documents" / "hot_seat")
    return d


def _folder_name(ticker: str, form: str, fy: str) -> str:
    form_slug = form.upper().replace("-", "")  # 10-K -> 10K
    fy_slug = f"FY{fy}" if fy else "FYunknown"
    return f"{ticker.upper()}_{form_slug}_{fy_slug}"


def download_filing(ticker: str, cik: int, filing: dict, *,
                    base_dir: Path | None = None) -> dict:
    """Download the filing's primary document. Idempotent per folder."""
    base = base_dir or hot_seat_dir()
    folder = base / _folder_name(ticker, filing["form"], filing.get("fy", ""))
    folder.mkdir(parents=True, exist_ok=True)

    doc = filing.get("primary_doc") or "filing.htm"
    doc_name = Path(doc).name or "filing.htm"
    dest = folder / doc_name

    if dest.exists() and dest.stat().st_size > 0:
        return {"folder": str(folder), "file": str(dest),
                "downloaded": False, "reason": "already_present",
                "bytes": dest.stat().st_size}

    url = ARCHIVE_DOC_URL.format(
        cik=int(cik), accn=filing["accession_nodash"], doc=doc,
    )
    try:
        payload = _http_get(url, timeout=60.0)
    except Exception as exc:
        raise RuntimeError(f"download failed: {url}: {exc}")
    if not payload:
        raise RuntimeError(f"empty download: {url}")
    dest.write_bytes(payload)
    return {"folder": str(folder), "file": str(dest),
            "downloaded": True, "url": url, "bytes": len(payload)}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def fetch_10k(query: str, *, form: str = "10-K", base_dir: Path | None = None,
              resolver_mode: str = "auto", dry_run: bool = False,
              tickers: list[dict] | None = None) -> dict:
    """High-level: resolve -> find latest filing -> download. Returns a result dict.

    Result always includes 'ok' (bool) and 'stage' on failure.
    """
    resolved = resolve_ticker(query, tickers=tickers, mode=resolver_mode)
    if not resolved:
        return {"ok": False, "stage": "resolve", "query": query,
                "error": f"could not resolve ticker for {query!r}"}

    ticker = resolved["ticker"]
    cik = resolved["cik_str"]

    filing = resolve_latest_filing(cik, form=form)
    if not filing:
        return {"ok": False, "stage": "filing", "query": query,
                "ticker": ticker, "cik": cik, "title": resolved.get("title"),
                "error": f"no {form} found for {ticker} (CIK {cik})"}

    result = {
        "ok": True,
        "query": query,
        "ticker": ticker,
        "cik": cik,
        "title": resolved.get("title"),
        "resolver": resolved.get("source"),
        "form": filing["form"],
        "fy": filing.get("fy", ""),
        "accession": filing.get("accession", ""),
        "filing_date": filing.get("filing_date", ""),
        "report_date": filing.get("report_date", ""),
        "folder_name": _folder_name(ticker, filing["form"], filing.get("fy", "")),
    }

    if dry_run:
        result["dry_run"] = True
        result["target_folder"] = str(
            (base_dir or hot_seat_dir()) / result["folder_name"]
        )
        return result

    try:
        dl = download_filing(ticker, cik, filing, base_dir=base_dir)
    except Exception as exc:
        result["ok"] = False
        result["stage"] = "download"
        result["error"] = str(exc)
        return result

    result.update({
        "folder": dl["folder"],
        "file": dl["file"],
        "downloaded": dl["downloaded"],
        "bytes": dl.get("bytes", 0),
    })
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _bootstrap_env() -> None:
    """Load journalLinker env (for OPENAI_API_KEY etc.) when run standalone."""
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from journal_linker_env import bootstrap_journal_linker_env
        bootstrap_journal_linker_env(repo_root=repo_root)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    _bootstrap_env()
    parser = argparse.ArgumentParser(description="Fetch a company's latest 10-K into hot_seat.")
    parser.add_argument("query", help="Ticker or company name (e.g. 'Ford', 'AAPL').")
    parser.add_argument("--form", default="10-K", help="Filing form (default: 10-K).")
    parser.add_argument("--hot-seat-dir", default=None, help="Override download target.")
    parser.add_argument("--resolver", default=os.getenv("HOT_SEAT_TICKER_RESOLVER", "auto"),
                        choices=["auto", "openai", "local"], help="Ticker resolver mode.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve only, do not download.")
    parser.add_argument("--json", action="store_true", help="Emit JSON result to stdout.")
    args = parser.parse_args(argv)

    base_dir = Path(args.hot_seat_dir).expanduser() if args.hot_seat_dir else None
    result = fetch_10k(
        args.query, form=args.form, base_dir=base_dir,
        resolver_mode=args.resolver, dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result.get("ok"):
            if result.get("dry_run"):
                print(f"[hot_seat] would fetch {result['ticker']} {result['form']} "
                      f"{result['fy']} -> {result['target_folder']}")
            else:
                verb = "downloaded" if result.get("downloaded") else "already present"
                print(f"[hot_seat] {result['ticker']} {result['form']} {result['fy']} "
                      f"({verb}) -> {result['file']}")
        else:
            print(f"[hot_seat] FAILED at {result.get('stage')}: {result.get('error')}",
                  file=sys.stderr)

    if result.get("ok"):
        return EXIT_OK
    stage = result.get("stage")
    return {"resolve": EXIT_NO_TICKER, "filing": EXIT_NO_FILING,
            "download": EXIT_DOWNLOAD}.get(stage, EXIT_DOWNLOAD)


if __name__ == "__main__":
    sys.exit(main())
