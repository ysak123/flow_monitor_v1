#!/usr/bin/env python3
"""
apex_monitor.py -- Live dashboard to monitor Block/APEX (apr_snps) PnR flow progress
across one or more workareas by watching stage status files (.pass/.fail/.invalid)
and the freshness of the per-stage log files.

Stdlib only (works on Python 3.6+). No external deps.

Usage:
    python3 apex_monitor.py [--port 8899] [--host 0.0.0.0]

Then open the printed URL in a browser, paste one workarea path per line, and Start.

A "workarea path" can be any of:
  * the block root, e.g.  .../r2g/km_noc_vnoc_l_01
  * the apr_snps dir,     .../km_noc_vnoc_l_01/apr_snps
  * the logs dir itself,  .../apr_snps/logs
  * an apex/r2g/default parent -- the tool searches downward for apr_snps/logs.
"""

import argparse
import json
import os
import re
import socketserver
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

# Canonical ordered stage list for the apr_snps block flow. Discovery is dynamic,
# but this guarantees not-yet-started stages still appear as "pending".
DEFAULT_STAGES = [
    "01_init_design",
    "02_floorplan",
    "03_logic_opto",
    "04_insert_dft",
    "05_compile_initial",
    "06_compile_final",
    "07_cts",
    "08_clock_opt",
    "09_route",
    "10_route_opt",
    "11_fill",
    "12_pnr_finish",
]

# Seconds: if a stage log was modified within this window (and no pass/fail yet),
# the stage is considered actively RUNNING.
DEFAULT_RUNNING_WINDOW = 240

STATUS_RE = re.compile(r"^(\d+_[A-Za-z0-9_.\-]+?)\.(pass|fail|invalid)$")
STAGEDIR_RE = re.compile(r"^(\d+_[A-Za-z0-9_.\-]+)$")


# ----------------------------------------------------------------------------
# Core scanning logic
# ----------------------------------------------------------------------------

def looks_like_logs_dir(path):
    """True if the dir contains stage status files or apr_snps_* logs."""
    try:
        for name in os.listdir(path):
            if STATUS_RE.match(name):
                return True
            if name.startswith("apr_snps_") and name.endswith(".log"):
                return True
    except OSError:
        return False
    return False


def resolve_logs_dir(path):
    """Given a user-entered path, find the apr_snps/logs directory."""
    path = os.path.expanduser(path.strip())
    if not path:
        return None
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return None

    candidates = [
        path,
        os.path.join(path, "logs"),
        os.path.join(path, "apr_snps", "logs"),
        os.path.join(path, "apr_snps"),
    ]
    for cand in candidates:
        if os.path.isdir(cand) and looks_like_logs_dir(cand):
            return cand

    # Bounded downward search for an apr_snps/logs (or any logs dir that qualifies).
    best = None
    base_depth = path.rstrip(os.sep).count(os.sep)
    for root, dirs, _files in os.walk(path):
        depth = root.count(os.sep) - base_depth
        if depth > 6:
            dirs[:] = []
            continue
        if os.path.basename(root) == "logs" and looks_like_logs_dir(root):
            # Prefer one whose parent is apr_snps.
            if os.path.basename(os.path.dirname(root)) == "apr_snps":
                return root
            if best is None:
                best = root
    return best


def stage_num(stage):
    m = re.match(r"^(\d+)_", stage)
    return int(m.group(1)) if m else 9999


def base_name(stage):
    """01_init_design -> init_design"""
    return re.sub(r"^\d+_", "", stage)


def find_log_for_stage(logs_dir, stage, listing):
    """Locate the active (non-.bak) log file for a stage."""
    b = base_name(stage)
    exact = "apr_snps_%s.log" % b
    # APEX >= 1.1 writes logs/<NN_stage>/apr_snps_<stage>.log; older flows keep
    # them flat in logs/. A migrated workarea can have both, so take the newest.
    candidates = []
    if exact in listing:
        candidates.append(exact)
    if stage in listing:
        nested = os.path.join(stage, exact)
        if os.path.isfile(os.path.join(logs_dir, nested)):
            candidates.append(nested)
    if candidates:
        return max(candidates,
                   key=lambda n: os.path.getmtime(os.path.join(logs_dir, n)))
    # Fallback: fuzzy match apr_snps_*<base>*.log (no timestamp .bak)
    for name in listing:
        if (name.startswith("apr_snps_") and name.endswith(".log")
                and ".log.bak" not in name and b in name):
            return name
    return None


def read_tail(filepath, max_bytes=200000, max_lines=400):
    """Efficiently read the last max_lines of a (possibly huge) file."""
    try:
        size = os.path.getsize(filepath)
        with open(filepath, "rb") as f:
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
        text = data.decode("utf-8", "replace")
        if start > 0:
            # drop first partial line
            nl = text.find("\n")
            if nl != -1:
                text = text[nl + 1:]
        lines = text.splitlines()
        return lines[-max_lines:]
    except OSError:
        return []


def last_nonblank_line(filepath):
    lines = read_tail(filepath, max_bytes=8000, max_lines=40)
    for ln in reversed(lines):
        if ln.strip():
            return ln.strip()
    return ""


def parse_fail_file(filepath):
    """Extract counts + error lines from a NN_stage.fail file."""
    info = {"error_count": None, "warning_count": None, "errors": [], "raw": ""}
    try:
        with open(filepath, "r", errors="replace") as f:
            content = f.read()
    except OSError:
        return info
    info["raw"] = content[:4000]
    for line in content.splitlines():
        m = re.match(r"\s*Error Count:\s*(\d+)", line)
        if m:
            info["error_count"] = int(m.group(1))
            continue
        m = re.match(r"\s*Warning Count:\s*(\d+)", line)
        if m:
            info["warning_count"] = int(m.group(1))
            continue
        if re.search(r"\b(Error|APEX_ERROR)\b", line) and "Count" not in line:
            info["errors"].append(line.strip())
    info["errors"] = info["errors"][:12]
    return info


def iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def scan_workarea(path, running_window):
    """Return a dict describing one workarea's flow status."""
    result = {
        "input": path,
        "logs_dir": None,
        "resolved": False,
        "error": None,
        "stages": [],
        "state": "UNKNOWN",
        "current": None,
        "progress": {"pass": 0, "total": 0, "pct": 0},
        "scanned_at": iso(time.time()),
    }

    logs_dir = resolve_logs_dir(path)
    if not logs_dir:
        result["error"] = "Could not find an apr_snps/logs directory under this path."
        return result

    result["logs_dir"] = logs_dir
    result["resolved"] = True

    try:
        listing = os.listdir(logs_dir)
    except OSError as e:
        result["error"] = "Cannot list logs dir: %s" % e
        return result

    listing_set = set(listing)

    # Discover stages: default list + any found in status files / dirs.
    found = set(DEFAULT_STAGES)
    status_map = {}   # stage -> (status, filepath, mtime)
    for name in listing:
        m = STATUS_RE.match(name)
        if m:
            st = m.group(1)
            found.add(st)
            fp = os.path.join(logs_dir, name)
            try:
                mt = os.path.getmtime(fp)
            except OSError:
                mt = 0
            # fail/invalid win over pass if somehow both exist; keep newest signal
            prev = status_map.get(st)
            if prev is None or mt >= prev[2]:
                status_map[st] = (m.group(2), fp, mt)
        else:
            m2 = STAGEDIR_RE.match(name)
            if m2 and os.path.isdir(os.path.join(logs_dir, name)):
                found.add(m2.group(1))

    now = time.time()
    stages_sorted = sorted(found, key=stage_num)

    running_stage = None
    fail_stage = None
    last_pass_num = -1
    pass_count = 0

    for st in stages_sorted:
        log_name = find_log_for_stage(logs_dir, st, listing_set)
        log_path = os.path.join(logs_dir, log_name) if log_name else None
        log_mtime = None
        log_size = None
        if log_path and os.path.isfile(log_path):
            try:
                log_mtime = os.path.getmtime(log_path)
                log_size = os.path.getsize(log_path)
            except OSError:
                pass

        status = "pending"
        status_mtime = None
        fail_info = None
        last_line = None

        if st in status_map:
            raw_status, fp, mt = status_map[st]
            status_mtime = mt
            if raw_status == "fail":
                status = "fail"
                fail_info = parse_fail_file(fp)
            elif raw_status == "invalid":
                status = "invalid"
            else:
                status = "pass"
        else:
            if log_mtime is not None:
                if (now - log_mtime) <= running_window:
                    status = "running"
                    last_line = last_nonblank_line(log_path)
                else:
                    status = "stale"
            else:
                status = "pending"

        if status == "pass":
            pass_count += 1
            last_pass_num = max(last_pass_num, stage_num(st))
        elif status == "running" and running_stage is None:
            running_stage = st
        elif status == "fail" and fail_stage is None:
            fail_stage = st

        result["stages"].append({
            "stage": st,
            "num": stage_num(st),
            "status": status,
            "status_mtime": iso(status_mtime),
            "log": log_name,
            "log_mtime": iso(log_mtime),
            "log_mtime_ts": log_mtime,
            "log_age": int(now - log_mtime) if log_mtime else None,
            "log_size": log_size,
            "fail_info": fail_info,
            "last_line": last_line,
        })

    total = len(stages_sorted)
    result["progress"] = {
        "pass": pass_count,
        "total": total,
        "pct": int(round(100.0 * pass_count / total)) if total else 0,
    }

    # Overall state + current stage
    final_stage = stages_sorted[-1] if stages_sorted else None
    final_pass = any(s["stage"] == final_stage and s["status"] == "pass"
                     for s in result["stages"])

    if running_stage:
        result["state"] = "RUNNING"
        result["current"] = running_stage
    elif fail_stage:
        result["state"] = "FAILED"
        result["current"] = fail_stage
    elif final_pass:
        result["state"] = "COMPLETE"
        result["current"] = final_stage
    else:
        # idle: next pending after last pass
        nxt = None
        for s in result["stages"]:
            if s["status"] in ("pending", "stale"):
                nxt = s["stage"]
                break
        result["state"] = "IDLE"
        result["current"] = nxt or (final_stage if final_stage else None)

    return result


# ----------------------------------------------------------------------------
# LEC (altera_lec / Conformal) scanning
# ----------------------------------------------------------------------------

LEC_PHASES = [
    ("setup", "Setup"),
    ("libs", "Read libraries"),
    ("golden", "Read golden (RTL)"),
    ("revised", "Read revised (netlist)"),
    ("guidance", "Guidance + constraints"),
    ("modeling", "Modeling"),
    ("mapping", "Map key points"),
    ("compare", "Compare"),
    ("reports", "Reports"),
    ("done", "Done"),
]
LEC_PHASE_IDX = {k: i for i, (k, _) in enumerate(LEC_PHASES)}

# Matched anywhere in a line: Conformal glues "NN% completed" progress onto
# the same line as later messages.
LEC_MARKERS = [
    ("libs", re.compile(r"// Command: (vpx )?read library")),
    ("golden", re.compile(r"// Command: vpx read design .*-golden|// Command: vpx read design -sv")),
    ("revised", re.compile(r"// Command: vpx read design .*-revised")),
    ("guidance", re.compile(r"// Command: vpx read guidance")),
    ("modeling", re.compile(r"// Command: vpx set system mode lec")),
    ("mapping", re.compile(r"// Mapping key points")),
    ("compare", re.compile(r"// Command: vpx compare\b")),
    ("reports", re.compile(r"// Command: vpx report verification")),
    ("done", re.compile(r"// Command: vpx exit\b")),
]
LEC_SUBSTEP_RE = re.compile(r"// ((?:Processing|Modeling) (?:Golden|Revised))")
LEC_PCT_RE = re.compile(r"(\d+)% completed")
LEC_KEYPTS_RE = re.compile(r"(Golden|Revised)\s+key points\s*=\s*(\d+)")
LEC_MAPPED_RE = re.compile(r"Mapped (\d+) (DFF|DLAT)\(s\)")
LEC_CPU_RE = re.compile(r"^CPU time\s*:\s*([\d.]+)")
LEC_ELAPSE_RE = re.compile(r"^Elapse time\s*:\s*([\d.]+)")
LEC_MEM_RE = re.compile(r"^Memory usage\s*:\s*([\d.]+)\s*M")
LEC_ERROR_RE = re.compile(r"^// Error:")

DEFAULT_LEC_SILENT = 3600

# realpath -> incremental parse state (Conformal logs are many MB; only new
# bytes are parsed on each refresh).
_LEC_CACHE = {}


def lec_log_candidates(logs_dir):
    try:
        names = os.listdir(logs_dir)
    except OSError:
        return []
    return [n for n in names
            if n.endswith("_lec.log") or n.endswith("_lec.log~")]


