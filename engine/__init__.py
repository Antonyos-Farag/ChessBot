"""A small, friendly chess engine built on python-chess.

Quick start
-----------
>>> import chess
>>> from engine import Engine, SearchLimits
>>> engine = Engine()
>>> result = engine.search(chess.Board(), SearchLimits(movetime=1.0))
>>> result.best_move.uci()
'g1f3'
>>> result.score, result.depth > 0
(42, True)

The engine also ships a *bot* layer with six strength levels, an opening book
and move grading for the coach panel:

>>> from engine import Bot
>>> bot = Bot(level=3)
>>> move = bot.pick_move(chess.Board()).move
"""

from __future__ import annotations

import random

import chess

from . import book
from .evaluate import EvalCache, evaluate_side, evaluate_white
from .search import (
    MATE_SCORE,
    MATE_THRESHOLD,
    Engine,
    SearchLimits,
    SearchResult,
    san_line,
    san_of,
)

__all__ = [
    "Bot",
    "MATE_SCORE",
    "MATE_THRESHOLD",
    "Engine",
    "EvalCache",
    "LEVELS",
    "MoveReport",
    "SearchLimits",
    "SearchResult",
    "analyse_move",
    "book",
    "evaluate_side",
    "evaluate_white",
    "level_by_name",
]

#: Playable strengths.  ``estimated_elo`` is a rough ballpark (and deliberately
#: modest) — a pure-Python engine is not a Stockfish.
LEVELS = [
    {
        "level": 1,
        "name": "Pawn",
        "estimated_elo": 500,
        "think_ms": 50,
        "max_depth": 2,
        "tolerance": 150,
        "top_k": 4,
        "book": False,
        "blurb": "Learns the moves. Leaves pieces hanging — often.",
    },
    {
        "level": 2,
        "name": "Knight",
        "estimated_elo": 800,
        "think_ms": 120,
        "max_depth": 3,
        "tolerance": 90,
        "top_k": 3,
        "book": False,
        "blurb": "Sees one-move threats, forgets about the last one.",
    },
    {
        "level": 3,
        "name": "Bishop",
        "estimated_elo": 1050,
        "think_ms": 250,
        "max_depth": 4,
        "tolerance": 45,
        "top_k": 2,
        "book": True,
        "blurb": "Punishes loose pieces and plays real openings.",
    },
    {
        "level": 4,
        "name": "Rook",
        "estimated_elo": 1250,
        "think_ms": 500,
        "max_depth": 6,
        "tolerance": 20,
        "top_k": 2,
        "book": True,
        "blurb": "Solid tactics, competent endgames. A real club opponent.",
    },
    {
        "level": 5,
        "name": "Queen",
        "estimated_elo": 1450,
        "think_ms": 1000,
        "max_depth": 8,
        "tolerance": 8,
        "top_k": 1,
        "book": True,
        "blurb": "Calculates deep forcing lines and rarely gives anything away.",
    },
    {
        "level": 6,
        "name": "King",
        "estimated_elo": 1650,
        "think_ms": 1800,
        "max_depth": 12,
        "tolerance": 0,
        "top_k": 1,
        "book": True,
        "blurb": "Full strength. Thinks as long as you let it.",
    },
]

DEFAULT_LEVEL = 4


def level_by_name(name: str) -> dict:
    """Look up a level by name (case-insensitive) or number."""
    if isinstance(name, int) or (isinstance(name, str) and name.isdigit()):
        number = int(name)
        for level in LEVELS:
            if level["level"] == number:
                return level
        raise KeyError(f"no level {name!r} (1-{len(LEVELS)})")
    for level in LEVELS:
        if level["name"].lower() == str(name).lower():
            return level
    raise KeyError(f"unknown level {name!r}")


class MoveReport:
    """Everything the UI needs to talk about one move."""

    __slots__ = (
        "move", "san", "uci", "grade", "loss_cp", "score_before_cp",
        "score_after_cp", "best_move", "best_san", "pv", "mate_in", "note",
    )

    def __init__(self, **kwargs) -> None:
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))

    def as_dict(self) -> dict:
        """A JSON-friendly view of the report (moves become UCI strings)."""
        payload = {}
        for slot in self.__slots__:
            value = getattr(self, slot)
            if isinstance(value, chess.Move):
                value = value.uci()
            elif isinstance(value, list):
                value = [item.uci() if isinstance(item, chess.Move) else item for item in value]
            payload[slot] = value
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<MoveReport {self.uci} {self.grade} loss={self.loss_cp}>"


