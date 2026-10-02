#!/usr/bin/env python3
"""
web_client.py

Browser UI for BEER (Battleships: Engage in Explosive Rivalry).

This is a BEER client, just like client.py: it connects to server.py over TCP and
exchanges the same AES-256-CTR encrypted BEER packets, using the functions in
Protocol.py. The only difference is the front end. Instead of typing into a
terminal, you play on a web page that this script serves on your own computer:

    browser tab  <-- HTTP, this computer only -->  web_client.py  <-- BEER packets -->  server.py

server.py, battleship.py and Protocol.py are not changed.
(To play someone on another computer over the internet, use web_gateway.py.)

How to play (each command in its own terminal, from this project folder):
    python3 server.py        # 1. the game server (it creates .battleship_key)
    python3 web_client.py    # 2. player one -> opens a tab on http://127.0.0.1:8001
    python3 web_client.py    # 3. player two -> opens a tab on http://127.0.0.1:8002

Options:
    --port N        serve the page on port N instead of the first free port from 8001
    --no-browser    don't open a browser tab automatically (open the printed address yourself)
"""
import argparse
import json
import os
import random
import re
import select
import socket
import struct
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from Protocol import (KeyManager, MessageType, receive_encrypted_packet,
                      send_encrypted_packet)
from battleship import BOARD_SIZE, SHIPS, Board

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
GAME_HOST = "127.0.0.1"       # must match HOST in server.py
GAME_PORT = 5050              # must match PORT in server.py
KEY_FILE = ".battleship_key"  # shared AES key; server.py creates it when it starts
UI_HOST = "127.0.0.1"         # the web page is only reachable from this computer
UI_FIRST_PORT = 8001          # first copy uses 8001, the next copy 8002, ...
TURN_SECONDS = 60             # battleship.py waits 60 s for each shot (recv_msg timeout=60)
PLAY_AGAIN_SECONDS = 30       # battleship.py waits 30 s for a Y/N answer (ask_play_again)
STUCK_HINT_SECONDS = 6        # show a hint if the server goes quiet right after login
COMPUTER_DELAY = 0.9          # single player: seconds the computer "thinks" before it fires
HEADER_BYTES = struct.calcsize("!4sHH16sII")  # BEER header used by Protocol.py (32 bytes)

ROW_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[:BOARD_SIZE]
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,20}$")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def find_key_file():
    """Find .battleship_key where server.py writes it (the folder it was started from)."""
    for folder in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        path = os.path.join(folder, KEY_FILE)
        if os.path.isfile(path):
            return path
    return None


def parse_grid(payload):
    """Turn a GRID packet ('YOUR BOARD\\n 1 2 ...\\nA . S X o ...') into (title, 10x10 list)."""
    lines = payload.strip("\n").split("\n")
    title = lines[0].strip() if lines else ""
    rows = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) == BOARD_SIZE + 1 and parts[0] in ROW_LETTERS:
            rows.append(parts[1:])
    return title, (rows if len(rows) == BOARD_SIZE else None)


def changed_cells(old, new):
    """Squares that differ between two boards, i.e. the latest shot."""
    if not old or not new:
        return []
    return [[r, c] for r in range(BOARD_SIZE) for c in range(BOARD_SIZE) if old[r][c] != new[r][c]]


def cell_name(cell):
    return f"{ROW_LETTERS[cell[0]]}{cell[1] + 1}"


def parse_cell(text):
    """'b5' -> (1, 4). Returns None if it is not a square on the board."""
    mo = re.fullmatch(r"\s*([A-Za-z])\s*(\d{1,2})\s*", text or "")
    if not mo:
        return None
    r, c = ROW_LETTERS.find(mo.group(1).upper()), int(mo.group(2)) - 1
    if r < 0 or not 0 <= c < BOARD_SIZE:
        return None
    return r, c


def socket_closed(sock):
    """True if the server has closed the TCP connection (or the socket is unusable)."""
    try:
        sock.setblocking(False)
        try:
            return sock.recv(1, socket.MSG_PEEK) == b""
        except BlockingIOError:
            return False
        finally:
            sock.setblocking(True)
    except OSError:
        return True


# ---------------------------------------------------------------------------
# Single player: you against the computer (no server needed)
# ---------------------------------------------------------------------------
class SoloGame:
    """Both fleets use the Board class from battleship.py, so hits, misses and sunk
    ships follow exactly the same rules as the multiplayer game."""

    def __init__(self):
        self.mine, self.my_ships = self._fleet()      # your board; the computer fires at it
        self.theirs, _ = self._fleet()                # the computer's board; you fire at it
        self.ai_hits = set()                          # computer's hits on ships not sunk yet

    @staticmethod
    def _fleet():
        """Random fleet, like Board.place_ships_randomly() but without its debug prints."""
        board = Board(BOARD_SIZE)
        cells = {}
        for name, size in SHIPS:
            while True:
                orientation = random.randint(0, 1)  # 0 horizontal, 1 vertical
                row, col = random.randrange(BOARD_SIZE), random.randrange(BOARD_SIZE)
                if board.can_place_ship(row, col, size, orientation):
                    positions = board.do_place_ship(row, col, size, orientation)
                    board.placed_ships.append({"name": name, "positions": positions})
                    cells[name] = set(positions)
                    break
        return board, cells

    def computer_target(self):
        """Pick the computer's next square: finish off a ship it has hit, else search."""
        grid = self.mine.display_grid  # all the computer can see: X hit, o miss, . unknown
        n = BOARD_SIZE

        def unknown(r, c):
            return 0 <= r < n and 0 <= c < n and grid[r][c] == "."

        if self.ai_hits:
            line = []  # squares that continue a line of two or more hits
            for r, c in self.ai_hits:
                for dr, dc in ((0, 1), (1, 0)):
                    if (r + dr, c + dc) in self.ai_hits:
                        for step in (-1, 1):
                            rr, cc = r, c
                            while (rr, cc) in self.ai_hits:
                                rr, cc = rr + step * dr, cc + step * dc
                            if unknown(rr, cc):
                                line.append((rr, cc))
            if line:
                return random.choice(line)
            around = [(r + dr, c + dc) for r, c in self.ai_hits
                      for dr, dc in ((0, 1), (1, 0), (0, -1), (-1, 0)) if unknown(r + dr, c + dc)]
            if around:
                return random.choice(around)
        open_cells = [(r, c) for r in range(n) for c in range(n) if grid[r][c] == "."]
        checkerboard = [(r, c) for r, c in open_cells if (r + c) % 2 == 0]  # every ship covers one
        return random.choice(checkerboard or open_cells)

    def computer_fire(self):
        r, c = self.computer_target()
        result, sunk = self.mine.fire_at(r, c)
        if result == "hit":
            self.ai_hits.add((r, c))
            if sunk:
                self.ai_hits -= self.my_ships[sunk]
        return (r, c), result, sunk


