#!/usr/bin/env python3
"""
web_gateway.py

Online BEER: one game page that many players can use at the same time, so a friend
anywhere in the world can play you from their browser, with nothing to install.

    friend's browser --HTTPS--> ngrok --> web_gateway.py --BEER packets--> server.py
    your browser     ------------------------^

Each browser tab is its own player. Behind the scenes every player is a normal BEER
client (the same GameClient class that web_client.py uses) with its own TCP
connection to server.py, sending the same AES-256-CTR encrypted, CRC32-checked
packets. server.py, battleship.py and Protocol.py are not changed.

Why a gateway? Browsers are not allowed to open raw TCP connections, so a web page
cannot speak the BEER protocol itself. The gateway speaks HTTP to the browsers and
BEER to server.py.

How to run on your own computer (each command in its own terminal):
    python3 web_gateway.py                              # game page + server.py
    ngrok http 8080 --url https://<your-dev-domain>     # puts the page on the internet

On a hosting service such as Render (see render.yaml) the start command is just
    python web_gateway.py
The host tells it which port to use through the PORT environment variable.

Options:
    --port N       port for the game page (default 8080, or $PORT when a host sets it)
    --no-server    don't start server.py automatically (use one you started yourself)
"""
import argparse
import gzip
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from web_client import GAME_HOST, GAME_PORT, PAGE, GameClient, QuietServer, find_key_file

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
UI_HOST = "127.0.0.1"        # on your computer only ngrok talks to the gateway directly
DEFAULT_PORT = 8080
MAX_SESSIONS = 40            # player tabs at the same time
MAX_SESSIONS_PER_IP = 8
IDLE_SECONDS = 150           # a tab that stops checking in for this long is logged out
JOINS_PER_MINUTE = 10        # login attempts per internet address per minute
MAX_BODY = 4096              # biggest request body accepted (bytes)
SETTLE_SECONDS = 0.15        # group a burst of server messages into one answer (saves ngrok requests)
SESSION_RE = re.compile(r"^[0-9a-f]{32}$")

ONLINE_PAGE = PAGE.replace("<body>", '<body data-mode="online">', 1).encode("utf-8")
ONLINE_PAGE_GZ = gzip.compress(ONLINE_PAGE, 9)


# ---------------------------------------------------------------------------
# Player sessions (one per browser tab)
# ---------------------------------------------------------------------------
class Sessions:
    """Keeps one GameClient per browser tab. The tab sends its id in X-Beer-Session."""

    def __init__(self, idle_seconds=IDLE_SECONDS):
        self.lock = threading.Lock()
        self.items = {}           # token -> {"client", "seen": last activity, "busy": open requests, "ip"}
        self.joins = {}           # address -> recent login attempt times
        self.idle_seconds = idle_seconds
        self.counts = {"requests": 0, "updates": 0, "actions": 0}  # compare with ngrok's free quota

    def count(self, kind):
        with self.lock:
            self.counts["requests"] += 1
            if kind:
                self.counts[kind] += 1

    def begin(self, token, ip):
        """Find (or create) the player for this tab and mark a request as in progress."""
        with self.lock:
            item = self.items.get(token)
            if item is None:
                if len(self.items) >= MAX_SESSIONS:
                    return None, "The game is full right now. Try again in a few minutes."
                if sum(1 for it in self.items.values() if it["ip"] == ip) >= MAX_SESSIONS_PER_IP:
                    return None, "Too many game tabs are open from your network. Close some and refresh."
                client = GameClient()
                client.mode = "online"
                item = self.items[token] = {"client": client, "seen": time.time(), "busy": 0, "ip": ip}
            item["seen"] = time.time()
            item["busy"] += 1
            return item["client"], None

    def end(self, token):
        with self.lock:
            item = self.items.get(token)
            if item is not None:
                item["busy"] -= 1
                item["seen"] = time.time()

    def allow_join(self, ip):
        now = time.time()
        with self.lock:
            recent = [t for t in self.joins.get(ip, []) if now - t < 60]
            allowed = len(recent) < JOINS_PER_MINUTE
            if allowed:
                recent.append(now)
            self.joins[ip] = recent
            return allowed

    def reap_forever(self):
        while True:
            time.sleep(min(15, self.idle_seconds / 3))
            self.reap()

    def reap(self):
        """Log out tabs that were closed (they stopped checking in)."""
        now = time.time()
        with self.lock:
            stale = [t for t, it in self.items.items()
                     if it["busy"] <= 0 and now - it["seen"] > self.idle_seconds]
            gone = [self.items.pop(t)["client"] for t in stale]
            for ip in [ip for ip, times in self.joins.items() if all(now - t > 60 for t in times)]:
                del self.joins[ip]
        for client in gone:
            client.leave()  # closes that player's BEER connection, like quitting client.py

    def close_all(self):
        with self.lock:
            clients = [it["client"] for it in self.items.values()]
            self.items.clear()
        for client in clients:
            client.leave()