class Bot:
    """A playable opponent: opening book + search + level-appropriate choice."""

    def __init__(self, level: int | dict | str = DEFAULT_LEVEL, engine: Engine | None = None,
                 seed: int | None = None) -> None:
        self.level = level if isinstance(level, dict) else level_by_name(level)
        self.engine = engine or Engine()
        self.rng = random.Random(seed)

    # ------------------------------------------------------------------ #
    @property
    def name(self) -> str:
        return str(self.level["name"])

    def pick_move(self, board: chess.Board, movetime_ms: int | None = None,
                  stop_event=None, use_book: bool | None = None) -> SearchResult:
        """Choose a move for `board`. Never returns an illegal move."""
        legal = list(board.legal_moves)
        if not legal:
            return SearchResult(best_move=None)

        if use_book is None:
            use_book = bool(self.level["book"])
        if use_book and len(board.move_stack) < 24:
            move = book.lookup(board, self.rng)
            if move is not None and move in board.legal_moves:
                return SearchResult(
                    best_move=move,
                    score=evaluate_side(board),
                    depth=0,
                    pv=[move],
                    pv_san=[san_of(board, move)],
                    from_book=True,
                )

        think_ms = movetime_ms if movetime_ms is not None else int(self.level["think_ms"])
        limits = SearchLimits(depth=max(1, int(self.level["max_depth"])),
                              movetime=think_ms / 1000.0)
        result = self.engine.search(board, limits, stop_event=stop_event)
        if result.best_move is None:
            result.best_move = self.rng.choice(legal)
            result.pv = [result.best_move]
            result.pv_san = [san_of(board, result.best_move)]
            return result

        tolerance = int(self.level["tolerance"])
        top_k = int(self.level["top_k"])
        if tolerance > 0 and result.root_moves:
            best_score = result.root_moves[0].score
            candidates = [
                rm for rm in result.root_moves[:top_k]
                if best_score - rm.score <= tolerance
            ]
            if len(candidates) > 1:
                chosen = self.rng.choice(candidates)
                result.best_move = chosen.move
                result.pv = self.engine.search(
                    board, SearchLimits(depth=max(1, result.depth), movetime=min(0.6, think_ms / 1000.0)),
                    root_moves=[chosen.move],
                ).pv or [chosen.move]
                result.pv_san = san_line(board, result.pv)
        return result


# --------------------------------------------------------------------------- #
#  Move grading (powers the "coach" panel)
# --------------------------------------------------------------------------- #

GRADE_ORDER = ["Brilliant", "Best move", "Good", "Inaccuracy", "Mistake", "Blunder"]

GRADE_LABELS = {
    "Brilliant": ("!!", "excellent, the sharpest move in the position"),
    "Best move": ("!", "the engine's first choice"),
    "Good": ("", "keeps the evaluation, a healthy move"),
    "Inaccuracy": ("?!", "lets a little of your advantage slip"),
    "Mistake": ("?", "an error that changes the evaluation"),
    "Blunder": ("??", "a serious error"),
}

GRADE_THRESHOLDS = (
    (15, "Best move"),
    (45, "Good"),
    (100, "Inaccuracy"),
    (250, "Mistake"),
)


def _grade(loss_cp: int, played_is_best: bool) -> str:
    if played_is_best:
        return "Best move"
    for threshold, label in GRADE_THRESHOLDS:
        if loss_cp <= threshold:
            return label
    return "Blunder"


def analyse_move(
    board: chess.Board,
    move: chess.Move,
    engine: Engine | None = None,
    movetime_ms: int | None = 450,
    depth: int | None = None,
) -> MoveReport:
    """Grade `move` in `board` (the position *before* the move was played).

    Passing ``movetime_ms=None`` searches to a fixed ``depth`` instead, which
    makes the grading fully deterministic (handy for tests).
    """
    if not board.is_legal(move):
        raise ValueError(f"{move.uci()} is not legal in this position")
    engine = engine or Engine()

    target_depth = depth or 8
    limits = SearchLimits(depth=target_depth)
    if movetime_ms:
        limits.movetime = movetime_ms / 1000.0
    best = engine.search(board, limits)

    top_score = best.score
    best_uci = best.best_move.uci() if best.best_move else None
    played_uci = move.uci()

    if played_uci == best_uci and best.best_move is not None:
        played_score = top_score
    else:
        played_limits = SearchLimits(depth=max(1, best.depth or target_depth))
        if movetime_ms:
            played_limits.movetime = min(0.8, movetime_ms / 1000.0)
        played = engine.search(
            board,
            played_limits,
            root_moves=[move],
        )
        played_score = played.score if played.best_move else top_score

    loss = max(0, top_score - played_score)
    grade = _grade(loss, played_uci == best_uci)

    note = None
    if best.mate_in and best.mate_in > 0 and grade in ("Mistake", "Blunder", "Inaccuracy"):
        note = f"You missed a forced mate in {abs(best.mate_in)}."
    elif grade == "Blunder":
        note = "That hands over serious material or allows a winning reply."
    elif grade == "Mistake":
        note = "The evaluation swung your opponent's way."
    elif grade == "Inaccuracy":
        note = "Small slip — your advantage is smaller now."

    return MoveReport(
        move=move,
        san=san_of(board, move),
        uci=played_uci,
        grade=grade,
        loss_cp=int(loss),
        score_before_cp=int(top_score),
        score_after_cp=int(played_score),
        best_move=best.best_move,
        best_san=best.pv_san[0] if best.pv_san else None,
        pv=best.pv_san,
        mate_in=best.mate_in,
        note=note,
    )


def play_game(
    white: Bot,
    black: Bot,
    max_moves: int = 300,
    move_time_ms: int | None = None,
    on_move=None,
) -> chess.Board:
    """Play a game between two bots (used by the self-play tests)."""
    board = chess.Board()
    while not board.is_game_over(claim_draw=True) and len(board.move_stack) < max_moves:
        bot = white if board.turn == chess.WHITE else black
        result = bot.pick_move(board, movetime_ms=move_time_ms)
        if result.best_move is None:
            break
        board.push(result.best_move)
        if on_move is not None:
            on_move(board, result)
    return board
