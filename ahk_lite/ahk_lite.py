"""
ahk_lite - minimal AutoHotkey-style text expander + hotkey runner, in a
single file (config.txt stays separate -- it's your data, not code).

Setup (once):
    pip install --user keyboard
    pip install --user pystray pillow   # optional, for the tray icon

Run the daemon (default mode):
    python ahk_lite.py                 # uses config.txt next to this file
    python ahk_lite.py C:\\path\\to\\config.txt

Run the config editor GUI instead:
    python ahk_lite.py --gui
    python ahk_lite.py --gui C:\\path\\to\\config.txt

Add / remove ahk_lite from Windows startup (no admin rights needed):
    python ahk_lite.py --install-startup
    python ahk_lite.py --remove-startup

To run the daemon without a console window once you've tested it,
launch it with pythonw.exe instead of python.exe, or use
--install-startup, which does that for you automatically.

Edit config.txt (by hand, or with --gui) at any time and use the
"reload" hotkey (default ctrl+alt+r) to pick up the changes without
restarting.

Everything is processed in memory only: typed characters are held in a
short rolling buffer used purely to detect abbreviations, never written
to disk or sent anywhere, and the buffer is cleared as soon as a word
boundary is hit.
"""

import argparse
import os
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime

try:
    import keyboard
except ImportError:
    keyboard = None

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

import tkinter as tk
from tkinter import messagebox, ttk

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config.txt"
)

MIN_ABBREVIATION_LENGTH = 2
MAX_ABBREVIATION_LENGTH = 40

# Whether typed-case (ABC / Abc / abc) should influence the output case.
# Depends on the `keyboard` library reporting real letter case in
# event.name, which is unverified on Windows -- if it doesn't seem to do
# anything on your machine, that's why. Harmless either way: it just
# stays inert instead of misbehaving.
PRESERVE_CASE = True

# Characters ahk_lite can replay after consuming a trigger key that isn't
# meant to be consumed (e.g. space/enter should still reach the app).
TRIGGER_CHARS = {"space": " ", "enter": "\n", "tab": "\t"}

CURSOR_MARKER = "{cursor}"


# =====================================================================
# config_store -- reads config.txt, and writes it back one line at a
# time so comments and formatting always survive.
#
# A "disabled" (commented-out) entry is written as `#key = value` with
# NO space after the '#'. That's deliberate: every hand-written comment
# in config.txt starts with '# ' (a space), including ones that
# incidentally look like `key = value` (e.g. the section header
# comments), so the no-space rule never collides with them. The GUI is
# the only thing that writes/toggles this marker.
# =====================================================================

def unescape(text):
    return text.replace("\\n", "\n").replace("\\t", "\t")


def escape(text):
    return text.replace("\n", "\\n").replace("\t", "\\t")


def load_config(path):
    """What the daemon uses: active entries only (configparser skips
    every '#'/';' line, disabled or not -- which is exactly correct,
    a disabled entry must never be live)."""
    import configparser

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


def _parse_entry_line(line):
    """Returns (key, enabled) if this line is an entry (active, or
    GUI-disabled via the no-space '#key = value' marker); None if it's
    blank or an ordinary comment."""
    stripped = line.strip()
    if not stripped:
        return None
    body = stripped
    enabled = True
    if body.startswith("#") and not body.startswith("# "):
        body = body[1:]
        enabled = False
    elif body.startswith("#") or body.startswith(";"):
        return None
    if "=" not in body:
        return None
    key = body.split("=", 1)[0].strip()
    if not key:
        return None
    return key, enabled


def _find_key_line(lines, start, end, key):
    for i in range(start + 1, end):
        parsed = _parse_entry_line(lines[i])
        if parsed and parsed[0].lower() == key.lower():
            return i
    return None


def load_entries(path, section):
    """What the GUI uses: every entry, active or disabled, so a
    commented-out shortcut still shows up (greyed out) instead of
    silently vanishing."""
    lines = _read_lines(path)
    start, end = _section_bounds(lines, section)
    entries = {}
    if start is None:
        return entries
    for i in range(start + 1, end):
        parsed = _parse_entry_line(lines[i])
        if not parsed:
            continue
        key, enabled = parsed
        stripped = lines[i].strip()
        body = stripped[1:] if not enabled else stripped
        value = body.split("=", 1)[1].strip()
        entries[key] = {"value": unescape(value), "enabled": enabled}
    return entries


