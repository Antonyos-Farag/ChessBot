#!/usr/bin/env python3
"""Local web server for ChessBot.

Serves the single-page UI and exposes the engine over
WebSockets (``/ws``) and a small REST API:

======  =========================================================
GET     ``/``                the app
GET     ``/api/levels``      difficulty levels as JSON
GET     ``/api/book``        opening book name for a position
POST    ``/api/move``        best move for a FEN (``{"fen": ..., "level": 4}``)
POST    ``/api/evaluate``    engine evaluation of a FEN
POST    ``/api/analyse``     grade one move of a game
GET     ``/ws``              streaming engine protocol used by the UI
======  =========================================================

Run it with::

    python3 -m web.server            # http://127.0.0.1:8000

and open the printed URL in a browser.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import chess  # noqa: E402  (import order matters: sys.path patch above)

from engine import LEVELS, Bot, Engine, analyse_move, book, level_by_name  # noqa: E402
from engine.search import SearchLimits, san_of  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
MAX_MOVETIME_MS = 8_000
DEFAULT_MOVETIME_MS = 600

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}


# --------------------------------------------------------------------------- #
#  Engine service
# --------------------------------------------------------------------------- #

class EngineJob:
    """One unit of engine work, executed on the engine thread."""

    def __init__(self, reply, payload: dict) -> None:
        self.reply = reply              # callable(dict) -> None
        self.payload = payload
        self.stop = threading.Event()


class EngineService:
    """Serialises engine work and streams search progress to the client."""

    def __init__(self) -> None:
        self.jobs: queue.Queue[EngineJob | None] = queue.Queue()
        self.bots: dict[int, Bot] = {}
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name="engine", daemon=True)
        self.thread.start()

    # -- public API ---------------------------------------------------- #
    def submit(self, reply, payload: dict) -> EngineJob:
        job = EngineJob(reply, payload)
        self.jobs.put(job)
        return job

    def bot_for(self, level) -> Bot:
        try:
            info = level if isinstance(level, dict) else level_by_name(level)
        except KeyError:
            info = level_by_name(4)
        number = int(info["level"])
        with self.lock:
            bot = self.bots.get(number)
            if bot is None:
                bot = Bot(level=info)
                self.bots[number] = bot
        return bot

    # -- worker -------------------------------------------------------- #
    def _run(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                return
            if job.stop.is_set():
                continue
            try:
                self._handle(job)
            except Exception as exc:  # pragma: no cover - defensive
                job.reply({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

    def _handle(self, job: EngineJob) -> None:
        kind = job.payload.get("type")
        handler = {
            "move": self._move,
            "hint": self._hint,
            "eval": self._eval,
            "grade": self._grade,
        }.get(kind)
        if handler is None:
            job.reply({"type": "error", "message": f"unknown request {kind!r}"})
            return
        handler(job)

    # -- helpers ------------------------------------------------------- #
    @staticmethod
    def _board(payload) -> chess.Board:
        fen = payload.get("fen")
        return chess.Board(fen) if fen else chess.Board()

    @staticmethod
    def _movetime(payload, fallback: int = DEFAULT_MOVETIME_MS) -> float:
        value = payload.get("movetime_ms", fallback)
        try:
            millis = int(value)
        except (TypeError, ValueError):
            millis = fallback
        return max(50, min(MAX_MOVETIME_MS, millis)) / 1000.0

    def _streamer(self, job: EngineJob, board: chess.Board):
        """Throttled progress callback for a running search."""
        last = [0.0]

        def info(result):
            now = time.monotonic()
            if now - last[0] < 0.12 and result.depth < 3:
                return
            last[0] = now
            payload = result.as_dict()
            payload.update({
                "type": "info",
                "score_white": result.score if board.turn == chess.WHITE else -result.score,
                "white_to_move": board.turn == chess.WHITE,
                "fullmove": board.fullmove_number,
            })
            job.reply(payload)

        return info

    @staticmethod
    def _common(result, board, extra: dict | None = None) -> dict:
        payload = result.as_dict()
        payload.update({
            "type": "result",
            "score_white": result.score if board.turn == chess.WHITE else -result.score,
            "white_to_move": board.turn == chess.WHITE,
            "fullmove": board.fullmove_number,
            "fen_before": board.fen(),
        })
        name = book.name_for(board)
        payload["opening"] = {"eco": name[0], "name": name[1]} if name else None
        if extra:
            payload.update(extra)
        return payload

    # -- request handlers ---------------------------------------------- #
    def _move(self, job: EngineJob) -> None:
        board = self._board(job.payload)
        bot = self.bot_for(job.payload.get("level", 4))
        movetime = self._movetime(job.payload, int(bot.level["think_ms"]))
        result = bot.pick_move(
            board,
            movetime_ms=int(movetime * 1000),
            stop_event=job.stop,
        )
        if result.best_move is None:
            job.reply({"type": "result", "type_error": True, "best": None})
            return
        job.reply(self._common(result, board, {"book": result.from_book}))

    def _hint(self, job: EngineJob) -> None:
        board = self._board(job.payload)
        info = self.bot_for(job.payload.get("level", 4)).level
        strong = dict(info)
        strong.update({"max_depth": 10, "tolerance": 0, "top_k": 1, "book": False})
        bot = Bot(level=strong)
        movetime = self._movetime(job.payload, 900)
        result = bot.pick_move(board, movetime_ms=int(movetime * 1000), stop_event=job.stop)
        job.reply(self._common(result, board))

    def _eval(self, job: EngineJob) -> None:
        board = self._board(job.payload)
        bot = self.bot_for(job.payload.get("level", 4))
        movetime = self._movetime(job.payload, 450)
        result = bot.engine.search(
            board,
            SearchLimits(depth=12, movetime=movetime),
            info=self._streamer(job, board),
            stop_event=job.stop,
        )
        job.reply(self._common(result, board))

    def _grade(self, job: EngineJob) -> None:
        board = self._board(job.payload)
        uci = job.payload.get("move")
        if not uci:
            job.reply({"type": "error", "message": "grade needs a move"})
            return
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            job.reply({"type": "error", "message": f"bad move {uci!r}"})
            return
        if not board.is_legal(move):
            job.reply({"type": "error", "message": f"{uci} is not legal here"})
            return
        report = analyse_move(board, move, movetime_ms=int(self._movetime(job.payload, 400) * 1000))
        payload = {
            "type": "result",
            "report": {key: value for key, value in report.as_dict().items() if key != "move"},
            "score_white": report.score_before_cp if board.turn == chess.WHITE else -report.score_before_cp,
            "white_to_move": board.turn == chess.WHITE,
            "fullmove": board.fullmove_number,
        }
        job.reply(payload)


SERVICE = EngineService()


def analyse_position(fen: str, movetime_ms: int = 700, depth: int = 10, on_info=None) -> dict:
    """Blocking helper used by the REST endpoints."""
    board = chess.Board(fen) if fen else chess.Board()
    engine = Engine()
    result = engine.search(
        board,
        SearchLimits(depth=depth, movetime=movetime_ms / 1000.0),
        info=on_info,
    )
    payload = result.as_dict()
    payload.update({
        "score_white": result.score if board.turn == chess.WHITE else -result.score,
        "white_to_move": board.turn == chess.WHITE,
        "fullmove": board.fullmove_number,
        "fen": board.fen(),
    })
    name = book.name_for(board)
    payload["opening"] = {"eco": name[0], "name": name[1]} if name else None
    return payload


# --------------------------------------------------------------------------- #
#  HTTP + WebSocket plumbing
# --------------------------------------------------------------------------- #

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class Handler(BaseHTTPRequestHandler):
    server_version = "ChessBot/2.0"
    protocol_version = "HTTP/1.1"

    # ---- utilities ---------------------------------------------------- #
    def log_message(self, fmt, *args):  # quieter logs
        if "/ws" in self.path:
            return
        sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        # /static/... and bare /... both resolve inside the static directory.
        relative = relative[7:] if relative.startswith("static/") else relative
        candidate = (STATIC / relative).resolve()
        if not str(candidate).startswith(str(STATIC.resolve())) or not candidate.is_file():
            self.send_json({"error": "not found", "path": path}, 404)
            return
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(candidate.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    # ---- routes ------------------------------------------------------- #
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/ws"):
            self.handle_websocket()
            return
        if self.path.startswith("/api/levels"):
            self.send_json({
                "levels": [
                    {k: v for k, v in level.items() if k != "think_ms"} for level in LEVELS
                ],
                "default": 4,
            })
            return
        if self.path.startswith("/api/book"):
            fen = self.path.split("fen=", 1)[1] if "fen=" in self.path else ""
            from urllib.parse import unquote

            board = chess.Board(unquote(fen)) if fen else chess.Board()
            name = book.name_for(board)
            self.send_json({
                "in_book": book.is_in_book(board),
                "opening": {"eco": name[0], "name": name[1]} if name else None,
            })
            return
        self.serve_static(self.path.split("?")[0])

    def do_POST(self):  # noqa: N802
        payload = self.read_json()
        if self.path.startswith("/api/move"):
            bot = SERVICE.bot_for(payload.get("level", 4))
            board = chess.Board(payload["fen"]) if payload.get("fen") else chess.Board()
            movetime = int(payload.get("movetime_ms", bot.level["think_ms"]))
            result = bot.pick_move(board, movetime_ms=movetime)
            if result.best_move is None:
                self.send_json({"error": "no legal moves"}, 400)
                return
            name = book.name_for(board)
            self.send_json({
                "best": result.best_move.uci(),
                "best_san": san_of(board, result.best_move),
                "san": san_of(board, result.best_move),
                "pv": [move.uci() for move in result.pv],
                "pv_san": result.pv_san,
                "score": result.score,
                "score_white": result.score if board.turn == chess.WHITE else -result.score,
                "mate_in": result.mate_in,
                "white_to_move": board.turn == chess.WHITE,
                "fullmove": board.fullmove_number,
                "depth": result.depth,
                "seldepth": result.seldepth,
                "nodes": result.nodes,
                "nps": result.nps,
                "time_ms": result.time_ms,
                "book": result.from_book,
                "opening": {"eco": name[0], "name": name[1]} if name else None,
            })
            return
        if self.path.startswith("/api/evaluate"):
            fen = payload.get("fen", "")
            self.send_json(analyse_position(fen, movetime_ms=int(payload.get("movetime_ms", 600))))
            return
        if self.path.startswith("/api/analyse"):
            board = chess.Board(payload["fen"]) if payload.get("fen") else chess.Board()
            move = payload.get("move")
            if not move:
                self.send_json({"error": "move is required"}, 400)
                return
            try:
                report = analyse_move(
                    board,
                    chess.Move.from_uci(move),
                    movetime_ms=int(payload.get("movetime_ms", 500)),
                )
            except (ValueError, AssertionError) as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            out = report.as_dict()
            out.pop("move", None)
            out["fen"] = board.fen()
            out["score_white"] = (
                report.score_before_cp if board.turn == chess.WHITE else -report.score_before_cp
            )
            out["white_to_move"] = board.turn == chess.WHITE
            out["fullmove"] = board.fullmove_number
            self.send_json(out)
            return
        self.send_json({"error": f"no route for {self.path}"}, 404)

    # ---- websocket ---------------------------------------------------- #
    def handle_websocket(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_json({"error": "expected a websocket handshake"}, 400)
            return

        import base64
        import hashlib

        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("utf-8")).digest()
        ).decode("ascii")
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        conn = WebSocketConnection(self)
        try:
            conn.loop()
        except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
            pass
        finally:
            conn.close()


class WebSocketConnection:
    """Tiny RFC6455 server implementation, enough for this app's protocol."""

    def __init__(self, handler: Handler) -> None:
        self.wfile = handler.wfile
        self.rfile = handler.rfile
        self.write_lock = threading.Lock()
        self.alive = True
        self.jobs: dict[int, EngineJob] = {}

    # -- framing --------------------------------------------------------- #
    def _recv_exact(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self.rfile.read(count - len(data))
            if not chunk:
                raise ConnectionResetError("socket closed")
            data += chunk
        return data

    def read_frame(self) -> tuple[int, bytes]:
        header = self._recv_exact(2)
        opcode = header[0] & 0x0F
        masked = header[1] & 0x80
        length = header[1] & 0x7F
        if length == 126:
            length = int.from_bytes(self._recv_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(self._recv_exact(8), "big")
        mask = self._recv_exact(4) if masked else b"\x00\x00\x00\x00"
        payload = bytearray(self._recv_exact(length))
        if masked:
            for index in range(length):
                payload[index] ^= mask[index % 4]
        return opcode, bytes(payload)

    def send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        if not self.alive:
            return
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < (1 << 16):
            header.append(126)
            header += length.to_bytes(2, "big")
        else:
            header.append(127)
            header += length.to_bytes(8, "big")
        with self.write_lock:
            try:
                self.wfile.write(bytes(header) + payload)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.alive = False

    def send_json(self, payload: dict) -> None:
        try:
            self.send_frame(json.dumps(payload, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            pass

    # -- protocol -------------------------------------------------------- #
    def loop(self) -> None:
        while self.alive:
            opcode, payload = self.read_frame()
            if opcode == 0x8:            # close
                return
            if opcode == 0x9:            # ping
                self.send_frame(payload, 0xA)
                continue
            if opcode == 0xA:            # pong
                continue
            if opcode != 0x1:
                continue
            try:
                message = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            self.dispatch(message)

    def dispatch(self, message: dict) -> None:
        request_id = message.get("id")
        kind = message.get("type")
        if kind == "cancel":
            target = message.get("target")
            job = self.jobs.get(target)
            if job is not None:
                job.stop.set()
            return
        if kind == "ping":
            self.send_json({"id": request_id, "type": "pong"})
            return

        def reply(payload: dict) -> None:
            payload = {**payload, "id": request_id}
            self.send_json(payload)

        job = SERVICE.submit(reply, message)
        if request_id is not None:
            self.jobs[request_id] = job
            if len(self.jobs) > 40:
                for key in sorted(self.jobs)[:-20]:
                    self.jobs.pop(key, None)

    def close(self) -> None:
        self.alive = False
        for job in self.jobs.values():
            job.stop.set()


# --------------------------------------------------------------------------- #
#  Entry point
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the ChessBot web app.")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args(argv)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    info = book.stats()
    print(f"""
  ♞  ChessBot is running

     →  http://{display_host}:{args.port}

     {len(LEVELS)} difficulty levels · {info['lines']} book lines
     ({info['positions']} positions indexed, first moves: {', '.join(info['first_moves'][:6])}…)
     press Ctrl+C to stop
""")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  bye 👋")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
