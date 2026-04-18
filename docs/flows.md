# JournalLinker — Flow Coverage

Each section maps a code path to the user scenario that triggers it, the exact
command or file-state precondition, and the expected outcome. Fixture entries
are in `tests/fixtures/flows/`.

---

## Scribe.py

### Input Resolution

**argv text**
TJ pastes a paragraph directly on the CLI. Scribe uses that text and skips all
file lookups.
```
python3 Scribe.py "Met Jordan for coffee. Talked about the cycling trip."
```

---

**stdin pipe**
TJ pipes clipboard content into Scribe (the normal `pbpaste | Scribe.py` flow).
Scribe reads stdin and uses it as the input body.
```
echo "Worked on the training plan with Marcus." | python3 Scribe.py
```

---

**stdin empty → journal file wins**
TJ runs Scribe with `--active-date` but no piped text (e.g. from the voice
pipeline's `subprocess.run` call). Stdin is empty so Scribe reads the on-disk
note for that date instead.
```
python3 Scribe.py --active-date=2026-04-10
# (stdin closed / empty)
```

---

**`--active-file` override**
TJ wants to process a specific note regardless of today's date. Scribe uses
exactly that file as context and input.
```
python3 Scribe.py --active-file="/path/to/much thinks/2026-03-15.md"
```

---

**Date parsed from heading** · `tests/fixtures/flows/entry_date_in_header.md`
TJ pastes text that contains `# Daily Log - YYYY-MM-DD`. Scribe extracts the
date from the heading and resolves the matching journal note.
```
cat tests/fixtures/flows/entry_date_in_header.md | python3 Scribe.py
```
Expected: `active_context_source=input_date`, date=2026-01-15.

---

**Today's file exists (calendar today)**
TJ runs Scribe with no arguments on a day where `YYYY-MM-DD.md` already exists
in the journal dir. Scribe prefers today's file over the most recently modified.
```
python3 Scribe.py
# precondition: $(date +%Y-%m-%d).md exists in SCRIBE_JOURNAL_DIR
```
Expected: `active_context_source=calendar_today_file`.

---

**Latest-modified fallback**
TJ runs Scribe with no arguments on a day where no today-note exists. Scribe
falls back to whichever dated note was modified most recently.
```
python3 Scribe.py
# precondition: no today note; some older YYYY-MM-DD.md exists
```
Expected: `active_context_source=latest_modified_file`.

---

**No input at all (error path)**
TJ runs Scribe on a day with no note and empty stdin. Scribe writes an error
report and exits 2.
```
python3 Scribe.py
# precondition: no today note, stdin is a TTY or empty pipe
```
Expected: exit 2, error report written to `journal-linker/Journal Linker Run - Latest.md`.

---

### Navigation Link Sync

**Replace existing nav line** · `tests/fixtures/flows/entry_nav_replace.md`
The note already has a `[[DATE|Yesterday]] | [[DATE|Tomorrow]]` line from an
older template. Scribe replaces it in-place with the canonical arrow format.

Feed the fixture as input with `--write-back`:
```
cat tests/fixtures/flows/entry_nav_replace.md | python3 Scribe.py
```
Expected: nav line updated to `[[...|← Yesterday]] | [[...|Tomorrow →]]`.

---

**Insert after heading** · `tests/fixtures/flows/entry_nav_insert.md`
The note has a `# Daily Log - DATE` heading but no nav line yet. Scribe inserts
the nav line immediately after the heading.
```
cat tests/fixtures/flows/entry_nav_insert.md | python3 Scribe.py
```

---

**Append to end (no heading, no existing nav)** · `tests/fixtures/flows/entry_nav_no_heading.md`
The note has neither a heading nor an existing nav line. Scribe appends the nav
line at the end of the file.
```
cat tests/fixtures/flows/entry_nav_no_heading.md | python3 Scribe.py
```

---

**Previous-file side-effect (gap days)**
TJ skipped a day. The run resolves `2026-04-15`; the previous substantive note
is `2026-04-13`. Scribe also updates `2026-04-13`'s nav to point forward to
`2026-04-15`, filling in the gap.

Precondition: two dated notes exist with a gap between them, and `--write-back`
is used.

---

### Learning Feedback

**Positive feedback**
Yesterday's run suggested `[[cycling]]`. Today's note body contains
`[[cycling]]`. On today's run, Scribe increments `success_count` for that term.

Precondition: `scribe_learning.json` exists with a `runs` entry for yesterday
that includes `cycling` as a suggestion.

---

**Negative feedback**
Yesterday's run suggested `[[sneakers]]`. Today's note doesn't mention it. On
today's run, Scribe increments `failure_count` and decreases the weight.

Same precondition as above; `sneakers` absent from today's wikilinks.

---

**Reset learning**
TJ wants to wipe the reinforcement store and start fresh.
```
python3 Scribe.py --reset-learning
```
Expected: `scribe_learning.json` deleted; next run starts with empty state.

---

### Backlink Insertion

**Skip frontmatter** · `tests/fixtures/flows/entry_frontmatter.md`
The YAML block contains terms like `cycling` and `project`. Scribe must not
insert wikilinks inside the frontmatter.
```
cat tests/fixtures/flows/entry_frontmatter.md | python3 Scribe.py
```
Expected: no `[[...]]` inside the `---` block.

---

**Skip already-linked terms** · `tests/fixtures/flows/entry_existing_wikilinks.md`
The note already contains `[[cycling]]` and `[[Marcus]]`. Scribe must not wrap
them a second time as `[[ [[cycling]] ]]`.
```
cat tests/fixtures/flows/entry_existing_wikilinks.md | python3 Scribe.py
```
Expected: existing wikilinks unchanged; unlinked occurrences of the same terms
also left alone (first occurrence already linked).

---

**Skip shell/transcript paragraphs** · `tests/fixtures/flows/entry_shell_block.md`
A paragraph starting with `$` and containing `~/Library` paths scores ≥ 2 on
the shell-heuristic check. Scribe skips inserting links inside it.
```
cat tests/fixtures/flows/entry_shell_block.md | python3 Scribe.py
```
Expected: no wikilinks inside the `$ cd ~/Library/...` paragraph.

---

**Name-like boost** · `tests/fixtures/flows/entry_proper_nouns.md`
The note is dense with proper nouns (Marcus, Jordan, Sarah, Anthropic, Marin).
These score +20 in `rank_link_candidates`, so they rank to the top even without
prior learning data.
```
cat tests/fixtures/flows/entry_proper_nouns.md | python3 Scribe.py
```
Expected: proper nouns appear near the top of the ranked candidates in the
report.

---

**Write-back**
Scribe processes the note and writes the linked version back to disk.
```
python3 Scribe.py --write-back --active-date=2026-04-10
```
Expected: `2026-04-10.md` on disk updated with wikilinks and synced nav line.

---

## process_voice.py

### Single-File Flows

**Unprocessed file**
TJ drops a new recording; the pipeline runs it for the first time.
```
python3 scripts/process_voice.py /path/to/VoiceDrop/2026-04-17-1716.m4a
```
Expected: transcribed → appended to `2026-04-17.md` → Scribe runs → `.processed` marker created.

---

**Already processed, skip**
The `.processed` marker exists. Without `--force`, the pipeline exits 0 silently.
```
python3 scripts/process_voice.py /path/to/2026-04-17-1716.m4a
# precondition: 2026-04-17-1716.m4a.processed exists
```
Expected: `[voice] already processed` log, exit 0.

---

**Already processed + `--force`**
TJ wants to re-run a file even though it was already processed (e.g. Scribe
changed). `--force` bypasses the marker check.
```
python3 scripts/process_voice.py --force /path/to/2026-04-17-1716.m4a
```

---

**Night-cutoff attribution**
TJ recorded a voice note at 2:47 AM after a late night. The file timestamp
puts it on Apr 18 but it belongs to Apr 17's journal entry.
```
python3 scripts/process_voice.py /path/to/VoiceDrop/2026-04-18-0247.m4a
```
Expected: voice block appended to `2026-04-17.md` (hour 2 < `NIGHT_CUTOFF` 4).

---

**Duplicate timestamp guard** · `tests/fixtures/flows/entry_voice_callout.md`
The journal note already has a `> [!voice] 14:30` block. Running process_voice
on a file named `2026-04-10-1430.m4a` must not append a second identical block.

Precondition: `entry_voice_callout.md` is the active journal note for that date.
Expected: append skipped, file unchanged.

---

### Scan-Dir Flows

**Default scan (skip processed + permanent failures)**
TJ's systemd path unit fires. The pipeline scans VoiceDrop and processes any
file without a `.processed` or `.failed` marker, and shows a summary of
permanent failures without retrying them.
```
python3 scripts/process_voice.py
```

---

**`--retry` scan**
TJ's machine was asleep when Ollama was needed. The `.failed` markers say
`kind: transient`. After reboot, `--retry` re-queues those files.
```
python3 scripts/process_voice.py --retry
# precondition: at least one *.m4a.failed file with "kind: transient"
```

---

**`--force` scan**
TJ updated the Whisper model and wants all files re-processed from scratch.
```
python3 scripts/process_voice.py --force
```

---

### Failure Flows

**Transient failure (Scribe non-zero)**
Ollama is unreachable when `run_scribe()` is called. The pipeline marks the
file as transient-failed so `--retry` can pick it up later.

Precondition: Ollama not running during process.
Expected: `2026-04-17-1716.m4a.failed` created with `kind: transient`.

---

**Permanent failure (empty transcript)**
A silent or near-silent recording produces no transcript text. The pipeline
marks the file as permanently failed.

Precondition: audio file contains only silence.
Expected: `.failed` marker with `kind: permanent`.

---

**Vocabulary injection**
TJ has been using Scribe for weeks. `scribe_learning.json` has high-`success_count`
terms. Before loading the Whisper model, the pipeline extracts the top terms
into an `initial_prompt` string to bias transcription spelling.

Precondition: `scribe_learning.json` present with multiple successful terms.
Expected: `[voice] vocab prompt: N terms` in the log.

---

## weekly_insights.py

**Current week (default)**
TJ runs the insights script on a Friday afternoon. It reads the current ISO week
and writes `Insights/Weekly Insight - YYYY-Www.md`.
```
python3 weekly_insights.py
```

---

**Explicit week**
TJ wants insights for a past week.
```
python3 weekly_insights.py --week 2026-W10
```

---

**Idempotent rerun**
The insight file already exists. Running again does not overwrite it (the caller
checks the output path before writing).
```
python3 weekly_insights.py --week 2026-W10
# precondition: Insights/Weekly Insight - 2026-W10.md exists
```
Expected: no change to the file.

---

**Skip: sparse week** · `tests/fixtures/flows/entry_weekly_sparse.md`
Only one journal entry for the week, and it's below 35 words. The script
decides `substantive_entries < 2` and skips generating an insight.

Precondition: only `entry_weekly_sparse.md`-style entries in the target week.
Expected: output path is `None`, no file written.

---

**Write: substantive week** · `tests/fixtures/flows/entry_weekly_substantive.md`
Two or more rich entries exist for the week (≥ 35 words each, ≥ 80 total words,
confidence ≥ 0.45). The script generates and writes the insight.

Precondition: at least two `entry_weekly_substantive.md`-style entries in the
target week (e.g. dated 2026-04-07 and 2026-04-08).

---

## daily_reflection.py

**Within window, note present → push sent**
It's 6:30 PM and the window is 16:00–21:00. Yesterday's note has enough content.
`daily_reflection.py` calls Ollama, builds a reflection, and publishes to
Pushover.
```
python3 daily_reflection.py
# precondition: current time between WINDOW_START and WINDOW_END
```

---

**Before window → skip**
It's 9 AM. The window hasn't opened. The script logs the skip reason and exits.
```
python3 daily_reflection.py
# precondition: current time < SCRIBE_DAILY_REFLECTION_WINDOW_START
```

---

**After window → skip**
It's 11 PM. The window has closed. The script skips to avoid a late-night push.

---

**Already sent → skip**
The state file records `sent: true` for today's date. Running the script again
is a no-op.

Precondition: `daily_reflection_state.json` has `"sent": true` for today.

---

**Missing note → latest-modified fallback**
Yesterday's `YYYY-MM-DD.md` doesn't exist (TJ forgot to write). The script
falls back to the most recently modified journal note.

---

**`--dry-run`**
TJ wants to preview what would be sent without actually pushing.
```
python3 daily_reflection.py --dry-run
```
Expected: reflection printed to stdout, Pushover not called, state not updated.

---

**`--force-send`**
TJ wants to resend a reflection even though `sent: true` is already recorded.
```
python3 daily_reflection.py --force-send
```

---

## archivist.py

**Single-pass link suggestion (no learning)**
TJ pastes an archived note and wants quick backlink suggestions without
touching `scribe_learning.json` or writing any files.
```
echo "Ran into Marcus at the coffee shop near the Riverside trail." | python3 archivist.py
```
Expected: linked version printed to stdout; no state file changes.

---

## vault_mapper.py

**Co-occurrence clustering**
TJ wants to see which terms cluster together across all journal entries.
```
python3 vault_mapper.py
```
Expected: JSON/text listing of term clusters (e.g. `["Marcus", "cycling", "Jordan"]`)
where each group co-occurs in ≥ 2 entries (default `--min-cooccurrence`).
