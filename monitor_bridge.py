#!/usr/bin/env python3
"""
monitor_bridge.py -- desktop helper for the SSH flow & log monitor.

Holds one SSH session (Windows OpenSSH ssh.exe, key auth) to the Linux host,
ships apex_monitor.py + remote_agent.py over it, and serves monitor.html
on http://127.0.0.1:8899 for Chrome. Settings and categories live in
monitor_config.json next to this file. Stdlib only.

Usage:
    python monitor_bridge.py [--port 8899] [--open]

With --open it also opens the page in its own browser window and ties the two
together: closing the window stops the helper, and stopping the helper closes the window.
"""

import argparse
import atexit
import base64
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
APEX_SRC = os.path.join(HERE, "apex_monitor.py")
AGENT_SRC = os.path.join(HERE, "remote_agent.py")
PAGE = os.path.join(HERE, "monitor.html")
CONFIG = os.path.join(HERE, "monitor_config.json")
IMAGES = os.path.join(HERE, "docs", "images")
MARK = "@@APX "

# Single-quoted so it survives both bash and csh login shells (no ! or $ inside).
BOOTSTRAP = ("'import sys,base64;src=base64.b64decode(sys.stdin.readline());"
             "exec(compile(src,\"agent\",\"exec\"),{\"__name__\":\"agent\"})'")


SSH_DIR = os.path.join(os.path.expanduser("~"), ".ssh")
KEY_NAMES = ("id_ed25519", "id_rsa", "id_ecdsa")
SAFE_ARG = re.compile(r"^[\w.@-]+$")


def find_key():
    for name in KEY_NAMES:
        fp = os.path.join(SSH_DIR, name)
        if os.path.isfile(fp):
            return fp.replace("\\", "/")
    return ""


def key_info():
    key = find_key()
    pub = ""
    if key and os.path.isfile(key + ".pub"):
        with open(key + ".pub", encoding="utf-8", errors="replace") as f:
            pub = f.read().strip()
    return {"key": key, "pub": pub, "ssh_dir": SSH_DIR.replace("\\", "/")}


def create_key():
    """Make a new ed25519 key without a passphrase (BatchMode can't prompt for one)."""
    if find_key():
        return key_info()
    keygen = shutil.which("ssh-keygen") or r"C:\Windows\System32\OpenSSH\ssh-keygen.exe"
    os.makedirs(SSH_DIR, exist_ok=True)
    target = os.path.join(SSH_DIR, "id_ed25519")
    comment = "flow-monitor %s@%s" % (os.environ.get("USERNAME", "user"), socket.gethostname())
    res = subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", target],
                         capture_output=True, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if res.returncode != 0:
        raise AgentError("ssh-keygen failed: %s" % (res.stderr or res.stdout).strip())
    return key_info()


