#!/usr/bin/env python3
"""
SKE Manager — GUI for RKE2/Kubernetes (SKE) Platform
Dependencies: Python 3.x only (tkinter is bundled with Python on Windows)
Usage:  python ske_manager.py
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog, filedialog
import subprocess
import threading
import queue
import shlex
import os
import sys
import json
import base64
import time
from datetime import datetime, timezone

# ─── Configurable paths ────────────────────────────────────────────────────────
KUBECTL_CMD = os.environ.get("KUBECTL_CMD", "kubectl")
SKECTL_CMD  = os.environ.get("SKECTL_CMD",
                              "skectl.exe" if sys.platform == "win32" else "skectl")

# How many seconds before expiry to start warning / auto-renew
WARN_SECS   = 5 * 60   # warn at  5 min remaining
RENEW_SECS  = 2 * 60   # renew at 2 min remaining

RESOURCE_TYPES = [
    "pods", "deployments", "services", "replicasets", "statefulsets",
    "daemonsets", "configmaps", "secrets", "ingresses",
    "persistentvolumeclaims", "persistentvolumes", "nodes",
    "events", "jobs", "cronjobs", "serviceaccounts",
    "roles", "rolebindings", "clusterroles", "namespaces",
]

# ─── Colour palette (dark) ─────────────────────────────────────────────────────
BG       = "#1e1e2e"
SURFACE  = "#313244"
ENTRY_BG = "#45475a"
FG       = "#cdd6f4"
ACCENT   = "#89b4fa"
SUCCESS  = "#a6e3a1"
ERROR    = "#f38ba8"
WARN     = "#fab387"
DIM      = "#6c7086"
LOG_BG   = "#11111b"

# ─── Subprocess helpers ────────────────────────────────────────────────────────

def _win_flags():
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def run_cmd(args, timeout=30):
    """Blocking run; returns (stdout, stderr, returncode)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout, **_win_flags())
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", "Timed out after %ds" % timeout, 1
    except FileNotFoundError:
        return "", "Not found: %s  —  is it on PATH?" % args[0], 1
    except Exception as exc:
        return "", str(exc), 1


def stream_cmd(args, line_cb, stop_evt):
    """Stream stdout line-by-line, calling line_cb(line).  Thread target."""
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, **_win_flags()
        )
        for raw in iter(proc.stdout.readline, ""):
            if stop_evt.is_set():
                proc.terminate()
                break
            line_cb(raw.rstrip("\n"))
        proc.wait()
        if not stop_evt.is_set():
            line_cb("[stream ended]")
    except FileNotFoundError:
        line_cb("[ERROR] %s not found — is it on PATH?" % args[0])
    except Exception as exc:
        line_cb("[ERROR] %s" % exc)


# ─── JWT / kubeconfig helpers ──────────────────────────────────────────────────

