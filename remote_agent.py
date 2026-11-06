"""
remote_agent.py -- request loop that runs on the Linux host, driven by monitor_bridge.py.

The bridge ships this file appended to apex_monitor.py over one SSH session
(nothing is written to the remote disk), so scan_workarea / scan_lec / read_tail and
friends are already defined in this namespace.

Protocol: one JSON object per line on stdin; one reply per line on stdout, prefixed
with MARK so banner noise printed by remote shell rc files is ignored. Python 3.6+.
"""

import json
import os
import re
import sys
import time
import traceback

MARK = "@@APX "
TAIL_FIRST_BYTES = 400000
TAIL_MAX_BYTES = 2000000


def _stat_or_none(path):
    try:
        return os.stat(path)
    except OSError:
        return None


def tail_file(path, offset=None, ino=None, n=500):
    """Return complete lines appended since `offset`. On first read, after
    truncation, or after the file was replaced, return the last `n` lines."""
    path = os.path.expanduser(path)
    st = _stat_or_none(path)
    if st is None:
        return {"error": "File not found: %s" % path}
    if os.path.isdir(path):
        return {"error": "This is a directory, pick a file inside it.", "is_dir": True}
    size = st.st_size
    reset = offset is None or ino != st.st_ino or offset > size
    if reset:
        start = max(0, size - TAIL_FIRST_BYTES)
    else:
        start = offset
        if size - start > TAIL_MAX_BYTES:
            start, reset = size - TAIL_FIRST_BYTES, True
    try:
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(size - start)
    except OSError as e:
        return {"error": "Cannot read %s: %s" % (path, e)}

    if reset and start > 0:
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl != -1 else b""
        start = size - len(data)
    # Hold back a trailing partial line until it is complete.
    cut = data.rfind(b"\n")
    complete = data[:cut + 1] if cut != -1 else b""
    lines = complete.decode("utf-8", "replace").splitlines()
    if reset:
        lines = lines[-n:]
    return {
        "path": path,
        "lines": lines,
        "reset": reset,
        "offset": start + len(complete),
        "ino": st.st_ino,
        "size": size,
        "mtime": iso(st.st_mtime),
        "age": int(time.time() - st.st_mtime),
    }


def list_dir(path, limit=300):
    path = os.path.expanduser(path)
    try:
        names = os.listdir(path)
    except OSError as e:
        return {"error": "Cannot list %s: %s" % (path, e)}
    now = time.time()
    files = []
    for name in names:
        st = _stat_or_none(os.path.join(path, name))
        if st is None:
            continue
        isdir = os.path.isdir(os.path.join(path, name))
        files.append({"name": name, "dir": isdir, "size": st.st_size,
                      "mtime": iso(st.st_mtime), "age": int(now - st.st_mtime),
                      "ts": st.st_mtime})
    files.sort(key=lambda x: -x["ts"])
    return {"path": path, "entries": files[:limit], "total": len(files)}


def watch_paths(paths):
    """Freshness of each watched file or directory (newest file inside a dir)."""
    now = time.time()
    out = []
    for p in paths:
        full = os.path.expanduser(p.strip())
        st = _stat_or_none(full)
        if st is None:
            out.append({"input": p, "error": "Path not found"})
            continue
        item = {"input": p, "path": full, "kind": "dir" if os.path.isdir(full) else "file"}
        if item["kind"] == "file":
            item.update(newest=os.path.basename(full), mtime=iso(st.st_mtime),
                        age=int(now - st.st_mtime), size=st.st_size)
        else:
            best = None
            try:
                for name in os.listdir(full):
                    fp = os.path.join(full, name)
                    s = _stat_or_none(fp)
                    if s is None or os.path.isdir(fp):
                        continue
                    if best is None or s.st_mtime > best[1].st_mtime:
                        best = (name, s)
            except OSError as e:
                item["error"] = "Cannot list: %s" % e
            if best:
                item.update(newest=best[0], mtime=iso(best[1].st_mtime),
                            age=int(now - best[1].st_mtime), size=best[1].st_size)
            else:
                item.update(newest=None, mtime=None, age=None, size=None)
        out.append(item)
    return {"results": out}


def scan_many(paths, fn, *args):
    results = []
    for p in paths:
        if not p or not p.strip():
            continue
        try:
            results.append(fn(p, *args))
        except Exception as e:
            results.append({"input": p, "resolved": False, "error": "scan error: %s" % e,
                            "stages": [], "phases": []})
    return {"results": results, "server_time": iso(time.time())}


FLOW_RESOLVERS = [("apr", lambda p: resolve_logs_dir(p)),
                  ("lec", lambda p: resolve_lec_logs_dir(p))]
FLOW_NAMES = {"apr": "an APR workarea", "lec": "a LEC run"}


def detect_type(path, flow=None):
    """Return (type, hint). Only `flow`'s own resolver is tried before falling back
    to a plain log file/folder; another flow's match becomes a hint instead."""
    if not os.path.exists(os.path.expanduser(path.strip())):
        return None, "Path not found on the host"
    for name, resolve in FLOW_RESOLVERS:
        if (flow is None or name == flow) and resolve(path):
            return name, None
    if flow is not None:
        for name, resolve in FLOW_RESOLVERS:
            if name != flow and resolve(path):
                return None, "This is %s; add it in the %s tab" % (FLOW_NAMES[name], name.upper())
    return "log", None


