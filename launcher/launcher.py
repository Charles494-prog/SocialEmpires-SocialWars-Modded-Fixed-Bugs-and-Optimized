"""Social Point launcher: a small local page to pick a game, which starts that game's server on demand.

Run with:  py launcher\\launcher.py   (or double-click "Play Social Point.bat")
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 5045  # not 5040: Windows' Connected Devices Platform service listens there

LAUNCHER_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(LAUNCHER_DIR)

GAMES = {
    "socialemperors": {
        "name": "Social Empires",
        "dir": os.path.join(ROOT_DIR, "socialemperors-main"),
        "port": 5050,
    },
    "socialwarriors": {
        "name": "Social Wars",
        "dir": os.path.join(ROOT_DIR, "socialwarriors-main"),
        "port": 5055,
    },
}

STARTUP_TIMEOUT = 60  # seconds to wait for a game server to accept connections

processes = {}  # game id -> Popen of servers started by this launcher
lock = threading.Lock()


def port_open(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=0.3):
            return True
    except OSError:
        return False


def launcher_already_running(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "api/status", timeout=2) as r:
            return "games" in json.loads(r.read())
    except (OSError, ValueError):
        return False


def missing_modules() -> list:
    missing = []
    for module in ("flask", "jsonpatch"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    return missing


def game_status(game_id: str) -> dict:
    game = GAMES[game_id]
    proc = processes.get(game_id)
    owned = proc is not None and proc.poll() is None
    return {
        "id": game_id,
        "name": game["name"],
        "url": f"http://{HOST}:{game['port']}/",
        "installed": os.path.isfile(os.path.join(game["dir"], "server.py")),
        "running": port_open(game["port"]),
        "owned": owned,  # started by this launcher, so it can be stopped from here
    }


def start_game(game_id: str) -> dict:
    game = GAMES[game_id]
    with lock:
        if port_open(game["port"]):
            return {"ok": True, **game_status(game_id)}
        if not os.path.isfile(os.path.join(game["dir"], "server.py")):
            return {"ok": False, "error": f"Could not find {game['dir']}\\server.py"}
        missing = missing_modules()
        if missing:
            return {"ok": False, "error": "Missing Python packages: " + ", ".join(missing)
                    + ". Run:  py -m pip install " + " ".join(missing)}
        # Own console window, so the server log is visible and it can be closed like before
        flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
        processes[game_id] = subprocess.Popen([sys.executable, "server.py"], cwd=game["dir"], creationflags=flags)

    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if port_open(game["port"]):
            return {"ok": True, **game_status(game_id)}
        if processes[game_id].poll() is not None:
            return {"ok": False, "error": f"The {game['name']} server closed while starting. Check its window for the error."}
        time.sleep(0.25)
    return {"ok": False, "error": f"The {game['name']} server did not start within {STARTUP_TIMEOUT} seconds."}


def find_server_pid(port: int):
    """PID of the python process running this game's server.py on `port`, or None (Windows only)."""
    if os.name != "nt":
        return None
    script = (
        f"$c = Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; "
        "if ($c) { $p = Get-CimInstance Win32_Process -Filter \"ProcessId=$($c.OwningProcess)\"; "
        "$p.ProcessId; $p.Name; $p.CommandLine; (Get-Process -Id $p.ProcessId).Path }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                             timeout=15, creationflags=subprocess.CREATE_NO_WINDOW).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if len(out) < 3 or not out[0].strip().isdigit():
        return None
    pid, name, cmdline = int(out[0]), out[1].lower(), out[2]
    # Only ever stop a Python process running server.py, never some other program using the port
    if not name.startswith("python") or "server.py" not in cmdline:
        return None
    return pid


def stop_game(game_id: str) -> dict:
    game = GAMES[game_id]
    with lock:
        proc = processes.pop(game_id, None)
        if proc is not None and proc.poll() is None:
            # Started by the launcher: kill its whole tree (the py.exe wrapper and python.exe)
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                proc.terminate()
        elif port_open(game["port"]):
            # Started some other way (e.g. "py server.py" in a command window)
            pid = find_server_pid(game["port"])
            if pid is None:
                return {"ok": False, "error": f"Could not find the {game['name']} server process. Close its window to stop it."}
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.time() + 10
        while port_open(game["port"]) and time.time() < deadline:
            time.sleep(0.2)
    return {"ok": not port_open(game["port"]), **game_status(game_id)}


STATIC_FILES = {
    "/": (os.path.join(LAUNCHER_DIR, "launcher.html"), "text/html; charset=utf-8"),
    "/img/socialemperors.png": (os.path.join(GAMES["socialemperors"]["dir"], "templates", "img", "logo.png"), "image/png"),
    "/img/socialwarriors.png": (os.path.join(GAMES["socialwarriors"]["dir"], "templates", "img", "logo.png"), "image/png"),
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: dict):
        self._send(code, json.dumps(data).encode("utf-8"), "application/json")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            return self._json(200, {"games": [game_status(g) for g in GAMES]})
        if path in STATIC_FILES:
            file, content_type = STATIC_FILES[path]
            try:
                with open(file, "rb") as f:
                    return self._send(200, f.read(), content_type)
            except OSError:
                pass
        self._send(404, b"Not found", "text/plain")

    def do_POST(self):
        # Only the launcher page sends this header; other websites can't add it without a CORS
        # preflight, which this server never answers, so they can't start or stop games.
        if self.headers.get("X-Launcher") != "1":
            return self._json(403, {"ok": False, "error": "Forbidden"})
        parts = self.path.strip("/").split("/")  # api/<start|stop>/<game>
        if len(parts) != 3 or parts[0] != "api" or parts[2] not in GAMES:
            return self._json(404, {"ok": False, "error": "Not found"})
        if parts[1] == "start":
            return self._json(200, start_game(parts[2]))
        if parts[1] == "stop":
            return self._json(200, stop_game(parts[2]))
        self._json(404, {"ok": False, "error": "Not found"})

    def log_message(self, format, *args):
        pass  # keep the launcher window quiet


def main():
    url = f"http://{HOST}:{PORT}/"
    if port_open(PORT):
        if launcher_already_running(url):
            print(f"Launcher already running, opening {url}")
            webbrowser.open(url)
            return
        print(f" Port {PORT} is used by another program. Change PORT at the top of launcher\\launcher.py.")
        sys.exit(1)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f" Social Point launcher running at {url}")
    print(" Keep this window open while playing. Close it (or press Ctrl+C) to quit the launcher.")
    print(" Game servers keep running in their own windows until you close them.")
    threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
