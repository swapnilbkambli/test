#!/usr/bin/env python3
"""
GDP SKE Manager Web — Flask-based browser UI for GDP Kubernetes (SKE) Platform
Run:    python ske_manager_web.py
Opens:  http://localhost:5000

Config is shared with ske_manager.py  (~/.ske_manager.json)
"""

import subprocess, sys

# ─── Private PyPI index (fill in your internal URL if required) ───────────────
#
#   PIP_INDEX_URL = "https://pypi.internal.your-company.com/simple"
#
PIP_INDEX_URL = ""   # leave empty to use the default public PyPI

# ─── Auto-install dependencies before anything else ────────────────────────────
_REQUIRED = {"flask": "flask"}   # import_name → pip package name

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
from flask import Flask, render_template, jsonify, request, Response, stream_with_context
import threading, queue, json, base64, os, time, webbrowser, shlex
from datetime import datetime, timezone

app = Flask(__name__)
app.secret_key = os.urandom(24)

# ─── Paths ─────────────────────────────────────────────────────────────────────
KUBECTL_CMD = os.environ.get("KUBECTL_CMD", "kubectl")
SKECTL_CMD  = os.environ.get("SKECTL_CMD",
                              "skectl.exe" if sys.platform == "win32" else "skectl")
PORT        = int(os.environ.get("SKE_PORT", 5000))

# ─── Global session state (single-user local tool) ─────────────────────────────
_creds     = None          # (ske_url, auth_url, user, pw, skectl)
_log_stop  = threading.Event()
_log_q     = queue.Queue()

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


# ─── JWT / config helpers (shared logic with ske_manager.py) ──────────────────

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


def _ns_flags(resource, namespace, all_ns):
    cluster_wide = resource in ("namespaces", "nodes", "persistentvolumes",
                                 "clusterroles", "clusterrolebindings")
    if cluster_wide:
        return []
    if all_ns:
        return ["--all-namespaces"]
    return ["-n", namespace] if namespace else []


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

    args = [skectl, "login", ske, "-s", auth, "-u", user, "-p", pw]
    stdout, stderr, rc = run_cmd(args, timeout=30)
    if rc == 0:
        _creds = (ske, auth, user, pw, skectl)
        return jsonify(ok=True)
    return jsonify(ok=False, error=(stderr or stdout).splitlines()[0][:120]), 401


@app.route("/api/session")
def session_status():
    expiry = _kubeconfig_expiry()
    ctx, _, _ = run_cmd([KUBECTL_CMD, "config", "current-context"])
    if expiry is None:
        # Fall back to health check
        _, _, rc = run_cmd([KUBECTL_CMD, "get", "namespaces",
                            "--request-timeout=5s"], timeout=8)
        return jsonify(
            context=ctx.strip(),
            expiry=None,
            remaining=None,
            healthy=(rc == 0)
        )
    now       = datetime.now(tz=timezone.utc)
    remaining = max(0, (expiry - now).total_seconds())
    return jsonify(
        context=ctx.strip(),
        expiry=expiry.isoformat(),
        remaining=int(remaining),
        healthy=(remaining > 0)
    )


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
    if rc != 0:
        return jsonify(ok=False, error=stderr), 500
    return jsonify(ok=True, namespaces=sorted(stdout.strip().split()))


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
    lines = stdout.strip().splitlines()
    if not lines:
        return jsonify(ok=True, headers=[], rows=[], command=" ".join(args))
    headers = lines[0].split()
    rows    = [{"cells": l.split(), "raw": l} for l in lines[1:] if l.strip()]
    return jsonify(ok=True, headers=headers, rows=rows, command=" ".join(args))


@app.route("/api/describe")
def describe():
    res  = request.args.get("type", "pods")
    name = request.args.get("name", "")
    ns   = request.args.get("namespace", "default")
    all_ns = request.args.get("all", "false").lower() == "true"
    args = [KUBECTL_CMD, "describe", res, name] + _ns_flags(res, ns, all_ns)
    stdout, stderr, rc = run_cmd(args, timeout=30)
    out = stdout if rc == 0 else stderr
    return jsonify(ok=(rc == 0), output=out, command=" ".join(args))


@app.route("/api/yaml")
def get_yaml():
    res  = request.args.get("type", "pods")
    name = request.args.get("name", "")
    ns   = request.args.get("namespace", "default")
    all_ns = request.args.get("all", "false").lower() == "true"
    args = [KUBECTL_CMD, "get", res, name, "-o", "yaml"] + _ns_flags(res, ns, all_ns)
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
    pod  = request.args.get("pod", "")
    ns   = request.args.get("namespace", "default")
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

    pod     = request.args.get("pod", "")
    ns      = request.args.get("namespace", "default")
    cnt     = request.args.get("container", "")
    tail    = request.args.get("tail", "300")
    prev    = request.args.get("previous", "false") == "true"

    if not pod:
        return jsonify(ok=False, error="pod required"), 400

    # Stop any existing stream
    _log_stop.set()
    time.sleep(0.15)
    _log_stop.clear()
    _log_q = queue.Queue()

    args = [KUBECTL_CMD, "logs", "-f", pod, "-n", ns, f"--tail={tail}"]
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
            q.put(None)  # sentinel

    threading.Thread(target=_reader, daemon=True).start()

    def _generate():
        # Send the command as first event
        yield f"data: {json.dumps({'type': 'cmd', 'text': ' '.join(args)})}\n\n"
        while True:
            try:
                line = q.get(timeout=20)
                if line is None:
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    break
                yield f"data: {json.dumps({'type': 'line', 'text': line})}\n\n"
            except queue.Empty:
                yield ": ping\n\n"   # keep connection alive

    return Response(
        stream_with_context(_generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/logs/stop", methods=["POST"])
def stop_logs():
    _log_stop.set()
    return jsonify(ok=True)


# ─── Routes: Config ────────────────────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
def get_config():
    cfg = _load_cfg()
    # Return full env data including password (localhost-only tool)
    return jsonify(environments=cfg.get("environments", {}),
                   last_environment=cfg.get("last_environment", ""))


@app.route("/api/config", methods=["POST"])
def save_config():
    d    = request.json or {}
    name = d.get("name", "").strip()
    if not name:
        return jsonify(ok=False, error="Name required."), 400
    cfg  = _load_cfg()
    envs = cfg.setdefault("environments", {})
    existing_pins = envs.get(name, {}).get("pinned_namespaces", [])
    envs[name] = {
        "ske_url":            d.get("ske_url", ""),
        "auth_url":           d.get("auth_url", ""),
        "username":           d.get("username", ""),
        "password":           d.get("password", ""),
        "skectl_path":        d.get("skectl_path", SKECTL_CMD),
        "pinned_namespaces":  d.get("pinned_namespaces", existing_pins),
    }
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


# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    print(f"\n  GDP SKE Manager Web  →  {url}\n")
    # Open browser after a short delay to let Flask start
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
