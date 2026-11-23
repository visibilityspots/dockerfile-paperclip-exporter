#!/usr/bin/env python3
"""Prometheus exporter for a self-hosted Paperclip instance (https://github.com/paperclipai/paperclip).

Polls the Paperclip REST API with a board API key and serves the result on /metrics.
A key of a user with the read-only `viewer` role is enough. Stdlib only.

Design notes:
- Polling runs in a background thread; /metrics serves the last snapshot. Every
  request has a timeout so a hung API shows up as a stale
  paperclip_last_poll_success_timestamp_seconds instead of frozen gauges.
- /healthz answers 200 whenever the process runs, independent of the Paperclip API:
  an API outage is reported through paperclip_up, not by turning the exporter's own
  health check critical.
- No free text in labels. Agents and connections are identified by id prefix plus a
  sanitised name, error codes are whitelisted, routines by id prefix.
- Failed runs are exported as "last failure timestamp per error code", not as a
  counter: the exporter keeps no state, and a restart would re-count history.
"""

import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = os.environ.get("PAPERCLIP_API", "").rstrip("/")
COMPANY = os.environ.get("PAPERCLIP_COMPANY_ID", "")
KEY = os.environ.get("PAPERCLIP_API_KEY", "")
PORT = int(os.environ.get("METRICS_PORT", "9878"))
INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "20"))
RUN_ROW_LIMIT = 1000  # the API returns at most this many runs without a time filter

ERROR_CODES = {
    "claude_auth_required",
    "configuration_incomplete",
    "timeout",
    "process_lost",
    "adapter_failed",
    "budget_exceeded",
    "cancelled",
}
ISSUE_STATUSES = ["backlog", "todo", "in_progress", "in_review", "blocked", "done", "cancelled"]
RUN_STATUSES = ["queued", "running", "succeeded", "failed", "cancelled", "timed_out"]

_snapshot = "# no poll completed yet\n"
_lock = threading.Lock()
_poll_errors = {}  # endpoint -> count (monotone for the process lifetime)
_last_success = 0.0


def bad_config():
    """Reject missing settings, or consul-template's '<no value>', before touching anything."""
    return any(not v or v.strip() in ("", "<no value>") for v in (API, COMPANY, KEY))


