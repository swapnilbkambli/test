#!/usr/bin/env python3
"""
GDP SKE Manager Web — Flask-based browser UI for GDP Kubernetes (SKE) Platform
Run:    python ske_manager_web.py
Opens:  http://localhost:5000
"""

import subprocess, sys

# ─── Private PyPI index (fill in your internal URL if required) ───────────────
#
#   PIP_INDEX_URL = "https://pypi.internal.your-company.com/simple"
#
PIP_INDEX_URL = "https://artifactory.global.standardchartered.com/artifactory/api/pypi/pypi/simple"   # leave empty to use the default public PyPI

# ─── Auto-install dependencies before anything else ────────────────────────────
_REQUIRED = {"flask": "flask", "webview": "pywebview", "flask_sock": "flask-sock", "cryptography": "cryptography"}   # import_name → pip package name
if sys.platform == "win32":
    _REQUIRED["winpty"] = "pywinpty"   # ConPTY for real interactive Windows terminals

def _ensure_deps():
    missing = []
    for imp, pkg in _REQUIRED.items():
        try:
            __import__(imp)
        except ImportError:
            missing.append(pkg)
    if not missing:
        return
    print(f"[GDP SKE Manager] Installing missing packages: {', '.join(missing)}")
    cmd = [sys.executable, "-m", "pip", "install", "--quiet"]
    if PIP_INDEX_URL:
        cmd += ["--index-url", PIP_INDEX_URL]
    cmd += missing
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode != 0:
        print("[GDP SKE Manager] pip install failed:\n" + result.stderr)
        sys.exit(1)
    print("[GDP SKE Manager] Done.\n")

_ensure_deps()

# ─── Imports (guaranteed present after _ensure_deps) ──────────────────────────
from flask import Flask, render_template, jsonify, request, Response, stream_with_context, send_from_directory, redirect
import threading, queue, json, base64, os, time, shlex, signal, struct, urllib.request
import webview
from flask_sock import Sock
from datetime import datetime, timezone

# ─── Optional: Windows ConPTY via pywinpty ─────────────────────────────────────
_HAS_WINPTY = False
if sys.platform == "win32":
    try:
        from winpty import PtyProcess as _WinPtyProcess
        _HAS_WINPTY = True
    except Exception:
        pass

app = Flask(__name__)
app.secret_key = os.urandom(24)
sock = Sock(app)

# ─── Paths ─────────────────────────────────────────────────────────────────────
KUBECTL_CMD = os.environ.get("KUBECTL_CMD", "kubectl")
SKECTL_CMD  = os.environ.get("SKECTL_CMD",
                              "skectl.exe" if sys.platform == "win32" else "skectl")
PORT        = int(os.environ.get("SKE_PORT", 5000))

# ─── Local static assets (cached so the app works without CDN access) ──────────
_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_CDN_ASSETS = {
    "xterm.css":          "https://cdn.jsdelivr.net/npm/xterm@4.19.0/css/xterm.css",
    "xterm.js":           "https://cdn.jsdelivr.net/npm/xterm@4.19.0/lib/xterm.js",
    "xterm-addon-fit.js": "https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.5.0/lib/xterm-addon-fit.js",
}

def _ensure_local_assets():
    """Download CDN JS/CSS assets once to static/ dir — allows offline / CDN-blocked environments."""
    os.makedirs(_ASSETS_DIR, exist_ok=True)
    # Use system proxy settings (picks up WinInet / HTTPS_PROXY env var)
    proxy_handler = urllib.request.ProxyHandler()
    opener = urllib.request.build_opener(proxy_handler)
    urllib.request.install_opener(opener)
    for fname, url in _CDN_ASSETS.items():
        dest = os.path.join(_ASSETS_DIR, fname)
        if not os.path.exists(dest):
            try:
                print(f"[GDP SKE Manager] Downloading {fname}\u2026")
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as resp, open(dest, "wb") as f:
                    f.write(resp.read())
                print(f"[GDP SKE Manager] Saved {fname} ({os.path.getsize(dest)} bytes)")
            except Exception as e:
                print(f"[GDP SKE Manager] Could not download {fname}: {e}")

@app.route("/assets/<path:filename>")
def serve_asset(filename):
    local_path = os.path.join(_ASSETS_DIR, filename)
    if os.path.exists(local_path):
        return send_from_directory(_ASSETS_DIR, filename)
    cdn_url = _CDN_ASSETS.get(filename)
    if cdn_url:
        return redirect(cdn_url)
    return "Not found", 404

# ─── Global session state (single-user local tool) ─────────────────────────────
_creds          = None          # (ske_url, auth_url, user, pw, skectl)
_log_stop       = threading.Event()
_log_q          = queue.Queue()
_portforwards   = {}            # id -> {proc, local, remote, target, ns}
_pf_id_counter  = 0

# ─── Subprocess helpers ────────────────────────────────────────────────────────

