# iOS Voice Setup

Record a voice note on your iPhone → it automatically transcribes and lands in your Obsidian journal.

**What you need to do once:**

1. Verify one iCloud setting
2. Build a 4-action Shortcut in the Shortcuts app
3. Assign it somewhere fast (home screen, Action Button, or Lock Screen)

---

## Step 1 — Verify iCloud Drive is on

`Settings → [your name] → iCloud → iCloud Drive` → toggle **On**

That's it. The `VoiceDrop` folder syncs to your Mac automatically.

---

## Step 2 — Build the Shortcut (4 actions)

Open **Shortcuts** on your iPhone. Tap **+** to create a new shortcut.

Add these four actions in order:

> **Why 4 actions and not 3?** Shortcuts' "Format Date" action needs an actual Date object as its input — if you skip the Date step, Shortcuts tries to coerce the audio recording from Action 1 into a date and fails with "couldn't convert from Text to Date." The explicit "Date" action in step 2 captures the current date cleanly before formatting it.

### Action 1: Record Audio

- Search for and add **"Record Audio"**
- Tap the action to expand options:
  - **Start Recording:** `Immediately` (recording begins the moment you run the shortcut)
  - Leave all other settings as default

### Action 2: Date

- Search for and add **"Date"** (the simple one — no inputs, just outputs the current date and time)
- No configuration needed — it automatically returns the current date/time
- Tap the result label and rename it `RecordingDate`

### Action 3: Format Date

