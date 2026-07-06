#!/usr/bin/env python3
"""text_normalize.py — shared text cleanup for voice/command parsing.

Journal notes wrap recognized terms in Obsidian wikilinks (`[[Term]]`,
sometimes `[[Target|Alias]]`) before journal_commands.py or hot_seat_fetch.py
ever see the text. Dictation also sometimes spells short tickers out as
individual letters ("O-C-M", "O C M") instead of a contiguous word. Both
entry points need the same cleanup, so it lives here instead of being
duplicated.
"""

import re

_WIKILINK_RE = re.compile(r"\[\[(.*?)\]\]")

# A run of 2+ single letters separated by spaces/hyphens/dots, e.g. "O-C-M",
# "O C M", "O.C.M." Word-boundary guarded and letters-only so it doesn't touch
# ordinary hyphenated words (those aren't single-letter tokens).
_SPELLED_LETTERS_RE = re.compile(
    r"\b([A-Za-z])(?:[\s\-.]+([A-Za-z]))+\b"
)


def strip_wikilinks(text: str) -> str:
    """Replace `[[Target]]` / `[[Target|Alias]]` with the display text.

    Prefers the alias (text after `|`) since that's closer to what was
    actually said; drops any `#section` suffix either way.
    """
    if not text:
        return text

    def _replace(m: re.Match) -> str:
        inner = m.group(1)
        target, _, alias = inner.partition("|")
        display = alias if alias else target
        display = display.split("#", 1)[0].strip()
        return display

    return _WIKILINK_RE.sub(_replace, text)


def collapse_spelled_letters(text: str) -> str:
    """Collapse spelled-out letter runs ("O-C-M") into one token ("OCM")."""
    if not text:
        return text

    def _replace(m: re.Match) -> str:
        letters = re.findall(r"[A-Za-z]", m.group(0))
        return "".join(letters).upper()

    return _SPELLED_LETTERS_RE.sub(_replace, text)


def has_spelled_letters(text: str) -> bool:
    """Whether `text` contains a spelled-out letter run ("O-C-M", "O C M").

    Checked after wikilink stripping (so `[[O C M]]` counts) but before
    letter-collapsing, so callers can tell whether normalization actually
    corrected something worth flagging.
    """
    if not text:
        return False
    return bool(_SPELLED_LETTERS_RE.search(strip_wikilinks(text)))


def normalize_command_text(text: str) -> str:
    """Strip wikilinks, then collapse spelled-out letters."""
    text = strip_wikilinks(text)
    text = collapse_spelled_letters(text)
    return text