def install_key_window(host, user, port, key):
    """Open a visible PowerShell window that appends the public key to the host's
    authorized_keys; the user types their Linux password there, never in the page."""
    for v in (host, user, str(port)):
        if not SAFE_ARG.match(v or ""):
            raise AgentError("Fill in a valid host and user first.")
    pub = (key or find_key()) + ".pub"
    if not os.path.isfile(pub):
        raise AgentError("No public key found. Create a key first.")
    remote = ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys"
              " && chmod 600 ~/.ssh/authorized_keys && echo Key installed.")
    ps = ("Write-Host 'Installing your SSH key on %s. Type your Linux password when asked.';"
          " Get-Content -Raw '%s' | ssh -p %s %s@%s '%s';"
          " Write-Host ''; Write-Host 'You can close this window and press Connect in Flow monitor.'"
          % (host, pub.replace("'", "''"), port, user, host, remote))
    subprocess.Popen(["powershell", "-NoProfile", "-NoExit", "-Command", ps],
                     creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    return {"ok": True}


DEFAULT_SOUND = r"C:\Windows\Media\Alarm01.wav"
CONFIG_LOCK = threading.Lock()


def read_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def write_config(update):
    """Merge `update` into monitor_config.json, keeping keys the page doesn't edit
    (sound, alarm_minutes). Written via a temp file so a crash can't truncate it."""
    with CONFIG_LOCK:
        c = read_config()
        c.update(update)
        tmp = CONFIG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(c, f, indent=2)
        os.replace(tmp, CONFIG)
    return c


def page_state():
    c = read_config()
    return {"config": c, "exists": os.path.isfile(CONFIG),
            "defaults": {"key": find_key(), "port": 22, "python": "python3",
                         "alarm_minutes": int(c.get("alarm_minutes") or 20)}}


class AgentError(Exception):
    pass


class SSHSession:
    """One long-lived ssh.exe process speaking JSON lines with remote_agent.py."""

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.connect_lock = threading.Lock()
        self.replies = queue.Queue()
        self.stderr_tail = []
        self.next_id = 1
        self.info = {}
        self.target = {}
        self.state = "disconnected"
        self.error = None

    # -- lifecycle -----------------------------------------------------------
    def connect(self, host, user, key, port=22, python="python3", timeout=40):
        # Several tabs (or a tab and a retry) may connect at once; serialise them and
        # reuse a healthy session to the same target instead of killing it.
        with self.connect_lock:
            want = {"host": host, "user": user, "key": key, "port": port, "python": python}
            if self.status()["state"] == "connected" and self.target == want:
                return self.status()
            return self._connect(host, user, key, port, python, timeout)

    def _connect(self, host, user, key, port, python, timeout):
        self.close()
        ssh = shutil.which("ssh") or r"C:\Windows\System32\OpenSSH\ssh.exe"
        if not host:
            raise AgentError("Enter a host to connect to.")
        if key and not os.path.isfile(os.path.expanduser(key)):
            raise AgentError("Key file not found: %s" % key)
        target = "%s@%s" % (user, host) if user else host
        cmd = [ssh, "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
               "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
               "-o", "StrictHostKeyChecking=accept-new", "-p", str(int(port or 22))]
        if key:
            cmd += ["-i", os.path.expanduser(key)]
        cmd += [target, "%s -u -c %s" % (python or "python3", BOOTSTRAP)]

        with open(APEX_SRC, encoding="utf-8") as f:
            src = f.read()
        with open(AGENT_SRC, encoding="utf-8") as f:
            src += "\n\n" + f.read()
        payload = base64.b64encode(src.encode("utf-8")) + b"\n"

        self.state, self.error = "connecting", None
        self.target = {"host": host, "user": user, "key": key, "port": port, "python": python}
        self.replies = queue.Queue()
        self.stderr_tail = []
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, creationflags=flags)
        proc = self.proc
        threading.Thread(target=self._read_stdout, args=(proc, self.replies), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True).start()
        try:
            proc.stdin.write(payload)
            proc.stdin.flush()
            ready = self._wait(0, timeout)
        except (OSError, AgentError) as e:
            msg = self._explain(str(e))
            self.close()
            self.state, self.error = "disconnected", msg
            raise AgentError(msg)
        self.info = {k: ready.get(k) for k in ("host", "python", "user")}
        self.state = "connected"
        return self.status()

    def close(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.stdin.close()
            except OSError:
                pass
            try:
                p.terminate()
            except OSError:
                pass
        self.state = "disconnected"

    def status(self):
        alive = self.proc is not None and self.proc.poll() is None
        if self.state == "connected" and not alive:
            self.state = "disconnected"
            self.error = self._explain("SSH session ended.")
        return {"state": self.state, "error": self.error, "target": self.target,
                "remote": self.info if self.state == "connected" else {}}

    # -- I/O -----------------------------------------------------------------
    def _read_stdout(self, proc, q):
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith(MARK):
                try:
                    q.put(json.loads(line[len(MARK):]))
                except ValueError:
                    pass
        q.put(None)

    def _read_stderr(self, proc):
        for raw in iter(proc.stderr.readline, b""):
            self.stderr_tail.append(raw.decode("utf-8", "replace").strip())
            del self.stderr_tail[:-20]

    def _wait(self, rid, timeout):
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise AgentError("Timed out waiting for the remote host.")
            try:
                msg = self.replies.get(timeout=left)
            except queue.Empty:
                raise AgentError("Timed out waiting for the remote host.")
            if msg is None:
                raise AgentError("SSH session ended.")
            if msg.get("id") == rid:
                return msg

    def _explain(self, base):
        # Keep ssh's own complaints; drop login banners printed on stderr.
        useful = re.compile(r"(?i)ssh|denied|refused|timed out|not found|host key|resolve|error|python")
        err = " ".join(s for s in self.stderr_tail if s and not s.startswith("*") and useful.search(s))[-600:]
        if "Permission denied" in err:
            return ("SSH refused the key (permission denied). Check the user name, or press "
                    "'Install key on host' in Settings to add your key to the Linux host.")
        if "Could not resolve hostname" in err:
            return "Host name not found. Check the host field and that VPN is on."
        if "timed out" in err.lower():
            return "Connection timed out. Check VPN / network access to the host."
        if "not found" in err and "python" in err:
            return "Remote Python not found. Set the remote Python command (e.g. /usr/bin/python3)."
        return "%s %s" % (base, err) if err else base

    def call(self, req, timeout=90):
        if self.status()["state"] != "connected":
            raise AgentError("Not connected.")
        with self.lock:
            rid = self.next_id
            self.next_id += 1
            req = dict(req, id=rid)
            try:
                self.proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
                return self._wait(rid, timeout)
            except (OSError, AgentError) as e:
                if self.proc is None or self.proc.poll() is not None:
                    self.state, self.error = "disconnected", self._explain(str(e))
                raise AgentError(str(e))


SESSION = SSHSession()


class Handler(BaseHTTPRequestHandler):
    server_version = "FlowMonitorBridge/1.0"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, (bytes, bytearray)):
            body = (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}

    def _local_only(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._local_only():
            return self._send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/monitor.html"):
            with open(PAGE, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path.startswith("/docs/images/"):
            name = os.path.basename(path)
            fp = os.path.join(IMAGES, name)
            if re.match(r"^[\w.-]+\.png$", name) and os.path.isfile(fp):
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), "image/png")
            return self._send(404, {"error": "not found"})
        if path == "/api/state":
            return self._send(200, page_state())
        if path == "/api/keyinfo":
            return self._send(200, key_info())
        if path == "/api/conn":
            return self._send(200, SESSION.status())
        if path == "/api/sound":
            snd = read_config().get("sound") or DEFAULT_SOUND
            if snd and os.path.isfile(snd):
                ctype = "audio/mpeg" if snd.lower().endswith(".mp3") else "audio/wav"
                with open(snd, "rb") as f:
                    return self._send(200, f.read(), ctype)
            return self._send(404, {"error": "sound file not found"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._local_only():
            return self._send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        try:
            body = self._body()
        except ValueError as e:
            return self._send(400, {"error": "bad request: %s" % e})
        try:
            if path == "/api/connect":
                return self._send(200, SESSION.connect(
                    body.get("host", "").strip(), body.get("user", "").strip(),
                    body.get("key", "").strip(), body.get("port") or 22,
                    body.get("python", "").strip() or "python3"))
            if path == "/api/state":
                if not isinstance(body, dict):
                    return self._send(400, {"error": "expected an object"})
                write_config(body)
                return self._send(200, {"ok": True})
            if path == "/api/keygen":
                return self._send(200, create_key())
            if path == "/api/keyinstall":
                return self._send(200, install_key_window(
                    body.get("host", "").strip(), body.get("user", "").strip(),
                    body.get("port") or 22, body.get("key", "").strip()))
            if path == "/api/shutdown":
                self._send(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path == "/api/disconnect":
                SESSION.close()
                SESSION.error = None
                return self._send(200, SESSION.status())
            if path == "/api/call":
                op = body.get("op")
                if op not in ("ping", "scan", "apr_status", "lec_status", "tail", "list", "watch",
                              "grep", "range"):
                    return self._send(400, {"error": "unknown op"})
                return self._send(200, SESSION.call(body))
        except AgentError as e:
            return self._send(503, {"error": str(e), "conn": SESSION.status()})
        self._send(404, {"error": "not found"})


APP_PROFILE = os.path.join(os.environ.get("LOCALAPPDATA") or HERE, "FlowMonitor", "browser")


def find_browser():
    """Chrome first, then Edge: both support --app windows."""
    pf, pf86, local = (os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"))
    candidates = [os.path.join(b, r"Google\Chrome\Application\chrome.exe") for b in (pf, pf86, local) if b]
    try:
        import winreg
        for exe in ("chrome.exe", "msedge.exe"):
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    key = winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\\" + exe)
                    candidates.append(winreg.QueryValue(key, None))
                except OSError:
                    pass
    except ImportError:
        pass
    candidates += [os.path.join(b, r"Microsoft\Edge\Application\msedge.exe") for b in (pf86, pf) if b]
    for c in candidates:
        if c and os.path.isfile(c.strip('"')):
            return c.strip('"')
    return None


def tie_to_this_process(pid):
    """Put `pid` in a Windows job that is killed when this process ends, however it ends
    (window closed, crash, Task Manager). Returns the job handle, which must stay open."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_ulonglong * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JobObjectExtendedLimitInformation = 0x2000, 9
    PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001
    job = k32.CreateJobObjectW(None, None)
    info = Extended()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    proc = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    ok = (job and proc
          and k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info))
          and k32.AssignProcessToJobObject(job, proc))
    if proc:
        k32.CloseHandle(proc)
    if not ok:
        print("Note: couldn't tie the monitor window to this helper (error %d)." % ctypes.get_last_error())
    return job


class AppWindow:
    """The page in its own browser window. A dedicated profile makes the browser
    process stay alive exactly as long as the window, so we can watch and close it."""

    def __init__(self):
        self.proc = None
        self.job = None
        self.started = 0

    def open(self, url, on_closed):
        exe = find_browser()
        if not exe:
            import webbrowser
            webbrowser.open(url)
            print("No Chrome or Edge found; opened your default browser instead.")
            return
        os.makedirs(APP_PROFILE, exist_ok=True)
        self.proc = subprocess.Popen([exe, "--app=" + url, "--user-data-dir=" + APP_PROFILE,
                                      "--no-first-run", "--no-default-browser-check",
                                      "--window-size=1400,900", "--hide-crash-restore-bubble",
                                      "--autoplay-policy=no-user-gesture-required"])
        self.started = time.time()
        self.job = tie_to_this_process(self.proc.pid)
        threading.Thread(target=self._watch, args=(self.proc, on_closed), daemon=True).start()

    def _watch(self, proc, on_closed):
        proc.wait()
        if proc is not self.proc:
            return
        if time.time() - self.started < 5:
            # The browser handed the window to an already-running instance; we can't track it.
            print("The monitor window is managed by another browser instance; close this window to stop.")
            return
        print("Monitor window closed - stopping.")
        on_closed()

    def close(self):
        p, self.proc = self.proc, None
        if not p or p.poll() is not None:
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # Ask the main browser process to close its window (like pressing X); force it if that fails.
        subprocess.run(["taskkill", "/PID", str(p.pid)], capture_output=True, creationflags=flags)
        try:
            p.wait(2)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, creationflags=flags)


APP = AppWindow()


def cleanup():
    APP.close()
    SESSION.close()


def on_console_close():
    """Closing the console window kills Python after a few seconds without running
    finally blocks, so close the browser window from a console control handler."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    def handler(event):
        if event in (2, 5, 6):  # close, logoff, shutdown
            cleanup()
        return False

    on_console_close.handler = handler  # keep a reference so it isn't garbage collected
    ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows SO_REUSEADDR lets a second server bind the same port and silently
    # leaves the old one answering, so ask for exclusive use instead.
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def bind_replacing_old(port):
    """Bind the port; if an older bridge holds it, ask it to shut down first."""
    try:
        return Server(("127.0.0.1", port), Handler)
    except OSError:
        pass
    print("Another monitor bridge is running on port %d - replacing it." % port)
    try:
        urlopen(Request("http://127.0.0.1:%d/api/shutdown" % port, data=b"{}", method="POST"), timeout=5)
    except Exception:
        pass
    for _ in range(20):
        time.sleep(0.5)
        try:
            return Server(("127.0.0.1", port), Handler)
        except OSError:
            continue
    print("Port %d is still in use. Close the old 'Flow monitor bridge' window and try again." % port)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="SSH flow & log monitor bridge")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--open", action="store_true", help="open the page in its own window and stop when it closes")
    args = ap.parse_args()
    httpd = bind_replacing_old(args.port)
    atexit.register(cleanup)
    on_console_close()
    if args.open:
        APP.open("http://127.0.0.1:%d/" % args.port,
                 lambda: threading.Thread(target=httpd.shutdown, daemon=True).start())
    print("=" * 60)
    print(" SSH Flow Monitor bridge")
    print(" Open:  http://127.0.0.1:%d" % args.port)
    print(" Close the monitor window, or this window, to stop both.")
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()


if __name__ == "__main__":
    main()
