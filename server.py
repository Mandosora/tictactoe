"""Tic-Tac-Toe Online - game server.

Run:   py server.py
Open:  http://localhost:8000  (or the network address it prints, from a phone on the same Wi-Fi)

Uses only the Python standard library: accounts are stored in SQLite (data/app.db),
games live in memory, and the browser gets live updates through long polling.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import traceback
from contextlib import closing
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
DB_PATH = ROOT / "data" / "app.db"
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

SESSION_SECONDS = 30 * 24 * 3600   # stay logged in for 30 days
LONG_POLL_SECONDS = 25             # how long a "wait for changes" request is held open
ONLINE_WINDOW = LONG_POLL_SECONDS + 10
PBKDF2_ROUNDS = 200_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I confusion
WIN_LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8),
             (0, 3, 6), (1, 4, 7), (2, 5, 8),
             (0, 4, 8), (2, 4, 6))
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- database

def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def db_exec(sql, args=()):
    with closing(db()) as conn, conn:
        return conn.execute(sql, args).lastrowid


def db_one(sql, args=()):
    with closing(db()) as conn:
        return conn.execute(sql, args).fetchone()


def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    with closing(db()) as conn, conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id       INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                pw_hash  TEXT NOT NULL,
                salt     TEXT NOT NULL,
                wins     INTEGER NOT NULL DEFAULT 0,
                losses   INTEGER NOT NULL DEFAULT 0,
                draws    INTEGER NOT NULL DEFAULT 0,
                created  REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token   TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                expires REAL NOT NULL
            );
        """)


def hash_password(password, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), PBKDF2_ROUNDS).hex()


def create_session(user_id):
    token = secrets.token_urlsafe(32)
    db_exec("INSERT INTO sessions (token, user_id, expires) VALUES (?, ?, ?)",
            (token, user_id, time.time() + SESSION_SECONDS))
    return token


def user_from_token(token):
    if not token:
        return None
    row = db_one("""SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id
                    WHERE s.token = ? AND s.expires > ?""", (token, time.time()))
    return {"id": row["id"], "name": row["username"]} if row else None


def user_stats(user_id):
    row = db_one("SELECT wins, losses, draws FROM users WHERE id = ?", (user_id,))
    return dict(row) if row else {"wins": 0, "losses": 0, "draws": 0}


def record_result(game):
    x_id, o_id = game.players["X"]["id"], game.players["O"]["id"]
    with closing(db()) as conn, conn:
        if game.winner == "draw":
            conn.execute("UPDATE users SET draws = draws + 1 WHERE id IN (?, ?)", (x_id, o_id))
        else:
            winner_id = game.players[game.winner]["id"]
            loser_id = o_id if winner_id == x_id else x_id
            conn.execute("UPDATE users SET wins = wins + 1 WHERE id = ?", (winner_id,))
            conn.execute("UPDATE users SET losses = losses + 1 WHERE id = ?", (loser_id,))


# --------------------------------------------------------------------------- games
# All game state is guarded by `lock`. Changing a game calls notify_all() so that
# players waiting on a long poll get the update straight away.

lock = threading.Condition()
games = {}   # game id -> Game
codes = {}   # join code -> game id (only while the game is waiting for a player)


def other(symbol):
    return "O" if symbol == "X" else "X"


def new_code():
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
        if code not in codes:
            return code


