"""
expansion_logic.py - pure string logic for building an expansion's output.
No `keyboard` dependency here on purpose: everything in this file is
plain string manipulation, so it can be unit tested on any platform,
independent of whether a global keyboard hook can actually be installed.

ahk_lite.py's TextExpander calls build_expansion() and only handles the
actual keystrokes (backspace/write/left).
"""

from datetime import datetime

CURSOR_MARKER = "{cursor}"


def apply_dynamic_tokens(text, now=None):
    now = now or datetime.now()
    return (
        text.replace("{date}", now.strftime("%Y-%m-%d"))
            .replace("{time}", now.strftime("%H:%M"))
            .replace("{datetime}", now.strftime("%Y-%m-%d %H:%M"))
    )


def compute_case_mode(raw_typed):
    """Looks at what the user actually typed (before lowercasing) to
    decide whether the expansion should come out UPPER, Title, or as
    written in config.txt."""
    letters = [c for c in raw_typed if c.isalpha()]
    if not letters:
        return "none"
    if all(c.isupper() for c in letters):
        return "upper"
    if raw_typed[:1].isupper() and all(c.islower() for c in letters[1:]):
        return "title"
    return "none"


def build_expansion(template, raw_typed, consume_trigger, trigger_char, preserve_case=True):
    """Returns (text_to_type, left_presses).

    text_to_type is the literal string to send via keyboard.write().
    left_presses is how many times to press Left afterwards to land the
    cursor at the {cursor} marker's position (0 if the template has no
    marker)."""
    text = apply_dynamic_tokens(template)

    has_cursor = CURSOR_MARKER in text
    if has_cursor:
        before, after = text.split(CURSOR_MARKER, 1)
    else:
        before, after = text, ""

    if preserve_case:
        mode = compute_case_mode(raw_typed)
        if mode == "upper":
            before, after = before.upper(), after.upper()
        elif mode == "title":
            before = before[:1].upper() + before[1:]

    full_text = before + after
    text_to_type = full_text if consume_trigger else full_text + (trigger_char or "")

    left_presses = 0
    if has_cursor:
        left_presses = len(after) + (0 if consume_trigger else len(trigger_char or ""))

    return text_to_type, left_presses
