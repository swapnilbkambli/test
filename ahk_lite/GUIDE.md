# ahk_lite

A minimal, script-based replacement for two AutoHotkey features — text
expansion and global hotkeys — for machines where you can't install AHK
itself (e.g. a locked-down corporate Windows laptop). Everything is one
Python file, driven by one editable config text file.

## Files

Just two files matter:

| File | What it's for |
|---|---|
| `ahk_lite.py` | Everything: the daemon, the GUI editor, the tray icon, the startup installer. One file, dispatched by command-line flags (see below). |
| `config.txt` | Your actual shortcuts and expansions. Edit this to whatever you want — the shipped version is just a sample. |

`requirements.txt` (core) and `requirements-tray.txt` (optional, for the tray icon) just list pip packages — nothing to copy to the file system, just to run `pip install -r` against.

## Setup (Windows)

```bash
pip install --user -r requirements.txt
```

Optional, for the tray icon:
```bash
pip install --user -r requirements-tray.txt
```

## Running it

This is the only command you need day to day:

```bash
python ahk_lite.py                        # run the daemon (uses config.txt next to this file)
python ahk_lite.py C:\path\to\config.txt   # ...or point it at a different config file
```

The first time it runs, it also registers itself to start automatically at the next Windows login (no admin rights needed, and nothing extra to run for that — it just happens). If you ever want to stop that:

```bash
python ahk_lite.py --remove-startup
```

The config editor is reachable two ways: from the tray icon's "Open editor" item while the daemon is running, or directly:

```bash
python ahk_lite.py --gui
python ahk_lite.py --gui C:\path\to\config.txt
```

The daemon and the GUI are separate processes — you can run both at once, pointed at the same `config.txt`.

If `pystray`/`Pillow` are installed, the daemon shows a tray icon and the console window can be closed (launch with `pythonw.exe` instead of `python.exe` for no console at all — the auto-installed startup shortcut already does this). If not, it runs headless in the console, driven entirely by hotkeys.

## config.txt format

Three sections. Comments (`#` or `;`) and blank lines are yours to keep — every write path (reload, GUI saves, enable/disable) only ever touches the one line being changed, never rewrites the file, so your formatting and notes always survive.

### `[settings]`

```ini
trigger_key = tab
```

The key that ends an abbreviation and triggers expansion. `tab`, `space`, `enter`, or a function key like `f8` are reasonable choices. `tab` is consumed (not inserted); `space`/`enter` are replayed after the expansion so your sentence keeps flowing.

**Known limitation**: if `trigger_key` is `tab`, this can misfire in dialogs/forms where Tab jumps focus to the next field rather than inserting a character — the cleanup logic (see "How expansion actually works" below) assumes the trigger key inserted something it can backspace over. If that bites you, switch to `space` or a dedicated key like `f8` that never carries meaning as content.

### `[expansions]`

```ini
abbreviation = expansion text
```

Type the abbreviation (case-insensitive matching) then the trigger key to expand it. `\n` and `\t` inside the value become a real newline/tab. Special tokens you can use in the expansion text:

- `{cursor}` — leaves your cursor here instead of at the very end. Example:
  ```ini
  mtg = Subject: {cursor}\n\nHi team,\n\nBest,\nSwapnil
  ```
  Typing `mtg` + Tab writes the whole block but drops your cursor right after `Subject: `, ready to type.
- `{date}` — today's date (`2026-07-27`)
- `{time}` — current time (`14:05`)
- `{datetime}` — both combined

**Case preservation**: type the abbreviation as `ABC` and the expansion comes out upper-cased; type `Abc` and just the first letter of the expansion is capitalized; type `abc` (or mixed case) and it comes out exactly as written in `config.txt`. This depends on the `keyboard` library reporting real letter case from your keystrokes, which isn't fully verified on Windows yet — if it doesn't seem to do anything, that's why, and it's harmless either way. To turn it off entirely, set `PRESERVE_CASE = False` near the top of `ahk_lite.py`.

### `[hotkeys]`

```ini
key-combo = action
```

Modifiers: `ctrl`, `alt`, `shift`, `windows`, combined with `+` (e.g. `ctrl+alt+n`). Actions (no space after the colon):

