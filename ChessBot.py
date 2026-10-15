#!/usr/bin/env python3
"""ChessBot — desktop app (tkinter).

The original single-file script grew into this: a proper board, a live engine
readout, six difficulty levels, an opening book and a coach that grades your
moves.  All the chess brains live in the `engine` package, so this file is
purely presentation and threading.

    python3 ChessBot.py

Controls
--------
click a piece then a square, or drag it          (both work)
F  flip board     N  new game     U  undo
H  hint           Esc clear selection     L  cycle level
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import chess

from engine import LEVELS, Bot, Engine, analyse_move, book, level_by_name
from engine.search import SearchLimits, san_of

# --------------------------------------------------------------------------- #
#  Theme — matched to the web app so both look like the same product
# --------------------------------------------------------------------------- #

BG = "#0b0e15"
PANEL = "#141926"
PANEL_2 = "#1b2233"
LINE = "#2a3450"
TEXT = "#e8ecf6"
TEXT_DIM = "#98a3bd"
TEXT_FAINT = "#66708c"
ACCENT = "#7cc4ff"
ACCENT_2 = "#c9a6ff"
GOLD = "#f5c264"
GOOD = "#57d98b"
WARN = "#f6c352"
BAD = "#ff6f6f"

LIGHT_SQ = "#eadfc6"
DARK_SQ = "#6f8ba3"
SEL = "#7cc4ff"
LAST_MOVE = "#f5c264"
CHECK = "#ff5c5c"
LEGAL = "#2f9e6a"

MOVE_GRADE_COLOURS = {
    "Brilliant": GOOD,
    "Best move": GOOD,
    "Good": "#9fd0ff",
    "Inaccuracy": WARN,
    "Mistake": "#ff9d5c",
    "Blunder": BAD,
}

GRADE_SYMBOLS = {
    "Brilliant": "!!",
    "Best move": "!",
    "Good": "",
    "Inaccuracy": "?!",
    "Mistake": "?",
    "Blunder": "??",
}

FILES = "abcdefgh"
START_FEN = chess.STARTING_FEN


def piece_glyph(piece: chess.Piece) -> str:
    """A solid Unicode glyph for a piece, so the outline trick works."""
    solid = {
        chess.PAWN: "♟",
        chess.KNIGHT: "♞",
        chess.BISHOP: "♝",
        chess.ROOK: "♜",
        chess.QUEEN: "♛",
        chess.KING: "♚",
    }
    return solid[piece.piece_type]


# --------------------------------------------------------------------------- #
#  The app
# --------------------------------------------------------------------------- #

class ChessApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("ChessBot · alpha-beta engine")
        root.configure(bg=BG)
        root.minsize(940, 660)

        # ---- game state ------------------------------------------------ #
        self.board = chess.Board()
        self.human_color = chess.WHITE
        self.flipped = False
        self.level = level_by_name(4)
        self.selected: int | None = None
        self.legal_targets: set[int] = set()
        self.last_move: tuple[int, int] | None = None
        self.hint_move: chess.Move | None = None
        self.review_index: int | None = None
        self.grades: dict[int, dict] = {}
        self.opening_name: str | None = None

        # ---- engine ---------------------------------------------------- #
        self.bots: dict[int, Bot] = {}
        self.evaluator = Engine()
        self.grader = Engine()
        self.search_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.events: queue.Queue = queue.Queue()
        self.thinking = False
        self.engine_stats = {"depth": 0, "seldepth": 0, "nodes": 0, "nps": 0, "time": 0}
        self.pv_san: list[str] = []
        self.eval_cp = 0
        self.mate_in: int | None = None
        self.pending_move: chess.Move | None = None  # bot's answer, if any
        self._polling = False

        self._build_ui()
        self._bind_keys()
        self.new_game()
        self.poll_events()

    # ------------------------------------------------------------------ #
    #  layout
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=18, pady=16)

        # ---------------- left: board ---------------- #
        left = tk.Frame(outer, bg=BG)
        left.pack(side="left", fill="both", expand=True)

        self.canvas = tk.Canvas(left, bg=PANEL, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self.draw())
        self.canvas.bind("<Button-1>", self.on_click)

        self.status_top = tk.Label(left, text="", bg=BG, fg=TEXT_DIM,
                                   font=("Segoe UI", 11), anchor="w")
        self.status_top.pack(fill="x", pady=(8, 0))

        # ---------------- right: panels ---------------- #
        right = tk.Frame(outer, bg=BG, width=372)
        right.pack(side="left", fill="y", padx=(18, 0))
        right.pack_propagate(False)

        # status + eval
        card = self._card(right, "Position")
        self.headline = tk.Label(card, text="White to move", bg=PANEL_2, fg=TEXT,
                                 font=("Segoe UI Semibold", 15), anchor="w")
        self.headline.pack(fill="x")
        self.subline = tk.Label(card, text="", bg=PANEL_2, fg=TEXT_DIM,
                                font=("Segoe UI", 10), anchor="w")
        self.subline.pack(fill="x", pady=(2, 8))

        self.eval_canvas = tk.Canvas(card, height=14, bg=PANEL_2, highlightthickness=0)
        self.eval_canvas.pack(fill="x")
        self.eval_label = tk.Label(card, text="+0.00", bg=PANEL_2, fg=ACCENT,
                                   font=("Consolas", 11, "bold"), anchor="w")
        self.eval_label.pack(fill="x", pady=(4, 0))

        # engine
        card = self._card(right, "Engine")
        self.stats_label = tk.Label(card, text="", bg=PANEL_2, fg=TEXT,
                                    font=("Consolas", 11), anchor="w", justify="left")
        self.stats_label.pack(fill="x")
        self.pv_label = tk.Label(card, text="waiting for a move…", bg=PANEL_2, fg=TEXT_DIM,
                                 font=("Segoe UI", 10), anchor="w", justify="left",
                                 wraplength=320)
        self.pv_label.pack(fill="x", pady=(6, 0))
        self.opening_label = tk.Label(card, text="", bg=PANEL_2, fg=GOLD,
                                      font=("Segoe UI", 10, "italic"), anchor="w")
        self.opening_label.pack(fill="x", pady=(4, 0))

        # controls
        card = self._card(right, "Controls")
        row = tk.Frame(card, bg=PANEL_2)
        row.pack(fill="x")
        for text, command in (
            ("New game", self.new_game),
            ("Undo", self.undo),
            ("Hint", self.request_hint),
            ("Flip", self.flip),
            ("Swap sides", self.swap_sides),
        ):
            self._button(row, text, command).pack(side="left", padx=2, pady=1)

        row2 = tk.Frame(card, bg=PANEL_2)
        row2.pack(fill="x", pady=(6, 0))
        self.level_var = tk.IntVar(value=self.level["level"])
        for level in LEVELS:
            self._level_button(row2, level)

        row3 = tk.Frame(card, bg=PANEL_2)
        row3.pack(fill="x", pady=(8, 0))
        for text, command in (
            ("Copy PGN", self.copy_pgn),
            ("Copy FEN", self.copy_fen),
            ("Save PGN", self.save_pgn),
            ("Load FEN", self.load_fen_prompt),
        ):
            self._button(row3, text, command).pack(side="left", padx=2, pady=1)

        # moves
        card = self._card(right, "Moves", expand=True)
        self.moves = tk.Text(card, bg=PANEL_2, fg=TEXT, bd=0, highlightthickness=0,
                             font=("Consolas", 11), height=10, wrap="none", cursor="arrow")
        self.moves.pack(fill="both", expand=True)
        self.moves.configure(state="disabled")
        for grade, colour in MOVE_GRADE_COLOURS.items():
            self.moves.tag_configure(grade, foreground=colour)
        self.moves.tag_configure("dim", foreground=TEXT_FAINT)
        self.moves.tag_configure("you", foreground=TEXT)
        self.moves.tag_configure("bot", foreground="#bcd0ff")

        self.coach_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(card, text="Coach (grade my moves)", variable=self.coach_var,
                        style="TCheckbutton").pack(anchor="w", pady=(6, 0))

    def _card(self, parent, title: str, expand: bool = False) -> tk.Frame:
        outer = tk.Frame(parent, bg=LINE)
        outer.pack(fill="both" if expand else "x", expand=expand, pady=(0, 10))
        inner = tk.Frame(outer, bg=PANEL_2)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(inner, text=title.upper(), bg=PANEL_2, fg=TEXT_FAINT,
                 font=("Segoe UI", 8, "bold"), anchor="w").pack(fill="x", padx=12, pady=(9, 4))
        body = tk.Frame(inner, bg=PANEL_2)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        return body

    def _button(self, parent, text: str, command, small: bool = False) -> tk.Button:
        button = tk.Button(
            parent, text=text, command=command, bd=0, relief="flat",
            bg=PANEL, fg=TEXT, activebackground=ACCENT, activeforeground="#06121f",
            font=("Segoe UI", 9 if small else 10), padx=10, pady=5, cursor="hand2",
            highlightthickness=0,
        )
        button.bind("<Enter>", lambda _e: button.configure(bg=LINE))
        button.bind("<Leave>", lambda _e: button.configure(bg=PANEL))
        return button

    def _level_button(self, parent, level: dict) -> None:
        button = tk.Button(
            parent, text=f"{level['name']}\n{level['estimated_elo']}",
            command=lambda lvl=level: self.set_level(lvl),
            bd=0, relief="flat", bg=PANEL, fg=TEXT_DIM, activebackground=ACCENT,
            font=("Segoe UI", 8), padx=6, pady=3, cursor="hand2", highlightthickness=0,
            justify="center",
        )
        button.pack(side="left", fill="x", expand=True, padx=2)
        button.level_number = level["level"]  # type: ignore[attr-defined]
        setattr(self, f"_level_button_{level['level']}", button)
        self._paint_level_buttons()

    def _paint_level_buttons(self) -> None:
        for level in LEVELS:
            button = getattr(self, f"_level_button_{level['level']}", None)
            if button is None:
                continue
            active = level["level"] == self.level["level"]
            button.configure(
                bg=ACCENT if active else PANEL,
                fg="#06121f" if active else TEXT_DIM,
            )

    def _bind_keys(self) -> None:
        self.root.bind("<Control-n>", lambda _e: self.new_game())
        self.root.bind("<Control-z>", lambda _e: self.undo())
        self.root.bind("<Escape>", lambda _e: self.clear_selection())
        self.root.bind("<Key-n>", lambda _e: self.new_game())
        self.root.bind("<Key-u>", lambda _e: self.undo())
        self.root.bind("<Key-f>", lambda _e: self.flip())
        self.root.bind("<Key-h>", lambda _e: self.request_hint())
        self.root.bind("<Key-l>", lambda _e: self.cycle_level())

    # ------------------------------------------------------------------ #
    #  geometry helpers
    # ------------------------------------------------------------------ #
    def square_size(self) -> float:
        width = max(self.canvas.winfo_width(), 100)
        height = max(self.canvas.winfo_height(), 100)
        return min(width, height) / 8

    def board_origin(self) -> tuple[float, float]:
        size = self.square_size() * 8
        return (
            (self.canvas.winfo_width() - size) / 2,
            (self.canvas.winfo_height() - size) / 2,
        )

    def square_to_xy(self, square: int) -> tuple[float, float]:
        """Top-left pixel of a square. Flipping rotates the board 180°."""
        size = self.square_size()
        left, top = self.board_origin()
        file_index = chess.square_file(square)
        rank_index = chess.square_rank(square)
        if self.flipped:
            col, row = 7 - file_index, rank_index
        else:
            col, row = file_index, 7 - rank_index
        return left + col * size, top + row * size

    def xy_to_square(self, x: float, y: float) -> int | None:
        size = self.square_size()
        left, top = self.board_origin()
        col = int((x - left) // size)
        row = int((y - top) // size)
        if not (0 <= col < 8 and 0 <= row < 8):
            return None
        # row 0 is the top of the canvas: rank 8 normally, rank 1 when flipped.
        if self.flipped:
            return chess.square(7 - col, row)
        return chess.square(col, 7 - row)

    # ------------------------------------------------------------------ #
    #  drawing
    # ------------------------------------------------------------------ #
    def draw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        size = self.square_size()
        left, top = self.board_origin()
        board = self.view_board()

        for square in chess.SQUARES:
            x, y = self.square_to_xy(square)
            light = (chess.square_file(square) + chess.square_rank(square)) % 2 == 1
            canvas.create_rectangle(
                x, y, x + size, y + size,
                fill=LIGHT_SQ if light else DARK_SQ, outline="", tags="square",
            )

        # coordinates: file letters along the bottom, rank numbers down the left
        label_font = ("Segoe UI", max(7, int(size * 0.15)), "bold")
        for index in range(8):
            file_char = FILES[7 - index] if self.flipped else FILES[index]
            canvas.create_text(
                left + (index + 0.82) * size, top + 7.84 * size, text=file_char,
                fill="#5b6b7d", font=label_font,
            )
            rank_number = str(index + 1) if self.flipped else str(8 - index)
            canvas.create_text(
                left + 0.16 * size, top + (index + 0.16) * size, text=rank_number,
                fill="#5b6b7d", font=label_font,
            )

        # highlights
        if self.last_move:
            for square in self.last_move:
                self._highlight(square, LAST_MOVE, stipple="gray50")
        if self.selected is not None:
            self._highlight(self.selected, SEL, stipple="")
        if board.is_check() and board.king(board.turn) is not None:
            king = board.king(board.turn)
            x, y = self.square_to_xy(king)
            canvas.create_oval(
                x + size * 0.06, y + size * 0.06, x + size * 0.94, y + size * 0.94,
                fill=CHECK, outline="", stipple="gray50", tags="hl",
            )

        # legal move dots
        for target in self.legal_targets:
            x, y = self.square_to_xy(target)
            radius = size * 0.16
            canvas.create_oval(
                x + size / 2 - radius, y + size / 2 - radius,
                x + size / 2 + radius, y + size / 2 + radius,
                fill=LEGAL, outline="", tags="hl",
            )

        # pieces
        for square in chess.SQUARES:
            piece = board.piece_at(square)
            if piece is None:
                continue
            x, y = self.square_to_xy(square)
            self._draw_piece(x + size / 2, y + size / 2, piece, size)

        # hint arrow
        if self.hint_move is not None:
            self._draw_arrow(self.hint_move.from_square, self.hint_move.to_square, GOOD)

        # board border
        canvas.create_rectangle(
            left, top, left + size * 8, top + size * 8,
            outline=LINE, width=2, fill="", tags="frame",
        )

    def _highlight(self, square: int | None, colour: str, stipple: str = "gray50") -> None:
        if square is None:
            return
        size = self.square_size()
        x, y = self.square_to_xy(square)
        self.canvas.create_rectangle(
            x, y, x + size, y + size, fill=colour, outline="",
            stipple=stipple, tags="hl",
        )

    def _draw_piece(self, cx: float, cy: float, piece: chess.Piece, size: float) -> None:
        """Draw a glyph with a fake outline: four offset copies + the fill."""
        glyph = piece_glyph(piece)
        font = ("Segoe UI Symbol", max(10, int(size * 0.72)))
        if piece.color == chess.WHITE:
            fill, outline = "#fdfbf6", "#2a3040"
        else:
            fill, outline = "#22262f", "#0a0c12"
        offset = max(1.0, size * 0.03)
        for dx, dy in ((-offset, 0), (offset, 0), (0, -offset), (0, offset)):
            self.canvas.create_text(
                cx + dx, cy + dy, text=glyph, font=font, fill=outline, tags="piece",
            )
        self.canvas.create_text(cx, cy, text=glyph, font=font, fill=fill, tags="piece")

    def _draw_arrow(self, source: int, target: int, colour: str) -> None:
        size = self.square_size()
        x1, y1 = self.square_to_xy(source)
        x2, y2 = self.square_to_xy(target)
        x1, y1 = x1 + size / 2, y1 + size / 2
        x2, y2 = x2 + size / 2, y2 + size / 2
        self.canvas.create_line(
            x1, y1, x2, y2, fill=colour, width=max(3, size * 0.12),
            capstyle="round", arrow="last", arrowshape=(size * 0.4, size * 0.5, size * 0.2),
            tags="hint",
        )

    # ------------------------------------------------------------------ #
    #  game flow
    # ------------------------------------------------------------------ #
    def view_board(self) -> chess.Board:
        """The board as it should be displayed (respecting review mode)."""
        if self.review_index is None:
            return self.board
        replay = chess.Board()
        for move in self.board.move_stack[: self.review_index]:
            replay.push(move)
        return replay

    def new_game(self, human_color: chess.Color | None = None) -> None:
        self.stop_search()
        self.board = chess.Board()
        if human_color is not None:
            self.human_color = human_color
        self.flipped = self.human_color == chess.BLACK
        self.selected = None
        self.legal_targets = set()
        self.last_move = None
        self.hint_move = None
        self.review_index = None
        self.grades.clear()
        self.pv_san = []
        self.eval_cp = 0
        self.mate_in = None
        self.opening_name = None
        self.engine_stats = {"depth": 0, "seldepth": 0, "nodes": 0, "nps": 0, "time": 0}
        self.pending_move = None
        self.draw()
        self.refresh_panels()
        self.set_status(self.human_side_name() + " — you start", "make a move to begin")
        if self.board.turn != self.human_color:
            self.start_bot_turn()
        else:
            self.start_evaluation()

    def human_side_name(self) -> str:
        return "White" if self.human_color == chess.WHITE else "Black"

    def swap_sides(self) -> None:
        self.new_game(not self.human_color)

    def flip(self) -> None:
        self.flipped = not self.flipped
        self.draw()

    def cycle_level(self) -> None:
        numbers = [level["level"] for level in LEVELS]
        index = (numbers.index(self.level["level"]) + 1) % len(numbers)
        self.set_level(level_by_name(numbers[index]))

    def set_level(self, level: dict) -> None:
        self.level = level
        self.level_var.set(level["level"])
        self._paint_level_buttons()
        self.set_status(self.level["name"], f"~{level['estimated_elo']} Elo · {level['blurb']}")
        if not self.thinking and self.board.turn != self.human_color and not self.board.is_game_over():
            self.start_bot_turn()

    def undo(self) -> None:
        if self.thinking:
            self.stop_search()
        self.review_index = None
        self.hint_move = None
        plies = 0
        while self.board.move_stack and plies < 2:
            self.board.pop()
            plies += 1
            if self.board.turn == self.human_color:
                break
        self.grades.clear()
        self.last_move = None
        if self.board.move_stack:
            last = self.board.peek()
            self.last_move = (last.from_square, last.to_square)
        self.selected = None
        self.legal_targets = set()
        self.draw()
        self.refresh_panels()
        if self.board.turn != self.human_color and not self.board.is_game_over():
            self.start_bot_turn()
        else:
            self.start_evaluation()

    def clear_selection(self) -> None:
        self.selected = None
        self.legal_targets = set()
        self.hint_move = None
        self.draw()

    # ------------------------------------------------------------------ #
    #  input
    # ------------------------------------------------------------------ #
    def on_click(self, event) -> None:
        if self.review_index is not None:
            self.review_index = None
            self.draw()
            return
        if self.board.is_game_over() or self.thinking:
            return
        if self.board.turn != self.human_color:
            return

        square = self.xy_to_square(event.x, event.y)
        if square is None:
            return
        piece = self.board.piece_at(square)

        if self.selected is not None and square in self.legal_targets:
            self.play_human_move(self.selected, square)
            return

        if piece is None or piece.color != self.human_color:
            self.clear_selection()
            return
        if self.selected == square:
            self.clear_selection()
            return

        self.selected = square
        self.legal_targets = {
            move.to_square for move in self.board.legal_moves if move.from_square == square
        }
        self.hint_move = None
        self.draw()

    def play_human_move(self, source: int, target: int) -> None:
        promotion = None
        piece = self.board.piece_at(source)
        if piece is not None and piece.piece_type == chess.PAWN:
            if chess.square_rank(target) in (0, 7):
                promotion = self.ask_promotion()
                if promotion is None:
                    self.clear_selection()
                    return

        move = chess.Move(source, target, promotion=promotion)
        if move not in self.board.legal_moves:
            self.clear_selection()
            return

        before = self.board.fen()
        self.board.push(move)
        self.after_human_move(move, before)

    def after_human_move(self, move: chess.Move, fen_before: str) -> None:
        self.selected = None
        self.legal_targets = set()
        self.hint_move = None
        self.last_move = (move.from_square, move.to_square)
        opening = book.name_for(self.board)
        self.opening_name = " · ".join(opening) if opening else self.opening_name
        self.draw()
        self.refresh_panels()

        if self.board.is_game_over():
            self.show_result()
            return

        if self.coach_var.get():
            plies = len(self.board.move_stack) - 1
            self.start_grade(plies, fen_before, move.uci())

        if self.board.turn == self.human_color:
            self.start_evaluation()
        else:
            self.start_bot_turn()

    def ask_promotion(self) -> int | None:
        """Small modal dialog: queen / rook / bishop / knight."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Promote")
        dialog.configure(bg=PANEL)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        chosen: list[int | None] = [None]
        options = (
            ("Queen", chess.QUEEN), ("Rook", chess.ROOK),
            ("Bishop", chess.BISHOP), ("Knight", chess.KNIGHT),
        )
        tk.Label(dialog, text="Promote to:", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 10)).pack(padx=14, pady=(12, 6))
        row = tk.Frame(dialog, bg=PANEL)
        row.pack(padx=14, pady=(0, 12))
        for label, piece_type in options:
            button = self._button(row, label, lambda pt=piece_type: (chosen.__setitem__(0, pt), dialog.destroy()))
            button.pack(side="left", padx=3)
        dialog.grab_set()
        self.root.wait_window(dialog)
        return chosen[0]

    # ------------------------------------------------------------------ #
    #  engine plumbing (threads -> queue -> tk main loop)
    # ------------------------------------------------------------------ #
    def bot_for_level(self) -> Bot:
        number = self.level["level"]
        if number not in self.bots:
            self.bots[number] = Bot(level=self.level)
        return self.bots[number]

    def start_bot_turn(self) -> None:
        if self.board.is_game_over() or self.thinking:
            return
        self.thinking = True
        self.set_status(f"{self.level['name']} is thinking…", "")
        self.stop_event = threading.Event()
        board = self.board.copy(stack=False)
        bot = self.bot_for_level()

        def worker() -> None:
            try:
                result = bot.pick_move(
                    board,
                    movetime_ms=int(self.level["think_ms"]),
                    stop_event=self.stop_event,
                )
                self.events.put(("bot", result))
            except Exception as exc:  # pragma: no cover - defensive
                self.events.put(("error", str(exc)))

        self.search_thread = threading.Thread(target=worker, name="bot-move", daemon=True)
        self.search_thread.start()

    def start_evaluation(self, movetime_ms: int = 700) -> None:
        if self.board.is_game_over() or self.thinking:
            return
        self.stop_event = threading.Event()
        board = self.board.copy(stack=False)
        evaluator = self.evaluator

        def worker() -> None:
            try:
                result = evaluator.search(
                    board,
                    SearchLimits(depth=10, movetime=movetime_ms / 1000.0),
                    info=lambda partial: self.events.put(("info", partial)),
                    stop_event=self.stop_event,
                )
                self.events.put(("info", result))
            except Exception as exc:  # pragma: no cover - defensive
                self.events.put(("error", str(exc)))

        self.search_thread = threading.Thread(target=worker, name="eval", daemon=True)
        self.search_thread.start()

    def start_hint(self) -> None:
        if self.thinking or self.board.is_game_over():
            return
        self.thinking = True
        self.stop_event = threading.Event()
        board = self.board.copy(stack=False)
        strong = Bot(level={**self.level, "max_depth": 10, "tolerance": 0, "top_k": 1, "book": False})

        def worker() -> None:
            try:
                result = strong.pick_move(board, movetime_ms=1200, stop_event=self.stop_event)
                if result.best_move is not None:
                    self.events.put(("hint", result))
            except Exception as exc:  # pragma: no cover - defensive
                self.events.put(("error", str(exc)))

        self.search_thread = threading.Thread(target=worker, name="hint", daemon=True)
        self.search_thread.start()

    def start_grade(self, ply: int, fen_before: str, uci: str) -> None:
        def worker() -> None:
            try:
                board = chess.Board(fen_before)
                move = chess.Move.from_uci(uci)
                report = analyse_move(board, move, engine=self.grader, movetime_ms=350)
                self.events.put(("grade", (ply, report)))
            except Exception:  # pragma: no cover - grading is best effort
                pass

        threading.Thread(target=worker, name="grade", daemon=True).start()

    def stop_search(self) -> None:
        if self.stop_event is not None:
            self.stop_event.set()
        self.thinking = False

    def poll_events(self) -> None:
        """Drain engine messages on the Tk thread (one recurring timer only)."""
        if self._polling:
            # already scheduled; dropping the extra call keeps exactly one timer
            return
        self._polling = True
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "info":
                    self.apply_info(payload)
                elif kind == "bot":
                    self.apply_bot_move(payload)
                elif kind == "hint":
                    self.apply_hint(payload)
                elif kind == "grade":
                    self.apply_grade(*payload)
                elif kind == "error":
                    self.set_status("Engine error", payload)
                    self.thinking = False
        except queue.Empty:
            pass
        self._polling = False
        self.root.after(60, self.poll_events)

    def apply_info(self, result) -> None:
        white_to_move = self.board.turn == chess.WHITE
        self.engine_stats = {
            "depth": result.depth,
            "seldepth": result.seldepth,
            "nodes": result.nodes,
            "nps": result.nps,
            "time": result.time_ms,
        }
        self.eval_cp = result.score if white_to_move else -result.score
        self.mate_in = result.mate_in
        if result.pv_san:
            self.pv_san = result.pv_san
        self.refresh_panels()

    def apply_bot_move(self, result) -> None:
        self.thinking = False
        if result is None or result.best_move is None:
            self.set_status("No moves left", "")
            return
        if result.best_move not in self.board.legal_moves:
            self.set_status("Bot produced an illegal move", "that should not happen!")
            return
        self.board.push(result.best_move)
        self.last_move = (result.best_move.from_square, result.best_move.to_square)
        self.grades[len(self.board.move_stack) - 1] = {"grade": "Good", "symbol": "", "note": ""}
        if result.from_book:
            name = book.name_for(self.board)
            self.opening_name = " · ".join(name) if name else "opening book"
        self.draw()
        self.refresh_panels()

        if self.board.is_game_over():
            self.show_result()
            return
        self.set_status(f"{self.level['name']} played {san_of(self.board, result.best_move)}", "your move")
        self.start_evaluation()

    def apply_hint(self, result) -> None:
        self.thinking = False
        if result.best_move is None:
            return
        self.hint_move = result.best_move
        self.draw()
        self.set_status(f"Hint: {result.pv_san[0] if result.pv_san else result.best_move.uci()}",
                        f"evaluation {result.score / 100:+.2f}")

    def apply_grade(self, ply: int, report) -> None:
        self.grades[ply] = {
            "grade": report.grade,
            "symbol": GRADE_SYMBOLS.get(report.grade, ""),
            "note": report.note or "",
            "best": report.best_san,
        }
        self.refresh_panels()
        if report.grade in ("Blunder", "Mistake"):
            extra = f" (best was {report.best_san})" if report.best_san else ""
            self.set_status(f"{report.grade}: {report.san}{extra}", report.note or "")

    def request_hint(self) -> None:
        self.start_hint()

    # ------------------------------------------------------------------ #
    #  panels
    # ------------------------------------------------------------------ #
    def set_status(self, headline: str, sub: str) -> None:
        self.headline.configure(text=headline)
        self.subline.configure(text=sub)

    def refresh_panels(self) -> None:
        board = self.view_board()
        if self.review_index is None:
            if board.is_checkmate():
                winner = "Black" if board.turn == chess.WHITE else "White"
                self.set_status(f"Checkmate — {winner} wins", "press N for a new game")
            elif board.is_stalemate():
                self.set_status("Stalemate", "nobody wins this one")
            elif board.is_insufficient_material():
                self.set_status("Draw", "not enough material to mate")
            elif board.is_game_over(claim_draw=True):
                self.set_status("Draw", "fifty-move rule or repetition")
            elif board.is_check():
                whose = "White" if board.turn == chess.WHITE else "Black"
                self.set_status(f"{whose} is in check", "find the best defence")
            elif board.turn == self.human_color and not self.thinking:
                self.set_status("Your move", "")
            elif self.thinking:
                self.set_status(f"{self.level['name']} is thinking…", "")
        else:
            self.set_status(f"Reviewing move {self.review_index}", "click the board to jump back")

        # eval
        colour = GOOD if self.eval_cp > 30 else BAD if self.eval_cp < -30 else ACCENT
        if self.mate_in is not None and abs(self.mate_in) <= 30:
            text = f"{'M' if self.mate_in > 0 else '-M'}{abs(self.mate_in)}"
        else:
            text = f"{self.eval_cp / 100:+.2f}"
        self.eval_label.configure(text=f"evaluation  {text}", fg=colour)
        self._draw_eval_bar()

        stats = self.engine_stats
        speed = f"{stats['nps'] / 1000:.1f}k" if stats["nps"] else "—"
        nodes = f"{stats['nodes'] / 1000:.1f}k" if stats["nodes"] else "—"
        self.stats_label.configure(
            text=(
                f"depth   {stats['depth']}/{stats['seldepth']}\n"
                f"nodes   {nodes}\n"
                f"speed   {speed}/s\n"
                f"time    {stats['time'] / 1000:.2f}s"
            )
        )
        if self.pv_san:
            start_turn = board.turn
            moves = " ".join(
                (f"{board.fullmove_number + index // 2}." if (start_turn == chess.WHITE) == (index % 2 == 0) else "")
                + san
                for index, san in enumerate(self.pv_san[:8])
            )
            self.pv_label.configure(text=moves.strip())
        self.opening_label.configure(text=f"book: {self.opening_name}" if self.opening_name else "")
        self.refresh_moves()

    def _draw_eval_bar(self) -> None:
        """Horizontal bar: White's share grows from the left."""
        canvas = self.eval_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 50)
        height = 14
        cp = max(-800, min(800, self.eval_cp))
        white_share = 0.5 + cp / 1600
        canvas.create_rectangle(0, 0, width, height, fill="#232a3d", outline="")
        canvas.create_rectangle(0, 0, width * white_share, height, fill="#e7edff", outline="")
        canvas.create_line(width / 2, 0, width / 2, height, fill=LINE)
        canvas.create_line(width * white_share - 1, 0, width * white_share - 1, height,
                           fill=ACCENT, width=2)

    def refresh_moves(self) -> None:
        self.moves.configure(state="normal")
        self.moves.delete("1.0", "end")
        history = self.board.move_stack
        if not history:
            self.moves.insert("end", "no moves yet\n", "dim")
        else:
            replay = chess.Board()
            index = 0
            for ply, move in enumerate(history):
                if ply % 2 == 0:
                    self.moves.insert("end", f"{ply // 2 + 1:3d}. ", "dim")
                san = replay.san(move)
                grade = self.grades.get(ply, {})
                tag = grade.get("grade", "you" if replay.turn == self.human_color else "bot")
                self.moves.insert("end", san, tag)
                symbol = grade.get("symbol", "")
                if symbol:
                    self.moves.insert("end", symbol, tag)
                self.moves.insert("end", "  ", "dim")
                if ply % 2 == 1:
                    self.moves.insert("end", "\n")
                replay.push(move)
                index += 1
        self.moves.configure(state="disabled")
        self.moves.see("end")

    def show_result(self) -> None:
        board = self.board
        if board.is_checkmate():
            winner = "Black" if board.turn == chess.WHITE else "White"
            human_won = (winner == "White") == (self.human_color == chess.WHITE)
            title = "Checkmate!"
            detail = f"{winner} wins." + (" Nicely played." if human_won else " Better luck next time.")
        elif board.is_stalemate():
            title, detail = "Stalemate", "Nobody wins this one."
        elif board.is_insufficient_material():
            title, detail = "Draw", "Not enough material to mate."
        else:
            title, detail = "Draw", "Drawn position."
        self.set_status(title, detail)
        self.refresh_panels()
        if messagebox.askyesno("Game over", f"{title}\n{detail}\n\nPlay again?"):
            self.new_game()

    # ------------------------------------------------------------------ #
    #  clipboard / files
    # ------------------------------------------------------------------ #
    def copy_pgn(self) -> None:
        # NOTE: variation_san() plays the moves on a copy of the position it is
        # called on, so replay the game from a fresh board.
        pgn = (
            chess.Board().variation_san(self.board.move_stack)
            if self.board.move_stack else "(no moves)"
        )
        self.root.clipboard_clear()
        self.root.clipboard_append(pgn)
        self.set_status("PGN copied", "paste it anywhere")

    def copy_fen(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.board.fen())
        self.set_status("FEN copied", self.board.fen())

    def save_pgn(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".pgn", filetypes=[("PGN", "*.pgn")], initialfile="chessbot-game.pgn",
        )
        if not path:
            return
        game = chess.pgn.Game.from_board(self.board)
        with open(path, "w", encoding="utf-8") as handle:
            print(game, file=handle)
        self.set_status("Game saved", path)

    def load_fen_prompt(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Load FEN")
        dialog.configure(bg=PANEL)
        dialog.transient(self.root)
        tk.Label(dialog, text="FEN:", bg=PANEL, fg=TEXT, font=("Segoe UI", 10)).pack(padx=12, pady=(12, 4), anchor="w")
        entry = tk.Entry(dialog, width=56, font=("Consolas", 10), bg=PANEL_2, fg=TEXT,
                         insertbackground=TEXT, bd=0)
        entry.pack(padx=12, pady=(0, 8), ipady=4)
        entry.insert(0, self.board.fen())

        def apply() -> None:
            value = entry.get().strip()
            try:
                board = chess.Board(value)
            except ValueError:
                messagebox.showerror("Invalid FEN", "That position could not be parsed.")
                return
            self.stop_search()
            self.board = board
            self.review_index = None
            self.selected = None
            self.legal_targets = set()
            self.grades.clear()
            self.hint_move = None
            self.last_move = None
            if self.board.move_stack:
                last = self.board.peek()
                self.last_move = (last.from_square, last.to_square)
            dialog.destroy()
            self.draw()
            self.refresh_panels()
            if self.board.turn != self.human_color and not self.board.is_game_over():
                self.start_bot_turn()
            else:
                self.start_evaluation()

        row = tk.Frame(dialog, bg=PANEL)
        row.pack(padx=12, pady=(0, 12))
        self._button(row, "Load", apply).pack(side="left", padx=3)
        self._button(row, "Cancel", dialog.destroy).pack(side="left", padx=3)
        entry.focus_set()
        entry.bind("<Return>", lambda _e: apply())


# --------------------------------------------------------------------------- #

def main() -> int:
    root = tk.Tk()
    try:  # nicer fonts on high-DPI screens
        from ctypes import windll  # type: ignore

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    ChessApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
