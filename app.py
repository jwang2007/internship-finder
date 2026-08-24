#!/usr/bin/env python3
"""
Quant intern finder — desktop mode.

Serves the dashboard at http://127.0.0.1:8765, runs finder.py on a schedule (Settings → "Run every"),
on demand ("Run search now"), stores your star / applied / hidden marks and notes, and posts a
macOS notification when a run finds listings it hasn't seen before.

  python app.py                 start (opens the browser)
  python app.py --no-browser    start quietly (what the login item uses)
  python app.py --install-login-item     start automatically at login (macOS)
  python app.py --uninstall-login-item

Standard library only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
DATA_PATH = os.path.join(ROOT, "data", "internships.json")
MARKS_PATH = os.path.join(ROOT, "data", "marks.json")
TEMPLATE_PATH = os.path.join(ROOT, "templates", "dashboard.html")
FINDER = os.path.join(ROOT, "finder.py")
HOST, PORT = "127.0.0.1", 8765
LABEL = "com.quantinternfinder.app"


def now():
    return dt.datetime.now(dt.timezone.utc)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def app_settings():
    return (load_json(CONFIG_PATH, {}) or {}).get("app", {}) or {}


class Runner:
    """Owns the finder subprocess, its log, and the schedule."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.log: list[str] = []
        self.last_started = None
        self.last_finished = None
        self.last_result = ""
        self.next_run = now() + dt.timedelta(minutes=1)
        self.wake = threading.Event()

    def start(self, reason="manual"):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.log = [f"[{reason}] starting…"]
            self.last_started = now()
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self):
        started = now()
        settings = app_settings()
        env = dict(os.environ, PYTHONUNBUFFERED="1", SCHEDULE_HOURS=str(settings.get("schedule_hours", 12)),
                   DASHBOARD_URL=f"http://{HOST}:{PORT}/")
        if settings.get("ntfy_topic"):
            env["NTFY_TOPIC"] = settings["ntfy_topic"]
        if settings.get("discord_webhook_url"):
            env["DISCORD_WEBHOOK_URL"] = settings["discord_webhook_url"]
        code = -1
        try:
            p = subprocess.Popen([sys.executable, FINDER, "-v"], cwd=ROOT, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            for line in p.stderr:
                line = line.rstrip()
                if line and not line.startswith("  enrich:"):
                    self.log.append(line)
                    self.log = self.log[-400:]
            code = p.wait()
        except Exception as e:
            self.log.append(f"could not run finder.py: {e}")
        finally:
            with self.lock:
                self.running = False
                self.last_finished = now()
                hours = max(1, int(app_settings().get("schedule_hours", 12)))
                self.next_run = self.last_finished + dt.timedelta(hours=hours)
        payload = load_json(DATA_PATH, None)
        if code == 0 and payload:
            counts = payload.get("counts", {})
            new = [] if payload.get("first_run") else [
                l for l in payload.get("listings", []) if l.get("first_seen") and
                dt.datetime.fromisoformat(l["first_seen"]) >= started - dt.timedelta(seconds=5)]
            total = counts.get("total", 0)
            when = self.last_finished.astimezone().strftime("%b %d %H:%M")
            self.last_result = f"last run {when} · {total} open · {len(new)} new" + (" (baseline)" if payload.get("first_run") else "")
            self.log.append(f"done: {total} open, {len(new)} new" + (" — first run just records what exists" if payload.get("first_run") else ""))
            if new and settings.get("mac_notifications", True):
                self.notify_mac(new)
        else:
            self.last_result = f"last run failed (exit {code}) — open the log"
            self.log.append(f"finder.py exited with code {code}")

    @staticmethod
    def notify_mac(new):
        if sys.platform != "darwin":
            return
        names = "; ".join(f"{l['company']}: {l['title']}" for l in new[:3])
        if len(new) > 3:
            names += f" … +{len(new) - 3} more"
        title = f"{len(new)} new quant internship{'s' if len(new) != 1 else ''}"
        script = f'display notification "{names.replace(chr(34), chr(39))}" with title "{title}" sound name "Glass"'
        try:
            subprocess.run(["osascript", "-e", script], timeout=10, check=False)
        except Exception:
            pass

    def scheduler(self):
        while True:
            wait = (self.next_run - now()).total_seconds()
            if wait > 0:
                self.wake.wait(timeout=min(wait, 60))
                self.wake.clear()
                continue
            if not self.running:
                self.start("scheduled")
            time.sleep(5)

    def status(self):
        return {
            "running": self.running,
            "log": self.log[-120:],
            "last_started": self.last_started.isoformat() if self.last_started else None,
            "last_finished": self.last_finished.isoformat() if self.last_finished else None,
            "last_result": self.last_result,
            "next_run": self.next_run.isoformat(),
            "schedule_hours": app_settings().get("schedule_hours", 12),
        }


RUNNER = Runner()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the terminal quiet
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(TEMPLATE_PATH, encoding="utf-8") as f:
                return self._send(200, f.read().encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/status":
            return self._send(200, RUNNER.status())
        if path == "/api/data":
            payload = load_json(DATA_PATH, None)
            return self._send(200, payload) if payload else self._send(404, {"error": "no data yet"})
        if path == "/api/marks":
            return self._send(200, load_json(MARKS_PATH, {}))
        if path == "/api/config":
            return self._send(200, load_json(CONFIG_PATH, {}))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/run":
            return self._send(200, {"started": RUNNER.start("manual"), "running": True})
        if path == "/api/marks":
            b = self._body()
            marks = load_json(MARKS_PATH, {})
            if b.get("mark"):
                marks[b["id"]] = b["mark"]
            else:
                marks.pop(b.get("id"), None)
            save_json(MARKS_PATH, marks)
            return self._send(200, {"ok": True})
        if path == "/api/config":
            cfg = self._body()
            if not isinstance(cfg, dict) or "sources" not in cfg:
                return self._send(400, {"error": "config must be the full config.json object"})
            save_json(CONFIG_PATH, cfg)
            hours = max(1, int((cfg.get("app") or {}).get("schedule_hours", 12)))
            base = RUNNER.last_finished or now()
            RUNNER.next_run = min(RUNNER.next_run, base + dt.timedelta(hours=hours)) if RUNNER.last_finished else RUNNER.next_run
            RUNNER.wake.set()
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})