def set_value(path, section, key, value):
    """Insert or update a single `key = value` line inside [section].
    Only that one line is touched -- every other line in the file
    (comments, blank lines, other entries) is left byte-for-byte as-is.
    If the entry already existed and was disabled, it stays disabled."""
    lines = _read_lines(path)
    start, end = _section_bounds(lines, section)
    new_body = f"{key} = {escape(value)}"

    if start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines and lines[-1].strip() != "":
            lines.append("\n")
        lines.append(f"[{section}]\n")
        lines.append(new_body + "\n")
        _write_lines(path, lines)
        return

    existing = _find_key_line(lines, start, end, key)
    if existing is not None:
        _key, enabled = _parse_entry_line(lines[existing])
        lines[existing] = ("" if enabled else "#") + new_body + "\n"
    else:
        # Insert before any trailing blank lines in the section, so a
        # blank-line separator before the next section header survives.
        insert_at = end
        while insert_at > start + 1 and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        lines.insert(insert_at, new_body + "\n")
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


def set_enabled(path, section, key, enabled):
    """Comment out (enabled=False) or uncomment (enabled=True) the line
    for `key`, in place. Used by the GUI's Enable/Disable button."""
    lines = _read_lines(path)
    start, end = _section_bounds(lines, section)
    if start is None:
        return
    existing = _find_key_line(lines, start, end, key)
    if existing is None:
        return
    _key, currently_enabled = _parse_entry_line(lines[existing])
    if currently_enabled == enabled:
        return
    stripped = lines[existing].strip()
    body = stripped[1:] if stripped.startswith("#") else stripped
    lines[existing] = (body if enabled else "#" + body) + "\n"
    _write_lines(path, lines)


# =====================================================================
# expansion_logic -- pure string logic for building an expansion's
# output. No `keyboard` calls here, just string manipulation, so it's
# easy to reason about and unit test independent of whether a global
# keyboard hook can actually be installed on a given machine.
# =====================================================================

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


# =====================================================================
# daemon
# =====================================================================

class TextExpander:
    """Watches keystrokes for a known abbreviation, then backspaces over
    it and types the expansion. Does not use a suppressing hook: the
    trigger key is always allowed through to the OS first, then erased.
    This is deliberately conservative -- if this script misbehaves or
    dies, you never lose the ability to type, you just stop getting
    expansions."""

    def __init__(self, expansions, trigger_key):
        self.lock = threading.Lock()
        self.buffer = ""
        self.raw_buffer = ""  # same as buffer, but case-preserved (for PRESERVE_CASE)
        self.paused = False
        self.suppressed = False  # guards against reacting to our own synthetic typing
        self.set_config(expansions, trigger_key)

    def set_config(self, expansions, trigger_key):
        with self.lock:
            self.expansions = expansions
            self.trigger_key = trigger_key
            self.trigger_char = TRIGGER_CHARS.get(trigger_key)
            # Only "tab" is consumed by default: it rarely carries meaning
            # as typed content. Space/enter are replayed so sentence flow
            # and line breaks after the expansion look natural.
            self.consume_trigger = trigger_key == "tab"
            self.buffer = ""
            self.raw_buffer = ""

    def on_key_event(self, event):
        if self.suppressed or self.paused or event.event_type != "down":
            return

        name = event.name
        if not name:
            return

        if name == self.trigger_key:
            with self.lock:
                candidate, self.buffer = self.buffer, ""
                raw_typed, self.raw_buffer = self.raw_buffer, ""
            if (
                len(candidate) >= MIN_ABBREVIATION_LENGTH
                and candidate in self.expansions
            ):
                self._expand(candidate, raw_typed)
            return

        if name == "backspace":
            with self.lock:
                self.buffer = self.buffer[:-1]
                self.raw_buffer = self.raw_buffer[:-1]
            return

        if len(name) == 1 and name.isalnum():
            with self.lock:
                self.buffer = (self.buffer + name.lower())[-MAX_ABBREVIATION_LENGTH:]
                self.raw_buffer = (self.raw_buffer + name)[-MAX_ABBREVIATION_LENGTH:]
        else:
            # Any other key (space, enter, punctuation, arrows, F-keys,
            # modifiers...) breaks the current word.
            with self.lock:
                self.buffer = ""
                self.raw_buffer = ""

    def _expand(self, abbreviation, raw_typed):
        template = self.expansions[abbreviation]
        text, left_presses = build_expansion(
            template, raw_typed, self.consume_trigger, self.trigger_char, PRESERVE_CASE
        )
        self.suppressed = True
        try:
            # +1 erases whatever the trigger keypress itself produced
            # (e.g. a tab character). In apps where the trigger key moves
            # focus instead of inserting a character (some dialogs/forms),
            # this can misfire -- see notes in config.txt.
            for _ in range(len(abbreviation) + 1):
                keyboard.send("backspace")
            keyboard.write(text)
            for _ in range(left_presses):
                keyboard.send("left")
        finally:
            self.suppressed = False