# Anchored like the flows' own error_patterns ('^Error|^INTEL_ERROR|^Fatal|^INTEL_FATAL'),
# allowing a tool prompt ("fc_shell> ") or Conformal's "// " in front.
_LEAD = r"^\s*(?://\s*|[\w.-]+>\s*)?"
ERR_RX = re.compile(_LEAD + r"(Error|ERROR|INTEL_ERROR|APEX_ERROR|Fatal|FATAL|INTEL_FATAL)\b")
WARN_RX = re.compile(_LEAD + r"(Warning|WARNING|INTEL_WARNING)\b")
ZERO_RX = re.compile(r"(?i)\b(errors?|warnings?)\s*(count)?\s*[:=]?\s*0\b")
CODE_RX = re.compile(r"\(([A-Z][A-Z0-9_]*-\d+)\)\s*$")
NORM_RX = [(re.compile(r"'[^']*'"), "'*'"), (re.compile(r'"[^"]*"'), '"*"'),
           (re.compile(r"\{[^}]*\}"), "{*}"), (re.compile(r"\b0x[0-9a-fA-F]+\b"), "#"),
           (re.compile(r"\d+(\.\d+)?"), "#")]


def _message_key(line):
    m = CODE_RX.search(line)
    if m:
        return m.group(1)
    key = line
    for rx, rep in NORM_RX:
        key = rx.sub(rep, key)
    return key[:240]


def grep_file(path, kind, max_groups=300, max_hits=200):
    """Whole-file scan for error or warning lines, grouped by message (first seen first)."""
    path = os.path.expanduser(path)
    rx = ERR_RX if kind == "error" else WARN_RX
    needles = (b"rror", b"RROR", b"atal", b"ATAL") if kind == "error" else (b"arning", b"ARNING")
    groups, order, total, lineno = {}, [], 0, 0
    try:
        with open(path, "rb") as f:
            for raw in f:
                lineno += 1
                if not any(n in raw for n in needles):
                    continue
                line = raw.decode("utf-8", "replace").rstrip("\r\n").strip()
                if not rx.search(line) or ZERO_RX.search(line):
                    continue
                total += 1
                key = _message_key(line)
                g = groups.get(key)
                if g is None:
                    if len(groups) >= max_groups:
                        continue
                    g = groups[key] = {"text": line[:400], "count": 0, "lines": []}
                    order.append(key)
                g["count"] += 1
                if len(g["lines"]) < max_hits:
                    g["lines"].append(lineno)
    except OSError as e:
        return {"error": "Cannot read %s: %s" % (path, e)}
    return {"path": path, "kind": kind, "total": total, "line_count": lineno,
            "groups": [groups[k] for k in order]}


def read_range(path, start, count):
    path = os.path.expanduser(path)
    start = max(1, int(start))
    out, lineno = [], 0
    try:
        with open(path, "rb") as f:
            for raw in f:
                lineno += 1
                if lineno < start:
                    continue
                if lineno >= start + count:
                    break
                out.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
    except OSError as e:
        return {"error": "Cannot read %s: %s" % (path, e)}
    return {"path": path, "start": start, "lines": out}


def scan_items(items, apr_window, lec_window, lec_silent):
    """Scan a list of {id, path, type, flow, list}; type "auto" is detected first."""
    out = []
    for it in items:
        path = it.get("path", "")
        kind = it.get("type") or "auto"
        try:
            if kind == "auto":
                kind, hint = detect_type(path, it.get("flow"))
                if kind is None:
                    out.append({"id": it.get("id"), "type": "auto", "error": hint})
                    continue
            if kind == "apr":
                res = scan_workarea(path, apr_window)
            elif kind == "lec":
                res = scan_lec(path, lec_window, lec_silent)
            else:
                res = watch_paths([path])["results"][0]
                if it.get("list") and res.get("kind") == "dir":
                    res["listing"] = list_dir(res["path"])
        except Exception as e:
            res = {"error": "scan error: %s" % e}
        res["id"] = it.get("id")
        res["type"] = kind
        out.append(res)
    return {"results": out, "server_time": iso(time.time())}


def handle(req):
    op = req.get("op")
    if op == "ping":
        return {"pong": True, "time": iso(time.time())}
    if op == "scan":
        return scan_items(req.get("items", []),
                          int(req.get("apr_window", DEFAULT_RUNNING_WINDOW)),
                          int(req.get("lec_window", 1800)),
                          int(req.get("lec_silent", DEFAULT_LEC_SILENT)))
    if op == "apr_status":
        return scan_many(req.get("paths", []), scan_workarea,
                         int(req.get("running_window", DEFAULT_RUNNING_WINDOW)))
    if op == "lec_status":
        return scan_many(req.get("paths", []), scan_lec,
                         int(req.get("running_window", 1800)),
                         int(req.get("silent_window", DEFAULT_LEC_SILENT)))
    if op == "tail":
        return tail_file(req["path"], req.get("offset"), req.get("ino"), int(req.get("n", 500)))
    if op == "list":
        return list_dir(req["path"])
    if op == "grep":
        return grep_file(req["path"], req.get("kind", "error"))
    if op == "range":
        return read_range(req["path"], req.get("start", 1), min(int(req.get("count", 80)), 2000))
    if op == "watch":
        return watch_paths(req.get("paths", []))
    return {"error": "unknown op: %s" % op}


def _reply(obj):
    sys.stdout.write(MARK + json.dumps(obj) + "\n")
    sys.stdout.flush()


def serve():
    _reply({"id": 0, "ready": True, "host": os.uname()[1],
            "python": sys.version.split()[0], "user": os.environ.get("USER", "")})
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        rid = None
        try:
            req = json.loads(line)
            rid = req.get("id")
            res = handle(req)
        except Exception as e:
            res = {"error": "agent error: %s" % e, "trace": traceback.format_exc()[-1500:]}
        res["id"] = rid
        _reply(res)


serve()