# ----------------------------------------------------------------------------- login item (macOS)
def plist_path():
    return os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def install_login_item():
    if sys.platform != "darwin":
        print("Login items are only set up automatically on macOS; use cron or a systemd user service elsewhere.")
        return
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array><string>{sys.executable}</string><string>{os.path.join(ROOT, 'app.py')}</string><string>--no-browser</string></array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{os.path.join(ROOT, 'data', 'app.log')}</string>
  <key>StandardErrorPath</key><string>{os.path.join(ROOT, 'data', 'app.log')}</string>
</dict></plist>
"""
    os.makedirs(os.path.dirname(plist_path()), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    with open(plist_path(), "w") as f:
        f.write(plist)
    subprocess.run(["launchctl", "unload", plist_path()], capture_output=True)
    subprocess.run(["launchctl", "load", plist_path()], check=False)
    print(f"Installed. The finder now starts at login and stays running in the background.\nDashboard: http://{HOST}:{PORT}/")


def uninstall_login_item():
    if os.path.exists(plist_path()):
        subprocess.run(["launchctl", "unload", plist_path()], capture_output=True)
        os.remove(plist_path())
        print("Removed the login item.")
    else:
        print("No login item installed.")


def main():
    global PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--install-login-item", action="store_true")
    ap.add_argument("--uninstall-login-item", action="store_true")
    args = ap.parse_args()
    if args.install_login_item:
        return install_login_item()
    if args.uninstall_login_item:
        return uninstall_login_item()

    PORT = args.port
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"Port {PORT} is already in use — the app is probably already running. Opening it.")
        if not args.no_browser:
            webbrowser.open(f"http://{HOST}:{PORT}/")
        return
    if not os.path.exists(DATA_PATH):
        RUNNER.next_run = now()  # nothing to show yet: search immediately
    threading.Thread(target=RUNNER.scheduler, daemon=True).start()
    url = f"http://{HOST}:{PORT}/"
    print(f"Quant intern finder running at {url}  (Ctrl-C to stop)")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