def get(path):
    req = urllib.request.Request(
        f"{API}/{path}",
        headers={"Authorization": f"Bearer {KEY}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def as_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("items", "data", "connections", "results"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def clean(value, maxlen=40):
    """Label-safe name: lowercase, [a-z0-9_-] only, template parts like {{date}} removed."""
    value = re.sub(r"\{\{.*?\}\}", "", str(value or ""))
    value = re.sub(r"[^a-z0-9_-]+", "_", value.strip().lower()).strip("_")
    return value[:maxlen] or "unnamed"


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class Out:
    def __init__(self):
        self.lines = []
        self.seen = set()

    def m(self, name, value, labels=None, help_="", type_="gauge"):
        if name not in self.seen:
            self.seen.add(name)
            self.lines.append(f"# HELP {name} {help_}")
            self.lines.append(f"# TYPE {name} {type_}")
        lbl = ""
        if labels:
            lbl = "{" + ",".join(f'{k}="{esc(v)}"' for k, v in labels.items()) + "}"
        self.lines.append(f"{name}{lbl} {value}")


def poll():
    o = Out()
    ok = True

    def fetch(endpoint, path):
        nonlocal ok
        try:
            return get(path)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            ok = False
            _poll_errors[endpoint] = _poll_errors.get(endpoint, 0) + 1
            print(f"poll {endpoint} failed: {type(e).__name__}: {e}", flush=True)
            return None

    C = f"companies/{COMPANY}"

    health = fetch("health", "health")
    if isinstance(health, dict):
        o.m("paperclip_info", 1, {"version": clean(health.get("version"), 30)},
            "Paperclip server version")
        o.m("paperclip_health_ok", 1 if health.get("status") == "ok" else 0, None,
            "1 when /api/health reports status ok")

    agents = fetch("agents", f"{C}/agents")
    in_use = set()
    if agents is not None:
        for a in as_list(agents):
            aid = str(a.get("id", ""))[:8]
            lab = {"agent": clean(a.get("name")), "agent_id": aid}
            conn = ((a.get("runtimeConfig") or {}).get("aiConnection") or {}).get("connectionId")
            if conn:
                in_use.add(str(conn))
            o.m("paperclip_agent_info", 1,
                {**lab, "status": clean(a.get("status"), 20),
                 "lifecycle_state": clean(a.get("lifecycleState"), 20)},
                "Agent status and lifecycle state (always 1)")
            o.m("paperclip_agent_paused", 1 if a.get("pausedAt") else 0, lab,
                "1 when the agent is paused")
            o.m("paperclip_agent_error", 1 if (a.get("errorReason") or a.get("lifecycleError")) else 0,
                lab, "1 when the agent reports an errorReason or lifecycleError")
            hb = ts(a.get("lastHeartbeatAt"))
            if hb:
                o.m("paperclip_agent_last_heartbeat_timestamp_seconds", hb, lab,
                    "Last heartbeat of the agent")

    conns = fetch("ai_connections", f"{C}/ai-connections")
    if conns is not None:
        for c in as_list(conns):
            cid = str(c.get("id", ""))
            o.m("paperclip_ai_connection_connected", 1 if c.get("status") == "connected" else 0,
                {"connection": cid[:8], "provider": clean(c.get("provider"), 20),
                 "status": clean(c.get("status"), 20),
                 "in_use": "true" if cid in in_use else "false"},
                "1 when the AI connection status is connected; in_use = an agent points to it")

    runs = fetch("heartbeat_runs", f"{C}/heartbeat-runs?limit={RUN_ROW_LIMIT}")
    if runs is not None:
        rows = as_list(runs)
        counts = {s: 0 for s in RUN_STATUSES}
        last_fail = {}
        last_finish = None
        for r in rows:
            st = clean(r.get("status"), 20)
            counts[st] = counts.get(st, 0) + 1
            t = ts(r.get("finishedAt")) or ts(r.get("createdAt"))
            if t and st in ("succeeded", "failed") and (last_finish is None or t > last_finish):
                last_finish = t
            if st == "failed" and t:
                code = r.get("errorCode") or "none"
                code = code if code in ERROR_CODES or code == "none" else "other"
                last_fail[code] = max(last_fail.get(code, 0), t)
        for st, n in counts.items():
            o.m("paperclip_runs", n, {"status": st},
                "Heartbeat runs in the API response, per status")
        o.m("paperclip_runs_truncated", 1 if len(rows) >= RUN_ROW_LIMIT else 0, None,
            "1 when the run list hit the API row limit (counts are then partial)")
        for code, t in last_fail.items():
            o.m("paperclip_run_last_failure_timestamp_seconds", t, {"error_code": code},
                "finishedAt (or createdAt) of the latest failed run per error code")
        if last_finish:
            o.m("paperclip_run_last_finished_timestamp_seconds", last_finish, None,
                "finishedAt of the latest succeeded or failed run")

    routines = fetch("routines", f"{C}/routines")
    if routines is not None:
        rlist = as_list(routines)
        o.m("paperclip_routines", len(rlist), None, "Number of routines")
        for r in rlist:
            lab = {"routine": clean(r.get("title")), "routine_id": str(r.get("id", ""))[:8]}
            o.m("paperclip_routine_active", 1 if r.get("status") == "active" else 0, lab,
                "1 when the routine status is active")
            lr = r.get("lastRun") or {}
            t = ts(lr.get("completedAt")) or ts(lr.get("triggeredAt"))
            if t:
                o.m("paperclip_routine_last_run_timestamp_seconds", t, lab,
                    "completedAt (or triggeredAt) of the routine's last run")
            o.m("paperclip_routine_last_run_success", 1 if lr.get("status") == "completed" else 0,
                lab, "1 when the routine's last run status is completed (issue_created is not)")

    issues = fetch("issues", f"{C}/issues")
    if issues is not None:
        counts = {s: 0 for s in ISSUE_STATUSES}
        for i in as_list(issues):
            st = clean(i.get("status"), 20)
            counts[st] = counts.get(st, 0) + 1
        for st, n in counts.items():
            o.m("paperclip_issues", n, {"status": st}, "Issues per status")

    return o, ok


def loop():
    global _snapshot, _last_success
    while True:
        start = time.time()
        if bad_config():
            o, ok = Out(), False
            print("PAPERCLIP_API, PAPERCLIP_COMPANY_ID or PAPERCLIP_API_KEY missing, not polling", flush=True)
        else:
            try:
                o, ok = poll()
            except Exception as e:  # never let the thread die
                print(f"poll crashed: {type(e).__name__}: {e}", flush=True)
                o, ok = Out(), False
        if ok:
            _last_success = time.time()
        o.m("paperclip_up", 1 if ok else 0, None, "1 when the last poll of every endpoint succeeded")
        o.m("paperclip_last_poll_success_timestamp_seconds", _last_success, None,
            "Time of the last fully successful poll (0 = never)")
        o.m("paperclip_poll_duration_seconds", round(time.time() - start, 3), None,
            "Duration of the last poll")
        o.m("paperclip_poll_errors_total", 0, {"endpoint": "_none"}, "Failed API calls", "counter")
        for ep, n in sorted(_poll_errors.items()):
            o.m("paperclip_poll_errors_total", n, {"endpoint": ep}, "Failed API calls", "counter")
        with _lock:
            _snapshot = "\n".join(o.lines) + "\n"
        time.sleep(max(5, INTERVAL - (time.time() - start)))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            with _lock:
                body = _snapshot.encode()
            ctype = "text/plain; version=0.0.4; charset=utf-8"
        elif self.path == "/healthz":
            body, ctype = b"ok\n", "text/plain"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    # PID 1 in a container gets no default SIGTERM handler: exit cleanly instead of
    # waiting for the runtime's SIGKILL.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    threading.Thread(target=loop, daemon=True).start()
    print(f"paperclip-exporter on :{PORT}, polling {API} every {INTERVAL}s", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