def _jwt_expiry(token: str):
    """
    Decode the JWT payload (no signature verification needed — we just want exp).
    Returns a UTC-aware datetime, or None if unreadable.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        # Base64url → standard base64; add padding
        payload_b64 = parts[1].replace("-", "+").replace("_", "/")
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        data = json.loads(base64.b64decode(payload_b64))
        exp = data.get("exp")
        if exp:
            return datetime.fromtimestamp(exp, tz=timezone.utc)
    except Exception:
        pass
    return None


def _kubeconfig_expiry():
    """
    Read the current context's token from kubeconfig via kubectl,
    parse its JWT expiry.  Returns (datetime_utc | None, token_str | None).
    """
    stdout, _, rc = run_cmd(
        [KUBECTL_CMD, "config", "view", "--raw", "-o", "json"], timeout=10)
    if rc != 0:
        return None, None
    try:
        cfg     = json.loads(stdout)
        cur_ctx = cfg.get("current-context", "")
        ctx_map = {c["name"]: c.get("context", {})
                   for c in cfg.get("contexts", [])}
        usr_map = {u["name"]: u.get("user", {})
                   for u in cfg.get("users", [])}
        ctx     = ctx_map.get(cur_ctx, {})
        user    = usr_map.get(ctx.get("user", ""), {})
        token   = user.get("token", "")
        if token:
            return _jwt_expiry(token), token
    except Exception:
        pass
    return None, None


def _fmt_countdown(secs: float) -> str:
    """Format remaining seconds as  MM:SS  or  HH:MM:SS."""
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


# ─── Application ───────────────────────────────────────────────────────────────

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("SKE Manager")
        self.root.geometry("1340x860")
        self.root.minsize(960, 640)
        self.root.configure(bg=BG)

        # Kubernetes state
        self.ns_var        = tk.StringVar(value="default")
        self.res_var       = tk.StringVar(value="pods")
        self.all_ns_var    = tk.BooleanVar(value=False)
        self.context_var   = tk.StringVar(value="—")

        # Login form values
        self.ske_url_var   = tk.StringVar()
        self.auth_url_var  = tk.StringVar()
        self.user_var      = tk.StringVar()
        self.pass_var      = tk.StringVar()

        # Log viewer
        self.log_pod_var   = tk.StringVar()
        self.log_cnt_var   = tk.StringVar()
        self.log_tail_var  = tk.StringVar(value="300")
        self.prev_logs_var = tk.BooleanVar(value=False)

        # Custom command
        self.custom_var    = tk.StringVar(value="kubectl ")

        # Session management
        self.auto_renew_var   = tk.BooleanVar(value=True)
        self.session_lbl_var  = tk.StringVar(value="")
        self._session_expiry  = None      # datetime (UTC) or None
        self._session_stop    = threading.Event()
        self._renewing        = False     # guard against parallel renewals
        self._creds           = None      # (ske_url, auth_url, user, pw, skectl)

        # Internal
        self._q            = queue.Queue()
        self._log_stop     = threading.Event()
        self._log_thread   = None
        self._toast_win    = None

        self._apply_style()
        self._build_login_frame()
        self._build_main_frame()
        self._show_login()
        self._poll()

    # ── Style ──────────────────────────────────────────────────────────────────

    def _apply_style(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".",
                    background=BG, foreground=FG, font=("Consolas", 10))
        s.configure("TFrame",           background=BG)
        s.configure("TLabel",           background=BG,       foreground=FG)
        s.configure("TButton",          background=SURFACE,   foreground=FG,
                    relief="flat", padding=4)
        s.map("TButton",
              background=[("active", ACCENT)], foreground=[("active", BG)])
        s.configure("Accent.TButton",   background=ACCENT,    foreground=BG,
                    font=("Consolas", 10, "bold"))
        s.map("Accent.TButton",
              background=[("active", "#74c7ec")])
        s.configure("TEntry",
                    fieldbackground=ENTRY_BG, foreground=FG, insertcolor=FG)
        s.configure("TCombobox",
                    fieldbackground=ENTRY_BG, foreground=FG, background=SURFACE,
                    selectbackground=ACCENT,  selectforeground=BG)
        s.map("TCombobox", fieldbackground=[("readonly", ENTRY_BG)])
        s.configure("Treeview",
                    background=SURFACE, foreground=FG,
                    fieldbackground=SURFACE, rowheight=22)
        s.configure("Treeview.Heading",
                    background=ENTRY_BG, foreground=ACCENT,
                    font=("Consolas", 10, "bold"))
        s.map("Treeview",
              background=[("selected", ACCENT)], foreground=[("selected", BG)])
        s.configure("TNotebook",        background=BG, tabmargins=[2, 4, 2, 0])
        s.configure("TNotebook.Tab",    background=SURFACE, foreground=FG,
                    padding=[12, 4])
        s.map("TNotebook.Tab",
              background=[("selected", ACCENT)], foreground=[("selected", BG)])
        s.configure("TLabelframe",      background=BG,        foreground=ACCENT)
        s.configure("TLabelframe.Label",background=BG,        foreground=ACCENT,
                    font=("Consolas", 10, "bold"))
        s.configure("TCheckbutton",     background=BG,        foreground=FG)
        s.configure("TSeparator",       background=SURFACE)
        s.configure("TScrollbar",       background=SURFACE,   troughcolor=BG,
                    arrowcolor=FG)

    # ── Login frame ────────────────────────────────────────────────────────────

    def _build_login_frame(self):
        self.login_frame = tk.Frame(self.root, bg=BG)

        card = tk.Frame(self.login_frame, bg=SURFACE, padx=36, pady=36)
        card.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(card, text="SKE Manager", bg=SURFACE, fg=ACCENT,
                 font=("Consolas", 22, "bold")).grid(
                     row=0, column=0, columnspan=2, pady=(0, 24))

        fields = [
            ("SKE URL",   self.ske_url_var,  False, "https://ske.example.com"),
            ("Auth URL",  self.auth_url_var, False, "https://auth.example.com"),
            ("Username",  self.user_var,     False, ""),
            ("Password",  self.pass_var,     True,  ""),
        ]
        self._login_entries = []
        for i, (lbl, var, secret, placeholder) in enumerate(fields, 1):
            tk.Label(card, text=lbl, bg=SURFACE, fg=FG,
                     font=("Consolas", 10), width=10, anchor="e").grid(
                         row=i, column=0, padx=(0, 10), pady=6)
            e = tk.Entry(card, textvariable=var, show="*" if secret else "",
                         bg=ENTRY_BG, fg=FG, insertbackground=FG,
                         relief="flat", width=44, font=("Consolas", 10))
            e.grid(row=i, column=1, pady=6, ipady=4)
            if placeholder:
                self._add_placeholder(e, placeholder, var)
            self._login_entries.append(e)
            e.bind("<Return>", lambda _: self._do_login())

        # skectl path row
        tk.Label(card, text="skectl path", bg=SURFACE, fg=DIM,
                 font=("Consolas", 9), width=10, anchor="e").grid(
                     row=5, column=0, padx=(0, 10), pady=4)
        path_f = tk.Frame(card, bg=SURFACE)
        path_f.grid(row=5, column=1, pady=4, sticky="ew")
        self.skectl_path_var = tk.StringVar(value=SKECTL_CMD)
        tk.Entry(path_f, textvariable=self.skectl_path_var,
                 bg=ENTRY_BG, fg=DIM, insertbackground=FG,
                 relief="flat", width=36, font=("Consolas", 9)).pack(
                     side="left", ipady=3)
        tk.Button(path_f, text="Browse", bg=SURFACE, fg=DIM,
                  relief="flat", font=("Consolas", 9), cursor="hand2",
                  command=self._browse_skectl).pack(side="left", padx=(6, 0))

        self.login_btn = tk.Button(
            card, text="  Login  ", bg=ACCENT, fg=BG,
            font=("Consolas", 12, "bold"), relief="flat",
            cursor="hand2", padx=16, pady=6,
            command=self._do_login)
        self.login_btn.grid(row=6, column=0, columnspan=2, pady=(22, 0))

        self.login_status = tk.Label(card, text="", bg=SURFACE, fg=ERROR,
                                     font=("Consolas", 9))
        self.login_status.grid(row=7, column=0, columnspan=2, pady=(8, 0))

        self._login_entries[0].focus_set()

    def _add_placeholder(self, entry, text, var):
        def on_focus_in(_):
            if var.get() == text:
                var.set("")
                entry.config(fg=FG)
        def on_focus_out(_):
            if not var.get():
                var.set(text)
                entry.config(fg=DIM)
        var.set(text)
        entry.config(fg=DIM)
        entry.bind("<FocusIn>",  on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)

    def _browse_skectl(self):
        path = filedialog.askopenfilename(
            title="Locate skectl executable",
            filetypes=[("Executable", "*.exe"), ("All files", "*")])
        if path:
            self.skectl_path_var.set(path)

    def _show_login(self):
        self._session_stop.set()        # stop monitor when returning to login
        self.main_frame.pack_forget()
        self.login_frame.pack(fill="both", expand=True)

    def _show_main(self):
        self.login_frame.pack_forget()
        self.main_frame.pack(fill="both", expand=True)

    def _do_login(self):
        ske  = self.ske_url_var.get().strip()
        auth = self.auth_url_var.get().strip()
        user = self.user_var.get().strip()
        pw   = self.pass_var.get()

        for placeholder in ("https://ske.example.com", "https://auth.example.com"):
            if ske == placeholder:   ske  = ""
            if auth == placeholder:  auth = ""

        if not all([ske, auth, user, pw]):
            self.login_status.config(text="All fields are required.", fg=ERROR)
            return

        self.login_btn.config(text="Logging in…", state="disabled")
        self.login_status.config(text="", fg=FG)

        skectl = self.skectl_path_var.get().strip() or SKECTL_CMD
        args   = [skectl, "login", ske, "-s", auth, "-u", user, "-p", pw]

        def _t():
            stdout, stderr, rc = run_cmd(args, timeout=30)
            self._q.put(("login_done", rc, stdout or stderr,
                          ske, auth, user, pw, skectl))
        threading.Thread(target=_t, daemon=True).start()

    def _on_login_done(self, rc, msg, ske, auth, user, pw, skectl):
        self.login_btn.config(text="  Login  ", state="normal")
        if rc == 0:
            self.login_status.config(text="Login successful!", fg=SUCCESS)
            # Store credentials for auto-renew
            self._creds = (ske, auth, user, pw, skectl)
            self._load_namespaces()
            self._show_main()
            self._start_session_monitor()
        else:
            self.login_status.config(
                text="Login failed — " + msg.splitlines()[0][:80], fg=ERROR)

    # ── Main frame ─────────────────────────────────────────────────────────────

    def _build_main_frame(self):
        self.main_frame = tk.Frame(self.root, bg=BG)

        # ── Top bar ─────────────────────────────────────────────────────────────
        top = tk.Frame(self.main_frame, bg=SURFACE, pady=7, padx=10)
        top.pack(fill="x", side="top")

        tk.Label(top, text="SKE Manager", bg=SURFACE, fg=ACCENT,
                 font=("Consolas", 13, "bold")).pack(side="left", padx=(0, 12))

        tk.Label(top, text="Context:", bg=SURFACE, fg=DIM,
                 font=("Consolas", 9)).pack(side="left")
        tk.Label(top, textvariable=self.context_var, bg=SURFACE, fg=WARN,
                 font=("Consolas", 9)).pack(side="left", padx=(2, 14))

        # Namespace
        tk.Label(top, text="Namespace:", bg=SURFACE, fg=FG).pack(side="left")
        self.ns_combo = ttk.Combobox(top, textvariable=self.ns_var,
                                     width=26, state="readonly")
        self.ns_combo.pack(side="left", padx=4)
        self.ns_combo.bind("<<ComboboxSelected>>",
                           lambda _: self._refresh_resources())

        ttk.Checkbutton(top, text="All NS", variable=self.all_ns_var,
                        command=self._on_all_ns_toggle).pack(side="left", padx=6)

        # Resource type
        tk.Label(top, text="Resource:", bg=SURFACE, fg=FG).pack(
            side="left", padx=(10, 0))
        self.res_combo = ttk.Combobox(top, textvariable=self.res_var,
                                      values=RESOURCE_TYPES, width=22,
                                      state="readonly")
        self.res_combo.pack(side="left", padx=4)
        self.res_combo.bind("<<ComboboxSelected>>",
                            lambda _: self._refresh_resources())

        tk.Button(top, text="⟳  Refresh", bg=ACCENT, fg=BG,
                  relief="flat", font=("Consolas", 9, "bold"), cursor="hand2",
                  command=self._refresh_resources).pack(side="left", padx=8)

        tk.Button(top, text="⬡  Namespaces", bg=SURFACE, fg=FG,
                  relief="flat", font=("Consolas", 9), cursor="hand2",
                  command=self._load_namespaces).pack(side="left")

        # ── Session status (right side of top bar) ───────────────────────────
        tk.Button(top, text="Logout", bg=SURFACE, fg=WARN,
                  relief="flat", font=("Consolas", 9), cursor="hand2",
                  command=self._show_login).pack(side="right", padx=6)

        tk.Button(top, text="⟳ Renew", bg=SURFACE, fg=SUCCESS,
                  relief="flat", font=("Consolas", 9, "bold"), cursor="hand2",
                  command=self._manual_renew).pack(side="right", padx=2)

        ttk.Checkbutton(top, text="Auto-renew", variable=self.auto_renew_var
                        ).pack(side="right", padx=(0, 8))

        # Session countdown label — colour updated live
        self.session_lbl = tk.Label(top, textvariable=self.session_lbl_var,
                                    bg=SURFACE, fg=SUCCESS,
                                    font=("Consolas", 9, "bold"))
        self.session_lbl.pack(side="right", padx=(0, 10))

        tk.Label(top, text="Session:", bg=SURFACE, fg=DIM,
                 font=("Consolas", 9)).pack(side="right")

        # ── Horizontal paned: left list | right output ──────────────────────────
        pw = tk.PanedWindow(self.main_frame, orient="horizontal",
                            bg=BG, sashwidth=6, sashrelief="flat",
                            sashpad=2)
        pw.pack(fill="both", expand=True, padx=4, pady=4)

        left = tk.Frame(pw, bg=BG)
        pw.add(left, minsize=400, stretch="always")
        self._build_list_panel(left)

        right = tk.Frame(pw, bg=BG)
        pw.add(right, minsize=380, stretch="always")

        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)

        self._build_output_tab()
        self._build_logs_tab()
        self._build_custom_tab()
        self._build_yaml_tab()

        self._ctx = tk.Menu(self.root, tearoff=0, bg=SURFACE, fg=FG,
                            activebackground=ACCENT, activeforeground=BG,
                            font=("Consolas", 10))

    # ── List panel ─────────────────────────────────────────────────────────────

    def _build_list_panel(self, parent):
        lf = ttk.LabelFrame(parent, text=" Resources ", padding=4)
        lf.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(lf, show="headings", selectmode="extended")
        vsb = ttk.Scrollbar(lf, orient="vertical",   command=self.tree.yview)
        hsb = ttk.Scrollbar(lf, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)

        self.tree.bind("<Button-3>",         self._right_click)
        self.tree.bind("<Double-1>",         lambda _: self._describe())
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        act = tk.Frame(parent, bg=BG, pady=4)
        act.pack(fill="x", padx=2)

        btns = [
            ("Describe",    self._describe,    SURFACE, FG),
            ("Logs",        self._start_logs,  SURFACE, FG),
            ("Events",      self._show_events, SURFACE, FG),
            ("Get YAML",    self._get_yaml,    SURFACE, FG),
            ("Exec Shell",  self._exec_shell,  SURFACE, FG),
            ("Restart",     self._restart,     SURFACE, FG),
            ("Scale",       self._scale,       SURFACE, FG),
            ("Delete",      self._delete,      ERROR,   BG),
        ]
        cols = 4
        for i, (label, cmd, bg, fg) in enumerate(btns):
            tk.Button(act, text=label, bg=bg, fg=fg, relief="flat",
                      cursor="hand2", padx=6, pady=4, font=("Consolas", 9),
                      command=cmd).grid(row=i // cols, column=i % cols,
                                        padx=2, pady=2, sticky="ew")
        for c in range(cols):
            act.columnconfigure(c, weight=1)

    # ── Output tab ─────────────────────────────────────────────────────────────

    def _build_output_tab(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="  Output  ")
        bar = tk.Frame(f, bg=BG, pady=2)
        bar.pack(fill="x", padx=4)
        tk.Button(bar, text="Clear", bg=SURFACE, fg=FG, relief="flat",
                  font=("Consolas", 8), cursor="hand2",
                  command=lambda: self._clear_text(self.out_text)).pack(side="right")
        self.out_text = scrolledtext.ScrolledText(
            f, bg=SURFACE, fg=FG, font=("Consolas", 10),
            state="disabled", relief="flat", wrap="none", insertbackground=FG)
        self.out_text.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        for tag, colour in [("err", ERROR), ("ok", SUCCESS),
                             ("warn", WARN), ("accent", ACCENT), ("dim", DIM)]:
            self.out_text.tag_configure(tag, foreground=colour)

    # ── Live logs tab ──────────────────────────────────────────────────────────

    def _build_logs_tab(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="  Live Logs  ")

        ctrl = tk.Frame(f, bg=BG, pady=5, padx=6)
        ctrl.pack(fill="x")

        def lbl(text, col):
            tk.Label(ctrl, text=text, bg=BG, fg=FG).grid(
                row=0, column=col, padx=(6 if col else 0, 2), sticky="e")

        lbl("Pod:", 0)
        self.log_pod_combo = ttk.Combobox(ctrl, textvariable=self.log_pod_var,
                                           width=28, state="readonly")
        self.log_pod_combo.grid(row=0, column=1, padx=2)
        self.log_pod_combo.bind("<<ComboboxSelected>>", self._on_log_pod_select)

        lbl("Container:", 2)
        self.log_cnt_combo = ttk.Combobox(ctrl, textvariable=self.log_cnt_var,
                                           width=22, state="readonly")
        self.log_cnt_combo.grid(row=0, column=3, padx=2)

        lbl("Tail:", 4)
        tk.Entry(ctrl, textvariable=self.log_tail_var, width=6,
                 bg=ENTRY_BG, fg=FG, insertbackground=FG,
                 relief="flat", font=("Consolas", 10)).grid(
                     row=0, column=5, padx=2, ipady=2)

        tk.Button(ctrl, text="▶ Start", bg=SUCCESS, fg=BG, relief="flat",
                  font=("Consolas", 9, "bold"), cursor="hand2",
                  command=self._start_logs).grid(row=0, column=6, padx=(10, 2))
        tk.Button(ctrl, text="■ Stop", bg=ERROR, fg=BG, relief="flat",
                  font=("Consolas", 9, "bold"), cursor="hand2",
                  command=self._stop_logs).grid(row=0, column=7, padx=2)
        tk.Button(ctrl, text="Clear", bg=SURFACE, fg=FG, relief="flat",
                  font=("Consolas", 8), cursor="hand2",
                  command=lambda: self._clear_text(self.log_text)).grid(
                      row=0, column=8, padx=(6, 2))
        ttk.Checkbutton(ctrl, text="Previous", variable=self.prev_logs_var
                        ).grid(row=0, column=9, padx=4)

        self.log_text = scrolledtext.ScrolledText(
            f, bg=LOG_BG, fg=SUCCESS, font=("Consolas", 9),
            state="disabled", relief="flat", wrap="none", insertbackground=FG)
        self.log_text.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        for tag, colour in [("err", ERROR), ("warn", WARN),
                             ("info", ACCENT), ("dim", DIM)]:
            self.log_text.tag_configure(tag, foreground=colour)

    # ── Custom command tab ─────────────────────────────────────────────────────

    def _build_custom_tab(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="  Custom Command  ")

        tk.Label(f, text="Command:", bg=BG, fg=FG, anchor="w").pack(
            fill="x", padx=8, pady=(10, 2))

        row = tk.Frame(f, bg=BG)
        row.pack(fill="x", padx=8)
        self.custom_entry = tk.Entry(row, textvariable=self.custom_var,
                                     bg=ENTRY_BG, fg=FG, insertbackground=FG,
                                     relief="flat", font=("Consolas", 11))
        self.custom_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.custom_entry.bind("<Return>", lambda _: self._run_custom())

        tk.Button(row, text="  Run  ", bg=ACCENT, fg=BG, relief="flat",
                  font=("Consolas", 10, "bold"), cursor="hand2",
                  command=self._run_custom).pack(side="left", padx=(8, 0),
                                                 ipadx=6, ipady=6)

        tk.Label(f, text="Quick:", bg=BG, fg=DIM,
                 font=("Consolas", 9), anchor="w").pack(
                     fill="x", padx=8, pady=(12, 2))

        qf = tk.Frame(f, bg=BG)
        qf.pack(fill="x", padx=8)
        quick = [
            ("Get nodes",       "kubectl get nodes -o wide"),
            ("Cluster info",    "kubectl cluster-info"),
            ("Top nodes",       "kubectl top nodes"),
            ("Get contexts",    "kubectl config get-contexts"),
            ("Current context", "kubectl config current-context"),
            ("API resources",   "kubectl api-resources"),
            ("Get namespaces",  "kubectl get namespaces"),
            ("Version",         "kubectl version --short"),
        ]
        for i, (lbl, cmd) in enumerate(quick):
            def _cb(c=cmd):
                self.custom_var.set(c)
                self._run_custom()
            tk.Button(qf, text=lbl, bg=SURFACE, fg=FG, relief="flat",
                      cursor="hand2", font=("Consolas", 9),
                      command=_cb).grid(row=i // 4, column=i % 4,
                                        padx=3, pady=3, sticky="ew", ipady=3)
        for c in range(4):
            qf.columnconfigure(c, weight=1)

        tk.Button(f, text="Clear output", bg=SURFACE, fg=DIM, relief="flat",
                  font=("Consolas", 8), cursor="hand2",
                  command=lambda: self._clear_text(self.custom_text)).pack(
                      anchor="e", padx=8, pady=(8, 0))

        self.custom_text = scrolledtext.ScrolledText(
            f, bg=SURFACE, fg=FG, font=("Consolas", 10),
            state="disabled", relief="flat", wrap="none")
        self.custom_text.pack(fill="both", expand=True, padx=8, pady=6)
        for tag, colour in [("err", ERROR), ("ok", SUCCESS), ("accent", ACCENT)]:
            self.custom_text.tag_configure(tag, foreground=colour)

    # ── YAML tab ───────────────────────────────────────────────────────────────

    def _build_yaml_tab(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="  YAML  ")

        top = tk.Frame(f, bg=BG, pady=4, padx=6)
        top.pack(fill="x")
        tk.Button(top, text="Get YAML (selected)", bg=SURFACE, fg=FG,
                  relief="flat", font=("Consolas", 9), cursor="hand2",
                  command=self._get_yaml).pack(side="left", padx=(0, 8))
        tk.Button(top, text="Copy all", bg=SURFACE, fg=FG,
                  relief="flat", font=("Consolas", 9), cursor="hand2",
                  command=self._copy_yaml).pack(side="left")
        tk.Button(top, text="Clear", bg=SURFACE, fg=DIM,
                  relief="flat", font=("Consolas", 8), cursor="hand2",
                  command=lambda: self._clear_text(self.yaml_text)).pack(side="right")

        self.yaml_text = scrolledtext.ScrolledText(
            f, bg=LOG_BG, fg="#cba6f7", font=("Consolas", 10),
            state="disabled", relief="flat", wrap="none")
        self.yaml_text.pack(fill="both", expand=True, padx=4, pady=(0, 4))

    # ── Session monitor ────────────────────────────────────────────────────────

    def _start_session_monitor(self):
        """Launch the background thread that watches token expiry."""
        self._session_stop.clear()
        self._renewing = False
        t = threading.Thread(target=self._session_monitor_thread, daemon=True)
        t.start()

    def _session_monitor_thread(self):
        """
        Background thread: reads JWT expiry from kubeconfig every 30 s.
        Puts session status messages into the queue for the UI thread.
        Falls back to a kubectl health-check when no JWT expiry is found.
        """
        warned   = False   # have we shown the 5-min toast yet?
        expired_notified = False

        while not self._session_stop.is_set():
            expiry, _ = _kubeconfig_expiry()

            if expiry is None:
                # No JWT expiry found — do a lightweight health-check instead
                _, _, rc = run_cmd(
                    [KUBECTL_CMD, "get", "namespaces", "--request-timeout=5s"],
                    timeout=8)
                if rc != 0 and not self._session_stop.is_set():
                    self._q.put(("session_expired",))
                    expired_notified = True
                else:
                    self._q.put(("session_unknown",))
                # Re-check every 60 s when we have no JWT to read
                self._session_stop.wait(60)
                continue

            now       = datetime.now(tz=timezone.utc)
            remaining = (expiry - now).total_seconds()

            if remaining <= 0:
                if not expired_notified:
                    self._q.put(("session_expired",))
                    expired_notified = True
            else:
                expired_notified = False
                self._q.put(("session_tick", remaining))

                # Warn once when crossing 5-min threshold
                if remaining <= WARN_SECS and not warned:
                    warned = True
                    self._q.put(("session_warn", remaining))

                # Reset warn flag so it fires again after a renew
                if remaining > WARN_SECS:
                    warned = False

                # Auto-renew 2 min before expiry
                if remaining <= RENEW_SECS and self.auto_renew_var.get():
                    if not self._renewing:
                        self._q.put(("session_autorenew",))

            self._session_stop.wait(30)

    def _do_renew(self, silent=False):
        """Re-run skectl login using stored credentials."""
        if self._creds is None:
            if not silent:
                messagebox.showwarning("Cannot renew",
                                       "No stored credentials — please log in again.")
            return
        if self._renewing:
            return
        self._renewing = True

        ske, auth, user, pw, skectl = self._creds
        args = [skectl, "login", ske, "-s", auth, "-u", user, "-p", pw]

        def _t():
            stdout, stderr, rc = run_cmd(args, timeout=30)
            self._q.put(("renew_done", rc, stdout or stderr))
        threading.Thread(target=_t, daemon=True).start()

    def _manual_renew(self):
        self._do_renew(silent=False)

    def _on_renew_done(self, rc, msg):
        self._renewing = False
        if rc == 0:
            self._write(self.out_text,
                        "\n[%s] Session renewed successfully.\n"
                        % datetime.now().strftime("%H:%M:%S"), "ok")
            self._q.put(("session_toast", "Session renewed",
                         "Token refreshed successfully.", SUCCESS))
        else:
            self._write(self.out_text,
                        "\n[RENEW ERROR] " + msg + "\n", "err")
            self._q.put(("session_toast", "Renew failed",
                         msg.splitlines()[0][:60], ERROR))

    # ── Toast notification ─────────────────────────────────────────────────────

    def _show_toast(self, title, message, colour=WARN, duration_ms=7000):
        """Non-stealing overlay toast in the bottom-right corner."""
        if self._toast_win and self._toast_win.winfo_exists():
            self._toast_win.destroy()

        t = tk.Toplevel(self.root)
        self._toast_win = t
        t.overrideredirect(True)      # no title bar / decorations
        t.attributes("-topmost", True)
        t.configure(bg=colour)

        w, h = 340, 72
        sw   = t.winfo_screenwidth()
        sh   = t.winfo_screenheight()
        t.geometry("%dx%d+%d+%d" % (w, h, sw - w - 24, sh - h - 60))

        # Click anywhere on toast to dismiss
        dismiss = lambda _: t.destroy()
        t.bind("<Button-1>", dismiss)

        inner = tk.Frame(t, bg=colour, padx=14, pady=8)
        inner.pack(fill="both", expand=True)
        inner.bind("<Button-1>", dismiss)

        tk.Label(inner, text=title, bg=colour, fg=BG,
                 font=("Consolas", 11, "bold"), anchor="w").pack(fill="x")
        tk.Label(inner, text=message, bg=colour, fg=BG,
                 font=("Consolas", 9), anchor="w").pack(fill="x")

        t.after(duration_ms, lambda: t.destroy() if t.winfo_exists() else None)

    # ── Queue poll loop ────────────────────────────────────────────────────────

    def _poll(self):
        try:
            while True:
                msg  = self._q.get_nowait()
                kind = msg[0]

                if   kind == "login_done":      self._on_login_done(*msg[1:])
                elif kind == "ns_list":         self._on_ns_list(msg[1])
                elif kind == "ctx":             self.context_var.set(msg[1])
                elif kind == "tree":            self._populate_tree(*msg[1:])
                elif kind == "out":             self._write(self.out_text, msg[1], msg[2])
                elif kind == "log":             self._write_log(msg[1])
                elif kind == "yaml":            self._write(self.yaml_text, msg[1])
                elif kind == "custom":          self._write(self.custom_text, msg[1], msg[2])
                elif kind == "pod_list":        self._on_pod_list(msg[1])
                elif kind == "cnt_list":        self._on_cnt_list(msg[1])
                elif kind == "renew_done":      self._on_renew_done(msg[1], msg[2])

                # ── Session messages ─────────────────────────────────────────
                elif kind == "session_tick":
                    self._update_session_display(msg[1])

                elif kind == "session_unknown":
                    self.session_lbl_var.set("unknown")
                    self.session_lbl.config(fg=DIM)

                elif kind == "session_warn":
                    secs = msg[1]
                    self._show_toast(
                        "⚠  Session expiring soon",
                        "Expires in %s.  Auto-renew: %s" % (
                            _fmt_countdown(secs),
                            "ON" if self.auto_renew_var.get() else "OFF — click Renew"),
                        colour=WARN, duration_ms=10000)

                elif kind == "session_expired":
                    self.session_lbl_var.set("EXPIRED")
                    self.session_lbl.config(fg=ERROR)
                    self._show_toast(
                        "✖  Session expired",
                        "kubectl commands will fail.  Please renew or log in again.",
                        colour=ERROR, duration_ms=0)   # 0 = stays until clicked

                elif kind == "session_autorenew":
                    self._write(self.out_text,
                                "\n[%s] Auto-renewing session…\n"
                                % datetime.now().strftime("%H:%M:%S"), "warn")
                    self._do_renew(silent=True)

                elif kind == "session_toast":
                    _, title, body, colour = msg
                    self._show_toast(title, body, colour=colour)

        except queue.Empty:
            pass
        self.root.after(60, self._poll)

    def _update_session_display(self, secs: float):
        text = _fmt_countdown(secs)
        if secs <= RENEW_SECS:
            colour = ERROR
        elif secs <= WARN_SECS:
            colour = WARN
        else:
            colour = SUCCESS
        self.session_lbl_var.set(text)
        self.session_lbl.config(fg=colour)

    # ── Namespace helpers ──────────────────────────────────────────────────────

    def _load_namespaces(self):
        def _t():
            stdout, _, _ = run_cmd([KUBECTL_CMD, "config", "current-context"])
            self._q.put(("ctx", stdout.strip() or "—"))
            stdout, stderr, rc = run_cmd(
                [KUBECTL_CMD, "get", "namespaces",
                 "-o", "jsonpath={.items[*].metadata.name}"])
            if rc == 0:
                self._q.put(("ns_list", sorted(stdout.strip().split())))
            else:
                self._q.put(("out", "[WARN] Cannot list namespaces: "
                             + stderr + "\n", "warn"))
        threading.Thread(target=_t, daemon=True).start()

    def _on_ns_list(self, ns_list):
        self.ns_combo["values"] = ns_list
        if ns_list and self.ns_var.get() not in ns_list:
            self.ns_var.set(ns_list[0])
        self._refresh_resources()

    def _on_all_ns_toggle(self):
        self.ns_combo.config(
            state="disabled" if self.all_ns_var.get() else "readonly")
        self._refresh_resources()

    # ── Resource listing ───────────────────────────────────────────────────────

    def _ns_args(self):
        res = self.res_var.get()
        if res in ("namespaces", "nodes", "persistentvolumes",
                   "clusterroles", "clusterrolebindings"):
            return []
        if self.all_ns_var.get():
            return ["--all-namespaces"]
        return ["-n", self.ns_var.get()]

    def _refresh_resources(self):
        res  = self.res_var.get()
        args = [KUBECTL_CMD, "get", res, "-o", "wide"] + self._ns_args()
        self._write(self.out_text, "\n» " + " ".join(args) + "\n", "accent")

        def _t():
            stdout, stderr, rc = run_cmd(args, timeout=25)
            if rc == 0:
                cols, rows = _parse_table(stdout)
                self._q.put(("tree", cols, rows))
                if res == "pods":
                    self._q.put(("pod_list", [r[0] for r in rows if r]))
            else:
                self._q.put(("out", "[ERROR] " + stderr + "\n", "err"))
        threading.Thread(target=_t, daemon=True).start()

    def _populate_tree(self, cols, rows):
        self.tree.delete(*self.tree.get_children())
        if not cols:
            return
        self.tree["columns"] = cols
        for c in cols:
            self.tree.heading(c, text=c,
                              command=lambda col=c: _sort_tree(self.tree, col))
            self.tree.column(c, width=max(len(c) * 11, 90), stretch=True)
        for row in rows:
            padded = (row + [""] * len(cols))[:len(cols)]
            tag    = _status_tag(padded[1] if len(padded) > 1 else "")
            self.tree.insert("", "end", values=padded, tags=(tag,))
        self.tree.tag_configure("running",     foreground=SUCCESS)
        self.tree.tag_configure("error_row",   foreground=ERROR)
        self.tree.tag_configure("pending_row", foreground=WARN)

    # ── Tree events ────────────────────────────────────────────────────────────

    def _on_tree_select(self, _):
        if self.res_var.get() == "pods":
            name = self._selected()
            if name:
                self.log_pod_var.set(name)
                self._fetch_containers(name)

    def _right_click(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
        m = self._ctx
        m.delete(0, "end")
        for label, cmd in [
            ("Describe",   self._describe),
            ("Logs",       self._start_logs),
            ("Events",     self._show_events),
            ("Get YAML",   self._get_yaml),
            (None,         None),
            ("Exec Shell", self._exec_shell),
            ("Restart",    self._restart),
            ("Scale",      self._scale),
            (None,         None),
            ("Delete",     self._delete),
            ("Copy name",  self._copy_name),
        ]:
            if label is None:
                m.add_separator()
            else:
                m.add_command(label=label, command=cmd)
        m.tk_popup(event.x_root, event.y_root)

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("No selection", "Select a resource first.")
            return None
        return self.tree.item(sel[0], "values")[0]

    def _selected_all(self):
        return [self.tree.item(s, "values")[0] for s in self.tree.selection()]

    # ── kubectl actions ────────────────────────────────────────────────────────

    def _kube(self, args):
        self.nb.select(0)
        self._write(self.out_text, "\n» " + " ".join(args) + "\n", "accent")
        def _t():
            stdout, stderr, rc = run_cmd(args, timeout=40)
            out = stdout if rc == 0 else (stderr or stdout)
            self._q.put(("out", out + "\n", "ok" if rc == 0 else "err"))
        threading.Thread(target=_t, daemon=True).start()

    def _describe(self):
        name = self._selected()
        if not name: return
        self._kube([KUBECTL_CMD, "describe", self.res_var.get(), name]
                   + self._ns_args())

    def _show_events(self):
        name = self._selected()
        args = [KUBECTL_CMD, "get", "events"] + self._ns_args()
        if name:
            args += ["--field-selector", "involvedObject.name=" + name]
        self._kube(args)

    def _delete(self):
        names = self._selected_all()
        if not names: return
        ns_txt = ("" if self.all_ns_var.get()
                  else " in '%s'" % self.ns_var.get())
        if not messagebox.askyesno(
                "Confirm Delete",
                "Delete %d %s(s)%s?\n%s%s\n\nThis cannot be undone!" % (
                    len(names), self.res_var.get(), ns_txt,
                    ", ".join(names[:6]),
                    "…" if len(names) > 6 else "")):
            return
        for name in names:
            self._kube([KUBECTL_CMD, "delete", self.res_var.get(), name]
                       + self._ns_args())

    def _restart(self):
        name = self._selected()
        if not name: return
        res = self.res_var.get()
        if res not in ("deployments", "daemonsets", "statefulsets"):
            messagebox.showwarning("Not supported",
                                   "Restart works for deployments, daemonsets, statefulsets.")
            return
        self._kube([KUBECTL_CMD, "rollout", "restart", res + "/" + name]
                   + self._ns_args())

    def _scale(self):
        name = self._selected()
        if not name: return
        res = self.res_var.get()
        if res not in ("deployments", "replicasets", "statefulsets"):
            messagebox.showwarning("Not supported",
                                   "Scale works for deployments, replicasets, statefulsets.")
            return
        n = simpledialog.askinteger(
            "Scale", "Replicas for %s:" % name,
            initialvalue=1, minvalue=0, maxvalue=200)
        if n is None: return
        self._kube([KUBECTL_CMD, "scale", res + "/" + name,
                    "--replicas=" + str(n)] + self._ns_args())

    def _exec_shell(self):
        name = self._selected()
        if not name: return
        ns = self.ns_var.get()
        if sys.platform == "win32":
            cmd = ("kubectl exec -it %s -n %s -- /bin/sh 2>nul || "
                   "kubectl exec -it %s -n %s -- cmd" % (name, ns, name, ns))
            subprocess.Popen(["cmd.exe", "/c", "start", "cmd.exe", "/k", cmd],
                             **_win_flags())
        elif sys.platform == "darwin":
            script = ('tell app "Terminal" to do script '
                      '"kubectl exec -it %s -n %s -- /bin/bash || '
                      'kubectl exec -it %s -n %s -- /bin/sh"' % (name, ns, name, ns))
            subprocess.Popen(["osascript", "-e", script])
        else:
            for term in ("gnome-terminal", "xterm", "konsole"):
                try:
                    subprocess.Popen([term, "--", "kubectl", "exec", "-it",
                                      name, "-n", ns, "--", "/bin/bash"])
                    break
                except FileNotFoundError:
                    continue
        self._write(self.out_text,
                    "\n» Opened terminal exec for: %s\n" % name, "ok")

    # ── YAML ───────────────────────────────────────────────────────────────────

    def _get_yaml(self):
        name = self._selected()
        if not name: return
        args = ([KUBECTL_CMD, "get", self.res_var.get(), name,
                 "-o", "yaml"] + self._ns_args())
        self._write(self.out_text, "\n» " + " ".join(args) + "\n", "accent")
        self.nb.select(3)
        def _t():
            stdout, stderr, rc = run_cmd(args, timeout=15)
            self._q.put(("yaml", stdout if rc == 0 else stderr))
        threading.Thread(target=_t, daemon=True).start()

    def _copy_yaml(self):
        txt = self.yaml_text.get("1.0", "end").strip()
        if txt:
            self.root.clipboard_clear()
            self.root.clipboard_append(txt)

    # ── Live logs ──────────────────────────────────────────────────────────────

    def _on_pod_list(self, pods):
        self.log_pod_combo["values"] = pods
        if pods and not self.log_pod_var.get():
            self.log_pod_var.set(pods[0])
            self._fetch_containers(pods[0])

    def _on_cnt_list(self, containers):
        self.log_cnt_combo["values"] = [""] + containers
        if containers:
            self.log_cnt_var.set(containers[0])

    def _on_log_pod_select(self, _=None):
        pod = self.log_pod_var.get()
        if pod:
            self._fetch_containers(pod)

    def _fetch_containers(self, pod):
        ns = self.ns_var.get()
        def _t():
            stdout, _, rc = run_cmd([
                KUBECTL_CMD, "get", "pod", pod, "-n", ns,
                "-o", "jsonpath={.spec.containers[*].name}"])
            if rc == 0:
                self._q.put(("cnt_list", stdout.strip().split()))
        threading.Thread(target=_t, daemon=True).start()

    def _start_logs(self):
        pod = self.log_pod_var.get()
        if not pod:
            name = self._selected()
            if name and self.res_var.get() == "pods":
                pod = name
                self.log_pod_var.set(pod)
            else:
                messagebox.showwarning("No pod", "Select a pod first.")
                return

        self._stop_logs()
        ns   = self.ns_var.get()
        cnt  = self.log_cnt_var.get()
        tail = self.log_tail_var.get() or "300"

        args = [KUBECTL_CMD, "logs", "-f", pod, "-n", ns, "--tail=" + tail]
        if cnt:
            args += ["-c", cnt]
        if self.prev_logs_var.get():
            args.append("-p")

        self.nb.select(1)
        self._write(self.log_text,
                    "\n═══ %s%s  (tail=%s) ═══\n" % (
                        pod, " [%s]" % cnt if cnt else "", tail),
                    "info")

        self._log_stop.clear()
        stop = self._log_stop

        def _line(line):
            self._q.put(("log", line))

        self._log_thread = threading.Thread(
            target=stream_cmd, args=(args, _line, stop), daemon=True)
        self._log_thread.start()

    def _stop_logs(self):
        self._log_stop.set()

    def _write_log(self, line):
        low = line.lower()
        tag = ("err"  if any(x in low for x in ("error", "exception", "fatal", "panic")) else
               "warn" if "warn" in low else
               "info" if any(x in low for x in ("info", "debug")) else "")
        self._write(self.log_text, line + "\n", tag)

    # ── Custom command ─────────────────────────────────────────────────────────

    def _run_custom(self):
        raw = self.custom_var.get().strip()
        if not raw: return
        try:
            args = shlex.split(raw, posix=(sys.platform != "win32"))
        except ValueError as exc:
            self._write(self.custom_text, "[Parse error] %s\n" % exc, "err")
            return
        self._write(self.custom_text, "\n» " + raw + "\n", "accent")
        self.nb.select(2)
        def _t():
            stdout, stderr, rc = run_cmd(args, timeout=60)
            out = stdout if rc == 0 else (stderr or stdout)
            self._q.put(("custom", out + "\n", "ok" if rc == 0 else "err"))
        threading.Thread(target=_t, daemon=True).start()

    # ── Misc helpers ───────────────────────────────────────────────────────────

    def _copy_name(self):
        name = self._selected()
        if name:
            self.root.clipboard_clear()
            self.root.clipboard_append(name)

    @staticmethod
    def _clear_text(widget):
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.config(state="disabled")

    def _write(self, widget, text, tag=""):
        widget.config(state="normal")
        widget.insert("end", text, tag)
        widget.see("end")
        widget.config(state="disabled")


# ─── Utility functions ─────────────────────────────────────────────────────────

def _parse_table(text):
    lines = text.strip().splitlines()
    if not lines:
        return [], []
    return lines[0].split(), [l.split() for l in lines[1:] if l.strip()]


def _sort_tree(tree, col):
    data = [(tree.set(c, col), c) for c in tree.get_children("")]
    data.sort(key=lambda x: x[0])
    for i, (_, child) in enumerate(data):
        tree.move(child, "", i)
    tree.heading(col, command=lambda: _sort_tree_rev(tree, col))


def _sort_tree_rev(tree, col):
    data = [(tree.set(c, col), c) for c in tree.get_children("")]
    data.sort(key=lambda x: x[0], reverse=True)
    for i, (_, child) in enumerate(data):
        tree.move(child, "", i)
    tree.heading(col, command=lambda: _sort_tree(tree, col))


def _status_tag(status):
    up = status.upper()
    if any(x in up for x in ("ERROR", "CRASHLOOP", "OOMKILL", "FAILED",
                              "IMAGEPULL", "BACKOFF")):
        return "error_row"
    if any(x in up for x in ("RUNNING", "ACTIVE", "BOUND", "COMPLETE")):
        return "running"
    if any(x in up for x in ("PENDING", "TERMINAT", "UNKNOWN", "INIT")):
        return "pending_row"
    return ""


# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App(root)
    root.mainloop()