def _win_flags():
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def run_cmd(args, timeout=30):
    """Blocking; returns (stdout, stderr, returncode)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout, **_win_flags())
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", f"Timed out after {timeout}s", 1
    except FileNotFoundError:
        return "", f"Not found: {args[0]} — is it on PATH?", 1
    except Exception as e:
        return "", str(e), 1


# ─── JWT / config helpers ──────────────────────────────────────────────────────

def _jwt_expiry(token: str):
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1].replace("-", "+").replace("_", "/")
        payload += "=" * (4 - len(payload) % 4)
        data = json.loads(base64.b64decode(payload))
        exp = data.get("exp")
        if exp:
            return datetime.fromtimestamp(exp, tz=timezone.utc)
    except Exception:
        pass
    return None


def _kubeconfig_expiry():
    stdout, _, rc = run_cmd([KUBECTL_CMD, "config", "view", "--raw", "-o", "json"], timeout=10)
    if rc != 0:
        return None
    try:
        cfg     = json.loads(stdout)
        cur_ctx = cfg.get("current-context", "")
        ctx_map = {c["name"]: c.get("context", {}) for c in cfg.get("contexts", [])}
        usr_map = {u["name"]: u.get("user", {})    for u in cfg.get("users", [])}
        ctx     = ctx_map.get(cur_ctx, {})
        user    = usr_map.get(ctx.get("user", ""), {})
        token   = user.get("token", "")
        if token:
            return _jwt_expiry(token)
    except Exception:
        pass
    return None


def _config_path():
    return os.path.join(os.path.expanduser("~"), ".ske_manager.json")


def _load_cfg():
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"environments": {}, "last_environment": ""}


def _save_cfg(cfg):
    try:
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def _encode_pw(pw: str) -> str:
    """Base64-obfuscate password so it is not plain-text in the JSON file."""
    return base64.b64encode(pw.encode()).decode() if pw else ""


def _decode_pw(stored: str) -> str:
    """Reverse _encode_pw; also handles legacy plain-text (non-base64) values."""
    if not stored:
        return ""
    try:
        return base64.b64decode(stored.encode()).decode()
    except Exception:
        return stored  # legacy plain-text fallback


def _ns_flags(resource, namespace, all_ns):
    cluster_wide = resource in ("namespaces", "nodes", "persistentvolumes",
                                 "clusterroles", "clusterrolebindings")
    if cluster_wide:
        return []
    if all_ns:
        return ["--all-namespaces"]
    return ["-n", namespace] if namespace else []


# ─── Quota helpers ────────────────────────────────────────────────────────────

def _parse_quantity(s):
    """Parse a Kubernetes resource quantity string to a comparable float."""
    s = str(s).strip()
    if not s or s == "0":
        return 0.0
    suffixes = [("Ki", 1024), ("Mi", 1024**2), ("Gi", 1024**3), ("Ti", 1024**4),
                ("K", 1000),  ("M", 1000**2),  ("G", 1000**3),  ("T", 1000**4)]
    for suf, mult in suffixes:
        if s.endswith(suf):
            return float(s[:-len(suf)]) * mult
    if s.endswith("m"):           # millicores
        return float(s[:-1])
    try:
        return float(s) * 1000   # plain cores → millicores for comparison
    except ValueError:
        return 0.0


def _quota_pct(used_str, hard_str):
    """Return usage percentage (0-100) or None if unparseable."""
    try:
        h = _parse_quantity(hard_str)
        if h == 0:
            return None
        return round(_parse_quantity(used_str) / h * 100, 1)
    except Exception:
        return None


def _parse_kubectl_table(stdout):
    """Parse kubectl tabular output by fixed column positions.

    kubectl uses at least 2 spaces between columns.  Simple split() breaks
    when a cell value itself contains spaces, e.g. the newer RESTARTS field
    which renders as '21 (13h ago)'.  We detect column boundaries by finding
    runs of 2+ whitespace characters in the header line, then slice each data
    line at those same offsets.
    """
    import re
    lines = stdout.strip().splitlines()
    if not lines:
        return [], []
    header_line = lines[0]
    # Column boundary: position where a non-space char follows 2+ spaces
    col_starts = [0] + [m.start(1) for m in re.finditer(r'\s{2,}(\S)', header_line)]
    headers = []
    for i, start in enumerate(col_starts):
        end = col_starts[i + 1] if i + 1 < len(col_starts) else None
        h = (header_line[start:end] if end else header_line[start:]).strip()
        if h:
            headers.append(h)
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        cells = []
        for i, start in enumerate(col_starts):
            end = col_starts[i + 1] if i + 1 < len(col_starts) else None
            cell = (line[start:end] if end else line[start:]).strip()
            cells.append(cell)
        rows.append({"cells": cells, "raw": line})
    return headers, rows


def _fmt_age(secs):
    """Format seconds as a human-readable age string."""
    if secs is None:
        return "unknown"
    secs = int(secs)
    if secs < 60:    return f"{secs}s"
    if secs < 3600:  return f"{secs // 60}m"
    if secs < 86400: return f"{secs // 3600}h {(secs % 3600) // 60}m"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h"


# ─── Routes: UI ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ─── Routes: Auth ──────────────────────────────────────────────────────────────

@app.route("/api/login", methods=["POST"])
def login():
    global _creds
    d       = request.json or {}
    ske     = d.get("ske_url", "").strip()
    auth    = d.get("auth_url", "").strip()
    user    = d.get("username", "").strip()
    pw      = d.get("password", "")
    skectl  = d.get("skectl_path", "").strip() or SKECTL_CMD

    if not all([ske, auth, user, pw]):
        return jsonify(ok=False, error="All fields are required."), 400

    # Decode if the browser sends back a stored (encoded) password reused from cache.
    # In normal flow the password field is always typed fresh, but guard either way.
    pw_plain = _decode_pw(pw) if pw and not any(c in pw for c in ' /@:') else pw
    args = [skectl, "login", ske, "-s", auth, "-u", user, "-p", pw_plain]
    stdout, stderr, rc = run_cmd(args, timeout=30)
    if rc == 0:
        _creds = (ske, auth, user, pw_plain, skectl)
        return jsonify(ok=True)
    return jsonify(ok=False, error=(stderr or stdout).splitlines()[0][:120]), 401


@app.route("/api/session")
def session_status():
    expiry = _kubeconfig_expiry()
    ctx, _, _ = run_cmd([KUBECTL_CMD, "config", "current-context"])
    if expiry is None:
        _, _, rc = run_cmd([KUBECTL_CMD, "get", "namespaces",
                            "--request-timeout=5s"], timeout=8)
        return jsonify(context=ctx.strip(), expiry=None, remaining=None, healthy=(rc == 0))
    now       = datetime.now(tz=timezone.utc)
    remaining = max(0, (expiry - now).total_seconds())
    return jsonify(context=ctx.strip(), expiry=expiry.isoformat(),
                   remaining=int(remaining), healthy=(remaining > 0))


@app.route("/api/renew", methods=["POST"])
def renew():
    global _creds
    if _creds is None:
        return jsonify(ok=False, error="No stored credentials."), 400
    ske, auth, user, pw, skectl = _creds
    args = [skectl, "login", ske, "-s", auth, "-u", user, "-p", pw]
    stdout, stderr, rc = run_cmd(args, timeout=30)
    if rc == 0:
        return jsonify(ok=True)
    return jsonify(ok=False, error=(stderr or stdout).splitlines()[0][:120]), 401


# ─── Routes: Namespaces ────────────────────────────────────────────────────────

@app.route("/api/namespaces")
def namespaces():
    stdout, stderr, rc = run_cmd(
        [KUBECTL_CMD, "get", "namespaces",
         "-o", "jsonpath={.items[*].metadata.name}"])
    if rc == 0:
        return jsonify(ok=True, namespaces=sorted(stdout.strip().split()))
    # Fallback: RBAC may forbid listing namespaces but allow listing workloads
    for resource in ("pods", "services", "deployments"):
        out2, _, rc2 = run_cmd(
            [KUBECTL_CMD, "get", resource, "--all-namespaces",
             "-o", "jsonpath={.items[*].metadata.namespace}"],
            timeout=15)
        if rc2 == 0 and out2.strip():
            return jsonify(ok=True, namespaces=sorted(set(out2.strip().split())),
                           via_fallback=True)
    # All attempts failed — return HTTP 200 so frontend handles it gracefully
    return jsonify(ok=False, error=stderr.strip()), 200


# ─── Routes: Resources ─────────────────────────────────────────────────────────

@app.route("/api/resources")
def resources():
    res    = request.args.get("type", "pods")
    ns     = request.args.get("namespace", "default")
    all_ns = request.args.get("all", "false").lower() == "true"
    args   = [KUBECTL_CMD, "get", res, "-o", "wide"] + _ns_flags(res, ns, all_ns)
    stdout, stderr, rc = run_cmd(args, timeout=25)
    if rc != 0:
        return jsonify(ok=False, error=stderr), 500
    headers, rows = _parse_kubectl_table(stdout)
    cluster_wide = res in ("namespaces", "nodes", "persistentvolumes", "clusterroles", "clusterrolebindings")
    ns_col = None
    # Always restrict to t-55547* namespaces for namespaced resources
    if not cluster_wide and headers:
        try:
            ns_col = headers.index("NAMESPACE")
        except ValueError:
            ns_col = None
        if all_ns:
            if ns_col is not None:
                rows = [row for row in rows if len(row.cells) > ns_col and row.cells[ns_col].startswith("t-55547")]
        else:
            # Single namespace: if not t-55547*, return empty
            if not ns.startswith("t-55547"):
                rows = []
            elif ns_col is not None:
                rows = [row for row in rows if len(row.cells) > ns_col and row.cells[ns_col] == ns]
    return jsonify(ok=True, headers=headers, rows=rows, command=" ".join(args))


@app.route("/api/describe")
def describe():
    res    = request.args.get("type", "pods")
    name   = request.args.get("name", "")
    ns     = request.args.get("namespace", "default")
    all_ns = request.args.get("all", "false").lower() == "true"
    args   = [KUBECTL_CMD, "describe", res, name] + _ns_flags(res, ns, all_ns)
    stdout, stderr, rc = run_cmd(args, timeout=30)
    out = stdout if rc == 0 else stderr
    return jsonify(ok=(rc == 0), output=out, command=" ".join(args))


@app.route("/api/yaml")
def get_yaml():
    res    = request.args.get("type", "pods")
    name   = request.args.get("name", "")
    ns     = request.args.get("namespace", "default")
    all_ns = request.args.get("all", "false").lower() == "true"
    args   = [KUBECTL_CMD, "get", res, name, "-o", "yaml"] + _ns_flags(res, ns, all_ns)
    stdout, stderr, rc = run_cmd(args, timeout=15)
    out = stdout if rc == 0 else stderr
    return jsonify(ok=(rc == 0), output=out, command=" ".join(args))


@app.route("/api/events")
def events():
    ns     = request.args.get("namespace", "default")
    name   = request.args.get("name", "")
    all_ns = request.args.get("all", "false").lower() == "true"
    args   = [KUBECTL_CMD, "get", "events"] + _ns_flags("events", ns, all_ns)
    if name:
        args += ["--field-selector", f"involvedObject.name={name}"]
    stdout, stderr, rc = run_cmd(args, timeout=20)
    out = stdout if rc == 0 else stderr
    return jsonify(ok=(rc == 0), output=out, command=" ".join(args))


@app.route("/api/clipboard/read")
def clipboard_read():
    """Read clipboard text via PowerShell (no ctypes / no WebView2 permission dialog)."""
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"],
                capture_output=True, text=True, encoding="utf-8",
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            return jsonify(ok=True, text=(r.stdout or "").rstrip("\r\n"))
        return jsonify(ok=True, text="")
    except Exception as e:
        return jsonify(ok=False, text="", error=str(e))


@app.route("/api/clipboard/write", methods=["POST"])
def clipboard_write():
    """Write text to clipboard via PowerShell (no ctypes / no WebView2 permission dialog)."""
    try:
        text = (request.json or {}).get("text", "")
        if sys.platform == "win32":
            # Feed text through stdin to avoid any shell-escaping issues
            script = "[Console]::InputEncoding=[System.Text.Encoding]::UTF8; Set-Clipboard -Value ([Console]::In.ReadToEnd())"
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                input=text, text=True, encoding="utf-8", capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            return jsonify(ok=(r.returncode == 0))
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/api/delete", methods=["POST"])
def delete():
    d      = request.json or {}
    res    = d.get("type", "pods")
    names  = d.get("names", [])
    ns     = d.get("namespace", "default")
    all_ns = d.get("all", False)
    if not names:
        return jsonify(ok=False, error="No names provided."), 400
    outputs = []
    for name in names:
        args = [KUBECTL_CMD, "delete", res, name] + _ns_flags(res, ns, all_ns)
        stdout, stderr, rc = run_cmd(args, timeout=30)
        outputs.append({"name": name, "ok": rc == 0,
                         "output": stdout if rc == 0 else stderr,
                         "command": " ".join(args)})
    return jsonify(ok=True, results=outputs)


@app.route("/api/scale", methods=["POST"])
def scale():
    d        = request.json or {}
    res      = d.get("type", "deployments")
    name     = d.get("name", "")
    ns       = d.get("namespace", "default")
    replicas = d.get("replicas", 1)
    args     = [KUBECTL_CMD, "scale", f"{res}/{name}",
                f"--replicas={replicas}"] + _ns_flags(res, ns, False)
    stdout, stderr, rc = run_cmd(args, timeout=30)
    out = stdout if rc == 0 else stderr
    return jsonify(ok=(rc == 0), output=out, command=" ".join(args))


@app.route("/api/restart", methods=["POST"])
def restart():
    d    = request.json or {}
    res  = d.get("type", "deployments")
    name = d.get("name", "")
    ns   = d.get("namespace", "default")
    args = [KUBECTL_CMD, "rollout", "restart", f"{res}/{name}"] + _ns_flags(res, ns, False)
    stdout, stderr, rc = run_cmd(args, timeout=30)
    out = stdout if rc == 0 else stderr
    return jsonify(ok=(rc == 0), output=out, command=" ".join(args))


@app.route("/api/containers")
def containers():
    pod = request.args.get("pod", "")
    ns  = request.args.get("namespace", "default")
    stdout, _, rc = run_cmd([
        KUBECTL_CMD, "get", "pod", pod, "-n", ns,
        "-o", "jsonpath={.spec.containers[*].name}"])
    if rc != 0:
        return jsonify(ok=False, containers=[])
    return jsonify(ok=True, containers=stdout.strip().split())


@app.route("/api/command", methods=["POST"])
def run_command():
    d   = request.json or {}
    raw = d.get("cmd", "").strip()
    if not raw:
        return jsonify(ok=False, error="Empty command."), 400
    try:
        args = shlex.split(raw, posix=(sys.platform != "win32"))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    stdout, stderr, rc = run_cmd(args, timeout=60)
    out = stdout if rc == 0 else (stderr or stdout)
    return jsonify(ok=(rc == 0), output=out, command=raw)


# ─── Routes: Log streaming (SSE) ───────────────────────────────────────────────

@app.route("/api/logs/stream")
def stream_logs():
    global _log_stop, _log_q

    pod    = request.args.get("pod", "")
    ns     = request.args.get("namespace", "default")
    cnt    = request.args.get("container", "")
    tail   = request.args.get("tail", "300")
    prev   = request.args.get("previous", "false") == "true"
    follow = request.args.get("follow", "false") == "true"

    if not pod:
        return jsonify(ok=False, error="pod required"), 400

    _log_stop.set()
    time.sleep(0.15)
    _log_stop.clear()
    _log_q = queue.Queue()

    args = [KUBECTL_CMD, "logs", pod, "-n", ns, f"--tail={tail}"]
    if follow:
        args.append("-f")
    if cnt:
        args += ["-c", cnt]
    if prev:
        args += ["-p"]

    stop = _log_stop
    q    = _log_q

    def _reader():
        try:
            proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, **_win_flags())
            for line in iter(proc.stdout.readline, ""):
                if stop.is_set():
                    proc.terminate()
                    break
                q.put(line.rstrip("\n"))
            proc.wait()
        except Exception as e:
            q.put(f"[ERROR] {e}")
        finally:
            q.put(None)

    threading.Thread(target=_reader, daemon=True).start()

    def _generate():
        yield f"data: {json.dumps({'type': 'cmd', 'text': ' '.join(args)})}\n\n"
        while True:
            try:
                line = q.get(timeout=20)
                if line is None:
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    break
                yield f"data: {json.dumps({'type': 'line', 'text': line})}\n\n"
            except queue.Empty:
                yield ": ping\n\n"

    return Response(
        stream_with_context(_generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/logs/stop", methods=["POST"])
def stop_logs():
    _log_stop.set()
    return jsonify(ok=True)


# ─── Routes: Port Forwarding ───────────────────────────────────────────────────

@app.route("/api/portforward", methods=["GET"])
def list_portforwards():
    global _portforwards
    dead = [pid for pid, pf in _portforwards.items() if pf["proc"].poll() is not None]
    for pid in dead:
        del _portforwards[pid]
    result = [{"id": pid, "local": pf["local"], "remote": pf["remote"],
               "target": pf["target"], "ns": pf["ns"]}
              for pid, pf in _portforwards.items()]
    return jsonify(ok=True, portforwards=result)


@app.route("/api/portforward", methods=["POST"])
def start_portforward():
    global _pf_id_counter, _portforwards
    d      = request.json or {}
    target = d.get("target", "").strip()
    local  = str(d.get("local", "")).strip()
    remote = str(d.get("remote", "")).strip()
    ns     = d.get("namespace", "default")
    if not all([target, local, remote]):
        return jsonify(ok=False, error="target, local, and remote required"), 400
    args = [KUBECTL_CMD, "port-forward", target, f"{local}:{remote}", "-n", ns]
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, **_win_flags())
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500
    time.sleep(0.6)
    if proc.poll() is not None:
        err = proc.stderr.read()
        return jsonify(ok=False, error=err.strip() or "port-forward failed"), 500
    _pf_id_counter += 1
    pid = str(_pf_id_counter)
    _portforwards[pid] = {"proc": proc, "local": local, "remote": remote,
                          "target": target, "ns": ns}
    return jsonify(ok=True, id=pid, command=" ".join(args))


@app.route("/api/portforward/<pid>", methods=["DELETE"])
def stop_portforward(pid):
    global _portforwards
    pf = _portforwards.pop(pid, None)
    if pf:
        try:
            pf["proc"].terminate()
        except Exception:
            pass
    return jsonify(ok=True)


# ─── Routes: Rollout ───────────────────────────────────────────────────────────

@app.route("/api/rollout/history")
def rollout_history():
    res  = request.args.get("type", "deployments")
    name = request.args.get("name", "")
    ns   = request.args.get("namespace", "default")
    args = [KUBECTL_CMD, "rollout", "history", f"{res}/{name}", "-n", ns]
    stdout, stderr, rc = run_cmd(args, timeout=20)
    out = stdout if rc == 0 else stderr
    return jsonify(ok=(rc == 0), output=out, command=" ".join(args))


@app.route("/api/rollout/undo", methods=["POST"])
def rollout_undo():
    d        = request.json or {}
    res      = d.get("type", "deployments")
    name     = d.get("name", "")
    ns       = d.get("namespace", "default")
    revision = str(d.get("revision", "")).strip()
    args     = [KUBECTL_CMD, "rollout", "undo", f"{res}/{name}", "-n", ns]
    if revision:
        args += [f"--to-revision={revision}"]
    stdout, stderr, rc = run_cmd(args, timeout=30)
    out = stdout if rc == 0 else stderr
    return jsonify(ok=(rc == 0), output=out, command=" ".join(args))


# ─── Routes: ConfigMap / Secret editor ────────────────────────────────────────

@app.route("/api/editor")
def get_editor():
    res  = request.args.get("type", "configmaps")
    name = request.args.get("name", "")
    ns   = request.args.get("namespace", "default")
    stdout, stderr, rc = run_cmd(
        [KUBECTL_CMD, "get", res, name, "-n", ns, "-o", "json"], timeout=15)
    if rc != 0:
        return jsonify(ok=False, error=stderr), 500
    try:
        obj       = json.loads(stdout)
        data      = obj.get("data", {}) or {}
        is_secret = (res == "secrets")
        if is_secret:
            decoded = {}
            for k, v in data.items():
                try:
                    decoded[k] = base64.b64decode(v + "==").decode("utf-8", errors="replace")
                except Exception:
                    decoded[k] = v
            data = decoded
        return jsonify(ok=True, data=data, is_secret=is_secret, name=name, namespace=ns, type=res)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/editor", methods=["POST"])
def save_editor():
    d        = request.json or {}
    res      = d.get("type", "configmaps")
    name     = d.get("name", "")
    ns       = d.get("namespace", "default")
    new_data = d.get("data", {})

    stdout, stderr, rc = run_cmd(
        [KUBECTL_CMD, "get", res, name, "-n", ns, "-o", "json"], timeout=15)
    if rc != 0:
        return jsonify(ok=False, error=stderr), 500
    try:
        obj = json.loads(stdout)
        obj.get("metadata", {}).pop("managedFields", None)
        obj.get("metadata", {}).pop("resourceVersion", None)
        if res == "secrets":
            obj["data"] = {k: base64.b64encode(v.encode()).decode()
                           for k, v in new_data.items()}
        else:
            obj["data"] = new_data
        proc = subprocess.run(
            [KUBECTL_CMD, "apply", "-f", "-"],
            input=json.dumps(obj), capture_output=True, text=True,
            timeout=30, **_win_flags())
        out = proc.stdout if proc.returncode == 0 else proc.stderr
        return jsonify(ok=(proc.returncode == 0), output=out)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# ─── Routes: Health overview ───────────────────────────────────────────────────

@app.route("/api/health")
def health_overview():
    ns     = request.args.get("namespace", "default")
    all_ns = request.args.get("all", "false").lower() == "true"
    ns_f   = ["--all-namespaces"] if all_ns else ["-n", ns]

    stdout, stderr, rc = run_cmd(
        [KUBECTL_CMD, "get", "pods", "-o", "json"] + ns_f, timeout=20)
    if rc != 0:
        return jsonify(ok=False, error=stderr), 500
    # Run kubectl top pods in parallel
    top_out = [None]
    def _fetch_top():
        o, _, r = run_cmd([KUBECTL_CMD, "top", "pods"] + ns_f + ["--no-headers"], timeout=15)
        top_out[0] = o if r == 0 else ""
    top_thread = threading.Thread(target=_fetch_top)
    top_thread.start()
    try:
        pods   = json.loads(stdout).get("items", [])
        # When all-namespaces, restrict to t-55547* namespaces only
        if all_ns:
            pods = [pod for pod in pods if pod.get("metadata", {}).get("namespace", "").startswith("t-55547")]
        counts       = {"running": 0, "pending": 0, "failed": 0,
                        "succeeded": 0, "unknown": 0, "total": len(pods)}
        alerts       = []
        pending_pods = []
        now          = datetime.now(tz=timezone.utc)
        # For pending pod memory stats
        pending_pods_mem = []
        total_pending_mem_bytes = 0
        for pod in pods:
            phase = pod.get("status", {}).get("phase", "Unknown").lower()
            pname = pod["metadata"]["name"]
            pns   = pod["metadata"].get("namespace", ns)
            if   phase == "running":   counts["running"]   += 1
            elif phase == "pending":   counts["pending"]   += 1
            elif phase == "failed":    counts["failed"]    += 1
            elif phase == "succeeded": counts["succeeded"] += 1
            else:                      counts["unknown"]   += 1
            cstatuses = pod.get("status", {}).get("containerStatuses", [])
            for cs in cstatuses:
                reason = cs.get("state", {}).get("waiting", {}).get("reason", "")
                if reason in ("CrashLoopBackOff", "OOMKilled", "Error",
                               "ImagePullBackOff", "ErrImagePull"):
                    alerts.append({"name": pname, "namespace": pns, "reason": reason,
                                   "restarts": cs.get("restartCount", 0),
                                   "container": cs.get("name", "")})
            if phase == "pending":
                sched_reason, sched_msg = "", ""
                for cond in pod.get("status", {}).get("conditions", []):
                    if cond.get("status") == "False":
                        sched_reason = cond.get("reason", "")
                        sched_msg    = cond.get("message", "")[:120]
                        break
                pending_pods.append({"name": pname, "namespace": pns,
                                     "reason":  sched_reason or "Pending",
                                     "message": sched_msg})
                # Sum memory requests/limits for all containers (for pending pods)
                mem_req = 0
                mem_lim = 0
                for ctr in pod.get("spec", {}).get("containers", []):
                    mem_req += _parse_quantity(ctr.get("resources", {}).get("requests", {}).get("memory", "0"))
                    mem_lim += _parse_quantity(ctr.get("resources", {}).get("limits", {}).get("memory", "0"))
                total_pending_mem_bytes += mem_req
                def fmt_mem(b):
                    if b >= 1073741824: return f"{b/1073741824:.1f} Gi"
                    if b >= 1048576: return f"{b/1048576:.0f} Mi"
                    if b > 0: return f"{b/1024:.0f} Ki"
                    return "—"
                pending_pods_mem.append({
                    "name": pname,
                    "namespace": pns,
                    "mem_req": fmt_mem(mem_req),
                    "mem_lim": fmt_mem(mem_lim),
                })
        # Parse kubectl top pods output
        top_thread.join()
        top_pods = []
        for line in (top_out[0] or "").splitlines():
            parts = line.split()
            # all-namespaces: NAMESPACE  NAME  CPU  MEMORY
            # single-ns:      NAME  CPU  MEMORY
            if all_ns and len(parts) >= 4 and parts[0].startswith("t-55547"):
                top_pods.append({"namespace": parts[0], "name": parts[1],
                                 "cpu": parts[2], "memory": parts[3]})
            elif not all_ns and len(parts) >= 3 and ns.startswith("t-55547"):
                top_pods.append({"namespace": ns, "name": parts[0],
                                 "cpu": parts[1], "memory": parts[2]})
        # Format total pending memory
        def fmt_mem(b):
            if b >= 1073741824: return f"{b/1073741824:.1f} Gi"
            if b >= 1048576: return f"{b/1048576:.0f} Mi"
            if b > 0: return f"{b/1024:.0f} Ki"
            return "—"
        total_pending_mem = fmt_mem(total_pending_mem_bytes)
        # Quota snapshot (single-namespace only — skip for all-namespaces view)
        quota_snapshot = []
        if not all_ns:
            try:
                q_out, _, q_rc = run_cmd(
                    [KUBECTL_CMD, "get", "resourcequota", "-n", ns, "-o", "json"], timeout=10)
                if q_rc == 0:
                    for q in json.loads(q_out).get("items", []):
                        quota_snapshot.append({
                            "name": q["metadata"]["name"],
                            "hard": q.get("spec", {}).get("hard", {}),
                            "used": q.get("status", {}).get("used", {}),
                        })
            except Exception:
                pass
        return jsonify(ok=True, counts=counts, alerts=alerts,
                       pending_pods=pending_pods, top_pods=top_pods,
                       pending_pods_mem=pending_pods_mem,
                       total_pending_mem=total_pending_mem,
                       quota_snapshot=quota_snapshot)
    except Exception as e:
        top_thread.join(timeout=1)
        return jsonify(ok=False, error=str(e)), 500


# ─── Routes: Quota Monitor ────────────────────────────────────────────────────
@app.route("/api/quota")
def quota_monitor():
    nss_param   = request.args.get("namespaces", "")
    stale_hours = float(request.args.get("stale_hours", "4"))

    if nss_param:
        namespaces = [n.strip() for n in nss_param.split(",") if n.strip()]
    else:
        cfg        = _load_cfg()
        last_env   = cfg.get("last_environment", "")
        env        = cfg.get("environments", {}).get(last_env, {})
        namespaces = env.get("pinned_namespaces", [])

    if not namespaces:
        return jsonify(ok=True, namespaces=[], stale_jobs=[])

    raw = {}   # ns -> {quota, jobs, error}

    def _fetch(ns):
        q_out,  q_err, q_rc  = run_cmd(
            [KUBECTL_CMD, "get", "resourcequota", "-n", ns, "-o", "json"], timeout=20)
        j_out,  _,     j_rc  = run_cmd(
            [KUBECTL_CMD, "get", "jobs",          "-n", ns, "-o", "json"], timeout=20)
        lr_out, _,     lr_rc = run_cmd(
            [KUBECTL_CMD, "get", "limitrange",    "-n", ns, "-o", "json"], timeout=20)
        pvc_out, _,   pvc_rc = run_cmd(
            [KUBECTL_CMD, "get", "pvc",           "-n", ns, "-o", "json"], timeout=20)
        pod_out, _,   pod_rc = run_cmd(
            [KUBECTL_CMD, "get", "pods",          "-n", ns, "-o", "json", 
             "--field-selector", "status.phase!=Succeeded,status.phase!=Failed"], timeout=20)
        raw[ns] = {
            "quota":      q_out   if q_rc   == 0 else None,
            "jobs":       j_out   if j_rc   == 0 else None,
            "limitrange": lr_out  if lr_rc  == 0 else None,
            "pvcs":       pvc_out if pvc_rc == 0 else None,
            "pods":       pod_out if pod_rc == 0 else None,
            "error":      q_err.strip() if q_rc != 0 else None,
        }

    threads = [threading.Thread(target=_fetch, args=(ns,)) for ns in namespaces]
    for t in threads: t.start()
    for t in threads: t.join()

    ns_data       = []
    stale_jobs    = []
    orphaned_pvcs = []
    now        = datetime.now(tz=timezone.utc)
    stale_secs = stale_hours * 3600

    for ns in namespaces:
        r = raw.get(ns, {})

        # ── Parse ResourceQuota ──────────────────────────────────────────────
        quotas = []
        if r.get("quota"):
            try:
                obj   = json.loads(r["quota"])
                items = obj.get("items", []) if "items" in obj else [obj]
                for q in items:
                    hard      = q.get("status", {}).get("hard", {})
                    used      = q.get("status", {}).get("used", {})
                    resources = [
                        {"resource": k,
                         "used":     used.get(k, "0"),
                         "hard":     hard[k],
                         "pct":      _quota_pct(used.get(k, "0"), hard[k])}
                        for k in sorted(hard.keys())
                    ]
                    quotas.append({
                        "name":      q.get("metadata", {}).get("name", ""),
                        "resources": resources,
                    })
            except Exception:
                pass

        # ── Parse LimitRange ──────────────────────────────────────────────────
        limitranges = []
        if r.get("limitrange"):
            try:
                lr_obj   = json.loads(r["limitrange"])
                lr_items = lr_obj.get("items", []) if "items" in lr_obj else [lr_obj]
                for lr in lr_items:
                    lr_name = lr.get("metadata", {}).get("name", "")
                    parsed  = []
                    for lim in lr.get("spec", {}).get("limits", []):
                        parsed.append({
                            "type":           lim.get("type", ""),
                            "max":            lim.get("max", {}),
                            "min":            lim.get("min", {}),
                            "default":        lim.get("default", {}),
                            "defaultRequest": lim.get("defaultRequest", {}),
                        })
                    limitranges.append({"name": lr_name, "limits": parsed})
            except Exception:
                pass

        ns_data.append({"namespace": ns, "quotas": quotas, "limitranges": limitranges, "error": r.get("error")})

        # ── Parse Jobs ───────────────────────────────────────────────────────
        if r.get("jobs"):
            try:
                jobs = json.loads(r["jobs"]).get("items", [])
                for job in jobs:
                    jname      = job["metadata"]["name"]
                    jns        = job["metadata"].get("namespace", ns)
                    conditions = job.get("status", {}).get("conditions", [])
                    succeeded  = job.get("status", {}).get("succeeded", 0) or 0
                    is_complete = succeeded > 0 or any(
                        c.get("type") == "Complete" and c.get("status") == "True"
                        for c in conditions)
                    is_failed = any(
                        c.get("type") == "Failed" and c.get("status") == "True"
                        for c in conditions)

                    if not is_complete and not is_failed:
                        continue

                    ct_str = (job.get("status", {}).get("completionTime")
                              or job.get("status", {}).get("startTime"))
                    age_secs = None
                    if ct_str:
                        try:
                            ct       = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
                            age_secs = (now - ct).total_seconds()
                        except Exception:
                            pass

                    if is_failed or (is_complete
                                     and age_secs is not None
                                     and age_secs > stale_secs):
                        # Read memory from job's pod template spec — always
                        # present even after pods are deleted
                        mem_bytes = 0
                        for ctr in (job.get("spec", {})
                                        .get("template", {})
                                        .get("spec", {})
                                        .get("containers", [])):
                            mem_str = (ctr.get("resources", {})
                                          .get("requests", {})
                                          .get("memory", "0"))
                            mem_bytes += _parse_quantity(mem_str)
                        if mem_bytes >= 1073741824:
                            mem_human = f"{mem_bytes/1073741824:.1f} Gi"
                        elif mem_bytes >= 1048576:
                            mem_human = f"{mem_bytes/1048576:.0f} Mi"
                        elif mem_bytes > 0:
                            mem_human = f"{mem_bytes/1024:.0f} Ki"
                        else:
                            mem_human = "—"
                        stale_jobs.append({
                            "namespace": jns,
                            "name":      jname,
                            "status":    "Failed" if is_failed else "Complete",
                            "age_secs":  int(age_secs) if age_secs is not None else None,
                            "age_human": _fmt_age(age_secs),
                            "mem_bytes": mem_bytes,
                            "mem_human": mem_human,
                        })
            except Exception:
                pass

    # Pending pods memory stats
    pending_pods_mem = []
    for ns in namespaces:
        r = raw.get(ns, {})
        if not r.get("pods"): continue
        try:
            pods = json.loads(r["pods"]).get("items", [])
            for pod in pods:
                phase = pod.get("status", {}).get("phase", "Unknown").lower()
                if phase != "pending": continue
                pname = pod["metadata"]["name"]
                pns   = pod["metadata"].get("namespace", ns)
                # Sum memory requests/limits for all containers
                mem_req = 0
                mem_lim = 0
                for ctr in pod.get("spec", {}).get("containers", []):
                    mem_req += _parse_quantity(ctr.get("resources", {}).get("requests", {}).get("memory", "0"))
                    mem_lim += _parse_quantity(ctr.get("resources", {}).get("limits", {}).get("memory", "0"))
                def fmt_mem(b):
                    if b >= 1073741824: return f"{b/1073741824:.1f} Gi"
                    if b >= 1048576: return f"{b/1048576:.0f} Mi"
                    if b > 0: return f"{b/1024:.0f} Ki"
                    return "—"
                pending_pods_mem.append({
                    "name": pname,
                    "namespace": pns,
                    "mem_req": fmt_mem(mem_req),
                    "mem_lim": fmt_mem(mem_lim),
                })
        except Exception:
            pass

    return jsonify(ok=True, namespaces=ns_data, stale_jobs=stale_jobs, orphaned_pvcs=orphaned_pvcs, pending_pods_mem=pending_pods_mem)


# ─── Routes: YAML Apply ────────────────────────────────────────────────────────

@app.route("/api/apply", methods=["POST"])
def apply_yaml():
    d         = request.json or {}
    yaml_text = d.get("yaml", "").strip()
    if not yaml_text:
        return jsonify(ok=False, error="yaml required"), 400
    proc = subprocess.run(
        [KUBECTL_CMD, "apply", "-f", "-"],
        input=yaml_text, capture_output=True, text=True,
        timeout=30, **_win_flags())
    out = proc.stdout if proc.returncode == 0 else proc.stderr
    return jsonify(ok=(proc.returncode == 0), output=out, command="kubectl apply -f -")


# ─── Routes: Topology ─────────────────────────────────────────────────────────

@app.route("/api/topology")
def topology():
    ns     = request.args.get("namespace", "default")
    all_ns = request.args.get("all", "false").lower() == "true"
    ns_f   = ["--all-namespaces"] if all_ns else ["-n", ns]

    nodes = {}

    dep_out, _, rc = run_cmd(
        [KUBECTL_CMD, "get", "deployments", "-o", "json"] + ns_f, timeout=20)
    if rc == 0:
        for dep in json.loads(dep_out).get("items", []):
            dname    = dep["metadata"]["name"]
            dns      = dep["metadata"].get("namespace", ns)
            selector = dep.get("spec", {}).get("selector", {}).get("matchLabels", {})
            nodes[f"{dns}/{dname}"] = {
                "kind": "Deployment", "name": dname, "namespace": dns,
                "replicas": dep.get("spec", {}).get("replicas", 0),
                "ready":    dep.get("status", {}).get("readyReplicas", 0),
                "selector": selector, "pods": []
            }

    pod_out, _, rc = run_cmd(
        [KUBECTL_CMD, "get", "pods", "-o", "json"] + ns_f, timeout=20)
    if rc == 0:
        for pod in json.loads(pod_out).get("items", []):
            pname   = pod["metadata"]["name"]
            pns     = pod["metadata"].get("namespace", ns)
            labels  = pod["metadata"].get("labels", {})
            phase   = pod.get("status", {}).get("phase", "Unknown")
            restarts = sum(cs.get("restartCount", 0)
                           for cs in pod.get("status", {}).get("containerStatuses", []))
            matched = False
            for key, dep in nodes.items():
                if dep["namespace"] != pns:
                    continue
                sel = dep.get("selector", {})
                if sel and all(labels.get(k) == v for k, v in sel.items()):
                    dep["pods"].append({"name": pname, "phase": phase, "restarts": restarts})
                    matched = True
                    break
            if not matched:
                key = f"{pns}/__standalone__"
                if key not in nodes:
                    nodes[key] = {"kind": "Standalone", "name": "Standalone Pods",
                                  "namespace": pns, "pods": []}
                nodes[key]["pods"].append({"name": pname, "phase": phase, "restarts": restarts})

    result = list(nodes.values())
    if all_ns:
        result = [n for n in result if n["namespace"].startswith("t-55547")]
    return jsonify(ok=True, topology=result)


# ─── Routes: Pod Exec (terminal) ──────────────────────────────────────────────

@app.route("/api/exec", methods=["POST"])
def pod_exec():
    d         = request.json or {}
    pod       = d.get("pod", "").strip()
    ns        = d.get("namespace", "default")
    container = d.get("container", "")
    cmd       = d.get("cmd", "").strip()
    if not pod or not cmd:
        return jsonify(ok=False, error="pod and cmd required"), 400
    args = [KUBECTL_CMD, "exec", pod, "-n", ns]
    if container:
        args += ["-c", container]
    args += ["--", "sh", "-c", cmd]
    stdout, stderr, rc = run_cmd(args, timeout=30)
    out = stdout if rc == 0 else (stderr or stdout)
    return jsonify(ok=(rc == 0), output=out, command=" ".join(args))


# ─── Routes: HPA ──────────────────────────────────────────────────────────────

@app.route("/api/hpa/patch", methods=["POST"])
def hpa_patch():
    d        = request.json or {}
    name     = d.get("name", "").strip()
    ns       = d.get("namespace", "default")
    min_r    = d.get("min_replicas")
    max_r    = d.get("max_replicas")
    if not name:
        return jsonify(ok=False, error="name required"), 400
    patch = {"spec": {}}
    if min_r is not None:
        patch["spec"]["minReplicas"] = int(min_r)
    if max_r is not None:
        patch["spec"]["maxReplicas"] = int(max_r)
    args = [KUBECTL_CMD, "patch", "hpa", name, "-n", ns,
            "--type=merge", f"--patch={json.dumps(patch)}"]
    stdout, stderr, rc = run_cmd(args, timeout=20)
    out = stdout if rc == 0 else (stderr or stdout)
    return jsonify(ok=(rc == 0), output=out, command=" ".join(args))


@app.route("/api/hpa")
def hpa_list():
    import logging
    ns     = request.args.get("namespace", "default")
    all_ns = request.args.get("all", "false").lower() == "true"
    ns_f   = ["--all-namespaces"] if all_ns else ["-n", ns]
    stdout, stderr, rc = run_cmd(
        [KUBECTL_CMD, "get", "hpa", "-o", "json"] + ns_f, timeout=20)
    if rc != 0:
        logging.error(f"/api/hpa kubectl error: {stderr}")
        return jsonify(ok=False, error=stderr), 500
    try:
        items = json.loads(stdout).get("items", [])
        if items is None:
            items = []
        # When all-namespaces, restrict to t-55547* namespaces only
        if all_ns:
            items = [hpa for hpa in items if hpa.get("metadata", {}).get("namespace", "").startswith("t-55547")]
        result = []
        for hpa in items:
            meta   = hpa.get("metadata", {}) or {}
            spec   = hpa.get("spec", {}) or {}
            status = hpa.get("status", {}) or {}
            ref    = spec.get("scaleTargetRef", {}) or {}
            metrics_info = []
            for m in spec.get("metrics", []) or []:
                mtype = m.get("type", "")
                if mtype == "Resource":
                    res    = m.get("resource", {}) or {}
                    target = res.get("target", {}) or {}
                    ttype  = target.get("type", "")
                    if ttype == "Utilization":
                        metrics_info.append(f"{res.get('name','?')} {target.get('averageUtilization','?')}%")
                    elif ttype == "AverageValue":
                        metrics_info.append(f"{res.get('name','?')} avg={target.get('averageValue','?')}")
                elif mtype in ("External", "Pods", "Object"):
                    metrics_info.append(mtype)
            current_metrics = []
            for m in status.get("currentMetrics", []) or []:
                mtype = m.get("type", "")
                if mtype == "Resource":
                    res = m.get("resource", {}) or {}
                    cur = res.get("current", {}) or {}
                    current_metrics.append(f"{res.get('name','?')} {cur.get('averageUtilization','?')}%")
            result.append({
                "name":             meta.get("name", ""),
                "namespace":        meta.get("namespace", ns),
                "target_kind":      ref.get("kind", ""),
                "target_name":      ref.get("name", ""),
                "min_replicas":     spec.get("minReplicas", 1),
                "max_replicas":     spec.get("maxReplicas", 0),
                "current_replicas": status.get("currentReplicas", 0),
                "desired_replicas": status.get("desiredReplicas", 0),
                "metrics":          ", ".join(metrics_info) if metrics_info else "—",
                "current_metrics":  ", ".join(current_metrics) if current_metrics else "—",
            })
        return jsonify(ok=True, hpa=result)
    except Exception as e:
        logging.error(f"/api/hpa exception: {e}")
        return jsonify(ok=False, error=str(e)), 500


# ─── Routes: Ingress viewer ────────────────────────────────────────────────────

@app.route("/api/ingresses")
def ingresses_detail():
    ns     = request.args.get("namespace", "default")
    all_ns = request.args.get("all", "false").lower() == "true"
    ns_f   = ["--all-namespaces"] if all_ns else ["-n", ns]
    stdout, stderr, rc = run_cmd(
        [KUBECTL_CMD, "get", "ingresses", "-o", "json"] + ns_f, timeout=20)
    if rc != 0:
        return jsonify(ok=False, error=stderr), 500
    try:
        items  = json.loads(stdout).get("items", [])
        result = []
        for ing in items:
            meta   = ing.get("metadata", {})
            spec   = ing.get("spec", {})
            status = ing.get("status", {})
            tls_hosts = set()
            for t in spec.get("tls", []):
                tls_hosts.update(t.get("hosts", []))
            lb_ingress = status.get("loadBalancer", {}).get("ingress", [])
            lb_ip = ", ".join(i.get("ip") or i.get("hostname") or "" for i in lb_ingress) or "—"
            ingress_class = (spec.get("ingressClassName") or
                             meta.get("annotations", {}).get("kubernetes.io/ingress.class") or "—")
            rules = []
            for rule in spec.get("rules", []):
                host = rule.get("host", "*")
                for path in rule.get("http", {}).get("paths", []):
                    be       = path.get("backend", {})
                    svc      = be.get("service", {}) or be
                    svc_name = svc.get("name", "") or be.get("serviceName", "")
                    port_obj = svc.get("port", {})
                    svc_port = (str(port_obj.get("number") or port_obj.get("name", ""))
                                if isinstance(port_obj, dict) else str(be.get("servicePort", "")))
                    rules.append({
                        "host":     host,
                        "path":     path.get("path", "/"),
                        "pathType": path.get("pathType", ""),
                        "service":  svc_name,
                        "port":     svc_port,
                        "tls":      host in tls_hosts,
                    })
            result.append({
                "name":      meta.get("name", ""),
                "namespace": meta.get("namespace", ns),
                "class":     ingress_class,
                "lb_ip":     lb_ip,
                "tls_count": len(spec.get("tls", [])),
                "rules":     rules,
            })
        if all_ns:
            result = [i for i in result if i["namespace"].startswith("t-55547")]
        return jsonify(ok=True, ingresses=result)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# ─── Routes: CRD browser ──────────────────────────────────────────────────────

@app.route("/api/crds")
def list_crds():
    stdout, stderr, rc = run_cmd(
        [KUBECTL_CMD, "get", "crd", "-o", "json"], timeout=30)
    if rc != 0:
        return jsonify(ok=False, error=stderr), 500
    try:
        items  = json.loads(stdout).get("items", [])
        result = []
        for crd in items:
            meta  = crd.get("metadata", {})
            spec  = crd.get("spec", {})
            names = spec.get("names", {})
            served = [v.get("name", "") for v in spec.get("versions", []) if v.get("served")]
            result.append({
                "name":     meta.get("name", ""),
                "group":    spec.get("group", ""),
                "kind":     names.get("kind", ""),
                "plural":   names.get("plural", ""),
                "scope":    spec.get("scope", "Namespaced"),
                "versions": served,
            })
        result.sort(key=lambda x: x["name"])
        return jsonify(ok=True, crds=result)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/crd-instances")
def crd_instances():
    plural = request.args.get("crd", "").strip()
    ns     = request.args.get("namespace", "default")
    all_ns = request.args.get("all", "false").lower() == "true"
    scope  = request.args.get("scope", "Namespaced")
    if not plural:
        return jsonify(ok=False, error="crd required"), 400
    ns_args = [] if scope == "Cluster" else (["--all-namespaces"] if all_ns else ["-n", ns])
    stdout, stderr, rc = run_cmd(
        [KUBECTL_CMD, "get", plural, "-o", "wide"] + ns_args, timeout=25)
    if rc != 0:
        return jsonify(ok=False, error=stderr), 500
    headers, rows = _parse_kubectl_table(stdout)
    if not headers:
        return jsonify(ok=True, headers=[], rows=[])
    return jsonify(ok=True, headers=headers, rows=rows)


# ─── Routes: Jobs / CronJobs panel ────────────────────────────────────────────

@app.route("/api/jobs-panel")
def jobs_panel():
    ns     = request.args.get("namespace", "default")
    all_ns = request.args.get("all", "false").lower() == "true"
    ns_f   = ["--all-namespaces"] if all_ns else ["-n", ns]
    now    = datetime.now(tz=timezone.utc)
    jobs_out, _, jobs_rc = run_cmd([KUBECTL_CMD, "get", "jobs",     "-o", "json"] + ns_f, timeout=20)
    cj_out,   _, cj_rc   = run_cmd([KUBECTL_CMD, "get", "cronjobs", "-o", "json"] + ns_f, timeout=20)
    jobs = []
    if jobs_rc == 0:
        for job in json.loads(jobs_out).get("items", []):
            meta       = job.get("metadata", {})
            spec       = job.get("spec", {})
            status     = job.get("status", {})
            conditions = status.get("conditions", [])
            succeeded  = status.get("succeeded", 0) or 0
            failed_cnt = status.get("failed", 0) or 0
            active     = status.get("active", 0) or 0
            is_complete = succeeded > 0 or any(
                c.get("type") == "Complete" and c.get("status") == "True" for c in conditions)
            is_failed = any(
                c.get("type") == "Failed" and c.get("status") == "True" for c in conditions)
            st = ("Running" if active > 0 else
                  "Complete" if is_complete else
                  "Failed"   if is_failed   else "Pending")
            start_time = status.get("startTime")
            age_secs   = None
            if start_time:
                try:
                    st_dt    = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                    age_secs = (now - st_dt).total_seconds()
                except Exception:
                    pass
            from_cj = next(
                (r.get("name") for r in meta.get("ownerReferences", [])
                 if r.get("kind") == "CronJob"), "")
            jobs.append({
                "name":       meta.get("name", ""),
                "namespace":  meta.get("namespace", ns),
                "status":     st,
                "active":     active,
                "succeeded":  succeeded,
                "failed_cnt": failed_cnt,
                "completions": spec.get("completions", 1),
                "age_secs":   int(age_secs) if age_secs is not None else None,
                "age_human":  _fmt_age(age_secs),
                "cronjob":    from_cj,
            })
    cronjobs = []
    if cj_rc == 0:
        for cj in json.loads(cj_out).get("items", []):
            meta   = cj.get("metadata", {})
            spec   = cj.get("spec", {})
            status = cj.get("status", {})
            last_sched = status.get("lastScheduleTime", "")
            last_secs  = None
            if last_sched:
                try:
                    ls_dt     = datetime.fromisoformat(last_sched.replace("Z", "+00:00"))
                    last_secs = (now - ls_dt).total_seconds()
                except Exception:
                    pass
            cronjobs.append({
                "name":       meta.get("name", ""),
                "namespace":  meta.get("namespace", ns),
                "schedule":   spec.get("schedule", ""),
                "suspend":    spec.get("suspend", False),
                "active":     len(status.get("active", [])),
                "last_sched": _fmt_age(last_secs) if last_secs is not None else "never",
            })
    if all_ns:
        jobs     = [j for j in jobs     if j["namespace"].startswith("t-55547")]
        cronjobs = [c for c in cronjobs if c["namespace"].startswith("t-55547")]
    return jsonify(ok=True, jobs=jobs, cronjobs=cronjobs)


@app.route("/api/cronjob/trigger", methods=["POST"])
def trigger_cronjob():
    d        = request.json or {}
    name     = d.get("name", "").strip()
    ns       = d.get("namespace", "default")
    job_name = d.get("job_name", "").strip()
    if not name:
        return jsonify(ok=False, error="name required"), 400
    if not job_name:
        ts       = datetime.now().strftime("%m%d%H%M")
        job_name = f"{name[:40]}-manual-{ts}"
    args = [KUBECTL_CMD, "create", "job", job_name, f"--from=cronjob/{name}", "-n", ns]
    stdout, stderr, rc = run_cmd(args, timeout=30)
    out = stdout if rc == 0 else stderr
    return jsonify(ok=(rc == 0), output=out, command=" ".join(args))


@app.route("/api/cronjob/suspend", methods=["POST"])
def suspend_cronjob():
    d       = request.json or {}
    name    = d.get("name", "").strip()
    ns      = d.get("namespace", "default")
    suspend = bool(d.get("suspend", True))
    if not name:
        return jsonify(ok=False, error="name required"), 400
    patch = json.dumps({"spec": {"suspend": suspend}})
    args  = [KUBECTL_CMD, "patch", "cronjob", name, "-n", ns,
             "--type=merge", f"--patch={patch}"]
    stdout, stderr, rc = run_cmd(args, timeout=20)
    out = stdout if rc == 0 else (stderr or stdout)
    return jsonify(ok=(rc == 0), output=out, command=" ".join(args))


@app.route("/api/job/pods")
def job_pods():
    """Return the pod names owned by a Job (via controller-uid label selector)."""
    name = request.args.get("name", "").strip()
    ns   = request.args.get("namespace", "default")
    if not name:
        return jsonify(ok=False, pods=[], error="name required"), 400
    # Get job to read its uid
    jout, _, jrc = run_cmd([KUBECTL_CMD, "get", "job", name, "-n", ns, "-o", "json"], timeout=10)
    if jrc != 0:
        return jsonify(ok=False, pods=[], error=f"Job not found: {name}")
    try:
        job_uid = json.loads(jout)["metadata"]["uid"]
    except Exception:
        return jsonify(ok=False, pods=[], error="Could not read job UID")
    # List pods with matching controller-uid label
    pout, _, prc = run_cmd([
        KUBECTL_CMD, "get", "pods", "-n", ns,
        "-l", f"controller-uid={job_uid}",
        "-o", "jsonpath={.items[*].metadata.name}"], timeout=10)
    pods = pout.strip().split() if prc == 0 and pout.strip() else []
    return jsonify(ok=True, pods=pods)


# ─── Routes: Config ────────────────────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
def get_config():
    cfg  = _load_cfg()
    envs = cfg.get("environments", {})
    # If a manual password entry exists in the JSON, decode and return it so
    # the login form can pre-fill.  If absent (the normal case), return empty.
    safe_envs = {
        name: {k: (_decode_pw(v) if k == "password" else v) for k, v in env.items()}
        for name, env in envs.items()
    }
    return jsonify(environments=safe_envs,
                   last_environment=cfg.get("last_environment", ""))


@app.route("/api/config", methods=["POST"])
def save_config():
    d    = request.json or {}
    name = d.get("name", "").strip()
    if not name:
        return jsonify(ok=False, error="Name required."), 400
    cfg  = _load_cfg()
    envs = cfg.setdefault("environments", {})
    existing      = envs.get(name, {})
    existing_pins = existing.get("pinned_namespaces", [])
    # Password is NEVER written by the app — only preserved if someone
    # manually added it to the JSON file (e.g. for convenience on a personal
    # machine).  The app always requires the user to type it on login.
    existing_pw   = existing.get("password", "")
    envs[name] = {
        "ske_url":            d.get("ske_url", ""),
        "auth_url":           d.get("auth_url", ""),
        "username":           d.get("username", ""),
        "password":           existing_pw,   # keep manual entry; never overwrite
        "skectl_path":        d.get("skectl_path", SKECTL_CMD),
        "pinned_namespaces":  d.get("pinned_namespaces", existing_pins),
    }
    cfg["last_environment"] = name
    _save_cfg(cfg)
    return jsonify(ok=True)


@app.route("/api/config/last", methods=["POST"])
def set_last_environment():
    """Persist which environment was most recently used for login."""
    d    = request.json or {}
    name = d.get("name", "").strip()
    if not name:
        return jsonify(ok=False, error="Name required."), 400
    cfg  = _load_cfg()
    if name in cfg.get("environments", {}):
        cfg["last_environment"] = name
        _save_cfg(cfg)
    return jsonify(ok=True)

@app.route("/api/config/<name>", methods=["DELETE"])
def delete_config(name):
    cfg = _load_cfg()
    cfg.get("environments", {}).pop(name, None)
    if cfg.get("last_environment") == name:
        cfg["last_environment"] = ""
    _save_cfg(cfg)
    return jsonify(ok=True)


@app.route("/api/config/<name>/namespaces", methods=["POST"])
def pin_namespace(name):
    d  = request.json or {}
    ns = d.get("namespace", "").strip()
    if not ns:
        return jsonify(ok=False, error="namespace required"), 400
    cfg  = _load_cfg()
    env  = cfg.setdefault("environments", {}).setdefault(name, {})
    pins = env.setdefault("pinned_namespaces", [])
    if ns not in pins:
        pins.append(ns)
    _save_cfg(cfg)
    return jsonify(ok=True, pinned_namespaces=pins)


@app.route("/api/config/<name>/namespaces/<ns>", methods=["DELETE"])
def unpin_namespace(name, ns):
    cfg  = _load_cfg()
    env  = cfg.get("environments", {}).get(name, {})
    pins = [p for p in env.get("pinned_namespaces", []) if p != ns]
    env["pinned_namespaces"] = pins
    _save_cfg(cfg)
    return jsonify(ok=True, pinned_namespaces=pins)


# ─── Routes: Saved shell commands ──────────────────────────────────────────────

@app.route("/api/commands", methods=["GET"])
def list_commands():
    cfg = _load_cfg()
    return jsonify(ok=True, commands=cfg.get("saved_commands", []))

@app.route("/api/commands", methods=["POST"])
def save_command():
    d       = request.json or {}
    label   = d.get("label", "").strip()
    command = d.get("command", "").strip()
    if not label or not command:
        return jsonify(ok=False, error="Label and command are required."), 400
    cfg  = _load_cfg()
    cmds = cfg.setdefault("saved_commands", [])
    cmds.append({"label": label, "command": command})
    _save_cfg(cfg)
    return jsonify(ok=True, commands=cmds)

@app.route("/api/commands/<int:index>", methods=["PUT"])
def update_command(index):
    d       = request.json or {}
    label   = d.get("label", "").strip()
    command = d.get("command", "").strip()
    if not label or not command:
        return jsonify(ok=False, error="Label and command are required."), 400
    cfg  = _load_cfg()
    cmds = cfg.get("saved_commands", [])
    if not (0 <= index < len(cmds)):
        return jsonify(ok=False, error="Index out of range."), 404
    cmds[index] = {"label": label, "command": command}
    _save_cfg(cfg)
    return jsonify(ok=True, commands=cmds)

@app.route("/api/commands/<int:index>", methods=["DELETE"])
def delete_command(index):
    cfg  = _load_cfg()
    cmds = cfg.get("saved_commands", [])
    if 0 <= index < len(cmds):
        cmds.pop(index)
        _save_cfg(cfg)
    return jsonify(ok=True, commands=cmds)


# ─── Routes: Triage ────────────────────────────────────────────────────────────

@app.route("/api/triage/events")
def triage_events():
    """Warning events across all or selected namespace, newest first."""
    ns     = request.args.get("namespace", "")
    all_ns = request.args.get("all", "true").lower() == "true"
    ns_f   = ["--all-namespaces"] if all_ns else ["-n", ns]
    out, err, rc = run_cmd(
        [KUBECTL_CMD, "get", "events", "-o", "json",
         "--field-selector", "type=Warning"] + ns_f, timeout=20)
    if rc != 0:
        return jsonify(ok=False, error=err.strip())
    try:
        items = json.loads(out).get("items", [])
        events = []
        for e in items:
            lt = e.get("lastTimestamp") or e.get("eventTime") or ""
            events.append({
                "namespace":  e["metadata"].get("namespace", ""),
                "name":       e["metadata"].get("name", ""),
                "reason":     e.get("reason", ""),
                "message":    e.get("message", "")[:200],
                "object":     e.get("involvedObject", {}).get("name", ""),
                "kind":       e.get("involvedObject", {}).get("kind", ""),
                "count":      e.get("count", 1),
                "last_time":  lt,
            })
        events.sort(key=lambda x: x["last_time"], reverse=True)
        return jsonify(ok=True, events=events[:200])
    except Exception as ex:
        return jsonify(ok=False, error=str(ex))


@app.route("/api/triage/deployments")
def triage_deployments():
    """Deployments with unavailable or mismatched replicas."""
    ns     = request.args.get("namespace", "")
    all_ns = request.args.get("all", "true").lower() == "true"
    ns_f   = ["--all-namespaces"] if all_ns else ["-n", ns]
    out, err, rc = run_cmd(
        [KUBECTL_CMD, "get", "deployments", "-o", "json"] + ns_f, timeout=20)
    if rc != 0:
        return jsonify(ok=False, error=err.strip())
    try:
        items = json.loads(out).get("items", [])
        unhealthy = []
        for d in items:
            spec   = d.get("spec", {})
            status = d.get("status", {})
            desired    = spec.get("replicas", 1)
            ready      = status.get("readyReplicas", 0)
            available  = status.get("availableReplicas", 0)
            updated    = status.get("updatedReplicas", 0)
            if ready != desired or available != desired or updated != desired:
                conds = [c for c in status.get("conditions", []) if c.get("status") != "True"]
                unhealthy.append({
                    "namespace": d["metadata"].get("namespace", ""),
                    "name":      d["metadata"]["name"],
                    "desired":   desired,
                    "ready":     ready,
                    "available": available,
                    "updated":   updated,
                    "reason":    conds[0].get("reason", "") if conds else "",
                    "message":   conds[0].get("message", "")[:150] if conds else "",
                })
        return jsonify(ok=True, deployments=unhealthy)
    except Exception as ex:
        return jsonify(ok=False, error=str(ex))


@app.route("/api/triage/oomkills")
def triage_oomkills():
    """Pods with OOMKilled containers in last N hours."""
    ns     = request.args.get("namespace", "")
    all_ns = request.args.get("all", "true").lower() == "true"
    hours  = int(request.args.get("hours", "24"))
    ns_f   = ["--all-namespaces"] if all_ns else ["-n", ns]
    out, err, rc = run_cmd(
        [KUBECTL_CMD, "get", "pods", "-o", "json"] + ns_f, timeout=20)
    if rc != 0:
        return jsonify(ok=False, error=err.strip())
    try:
        items  = json.loads(out).get("items", [])
        now    = datetime.now(tz=timezone.utc)
        cutoff = now.timestamp() - hours * 3600
        oomkills = []
        for pod in items:
            pname = pod["metadata"]["name"]
            pns   = pod["metadata"].get("namespace", "")
            for cs in (pod.get("status", {}).get("containerStatuses", []) +
                       pod.get("status", {}).get("initContainerStatuses", [])):
                for state_key in ("lastState", "state"):
                    term = cs.get(state_key, {}).get("terminated", {})
                    if term.get("reason") == "OOMKilled":
                        finished = term.get("finishedAt", "")
                        ts = 0
                        try:
                            ts = datetime.fromisoformat(
                                finished.replace("Z", "+00:00")).timestamp()
                        except Exception:
                            pass
                        if ts >= cutoff:
                            oomkills.append({
                                "namespace":    pns,
                                "pod":          pname,
                                "container":    cs.get("name", ""),
                                "finished_at":  finished,
                                "exit_code":    term.get("exitCode", ""),
                            })
        oomkills.sort(key=lambda x: x["finished_at"], reverse=True)
        return jsonify(ok=True, oomkills=oomkills)
    except Exception as ex:
        return jsonify(ok=False, error=str(ex))


@app.route("/api/triage/nodes")
def triage_nodes():
    """Node conditions (pressure/not-ready) + top nodes."""
    nodes_out, nerr, nrc = run_cmd(
        [KUBECTL_CMD, "get", "nodes", "-o", "json"], timeout=20)
    top_out,   _,    _   = run_cmd(
        [KUBECTL_CMD, "top", "nodes", "--no-headers"], timeout=15)
    if nrc != 0:
        return jsonify(ok=False, error=nerr.strip())
    try:
        # Parse top nodes
        top = {}
        for line in top_out.splitlines():
            parts = line.split()
            if len(parts) >= 5:
                top[parts[0]] = {"cpu": parts[1], "cpu_pct": parts[2],
                                 "mem": parts[3], "mem_pct": parts[4]}
        nodes = []
        for n in json.loads(nodes_out).get("items", []):
            name   = n["metadata"]["name"]
            conds  = {c["type"]: c for c in n.get("status", {}).get("conditions", [])}
            issues = [k for k, v in conds.items()
                      if (k == "Ready" and v.get("status") != "True") or
                         (k != "Ready" and v.get("status") == "True")]
            t = top.get(name, {})
            nodes.append({
                "name":     name,
                "ready":    conds.get("Ready", {}).get("status", "Unknown") == "True",
                "issues":   issues,
                "cpu":      t.get("cpu", "—"),
                "cpu_pct":  t.get("cpu_pct", "—"),
                "mem":      t.get("mem", "—"),
                "mem_pct":  t.get("mem_pct", "—"),
                "roles":    ",".join(
                    k.replace("node-role.kubernetes.io/", "")
                    for k in n["metadata"].get("labels", {})
                    if "node-role.kubernetes.io/" in k) or "worker",
            })
        return jsonify(ok=True, nodes=nodes)
    except Exception as ex:
        return jsonify(ok=False, error=str(ex))


@app.route("/api/triage/pod-deps")
def triage_pod_deps():
    """Check whether all ConfigMaps, Secrets and PVCs referenced by a pod exist."""
    name = request.args.get("name", "").strip()
    ns   = request.args.get("namespace", "default").strip()
    if not name:
        return jsonify(ok=False, error="Pod name required"), 400
    pod_out, err, rc = run_cmd(
        [KUBECTL_CMD, "get", "pod", name, "-n", ns, "-o", "json"], timeout=10)
    if rc != 0:
        return jsonify(ok=False, error=err.strip())
    try:
        pod  = json.loads(pod_out)
        spec = pod.get("spec", {})
        refs = {"ConfigMap": set(), "Secret": set(), "PersistentVolumeClaim": set()}
        # Volumes
        for vol in spec.get("volumes", []):
            if "configMap" in vol:
                refs["ConfigMap"].add(vol["configMap"]["name"])
            if "secret" in vol:
                refs["Secret"].add(vol["secret"]["secretName"])
            if "persistentVolumeClaim" in vol:
                refs["PersistentVolumeClaim"].add(vol["persistentVolumeClaim"]["claimName"])
        # Env / envFrom
        for ctr in spec.get("containers", []) + spec.get("initContainers", []):
            for ef in ctr.get("envFrom", []):
                if "configMapRef" in ef:  refs["ConfigMap"].add(ef["configMapRef"]["name"])
                if "secretRef"    in ef:  refs["Secret"].add(ef["secretRef"]["name"])
            for env in ctr.get("env", []):
                vfrom = env.get("valueFrom", {})
                if "configMapKeyRef" in vfrom: refs["ConfigMap"].add(vfrom["configMapKeyRef"]["name"])
                if "secretKeyRef"    in vfrom: refs["Secret"].add(vfrom["secretKeyRef"]["name"])
        # Check existence
        results = []
        kind_cmd = {"ConfigMap": "configmap", "Secret": "secret",
                    "PersistentVolumeClaim": "pvc"}
        for kind, names in refs.items():
            for rname in sorted(names):
                _, _, erc = run_cmd(
                    [KUBECTL_CMD, "get", kind_cmd[kind], rname, "-n", ns], timeout=8)
                results.append({"kind": kind, "name": rname, "exists": erc == 0})
        return jsonify(ok=True, pod=name, namespace=ns, deps=results)
    except Exception as ex:
        return jsonify(ok=False, error=str(ex))


@app.route("/api/triage/limits")
def triage_limits():
    """Pods missing CPU or memory limits/requests."""
    ns     = request.args.get("namespace", "")
    all_ns = request.args.get("all", "true").lower() == "true"
    ns_f   = ["--all-namespaces"] if all_ns else ["-n", ns]
    out, err, rc = run_cmd(
        [KUBECTL_CMD, "get", "pods", "-o", "json"] + ns_f, timeout=20)
    if rc != 0:
        return jsonify(ok=False, error=err.strip())
    try:
        items = json.loads(out).get("items", [])
        missing = []
        for pod in items:
            pname = pod["metadata"]["name"]
            pns   = pod["metadata"].get("namespace", "")
            phase = pod.get("status", {}).get("phase", "")
            if phase in ("Succeeded", "Failed"):
                continue
            for ctr in pod.get("spec", {}).get("containers", []):
                res = ctr.get("resources", {})
                lim = res.get("limits", {})
                req = res.get("requests", {})
                issues = []
                if not lim.get("cpu"):    issues.append("no CPU limit")
                if not lim.get("memory"): issues.append("no memory limit")
                if not req.get("cpu"):    issues.append("no CPU request")
                if not req.get("memory"): issues.append("no memory request")
                if issues:
                    missing.append({
                        "namespace": pns, "pod": pname,
                        "container": ctr["name"],
                        "issues":    ", ".join(issues),
                        "cpu_req":   req.get("cpu", "—"),
                        "mem_req":   req.get("memory", "—"),
                        "cpu_lim":   lim.get("cpu", "—"),
                        "mem_lim":   lim.get("memory", "—"),
                    })
        return jsonify(ok=True, missing=missing)
    except Exception as ex:
        return jsonify(ok=False, error=str(ex))


@app.route("/api/triage/pvcs")
def triage_pvcs():
    """PVCs that are Pending or Lost."""
    ns     = request.args.get("namespace", "")
    all_ns = request.args.get("all", "true").lower() == "true"
    ns_f   = ["--all-namespaces"] if all_ns else ["-n", ns]
    out, err, rc = run_cmd(
        [KUBECTL_CMD, "get", "pvc", "-o", "json"] + ns_f, timeout=20)
    if rc != 0:
        return jsonify(ok=False, error=err.strip())
    try:
        items = json.loads(out).get("items", [])
        bad   = []
        for p in items:
            phase = p.get("status", {}).get("phase", "")
            if phase in ("Pending", "Lost"):
                bad.append({
                    "namespace":    p["metadata"].get("namespace", ""),
                    "name":         p["metadata"]["name"],
                    "phase":        phase,
                    "storage_class": p["spec"].get("storageClassName", "—"),
                    "capacity":     p["spec"].get("resources", {}).get("requests", {}).get("storage", "—"),
                    "volume":       p["spec"].get("volumeName", "—"),
                })
        return jsonify(ok=True, pvcs=bad)
    except Exception as ex:
        return jsonify(ok=False, error=str(ex))


@app.route("/api/triage/tls-expiry")
def triage_tls_expiry():
    """TLS secrets with expiry dates."""
    ns     = request.args.get("namespace", "")
    all_ns = request.args.get("all", "true").lower() == "true"
    ns_f   = ["--all-namespaces"] if all_ns else ["-n", ns]
    out, err, rc = run_cmd(
        [KUBECTL_CMD, "get", "secrets", "-o", "json",
         "--field-selector", "type=kubernetes.io/tls"] + ns_f, timeout=20)
    if rc != 0:
        return jsonify(ok=False, error=err.strip())

    def _cert_expiry(pem_data: bytes):
        """Return expiry datetime from PEM cert bytes. Tries multiple methods."""
        # Method 1: cryptography library (available as a Flask/pyOpenSSL dep)
        try:
            from cryptography import x509 as _x509
            cert = _x509.load_pem_x509_certificate(pem_data)
            # not_valid_after_utc is cryptography >= 42; fall back to not_valid_after
            try:
                return cert.not_valid_after_utc
            except AttributeError:
                import pytz as _pytz
                return cert.not_valid_after.replace(tzinfo=timezone.utc)
        except Exception:
            pass
        # Method 2: pyOpenSSL
        try:
            from OpenSSL import crypto as _crypto
            c = _crypto.load_certificate(_crypto.FILETYPE_PEM, pem_data)
            raw = c.get_notAfter().decode()  # b'20261231235959Z'
            return datetime.strptime(raw, "%Y%m%d%H%M%S%z")
        except Exception:
            pass
        # Method 3: openssl subprocess (may not exist on Windows)
        try:
            import subprocess as _sp, tempfile as _tmp, os as _os
            with _tmp.NamedTemporaryFile(delete=False, suffix=".pem") as tf:
                tf.write(pem_data); tfn = tf.name
            r = _sp.run(["openssl", "x509", "-enddate", "-noout", "-in", tfn],
                        capture_output=True, text=True)
            _os.unlink(tfn)
            if r.returncode == 0:
                raw = r.stdout.strip().split("=", 1)[-1]
                return datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        except Exception:
            pass
        # Method 4: scan raw ASN.1 for UTCTime/GeneralizedTime tags (no deps)
        try:
            import base64 as _b64
            # Strip PEM header/footer and decode to DER
            b64 = b"".join(
                ln for ln in pem_data.splitlines()
                if ln and not ln.startswith(b"-----"))
            der = _b64.b64decode(b64)
            times = []
            i = 0
            while i < len(der) - 2:
                tag = der[i]
                if tag in (0x17, 0x18):
                    ln = der[i + 1]
                    val = der[i + 2: i + 2 + ln].decode("ascii", errors="ignore")
                    try:
                        if tag == 0x17:   # UTCTime YYMMDDHHMMSSZ
                            yr = int(val[:2])
                            yr = 2000 + yr if yr < 50 else 1900 + yr
                            val = str(yr) + val[2:]
                        times.append(
                            datetime.strptime(val[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc))
                    except Exception:
                        pass
                i += 1
            # X.509 Validity has notBefore then notAfter — we want the 2nd one
            if len(times) >= 2:
                return times[1]
        except Exception:
            pass
        return None

    try:
        import base64 as _b64
        items = json.loads(out).get("items", [])
        certs = []
        now   = datetime.now(tz=timezone.utc)
        for s in items:
            crt_b64 = s.get("data", {}).get("tls.crt", "")
            sname   = s["metadata"]["name"]
            sns     = s["metadata"].get("namespace", "")
            expiry_str = "—"
            days_left  = None
            if crt_b64:
                try:
                    pem = _b64.b64decode(crt_b64)
                    exp = _cert_expiry(pem)
                    if exp is not None:
                        days_left  = (exp - now).days
                        expiry_str = exp.strftime("%Y-%m-%d")
                except Exception:
                    pass
            certs.append({
                "namespace": sns, "name": sname,
                "expiry": expiry_str, "days_left": days_left,
            })
        certs.sort(key=lambda x: (x["days_left"] if x["days_left"] is not None else 9999))
        return jsonify(ok=True, certs=certs)
    except Exception as ex:
        return jsonify(ok=False, error=str(ex))


@app.route("/api/triage/webhooks")
def triage_webhooks():
    """MutatingWebhookConfigurations and ValidatingWebhookConfigurations."""
    mut_out, _, _   = run_cmd(
        [KUBECTL_CMD, "get", "mutatingwebhookconfigurations", "-o", "json"], timeout=15)
    val_out, verr, vrc = run_cmd(
        [KUBECTL_CMD, "get", "validatingwebhookconfigurations", "-o", "json"], timeout=15)
    try:
        webhooks = []
        for kind, raw in [("Mutating", mut_out), ("Validating", val_out)]:
            try:
                for wh in json.loads(raw).get("items", []):
                    wname = wh["metadata"]["name"]
                    for hook in wh.get("webhooks", []):
                        svc = hook.get("clientConfig", {}).get("service", {})
                        webhooks.append({
                            "kind":            kind,
                            "config_name":     wname,
                            "webhook_name":    hook.get("name", ""),
                            "namespace":       svc.get("namespace", "—"),
                            "service":         svc.get("name", "—"),
                            "failure_policy":  hook.get("failurePolicy", "—"),
                            "rules":           len(hook.get("rules", [])),
                        })
            except Exception:
                pass
        return jsonify(ok=True, webhooks=webhooks)
    except Exception as ex:
        return jsonify(ok=False, error=str(ex))

# ─── Routes: Network Policies ─────────────────────────────────────────────────

@app.route("/api/netpolicies")
def netpolicies():
    ns     = request.args.get("namespace", "default")
    all_ns = request.args.get("all", "false").lower() == "true"
    ns_f   = ["--all-namespaces"] if all_ns else ["-n", ns]
    stdout, stderr, rc = run_cmd(
        [KUBECTL_CMD, "get", "networkpolicies", "-o", "json"] + ns_f, timeout=20)
    if rc != 0:
        return jsonify(ok=False, error=stderr), 500
    try:
        items = json.loads(stdout).get("items", [])
        if all_ns:
            items = [i for i in items
                     if i.get("metadata", {}).get("namespace", "").startswith("t-55547")]

        def _peer(p):
            parts = []
            if "podSelector" in p:
                lbls = (p["podSelector"] or {}).get("matchLabels", {})
                parts.append("pod:" + (", ".join(f"{k}={v}" for k, v in lbls.items()) or "any"))
            if "namespaceSelector" in p:
                lbls = (p["namespaceSelector"] or {}).get("matchLabels", {})
                parts.append("ns:" + (", ".join(f"{k}={v}" for k, v in lbls.items()) or "any"))
            if "ipBlock" in p:
                parts.append("ip:" + (p["ipBlock"] or {}).get("cidr", "?"))
            return " + ".join(parts) if parts else "any"

        result = []
        for np in items:
            meta = np.get("metadata", {})
            spec = np.get("spec", {})
            ps   = spec.get("podSelector") or {}
            ps_labels = ps.get("matchLabels", {})
            ps_str = ", ".join(f"{k}={v}" for k, v in ps_labels.items()) if ps_labels else "(all pods)"
            ingress_rules = []
            for rule in (spec.get("ingress") or []):
                froms = [_peer(p) for p in (rule.get("from") or [])] or ["any"]
                ports = [f"{p.get('protocol','TCP')}/{p.get('port','*')}"
                         for p in (rule.get("ports") or [])] or ["all ports"]
                ingress_rules.append({"from": froms, "ports": ports})
            egress_rules = []
            for rule in (spec.get("egress") or []):
                tos   = [_peer(p) for p in (rule.get("to") or [])] or ["any"]
                ports = [f"{p.get('protocol','TCP')}/{p.get('port','*')}"
                         for p in (rule.get("ports") or [])] or ["all ports"]
                egress_rules.append({"to": tos, "ports": ports})
            result.append({
                "name":          meta.get("name", ""),
                "namespace":     meta.get("namespace", ns),
                "pod_selector":  ps_str,
                "policy_types":  spec.get("policyTypes", []),
                "ingress_rules": ingress_rules,
                "egress_rules":  egress_rules,
            })
        return jsonify(ok=True, policies=result)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# ─── Routes: Compliance Checker ────────────────────────────────────────────────

@app.route("/api/compliance")
def compliance_check():
    ns = request.args.get("namespace", "default")
    checks = []

    def _chk(name, category, severity, description, remediation, passed, detail=""):
        checks.append({"name": name, "category": category, "severity": severity,
                        "description": description, "remediation": remediation,
                        "passed": passed, "detail": detail})

    # ── Governance ────────────────────────────────────────────────────────────
    out, _, rc = run_cmd([KUBECTL_CMD, "get", "resourcequota", "-n", ns, "-o", "json"], timeout=10)
    if rc == 0:
        items = json.loads(out).get("items", [])
        _chk("Resource Quota", "Governance", "high",
             "Namespace has a ResourceQuota to prevent resource starvation",
             f"kubectl create resourcequota default --hard=cpu=4,memory=8Gi,pods=20 -n {ns}",
             bool(items),
             f"{len(items)} quota(s): {', '.join(i['metadata']['name'] for i in items)}" if items
             else "No ResourceQuota found")

    out, _, rc = run_cmd([KUBECTL_CMD, "get", "limitrange", "-n", ns, "-o", "json"], timeout=10)
    if rc == 0:
        items = json.loads(out).get("items", [])
        _chk("Limit Range", "Governance", "medium",
             "Namespace has a LimitRange to set default resource requests/limits",
             "Create a LimitRange with sensible default CPU/memory requests and limits",
             bool(items),
             f"{len(items)} limitrange(s): {', '.join(i['metadata']['name'] for i in items)}" if items
             else "No LimitRange found")

    # ── Security ──────────────────────────────────────────────────────────────
    out, _, rc = run_cmd([KUBECTL_CMD, "get", "networkpolicy", "-n", ns, "-o", "json"], timeout=10)
    if rc == 0:
        items = json.loads(out).get("items", [])
        _chk("Network Policy", "Security", "high",
             "Namespace has NetworkPolicy to restrict pod-to-pod traffic",
             "Create a default-deny NetworkPolicy and add explicit allow rules",
             bool(items),
             f"{len(items)} policy(s): {', '.join(i['metadata']['name'] for i in items)}" if items
             else "No NetworkPolicy found")

    # ── Pod-level checks ──────────────────────────────────────────────────────
    pod_out, _, pod_rc = run_cmd([KUBECTL_CMD, "get", "pods", "-n", ns, "-o", "json"], timeout=15)
    if pod_rc == 0:
        pods = [p for p in json.loads(pod_out).get("items", [])
                if p.get("status", {}).get("phase") not in ("Succeeded", "Failed")]

        latest_ctrs, no_req_ctrs, no_lim_ctrs = [], [], []
        root_ctrs, priv_ctrs = [], []
        no_live_pods, no_ready_pods = [], []

        for pod in pods:
            pname = pod["metadata"]["name"]
            spec  = pod.get("spec", {})
            all_ctrs = (spec.get("containers") or []) + (spec.get("initContainers") or [])
            if not any(c.get("livenessProbe")  for c in spec.get("containers") or []):
                no_live_pods.append(pname)
            if not any(c.get("readinessProbe") for c in spec.get("containers") or []):
                no_ready_pods.append(pname)
            for c in all_ctrs:
                cref = f"{pname}/{c['name']}"
                img  = c.get("image", "")
                tag  = img.split(":")[-1] if ":" in img.split("/")[-1] else ""
                if not tag or tag == "latest":
                    latest_ctrs.append(cref)
                res = c.get("resources") or {}
                if not (res.get("requests") or {}).get("cpu"):
                    no_req_ctrs.append(cref)
                if not (res.get("limits") or {}).get("memory"):
                    no_lim_ctrs.append(cref)
                sc = c.get("securityContext") or {}
                if sc.get("runAsUser") == 0 or sc.get("runAsNonRoot") is False:
                    root_ctrs.append(cref)
                if sc.get("privileged"):
                    priv_ctrs.append(cref)

        def _fmt(lst):
            s = ", ".join(lst[:3])
            return s + (f" (+{len(lst)-3} more)" if len(lst) > 3 else "")

        total = len(pods)
        _chk("No Latest Image Tags", "Security", "medium",
             "No containers use :latest or untagged images (unpredictable deployments)",
             "Pin all images to a specific version tag, e.g. image:1.2.3",
             not latest_ctrs,
             f"{len(latest_ctrs)} container(s) untagged/latest: {_fmt(latest_ctrs)}" if latest_ctrs
             else f"All containers across {total} pod(s) use pinned tags")

        _chk("Resource Requests Defined", "Reliability", "high",
             "All containers have CPU/memory requests so the scheduler places pods correctly",
             "Add resources.requests.cpu and resources.requests.memory to every container",
             not no_req_ctrs,
             f"{len(no_req_ctrs)} container(s) missing CPU requests: {_fmt(no_req_ctrs)}" if no_req_ctrs
             else "All containers have requests defined")

        _chk("Resource Limits Defined", "Reliability", "high",
             "All containers have resource limits to prevent runaway memory consumption",
             "Add resources.limits.cpu and resources.limits.memory to every container",
             not no_lim_ctrs,
             f"{len(no_lim_ctrs)} container(s) missing memory limits: {_fmt(no_lim_ctrs)}" if no_lim_ctrs
             else "All containers have limits defined")

        _chk("No Root Containers", "Security", "critical",
             "No containers run as UID 0 (root), which escalates blast radius of a breach",
             "Set securityContext.runAsNonRoot: true or runAsUser to a non-zero UID",
             not root_ctrs,
             f"{len(root_ctrs)} container(s) running as root: {_fmt(root_ctrs)}" if root_ctrs
             else "No root containers detected")

        _chk("No Privileged Containers", "Security", "critical",
             "No containers have securityContext.privileged: true",
             "Remove privileged: true — use specific capabilities (securityContext.capabilities.add) instead",
             not priv_ctrs,
             f"{len(priv_ctrs)} privileged container(s): {_fmt(priv_ctrs)}" if priv_ctrs
             else "No privileged containers detected")

        _chk("Liveness Probes", "Reliability", "medium",
             "All pods have a livenessProbe so Kubernetes restarts hung containers automatically",
             "Add livenessProbe (httpGet, exec, or tcpSocket) to each container spec",
             not no_live_pods,
             f"{len(no_live_pods)} pod(s) missing liveness probe: {_fmt(no_live_pods)}" if no_live_pods
             else "All pods have liveness probes")

        _chk("Readiness Probes", "Reliability", "medium",
             "All pods have a readinessProbe so traffic is only sent to ready pods",
             "Add readinessProbe to each container spec",
             not no_ready_pods,
             f"{len(no_ready_pods)} pod(s) missing readiness probe: {_fmt(no_ready_pods)}" if no_ready_pods
             else "All pods have readiness probes")

    # ── PodDisruptionBudget ───────────────────────────────────────────────────
    out, _, rc = run_cmd([KUBECTL_CMD, "get", "pdb", "-n", ns, "-o", "json"], timeout=10)
    if rc == 0:
        items = json.loads(out).get("items", [])
        _chk("Pod Disruption Budget", "Reliability", "medium",
             "Production workloads have PodDisruptionBudgets to survive node drains/maintenance",
             f"kubectl create poddisruptionbudget <name> --min-available=1 -n {ns}",
             bool(items),
             f"{len(items)} PDB(s): {', '.join(i['metadata']['name'] for i in items)}" if items
             else "No PodDisruptionBudget found")

    # ── Stale failed jobs ─────────────────────────────────────────────────────
    out, _, rc = run_cmd([KUBECTL_CMD, "get", "jobs", "-n", ns, "-o", "json"], timeout=10)
    if rc == 0:
        failed = [j["metadata"]["name"] for j in json.loads(out).get("items", [])
                  if any(c.get("type") == "Failed" and c.get("status") == "True"
                         for c in j.get("status", {}).get("conditions", []))]
        _chk("No Failed Jobs", "Operations", "low",
             "No failed jobs are lingering in the namespace",
             f"kubectl delete jobs --field-selector status.successful=0 -n {ns}",
             not failed,
             f"{len(failed)} failed job(s): {', '.join(failed[:5])}" if failed else "No failed jobs")

    passed = sum(1 for c in checks if c["passed"])
    total  = len(checks)
    score  = round(passed / total * 100) if total else 0
    return jsonify(ok=True, checks=checks, passed=passed, total=total, score=score, namespace=ns)


def _pty_set_size(fd, rows, cols):
    """Resize a Unix PTY to the given dimensions."""
    try:
        import fcntl, termios
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except Exception:
        pass


@sock.route("/ws/shell")
def shell_ws(ws):
    """Full interactive shell over WebSocket.
    Input protocol:
        !resize:ROWS:COLS  — resize the PTY
        everything else    — raw stdin to the shell

    Architecture: flask-sock/simple_websocket is NOT thread-safe for concurrent
    send+receive from different threads.  We solve this by keeping ws.send()
    exclusively on the main thread and running ws.receive() in a side thread
    that puts data into an input queue.  The main thread multiplexes the PTY
    fd (via select) with the input queue, so it is the only caller of ws.send().
    """
    shell_choice = request.args.get("shell", "").strip()

    if sys.platform == "win32":
        if shell_choice == "cmd":
            cmd = ["cmd.exe"]
        elif shell_choice == "wsl":
            cmd = ["wsl.exe"]
        else:
            cmd = ["powershell.exe", "-NoLogo", "-NoExit"]
    else:
        shells = {"bash": "/bin/bash", "zsh": "/bin/zsh", "sh": "/bin/sh"}
        chosen = shells.get(shell_choice)
        if chosen and os.path.isfile(chosen):
            cmd = [chosen, "--login"]
        else:
            cmd = [os.environ.get("SHELL", "/bin/bash"), "--login"]

    # ── shared: input queue and stop event ────────────────────────────────────
    inp_q   = queue.Queue()   # browser → shell
    stopped = threading.Event()

    def _ws_receiver():
        """Side thread: only calls ws.receive() and feeds inp_q."""
        while not stopped.is_set():
            try:
                data = ws.receive(timeout=5)
                if data is None:
                    continue   # idle timeout — no data, keep waiting
                inp_q.put(data)
            except Exception:
                stopped.set()
                break

    threading.Thread(target=_ws_receiver, daemon=True).start()

    if sys.platform == "win32" and _HAS_WINPTY:
        # ── Windows: ConPTY via pywinpty (full interactive PTY) ───────────────
        pty_cmd = "cmd.exe" if shell_choice == "cmd" else "powershell.exe -NoLogo -NoExit"
        try:
            pty_proc = _WinPtyProcess.spawn(pty_cmd, dimensions=(24, 80))
        except Exception as e:
            try: ws.send(f"\r\n[ConPTY launch error: {e}]\r\n")
            except Exception: pass
            stopped.set()
            return

        def _conpty_reader():
            """Daemon thread: read PTY output and post to queue."""
            while not stopped.is_set():
                try:
                    chunk = pty_proc.read(4096)
                    if chunk:
                        inp_q.put(("__pty__", chunk))
                except EOFError:
                    break
                except Exception:
                    break
            stopped.set()

        threading.Thread(target=_conpty_reader, daemon=True).start()

        while not stopped.is_set():
            try:
                item = inp_q.get(timeout=0.1)
            except Exception:
                continue
            if isinstance(item, tuple) and item[0] == "__pty__":
                data = item[1]
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                try:
                    ws.send(data)
                except Exception:
                    break
            elif isinstance(item, str):
                if item.startswith("!resize:"):
                    try:
                        _, rows, cols = item.split(":")
                        pty_proc.setwinsize(int(rows), int(cols))
                    except Exception:
                        pass
                else:
                    try:
                        pty_proc.write(item)
                    except Exception:
                        break

        stopped.set()
        try: pty_proc.terminate()
        except Exception: pass

    elif sys.platform == "win32":
        # ── Windows fallback: subprocess pipes (pywinpty not available) ───────
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=False, **_win_flags(),
        )

        def _win_reader():
            while proc.poll() is None:
                chunk = proc.stdout.read(512)
                if not chunk:
                    break
                inp_q.put(("__pty__", chunk))
            stopped.set()

        threading.Thread(target=_win_reader, daemon=True).start()

        while not stopped.is_set():
            try:
                item = inp_q.get(timeout=0.05)
            except Exception:
                continue
            if isinstance(item, tuple) and item[0] == "__pty__":
                try:
                    ws.send(item[1].decode("utf-8", errors="replace"))
                except Exception:
                    break
            else:
                if not item.startswith("!resize:"):
                    try:
                        proc.stdin.write(item.encode() if isinstance(item, str) else item)
                        proc.stdin.flush()
                    except Exception:
                        break

        stopped.set()
        try: proc.kill()
        except Exception: pass

    else:
        # ── Unix/macOS: real PTY ──────────────────────────────────────────────
        import pty, select as _select

        master, slave = pty.openpty()
        _pty_set_size(master, 24, 80)   # match xterm.js defaults; JS sends correct size on connect

        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("COLORTERM", "truecolor")
        proc = subprocess.Popen(
            cmd,
            stdin=slave, stdout=slave, stderr=slave,
            preexec_fn=os.setsid, close_fds=True,
            env=env,
        )
        os.close(slave)

        while not stopped.is_set():
            # Check PTY for output (short timeout so we stay responsive)
            try:
                r, _, _ = _select.select([master], [], [], 0.02)
            except Exception:
                break
            if r:
                try:
                    chunk = os.read(master, 4096)
                    if not chunk:
                        break
                    ws.send(chunk.decode("latin-1"))   # only send from this thread
                except Exception:
                    break

            # Forward any pending browser input to the PTY
            while not inp_q.empty():
                try:
                    data = inp_q.get_nowait()
                except Exception:
                    break
                if isinstance(data, str) and data.startswith("!resize:"):
                    try:
                        _, rows, cols = data.split(":")
                        _pty_set_size(master, int(rows), int(cols))
                    except Exception:
                        pass
                else:
                    try:
                        os.write(master, data.encode("latin-1") if isinstance(data, str) else data)
                    except Exception:
                        break

            if proc.poll() is not None:
                break

        stopped.set()
        try: os.kill(proc.pid, signal.SIGTERM)
        except Exception: pass
        try: os.close(master)
        except Exception: pass


# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}"
    print(f"\n  GDP SKE Manager Web  ->  {url}\n")

    # Start Flask in a background daemon thread so pywebview owns the main thread
    flask_thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True),
        daemon=True,
    )
    flask_thread.start()
    time.sleep(0.8)   # give Flask a moment to bind the port

    # Calculate centered position using tkinter (already available on all platforms)
    win_w, win_h = 1400, 860
    try:
        import tkinter as _tk
        _r = _tk.Tk(); _r.withdraw()
        sw, sh = _r.winfo_screenwidth(), _r.winfo_screenheight()
        _r.destroy()
        win_x = max(0, (sw - win_w) // 2)
        win_y = max(0, (sh - win_h) // 2)
    except Exception:
        win_x = win_y = None

    # Resolve icon path: works both in dev (next to script) and PyInstaller bundle
    _icon_path = os.path.join(os.path.dirname(os.path.abspath(
        sys.executable if getattr(sys, 'frozen', False) else __file__
    )), 'icon.ico')
    _icon_arg = _icon_path if os.path.exists(_icon_path) else None

    win = webview.create_window(
        "GDP SKE Manager — Standard Chartered",
        url,
        width=win_w,
        height=win_h,
        x=win_x,
        y=win_y,
        min_size=(900, 600),
        resizable=True,
    )
    # private_mode=False: persists cookies/localStorage across restarts so that
    # the one-time clipboard-read Allow/Block decision is remembered by WebView2.
    webview.start(lambda: win.maximize(), private_mode=False,
                  icon=_icon_arg)
