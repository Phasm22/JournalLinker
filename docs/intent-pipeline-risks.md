# Intent pipeline — design risks

Behavioral notes for `scripts/process_intents.py`. Configuration and tunables live only in that script’s module docstring and the environment, not here.

## Recurrence signal

Enrichment sets `recurrence_signal` from **hit-count-style** rules: enough overlapping local cortex hits on one branch, or enough combined local matches plus retrieved chunks on another. Lexical overlap is used to **rank** which cortex files surface as related; the recurrence flag is **not** a semantic detector for “this theme keeps coming back over months.” Treat it as a coarse “there is related material in reach” hint, not proof of thematic recurrence.

## Gate layer

The gate uses a **local** model over **short excerpts** (top paragraph chunks), not the full note every time. Code normalizes categories and intent classes to allowed sets, but the model still must emit parseable JSON. That stage is usually the **weakest** link for missed intents or spurious extractions compared with downstream routing, which uses API-enforced JSON and enum validation.

## Time and state

Journal timestamps prefer the **`YYYY-MM-DD`** stem of the note path; otherwise the file’s modification time is used. Routing urgency, scheduled feedback, and ledger maintenance all depend on **wall-clock time** and that inferred journal time. When reasoning about “today,” deadlines, or pruning windows, use the real current date.

## Enrichment

Enrichment merges **local** cortex search with **optional** remote retrieval. It is best-effort: failures are logged and the pipeline continues; routing still runs on the envelope as built.

## Validation vs residual risk

**Enforced in code:** routing outputs must match allowed values for urgency, format, and action; titles are truncated to length limits. Gate output is filtered to known categories and intent classes; malformed list entries are dropped.

**Residual risk:** gate JSON is still free-form model output (extracted and parsed, unlike routing’s structured API mode). Enrichment does not block the run on partial failure. Reviews should focus on those boundaries rather than assuming “the model always returns valid structured data” end-to-end.