def make_hotkey_action(action):
    action = action.strip()
    lower = action.lower()

    if lower.startswith("run:"):
        target = action[len("run:"):].strip()
        def handler(target=target):
            try:
                os.startfile(target)  # Windows-only, handles exe/doc/folder
            except OSError as exc:
                print(f"[ahk_lite] Failed to run {target!r}: {exc}")
        return handler

    if lower.startswith("open:"):
        url = action[len("open:"):].strip()
        def handler(url=url):
            webbrowser.open(url)
        return handler

    if lower.startswith("type:"):
        text = action[len("type:"):]
        def handler(text=text):
            keyboard.write(text)
        return handler

    def handler(action=action):
        print(f"[ahk_lite] Unknown hotkey action, ignoring: {action!r}")
    return handler


class App:
    def __init__(self, config_path):
        self.config_path = config_path
        self.hotkey_handles = []

        trigger_key, expansions, hotkeys = load_config(config_path)
        self.expander = TextExpander(expansions, trigger_key)
        keyboard.on_press(self.expander.on_key_event)
        self._register_hotkeys(hotkeys)

        print(f"[ahk_lite] Loaded {len(expansions)} expansion(s), "
              f"{len(hotkeys)} hotkey(s) from {config_path}")
        print(f"[ahk_lite] Expansion trigger key: {trigger_key}")

    def _register_hotkeys(self, hotkeys):
        for combo, action in hotkeys.items():
            lowered = action.strip().lower()
            if lowered == "reload":
                handler = self.reload
            elif lowered == "pause":
                handler = self.toggle_pause
            elif lowered == "quit":
                handler = self.quit
            else:
                handler = make_hotkey_action(action)
            self.hotkey_handles.append(keyboard.add_hotkey(combo, handler))

    def reload(self):
        try:
            trigger_key, expansions, hotkeys = load_config(self.config_path)
        except Exception as exc:
            print(f"[ahk_lite] Reload failed, keeping previous config: {exc}")
            return

        self.expander.set_config(expansions, trigger_key)

        for handle in self.hotkey_handles:
            try:
                keyboard.remove_hotkey(handle)
            except (KeyError, ValueError):
                pass
        self.hotkey_handles = []
        self._register_hotkeys(hotkeys)

        print(f"[ahk_lite] Reloaded {len(expansions)} expansion(s), "
              f"{len(hotkeys)} hotkey(s) (trigger key: {trigger_key})")

    def toggle_pause(self):
        self.expander.paused = not self.expander.paused
        self.expander.buffer = ""
        self.expander.raw_buffer = ""
        print(f"[ahk_lite] Text expansion {'paused' if self.expander.paused else 'resumed'}")

    def quit(self):
        print("[ahk_lite] Quit hotkey pressed. Exiting.")
        os._exit(0)


# =====================================================================
# tray icon (optional -- needs pystray + Pillow)
# =====================================================================

class TrayUnavailable(Exception):
    pass


def _make_icon_image(paused):
    color = (150, 150, 150) if paused else (0, 170, 110)
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 56, 56), fill=color)
    return image


def run_tray(app):
    """Blocks until Quit is chosen, same role as keyboard.wait()."""
    if not TRAY_AVAILABLE:
        raise TrayUnavailable("pystray/Pillow not installed")

    def on_reload(icon, item):
        app.reload()
        icon.icon = _make_icon_image(app.expander.paused)

    def on_toggle_pause(icon, item):
        app.toggle_pause()
        icon.icon = _make_icon_image(app.expander.paused)

    def on_open_editor(icon, item):
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--gui", app.config_path])

    def on_install_startup(icon, item):
        try:
            path = install_startup()
            print(f"[ahk_lite] Startup shortcut installed: {path}")
        except Exception as exc:
            print(f"[ahk_lite] Could not install startup shortcut: {exc}")

    def on_quit(icon, item):
        icon.stop()
        app.quit()

    def is_paused(item):
        return app.expander.paused

    menu = pystray.Menu(
        pystray.MenuItem("Reload config", on_reload),
        pystray.MenuItem("Pause expansion", on_toggle_pause, checked=is_paused),
        pystray.MenuItem("Open editor", on_open_editor),
        pystray.MenuItem("Add to Windows startup", on_install_startup),
        pystray.MenuItem("Quit", on_quit),
    )
    icon = pystray.Icon("ahk_lite", _make_icon_image(paused=False), "ahk_lite", menu)
    icon.run()


