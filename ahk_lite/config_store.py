"""
config_store.py - shared config.txt reader + comment-preserving writer for
ahk_lite. Used by both ahk_lite.py (the daemon) and ahk_lite_gui.py (the
editor), so both always agree on file format and neither has its own copy
of the parsing logic.

The writer never does a full-file rewrite from parsed data (that's what
configparser.write() does, and it silently drops every comment). Instead
it edits the file as plain text: find the one line for a given key and
replace it in place, or insert a new line, touching nothing else.
"""

import configparser
import os


def unescape(text):
    return text.replace("\\n", "\n").replace("\\t", "\t")


def escape(text):
    return text.replace("\n", "\\n").replace("\t", "\\t")


def load_config(path):
    parser = configparser.ConfigParser(
        delimiters=("=",), inline_comment_prefixes=("#", ";")
    )
    parser.optionxform = str  # preserve case of keys as written in the file
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    parser.read(path, encoding="utf-8")

    trigger_key = "tab"
    if parser.has_section("settings"):
        trigger_key = parser.get("settings", "trigger_key", fallback="tab").strip().lower()

    expansions = {}
    if parser.has_section("expansions"):
        for key, value in parser.items("expansions"):
            expansions[key.strip().lower()] = unescape(value)

    hotkeys = {}
    if parser.has_section("hotkeys"):
        for key, value in parser.items("hotkeys"):
            hotkeys[key.strip()] = unescape(value.strip())

    return trigger_key, expansions, hotkeys


def _read_lines(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.readlines()


def _write_lines(path, lines):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.replace(tmp_path, path)  # atomic on both Windows and POSIX


def _is_section_header(line):
    stripped = line.strip()
    return stripped.startswith("[") and stripped.endswith("]")


def _section_bounds(lines, section):
    """Returns (start, end): start is the index of the `[section]` header
    line (or None if absent), end is the exclusive index where the next
    section header begins (or len(lines))."""
    header = f"[{section}]"
    start = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            start = i
            break
    if start is None:
        return None, None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if _is_section_header(lines[i]):
            end = i
            break
    return start, end


def _find_key_line(lines, start, end, key):
    for i in range(start + 1, end):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        if "=" not in stripped:
            continue
        existing_key = stripped.split("=", 1)[0].strip()
        if existing_key.lower() == key.lower():
            return i
    return None


def set_value(path, section, key, value):
    """Insert or update a single `key = value` line inside [section].
    Only that one line is touched -- every other line in the file
    (comments, blank lines, other entries) is left byte-for-byte as-is."""
    lines = _read_lines(path)
    start, end = _section_bounds(lines, section)
    new_line = f"{key} = {escape(value)}\n"

    if start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines and lines[-1].strip() != "":
            lines.append("\n")
        lines.append(f"[{section}]\n")
        lines.append(new_line)
        _write_lines(path, lines)
        return

    existing = _find_key_line(lines, start, end, key)
    if existing is not None:
        lines[existing] = new_line
    else:
        # Insert before any trailing blank lines in the section, so a
        # blank-line separator before the next section header survives.
        insert_at = end
        while insert_at > start + 1 and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        lines.insert(insert_at, new_line)
    _write_lines(path, lines)


def delete_value(path, section, key):
    lines = _read_lines(path)
    start, end = _section_bounds(lines, section)
    if start is None:
        return
    existing = _find_key_line(lines, start, end, key)
    if existing is not None:
        del lines[existing]
        _write_lines(path, lines)
