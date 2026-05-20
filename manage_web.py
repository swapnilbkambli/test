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
PIP_INDEX_URL = ""   # leave empty to use the default public PyPI

# ─── Auto-install dependencies before anything else ────────────────────────────
_REQUIRED = {"flask": "flask", "webview": "pywebview", "flask_sock": "flask-sock"}   # import_name → pip package name

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
import threading, queue, json, base64, os, time, shlex, signal, struct
import webview
from flask_sock import Sock
from datetime import datetime, timezone

app = Flask(__name__)
app.secret_key = os.urandom(24)
sock = Sock(app)

# ─── Paths ─────────────────────────────────────────────────────────────────────
KUBECTL_CMD = os.environ.get("KUBECTL_CMD", "kubectl")
SKECTL_CMD  = os.environ.get("SKECTL_CMD",
                              "skectl.exe" if sys.platform == "win32" else "skectl")
PORT        = int(os.environ.get("SKE_PORT", 5000))

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


# ─── Routes: Contexts ──────────────────────────────────────────────────────────

@app.route("/api/contexts")
def list_contexts():
    stdout, _, _ = run_cmd([KUBECTL_CMD, "config", "get-contexts", "-o", "name"])
    ctx, _, _    = run_cmd([KUBECTL_CMD, "config", "current-context"])
    contexts = [c.strip() for c in stdout.strip().splitlines() if c.strip()]
    return jsonify(ok=True, contexts=contexts, current=ctx.strip())


@app.route("/api/contexts/switch", methods=["POST"])
def switch_context():
    d   = request.json or {}
    ctx = d.get("context", "").strip()
    if not ctx:
        return jsonify(ok=False, error="context required"), 400
    stdout, stderr, rc = run_cmd([KUBECTL_CMD, "config", "use-context", ctx])
    out = stdout if rc == 0 else stderr
    return jsonify(ok=(rc == 0), output=out.strip())


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

    pod  = request.args.get("pod", "")
    ns   = request.args.get("namespace", "default")
    cnt  = request.args.get("container", "")
    tail = request.args.get("tail", "300")
    prev = request.args.get("previous", "false") == "true"

    if not pod:
        return jsonify(ok=False, error="pod required"), 400

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
    try:
        pods   = json.loads(stdout).get("items", [])
        counts = {"running": 0, "pending": 0, "failed": 0,
                  "succeeded": 0, "unknown": 0, "total": len(pods)}
        alerts = []
        for pod in pods:
            phase = pod.get("status", {}).get("phase", "Unknown").lower()
            pname = pod["metadata"]["name"]
            pns   = pod["metadata"].get("namespace", ns)
            if   phase == "running":   counts["running"]   += 1
            elif phase == "pending":   counts["pending"]   += 1
            elif phase == "failed":    counts["failed"]    += 1
            elif phase == "succeeded": counts["succeeded"] += 1
            else:                      counts["unknown"]   += 1
            for cs in pod.get("status", {}).get("containerStatuses", []):
                reason = cs.get("state", {}).get("waiting", {}).get("reason", "")
                if reason in ("CrashLoopBackOff", "OOMKilled", "Error",
                               "ImagePullBackOff", "ErrImagePull"):
                    alerts.append({"name": pname, "namespace": pns, "reason": reason,
                                   "restarts": cs.get("restartCount", 0),
                                   "container": cs.get("name", "")})
        return jsonify(ok=True, counts=counts, alerts=alerts)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


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

    return jsonify(ok=True, topology=list(nodes.values()))


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


# ─── Routes: Config ────────────────────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
def get_config():
    cfg = _load_cfg()
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


# ─── WebSocket: interactive shell ─────────────────────────────────────────────

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

    if sys.platform == "win32":
        # ── Windows: subprocess pipes ─────────────────────────────────────────
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
                inp_q.put(("__pty__", chunk))   # tagged tuple = PTY output
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
    print(f"\n  GDP SKE Manager Web  →  {url}\n")

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

    webview.create_window(
        "GDP SKE Manager — Standard Chartered",
        url,
        width=win_w,
        height=win_h,
        x=win_x,
        y=win_y,
        min_size=(900, 600),
        resizable=True,
    )
    webview.start()