class Game:
    def __init__(self, creator, public):
        self.id = secrets.token_urlsafe(9)
        self.code = new_code()
        self.public = public
        self.players = {"X": creator, "O": None}
        self.board = [None] * 9
        self.turn = "X"
        self.status = "waiting"      # waiting -> playing -> finished (or closed)
        self.winner = None           # "X", "O" or "draw"
        self.line = None             # winning cells
        self.left_by = None          # symbol of a player who left
        self.rematch = set()         # user ids who asked for a rematch
        self.round = 1
        self.version = 0
        self.updated = time.time()
        self.seen = {creator["id"]: time.time()}
        games[self.id] = self
        codes[self.code] = self.id
        self.changed()

    def changed(self):
        self.version += 1
        self.updated = time.time()
        lock.notify_all()

    def symbol_of(self, user_id):
        for symbol, player in self.players.items():
            if player and player["id"] == user_id:
                return symbol
        return None

    def close(self):
        self.status = "closed"
        games.pop(self.id, None)
        if codes.get(self.code) == self.id:
            del codes[self.code]
        self.changed()

    def join(self, user):
        if self.symbol_of(user["id"]):
            return
        if self.status != "waiting":
            raise ApiError("That game has already started.", 409)
        self.players["O"] = user
        self.status = "playing"
        self.seen[user["id"]] = time.time()
        codes.pop(self.code, None)
        self.changed()

    def move(self, user_id, cell):
        symbol = self.symbol_of(user_id)
        if self.status != "playing":
            raise ApiError("The game is not in progress.")
        if symbol != self.turn:
            raise ApiError("It's not your turn.")
        if isinstance(cell, bool) or not isinstance(cell, int) or not 0 <= cell <= 8:
            raise ApiError("Invalid square.")
        if self.board[cell]:
            raise ApiError("That square is already taken.")
        self.board[cell] = symbol
        for line in WIN_LINES:
            if all(self.board[i] == symbol for i in line):
                self.finish(symbol, list(line))
                break
        else:
            if all(self.board):
                self.finish("draw")
            else:
                self.turn = other(symbol)
        self.changed()

    def finish(self, winner, line=None):
        self.status = "finished"
        self.winner = winner
        self.line = line
        record_result(self)

    def ask_rematch(self, user_id):
        if self.status != "finished" or self.left_by:
            raise ApiError("A rematch isn't available.")
        self.rematch.add(user_id)
        if len(self.rematch) == 2:
            # Swap sides so the other player starts this time.
            self.players["X"], self.players["O"] = self.players["O"], self.players["X"]
            self.board = [None] * 9
            self.turn = "X"
            self.status = "playing"
            self.winner = None
            self.line = None
            self.rematch.clear()
            self.round += 1
        self.changed()

    def leave(self, user_id):
        symbol = self.symbol_of(user_id)
        if self.status == "waiting":
            self.close()
        elif self.status == "playing":
            self.left_by = symbol
            self.finish(other(symbol))   # leaving mid-game counts as a forfeit
            self.changed()
        elif self.status == "finished" and not self.left_by:
            self.left_by = symbol
            self.changed()

    def view(self, user_id):
        you = self.symbol_of(user_id)
        opponent = self.players[other(you)]
        now = time.time()
        return {
            "id": self.id,
            "code": self.code,
            "public": self.public,
            "status": self.status,
            "board": self.board,
            "turn": self.turn,
            "winner": self.winner,
            "line": self.line,
            "round": self.round,
            "version": self.version,
            "you": you,
            "players": {s: (p["name"] if p else None) for s, p in self.players.items()},
            "leftBy": self.left_by,
            "rematchYou": user_id in self.rematch,
            "rematchOpponent": bool(opponent) and opponent["id"] in self.rematch,
            "opponentOnline": bool(opponent) and now - self.seen.get(opponent["id"], 0) < ONLINE_WINDOW,
        }


def find_game(user, game_id):
    game = games.get(game_id)
    if not game or not game.symbol_of(user["id"]):
        raise ApiError("Game not found.", 404)
    game.seen[user["id"]] = time.time()
    return game


def active_game_for(user_id):
    mine = [g for g in games.values() if g.symbol_of(user_id) and g.status in ("waiting", "playing")]
    return max(mine, key=lambda g: g.updated) if mine else None


def ensure_free(user):
    """Cancel any game this user is still waiting in; refuse if they're mid-game."""
    for game in list(games.values()):
        if game.symbol_of(user["id"]):
            if game.status == "waiting":
                game.close()
            elif game.status == "playing":
                raise ApiError("Finish or leave your current game first.", 409)


def create_game(user):
    with lock:
        ensure_free(user)
        return Game(user, public=False).view(user["id"])


def quick_match(user):
    with lock:
        ensure_free(user)
        waiting = [g for g in games.values() if g.public and g.status == "waiting"]
        if waiting:
            game = min(waiting, key=lambda g: g.updated)
            game.join(user)
        else:
            game = Game(user, public=True)
        return game.view(user["id"])


def join_by_code(user, code):
    code = str(code or "").strip().upper()
    with lock:
        game = games.get(codes.get(code, ""))
        if not game:
            raise ApiError("No game found with that code.", 404)
        if game.symbol_of(user["id"]):
            return game.view(user["id"])
        ensure_free(user)
        game.join(user)
        return game.view(user["id"])


def poll_game(user, game_id, query):
    """Return the game as soon as its version differs from ?v=, or after LONG_POLL_SECONDS."""
    try:
        since = int(query.get("v", ["-1"])[0])
    except ValueError:
        since = -1
    with lock:
        game = find_game(user, game_id)
        lock.wait_for(lambda: game.version != since, timeout=LONG_POLL_SECONDS)
        game.seen[user["id"]] = time.time()
        return game.view(user["id"])


def game_action(user, game_id, action, body):
    with lock:
        game = find_game(user, game_id)
        if action == "move":
            game.move(user["id"], body.get("cell"))
        elif action == "rematch":
            game.ask_rematch(user["id"])
        elif action == "leave":
            game.leave(user["id"])
        return game.view(user["id"])


def janitor():
    """Remove old games and expired sessions."""
    while True:
        time.sleep(60)
        now = time.time()
        with lock:
            for game in list(games.values()):
                idle = now - game.updated
                if (game.status == "finished" and idle > 10 * 60) or idle > 60 * 60:
                    game.close()
        try:
            db_exec("DELETE FROM sessions WHERE expires < ?", (now,))
        except sqlite3.Error:
            traceback.print_exc()


# --------------------------------------------------------------------------- HTTP

def session_cookie(token, secure, max_age=SESSION_SECONDS):
    flags = "; Secure" if secure else ""
    return ("Set-Cookie", f"session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{flags}")