# ---------------------------------------------------------------------------
# The BEER connection + everything the web page shows
# ---------------------------------------------------------------------------
class GameClient:
    """One player's connection to server.py, plus the state the web page draws."""

    def __init__(self):
        self.cond = threading.Condition(threading.RLock())  # guards all state below
        self.send_lock = threading.Lock()
        self.sock = None
        self.key = None
        self.seq = 0            # BEER sequence number for packets we send (mod 256, like client.py)
        self.version = 0        # bumped on every change so the page knows when to redraw
        self.session = 0        # bumped on every login
        self.match_id = 0       # bumped on every new match
        self.leaving = False
        self.mode = "local"     # web_gateway.py sets "online"
        self.next_id = int(time.time() * 1000)  # feed ids keep increasing, even across restarts
        self.log = []           # activity feed
        self.packets = []       # packet log (Packets tab)
        self.solo = None        # a SoloGame while playing the computer
        self.solo_id = 0
        self.st = self._fresh_state()

    # ----- state ------------------------------------------------------------
    @staticmethod
    def _fresh_match():
        return {
            "opponent": None,
            "my_turn": False,
            "can_fire": False,          # True once the server asks "Enter coordinate to fire at"
            "turn_started": None,
            "opp_turn_started": None,
            "my_board": None,           # YOUR BOARD: S ship, X hit, o miss, . water
            "enemy_board": None,        # ENEMY BOARD: X hit, o miss, . unknown
            "pending_shot": None,       # [row, col] sent, waiting for HIT/MISS
            "last_shot": None,          # my latest shot, highlighted on the enemy board
            "incoming": [],             # opponent's latest shot, highlighted on my board
            "enemy_sunk": [],
            "my_sunk": [],
            "shots": 0,
            "hits": 0,
            "timeouts": 0,
            "opponent_away": False,
            "outcome": None,            # "win" or "lose"
            "outcome_text": None,
            "ask_play_again": False,
            "play_again_answer": None,  # "y" or "n"
            "asked_at": None,
        }

    @staticmethod
    def _fresh_spectate():
        return {"p1": None, "p2": None, "view1": None, "view2": None, "last1": [], "last2": [],
                "turn": None, "active": False, "winner": None, "pending_view": None}

    def _fresh_state(self, username=None):
        return {
            # login -> lobby -> queue / spectating -> playing -> game_over -> closed
            "phase": "login",
            "username": username,
            "queue_pos": None,
            "stuck": False,
            "played_match": False,
            "ended_by_server": False,
            "can_resume": False,
            "notice": None,
            "solo": False,      # True while playing against the computer
            "match": self._fresh_match(),
            "spec": self._fresh_spectate(),
        }

    def _changed(self):
        self.version += 1
        self.cond.notify_all()

    def _add_log(self, kind, text):
        last = self.log[-1] if self.log else None
        if last and last["text"] == text and time.time() - last["t"] < 3:
            return  # the server sometimes sends the same notice twice
        self.log.append({"id": self.next_id, "t": time.time(), "kind": kind, "text": text})
        self.next_id += 1
        del self.log[:-200]

    def _add_packet(self, direction, mtype, payload, shown=None):
        try:
            name = MessageType(mtype).name
        except ValueError:
            name = str(mtype)
        text = shown if shown is not None else payload
        if mtype == MessageType.GRID:
            text = text.strip().split("\n")[0] + "  (10x10 board)"
        self.packets.append({
            "id": self.next_id, "t": time.time(), "dir": direction, "type": name,
            "bytes": HEADER_BYTES + len(payload.encode("utf-8")),
            "text": " / ".join(part for part in text.strip().split("\n") if part.strip())[:200],
        })
        self.next_id += 1
        del self.packets[:-150]

    def snapshot_json(self, since_log=0, since_pkt=0):
        with self.cond:
            now = time.time()
            m = self.st["match"]
            data = dict(self.st)
            data.update({
                "version": self.version,
                "session": self.session,
                "match_id": self.match_id,
                "connected": self.sock is not None,
                "turn_elapsed": (now - m["turn_started"]) if (m["can_fire"] and m["turn_started"]) else None,
                "opp_elapsed": (now - m["opp_turn_started"]) if m["opp_turn_started"] else None,
                "again_elapsed": (now - m["asked_at"]) if (m["ask_play_again"] and m["asked_at"]) else None,
                "mode": self.mode,
                "log": [e for e in self.log if e["id"] > since_log][-150:],
                "packets": [p for p in self.packets if p["id"] > since_pkt][-150:],
                "ships": [[name, size] for name, size in SHIPS],
                "board_size": BOARD_SIZE,
                "turn_seconds": TURN_SECONDS,
                "again_seconds": PLAY_AGAIN_SECONDS,
                "server": f"{GAME_HOST}:{GAME_PORT}",
            })
            return json.dumps(data)

    def wait_for_change(self, since, timeout=25.0, since_log=0, since_pkt=0, settle=0.0):
        """Long-poll: answer as soon as something changes (or after `timeout` seconds).
        `settle` waits a moment longer so a burst of packets goes out as one answer."""
        with self.cond:
            changed = self.cond.wait_for(lambda: self.version != since, timeout)
            if changed and settle and since >= 0:
                end = time.time() + settle
                while time.time() < end:
                    self.cond.wait(end - time.time())
            return self.snapshot_json(since_log, since_pkt)

    # ----- joining ------------------------------------------------------------
    def _try_auth(self, key, username, method, password):
        """One login attempt on a fresh connection, exactly like client.py does it.
        Returns (sock, reply, problem): sock is set on success; `problem` explains
        connection trouble; (None, reply, None) means the server said no."""
        try:
            sock = socket.create_connection((GAME_HOST, GAME_PORT), timeout=5)
        except ConnectionRefusedError:
            return None, None, (f"Can't reach the game server at {GAME_HOST}:{GAME_PORT}. "
                                "Start it first with: python3 server.py")
        except OSError as exc:
            return None, None, f"Can't reach the game server at {GAME_HOST}:{GAME_PORT} ({exc})."
        sock.settimeout(None)
        hello = f"USERNAME {username} {method}:{password}"
        with self.cond:
            self._add_packet("out", MessageType.DEFAULT, hello, f"USERNAME {username} {method}:******")
            self._changed()
        if not send_encrypted_packet(sock, MessageType.DEFAULT, hello, key, 0):
            sock.close()
            return None, None, "Couldn't send the login packet to the game server."
        ready, _, _ = select.select([sock], [], [], 10)
        ok, mtype, reply = receive_encrypted_packet(sock, key) if ready else (False, None, None)
        if not ok or not isinstance(reply, str):
            sock.close()
            if not ready:
                return None, None, "The game server didn't answer the login within 10 seconds."
            return None, None, ("The game server closed the connection during login. This usually means "
                                "web_client.py found a different .battleship_key from the one server.py "
                                "is using. Run both from the same project folder and try again.")
        with self.cond:
            self._add_packet("in", mtype, reply)
            self._changed()
        if "successful" in reply:
            return sock, reply, None
        sock.close()
        return None, reply, None

    def join(self, username, password):
        username = (username or "").strip()
        password = password or ""
        if not USERNAME_RE.match(username):
            return False, "Pick a player name of 1-20 letters, numbers, dots, dashes or underscores (no spaces)."
        if not password or len(password) > 64 or "\n" in password:
            return False, "Enter a password (up to 64 characters)."
        with self.cond:
            if self.sock is not None:
                return False, "You're already connected."
            if self.solo is not None:
                self._end_solo()
        key_path = find_key_file()
        if not key_path:
            return False, ("Can't find .battleship_key. Start server.py first (it creates the key), "
                           "and run web_client.py from the same project folder.")
        key = KeyManager.load_from_file(key_path).key

        # Returning players log in with their password; a new name gets registered.
        sock, reply, problem = self._try_auth(key, username, "PASSWORD", password)
        registered = False
        if sock is None and problem is None:
            sock, reply, problem = self._try_auth(key, username, "REGISTER", password)
            registered = sock is not None
            if sock is None and problem is None:
                if reply and "already exists" in reply:
                    return False, (f"Wrong password for “{username}”. That name is already "
                                   "registered on this server.")
                return False, f"The server refused the login: {reply}"
        if problem:
            return False, problem

        with self.cond:
            self.sock, self.key, self.seq = sock, key, 1   # client.py also sends its first packet as seq 0
            self.leaving = False
            self.session += 1
            session = self.session
            self.log = []
            self.st = self._fresh_state(username)
            self.st["phase"] = "lobby"
            self._add_log("start", f"Registered new player {username}." if registered
                          else f"Logged in as {username}.")
            self._changed()
        threading.Thread(target=self._read_loop, args=(sock, key, session), daemon=True).start()
        timer = threading.Timer(STUCK_HINT_SECONDS, self._check_stuck, args=(session,))
        timer.daemon = True
        timer.start()
        return True, None

    def _check_stuck(self, session):
        with self.cond:
            if session == self.session and self.sock is not None and self.st["phase"] == "lobby":
                self.st["stuck"] = True
                self._changed()

    # ----- actions from the page --------------------------------------------
    def _send(self, text, mtype=MessageType.DEFAULT):
        with self.cond:
            sock, key = self.sock, self.key
        if sock is None:
            return False
        with self.send_lock:
            ok = send_encrypted_packet(sock, mtype, text, key, self.seq)
            if ok:
                self.seq = (self.seq + 1) % 256
        with self.cond:
            self._add_packet("out", mtype, text)
            self._changed()
        return ok

    def fire(self, text):
        cell = parse_cell(text)
        if cell is None:
            return False, f"Pick a square from A1 to {ROW_LETTERS[-1]}{BOARD_SIZE}."
        with self.cond:
            if self.solo is not None:
                return self._solo_fire(cell)
            m = self.st["match"]
            if self.sock is None or not m["can_fire"]:
                return False, "It isn't your turn."
            if m["enemy_board"] and m["enemy_board"][cell[0]][cell[1]] != ".":
                return False, f"You already fired at {cell_name(cell)}."
            m["can_fire"] = False
            m["pending_shot"] = list(cell)
            self._changed()
        # The server only needs the square ("B5"); we label the packet FIRE.
        if not self._send(cell_name(cell), MessageType.FIRE):
            with self.cond:
                m["can_fire"], m["pending_shot"] = True, None
                self._changed()
            return False, "Couldn't send the shot. Check that server.py is still running."
        return True, None

    def answer(self, yes):
        with self.cond:
            m = self.st["match"]
            if self.solo is not None:
                if not m["ask_play_again"]:
                    return False, "There's no question to answer right now."
                if yes:
                    self._new_solo(self.st["username"])
                else:
                    self._end_solo()
                self._changed()
                return True, None
            if self.sock is None or not m["ask_play_again"]:
                return False, "There's no question to answer right now."
            m["play_again_answer"] = "y" if yes else "n"
            m["ask_play_again"] = False
            self._add_log("you", "You chose to play again." if yes else "You chose not to play again.")
            self._changed()
        self._send("y" if yes else "n")
        return True, None

    def forfeit(self):
        with self.cond:
            m = self.st["match"]
            if self.solo is not None:
                if not m["can_fire"]:
                    return False, "You can only give up on your own turn."
                self._finish("lose", "You gave up.")
                m["ask_play_again"] = True
                self._changed()
                return True, None
            if self.sock is None or not m["can_fire"]:
                return False, "You can only give up on your own turn."
            m["can_fire"] = False
            self._changed()
        self._send("quit")
        return True, None

    def leave(self):
        with self.cond:
            if self.solo is not None:
                self._end_solo()
                self._changed()
                return True, None
            sock = self.sock
            if sock is None:
                return False, "You're not connected."
            self.leaving = True
        try:
            sock.shutdown(socket.SHUT_RDWR)   # the read loop notices and tidies up
        except OSError:
            pass
        return True, None

    def reset(self):
        with self.cond:
            if self.sock is not None:
                return False, "Leave the game first."
            if self.solo is not None:
                self._end_solo()
            self.st = self._fresh_state(self.st.get("username"))
            self._changed()
        return True, None

    # ----- single player -------------------------------------------------------
    def start_solo(self, username):
        username = (username or "").strip() or "You"
        if not USERNAME_RE.match(username):
            return False, "Pick a player name of 1-20 letters, numbers, dots, dashes or underscores (no spaces)."
        with self.cond:
            if self.sock is not None:
                return False, "Leave the online game first."
            self._new_solo(username)
            self._changed()
        return True, None

    def _new_solo(self, username):
        self.solo = SoloGame()
        self.solo_id += 1
        self.session += 1
        self.match_id += 1
        self.log = []
        self.st = self._fresh_state(username)
        self.st.update(phase="playing", solo=True, played_match=True)
        m = self.st["match"]
        m.update(opponent="Computer", my_turn=True, can_fire=True,
                 my_board=[row[:] for row in self.solo.mine.hidden_grid],
                 enemy_board=[row[:] for row in self.solo.theirs.display_grid])
        self._add_log("start", "Single player: you vs the computer. Both fleets were placed at random. You go first.")

    def _end_solo(self):
        self.solo = None
        self.solo_id += 1  # cancels a computer move that is still pending
        self.st = self._fresh_state(self.st.get("username"))

    def _solo_fire(self, cell):
        """Your shot at the computer's fleet (called with the lock held)."""
        m = self.st["match"]
        if not m["can_fire"]:
            return False, "Wait for the computer to finish its turn."
        r, c = cell
        if m["enemy_board"][r][c] != ".":
            return False, f"You already fired at {cell_name(cell)}."
        result, sunk = self.solo.theirs.fire_at(r, c)
        hit = result == "hit"
        m["enemy_board"] = [row[:] for row in self.solo.theirs.display_grid]
        m.update(last_shot=[r, c], my_turn=False, can_fire=False)
        m["shots"] += 1
        if hit:
            m["hits"] += 1
        if sunk:
            m["enemy_sunk"].append(sunk)
            self._add_log("sunk", f"{cell_name(cell)}: hit! You sank the computer's {sunk}.")
        elif hit:
            self._add_log("hit", f"{cell_name(cell)}: hit!")
        else:
            self._add_log("miss", f"{cell_name(cell)}: miss.")
        if self.solo.theirs.all_ships_sunk():
            self._finish("win", f"You sank the computer's whole fleet in {m['shots']} shots.")
            m["ask_play_again"] = True
        else:
            timer = threading.Timer(COMPUTER_DELAY, self._solo_computer_turn, args=(self.solo_id,))
            timer.daemon = True
            timer.start()
        self._changed()
        return True, None

    def _solo_computer_turn(self, solo_id):
        with self.cond:
            if self.solo is None or solo_id != self.solo_id or self.st["match"]["outcome"]:
                return  # the game ended or a new one started meanwhile
            m = self.st["match"]
            (r, c), result, sunk = self.solo.computer_fire()
            m["my_board"] = [row[:] for row in self.solo.mine.hidden_grid]
            m["incoming"] = [[r, c]]
            where = cell_name((r, c))
            if sunk:
                m["my_sunk"].append(sunk)
                self._add_log("incoming-sunk", f"The computer fired at {where} and sank your {sunk}.")
            elif result == "hit":
                self._add_log("incoming-hit", f"The computer fired at {where} and hit one of your ships.")
            else:
                self._add_log("incoming-miss", f"The computer fired at {where} and missed.")
            if self.solo.mine.all_ships_sunk():
                self._finish("lose", "The computer sank your whole fleet.")
                m["ask_play_again"] = True
            else:
                m.update(my_turn=True, can_fire=True)
            self._changed()

    # ----- receiving -----------------------------------------------------------
    def _read_loop(self, sock, key, session):
        """Background thread: receive BEER packets from server.py and update the state."""
        corrupted = 0
        while True:
            ok, mtype, payload = receive_encrypted_packet(sock, key)
            if ok and isinstance(payload, str):
                corrupted = 0
                with self.cond:
                    if session != self.session:
                        break
                    self._add_packet("in", mtype, payload)
                    try:
                        self._handle(mtype, payload)
                    except Exception as exc:  # never let one odd message break the connection
                        print(f"[WARNING] web_client could not interpret {payload!r}: {exc}")
                        self._add_log("system", payload.strip())
                    self._changed()
                continue
            if socket_closed(sock):
                break
            corrupted += 1  # Protocol.py rejected it (magic bytes, length or CRC32 failed)
            with self.cond:
                self._add_log("warn", "A corrupted packet arrived and was discarded (it failed the checks in Protocol.py).")
                self._changed()
            if corrupted >= 5:
                break
            time.sleep(0.05)
        try:
            sock.close()
        except OSError:
            pass
        with self.cond:
            if session == self.session:
                self._closed()
                self._changed()

    def _closed(self):
        st, m = self.st, self.st["match"]
        self.sock = None
        mid_match = st["phase"] == "playing" and not m["outcome"]
        if self.leaving:
            st["notice"] = "You left the game server."
            st["can_resume"] = mid_match
        elif st["ended_by_server"]:
            st["notice"] = "The match is over and the server closed this session."
        else:
            st["notice"] = "Lost the connection to the game server. Was server.py stopped?"
            st["can_resume"] = mid_match
        m.update(can_fire=False, my_turn=False, ask_play_again=False)
        st["phase"] = "closed"
        self._add_log("system", st["notice"])

    def _saw_opponent(self, name):
        m = self.st["match"]
        if not m["opponent"]:
            m["opponent"] = name

    def _finish(self, outcome, reason):
        st, m = self.st, self.st["match"]
        if m["outcome"]:
            return  # e.g. two "did not reconnect" notices for the same forfeit
        m.update(outcome=outcome, outcome_text=reason, my_turn=False, can_fire=False,
                 turn_started=None, opp_turn_started=None, pending_shot=None, opponent_away=False)
        st["phase"] = "game_over"
        self._add_log(outcome, ("You win! " if outcome == "win" else "You lose. ") + reason)

    def _handle_grid(self, payload):
        st, m, sp = self.st, self.st["match"], self.st["spec"]
        title, grid = parse_grid(payload)
        if grid is None:
            self._add_log("system", payload.strip())
            return
        if title == "YOUR BOARD":
            diff = changed_cells(m["my_board"], grid)
            if diff:
                m["incoming"] = diff
            m["my_board"] = grid
            if st["phase"] not in ("playing", "game_over"):
                st["phase"] = "playing"
        elif title == "ENEMY BOARD":
            m["enemy_board"] = grid
        else:  # spectators get plain "GRID" boards right after "PLAYER n's view:"
            n = sp["pending_view"] or 1
            diff = changed_cells(sp["view%d" % n], grid)
            if diff:
                sp["last%d" % n] = diff
            sp["view%d" % n] = grid
            sp["pending_view"] = None
            if st["phase"] in ("lobby", "queue"):
                st["phase"] = "spectating"

    def _handle_spectator(self, body):
        st = self.st
        sp = st["spec"]
        if st["phase"] in ("lobby", "queue"):
            st["phase"] = "spectating"

        def other(name):
            if sp["p1"] and sp["p2"]:
                return sp["p2"] if name == sp["p1"] else sp["p1"]
            return "the other player"

        mo = re.match(r"PLAYER ([12])'s view:", body)
        if mo:
            sp["pending_view"] = int(mo.group(1))
            return
        mo = re.match(r"(?:Game starting between (\S+) and (\S+)|New game started: (\S+) vs (\S+))$", body)
        if mo:
            p1, p2 = mo.group(1) or mo.group(3), mo.group(2) or mo.group(4)
            if not (sp["active"] and sp["p1"] == p1 and sp["p2"] == p2 and not sp["winner"]):
                st["spec"] = sp = self._fresh_spectate()
                sp.update(p1=p1, p2=p2, active=True)
                self._add_log("start", f"New match: {p1} vs {p2}.")
            return
        mo = re.match(r"(\S+)'s turn$", body)
        if mo:
            sp["turn"] = mo.group(1)
            return
        mo = re.match(r"(\S+) scored a HIT\.$", body)
        if mo:
            self._add_log("hit", f"{mo.group(1)} hit one of {other(mo.group(1))}'s ships.")
            return
        mo = re.match(r"(\S+) MISSED\.$", body)
        if mo:
            self._add_log("miss", f"{mo.group(1)} missed.")
            return
        mo = re.match(r"(\S+) sank the (.+?)!$", body)
        if mo:
            self._add_log("sunk", f"{mo.group(1)} sank {other(mo.group(1))}'s {mo.group(2)}!")
            return
        mo = re.match(r"(\S+) WON the game!$", body)
        if mo:
            sp.update(winner=mo.group(1), turn=None)
            self._add_log("win", f"{mo.group(1)} won the match!")
            return
        if body.startswith("Game ended"):
            sp.update(active=False, turn=None)
            if "waiting for next match" in body:
                self._add_log("system", "Match over. Waiting for the next one...")
            return
        self._add_log("system", body)

    def _handle(self, mtype, payload):
        """Update the state from one server message. The texts come from server.py and battleship.py."""
        st = self.st
        m, sp = st["match"], st["spec"]
        st["stuck"] = False

        if mtype == MessageType.GRID:
            self._handle_grid(payload)
            return
        text = payload.strip()
        if not text:
            return
        if text == "GAME_ENDED_DISCONNECTING":
            st["ended_by_server"] = True
            m["ask_play_again"] = False
            return
        if text.startswith("[SPECTATOR]"):
            self._handle_spectator(text[len("[SPECTATOR]"):].strip())
            return

        # --- lobby and queue (server.py) ---
        if text.startswith("Welcome to Battleship Multiplayer"):
            if "joined as a spectator" in text:
                st["phase"] = "spectating"
                mo = re.search(r"watching a game between (\S+) and (\S+?)\.", text)
                if mo:
                    sp.update(p1=mo.group(1), p2=mo.group(2), active=True)
                    self._add_log("system", f"A match is on: {mo.group(1)} vs {mo.group(2)}. "
                                            "You're watching as a spectator.")
                else:
                    self._add_log("system", "A match is on. You're watching as a spectator.")
            else:
                mo = re.search(r"number (\d+) in the queue", text)
                st["phase"] = "queue"
                st["queue_pos"] = int(mo.group(1)) if mo else None
                where = f" (number {st['queue_pos']})" if mo else ""
                self._add_log("system", f"You're in the queue{where}. Waiting for an opponent...")
            return
        mo = re.match(r"UPDATE: You are now number (\d+)", text)
        if mo:
            st["queue_pos"] = int(mo.group(1))
            st["phase"] = "queue"
            self._add_log("system", f"Queue update: you're number {st['queue_pos']}.")
            return
        if "promoted from spectator to player" in text:
            st["phase"] = "queue"
            st["queue_pos"] = None
            self._add_log("start", "You've been picked to play the next match. Get ready!")
            return

        # --- a new match (battleship.py) ---
        mo = re.match(r"Welcome (\S+)! You are playing against (\S+?)\.$", text)
        if mo:
            self.match_id += 1
            st["match"] = m = self._fresh_match()
            m["opponent"] = mo.group(2)
            m["opp_turn_started"] = time.time()
            st.update(phase="playing", queue_pos=None, played_match=True, ended_by_server=False)
            self._add_log("start", f"Match started: you vs {m['opponent']}. "
                                   "The server placed both fleets at random.")
            return

        # --- my turn ---
        if text == "Your turn!":
            m["my_turn"] = True
            m["opp_turn_started"] = None
            st["phase"] = "playing"
            return
        if text.startswith("Enter coordinate to fire at"):
            m.update(my_turn=True, can_fire=True, turn_started=time.time(), pending_shot=None)
            st["phase"] = "playing"
            return

        # --- the result of my shot (RESULT packets) ---
        sunk = re.match(r"HIT! You sank the (.+?)!$", text)
        if sunk or text in ("HIT!", "MISS!"):
            hit = text != "MISS!"
            cell = m["pending_shot"]
            where = cell_name(cell) if cell else "Your shot"
            if cell and m["enemy_board"]:
                m["enemy_board"][cell[0]][cell[1]] = "X" if hit else "o"
            m.update(last_shot=cell, pending_shot=None, my_turn=False, can_fire=False,
                     turn_started=None, opp_turn_started=time.time())
            m["shots"] += 1
            if hit:
                m["hits"] += 1
            if sunk:
                m["enemy_sunk"].append(sunk.group(1))
                self._add_log("sunk", f"{where}: hit! You sank their {sunk.group(1)}.")
            elif hit:
                self._add_log("hit", f"{where}: hit!")
            else:
                self._add_log("miss", f"{where}: miss.")
            return
        if text.startswith("Already fired there"):
            m["pending_shot"] = None
            self._add_log("warn", "You already fired at that square. Pick another one.")
            return
        if text.startswith(("Invalid", "Coordinates out of bounds", "Error processing your shot",
                            "An error occurred processing", "Communication timed out")):
            m["pending_shot"] = None
            self._add_log("warn", text)
            return

        # --- the opponent fired at me ---
        mo = re.match(r"(\S+) sank your (.+?)!$", text)
        if mo:
            self._saw_opponent(mo.group(1))
            m["my_sunk"].append(mo.group(2))
            self._add_log("incoming-sunk", f"{mo.group(1)} sank your {mo.group(2)}.")
            return
        mo = re.match(r"(\S+) hit your ship!$", text)
        if mo:
            self._saw_opponent(mo.group(1))
            self._add_log("incoming-hit", f"{mo.group(1)} hit one of your ships.")
            return
        mo = re.match(r"(\S+) missed\.$", text)
        if mo:
            self._saw_opponent(mo.group(1))
            self._add_log("incoming-miss", f"{mo.group(1)} missed.")
            return

        # --- the end of a match ---
        if text.startswith("You win!"):
            self._finish("win", "You sank the whole enemy fleet.")
            return
        if text.startswith("You lose."):
            board = m["my_board"]
            if board:  # every ship is sunk, so any square still showing a ship was the final hit
                last = [[r, c] for r in range(BOARD_SIZE) for c in range(BOARD_SIZE) if board[r][c] == "S"]
                for r, c in last:
                    board[r][c] = "X"
                if last:
                    m["incoming"] = last
            self._finish("lose", "Your whole fleet was sunk.")
            return
        mo = re.match(r"(\S+) quit\. You win!", text)
        if mo:
            self._saw_opponent(mo.group(1))
            self._finish("win", f"{mo.group(1)} gave up.")
            return
        if text.startswith("You quit."):
            self._finish("lose", "You gave up.")
            return
        mo = re.match(r"(\S+) timed out 3 times\. You win!", text)
        if mo:
            self._finish("win", f"{mo.group(1)} ran out of time 3 times.")
            return
        if text.startswith("You timed out 3 times"):
            self._finish("lose", "You ran out of time 3 times.")
            return
        mo = re.match(r"(\S+) did not reconnect.*You win by forfeit", text, re.S)
        if mo:
            self._finish("win", f"{mo.group(1)} left and didn't come back within 60 seconds.")
            return
        if re.search(r"would you like to play again", text, re.I):
            if m["play_again_answer"] is None:
                if not m["ask_play_again"]:
                    self._add_log("turn", f"Play again? The server waits {PLAY_AGAIN_SECONDS} seconds for your answer.")
                m["ask_play_again"] = True
                m["asked_at"] = time.time()  # the server restarts its wait with each prompt
            st["phase"] = "game_over"
            return

        # --- time-outs (battleship.py gives 60 s per shot, 3 strikes) ---
        mo = re.match(r"Timeout! You took too long \((\d+)/3\)", text)
        if mo:
            m.update(my_turn=False, can_fire=False, turn_started=None, pending_shot=None,
                     timeouts=int(mo.group(1)), opp_turn_started=time.time())
            self._add_log("warn", f"Time's up ({mo.group(1)}/3). Your turn was skipped; "
                                  "a third time-out loses the match.")
            return
        mo = re.match(r"(\S+) took too long\. Your turn next!", text)
        if mo:
            self._saw_opponent(mo.group(1))
            self._add_log("system", f"{mo.group(1)} ran out of time. Your turn next.")
            return

        # --- disconnects and reconnects ---
        if "OPPONENT DISCONNECTED" in text:
            mo = re.search(r"(\S+) has disconnected", text)
            who = mo.group(1) if mo else "Your opponent"
            m["opponent_away"] = True
            self._add_log("warn", f"{who} disconnected. The server waits up to 60 seconds for them to come back.")
            return
        mo = re.match(r"(\S+) has reconnected!", text)
        if mo:
            m["opponent_away"] = False
            self._add_log("system", f"{mo.group(1)} is back. The match continues.")
            return
        if text.startswith("You have reconnected"):
            st.update(phase="playing", played_match=True)
            if "Waiting for opponent" in text:
                m["opponent_away"] = True
                self._add_log("start", "Reconnected. Waiting for your opponent to come back...")
            else:
                self._add_log("start", "Reconnected. Your match carries on from where you left it.")
            return
        if text.startswith("Both players have reconnected"):
            m["opponent_away"] = False
            self._add_log("system", "Both players are back. The match continues.")
            return

        # Anything else is shown exactly as the server sent it.
        self._add_log("system", text)