# =====================================================================
# Windows startup installer (no admin rights needed)
# =====================================================================

STARTUP_LAUNCHER_NAME = "ahk_lite.vbs"


def _startup_folder():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA is not set -- this only works on Windows.")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _pythonw_path():
    exe = sys.executable
    candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return candidate if os.path.exists(candidate) else exe


def install_startup():
    folder = _startup_folder()
    os.makedirs(folder, exist_ok=True)
    vbs_path = os.path.join(folder, STARTUP_LAUNCHER_NAME)
    pyw = _pythonw_path()
    this_file = os.path.abspath(__file__)
    script = (
        'Set WshShell = CreateObject("WScript.Shell")\r\n'
        f'WshShell.Run """{pyw}"" ""{this_file}""", 0, False\r\n'
    )
    with open(vbs_path, "w", encoding="utf-8") as f:
        f.write(script)
    return vbs_path


def remove_startup():
    vbs_path = os.path.join(_startup_folder(), STARTUP_LAUNCHER_NAME)
    if os.path.exists(vbs_path):
        os.remove(vbs_path)
        return vbs_path
    return None


# =====================================================================
# GUI config editor
# =====================================================================

def _notify_daemon(config_path):
    """Best-effort: if a hotkey is mapped to 'reload', replay it so a
    running ahk_lite.py daemon picks up the change immediately. Silently
    does nothing if that's not set up, or if `keyboard` isn't available
    (the GUI itself never requires it)."""
    if keyboard is None:
        return
    try:
        _trigger_key, _expansions, hotkeys = load_config(config_path)
    except Exception:
        return
    reload_combo = next(
        (combo for combo, action in hotkeys.items() if action.strip().lower() == "reload"),
        None,
    )
    if reload_combo:
        try:
            keyboard.send(reload_combo)
        except Exception:
            pass