class Handler(BaseHTTPRequestHandler):
    server_version = "TicTacToe/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # keep the console quiet; errors are still printed

    # ---- helpers
    def send_body(self, status, body, content_type, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, status=200, headers=()):
        self.send_body(status, json.dumps(obj).encode(), "application/json; charset=utf-8", headers)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 10_000:
            self.close_connection = True
            raise ApiError("Request too large.", 413)
        raw = self.rfile.read(length) if length else b""
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise ApiError("Expected JSON.", 415)
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError("Invalid JSON.")
        if not isinstance(data, dict):
            raise ApiError("Invalid JSON.")
        return data

    def session_token(self):
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return None
        morsel = jar.get("session")
        return morsel.value if morsel else None

    def is_https(self):
        # Hosting services (e.g. Render) handle HTTPS in front of us and say so in this header.
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def require_user(self):
        user = user_from_token(self.session_token())
        if not user:
            raise ApiError("Please log in.", 401)
        return user

    # ---- entry points
    def do_GET(self):
        url = urlparse(self.path)
        if url.path.startswith("/api/"):
            self.handle_api("GET", url)
        else:
            self.serve_static(url.path)

    def do_POST(self):
        url = urlparse(self.path)
        if url.path.startswith("/api/"):
            self.handle_api("POST", url)
        else:
            self.close_connection = True
            self.send_json({"error": "Not found."}, 404)

    def handle_api(self, method, url):
        try:
            body = self.read_json() if method == "POST" else {}
            result = self.route(method, url.path, parse_qs(url.query), body)
            obj, headers = result if isinstance(result, tuple) else (result, ())
            self.send_json(obj, 200, headers)
        except ApiError as err:
            self.send_json({"error": str(err)}, err.status)
        except Exception:
            traceback.print_exc()
            self.send_json({"error": "Server error."}, 500)

    def route(self, method, path, query, body):
        if method == "POST" and path == "/api/register":
            return self.register(body)
        if method == "POST" and path == "/api/login":
            return self.login(body)
        if method == "POST" and path == "/api/logout":
            return self.logout()
        if method == "GET" and path == "/api/me":
            return self.me()

        user = self.require_user()
        if method == "POST" and path == "/api/games":
            return create_game(user)
        if method == "POST" and path == "/api/games/quick":
            return quick_match(user)
        if method == "POST" and path == "/api/games/join":
            return join_by_code(user, body.get("code"))
        match = re.fullmatch(r"/api/games/([A-Za-z0-9_-]+)(?:/(move|rematch|leave))?", path)
        if match:
            game_id, action = match.groups()
            if method == "GET" and not action:
                return poll_game(user, game_id, query)
            if method == "POST" and action:
                return game_action(user, game_id, action, body)
        raise ApiError("Not found.", 404)

    # ---- accounts
    def register(self, body):
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        if not USERNAME_RE.match(username):
            raise ApiError("Username must be 3-20 letters, numbers or underscores.")
        if not 6 <= len(password) <= 200:
            raise ApiError("Password must be at least 6 characters.")
        salt = secrets.token_hex(16)
        try:
            user_id = db_exec("INSERT INTO users (username, pw_hash, salt, created) VALUES (?, ?, ?, ?)",
                              (username, hash_password(password, salt), salt, time.time()))
        except sqlite3.IntegrityError:
            raise ApiError("That username is already taken.", 409)
        token = create_session(user_id)
        return {"user": {"id": user_id, "name": username}}, [session_cookie(token, self.is_https())]

    def login(self, body):
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        row = db_one("SELECT id, username, pw_hash, salt FROM users WHERE username = ?", (username,))
        if row:
            ok = hmac.compare_digest(hash_password(password, row["salt"]), row["pw_hash"])
        else:
            hash_password(password, "00" * 16)  # same work either way, so timing doesn't reveal usernames
            ok = False
        if not ok:
            raise ApiError("Wrong username or password.", 401)
        token = create_session(row["id"])
        return {"user": {"id": row["id"], "name": row["username"]}}, [session_cookie(token, self.is_https())]

    def logout(self):
        token = self.session_token()
        if token:
            db_exec("DELETE FROM sessions WHERE token = ?", (token,))
        return {"ok": True}, [session_cookie("", self.is_https(), max_age=0)]

    def me(self):
        user = user_from_token(self.session_token())
        if not user:
            return {"user": None}
        with lock:
            game = active_game_for(user["id"])
        return {"user": {**user, "stats": user_stats(user["id"])},
                "activeGame": game.id if game else None}

    # ---- static files
    def serve_static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        target = (PUBLIC / path.lstrip("/")).resolve()
        if not target.is_relative_to(PUBLIC) or not target.is_file():
            self.send_body(404, b"Not found", "text/plain; charset=utf-8")
            return
        content_type = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self.send_body(200, target.read_bytes(), content_type)


def lan_address():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packets are sent; this just picks the Wi-Fi interface
            return s.getsockname()[0]
    except OSError:
        return None


def main():
    init_db()
    threading.Thread(target=janitor, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Tic-Tac-Toe server running on http://localhost:{PORT}", flush=True)
    ip = lan_address()
    if ip and HOST == "0.0.0.0":
        print(f"On a phone on the same Wi-Fi, open http://{ip}:{PORT}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