# ---------------------------------------------------------------------------
# The local web server that the browser page talks to
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    client = None  # the GameClient, set in main()
    server_version = "BEERWeb/1.0"

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet (the page polls constantly)

    def _host_ok(self):
        # Only answer requests addressed to this computer (blocks DNS-rebinding tricks).
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def _reply(self, code, body, content_type):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            self.send_error(403)
            return
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._reply(200, PAGE, "text/html; charset=utf-8")
        elif url.path == "/api/state":
            query = parse_qs(url.query)

            def number(name, default):
                try:
                    return int(query.get(name, [default])[0])
                except ValueError:
                    return default
            state = self.client.wait_for_change(number("v", -1), 25.0, number("l", 0), number("p", 0))
            self._reply(200, state, "application/json")
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._host_ok():
            self.send_error(403)
            return
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self.send_error(415)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        c = self.client
        actions = {
            "/api/join": lambda: c.join(body.get("username"), body.get("password")),
            "/api/solo": lambda: c.start_solo(body.get("username")),
            "/api/fire": lambda: c.fire(body.get("cell")),
            "/api/answer": lambda: c.answer(bool(body.get("yes"))),
            "/api/forfeit": c.forfeit,
            "/api/leave": c.leave,
            "/api/reset": c.reset,
        }
        action = actions.get(urlparse(self.path).path)
        if action is None:
            self.send_error(404)
            return
        ok, error = action()
        self._reply(200, json.dumps({"ok": ok, "error": error}), "application/json")


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    allow_reuse_port = False  # never share a port with another copy of this script

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return  # a browser tab closed mid-request: normal, nothing to report
        super().handle_error(request, client_address)


