"""Alpha-beta search for the chess engine.

Features
--------
* iterative deepening with aspiration windows
* principal variation search (null-window scouting + re-search)
* transposition table with Zobrist keys
* quiescence search with MVV-LVA ordering and delta pruning
* null-move pruning and reverse futility pruning
* late move reductions, check extensions
* killer moves + history heuristic
* mate-distance aware scores and PV reporting for the UI
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

import chess

from .evaluate import EvalCache, evaluate_white

MATE_SCORE = 30_000
MATE_THRESHOLD = MATE_SCORE - 1_000
INFINITY = 1_000_000

# Transposition table flag values.
TT_EXACT = 0
TT_LOWER = 1
TT_UPPER = 2

_NULL_MOVE = chess.Move.null()

# python-chess exposes a fast incremental key; fall back to polyglot hashing.
if hasattr(chess.Board, "_transposition_key"):
    def _position_key(board: chess.Board):
        return board._transposition_key()
else:  # pragma: no cover - depends on python-chess version
    def _position_key(board: chess.Board):
        import chess.polyglot

        return chess.polyglot.zobrist_hash(board)


class SearchTimeout(Exception):
    """Raised internally when the time budget or a stop request is hit."""


@dataclass
class SearchLimits:
    """How long / how deep the engine may think."""

    depth: int = 64
    movetime: float | None = None  # seconds
    nodes: int | None = None

    @classmethod
    def from_millis(cls, millis: int | None, depth: int = 64) -> "SearchLimits":
        if millis is None:
            return cls(depth=depth)
        return cls(depth=depth, movetime=millis / 1000.0)


@dataclass
class RootMove:
    move: chess.Move
    score: int
    san: str = ""

    def as_dict(self) -> dict:
        return {"uci": self.move.uci(), "san": self.san, "score": self.score}


@dataclass
class SearchResult:
    best_move: chess.Move | None
    score: int = 0                     # centipawns, side to move's point of view
    depth: int = 0
    seldepth: int = 0
    nodes: int = 0
    time_ms: int = 0
    pv: list[chess.Move] = field(default_factory=list)
    pv_san: list[str] = field(default_factory=list)
    root_moves: list[RootMove] = field(default_factory=list)
    from_book: bool = False
    stopped: bool = False

    @property
    def nps(self) -> int:
        return int(self.nodes / self.time_ms * 1000) if self.time_ms else 0

    @property
    def mate_in(self) -> int | None:
        """Positive: the side to move mates in this many moves."""
        if self.score >= MATE_THRESHOLD:
            return (MATE_SCORE - self.score + 1) // 2
        if self.score <= -MATE_THRESHOLD:
            return -((MATE_SCORE + self.score + 1) // 2)
        return None

    @property
    def eval_white(self) -> int:
        return self.score

    def as_dict(self) -> dict:
        return {
            "best": self.best_move.uci() if self.best_move else None,
            "best_san": self.pv_san[0] if self.pv_san else None,
            "score": self.score,
            "mate_in": self.mate_in,
            "depth": self.depth,
            "seldepth": self.seldepth,
            "nodes": self.nodes,
            "nps": self.nps,
            "time_ms": self.time_ms,
            "pv": [m.uci() for m in self.pv],
            "pv_san": self.pv_san,
            "book": self.from_book,
            "root_moves": [rm.as_dict() for rm in self.root_moves[:5]],
        }


class Engine:
    """A self-contained alpha-beta engine instance (own TT + eval cache)."""

    def __init__(self, tt_entries: int = 400_000, features: dict | None = None) -> None:
        self.tt: dict = {}
        self.tt_entries = tt_entries
        self.eval_cache = EvalCache()
        #: Flip individual search features off (used by the test-suite to
        #: compare search behaviour with and without aggressive pruning).
        self.features = {
            "aspiration": True,
            "null_move": True,
            "rfp": True,      # reverse futility pruning
            "lmr": True,      # late move reductions
            "tt": True,
            "delta": True,    # delta pruning in quiescence
        }
        if features:
            self.features.update(features)
        self.killers = [[None, None] for _ in range(128)]
        self.history: dict[tuple[int, int], int] = {}
        self._stop_event = None
        self._deadline = None
        self._node_limit = None
        self.reset_stats()

    # ------------------------------------------------------------------ #
    #  bookkeeping
    # ------------------------------------------------------------------ #
    def reset_stats(self) -> None:
        self.nodes = 0
        self.seldepth = 0
        self.tt_hits = 0
        self.cutoffs = 0
        self._stop_event = None
        self._deadline = None
        self._node_limit = None

    def clear(self) -> None:
        self.tt.clear()
        self.eval_cache.clear()
        self.history.clear()
        self.reset_stats()

    # ------------------------------------------------------------------ #
    #  public API
    # ------------------------------------------------------------------ #
    def search(
        self,
        board: chess.Board,
        limits: SearchLimits | None = None,
        info: Callable[[SearchResult], None] | None = None,
        stop_event=None,
        root_moves: Iterable[chess.Move] | None = None,
    ) -> SearchResult:
        """Search `board` and return the best move plus reporting info."""
        limits = limits or SearchLimits()
        self.reset_stats()
        self.killers = [[None, None] for _ in range(128)]
        self._stop_event = stop_event
        self._deadline = (
            time.monotonic() + limits.movetime if limits.movetime else None
        )
        self._node_limit = limits.nodes
        started = time.monotonic()

        if root_moves is None:
            legal = list(board.legal_moves)
        else:
            # Only search moves that are actually playable in this position.
            legal = [move for move in root_moves if board.is_legal(move)]
        if not legal:
            return SearchResult(best_move=None, time_ms=0)

        ordered = [(0, move) for move in legal]
        result = SearchResult(best_move=ordered[0][1], pv=[ordered[0][1]])

        for depth in range(1, limits.depth + 1):
            try:
                score, root_scores = self._iterate(board, depth, ordered)
            except SearchTimeout:
                result.stopped = True
                break

            ordered = sorted(root_scores, key=lambda item: -item[0])
            pv = self._extract_pv(board)
            result = SearchResult(
                best_move=ordered[0][1],
                score=score,
                depth=depth,
                seldepth=self.seldepth,
                nodes=self.nodes,
                time_ms=int((time.monotonic() - started) * 1000),
                pv=pv,
                pv_san=san_line(board, pv),
                root_moves=[
                    RootMove(move=move, score=move_score, san=san_of(board, move))
                    for move_score, move in ordered
                ],
            )
            if info:
                info(result)
            if abs(score) >= MATE_THRESHOLD:
                break
            if self._deadline and time.monotonic() >= self._deadline - 0.002:
                break

        result.nodes = self.nodes
        result.seldepth = max(result.seldepth, self.seldepth)
        result.time_ms = max(result.time_ms, int((time.monotonic() - started) * 1000))
        return result

    def evaluate_for_white(self, board: chess.Board) -> int:
        return evaluate_white(board)

    # ------------------------------------------------------------------ #
    #  iterative deepening driver
    # ------------------------------------------------------------------ #
    def _iterate(self, board, depth, ordered):
        """One full-width iteration. Returns (score, root scores)."""
        alpha, beta = -INFINITY, INFINITY
        window = 25 if self.features["aspiration"] else INFINITY
        while True:
            root_scores: list[tuple[int, chess.Move]] = []
            score = self._pvs(board, depth, alpha, beta, 0, True, ordered, root_scores)[0]
            if score <= alpha and alpha > -INFINITY:
                alpha = max(score - window, -INFINITY)
                window *= 4
                continue
            if score >= beta and beta < INFINITY:
                beta = min(score + window, INFINITY)
                window *= 4
                continue
            return score, root_scores

    def _extract_pv(self, board, max_length: int = 20) -> list[chess.Move]:
        """Read the principal variation back out of the transposition table."""
        line: list[chess.Move] = []
        clone = board.copy(stack=False)
        seen = set()
        for _ in range(max_length):
            entry = self.tt.get(_position_key(clone))
            if not entry or entry[3] is None:
                break
            move = entry[3]
            if move not in clone.legal_moves:
                break
            line.append(move)
            clone.push(move)
            key = _position_key(clone)
            if key in seen:
                break
            seen.add(key)
        return line

    # ------------------------------------------------------------------ #
    #  principal variation search
    # ------------------------------------------------------------------ #
    def _pvs(self, board, depth, alpha, beta, ply, pv_node, ordered=None, root_scores=None):
        self.nodes += 1
        if self.nodes & 1023 == 0:
            self._check_time()
        if ply > self.seldepth:
            self.seldepth = ply

        # --- draws ------------------------------------------------------ #
        if ply and (board.is_insufficient_material() or board.halfmove_clock >= 100):
            return 0, []
        if ply and board.halfmove_clock >= 4 and board.is_repetition(2):
            return 0, []

        if depth <= 0:
            return self._quiescence(board, alpha, beta, ply), []

        in_check = board.is_check()
        if in_check and depth < 32:
            depth += 1  # check extension

        key = _position_key(board)
        tt_move = None
        entry = self.tt.get(key) if self.features["tt"] else None
        if entry is not None:
            e_depth, e_score, e_flag, e_move = entry
            tt_move = e_move
            if e_depth >= depth and not pv_node:
                self.tt_hits += 1
                score = self._from_tt(e_score, ply)
                if e_flag == TT_EXACT:
                    return score, [e_move] if e_move else []
                if e_flag == TT_LOWER and score >= beta:
                    return score, []
                if e_flag == TT_UPPER and score <= alpha:
                    return score, []

        # --- reverse futility pruning ----------------------------------- #
        if (
            self.features["rfp"]
            and not pv_node
            and not in_check
            and depth <= 3
            and abs(beta) < MATE_THRESHOLD
        ):
            static = self._eval(board)
            if static - 130 * depth >= beta:
                return static, []

        # --- null move pruning ------------------------------------------ #
        if (
            self.features["null_move"]
            and not pv_node
            and not in_check
            and depth >= 3
            and beta < MATE_THRESHOLD
            and ply
            and self._has_non_pawn_material(board, board.turn)
        ):
            reduction = 2 + depth // 5
            board.push(_NULL_MOVE)
            try:
                score = -self._pvs(
                    board, depth - 1 - reduction, -beta, -beta + 1, ply + 1, False
                )[0]
            except SearchTimeout:
                board.pop()
                raise
            board.pop()
            if score >= beta and abs(score) < MATE_THRESHOLD:
                return beta, []

        if ordered is None:
            moves = self._order_moves(board, self._legal_moves(board), tt_move, ply)
        else:
            # Root node: keep the caller's ordering (best move from the previous
            # iteration first) so alpha-beta gets the cheapest cutoffs.
            moves = [m for _, m in ordered]

        if not moves:
            return (-MATE_SCORE + ply, []) if in_check else (0, [])

        best_score = -INFINITY
        best_move = None
        original_alpha = alpha
        move_index = 0
        use_lmr = self.features["lmr"]

        for move in moves:
            is_capture = board.is_capture(move)
            # Late move reductions only apply to quiet, non-checking late moves.
            try_reduction = (
                use_lmr
                and depth >= 3
                and move_index >= 3
                and not is_capture
                and not move.promotion
                and not in_check
            )
            gives_check = board.gives_check(move) if try_reduction else False

            board.push(move)
            try:
                if move_index == 0:
                    # First move gets the full window.
                    score = -self._pvs(board, depth - 1, -beta, -alpha, ply + 1, pv_node)[0]
                else:
                    reduction = 0
                    if try_reduction and not gives_check:
                        reduction = 1 + (1 if (move_index >= 6 and depth >= 5) else 0)
                        reduction = min(reduction, depth - 2)
                    # Null-window scout, optionally at reduced depth.
                    score = -self._pvs(
                        board, depth - 1 - reduction, -alpha - 1, -alpha, ply + 1, False
                    )[0]
                    if reduction and score > alpha:
                        score = -self._pvs(
                            board, depth - 1, -alpha - 1, -alpha, ply + 1, False
                        )[0]
                    if alpha < score < beta:
                        # Scout beat alpha: re-search with the real window.
                        score = -self._pvs(
                            board, depth - 1, -beta, -alpha, ply + 1, pv_node
                        )[0]
            finally:
                board.pop()

            if root_scores is not None:
                root_scores.append((score, move))

            if score > best_score:
                best_score = score
                best_move = move
            if score > alpha:
                alpha = score
            if alpha >= beta:
                self.cutoffs += 1
                if not is_capture:
                    self._store_killer(move, ply)
                    slot = (board.turn << 12) | (move.from_square << 6) | move.to_square
                    self.history[slot] = self.history.get(slot, 0) + depth * depth
                break
            move_index += 1

        if len(self.tt) > self.tt_entries:
            self.tt.clear()
        flag = TT_EXACT
        if best_score <= original_alpha:
            flag = TT_UPPER
        elif best_score >= beta:
            flag = TT_LOWER
        self.tt[key] = (depth, self._to_tt(best_score, ply), flag, best_move)

        return best_score, []

    # ------------------------------------------------------------------ #
    #  quiescence
    # ------------------------------------------------------------------ #
    def _quiescence(self, board, alpha, beta, ply, qply=0):
        self.nodes += 1
        if self.nodes & 1023 == 0:
            self._check_time()
        if ply > self.seldepth:
            self.seldepth = ply

        in_check = board.is_check()

        if in_check:
            # Never stand pat in check: search every evasion.
            moves = list(board.legal_moves)
            if not moves:
                return -MATE_SCORE + ply
            moves = self._order_moves(board, moves, None, ply)
            if qply > 16:  # guard against endless check sequences
                return self._eval(board)
            best = -MATE_SCORE
            for move in moves:
                board.push(move)
                try:
                    score = -self._quiescence(board, -beta, -alpha, ply + 1, qply + 1)
                finally:
                    board.pop()
                if score > best:
                    best = score
                if score > alpha:
                    alpha = score
                if alpha >= beta:
                    return best
            return best

        stand_pat = self._eval(board)
        if stand_pat >= beta:
            return stand_pat
        if stand_pat > alpha:
            alpha = stand_pat

        moves = [m for m in self._legal_moves(board) if board.is_capture(m) or m.promotion]
        moves = self._order_captures(board, moves)
        use_delta = self.features["delta"]

        for move in moves:
            # delta pruning: skip hopeless captures
            victim = board.piece_at(move.to_square)
            gain = 0 if victim is None else PIECE_GAIN[victim.piece_type]
            if move.promotion:
                gain += PIECE_GAIN[move.promotion]
            if use_delta and stand_pat + gain + 150 < alpha:
                continue

            board.push(move)
            try:
                score = -self._quiescence(board, -beta, -alpha, ply + 1, qply + 1)
            finally:
                board.pop()

            if score >= beta:
                return score
            if score > alpha:
                alpha = score
        return alpha

    # ------------------------------------------------------------------ #
    #  move helpers
    # ------------------------------------------------------------------ #
    def _legal_moves(self, board):
        return list(board.legal_moves)

    def _order_moves(self, board, moves, tt_move, ply):
        killers = self.killers[ply]
        scored = []
        for move in moves:
            if move == tt_move:
                scored.append((1 << 30, move))
                continue
            if board.is_capture(move):
                victim = board.piece_at(move.to_square)
                attacker = board.piece_at(move.from_square)
                victim_value = PIECE_GAIN[victim.piece_type] if victim else 100
                attacker_value = PIECE_GAIN[attacker.piece_type] if attacker else 100
                scored.append(((1 << 24) + victim_value * 16 - attacker_value, move))
                continue
            if move.promotion:
                scored.append(((1 << 23) + PIECE_GAIN[move.promotion], move))
                continue
            if move == killers[0]:
                scored.append(((1 << 22) + 1, move))
                continue
            if move == killers[1]:
                scored.append(((1 << 22), move))
                continue
            slot = (board.turn << 12) | (move.from_square << 6) | move.to_square
            scored.append((self.history.get(slot, 0), move))
        scored.sort(key=lambda item: -item[0])
        return [move for _, move in scored]

    def _order_captures(self, board, moves):
        scored = []
        for move in moves:
            victim = board.piece_at(move.to_square)
            victim_value = PIECE_GAIN[victim.piece_type] if victim else 100
            attacker = board.piece_at(move.from_square)
            attacker_value = PIECE_GAIN[attacker.piece_type] if attacker else 100
            bonus = PIECE_GAIN[move.promotion] if move.promotion else 0
            scored.append(((victim_value * 16 - attacker_value + bonus, move)))
        scored.sort(key=lambda item: -item[0])
        return [move for _, move in scored]

    def _store_killer(self, move, ply):
        if ply >= len(self.killers):
            return
        slot = self.killers[ply]
        if slot[0] != move:
            slot[1] = slot[0]
            slot[0] = move

    @staticmethod
    def _has_non_pawn_material(board, color) -> bool:
        material = (
            board.pieces_mask(chess.KNIGHT, color)
            | board.pieces_mask(chess.BISHOP, color)
            | board.pieces_mask(chess.ROOK, color)
            | board.pieces_mask(chess.QUEEN, color)
        )
        return bool(material)

    def _eval(self, board):
        return self.eval_cache.get(board) * (1 if board.turn == chess.WHITE else -1)

    def _check_time(self):
        if self._stop_event is not None and self._stop_event.is_set():
            raise SearchTimeout
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise SearchTimeout
        if self._node_limit is not None and self.nodes >= self._node_limit:
            raise SearchTimeout

    # --- mate-score fixups for the transposition table ------------------ #
    @staticmethod
    def _to_tt(score: int, ply: int) -> int:
        if score >= MATE_THRESHOLD:
            return score + ply
        if score <= -MATE_THRESHOLD:
            return score - ply
        return score

    @staticmethod
    def _from_tt(score: int, ply: int) -> int:
        if score >= MATE_THRESHOLD:
            return score - ply
        if score <= -MATE_THRESHOLD:
            return score + ply
        return score


PIECE_GAIN = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 20_000,
}


# --------------------------------------------------------------------------- #
#  small SAN helpers (used for reporting)
# --------------------------------------------------------------------------- #

def san_of(board: chess.Board, move: chess.Move) -> str:
    try:
        return board.san(move)
    except Exception:  # pragma: no cover - defensive
        return move.uci()


def san_line(board: chess.Board, moves: Iterable[chess.Move], max_moves: int = 12) -> list[str]:
    """Convert a PV into readable SAN, stopping if the line ever goes illegal."""
    clone = board.copy(stack=False)
    legal = clone.legal_moves
    line = []
    for index, move in enumerate(moves):
        if index >= max_moves or move not in legal:
            break
        try:
            line.append(clone.san(move))
        except Exception:  # pragma: no cover - defensive
            break
        clone.push(move)
        legal = clone.legal_moves
    return line