def resolve_lec_logs_dir(path):
    """Given a block root / lec dir / logs dir, find the LEC logs directory."""
    path = os.path.expanduser(path.strip())
    if not path:
        return None
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return None
    block = os.path.basename(path.rstrip(os.sep))
    candidates = [
        path,
        os.path.join(path, "logs"),
        os.path.join(path, "lec", block, "logs"),
        os.path.join(path, block, "logs"),
    ]
    for cand in candidates:
        if os.path.isdir(cand) and lec_log_candidates(cand):
            return cand
    for sub in ("lec", ""):
        base = os.path.join(path, sub) if sub else path
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for e in entries:
            cand = os.path.join(base, e, "logs")
            if os.path.isdir(cand) and lec_log_candidates(cand):
                return cand
    return None


def pick_lec_log(logs_dir):
    """Newest of <task>_lec.log / <task>_lec.log~ (an editor rename leaves
    Conformal writing to the '~' file)."""
    best = None
    for n in lec_log_candidates(logs_dir):
        fp = os.path.join(logs_dir, n)
        try:
            mt = os.path.getmtime(fp)
        except OSError:
            continue
        if best is None or mt > best[1]:
            best = (fp, mt)
    return best[0] if best else None


def _new_lec_state():
    return {"ino": None, "offset": 0, "partial": "", "phase": "setup",
            "phase_line": "", "substep": None, "pct": None,
            "keypts": {}, "mapped": {"DFF": 0, "DLAT": 0},
            "cpu": None, "elapse": None, "mem": None, "mem_peak": None,
            "errors": 0, "first_errors": []}


def parse_lec_log(fp):
    """Incrementally parse a Conformal log; returns the cached state dict."""
    real = os.path.realpath(fp)
    try:
        st = os.stat(real)
    except OSError:
        return None
    state = _LEC_CACHE.get(real)
    if state is None or state["ino"] != st.st_ino or st.st_size < state["offset"]:
        state = _new_lec_state()
        state["ino"] = st.st_ino
        _LEC_CACHE[real] = state
    if st.st_size == state["offset"]:
        return state
    try:
        with open(real, "rb") as f:
            f.seek(state["offset"])
            data = f.read()
    except OSError:
        return state
    state["offset"] += len(data)
    text = state["partial"] + data.decode("utf-8", "replace")
    lines = text.split("\n")
    state["partial"] = lines.pop()
    for ln in lines:
        for key, rx in LEC_MARKERS:
            if rx.search(ln) and LEC_PHASE_IDX[key] >= LEC_PHASE_IDX[state["phase"]]:
                if key != state["phase"]:
                    state["pct"] = None
                    state["substep"] = None
                state["phase"] = key
                state["phase_line"] = ln.strip()[:200]
        m = None
        for m in LEC_SUBSTEP_RE.finditer(ln):
            pass
        if m:
            state["substep"] = m.group(1)
            state["pct"] = None
        pm = None
        for pm in LEC_PCT_RE.finditer(ln):
            pass
        if pm:
            state["pct"] = int(pm.group(1))
        km = LEC_KEYPTS_RE.search(ln)
        if km:
            state["keypts"][km.group(1).lower()] = int(km.group(2))
        for mm in LEC_MAPPED_RE.finditer(ln):
            state["mapped"][mm.group(2)] += int(mm.group(1))
        c = LEC_CPU_RE.match(ln)
        if c:
            state["cpu"] = float(c.group(1))
        e = LEC_ELAPSE_RE.match(ln)
        if e:
            state["elapse"] = float(e.group(1))
        mem = LEC_MEM_RE.match(ln)
        if mem:
            v = float(mem.group(1))
            state["mem"] = v
            state["mem_peak"] = max(v, state["mem_peak"] or 0)
        if LEC_ERROR_RE.match(ln):
            state["errors"] += 1
            if len(state["first_errors"]) < 8:
                state["first_errors"].append(ln.strip()[:300])
    return state


def parse_lec_summary(fp):
    info = {"status": None, "counts": {}, "errors": []}
    try:
        with open(fp, "r", errors="replace") as f:
            content = f.read(200000)
    except OSError:
        return info
    m = re.search(r"Status\s*:\s*>*\s*([A-Z\-]+)", content)
    if m:
        info["status"] = m.group(1)
    for label in ("Equivalent", "Non-Equivalent", "Aborted", "Not-Mapped",
                  "Extra", "Unreachable"):
        cm = re.search(r"^\s*%s\s*:\s*(\d+)" % re.escape(label), content, re.M)
        if cm:
            info["counts"][label] = int(cm.group(1))
    for ln in content.splitlines():
        if "// Error:" in ln and len(info["errors"]) < 8:
            info["errors"].append(ln.strip()[:300])
    return info


def scan_lec(path, running_window, silent_window):
    result = {
        "input": path, "logs_dir": None, "resolved": False, "error": None,
        "state": "UNKNOWN", "block": None, "log": None, "phases": [],
        "scanned_at": iso(time.time()),
    }
    logs_dir = resolve_lec_logs_dir(path)
    if not logs_dir:
        result["error"] = "Could not find a LEC logs dir (*_lec.log) under this path."
        return result
    result["logs_dir"] = logs_dir
    result["resolved"] = True
    run_dir = os.path.dirname(logs_dir)
    result["block"] = os.path.basename(run_dir)

    fp = pick_lec_log(logs_dir)
    if not fp:
        result["error"] = "No *_lec.log found in %s" % logs_dir
        return result
    now = time.time()
    log_mtime = os.path.getmtime(fp)
    st = parse_lec_log(fp) or _new_lec_state()
    result["log"] = fp
    result["log_mtime"] = iso(log_mtime)
    result["log_age"] = int(now - log_mtime)
    result["log_size"] = os.path.getsize(fp)

    threads = None
    try:
        with open(os.path.join(run_dir, "run_info.json")) as f:
            ri = json.load(f)
        threads = ri.get("threads")
        result["task"] = ri.get("task")
        result["compare_effort"] = ri.get("compare_effort")
    except (OSError, ValueError):
        pass
    result["threads"] = threads

    summary = None
    sfp = os.path.join(run_dir, "lec_summary.rpt")
    if os.path.isfile(sfp) and os.path.getmtime(sfp) >= log_mtime - 120:
        summary = parse_lec_summary(sfp)
        summary["mtime"] = iso(os.path.getmtime(sfp))
    result["summary"] = summary

    phase = st["phase"]
    cur = LEC_PHASE_IDX[phase]
    if summary and summary.get("status"):
        s = summary["status"]
        result["state"] = {"PASS": "COMPLETE", "PASSED": "COMPLETE",
                           "EQUIVALENT": "COMPLETE"}.get(s, "FAILED")
        result["final_status"] = s
    elif phase == "done":
        result["state"] = "COMPLETE"
    elif result["log_age"] <= running_window:
        result["state"] = "RUNNING"
    elif result["log_age"] <= silent_window:
        result["state"] = "IDLE"
    else:
        result["state"] = "SILENT"

    for i, (k, label) in enumerate(LEC_PHASES):
        if i < cur or (i == cur and result["state"] == "COMPLETE"):
            pst = "pass"
        elif i == cur:
            pst = {"RUNNING": "running", "FAILED": "fail",
                   "SILENT": "stale", "IDLE": "stale"}.get(result["state"], "running")
        else:
            pst = "pending"
        result["phases"].append({"key": k, "label": label, "num": i + 1, "status": pst})

    result["current"] = dict(LEC_PHASES)[phase]
    result["substep"] = st["substep"]
    result["pct"] = st["pct"]
    result["keypts"] = st["keypts"]
    result["mapped"] = st["mapped"]
    result["cpu"] = st["cpu"]
    result["elapse"] = st["elapse"]
    result["mem"] = st["mem"]
    result["mem_peak"] = st["mem_peak"]
    result["errors"] = st["errors"]
    result["first_errors"] = st["first_errors"]
    result["last_line"] = last_nonblank_line(fp)[-300:]
    return result