- Search for and add **"Format Date"**
- Tap the action to expand:
  - **Date:** tap and select the `RecordingDate` variable from Action 2 (the blue chip)
  - **Format:** tap and choose `Custom`
  - **Custom format field:** type exactly (this is a [UTS#35](https://unicode.org/reports/tr35/tr35-dates.html#Date_Format_Patterns) date pattern — iOS Shortcuts uses this standard for custom date formatting):
    ```
    yyyy-MM-dd-HHmm
    ```
  - Tap **Done**
- Tap the result label and rename it `Timestamp`

### Action 4: Save File

- Search for and add **"Save File"**
- Tap the action to expand:
  - **File:** tap the field → tap the variable picker (`{x}`) → select **Recording** from Action 1 (the audio, not the Timestamp text). This is the most common mistake — the field must be the audio recording, not any text variable.
  - **Destination:** choose `iCloud Drive`
  - **Folder:** type `VoiceDrop`
  - **File Name:** tap and select the `Timestamp` variable from Action 3
  - Toggle **"Ask Where to Save"** → **Off**

> **Common mistake:** If the "File" field shows a text-colored variable (like Timestamp), that's wrong — it will save a .txt file containing just the timestamp string. The File field must be the audio Recording chip from Action 1.

### Name and save

Tap the name at the top of the shortcut (defaults to "New Shortcut") and rename it to something short, e.g. **Journal Voice**.

Tap **Done**.

---

## Step 3 — Make it one tap

Pick one of these:

### Option A — Action Button (iPhone 15 Pro / 16 / 16 Pro / 16 Pro Max)

`Settings → Action Button → Shortcut → Journal Voice`

Press the Action Button → recording starts immediately → press again to stop → file saved.

### Option B — Home Screen widget

In Shortcuts, long-press the shortcut → **Add to Home Screen** → place it on your main home screen. One tap to start recording, one tap to stop.

### Option C — Lock Screen shortcut

Long-press your Lock Screen → **Customize → Lock Screen** → tap one of the shortcut slots → choose **Journal Voice**.

---

## What the file looks like on Mac

After iCloud syncs (usually within a few seconds on Wi-Fi), the Mac sees:

```
~/Library/Mobile Documents/com~apple~CloudDocs/VoiceDrop/
    2026-04-04-1430.m4a          ← your recording
    2026-04-04-1430.m4a.processed ← added by the Mac after transcription
```

The filename encodes the exact date and time of the recording. If you record at 01:45 AM, the pipeline attributes it to the previous calendar day (before 4 AM is treated as "still last night" — configurable with `SCRIBE_NIGHT_CUTOFF`).

---

## How the Mac side processes it

When the file appears, launchd wakes `voice_watcher.sh`, which calls `process_voice.py`:

1. Reads `scribe_learning.json` and ranks your known wikilink terms by success score × recency — these become Whisper's vocabulary hint.
2. Transcribes the `.m4a` with `faster-whisper`, biased toward your vault's terminology.
3. Appends a voice callout block to `YYYY-MM-DD.md`:
  ```
   > [!voice] 14:30
   > Transcribed text goes here. Your project names and people's names
   > are recognized correctly because of the vocabulary injection.
  ```
4. Calls `Scribe.py --write-back --active-date=YYYY-MM-DD` — the same wikilink suggestion, learning feedback, and nav-link sync pipeline that runs on typed entries.

---

## Troubleshooting

**File doesn't appear on Mac**

- Check `System Settings → Apple ID → iCloud → iCloud Drive` is on.
- Open the Files app on your iPhone → iCloud Drive → VoiceDrop — is the file there? If yes but not on Mac, give iCloud a minute or check Mac internet connection.

**Transcription never runs**

- Check `just voice-doctor` in the repo for status.
- Check logs: `tail "$(readlink "$HOME/Library/Logs/JournalLinker/voice-latest.log")"`
- Check the LaunchAgent is loaded: `launchctl list | grep journal-linker.voice`

**LaunchAgent not loaded**
Follow the install steps in the README under "Voice pipeline on your Mac".

`**faster-whisper` not installed**

```bash
just voice-install
```

**"Couldn't convert from Text to Date" on Action 3 (Format Date)**
This means Format Date is trying to use the audio recording from Action 1 as its date input instead of the current date. Fix: make sure Action 2 is the plain **"Date"** action (returns current date/time, no inputs), and that Action 3's Date field shows the `RecordingDate` variable (blue chip) — not the recording audio variable.

**Wrong date on the entry / garbled filename**
The format string uses [UTS#35](https://unicode.org/reports/tr35/tr35-dates.html#Date_Format_Patterns) tokens — case-sensitive. Re-enter the custom format in Action 3 exactly as `yyyy-MM-dd-HHmm`. The most common mistakes: using `mm` where `MM` is needed (month vs minutes), `hh` instead of `HH` (12h vs 24h clock), or `YYYY` instead of `yyyy` (week year vs calendar year). See the **Filename format reference** section at the bottom of this doc.

**Test manually without the launchd watcher**

```bash
# Dry-run a specific file (transcribe + print, no writes)
just voice-dry ~/path/to/2026-04-04-1430.m4a

# Process for real
just voice ~/path/to/2026-04-04-1430.m4a
```

---

## Filename format reference (UTS#35)

iOS Shortcuts uses **Unicode Technical Standard #35** (the same ICU/CLDR standard used by most programming languages) for custom date format strings. Reference: [UTS#35 Date Format Patterns](https://unicode.org/reports/tr35/tr35-dates.html#Date_Format_Patterns).

The format string for this pipeline: `**yyyy-MM-dd-HHmm`**


| Token  | UTS#35 meaning                               | Case            | Example output |
| ------ | -------------------------------------------- | --------------- | -------------- |
| `yyyy` | Calendar year, 4 digits                      | lowercase       | `2026`         |
| `-`    | Literal hyphen                               | —               | `-`            |
| `MM`   | Month of year, 2 digits (01–12)              | **uppercase M** | `04`           |
| `-`    | Literal hyphen                               | —               | `-`            |
| `dd`   | Day of month, 2 digits (01–31)               | lowercase       | `04`           |
| `-`    | Literal hyphen                               | —               | `-`            |
| `HH`   | Hour of day, 24-hour clock, 2 digits (00–23) | **uppercase H** | `14`           |
| `mm`   | Minutes, 2 digits (00–59)                    | **lowercase m** | `30`           |


**Full result: `2026-04-04-1430`** → saved as `2026-04-04-1430.m4a`

**Case is critical — wrong case produces wrong output:**


| Wrong          | Right  | Why                                                                  |
| -------------- | ------ | -------------------------------------------------------------------- |
| `YYYY`         | `yyyy` | `YYYY` is ISO week-based year — wrong in January/December edge cases |
| `mm` for month | `MM`   | `mm` = minutes; `MM` = month                                         |
| `hh`           | `HH`   | `hh` = 12-hour clock; `HH` = 24-hour clock                           |
| `DD`           | `dd`   | `DD` = day of year (1–365); `dd` = day of month                      |