class EntryTab(ttk.Frame):
    def __init__(self, parent, config_path, section, key_label, value_label, value_hint=None):
        super().__init__(parent)
        self.config_path = config_path
        self.section = section

        self.tree = ttk.Treeview(
            self, columns=("key", "value", "status"), show="headings", height=10
        )
        self.tree.heading("key", text=key_label)
        self.tree.heading("value", text=value_label)
        self.tree.heading("status", text="Status")
        self.tree.column("key", width=150, anchor="w")
        self.tree.column("value", width=300, anchor="w")
        self.tree.column("status", width=70, anchor="center")
        self.tree.tag_configure("disabled", foreground="#999999")
        self.tree.pack(fill="both", expand=True, padx=6, pady=6)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        form = ttk.Frame(self)
        form.pack(fill="x", padx=6, pady=(0, 4))
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text=key_label + ":").grid(row=0, column=0, sticky="w")
        self.key_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.key_var).grid(row=0, column=1, sticky="ew", padx=4, pady=2)

        ttk.Label(form, text=value_label + ":").grid(row=1, column=0, sticky="w")
        self.value_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.value_var).grid(row=1, column=1, sticky="ew", padx=4, pady=2)

        if value_hint:
            ttk.Label(form, text=value_hint, foreground="#777").grid(
                row=2, column=0, columnspan=2, sticky="w", pady=(2, 0)
            )

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=6, pady=(0, 8))
        ttk.Button(buttons, text="Save / Update", command=self._save).pack(side="left")
        ttk.Button(buttons, text="Enable / Disable", command=self._toggle_enabled).pack(side="left", padx=6)
        ttk.Button(buttons, text="Delete selected", command=self._delete).pack(side="left")
        ttk.Button(buttons, text="Clear form", command=self._clear).pack(side="left", padx=6)

        self.refresh()

    def refresh(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        entries = load_entries(self.config_path, self.section)
        for key in sorted(entries):
            info = entries[key]
            status = "enabled" if info["enabled"] else "disabled"
            tags = () if info["enabled"] else ("disabled",)
            self.tree.insert("", "end", values=(key, info["value"], status), tags=tags)

    def _on_select(self, _event):
        selection = self.tree.selection()
        if not selection:
            return
        key, value, _status = self.tree.item(selection[0], "values")
        self.key_var.set(key)
        self.value_var.set(value)

    def _save(self):
        key = self.key_var.get().strip()
        value = self.value_var.get()
        if not key or not value:
            messagebox.showwarning("ahk_lite", "Both fields are required.")
            return
        set_value(self.config_path, self.section, key, value)
        self.refresh()
        _notify_daemon(self.config_path)

    def _toggle_enabled(self):
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("ahk_lite", "Select a row first.")
            return
        key, _value, status = self.tree.item(selection[0], "values")
        set_enabled(self.config_path, self.section, key, enabled=(status != "enabled"))
        self.refresh()
        _notify_daemon(self.config_path)

    def _delete(self):
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("ahk_lite", "Select a row to delete first.")
            return
        key = self.tree.item(selection[0], "values")[0]
        if not messagebox.askyesno("ahk_lite", f"Delete {key!r}?"):
            return
        delete_value(self.config_path, self.section, key)
        self._clear()
        self.refresh()
        _notify_daemon(self.config_path)

    def _clear(self):
        self.key_var.set("")
        self.value_var.set("")
        for row in self.tree.selection():
            self.tree.selection_remove(row)


class ConfigEditor(tk.Tk):
    def __init__(self, config_path):
        super().__init__()
        self.title("ahk_lite config editor")
        self.geometry("600x460")

        if not os.path.exists(config_path):
            messagebox.showerror("ahk_lite", f"Config file not found:\n{config_path}")
            self.destroy()
            return

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        expansions_tab = EntryTab(
            notebook, config_path, section="expansions",
            key_label="Abbreviation", value_label="Expansion text",
            value_hint="\\n newline, \\t tab, {cursor} place cursor, {date} {time} {datetime}",
        )
        hotkeys_tab = EntryTab(
            notebook, config_path, section="hotkeys",
            key_label="Hotkey (e.g. ctrl+alt+n)", value_label="Action",
            value_hint="run:<path>  |  open:<url>  |  type:<text>  |  reload  |  pause  |  quit",
        )
        notebook.add(expansions_tab, text="Text expansions")
        notebook.add(hotkeys_tab, text="Hotkeys")


# =====================================================================
# CLI entry point
# =====================================================================

def run_daemon(config_path):
    if keyboard is None:
        print("[ahk_lite] The 'keyboard' package is required to run the daemon.")
        print("[ahk_lite] Install it with: pip install --user keyboard")
        sys.exit(1)

    app = App(config_path)
    print("[ahk_lite] Running.")
    try:
        run_tray(app)
    except TrayUnavailable:
        print("[ahk_lite] No tray icon (pip install pystray pillow to enable one). "
              "Running headless -- use the hotkeys from config.txt, or Ctrl+C here to stop.")
        try:
            keyboard.wait()
        except KeyboardInterrupt:
            pass


def run_gui(config_path):
    ConfigEditor(config_path).mainloop()


def main():
    parser = argparse.ArgumentParser(description="ahk_lite - text expansion + hotkeys")
    parser.add_argument("config", nargs="?", default=DEFAULT_CONFIG_PATH, help="path to config.txt")
    parser.add_argument("--gui", action="store_true", help="open the config editor instead of running the daemon")
    parser.add_argument("--install-startup", action="store_true", help="add ahk_lite to Windows startup and exit")
    parser.add_argument("--remove-startup", action="store_true", help="remove ahk_lite from Windows startup and exit")
    args = parser.parse_args()

    if args.install_startup:
        path = install_startup()
        print(f"[ahk_lite] Installed startup launcher: {path}")
        print("[ahk_lite] ahk_lite will now start automatically at login.")
        print("[ahk_lite] To undo: python ahk_lite.py --remove-startup")
        return

    if args.remove_startup:
        removed = remove_startup()
        if removed:
            print(f"[ahk_lite] Removed startup launcher: {removed}")
        else:
            print("[ahk_lite] No startup launcher was installed.")
        return

    if args.gui:
        run_gui(args.config)
        return

    run_daemon(args.config)


if __name__ == "__main__":
    main()