# ----------------------------------------------------------------------------
# HTTP server
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ApexMonitor/1.0"

    def log_message(self, *args):
        pass  # quiet

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, HTML_PAGE, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/log":
            qs = parse_qs(parsed.query)
            path = (qs.get("path") or [""])[0]
            stage = (qs.get("stage") or [""])[0]
            n = int((qs.get("n") or ["400"])[0])
            logs_dir = resolve_logs_dir(path)
            if not logs_dir:
                self._send(404, json.dumps({"error": "logs dir not found"}))
                return
            listing = set(os.listdir(logs_dir))
            log_name = find_log_for_stage(logs_dir, stage, listing)
            if not log_name:
                self._send(404, json.dumps({"error": "log not found for stage"}))
                return
            fp = os.path.join(logs_dir, log_name)
            lines = read_tail(fp, max_lines=n)
            self._send(200, json.dumps({
                "log": log_name, "path": fp,
                "mtime": iso(os.path.getmtime(fp)),
                "lines": lines,
            }))
            return
        if parsed.path == "/api/lec_log":
            qs = parse_qs(parsed.query)
            path = (qs.get("path") or [""])[0]
            n = int((qs.get("n") or ["500"])[0])
            logs_dir = resolve_lec_logs_dir(path)
            fp = pick_lec_log(logs_dir) if logs_dir else None
            if not fp:
                self._send(404, json.dumps({"error": "LEC log not found"}))
                return
            lines = read_tail(fp, max_lines=n)
            # Split glued "NN% completed" progress so the tail stays readable.
            out = []
            for ln in lines:
                out.extend(p for p in ln.replace("completed//", "completed\n//").split("\n") if p)
            self._send(200, json.dumps({
                "log": os.path.basename(fp), "path": fp,
                "mtime": iso(os.path.getmtime(fp)),
                "lines": out[-n:],
            }))
            return
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path not in ("/api/status", "/api/lec_status"):
            self._send(404, json.dumps({"error": "not found"}))
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception as e:
            self._send(400, json.dumps({"error": "bad request: %s" % e}))
            return
        paths = payload.get("paths", [])
        window = int(payload.get("running_window", DEFAULT_RUNNING_WINDOW))
        silent = int(payload.get("silent_window", DEFAULT_LEC_SILENT))
        is_lec = parsed.path == "/api/lec_status"
        results = []
        for p in paths:
            if not p or not p.strip():
                continue
            try:
                if is_lec:
                    results.append(scan_lec(p, window, silent))
                else:
                    results.append(scan_workarea(p, window))
            except Exception as e:
                results.append({"input": p, "resolved": False,
                                "error": "scan error: %s" % e, "stages": []})
        self._send(200, json.dumps({
            "results": results,
            "server_time": iso(time.time()),
            "running_window": window,
        }))


class ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