# ---------------------------------------------------------------------------
# The web server that browsers (through ngrok) talk to
# ---------------------------------------------------------------------------
class GatewayHandler(BaseHTTPRequestHandler):
    sessions = None  # set in main()
    server_version = "BEERGateway/1.0"

    def log_message(self, fmt, *args):
        pass  # keep the terminal readable (pages check in constantly)

    def _ip(self):
        forwarded = self.headers.get("X-Forwarded-For")  # set by ngrok: the player's real address
        return forwarded.split(",")[0].strip() if forwarded else self.client_address[0]

    def _send(self, code, data, content_type, gz_ready=None):
        if isinstance(data, str):
            data = data.encode("utf-8")
        wants_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "")
        if wants_gzip and gz_ready is not None:
            data = gz_ready
        elif wants_gzip and len(data) > 1024:
            data = gzip.compress(data, 6)
        else:
            wants_gzip = False
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        if wants_gzip:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, obj if isinstance(obj, str) else json.dumps(obj), "application/json")

    def _client(self):
        """The player for this browser tab. Call self.sessions.end(self.token) when done."""
        self.token = (self.headers.get("X-Beer-Session") or "").strip().lower()
        if not SESSION_RE.match(self.token):
            self._json({"ok": False, "error": "Please refresh the page."}, 400)
            return None
        client, problem = self.sessions.begin(self.token, self._ip())
        if client is None:
            self._json({"ok": False, "error": problem}, 503)
        return client

    def do_GET(self):
        url = urlparse(self.path)
        self.sessions.count("updates" if url.path == "/api/state" and "l=" in url.query else None)
        if url.path in ("/", "/index.html"):
            self._send(200, ONLINE_PAGE, "text/html; charset=utf-8", gz_ready=ONLINE_PAGE_GZ)
        elif url.path == "/healthz":
            c = self.sessions.counts
            self._send(200, f"ok, {len(self.sessions.items)} players, {c['requests']} requests served "
                            f"({c['updates']} page updates, {c['actions']} actions)", "text/plain; charset=utf-8")
        elif url.path == "/api/state":
            client = self._client()
            if client is None:
                return
            query = parse_qs(url.query)

            def number(name, default):
                try:
                    return int(query.get(name, [default])[0])
                except ValueError:
                    return default
            try:
                state = client.wait_for_change(number("v", -1), 25.0, number("l", 0), number("p", 0),
                                               settle=SETTLE_SECONDS)
            finally:
                self.sessions.end(self.token)
            self._json(state)
        else:
            self.send_error(404)

    def do_POST(self):
        self.sessions.count("actions")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self.send_error(415)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.send_error(413)
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        path = urlparse(self.path).path
        if path not in ("/api/join", "/api/solo", "/api/fire", "/api/answer", "/api/forfeit",
                        "/api/leave", "/api/reset"):
            self.send_error(404)
            return
        client = self._client()
        if client is None:
            return
        try:
            self._act(path, client, body)
        finally:
            self.sessions.end(self.token)

    def _act(self, path, client, body):
        if path == "/api/join" and not self.sessions.allow_join(self._ip()):
            self._json({"ok": False, "error": "Too many login attempts. Wait a minute, then try again."})
            return
        actions = {
            "/api/join": lambda: client.join(body.get("username"), body.get("password")),
            "/api/solo": lambda: client.start_solo(body.get("username")),
            "/api/fire": lambda: client.fire(body.get("cell")),
            "/api/answer": lambda: client.answer(bool(body.get("yes"))),
            "/api/forfeit": client.forfeit,
            "/api/leave": client.leave,
            "/api/reset": client.reset,
        }
        ok, error = actions[path]()
        self._json({"ok": ok, "error": error})


