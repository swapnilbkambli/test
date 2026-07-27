"""
ahk_lite - minimal AutoHotkey-style text expander + hotkey runner.

Setup (once):
    pip install --user keyboard

Run:
    python ahk_lite.py                 # uses config.txt next to this file
    python ahk_lite.py C:\\path\\to\\config.txt

To run without a console window once you've tested it, launch it with
pythonw.exe instead of python.exe, and add a shortcut to it in:
    shell:startup
so it starts automatically when you log in.

Edit config.txt (by hand, or with ahk_lite_gui.py) at any time and press
the "reload" hotkey configured there (default ctrl+alt+r) to pick up the
changes without restarting.

Everything is processed in memory only: typed characters are held in a
short rolling buffer used purely to detect abbreviations, never written
to disk or sent anywhere, and the buffer is cleared as soon as a word
boundary is hit.
"""

import os
import sys
import threading
import webbrowser

import keyboard

import config_store

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config.txt"
)

MIN_ABBREVIATION_LENGTH = 2
MAX_ABBREVIATION_LENGTH = 40

# Characters ahk_lite can replay after consuming a trigger key that isn't
# meant to be consumed (e.g. space/enter should still reach the app).
TRIGGER_CHARS = {"space": " ", "enter": "\n", "tab": "\t"}


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

    def on_key_event(self, event):
        if self.suppressed or event.event_type != "down":
            return

        name = event.name
        if not name:
            return

        if name == self.trigger_key:
            with self.lock:
                candidate, self.buffer = self.buffer, ""
            if (
                len(candidate) >= MIN_ABBREVIATION_LENGTH
                and candidate in self.expansions
            ):
                self._expand(candidate)
            return

        if name == "backspace":
            with self.lock:
                self.buffer = self.buffer[:-1]
            return

        if len(name) == 1 and name.isalnum():
            with self.lock:
                self.buffer = (self.buffer + name.lower())[-MAX_ABBREVIATION_LENGTH:]
        else:
            # Any other key (space, enter, punctuation, arrows, F-keys,
            # modifiers...) breaks the current word.
            with self.lock:
                self.buffer = ""

    def _expand(self, abbreviation):
        replacement = self.expansions[abbreviation]
        self.suppressed = True
        try:
            # +1 erases whatever the trigger keypress itself produced
            # (e.g. a tab character). In apps where the trigger key moves
            # focus instead of inserting a character (some dialogs/forms),
            # this can misfire -- see notes in config.txt.
            for _ in range(len(abbreviation) + 1):
                keyboard.send("backspace")
            text = replacement if self.consume_trigger else replacement + (self.trigger_char or "")
            keyboard.write(text)
        finally:
            self.suppressed = False


def make_hotkey_action(action):
    action = action.strip()
    lower = action.lower()

    if lower == "quit":
        def handler():
            print("[ahk_lite] Quit hotkey pressed. Exiting.")
            os._exit(0)
        return handler

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

        trigger_key, expansions, hotkeys = config_store.load_config(config_path)
        self.expander = TextExpander(expansions, trigger_key)
        keyboard.on_press(self.expander.on_key_event)
        self._register_hotkeys(hotkeys)

        print(f"[ahk_lite] Loaded {len(expansions)} expansion(s), "
              f"{len(hotkeys)} hotkey(s) from {config_path}")
        print(f"[ahk_lite] Expansion trigger key: {trigger_key}")

    def _register_hotkeys(self, hotkeys):
        for combo, action in hotkeys.items():
            if action.strip().lower() == "reload":
                handler = self.reload
            else:
                handler = make_hotkey_action(action)
            self.hotkey_handles.append(keyboard.add_hotkey(combo, handler))

    def reload(self):
        try:
            trigger_key, expansions, hotkeys = config_store.load_config(self.config_path)
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


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    App(config_path)

    print("[ahk_lite] Running. Press the 'quit' hotkey from config.txt "
          "(default ctrl+alt+q) or Ctrl+C here to stop.")

    try:
        keyboard.wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
