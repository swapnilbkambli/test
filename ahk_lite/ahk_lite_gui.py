"""
ahk_lite_gui - minimal editor for ahk_lite's config.txt.

Lets you add, update, and delete text expansions and hotkeys without
hand-editing the file. Every save touches only the one line being
changed, so existing comments and formatting in config.txt are left
alone (see config_store.py).

If ahk_lite.py is already running and config.txt has a hotkey mapped to
the "reload" action, saving here also triggers that hotkey so the
running daemon picks up the change immediately -- no restart needed.

Run:
    python ahk_lite_gui.py                 # uses config.txt next to this file
    python ahk_lite_gui.py C:\\path\\to\\config.txt
"""

import os
import sys
import tkinter as tk
from tkinter import messagebox, ttk

import config_store

try:
    import keyboard
except ImportError:
    keyboard = None

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config.txt"
)


class EntryTab(ttk.Frame):
    def __init__(self, parent, config_path, section, key_label, value_label, value_hint=None):
        super().__init__(parent)
        self.config_path = config_path
        self.section = section

        self.tree = ttk.Treeview(
            self, columns=("key", "value"), show="headings", height=10
        )
        self.tree.heading("key", text=key_label)
        self.tree.heading("value", text=value_label)
        self.tree.column("key", width=170, anchor="w")
        self.tree.column("value", width=340, anchor="w")
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
        ttk.Button(buttons, text="Delete selected", command=self._delete).pack(side="left", padx=6)
        ttk.Button(buttons, text="Clear form", command=self._clear).pack(side="left")

        self.refresh()

    def refresh(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        _trigger_key, expansions, hotkeys = config_store.load_config(self.config_path)
        data = expansions if self.section == "expansions" else hotkeys
        for key in sorted(data):
            self.tree.insert("", "end", values=(key, data[key]))

    def _on_select(self, _event):
        selection = self.tree.selection()
        if not selection:
            return
        key, value = self.tree.item(selection[0], "values")
        self.key_var.set(key)
        self.value_var.set(value)

    def _save(self):
        key = self.key_var.get().strip()
        value = self.value_var.get()
        if not key or not value:
            messagebox.showwarning("ahk_lite", "Both fields are required.")
            return
        config_store.set_value(self.config_path, self.section, key, value)
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
        config_store.delete_value(self.config_path, self.section, key)
        self._clear()
        self.refresh()
        _notify_daemon(self.config_path)

    def _clear(self):
        self.key_var.set("")
        self.value_var.set("")
        for row in self.tree.selection():
            self.tree.selection_remove(row)


def _notify_daemon(config_path):
    """Best-effort: if a hotkey is mapped to 'reload', replay it so a
    running ahk_lite.py picks up the change immediately. Silently does
    nothing if that's not set up, or if `keyboard` isn't available."""
    if keyboard is None:
        return
    try:
        _trigger_key, _expansions, hotkeys = config_store.load_config(config_path)
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


class ConfigEditor(tk.Tk):
    def __init__(self, config_path):
        super().__init__()
        self.title("ahk_lite config editor")
        self.geometry("580x440")

        if not os.path.exists(config_path):
            messagebox.showerror("ahk_lite", f"Config file not found:\n{config_path}")
            self.destroy()
            return

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        expansions_tab = EntryTab(
            notebook, config_path, section="expansions",
            key_label="Abbreviation", value_label="Expansion text",
            value_hint="Use \\n for a newline, \\t for a tab.",
        )
        hotkeys_tab = EntryTab(
            notebook, config_path, section="hotkeys",
            key_label="Hotkey (e.g. ctrl+alt+n)", value_label="Action",
            value_hint="run:<path>  |  open:<url>  |  type:<text>  |  reload  |  quit",
        )
        notebook.add(expansions_tab, text="Text expansions")
        notebook.add(hotkeys_tab, text="Hotkeys")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    ConfigEditor(path).mainloop()