| Action | Effect |
|---|---|
| `run:<path or command>` | Launch a program, or open a file/folder (Windows `os.startfile`) |
| `open:<url>` | Open a URL in your default browser |
| `type:<text>` | Type literal text at the cursor (`\n`/`\t` work here too) |
| `reload` | Re-read `config.txt` and re-register everything, no restart |
| `pause` | Toggle text expansion on/off (hotkeys keep working while paused) |
| `quit` | Stop ahk_lite |

### Disabling an entry without deleting it

A line written as `#key = value` — **no space** after the `#` — is a *disabled* entry: it still shows up in the GUI (greyed out, marked "disabled") so you can bring it back later, but the daemon ignores it completely, exactly as if it didn't exist.

This is deliberately different from an ordinary comment, which always has a space after the `#` (`# like this`, including every explanatory comment already in this file). That's not a coincidence — it's how the GUI's Enable/Disable button avoids ever mistaking your notes for a real (if disabled) shortcut. Hand-editing the file, just remove/add that one `#` yourself to the same effect.

## The GUI editor

```bash
python ahk_lite.py --gui
```

Two tabs — Text expansions, Hotkeys — each a table with **Save/Update**, **Enable/Disable**, **Delete**, and a form to add new rows. A disabled row shows greyed out with "disabled" in the Status column; selecting it and clicking Enable/Disable brings it back. Editing the value of a disabled row (Save/Update) keeps it disabled — the enabled/disabled state and the value are independent.

It writes back to `config.txt` using the same line-preserving logic as reload, so nothing you've hand-written in the file gets clobbered.

If `config.txt` has a hotkey mapped to `reload` (the sample does, `ctrl+alt+r`), every save/delete/toggle from the GUI also replays that hotkey — if `ahk_lite.py` is already running, it picks up the change immediately, no manual reload needed.

## Tray icon

If `pystray`/`Pillow` are installed, running the daemon shows a small tray icon (green = active, grey = paused) instead of just a bare console. Right-click menu:

- **Reload config**
- **Pause expansion** (checkbox, reflects current state)
- **Open editor** — launches `python ahk_lite.py --gui` pointed at the same config file
- **Quit**

If those packages aren't installed, the daemon just prints a note and falls back to running headless in the console — nothing breaks.

## Auto-start at Windows login

Automatic — nothing to run. The first time `python ahk_lite.py` starts the daemon, it drops a small VBScript launcher into your Startup folder (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`) that runs the daemon hidden via `pythonw.exe` at every login. No admin rights needed — it's a per-user folder, and every run after the first is a silent no-op (it only writes the launcher if it isn't already there). To undo it:

```bash
python ahk_lite.py --remove-startup
```

## How expansion actually works (and its one real caveat)

ahk_lite deliberately does **not** try to intercept/block your trigger keystroke before it reaches the active app. Instead: the trigger key is always let through first, then ahk_lite sends backspaces to erase the abbreviation (+1 for whatever the trigger itself produced), then types the replacement.

This is a conservative choice: if the script crashes or misbehaves, you never lose the ability to type — you just stop getting expansions. The tradeoff is the Tab-in-forms caveat mentioned above under `[settings]`.

Also: typed characters are only ever held in a short in-memory buffer to check for a match, and are cleared the instant a word boundary is hit. Nothing is logged, written to disk, or sent anywhere.

## Troubleshooting

- **Nothing happens when I type an abbreviation** — check `trigger_key` in `config.txt` matches what you're actually pressing, that the entry isn't disabled (see above), and that the abbreviation is at least 2 characters.
- **Windows dialogs eat my Tab keystroke oddly** — see the Tab caveat above; switch `trigger_key` to `space` or `f8`.
- **`run:` hotkey does nothing on Mac** — `os.startfile` is Windows-only by design; this tool targets the Windows laptop, macOS is only useful here for testing the GUI/config-editing pieces (see below).
- **`ModuleNotFoundError: No module named 'keyboard'` after installing it** — you likely installed it into a different Python than the one running the script (common with `sudo` resetting PATH on Mac, or multiple Pythons on Windows). Check `pip show keyboard` and make sure you're running the *same* interpreter you installed into.
- **On macOS**: the `keyboard` library needs root or Input Monitoring permission to actually tap keyboard events (`OSError: Error 13 - Must be run as administrator` otherwise), and `run:` hotkeys don't work at all. `python ahk_lite.py --gui` needs neither — it's a plain Tkinter form and works normally without `sudo`.