# ----------------------------------------------------------------------------
# Front-end (single embedded page)
# ----------------------------------------------------------------------------

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>APEX Flow Monitor</title>
<style>
  :root{
    --bg:#0f1420; --panel:#171e2e; --panel2:#1e2740; --line:#2a3550;
    --txt:#e6ecf7; --muted:#8ea0c0; --accent:#4f8cff;
    --pass:#2fbf71; --fail:#ff5c5c; --run:#4f8cff; --pending:#3a4560;
    --stale:#c9a227; --invalid:#ff9d3d;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
       background:var(--bg);color:var(--txt);font-size:14px}
  header{position:sticky;top:0;z-index:20;background:linear-gradient(180deg,#141b2b,#10151f);
         border-bottom:1px solid var(--line);padding:12px 18px;display:flex;
         align-items:center;gap:16px;flex-wrap:wrap}
  header h1{font-size:17px;margin:0;font-weight:650;letter-spacing:.3px}
  header h1 span{color:var(--accent)}
  .controls{display:flex;align-items:center;gap:10px;margin-left:auto;flex-wrap:wrap}
  .controls label{color:var(--muted);font-size:12px}
  select,input,button,textarea{font-family:inherit;color:var(--txt);
       background:var(--panel2);border:1px solid var(--line);border-radius:8px;
       padding:7px 10px;font-size:13px;outline:none}
  button{cursor:pointer;background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
  button.ghost{background:var(--panel2);color:var(--txt);border-color:var(--line);font-weight:500}
  button:hover{filter:brightness(1.08)}
  .wrap{padding:18px;max-width:1500px;margin:0 auto}
  .setup{background:var(--panel);border:1px solid var(--line);border-radius:12px;
         padding:16px;margin-bottom:18px}
  .setup h2{margin:0 0 4px;font-size:14px}
  .setup p{margin:0 0 10px;color:var(--muted);font-size:12px}
  textarea{width:100%;min-height:90px;resize:vertical;line-height:1.5;
           font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
  .setup .row{display:flex;gap:10px;align-items:center;margin-top:10px;flex-wrap:wrap}
  .pill{font-size:11px;color:var(--muted)}
  .updated{font-size:12px;color:var(--muted)}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--muted);
       margin-right:6px;vertical-align:middle}
  .dot.live{background:var(--pass);box-shadow:0 0 0 0 rgba(47,191,113,.6);animation:pulse 2s infinite}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(47,191,113,.5)}70%{box-shadow:0 0 0 7px rgba(47,191,113,0)}100%{box-shadow:0 0 0 0 rgba(47,191,113,0)}}

  .cards{display:flex;flex-direction:column;gap:16px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
  .card .top{display:flex;align-items:center;gap:12px;padding:14px 16px;flex-wrap:wrap}
  .card .title{font-size:15px;font-weight:650}
  .card .path{font-size:11.5px;color:var(--muted);font-family:ui-monospace,monospace;
              word-break:break-all}
  .badge{font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px;letter-spacing:.4px}
  .b-RUNNING{background:rgba(79,140,255,.18);color:#8fb6ff;border:1px solid #35507f}
  .b-FAILED{background:rgba(255,92,92,.16);color:#ff9a9a;border:1px solid #7a3838}
  .b-COMPLETE{background:rgba(47,191,113,.16);color:#7ce0a6;border:1px solid #2f6f4d}
  .b-IDLE{background:rgba(142,160,192,.14);color:#c3cfe4;border:1px solid #3a4560}
  .b-UNKNOWN,.b-ERROR{background:rgba(201,162,39,.16);color:#e6c65a;border:1px solid #6b5a1f}
  .rm{margin-left:auto;background:transparent;border:1px solid var(--line);color:var(--muted);
      padding:5px 9px;font-size:12px}
  .progress{height:8px;background:var(--panel2);border-radius:6px;overflow:hidden;margin:0 16px}
  .progress > i{display:block;height:100%;background:linear-gradient(90deg,#2fbf71,#4f8cff)}
  .meta{padding:8px 16px 2px;color:var(--muted);font-size:12px;display:flex;gap:16px;flex-wrap:wrap}
  .meta b{color:var(--txt);font-weight:600}

  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
        gap:10px;padding:14px 16px 18px}
  .chip{background:var(--panel2);border:1px solid var(--line);border-radius:10px;
        padding:9px 11px;cursor:pointer;position:relative;transition:.12s;overflow:hidden}
  .chip:hover{border-color:var(--accent)}
  .chip .num{font-size:10px;color:var(--muted)}
  .chip .nm{font-size:12.5px;font-weight:600;margin-top:1px;word-break:break-word}
  .chip .st{font-size:10.5px;margin-top:6px;text-transform:uppercase;letter-spacing:.5px;font-weight:700}
  .chip .age{font-size:10px;color:var(--muted);margin-top:3px}
  .chip .bar{position:absolute;left:0;top:0;bottom:0;width:4px}
  .s-pass  .bar{background:var(--pass)}    .s-pass  .st{color:var(--pass)}
  .s-fail  .bar{background:var(--fail)}    .s-fail  .st{color:var(--fail)}
  .s-running .bar{background:var(--run);animation:blink 1.2s infinite}
  .s-running{border-color:#35507f;background:rgba(79,140,255,.10)}
  .s-running .st{color:#8fb6ff}
  .s-pending .bar{background:var(--pending)} .s-pending .st{color:var(--muted)}
  .s-stale .bar{background:var(--stale)}   .s-stale .st{color:var(--stale)}
  .s-invalid .bar{background:var(--invalid)} .s-invalid .st{color:var(--invalid)}
  @keyframes blink{0%,100%{opacity:1}50%{opacity:.35}}

  .fail-box{margin:0 16px 14px;padding:10px 12px;background:rgba(255,92,92,.07);
            border:1px solid #5c2f2f;border-radius:9px;font-size:12px}
  .fail-box .h{color:#ff9a9a;font-weight:700;margin-bottom:5px}
  .fail-box code{display:block;color:#f0c2c2;font-family:ui-monospace,monospace;
                 font-size:11.5px;white-space:pre-wrap;line-height:1.45}
  .lastline{margin:0 16px 12px;font-family:ui-monospace,monospace;font-size:11.5px;
            color:#9fd0ff;background:rgba(79,140,255,.07);border:1px solid #2c3f63;
            border-radius:8px;padding:8px 11px;white-space:pre-wrap;word-break:break-all}
  .err{color:#ff9a9a;padding:14px 16px;font-size:13px}
  .empty{color:var(--muted);text-align:center;padding:50px 0;font-size:14px}

  /* modal */
  .modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;z-index:50;
         align-items:center;justify-content:center;padding:24px}
  .modal.on{display:flex}
  .modal .box{background:var(--panel);border:1px solid var(--line);border-radius:12px;
              width:min(1100px,96vw);height:min(80vh,900px);display:flex;flex-direction:column}
  .modal .mh{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;
             align-items:center;gap:12px}
  .modal .mh .t{font-weight:650}
  .modal .mh .p{font-size:11px;color:var(--muted);font-family:monospace;word-break:break-all}
  .modal pre{margin:0;padding:14px 16px;overflow:auto;flex:1;font-size:12px;line-height:1.5;
             font-family:ui-monospace,Menlo,Consolas,monospace;color:#cdd8ee}
  .modal .close{margin-left:auto}
  .hl-err{color:#ff8a8a} .hl-warn{color:#e6c65a}

  .tabs{display:flex;gap:4px}
  .tab{background:var(--panel2);color:var(--muted);border:1px solid var(--line);
       font-weight:600;padding:6px 14px}
  .tab.on{background:var(--accent);border-color:var(--accent);color:#fff}
  .b-SILENT{background:rgba(201,162,39,.16);color:#e6c65a;border:1px solid #6b5a1f}
  .lec-grid{grid-template-columns:repeat(auto-fill,minmax(130px,1fr))}
  .pctbar{height:4px;background:var(--line);border-radius:3px;margin-top:6px;overflow:hidden}
  .pctbar > i{display:block;height:100%;background:var(--run)}
  .warnline{margin:0 16px 12px;font-size:12px;color:#e6c65a;background:rgba(201,162,39,.08);
            border:1px solid #6b5a1f;border-radius:8px;padding:8px 11px}
  .card.clickable .top{cursor:pointer}
</style>
</head>
<body>
<header>
  <h1>APEX <span>Flow Monitor</span></h1>
  <div class="tabs">
    <button class="tab on" id="tabApr" data-tab="apr">APR Flow</button>
    <button class="tab" id="tabLec" data-tab="lec">LEC</button>
  </div>
  <div class="controls">
    <span class="updated"><span id="live" class="dot"></span><span id="updated">not started</span></span>
    <label>Refresh</label>
    <select id="interval">
      <option value="0">Off</option>
      <option value="5">5s</option>
      <option value="10" selected>10s</option>
      <option value="30">30s</option>
      <option value="60">60s</option>
    </select>
    <button id="refresh" class="ghost">Refresh now</button>
  </div>
</header>

<div class="wrap" id="tab-apr">
  <div class="setup" id="setup">
    <h2>Workareas to monitor</h2>
    <p>Paste one path per line &mdash; block root, <code>apr_snps</code>, or <code>apr_snps/logs</code>. Saved in your browser.</p>
    <textarea id="paths" placeholder="/nfs/site/disks/.../r2g/km_noc_vnoc_l_01
/nfs/site/disks/.../r2g/another_block"></textarea>
    <div class="row">
      <button id="start">Start monitoring</button>
      <button id="clear" class="ghost">Clear</button>
      <label>Running window (s)</label>
      <input id="window" type="number" value="240" min="30" step="30" style="width:80px">
      <span class="pill" id="count"></span>
    </div>
  </div>

  <div class="cards" id="cards"></div>
  <div class="empty" id="empty">Add one or more workareas above to begin.</div>
</div>

<div class="wrap" id="tab-lec" style="display:none">
  <div class="setup">
    <h2>LEC runs to monitor</h2>
    <p>Paste one path per line &mdash; block root (<code>.../r2g/&lt;block&gt;</code>), <code>lec/&lt;block&gt;</code>, or <code>lec/&lt;block&gt;/logs</code>. Saved in your browser.</p>
    <textarea id="lpaths" placeholder="/nfs/site/disks/.../r2g/km_noc_vnoc_l_03
/nfs/site/disks/.../r2g/km_noc_vnoc_l_03/lec/km_noc_vnoc_l_03/logs"></textarea>
    <div class="row">
      <button id="lstart">Start monitoring</button>
      <button id="lclear" class="ghost">Clear</button>
      <label>Running window (s)</label>
      <input id="lwindow" type="number" value="1800" min="60" step="60" style="width:80px">
      <label>Silent after (s)</label>
      <input id="lsilent" type="number" value="3600" min="300" step="300" style="width:80px">
      <span class="pill" id="lcount"></span>
    </div>
  </div>
  <div class="cards" id="lcards"></div>
  <div class="empty" id="lempty">Add one or more LEC runs above to begin.</div>
</div>

<div class="modal" id="modal">
  <div class="box">
    <div class="mh">
      <div>
        <div class="t" id="mTitle">log</div>
        <div class="p" id="mPath"></div>
      </div>
      <button class="ghost close" id="mClose">Close</button>
    </div>
    <pre id="mBody">loading...</pre>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const LS = "apex_monitor_paths_v1";
const LS_LEC = "apex_monitor_lec_v1";
const LS_TAB = "apex_monitor_tab_v1";
let timer = null, lastData = null;
let activeTab = localStorage.getItem(LS_TAB) || "apr";

function getPaths(){
  return $("#paths").value.split("\n").map(s=>s.trim()).filter(Boolean);
}
function saveState(){
  localStorage.setItem(LS, JSON.stringify({
    paths: $("#paths").value, interval: $("#interval").value, window: $("#window").value
  }));
  const n = getPaths().length;
  $("#count").textContent = n ? (n + " workarea" + (n>1?"s":"")) : "";
}
function loadState(){
  try{
    const s = JSON.parse(localStorage.getItem(LS)||"{}");
    if(s.paths) $("#paths").value = s.paths;
    if(s.interval) $("#interval").value = s.interval;
    if(s.window) $("#window").value = s.window;
  }catch(e){}
  saveState();
}

function esc(s){return (s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}

function agoStr(sec){
  if(sec==null) return "";
  if(sec<60) return sec+"s ago";
  if(sec<3600) return Math.floor(sec/60)+"m ago";
  if(sec<86400) return Math.floor(sec/3600)+"h ago";
  return Math.floor(sec/86400)+"d ago";
}

function refresh(){ return activeTab==="lec" ? refreshLec() : refreshApr(); }

async function refreshApr(){
  const paths = getPaths();
  $("#empty").style.display = paths.length ? "none" : "block";
  if(!paths.length){ $("#cards").innerHTML=""; return; }
  $("#live").className = "dot live";
  try{
    const r = await fetch("/api/status", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({paths, running_window: parseInt($("#window").value||"240",10)})
    });
    lastData = await r.json();
    render(lastData);
    $("#updated").textContent = "updated " + new Date().toLocaleTimeString();
  }catch(e){
    $("#updated").textContent = "error: " + e;
  }finally{
    setTimeout(()=>{$("#live").className="dot";}, 600);
  }
}

function render(data){
  const cards = data.results.map(cardHTML).join("");
  $("#cards").innerHTML = cards;
}

function cardHTML(w){
  const state = w.state || (w.error ? "ERROR" : "UNKNOWN");
  const title = w.logs_dir ? guessTitle(w.logs_dir) : w.input;
  let body = "";
  if(w.error){
    body = `<div class="err">&#9888; ${esc(w.error)}<br><span class="path">${esc(w.input)}</span></div>`;
    return card(title, w.input, state, 0, "", body, w);
  }
  const chips = w.stages.map(chipHTML).join("");
  // fail detail (for the current failing stage)
  let failBox = "";
  const fs = w.stages.find(s=>s.status==="fail" && s.fail_info);
  if(fs){
    const fi = fs.fail_info;
    const errs = (fi.errors||[]).map(e=>esc(e)).join("\n");
    failBox = `<div class="fail-box"><div class="h">&#10007; ${esc(fs.stage)} failed
       &nbsp;&middot;&nbsp; ${fi.error_count!=null?fi.error_count:"?"} errors,
       ${fi.warning_count!=null?fi.warning_count:"?"} warnings</div>
       <code>${errs||"(see log)"}</code></div>`;
  }
  // last line for running stage
  let lastLine = "";
  const rs = w.stages.find(s=>s.status==="running" && s.last_line);
  if(rs){
    lastLine = `<div class="lastline">&#9656; ${esc(rs.stage)}: ${esc(rs.last_line)}</div>`;
  }
  body = `<div class="grid">${chips}</div>${failBox}${lastLine}`;
  return card(title, w.logs_dir, state, w.progress.pct, metaLine(w), body, w);
}

function metaLine(w){
  const cur = w.current ? `<b>${esc(w.current)}</b>` : "&mdash;";
  return `<div class="meta">
    <span>Current: ${cur}</span>
    <span>Progress: <b>${w.progress.pass}/${w.progress.total}</b> (${w.progress.pct}%)</span>
    <span>Scanned: ${esc(w.scanned_at||"")}</span>
  </div>`;
}

function card(title, path, state, pct, meta, body, w){
  const wid = encodeURIComponent(w.input);
  return `<div class="card" data-input="${wid}">
    <div class="top">
      <div>
        <div class="title">${esc(title)}</div>
        <div class="path">${esc(path||w.input)}</div>
      </div>
      <span class="badge b-${state}">${state}</span>
      <button class="rm" onclick="removeWA('${wid}')">remove</button>
    </div>
    <div class="progress"><i style="width:${pct}%"></i></div>
    ${meta}
    ${body}
  </div>`;
}

function chipHTML(s){
  const age = s.status==="running" ? agoStr(s.log_age) :
              (s.status==="pending" ? "" : (s.status_mtime||s.log_mtime||""));
  const showAge = s.status==="running" ? ("updated "+agoStr(s.log_age)) :
                  (s.status==="pending" ? "not started" : (s.status_mtime||s.log_mtime||""));
  return `<div class="chip s-${s.status}" onclick="openLog(this)"
       data-stage="${esc(s.stage)}" data-log="${s.log?1:0}">
    <span class="bar"></span>
    <div class="num">${esc(String(s.num).padStart(2,"0"))}</div>
    <div class="nm">${esc(s.stage.replace(/^\d+_/,""))}</div>
    <div class="st">${s.status}</div>
    <div class="age">${esc(showAge)}</div>
  </div>`;
}

function guessTitle(logsDir){
  // .../<block>/apr_snps/logs -> block name
  const parts = logsDir.split("/").filter(Boolean);
  const i = parts.indexOf("apr_snps");
  if(i>0) return parts[i-1];
  return parts.slice(-2).join("/");
}

function removeWA(wid){
  const input = decodeURIComponent(wid);
  const lines = getPaths().filter(p=>p!==input);
  $("#paths").value = lines.join("\n");
  saveState(); refresh();
}

async function openLog(chip){
  if(chip.getAttribute("data-log")!=="1") return;
  const cardEl = chip.closest(".card");
  const input = decodeURIComponent(cardEl.getAttribute("data-input"));
  const stage = chip.getAttribute("data-stage");
  $("#modal").classList.add("on");
  $("#mTitle").textContent = stage;
  $("#mPath").textContent = "loading...";
  $("#mBody").textContent = "loading...";
  try{
    const r = await fetch(`/api/log?path=${encodeURIComponent(input)}&stage=${encodeURIComponent(stage)}&n=500`);
    const d = await r.json();
    if(d.error){ $("#mBody").textContent = d.error; return; }
    $("#mPath").textContent = d.path + "   (" + (d.mtime||"") + ")";
    $("#mBody").innerHTML = d.lines.map(colorize).join("\n");
    const pre = $("#mBody"); pre.scrollTop = pre.scrollHeight;
  }catch(e){ $("#mBody").textContent = "error: "+e; }
}
function colorize(line){
  const e = esc(line);
  if(/\b(Error|APEX_ERROR|FATAL)\b/.test(line)) return `<span class="hl-err">${e}</span>`;
  if(/\bWarning\b/i.test(line)) return `<span class="hl-warn">${e}</span>`;
  return e;
}

// ---------------------------------------------------------------- LEC tab
function getLecPaths(){
  return $("#lpaths").value.split("\n").map(s=>s.trim()).filter(Boolean);
}
function saveLecState(){
  localStorage.setItem(LS_LEC, JSON.stringify({
    paths: $("#lpaths").value, window: $("#lwindow").value, silent: $("#lsilent").value
  }));
  const n = getLecPaths().length;
  $("#lcount").textContent = n ? (n + " LEC run" + (n>1?"s":"")) : "";
}
function loadLecState(){
  try{
    const s = JSON.parse(localStorage.getItem(LS_LEC)||"{}");
    if(s.paths) $("#lpaths").value = s.paths;
    if(s.window) $("#lwindow").value = s.window;
    if(s.silent) $("#lsilent").value = s.silent;
  }catch(e){}
  saveLecState();
}

async function refreshLec(){
  const paths = getLecPaths();
  $("#lempty").style.display = paths.length ? "none" : "block";
  if(!paths.length){ $("#lcards").innerHTML=""; return; }
  $("#live").className = "dot live";
  try{
    const r = await fetch("/api/lec_status", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({paths,
        running_window: parseInt($("#lwindow").value||"1800",10),
        silent_window: parseInt($("#lsilent").value||"3600",10)})
    });
    const d = await r.json();
    $("#lcards").innerHTML = d.results.map(lecCardHTML).join("");
    $("#updated").textContent = "updated " + new Date().toLocaleTimeString();
  }catch(e){
    $("#updated").textContent = "error: " + e;
  }finally{
    setTimeout(()=>{$("#live").className="dot";}, 600);
  }
}

function fmtSec(s){
  if(s==null) return "?";
  s = Math.round(s);
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60);
  return h ? `${h}h${String(m).padStart(2,"0")}m` : `${m}m${String(s%60).padStart(2,"0")}s`;
}
function fmtNum(n){ return n==null ? "?" : Number(n).toLocaleString(); }

function lecCardHTML(w){
  const wid = encodeURIComponent(w.input);
  const title = w.block || w.input;
  if(w.error){
    return `<div class="card" data-input="${wid}"><div class="top"><div>
      <div class="title">${esc(title)}</div><div class="path">${esc(w.input)}</div></div>
      <span class="badge b-ERROR">ERROR</span>
      <button class="rm" onclick="removeLec('${wid}')">remove</button></div>
      <div class="err">&#9888; ${esc(w.error)}</div></div>`;
  }
  const state = w.state;
  const nDone = w.phases.filter(p=>p.status==="pass").length;
  const pct = Math.round(100*nDone/w.phases.length);
  const chips = w.phases.map(p=>{
    const isCur = p.status==="running" || p.status==="stale" || p.status==="fail";
    const sub = isCur && w.substep ? w.substep : "";
    const bar = isCur && w.pct!=null ? `<div class="pctbar"><i style="width:${w.pct}%"></i></div>` : "";
    const stTxt = isCur && w.pct!=null ? `${p.status} ${w.pct}%` : p.status;
    return `<div class="chip s-${p.status}" onclick="openLecLog('${wid}')">
      <span class="bar"></span>
      <div class="num">${String(p.num).padStart(2,"0")}</div>
      <div class="nm">${esc(p.label)}</div>
      <div class="st">${esc(stTxt)}</div>
      <div class="age">${esc(sub)}</div>${bar}</div>`;
  }).join("");

  const kp = w.keypts||{}, mp = w.mapped||{};
  const stats = [];
  stats.push(`Current: <b>${esc(w.current)}${w.substep?" &middot; "+esc(w.substep):""}${w.pct!=null?" ("+w.pct+"%)":""}</b>`);
  stats.push(`Threads: <b>${w.threads!=null?w.threads:"?"}</b>`);
  if(kp.golden!=null || kp.revised!=null)
    stats.push(`Key points G/R: <b>${fmtNum(kp.golden)} / ${fmtNum(kp.revised)}</b>`);
  if(mp.DFF || mp.DLAT)
    stats.push(`Mapped by guidance: <b>${fmtNum(mp.DFF)} DFF, ${fmtNum(mp.DLAT)} DLAT</b>`);
  stats.push(`CPU/Elapsed (last report): <b>${fmtSec(w.cpu)} / ${fmtSec(w.elapse)}</b>`);
  stats.push(`Peak mem: <b>${w.mem_peak!=null?(w.mem_peak/1024).toFixed(1)+" GB":"?"}</b>`);
  stats.push(`Log updated: <b>${agoStr(w.log_age)}</b>`);
  const meta = `<div class="meta">${stats.map(s=>`<span>${s}</span>`).join("")}</div>`;

  let extra = "";
  if(state==="SILENT" || state==="IDLE"){
    extra += `<div class="warnline">&#9888; No log update for ${fmtSec(w.log_age)} while in
      <b>${esc(w.current)}</b>. Conformal may still be computing (mapping/compare can be silent
      for hours) or the job may have died &mdash; check the process on its host.</div>`;
  }
  const sm = w.summary;
  if(sm){
    const c = sm.counts||{};
    const cs = Object.keys(c).map(k=>`${k}: ${c[k]}`).join(" &middot; ");
    const errs = (sm.errors||[]).map(esc).join("\n");
    extra += `<div class="fail-box" style="${state==="COMPLETE"?"border-color:#2f6f4d;background:rgba(47,191,113,.07)":""}">
      <div class="h" style="${state==="COMPLETE"?"color:#7ce0a6":""}">lec_summary.rpt: ${esc(sm.status||"?")}
      &nbsp;&middot;&nbsp; ${esc(sm.mtime||"")}</div>
      <code>${cs}${errs?"\n"+errs:""}</code></div>`;
  } else if(w.errors){
    extra += `<div class="fail-box"><div class="h">${w.errors} "// Error:" lines in log</div>
      <code>${(w.first_errors||[]).map(esc).join("\n")}</code></div>`;
  }
  if(w.last_line){
    extra += `<div class="lastline">&#9656; ${esc(w.last_line)}</div>`;
  }
  const badge = w.final_status ? `${state} (${esc(w.final_status)})` : state;
  return `<div class="card" data-input="${wid}">
    <div class="top">
      <div>
        <div class="title">${esc(title)} <span class="pill">${esc(w.task||"")}</span></div>
        <div class="path">${esc(w.log)}</div>
      </div>
      <span class="badge b-${state}">${badge}</span>
      <button class="rm" onclick="removeLec('${wid}')">remove</button>
    </div>
    <div class="progress"><i style="width:${pct}%"></i></div>
    ${meta}
    <div class="grid lec-grid">${chips}</div>
    ${extra}
  </div>`;
}

function removeLec(wid){
  const input = decodeURIComponent(wid);
  $("#lpaths").value = getLecPaths().filter(p=>p!==input).join("\n");
  saveLecState(); refreshLec();
}

async function openLecLog(wid){
  const input = decodeURIComponent(wid);
  $("#modal").classList.add("on");
  $("#mTitle").textContent = "LEC log";
  $("#mPath").textContent = "loading...";
  $("#mBody").textContent = "loading...";
  try{
    const r = await fetch(`/api/lec_log?path=${encodeURIComponent(input)}&n=500`);
    const d = await r.json();
    if(d.error){ $("#mBody").textContent = d.error; return; }
    $("#mTitle").textContent = d.log;
    $("#mPath").textContent = d.path + "   (" + (d.mtime||"") + ")";
    $("#mBody").innerHTML = d.lines.map(colorize).join("\n");
    const pre = $("#mBody"); pre.scrollTop = pre.scrollHeight;
  }catch(e){ $("#mBody").textContent = "error: "+e; }
}

function showTab(t){
  activeTab = t;
  localStorage.setItem(LS_TAB, t);
  $("#tab-apr").style.display = t==="apr" ? "" : "none";
  $("#tab-lec").style.display = t==="lec" ? "" : "none";
  $("#tabApr").classList.toggle("on", t==="apr");
  $("#tabLec").classList.toggle("on", t==="lec");
  refresh();
}

function setInterval_(){
  if(timer) clearInterval(timer);
  const s = parseInt($("#interval").value,10);
  if(s>0) timer = setInterval(refresh, s*1000);
}

$("#start").onclick = ()=>{ saveState(); refresh(); setInterval_(); };
$("#clear").onclick = ()=>{ $("#paths").value=""; saveState(); $("#cards").innerHTML=""; $("#empty").style.display="block"; };
$("#refresh").onclick = refresh;
$("#interval").onchange = ()=>{ saveState(); setInterval_(); };
$("#paths").oninput = saveState;
$("#window").oninput = saveState;
$("#mClose").onclick = ()=> $("#modal").classList.remove("on");
$("#modal").onclick = e=>{ if(e.target.id==="modal") $("#modal").classList.remove("on"); };
document.addEventListener("keydown", e=>{ if(e.key==="Escape") $("#modal").classList.remove("on"); });

$("#lstart").onclick = ()=>{ saveLecState(); refreshLec(); setInterval_(); };
$("#lclear").onclick = ()=>{ $("#lpaths").value=""; saveLecState(); $("#lcards").innerHTML=""; $("#lempty").style.display="block"; };
$("#lpaths").oninput = saveLecState;
$("#lwindow").oninput = saveLecState;
$("#lsilent").oninput = saveLecState;
$("#tabApr").onclick = ()=>showTab("apr");
$("#tabLec").onclick = ()=>showTab("lec");

loadState();
loadLecState();
showTab(activeTab);
setInterval_();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="APEX flow monitor dashboard")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    host = os.uname()[1]
    print("=" * 64)
    print(" APEX Flow Monitor is running")
    print(" Local:   http://localhost:%d" % args.port)
    print(" Network: http://%s:%d" % (host, args.port))
    print(" (Ctrl-C to stop)")
    print("=" * 64)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...")
        httpd.shutdown()


if __name__ == "__main__":
    main()