def make_server(port):
    if port:
        return QuietServer((UI_HOST, port), Handler)
    for candidate in range(UI_FIRST_PORT, UI_FIRST_PORT + 50):
        try:
            return QuietServer((UI_HOST, candidate), Handler)
        except OSError:
            continue  # taken (probably by the other player's copy); try the next one
    raise SystemExit(f"[ERROR] No free port between {UI_FIRST_PORT} and {UI_FIRST_PORT + 49}.")


def main():
    parser = argparse.ArgumentParser(description="Play BEER Battleships in your browser.")
    parser.add_argument("--port", type=int, default=0,
                        help=f"port for the web page (default: first free port from {UI_FIRST_PORT})")
    parser.add_argument("--no-browser", action="store_true",
                        help="don't open a browser tab automatically")
    args = parser.parse_args()

    Handler.client = GameClient()
    try:
        httpd = make_server(args.port)
    except OSError as exc:
        raise SystemExit(f"[ERROR] Can't use port {args.port}: {exc}")
    url = f"http://{UI_HOST}:{httpd.server_address[1]}/"
    print(f"[INFO] BEER web client is running. Open {url} in your browser.")
    print(f"[INFO] It talks to the game server at {GAME_HOST}:{GAME_PORT}. Press Ctrl+C here to quit.")
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n[INFO] Closing the web client...")
    finally:
        Handler.client.leave()
        httpd.server_close()


# ---------------------------------------------------------------------------
# The web page (HTML, CSS and JavaScript in one piece, no internet needed)
# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BEER Battleships</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 40 40'%3E%3Ccircle cx='20' cy='20' r='18' fill='%230b2340' stroke='%2338bdf8' stroke-width='2'/%3E%3Cpath d='M20 20 L20 2 A18 18 0 0 1 35.6 11 Z' fill='%2338bdf8' fill-opacity='.35'/%3E%3Ccircle cx='27' cy='12.5' r='3' fill='%23ff6b3d'/%3E%3C/svg%3E">
<style>
:root{
  --bg:#06101d; --panel:rgba(14,30,52,.78); --panel-2:rgba(9,21,38,.9);
  --line:rgba(148,178,214,.14); --line-2:rgba(148,178,214,.28);
  --text:#e6eef8; --muted:#94a8c2; --faint:#62789a;
  --accent:#38bdf8; --accent-ink:#04121f;
  --hit:#ff6b3d; --hit-2:#ffb347; --miss:#dbeafe;
  --win:#fbbf24; --lose:#f87171; --ok:#34d399; --warn:#fbbf24;
  --radius:14px;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
}
*,*::before,*::after{box-sizing:border-box}
html,body{margin:0}
body{min-height:100vh;color:var(--text);font:15px/1.45 var(--sans);background:var(--bg);
  background-image:
    radial-gradient(1100px 560px at 12% -8%,rgba(56,189,248,.11),transparent 60%),
    radial-gradient(900px 480px at 108% 4%,rgba(255,107,61,.06),transparent 60%),
    linear-gradient(rgba(148,178,214,.035) 1px,transparent 1px),
    linear-gradient(90deg,rgba(148,178,214,.035) 1px,transparent 1px);
  background-size:auto,auto,44px 44px,44px 44px;background-attachment:fixed}
[hidden]{display:none!important}
button{font:inherit}
code{font-family:var(--mono);font-size:.9em;background:rgba(255,255,255,.07);padding:1px 6px;border-radius:6px}

/* top bar */
.topbar{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:14px;padding:10px 20px;
  border-bottom:1px solid var(--line);background:rgba(6,16,29,.82);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;letter-spacing:.01em;min-width:0;white-space:nowrap}
.brand small{display:block;font-weight:600;color:var(--faint);font-size:10.5px;letter-spacing:.14em;text-transform:uppercase}
.logo{width:32px;height:32px;flex:none}
.spacer{flex:1}
.secure{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);padding:5px 10px;border:1px solid var(--line);border-radius:999px;white-space:nowrap}
.who{font-size:13px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:34vw}
.who b{color:var(--text)}

/* buttons and inputs */
.btn{appearance:none;border:1px solid var(--line-2);background:rgba(255,255,255,.05);color:var(--text);
  font-weight:600;font-size:14px;line-height:1;padding:11px 16px;border-radius:10px;cursor:pointer;
  transition:background .15s,border-color .15s,transform .05s;white-space:nowrap}
