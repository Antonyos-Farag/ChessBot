"""The opening book.

The raw theory lives in :mod:`engine.opening_lines` (3844 named ECO lines,
generated from the public-domain *chess-openings* dataset).  This module turns
those lines into something the bot can play from:

* every line is replayed on a board at import time, so a typo in the data fails
  loudly instead of producing an illegal move mid-game;
* positions are indexed by Zobrist hash, which means **transpositions** work for
  free — 1.d4 Nf6 2.c4 and 1.c4 Nf6 2.d4 land in the same book position;
* each move carries a *priority* (how deep the deepest line that plays it goes),
  so the bot prefers main-line theory and only occasionally wanders into a
  short sideline.

Using it::

    >>> import chess
    >>> from engine import book
    >>> move = book.lookup(chess.Board())            # some first move
    >>> move in chess.Board().legal_moves
    True
    >>> book.name_for(chess.Board())                 # nothing played yet
"""

from __future__ import annotations

import random

import chess
import chess.polyglot

from .opening_lines import OPENING_LINES

#: Deeper lines are considered better documented, but cap the effect so the
#: book keeps some variety instead of always playing the single longest line.
MAX_PRIORITY = 14


class _Book:
    """Position -> candidate moves, built once from the SAN lines."""

    def __init__(self) -> None:
        self.tree: dict = {}
        #: SAN prefix -> (eco, name), so a game can be named by what was played
        self.names: dict[tuple[str, ...], tuple[str, str]] = {}
        self.lines = 0
        self.positions = 0
        self._build()

    def _build(self) -> None:
        tree = self.tree
        for eco, name, san_line in OPENING_LINES:
            moves = san_line.split()
            board = chess.Board()
            self.lines += 1
            # index the *whole* line, so a game is only ever named as
            # specifically as the moves actually played
            key = tuple(moves)
            current = self.names.get(key)
            if current is None or len(name) < len(current[1]):
                self.names[key] = (eco, name)
            for ply, san in enumerate(moves):
                key = chess.polyglot.zobrist_hash(board)
                try:
                    move = board.parse_san(san)
                except ValueError as exc:  # pragma: no cover - guards the data
                    raise ValueError(
                        f"illegal book move {san!r} (ply {ply}) in {eco} {name!r}"
                    ) from exc
                entry = tree.get(key)
                if entry is None:
                    entry = tree[key] = {"moves": {}}
                    self.positions += 1
                slot = entry["moves"].get(move.uci())
                priority = min(len(moves) - ply, MAX_PRIORITY)
                if slot is None:
                    entry["moves"][move.uci()] = [san, priority, 1]
                else:
                    slot[1] = max(slot[1], priority)
                    slot[2] += 1
                _ = name  # (names come from the full-line index, not the tree)
                board.push(move)

    # -- queries -------------------------------------------------------- #
    def entry(self, board: chess.Board):
        return self.tree.get(chess.polyglot.zobrist_hash(board))

    def lookup(self, board: chess.Board, rng: random.Random | None = None):
        """A book move for this position, or ``None`` when out of book."""
        entry = self.entry(board)
        if not entry:
            return None
        picks = list(entry["moves"].items())
        # Weight by "how much theory plays this move": priority (how deep the
        # deepest matching line goes) times how many named lines contain it.
        # That makes 1.e4 the most likely first move and 1.a3 a rare curiosity,
        # without making the book deterministic.
        weights = [
            max(1, slot[1]) ** 2 * (1 + slot[2])
            for _, slot in picks
        ]
        chooser = rng or random
        uci, _ = chooser.choices(picks, weights=weights, k=1)[0]
        return chess.Move.from_uci(uci)

    def name(self, board: chess.Board):
        """``(eco, name)`` of the opening as actually played, or ``None``.

        Names are matched against the moves played, so a 5-ply Ruy Lopez is
        reported as "Ruy Lopez" rather than as whatever deep sub-variation
        happens to run through the same position.  Transpositions that leave
        the SAN tree fall back to the position index.
        """
        history = _san_history(board)
        if history:
            # only lines that have been played out in full count, and the
            # deepest of those wins
            for length in range(len(history), 0, -1):
                hit = self.names.get(tuple(history[:length]))
                if hit is not None:
                    return hit[0], hit[1]
        return None

    def sample_line(self, board: chess.Board, rng=None) -> list[chess.Move]:
        """A full book continuation from here (used for tips and tests)."""
        clone = board.copy(stack=False)
        line: list[chess.Move] = []
        while len(line) < 12:
            move = self.lookup(clone, rng)
            if move is None:
                break
            line.append(move)
            clone.push(move)
        return line


def _san_history(board: chess.Board) -> list[str]:
    """SAN of the moves played from the start position in this game."""
    line: list[chess.Move] = []
    clone = board.copy()
    while clone.move_stack:
        line.append(clone.pop())
    line.reverse()
    replay = chess.Board()
    out = []
    for move in line:
        try:
            out.append(replay.san(move))
        except Exception:  # pragma: no cover - defensive
            return []
        replay.push(move)
    return out


BOOK = _Book()
START_IN_BOOK = BOOK.entry(chess.Board()) is not None


# -- module level helpers (thin wrappers, keep the API friendly) ------------ #

def lookup(board: chess.Board, rng: random.Random | None = None) -> chess.Move | None:
    """Return a book move for this position, or ``None`` if out of book."""
    return BOOK.lookup(board, rng)


def name_for(board: chess.Board) -> tuple[str, str] | None:
    """``(ECO, name)`` of the opening played so far, or ``None``."""
    return BOOK.name(board)


def line_for(board: chess.Board) -> list[chess.Move]:
    """The book continuation that matches the game so far (for hints)."""
    return BOOK.sample_line(board)


def is_in_book(board: chess.Board) -> bool:
    return BOOK.entry(board) is not None


def book_move(board: chess.Board) -> tuple[str, str] | None:
    """SAN of a sample book move plus the opening name it belongs to."""
    move = lookup(board)
    if move is None:
        return None
    name = name_for(board)
    return board.san(move), (f"{name[0]} {name[1]}" if name else "")


def stats() -> dict:
    """Book size, handy for the startup banner and tests."""
    return {
        "lines": BOOK.lines,
        "positions": BOOK.positions,
        "first_moves": sorted(
            {slot[0] for slot in BOOK.entry(chess.Board())["moves"].values()}
        ),
    }


if __name__ == "__main__":  # pragma: no cover - smoke test
    info = stats()
    print(f"{info['lines']} lines -> {info['positions']} book positions")
    print("first moves:", ", ".join(info["first_moves"]))
    board = chess.Board()
    rng = random.Random(1)
    while (move := lookup(board, rng)) is not None and len(board.move_stack) < 24:
        board.push(move)
    print("random walk:", chess.Board().variation_san(board.move_stack))
    print("opening:", name_for(board))