# ---------------------------------------------------------------------------
# Starting server.py
# ---------------------------------------------------------------------------
def port_in_use(host, port):
    """True if something is already listening on host:port (e.g. server.py)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def start_game_server():
    """Start server.py (unchanged) in the background, unless it is already running."""
    if port_in_use(GAME_HOST, GAME_PORT):
        print(f"[INFO] Using the game server that is already running on {GAME_HOST}:{GAME_PORT}.")
        return None
    print("[INFO] Starting server.py ...")
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")], cwd=HERE)
    for _ in range(100):
        if port_in_use(GAME_HOST, GAME_PORT) and find_key_file():
            return proc
        if proc.poll() is not None:
            raise SystemExit("[ERROR] server.py stopped straight away. See the messages above.")
        time.sleep(0.1)
    proc.terminate()
    raise SystemExit("[ERROR] server.py didn't start within 10 seconds.")


def stop(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()  # server.py cleans up its key and account files when it stops
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    parser = argparse.ArgumentParser(description="Host BEER Battleships online so friends can play from a browser.")
    parser.add_argument("--port", type=int, default=None,
                        help=f"port for the game page (default {DEFAULT_PORT}, or $PORT when a host sets it)")
    parser.add_argument("--no-server", action="store_true",
                        help="don't start server.py automatically (use one you started yourself)")
    parser.add_argument("--idle", type=int, default=IDLE_SECONDS, help=argparse.SUPPRESS)  # for testing
    args = parser.parse_args()

    # A hosting service (Render) sets PORT and needs the page on all network interfaces.
    hosted = bool(os.environ.get("PORT"))
    host = "0.0.0.0" if hosted else UI_HOST
    port = args.port or (int(os.environ["PORT"]) if hosted else DEFAULT_PORT)

    def stop_on_sigterm(signum, frame):
        raise KeyboardInterrupt  # the host is shutting us down: clean up like Ctrl+C
    signal.signal(signal.SIGTERM, stop_on_sigterm)

    server_proc = None if args.no_server else start_game_server()
    sessions = Sessions(idle_seconds=args.idle)
    GatewayHandler.sessions = sessions
    threading.Thread(target=sessions.reap_forever, daemon=True).start()
    try:
        httpd = QuietServer((host, port), GatewayHandler)
    except OSError as exc:
        stop(server_proc)
        raise SystemExit(f"[ERROR] Port {port} is already in use ({exc}). "
                         f"Try another one, for example: python3 web_gateway.py --port 8090")

    if hosted:
        print(f"[INFO] BEER online is running on port {port} (the hosting service provides HTTPS).", flush=True)
    else:
        print(f"[INFO] BEER online is running. Game page on this computer: http://127.0.0.1:{port}/")
        print(f"[INFO] To let friends play over the internet, run this in a second terminal:")
        print(f"[INFO]     ngrok http {port} --url https://<your-dev-domain>")
        print("[INFO] Press Ctrl+C here to stop the online game (server.py stops too).")
    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n[INFO] Stopping the online game...")
    finally:
        sessions.close_all()
        httpd.server_close()
        stop(server_proc)


if __name__ == "__main__":
    main()