.btn:hover{background:rgba(255,255,255,.1)}
.btn:active{transform:translateY(1px)}
.btn:disabled{opacity:.4;cursor:not-allowed;transform:none}
.btn.primary{background:linear-gradient(180deg,#4cc6fa,#0ea5e9);border-color:transparent;color:var(--accent-ink)}
.btn.primary:hover{background:linear-gradient(180deg,#6fd3fb,#27b3ee)}
.btn.ghost{background:transparent}
.btn.danger{border-color:rgba(248,113,113,.5);color:#fecaca}
.btn.armed{background:rgba(248,113,113,.18);border-color:rgba(248,113,113,.7);color:#fee2e2}
.btn.sm{padding:8px 12px;font-size:13px}
input{font:15px var(--sans);color:var(--text);background:rgba(3,10,20,.65);border:1px solid var(--line-2);border-radius:10px;padding:11px 12px;width:100%}
input:disabled{opacity:.45}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

/* page layout */
.stage{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:20px;max-width:1460px;margin:0 auto;padding:20px}
.stage.solo{grid-template-columns:minmax(0,1fr)}
.stage.solo .side{display:none}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);box-shadow:0 18px 50px rgba(0,0,0,.28)}
.center{min-height:calc(100vh - 150px);display:grid;place-items:center;align-content:center;gap:22px;text-align:center;padding:10px 0}

/* login */
.hero .logo{width:76px;height:76px}
.hero h1{font-size:clamp(30px,4.2vw,46px);margin:10px 0 2px;letter-spacing:-.01em}
.tag{color:var(--muted);margin:0;text-transform:uppercase;letter-spacing:.22em;font-size:12px}
.login{width:min(100%,380px);padding:22px;text-align:left;display:grid;gap:14px}
.field{display:grid;gap:6px;font-size:13px;color:var(--muted)}
.hint{font-size:12.5px;color:var(--faint);margin:0}
.error{color:#fca5a5;font-size:13.5px;margin:0}
.error:empty{display:none}
.foot{font-size:12.5px;color:var(--faint);margin:0;max-width:560px}

/* waiting room */
.lobby{width:min(100%,460px);padding:30px 26px;display:grid;gap:12px;justify-items:center}
.lobby h1{margin:6px 0 0;font-size:24px}
.lobby p{margin:0;color:var(--muted)}
.qpos{font:700 44px var(--mono);color:var(--accent);line-height:1}
.tip{font-size:13px;color:var(--faint)!important;border-top:1px solid var(--line);padding-top:14px;margin-top:6px!important}
.stuck{border:1px solid rgba(251,191,36,.45);background:rgba(251,191,36,.08);color:#fde68a;border-radius:10px;padding:12px 14px;font-size:13.5px;text-align:left;display:grid;gap:10px;justify-items:start}
.radar{width:150px;height:150px;border-radius:50%;position:relative;overflow:hidden;border:1px solid rgba(56,189,248,.4);
  background:
    radial-gradient(circle,transparent 0 24%,rgba(56,189,248,.2) 24.5% 25.5%,transparent 26% 49%,rgba(56,189,248,.2) 49.5% 50.5%,transparent 51% 74%,rgba(56,189,248,.2) 74.5% 75.5%,transparent 76%),
    radial-gradient(circle,#0d2c49,#071a2d 72%);box-shadow:inset 0 0 40px rgba(56,189,248,.15)}
.radar::before{content:"";position:absolute;inset:0;border-radius:50%;background:conic-gradient(from 0deg,rgba(56,189,248,.5),rgba(56,189,248,0) 75deg,transparent);animation:sweep 2.8s linear infinite}
.radar::after{content:"";position:absolute;inset:0;background:linear-gradient(rgba(56,189,248,.2),rgba(56,189,248,.2)) center/1px 100% no-repeat,linear-gradient(rgba(56,189,248,.2),rgba(56,189,248,.2)) center/100% 1px no-repeat}
.radar i{position:absolute;width:9px;height:9px;border-radius:50%;background:var(--hit);left:66%;top:28%;box-shadow:0 0 12px var(--hit);animation:blip 2.8s ease-in-out infinite}
@keyframes sweep{to{transform:rotate(360deg)}}
@keyframes blip{0%,8%{opacity:1}40%,100%{opacity:0}}

/* turn banner */
.banner{position:relative;display:flex;align-items:center;gap:14px;padding:14px 18px;margin-bottom:18px;overflow:hidden;min-height:72px}
.pill{font-weight:700;font-size:11px;letter-spacing:.12em;text-transform:uppercase;padding:6px 10px;border-radius:999px;background:rgba(255,255,255,.08);color:var(--muted);white-space:nowrap;max-width:40%;overflow:hidden;text-overflow:ellipsis}
.banner-text{display:grid;gap:2px;min-width:0}
.banner-text strong{font-size:17px}
.banner-text span{font-size:13px;color:var(--muted)}
.banner.mine{border-color:rgba(56,189,248,.55);box-shadow:0 0 0 1px rgba(56,189,248,.18),0 12px 40px rgba(14,165,233,.16)}
.banner.mine .pill{background:var(--accent);color:var(--accent-ink)}
.banner.warn{border-color:rgba(251,191,36,.45)}
.banner.warn .pill{background:var(--warn);color:#1f1300}
.banner.win{border-color:rgba(251,191,36,.55)}
.banner.win .pill{background:var(--win);color:#1f1300}
.banner.lose .pill{background:var(--lose);color:#2b0a0a}
.timer{margin-left:auto;font:700 24px var(--mono);font-variant-numeric:tabular-nums;color:var(--muted)}
.banner.mine .timer{color:var(--text)}
.timer small{font-size:12px;color:var(--faint);margin-left:2px}
.timer.low{color:var(--warn)!important}
.timer.crit{color:var(--lose)!important}
.tbar{position:absolute;left:0;right:0;bottom:0;height:3px;background:rgba(255,255,255,.05)}
.tbar i{display:block;height:100%;background:var(--faint);transform-origin:left;transition:transform .25s linear}
.banner.mine .tbar i{background:var(--accent)}

/* boards */
.boards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
.board-card{padding:16px 16px 14px;display:grid;gap:12px;align-content:start;transition:border-color .2s,box-shadow .2s}
.board-card.active{border-color:rgba(56,189,248,.45);box-shadow:0 0 0 1px rgba(56,189,248,.14),0 18px 50px rgba(0,0,0,.28)}
.board-head{display:flex;align-items:baseline;justify-content:space-between;gap:10px;min-width:0}
.board-head h2{margin:0;font-size:12.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.board-head .sub{font-size:13px;color:var(--faint);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.board-wrap{position:relative}
.board{display:grid;grid-template-columns:18px repeat(10,minmax(0,1fr));grid-template-rows:18px repeat(10,minmax(0,1fr));
  gap:3px;width:100%;max-width:min(470px,max(300px,calc(100vh - 400px)));aspect-ratio:1;margin:0 auto;user-select:none;-webkit-user-select:none}
.lbl{display:grid;place-items:center;font:600 10.5px var(--mono);color:var(--faint)}
.cell{position:relative;border:0;padding:0;margin:0;border-radius:5px;min-width:0;min-height:0;color:inherit;cursor:default;
  background:linear-gradient(160deg,#10304f,#0a2139);box-shadow:inset 0 0 0 1px rgba(125,175,230,.09)}
.cell:disabled{cursor:default}
.cell.target{cursor:crosshair}
.cell.target:hover,.cell.target:focus-visible{background:linear-gradient(160deg,#175783,#0f3d63);box-shadow:inset 0 0 0 2px var(--accent);outline:none}
.cell.target:hover::before,.cell.target:focus-visible::before{content:"";position:absolute;inset:22%;border-radius:50%;border:2px solid var(--accent);
  background:linear-gradient(var(--accent),var(--accent)) center/2px 60% no-repeat,linear-gradient(var(--accent),var(--accent)) center/60% 2px no-repeat}
.cell.ship{background:linear-gradient(180deg,#8d9eb5,#5b6b81);box-shadow:inset 0 1px 0 rgba(255,255,255,.28),inset 0 -2px 0 rgba(0,0,0,.28)}
.cell.hit{background:linear-gradient(160deg,#40211b,#2a1411)}
.cell.ship.hit{background:linear-gradient(180deg,#80605a,#4b3531)}
.cell.hit::after,.cell.miss::after{content:"";position:absolute;inset:0;border-radius:inherit}
.cell.hit::after{background:radial-gradient(circle,#fff3c4 0 10%,var(--hit-2) 11% 25%,var(--hit) 26% 42%,rgba(255,107,61,0) 62%)}
.cell.miss::after{background:radial-gradient(circle,var(--miss) 0 12%,rgba(219,234,254,.25) 13% 22%,transparent 23%)}
.cell.pending{animation:pend .8s ease-in-out infinite;box-shadow:inset 0 0 0 2px var(--accent)}
.cell.pulse{box-shadow:inset 0 0 0 2px rgba(255,255,255,.6)}
.cell.pulse::before{content:"";position:absolute;inset:-5px;border-radius:9px;border:2px solid currentColor;opacity:0;animation:ping 1.3s ease-out 3;pointer-events:none}
.cell.hit.pulse{color:var(--hit)}
.cell.miss.pulse{color:#bae6fd}
@keyframes ping{0%{transform:scale(.55);opacity:1}100%{transform:scale(1.5);opacity:0}}
@keyframes pend{50%{background:#1d6aa0}}
.board-note{position:absolute;left:18px;right:0;top:50%;transform:translateY(-50%);margin:0 auto;max-width:260px;text-align:center;font-size:13px;
  color:var(--muted);background:rgba(6,16,29,.82);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.under{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;min-height:38px}
.typed{display:flex;gap:8px;align-items:center}
.typed input{width:86px;text-transform:uppercase;font:600 15px var(--mono);text-align:center;padding:9px 8px}
.typed input::placeholder{text-transform:none;font-weight:400;color:var(--faint)}
.aim{font:600 13px var(--mono);color:var(--accent);min-width:70px}
.fleet{display:flex;flex-wrap:wrap;gap:5px}
.fleet-label{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);margin:0}
.ship-chip{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;color:var(--muted);padding:3px 8px;border:1px solid var(--line);border-radius:999px}
.ship-chip i{display:inline-flex;gap:2px}
.ship-chip i b{width:5px;height:5px;border-radius:2px;background:#7b8ca3}
.ship-chip.sunk{color:#fca5a5;border-color:rgba(248,113,113,.35);text-decoration:line-through}
.ship-chip.sunk i b{background:var(--hit)}

/* stats row */
.statsrow{display:flex;flex-wrap:wrap;align-items:center;gap:12px 26px;margin-top:18px;padding:14px 18px}
.stat{display:grid;gap:3px}
.stat b{font:700 19px var(--mono);font-variant-numeric:tabular-nums}
.stat span{font-size:10.5px;color:var(--faint);text-transform:uppercase;letter-spacing:.12em}
.statsrow .spacer{flex:1}

/* spectator */
.spec-note{font-size:13px;color:var(--faint);text-align:center;margin:16px 0 0}

/* side panel: activity + packets */
.side{display:grid;grid-template-rows:auto auto minmax(0,1fr);position:sticky;top:76px;height:calc(100vh - 96px);min-height:380px;overflow:hidden}
.tabs{display:flex;gap:4px;padding:8px}
.tab{flex:1;appearance:none;border:0;background:transparent;color:var(--muted);font-weight:600;font-size:13px;padding:9px;border-radius:8px;cursor:pointer}
.tab[aria-selected="true"]{background:rgba(255,255,255,.08);color:var(--text)}
.side-note{font-size:12px;color:var(--faint);margin:0;padding:0 14px 10px;border-bottom:1px solid var(--line)}
.list{list-style:none;margin:0;padding:6px 8px 10px;overflow:auto;min-height:0}
.empty{color:var(--faint);font-size:13px;padding:14px}
.ev{display:grid;grid-template-columns:9px minmax(0,1fr) auto;gap:10px;align-items:start;padding:8px 6px;border-bottom:1px solid rgba(148,178,214,.07);font-size:13.5px}
.ev .dot{width:8px;height:8px;border-radius:50%;margin-top:6px;background:var(--faint)}
.ev time{font:11px var(--mono);color:var(--faint);padding-top:2px}
.ev.fresh{animation:slidein .35s ease-out}
@keyframes slidein{from{opacity:0;transform:translateY(-6px)}}
.ev.start .dot,.ev.turn .dot{background:var(--accent)}
.ev.hit .dot,.ev.incoming-hit .dot{background:var(--hit)}
.ev.sunk .dot,.ev.incoming-sunk .dot{background:var(--hit);box-shadow:0 0 0 3px rgba(255,107,61,.25)}
.ev.sunk .txt{color:#ffd3c2;font-weight:600}
.ev.incoming-sunk .txt{color:#fca5a5;font-weight:600}
.ev.miss .dot,.ev.incoming-miss .dot{background:#7dd3fc}
.ev.warn .dot{background:var(--warn)}
.ev.warn .txt{color:#fde68a}
.ev.win .dot{background:var(--win)}
.ev.win .txt{color:var(--win);font-weight:700}
.ev.lose .dot{background:var(--lose)}
.ev.lose .txt{color:var(--lose);font-weight:700}
.ev.you .dot{background:var(--text)}
.ev.system .txt{color:var(--muted)}
.pk{display:grid;grid-template-columns:auto minmax(0,1fr);gap:2px 10px;padding:7px 6px;border-bottom:1px solid rgba(148,178,214,.07);font:12px/1.4 var(--mono)}
.pk .dir{font-weight:700}
.pk.out .dir{color:var(--accent)}
.pk.in .dir{color:var(--ok)}
.pk .ty{color:var(--muted)}
.pk .tx{grid-column:1/-1;color:var(--text);word-break:break-word}

/* result pop-up */
.overlay{position:fixed;inset:0;z-index:30;display:grid;place-items:center;padding:20px;background:rgba(3,8,16,.6);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px)}
.modal{width:min(100%,430px);padding:28px 26px 24px;text-align:center;display:grid;gap:12px;animation:pop .3s ease-out}
@keyframes pop{from{opacity:0;transform:scale(.96)}}
.modal h2{margin:0;font-size:38px;letter-spacing:.03em}
.modal.win h2{color:var(--win);text-shadow:0 0 34px rgba(251,191,36,.35)}
.modal.lose h2{color:var(--lose)}
.modal p{margin:0;color:var(--muted)}
.modal .res-stats{font:13px var(--mono);color:var(--faint)}
.ask{display:grid;gap:10px;border-top:1px solid var(--line);padding-top:14px;margin-top:4px}
.ask strong{font-size:16px}
.actions{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}
.small{font-size:12.5px;color:var(--faint)!important}
.float{position:fixed;right:20px;bottom:20px;z-index:25;box-shadow:0 10px 30px rgba(0,0,0,.4)}

/* closed */
.closed{width:min(100%,480px);padding:28px 26px;display:grid;gap:14px;justify-items:center}
.closed h1{margin:0;font-size:24px}
.closed p{margin:0;color:var(--muted)}
.closed .note{font-size:13.5px;color:#fde68a;background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.35);border-radius:10px;padding:10px 12px}

/* toast + lost-contact bar */
#toast{position:fixed;left:50%;bottom:26px;z-index:40;transform:translate(-50%,16px);opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;
  background:#16263d;border:1px solid var(--line-2);padding:10px 16px;border-radius:10px;font-size:14px;max-width:min(92vw,520px);text-align:center}
#toast.show{opacity:1;transform:translate(-50%,0)}
.bridge-down{position:fixed;left:0;right:0;top:0;z-index:50;background:#7f1d1d;color:#fee2e2;text-align:center;padding:10px 16px;font-size:14px}

@media (max-width:1180px){
  .stage{grid-template-columns:minmax(0,1fr)}
  .side{position:static;height:420px}
}
@media (max-width:760px){
  .stage{padding:14px 16px}
  .boards{grid-template-columns:minmax(0,1fr)}
  .topbar{padding:10px 16px;gap:10px}
  .brand small,.secure span{display:none}
  .who{max-width:44vw}
}
@media (max-width:520px){
  .brand .long,.secure{display:none}
  .banner{flex-wrap:wrap}
  .timer{font-size:20px}
}
body[data-mode="online"] .only-local{display:none!important}
body:not([data-mode="online"]) .only-online{display:none!important}
.share{display:flex;gap:8px;align-items:center;justify-content:center;flex-wrap:wrap}
.share code{font-size:12.5px;word-break:break-all}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<div class="bridge-down" id="bridgeDown" hidden><span id="downMsg"></span></div>

<header class="topbar">
  <div class="brand">
    <svg class="logo" viewBox="0 0 40 40" aria-hidden="true"><circle cx="20" cy="20" r="18" fill="#0b2340" stroke="#38bdf8" stroke-width="1.5"/><circle cx="20" cy="20" r="11" fill="none" stroke="#38bdf8" stroke-opacity=".35"/><path d="M20 2v36M2 20h36" stroke="#38bdf8" stroke-opacity=".22"/><path d="M20 20 L20 2 A18 18 0 0 1 35.6 11 Z" fill="#38bdf8" fill-opacity=".3"/><circle cx="27" cy="12.5" r="2.6" fill="#ff6b3d"/></svg>
    <div class="brand-text">BEER <span class="long">Battleships</span><small>Engage in Explosive Rivalry</small></div>
  </div>
  <div class="spacer"></div>
  <span class="secure" title="Every packet has the BEER header (magic, type, sequence, IV, CRC32, length) and an AES-256-CTR encrypted payload">
    <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><rect x="3" y="7" width="10" height="7" rx="1.5" fill="currentColor"/><path d="M5 7V5a3 3 0 0 1 6 0v2" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>
    <span>AES-256 &middot; CRC32</span>
  </span>
  <span class="who" id="who" hidden><b id="whoName"></b><span id="whoVs"></span></span>
  <button class="btn sm ghost" id="leaveBtn" hidden>Leave</button>
</header>

<div class="stage solo" id="stage">
  <main>
    <!-- 1. join -->
    <section class="screen" id="scr-login">
      <div class="center">
        <div class="hero">
          <svg class="logo" viewBox="0 0 40 40" aria-hidden="true"><circle cx="20" cy="20" r="18" fill="#0b2340" stroke="#38bdf8" stroke-width="1.2"/><circle cx="20" cy="20" r="11" fill="none" stroke="#38bdf8" stroke-opacity=".35" stroke-width=".8"/><path d="M20 2v36M2 20h36" stroke="#38bdf8" stroke-opacity=".22" stroke-width=".8"/><path d="M20 20 L20 2 A18 18 0 0 1 35.6 11 Z" fill="#38bdf8" fill-opacity=".3"/><circle cx="27" cy="12.5" r="2.4" fill="#ff6b3d"/></svg>
          <h1>BEER Battleships</h1>
          <p class="tag">Engage in Explosive Rivalry</p>
        </div>
        <form class="card login" id="joinForm" autocomplete="on">
          <label class="field">Player name
            <input id="name" name="username" autocomplete="username" maxlength="20" required spellcheck="false">
          </label>
          <label class="field">Password
            <input id="pw" name="password" type="password" autocomplete="current-password" maxlength="64" required>
          </label>
          <button class="btn primary" id="joinBtn" type="submit">Join game</button>
          <button class="btn" id="soloBtn" type="button">Play vs computer</button>
          <p class="hint">New name? You'll be registered. Coming back? Use the same password. Playing the computer needs no password.</p>
          <p class="error" id="joinErr" role="alert"></p>
        </form>
        <p class="foot only-local">Game server <code id="srvAddr">127.0.0.1:5050</code> &middot; every packet is AES-256-CTR encrypted and CRC32-checked by your BEER protocol</p>
        <p class="foot only-online">The game server behind this page speaks the BEER protocol: every packet is AES-256-CTR encrypted and CRC32-checked.</p>
      </div>
    </section>

    <!-- 2. waiting room -->
    <section class="screen" id="scr-lobby" hidden>
      <div class="center">
        <div class="card lobby">
          <div class="radar"><i></i></div>
          <h1 id="lobbyTitle">You're in the queue</h1>
          <div class="qpos" id="queuePos" hidden>#1</div>
          <p id="lobbySub">Waiting for an opponent...</p>
          <div class="stuck" id="stuck" hidden>
            <span>The server logged you in but hasn't placed you yet. Wait a few more seconds; if nothing happens, leave and join again.</span>
            <button class="btn sm" id="stuckLeave">Leave</button>
          </div>
          <p class="tip only-local">Playing on your own? Open another terminal in this folder and run <code>python3 web_client.py</code> again. It opens a second player in a new tab.</p>
          <div class="tip only-online" id="shareBox">
            <p id="shareText">Waiting for your friend? Send them this link:</p>
            <div class="share" id="shareRow"><code id="shareLink"></code><button class="btn sm" id="copyLink" type="button">Copy link</button></div>
          </div>
        </div>
      </div>
    </section>

    <!-- 3. the match -->
    <section class="screen" id="scr-game" hidden>
      <div class="card banner" id="banner">
        <span class="pill" id="bPill">Your turn</span>
        <div class="banner-text"><strong id="bTitle"></strong><span id="bSub"></span></div>
        <div class="timer" id="timer" hidden><span id="timerNum">60</span><small>s</small></div>
        <div class="tbar" id="tbar" hidden><i id="timerBar"></i></div>
      </div>
      <div class="boards">
        <div class="card board-card" id="enemyCard">
          <div class="board-head"><h2>Enemy waters</h2><span class="sub" id="enemySub"></span></div>
          <div class="board-wrap"><div id="enemyBoard"></div></div>
          <div class="under">
            <form class="typed" id="typedForm">
              <input id="typed" placeholder="e.g. B5" maxlength="3" autocomplete="off" spellcheck="false" aria-label="Square to fire at">
              <button class="btn primary sm" id="typedBtn" type="submit">Fire</button>
            </form>
            <span class="aim" id="aim"></span>
          </div>
          <p class="fleet-label">Enemy ships</p>
          <div class="fleet" id="enemyFleet"></div>
        </div>
        <div class="card board-card" id="myCard">
          <div class="board-head"><h2>Your fleet</h2><span class="sub" id="mySub"></span></div>
          <div class="board-wrap"><div id="myBoard"></div><p class="board-note" id="myNote" hidden>Your ships appear when your first turn starts.</p></div>
          <p class="fleet-label">Your ships</p>
          <div class="fleet" id="myFleet"></div>
        </div>
      </div>
      <div class="card statsrow">
        <div class="stat"><b id="stShots">0</b><span>Shots</span></div>
        <div class="stat"><b id="stHits">0</b><span>Hits</span></div>
        <div class="stat"><b id="stAcc">0%</b><span>Accuracy</span></div>
        <div class="stat"><b id="stSunk">0/5</b><span>Ships sunk</span></div>
        <div class="stat"><b id="stLost">0/5</b><span>Ships lost</span></div>
        <div class="spacer"></div>
        <button class="btn sm danger" id="giveUp" disabled>Give up</button>
      </div>
    </section>

    <!-- 4. spectating -->
    <section class="screen" id="scr-spectate" hidden>
      <div class="card banner">
        <span class="pill">Spectating</span>
        <div class="banner-text"><strong id="specTitle"></strong><span id="specSub"></span></div>
      </div>
      <div class="boards">
        <div class="card board-card" id="specCard1">
          <div class="board-head"><h2 id="specH1"></h2><span class="sub" id="specS1"></span></div>
          <div class="board-wrap"><div id="specBoard1"></div></div>
        </div>
        <div class="card board-card" id="specCard2">
          <div class="board-head"><h2 id="specH2"></h2><span class="sub" id="specS2"></span></div>
          <div class="board-wrap"><div id="specBoard2"></div></div>
        </div>
      </div>
      <p class="spec-note">Spectators see hits and misses only. Ship positions stay secret. You'll be picked to play when a place opens up.</p>
    </section>

    <!-- 5. session over -->
    <section class="screen" id="scr-closed" hidden>
      <div class="center">
        <div class="card closed">
          <h1>Session ended</h1>
          <p id="closedText"></p>
          <p class="note" id="closedNote" hidden></p>
          <button class="btn primary" id="backBtn">Back to start</button>
        </div>
      </div>
    </section>
  </main>

  <aside class="card side">
    <div class="tabs" role="tablist">
      <button class="tab" role="tab" data-tab="feed" aria-selected="true">Activity</button>
      <button class="tab" role="tab" data-tab="packets" aria-selected="false">Packets</button>
    </div>
    <p class="side-note" id="sideNote">Match updates, newest first.</p>
    <ul class="list" id="feed"></ul>
    <ul class="list" id="packets" hidden></ul>
  </aside>
</div>

<div class="overlay" id="overlay" hidden>
  <div class="card modal" id="modal" role="dialog" aria-modal="true" aria-labelledby="resTitle">
    <h2 id="resTitle">Victory</h2>
    <p id="resText"></p>
    <p class="res-stats" id="resStats"></p>
    <div class="ask" id="askBox">
      <strong id="askTitle">Play again?</strong>
      <div class="actions" id="askBtns">
        <button class="btn primary" id="againYes">Play again</button>
        <button class="btn" id="againNo">No thanks</button>
      </div>
      <p class="small" id="askInfo"></p>
    </div>
    <div class="actions"><button class="btn sm ghost" id="viewBoards">View the boards</button></div>
  </div>
</div>
<button class="btn sm float" id="showResult" hidden>Show result</button>
<div id="toast" role="status" aria-live="polite"></div>

<script>
(function () {
  "use strict";
  const $ = (sel) => document.querySelector(sel);
  const LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
  let S = null, version = -1, session = null, recvAt = performance.now();
  let lastLogId = 0, lastPktId = 0, lastMatch = null, overlayHidden = false, firstPaint = true, screen = null;
  let N = 10;
  const boards = {};
  const ONLINE = document.body.dataset.mode === "online";

  // Each browser tab is its own player. web_gateway.py uses this id to tell tabs apart;
  // web_client.py ignores it. The ngrok header skips ngrok's warning page for these requests.
  let TOKEN = "";
  try { TOKEN = sessionStorage.getItem("beer-session") || ""; } catch (e) {}
  if (!/^[0-9a-f]{32}$/.test(TOKEN)) {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    TOKEN = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    try { sessionStorage.setItem("beer-session", TOKEN); } catch (e) {}
  }
  const HDRS = { "X-Beer-Session": TOKEN, "ngrok-skip-browser-warning": "1" };

  function h(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function setText(sel, text) { const e = $(sel); if (e.textContent !== text) e.textContent = text; }
  function cellName(rc) { return LETTERS[rc[0]] + (rc[1] + 1); }
  function clock(t) { const d = new Date(t * 1000), p = (n) => String(n).padStart(2, "0"); return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds()); }
  function accuracy(m) { return m.shots ? Math.round((100 * m.hits) / m.shots) + "%" : "0%"; }

  let toastTimer = null;
  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 3200);
  }

  async function post(path, body) {
    try {
      const r = await fetch(path, { method: "POST", headers: Object.assign({ "Content-Type": "application/json" }, HDRS), body: JSON.stringify(body || {}) });
      return await r.json();
    } catch (e) {
      return { ok: false, error: ONLINE ? "Can't reach the game server. Check your internet connection." : "Can't reach web_client.py. Is it still running?" };
    }
  }

  // ---------- boards ----------
  function buildBoard(id, hostSel, clickable) {
    const host = $(hostSel);
    host.textContent = "";
    host.className = "board";
    host.style.gridTemplateColumns = `18px repeat(${N}, minmax(0, 1fr))`;
    host.style.gridTemplateRows = `18px repeat(${N}, minmax(0, 1fr))`;
    host.appendChild(h("div", "lbl"));
    for (let c = 0; c < N; c++) host.appendChild(h("div", "lbl", String(c + 1)));
    const cells = [];
    for (let r = 0; r < N; r++) {
      host.appendChild(h("div", "lbl", LETTERS[r]));
      const row = [];
      for (let c = 0; c < N; c++) {
        const b = h(clickable ? "button" : "div", "cell");
        b.dataset.name = LETTERS[r] + (c + 1);
        if (clickable) {
          b.type = "button";
          b.disabled = true;
          b.addEventListener("click", () => fire(b.dataset.name));
        } else {
          b.setAttribute("role", "img");
        }
        host.appendChild(b);
        row.push(b);
      }
      cells.push(row);
    }
    boards[id] = cells;
  }

  function paint(id, grid, o) {
    const cells = boards[id];
    if (!cells) return;
    const pulse = new Set((o.pulse || []).map((rc) => rc[0] * 100 + rc[1]));
    const pend = o.pending ? o.pending[0] * 100 + o.pending[1] : -1;
    for (let r = 0; r < N; r++) {
      for (let c = 0; c < N; c++) {
        const v = grid ? grid[r][c] : ".";
        const b = cells[r][c];
        const k = r * 100 + c;
        let cls = "cell";
        if (v === "S") cls += " ship";
        else if (v === "X") cls += o.mine ? " ship hit" : " hit";
        else if (v === "o") cls += " miss";
        if (k === pend) cls += " pending";
        else if (v === "." && o.target) cls += " target";
        if (pulse.has(k)) cls += " pulse";
        if (b.className !== cls) b.className = cls;
        const what = v === "S" ? "your ship" : v === "X" ? "hit" : v === "o" ? "miss" : o.target ? "open water, fire here" : "water";
        const label = b.dataset.name + ": " + what;
        if (b.getAttribute("aria-label") !== label) b.setAttribute("aria-label", label);
        if (b.tagName === "BUTTON") b.disabled = !(o.target && v === "." && k !== pend);
      }
    }
  }

  function fleet(sel, ships, sunk) {
    const host = $(sel);
    const left = sunk.slice();
    const list = ships.map(([name, size]) => {
      const i = left.indexOf(name);
      if (i >= 0) left.splice(i, 1);
      return { name, size, sunk: i >= 0 };
    });
    const key = JSON.stringify(list);
    if (host.dataset.key === key) return;
    host.dataset.key = key;
    host.textContent = "";
    for (const s of list) {
      const chip = h("span", "ship-chip" + (s.sunk ? " sunk" : ""));
      const pips = h("i");
      for (let k = 0; k < s.size; k++) pips.appendChild(h("b"));
      chip.appendChild(pips);
      chip.appendChild(document.createTextNode(s.name));
      chip.title = `${s.name} (${s.size} squares)${s.sunk ? " - sunk" : ""}`;
      host.appendChild(chip);
    }
  }

  // ---------- actions ----------
  async function fire(cell) {
    if (!S || !S.match.can_fire) return;
    const res = await post("/api/fire", { cell });
    if (!res.ok && res.error) toast(res.error);
  }

  function confirmButton(btn, confirmText, needsConfirm, action) {
    const label = btn.textContent;
    let armed = null;
    const disarm = () => { clearTimeout(armed); armed = null; btn.textContent = label; btn.classList.remove("armed"); };
    btn.addEventListener("click", async () => {
      if (needsConfirm() && !armed) {
        btn.textContent = confirmText;
        btn.classList.add("armed");
        armed = setTimeout(disarm, 3000);
        return;
      }
      disarm();
      const res = await action();
      if (res && !res.ok && res.error) toast(res.error);
    });
  }

  // ---------- rendering ----------
  function showScreen(name) {
    if (screen === name) return;
    screen = name;
    for (const sc of document.querySelectorAll(".screen")) sc.hidden = sc.id !== "scr-" + name;
    $("#stage").classList.toggle("solo", name === "login");
    if (name === "login") {
      const input = $("#name");
      let saved = "";
      try { saved = localStorage.getItem("beer-name") || ""; } catch (e) {}
      if (!input.value) input.value = (S && S.username) || saved;
      (input.value ? $("#pw") : input).focus();
    }
  }

  function renderHeader(s) {
    const showWho = !!s.username && s.phase !== "login";
    $("#who").hidden = !showWho;
    setText("#whoName", s.username || "");
    const m = s.match;
    let vs = "";
    if ((s.phase === "playing" || s.phase === "game_over") && m.opponent) vs = " vs " + m.opponent;
    else if (s.phase === "spectating") vs = " · spectating";
    setText("#whoVs", vs);
    $("#leaveBtn").hidden = !(s.connected || s.solo);
    setText("#srvAddr", s.server);
  }

  function renderLobby(s) {
    const q = s.phase === "queue";
    setText("#lobbyTitle", s.phase === "lobby" ? "Connected" : q && s.queue_pos ? "You're in the queue" : "Get ready");
    $("#queuePos").hidden = !(q && s.queue_pos);
    setText("#queuePos", "#" + (s.queue_pos || ""));
    let sub;
    if (s.phase === "lobby") sub = "Waiting for the server to place you...";
    else if (s.queue_pos === 1) sub = "Waiting for an opponent to join...";
    else if (s.queue_pos) sub = "Your match starts in a moment...";
    else sub = "Waiting for your next match to start...";
    setText("#lobbySub", sub);
    $("#stuck").hidden = !s.stuck;
  }

  function renderGame(s) {
    const m = s.match;
    const opp = m.opponent || "Your opponent";
    paint("enemy", m.enemy_board, { target: m.can_fire, pulse: m.last_shot ? [m.last_shot] : [], pending: m.pending_shot });
    paint("mine", m.my_board, { mine: true, pulse: m.incoming });
    setText("#enemySub", m.opponent ? m.opponent + "'s fleet" : "");
    setText("#mySub", s.username || "");
    $("#myNote").hidden = !!m.my_board || s.phase !== "playing";
    fleet("#enemyFleet", s.ships, m.enemy_sunk);
    fleet("#myFleet", s.ships, m.my_sunk);
    $("#enemyCard").classList.toggle("active", m.can_fire);

    let pill, title, sub, cls = "";
    if (s.phase === "game_over") {
      pill = m.outcome === "win" ? "Victory" : "Defeat";
      title = m.outcome === "win" ? "You win!" : "You lose.";
      sub = m.outcome_text || "";
      cls = m.outcome || "";
    } else if (m.opponent_away) {
      pill = "Paused"; title = opp + " disconnected"; sub = "The server waits up to 60 seconds for them to come back."; cls = "warn";
    } else if (m.can_fire) {
      pill = "Your turn"; title = "Fire at enemy waters"; sub = "Click a square, or type one like B5 and press Fire."; cls = "mine";
    } else if (m.pending_shot) {
      pill = "Firing"; title = "Firing at " + cellName(m.pending_shot) + "..."; sub = "Waiting for the result."; cls = "mine";
    } else if (m.my_turn) {
      pill = "Your turn"; title = "Get ready..."; sub = ""; cls = "mine";
    } else if (m.opponent) {
      pill = opp + "'s turn"; title = opp + " is aiming...";
      sub = m.timeouts ? `You've run out of time ${m.timeouts}/3 times. A third time loses the match.` : "Their shots show up on your fleet.";
    } else {
      pill = "Match"; title = "Waiting for the server..."; sub = "Your boards appear on your next turn.";
    }
    $("#banner").className = "card banner " + cls;
    setText("#bPill", pill); setText("#bTitle", title); setText("#bSub", sub);

    setText("#stShots", String(m.shots));
    setText("#stHits", String(m.hits));
    setText("#stAcc", accuracy(m));
    setText("#stSunk", `${m.enemy_sunk.length}/${s.ships.length}`);
    setText("#stLost", `${m.my_sunk.length}/${s.ships.length}`);
    $("#typed").disabled = !m.can_fire;
    $("#typedBtn").disabled = !m.can_fire;
    $("#giveUp").disabled = !m.can_fire;
    if (!m.can_fire) setText("#aim", "");
    renderResult(s);
  }

  function renderResult(s) {
    const m = s.match;
    if (s.match_id !== lastMatch) { lastMatch = s.match_id; overlayHidden = false; }
    const show = screen === "game" && s.phase === "game_over" && !!m.outcome;
    $("#overlay").hidden = !show || overlayHidden;
    $("#showResult").hidden = !show || !overlayHidden;
    if (!show) return;
    $("#modal").className = "card modal " + m.outcome;
    setText("#resTitle", m.outcome === "win" ? "Victory" : "Defeat");
    setText("#resText", m.outcome_text || "");
    setText("#resStats", `${m.shots} shots · ${m.hits} hits · ${accuracy(m)} accuracy`);
    $("#askBtns").hidden = !m.ask_play_again;
    if (m.ask_play_again && s.solo) {
      setText("#askTitle", "Play again?");
      setText("#askInfo", "A new game against the computer starts straight away.");
    } else if (m.ask_play_again) {
      setText("#askTitle", "Play again?");
    } else if (m.play_again_answer === "y") {
      setText("#askTitle", "You're in for another match");
      setText("#askInfo", "If your opponent also said yes, the rematch starts in a moment. Otherwise you'll play the next person who joins.");
    } else if (m.play_again_answer === "n") {
      setText("#askTitle", "Thanks for playing");
      setText("#askInfo", "The server will close your session.");
    } else if (s.connected) {
      setText("#askTitle", "Waiting for the server...");
      setText("#askInfo", "");
    } else {
      setText("#askTitle", "");
      setText("#askInfo", "");
    }
  }

  function renderSpectate(s) {
    const sp = s.spec;
    const p1 = sp.p1 || "Player 1", p2 = sp.p2 || "Player 2";
    setText("#specTitle", sp.p1 ? `${p1} vs ${p2}` : "Waiting for a match");
    let sub;
    if (sp.winner) sub = `${sp.winner} won the match. Waiting for the next one...`;
    else if (sp.active) sub = sp.turn ? `${sp.turn}'s turn` : "Match in progress";
    else sub = "No match right now.";
    setText("#specSub", sub);
    setText("#specH1", `${p1}'s shots`); setText("#specS1", `at ${p2}'s fleet`);
    setText("#specH2", `${p2}'s shots`); setText("#specS2", `at ${p1}'s fleet`);
    paint("spec1", sp.view1, { pulse: sp.last1 });
    paint("spec2", sp.view2, { pulse: sp.last2 });
    $("#specCard1").classList.toggle("active", !!sp.turn && sp.turn === sp.p1);
    $("#specCard2").classList.toggle("active", !!sp.turn && sp.turn === sp.p2);
  }

  function renderClosed(s) {
    setText("#closedText", s.notice || "Your session has ended.");
    const note = $("#closedNote");
    if (s.can_resume) {
      note.hidden = false;
      setText("#closedNote", "Your match is kept for 60 seconds. Join again with the same name and password to carry on.");
    } else {
      note.hidden = true;
    }
  }

  function renderSide(s) {
    if (s.session !== session) {
      session = s.session;
      lastLogId = 0;
      $("#feed").textContent = "";
    }
    const feed = $("#feed");
    for (const e of s.log) {
      if (e.id <= lastLogId) continue;
      lastLogId = e.id;
      const li = h("li", "ev " + e.kind + (firstPaint ? "" : " fresh"));
      li.appendChild(h("span", "dot"));
      li.appendChild(h("span", "txt", e.text));
      li.appendChild(h("time", null, clock(e.t)));
      feed.prepend(li);
    }
    while (feed.children.length > 150) feed.lastChild.remove();
    const pk = $("#packets");
    for (const p of s.packets) {
      if (p.id <= lastPktId) continue;
      lastPktId = p.id;
      const li = h("li", "pk " + p.dir);
      li.appendChild(h("span", "dir", p.dir === "out" ? "↑ sent" : "↓ received"));
      li.appendChild(h("span", "ty", `${p.type} · ${p.bytes} bytes · ${clock(p.t)}`));
      li.appendChild(h("span", "tx", p.text));
      pk.prepend(li);
    }
    while (pk.children.length > 150) pk.lastChild.remove();
  }

  function render(s) {
    S = s;
    if (s.board_size && s.board_size !== N) {
      N = s.board_size;
      build();
    }
    const screens = { login: "login", lobby: "lobby", queue: "lobby", spectating: "spectate", playing: "game", game_over: "game", closed: "closed" };
    showScreen(screens[s.phase] || "login");
    renderHeader(s);
    if (screen === "lobby") renderLobby(s);
    if (screen === "game") renderGame(s); else renderResult(s);
    if (screen === "spectate") renderSpectate(s);
    if (screen === "closed") renderClosed(s);
    renderSide(s);
    firstPaint = false;
    const mine = s.phase === "playing" && s.match.can_fire;
    document.title = (mine ? "● Your turn · " : "") + "BEER Battleships" + (s.username && s.phase !== "login" ? " · " + s.username : "");
    tick();
  }

  // countdowns (60 s per shot, 30 s to answer "play again")
  function tick() {
    if (!S) return;
    const m = S.match;
    const since = (performance.now() - recvAt) / 1000;
    let left = null;
    if (S.phase === "playing" && m.can_fire && S.turn_elapsed != null) left = S.turn_seconds - S.turn_elapsed - since;
    else if (S.phase === "playing" && !m.my_turn && !m.opponent_away && m.opponent && S.opp_elapsed != null) left = S.turn_seconds - S.opp_elapsed - since;
    const timer = $("#timer"), tbar = $("#tbar");
    if (left == null) {
      timer.hidden = true; tbar.hidden = true;
    } else {
      left = Math.max(0, left);
      timer.hidden = false; tbar.hidden = false;
      setText("#timerNum", String(Math.ceil(left)));
      timer.classList.toggle("low", left <= 15 && left > 5);
      timer.classList.toggle("crit", left <= 5);
      $("#timerBar").style.transform = `scaleX(${left / S.turn_seconds})`;
    }
    if (m.ask_play_again && S.again_elapsed != null) {
      const l2 = Math.max(0, S.again_seconds - S.again_elapsed - since);
      setText("#askInfo", `${Math.ceil(l2)} seconds left to answer. No answer counts as no.`);
    }
  }
  setInterval(tick, 250);

  // ---------- talking to web_client.py ----------
  async function loop() {
    for (;;) {
      try {
        const r = await fetch(`/api/state?v=${version}&l=${lastLogId}&p=${lastPktId}`, { cache: "no-store", headers: HDRS });
        if (!r.ok) {
          let msg = "";
          try { msg = (await r.json()).error || ""; } catch (e) {}
          throw new Error(msg);
        }
        const s = await r.json();
        recvAt = performance.now();
        $("#bridgeDown").hidden = true;
        version = s.version;
        render(s);
      } catch (e) {
        const msg = (e && e.message) || (ONLINE
          ? "Lost contact with the game server. Check your internet connection. If it keeps happening, the host may have stopped the game."
          : "Lost contact with web_client.py. Is it still running in your terminal? Start it again, then refresh this page.");
        setText("#downMsg", msg);
        $("#bridgeDown").hidden = false;
        await new Promise((res) => setTimeout(res, e && e.message ? 4000 : 1500));
      }
    }
  }

  function build() {
    buildBoard("enemy", "#enemyBoard", true);
    buildBoard("mine", "#myBoard", false);
    buildBoard("spec1", "#specBoard1", false);
    buildBoard("spec2", "#specBoard2", false);
  }

  // ---------- wiring ----------
  build();
  if (ONLINE) {
    const onThisComputer = ["127.0.0.1", "localhost"].includes(location.hostname);
    if (onThisComputer) {
      setText("#shareText", "Your friend joins through your ngrok link, or the Play button on your website.");
      $("#shareRow").hidden = true;
    } else {
      setText("#shareLink", location.origin);
      $("#copyLink").addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(location.origin); toast("Link copied"); }
        catch (e) { toast("Copy this link: " + location.origin); }
      });
    }
  }
  $("#joinForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = $("#joinBtn"), err = $("#joinErr");
    const username = $("#name").value.trim(), password = $("#pw").value;
    err.textContent = "";
    btn.disabled = true;
    btn.textContent = "Connecting...";
    const res = await post("/api/join", { username, password });
    btn.disabled = false;
    btn.textContent = "Join game";
    if (!res.ok) { err.textContent = res.error || "Couldn't join."; return; }
    try { localStorage.setItem("beer-name", username); } catch (e) {}
    $("#pw").value = "";
  });
  $("#soloBtn").addEventListener("click", async () => {
    setText("#joinErr", "");
    const res = await post("/api/solo", { username: $("#name").value.trim() });
    if (!res.ok) setText("#joinErr", res.error || "Couldn't start a game against the computer.");
  });
  $("#typedForm").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const v = $("#typed").value.trim().toUpperCase();
    const ok = new RegExp("^[A-" + LETTERS[N - 1] + "]([1-9]|10)$").test(v);
    if (!ok) { toast(`Type a square from A1 to ${LETTERS[N - 1]}${N}, like B5.`); return; }
    $("#typed").value = "";
    fire(v);
  });
  $("#enemyBoard").addEventListener("mouseover", (ev) => {
    const b = ev.target.closest(".cell.target");
    setText("#aim", b ? "Aim: " + b.dataset.name : "");
  });
  $("#enemyBoard").addEventListener("mouseleave", () => setText("#aim", ""));
  confirmButton($("#leaveBtn"), "Click again to leave", () => !!S && S.phase === "playing", () => post("/api/leave"));
  confirmButton($("#giveUp"), "Sure? Click again", () => true, () => post("/api/forfeit"));
  $("#stuckLeave").addEventListener("click", () => post("/api/leave"));
  $("#backBtn").addEventListener("click", () => post("/api/reset"));
  $("#againYes").addEventListener("click", async () => { const r = await post("/api/answer", { yes: true }); if (!r.ok) toast(r.error); });
  $("#againNo").addEventListener("click", async () => { const r = await post("/api/answer", { yes: false }); if (!r.ok) toast(r.error); });
  $("#viewBoards").addEventListener("click", () => { overlayHidden = true; if (S) render(S); });
  $("#showResult").addEventListener("click", () => { overlayHidden = false; if (S) render(S); });
  for (const t of document.querySelectorAll(".tab")) {
    t.addEventListener("click", () => {
      for (const u of document.querySelectorAll(".tab")) u.setAttribute("aria-selected", String(u === t));
      const pk = t.dataset.tab === "packets";
      $("#feed").hidden = pk;
      $("#packets").hidden = !pk;
      setText("#sideNote", pk
        ? "Every BEER packet this client sent or received, newest first. Size = 32-byte header (magic, type, seq, IV, CRC32, length) + encrypted payload."
        : "Match updates, newest first.");
    });
  }
  loop();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
