"""Static evaluation for the chess engine.

The evaluation is *tapered*: every term contributes to a middlegame score and an
endgame score, and the two are blended together according to how much material
is left on the board.  That way the engine knows a king belongs behind its
pawns early on and in the centre once the queens come off.

All scores are centipawns from **White's** point of view.  The search negates
them as needed.
"""

from __future__ import annotations

import chess

# --------------------------------------------------------------------------- #
#  Material
# --------------------------------------------------------------------------- #

MG_VALUE = {
    chess.PAWN: 82,
    chess.KNIGHT: 337,
    chess.BISHOP: 365,
    chess.ROOK: 477,
    chess.QUEEN: 1025,
    chess.KING: 0,
}

EG_VALUE = {
    chess.PAWN: 94,
    chess.KNIGHT: 281,
    chess.BISHOP: 297,
    chess.ROOK: 512,
    chess.QUEEN: 936,
    chess.KING: 0,
}

#: How much each piece contributes to "game phase".  24 == full middlegame.
PHASE_VALUE = {
    chess.PAWN: 0,
    chess.KNIGHT: 1,
    chess.BISHOP: 1,
    chess.ROOK: 2,
    chess.QUEEN: 4,
    chess.KING: 0,
}
MAX_PHASE = 24


def _table(*rows: str) -> tuple[int, ...]:
    """Parse a piece-square table written the way a human reads a board.

    The first row is rank 8 (a8..h8) and the last row is rank 1 (a1..h1).
    """
    values: list[int] = []
    for row in rows:
        values.extend(int(token) for token in row.replace(",", " ").split())
    if len(values) != 64:
        raise ValueError(f"piece-square table needs 64 values, got {len(values)}")
    return tuple(values)


# --------------------------------------------------------------------------- #
#  Piece-square tables (PeSTO-style)
# --------------------------------------------------------------------------- #

PAWN_MG = _table(
    "  0,  0,  0,  0,  0,  0,  0,  0",
    " 98,134, 61, 95, 68,126, 34,-11",
    " -6,  7, 26, 31, 65, 56, 25,-20",
    "-14, 13,  6, 21, 23, 12, 17,-23",
    "-27, -2, -5, 12, 17,  6, 10,-25",
    "-26, -4, -4,-10,  3,  3, 33,-12",
    "-35, -1,-20,-23,-15, 24, 38,-22",
    "  0,  0,  0,  0,  0,  0,  0,  0",
)
PAWN_EG = _table(
    "  0,  0,  0,  0,  0,  0,  0,  0",
    "178,173,158,134,147,132,165,187",
    " 94,100, 85, 67, 56, 53, 82, 84",
    " 32, 24, 13,  5, -2,  4, 17, 17",
    " 13,  9, -3, -7, -7, -8,  3, -1",
    "  4,  7, -6,  1,  0, -5, -1, -8",
    " 13,  8,  8, 10, 13,  0,  2, -7",
    "  0,  0,  0,  0,  0,  0,  0,  0",
)
KNIGHT_MG = _table(
    "-167,-89,-34,-49, 61,-97,-15,-107",
    " -73,-41, 72, 36, 23, 62,  7, -17",
    " -47, 60, 37, 65, 84,129, 73,  44",
    "  -9, 17, 19, 53, 37, 69, 18,  22",
    " -13,  4, 16, 13, 28, 19, 21,  -8",
    " -23, -9, 12, 10, 19, 17, 25, -16",
    " -29,-53,-12, -3, -1, 18,-14, -19",
    "-105,-21,-58,-33,-17,-28,-19, -23",
)
KNIGHT_EG = _table(
    "-58,-38,-13,-28,-31,-27,-63,-99",
    "-25, -8,-25, -2, -9,-25,-24,-52",
    "-24,-20, 10,  9, -1, -9,-19,-41",
    "-17,  3, 22, 22, 22, 11,  8,-18",
    "-18, -6, 16, 25, 16, 17,  4,-18",
    "-23, -3, -1, 15, 10, -3,-20,-22",
    "-42,-20,-10, -5, -2,-20,-23,-44",
    "-29,-51,-23,-15,-22,-18,-50,-64",
)
BISHOP_MG = _table(
    "-29,  4,-82,-37,-25,-42,  7, -8",
    "-26, 16,-18,-13, 30, 59, 18,-47",
    "-16, 37, 43, 40, 35, 50, 37, -2",
    " -4,  5, 19, 50, 37, 37,  7, -2",
    " -6, 13, 13, 26, 34, 12, 10,  4",
    "  0, 15, 15, 15, 14, 27, 18, 10",
    "  4, 15, 16,  0,  7, 21, 33,  1",
    "-33, -3,-14,-21,-13,-12,-39,-21",
)
BISHOP_EG = _table(
    "-14,-21,-11, -8, -7, -9,-17,-24",
    " -8, -4,  7,-12, -3,-13, -4,-14",
    "  2, -8,  0, -1, -2,  6,  0,  4",
    " -3,  9, 12,  9, 14, 10,  3,  2",
    " -6,  3, 13, 19,  7, 10, -3, -9",
    "-12, -3,  8, 10, 13,  3, -7,-15",
    "-14,-18, -7, -1,  4, -9,-15,-27",
    "-23, -9,-23, -5, -9,-16, -5,-17",
)
ROOK_MG = _table(
    " 32, 42, 32, 51, 63,  9, 31, 43",
    " 27, 32, 58, 62, 80, 67, 26, 44",
    " -5, 19, 26, 36, 17, 45, 61, 16",
    "-24,-11,  7, 26, 24, 35, -8,-20",
    "-36,-26,-12, -1,  9, -7,  6,-23",
    "-45,-25,-16,-17,  3,  0, -5,-33",
    "-44,-16,-20, -9, -1, 11, -6,-71",
    "-19,-13,  1, 17, 16,  7,-37,-26",
)
ROOK_EG = _table(
    " 13, 10, 18, 15, 12, 12,  8,  5",
    " 11, 13, 13, 11, -3,  3,  8,  3",
    "  7,  7,  7,  5,  4, -3, -5, -3",
    "  4,  3, 13,  1,  2,  1, -1,  2",
    "  3,  5,  8,  4, -5, -6, -8,-11",
    " -4,  0, -5, -1, -7,-12, -8,-16",
    " -6, -6,  0,  2, -9, -9,-11, -3",
    " -9,  2,  3, -1, -5,-13,  4,-20",
)
QUEEN_MG = _table(
    "-28,  0, 29, 12, 59, 44, 43, 45",
    "-24,-39, -5,  1,-16, 57, 28, 54",
    "-13,-17,  7,  8, 29, 56, 47, 57",
    "-27,-27,-16,-16, -1, 17, -2,  1",
    " -9,-26, -9,-10, -2, -4,  3, -3",
    "-14,  2,-11, -2, -5,  2, 14,  5",
    "-35, -8, 11,  2,  8, 15, -3,  1",
    " -1,-18, -9, 10,-15,-25,-31,-50",
)
QUEEN_EG = _table(
    " -9, 22, 22, 27, 27, 19, 10, 20",
    "-17, 20, 32, 41, 58, 25, 30,  0",
    "-20,  6,  9, 49, 47, 35, 19,  9",
    "  3, 22, 24, 45, 57, 40, 57, 36",
    "-18, 28, 19, 47, 31, 34, 39, 23",
    "-16,-27, 15,  6,  9, 17, 10,  5",
    "-22,-23,-30,-16,-16,-23,-36,-32",
    "-33,-28,-22,-43, -5,-32,-20,-41",
)
KING_MG = _table(
    "-65, 23, 16,-15,-56,-34,  2, 13",
    " 29, -1,-20, -7, -8, -4,-38,-29",
    " -9, 24,  2,-16,-20,  6, 22,-22",
    "-17,-20,-12,-27,-30,-25,-14,-36",
    "-49, -1,-27,-39,-46,-44,-33,-51",
    "-14,-14,-22,-46,-44,-30,-15,-27",
    "  1,  7, -8,-64,-43,-16,  9,  8",
    "-15, 36, 12,-54,  8,-28, 24, 14",
)
KING_EG = _table(
    "-74,-35,-18,-18,-11, 15,  4,-17",
    "-12, 17, 14, 17, 17, 38, 23, 11",
    " 10, 17, 23, 15, 20, 45, 44, 13",
    " -8, 22, 24, 27, 26, 33, 26,  3",
    "-18, -4, 21, 24, 27, 23,  9,-11",
    "-19, -3, 11, 21, 23, 16,  7, -9",
    "-27,-11,  4, 13, 14,  4, -5,-17",
    "-53,-34,-21,-11,-28,-14,-24,-43",
)

MG_PST = {
    chess.PAWN: PAWN_MG,
    chess.KNIGHT: KNIGHT_MG,
    chess.BISHOP: BISHOP_MG,
    chess.ROOK: ROOK_MG,
    chess.QUEEN: QUEEN_MG,
    chess.KING: KING_MG,
}
EG_PST = {
    chess.PAWN: PAWN_EG,
    chess.KNIGHT: KNIGHT_EG,
    chess.BISHOP: BISHOP_EG,
    chess.ROOK: ROOK_EG,
    chess.QUEEN: QUEEN_EG,
    chess.KING: KING_EG,
}

# The tables above are written with rank 8 on the first row, so a White piece on
# square `sq` indexes `sq ^ 56` and a Black piece indexes `sq` (the rank flip is
# already baked into the White formula).
_flip = [sq ^ 56 for sq in range(64)]

# --------------------------------------------------------------------------- #
#  Pre-computed masks
# --------------------------------------------------------------------------- #

_BB_RANKS = [chess.BB_RANKS[i] for i in range(8)]
_FULL = chess.BB_ALL

#: Squares strictly in front of a square (for passed-pawn detection).
RANKS_ABOVE = [[_BB_RANKS[r] for r in range(8)] for _ in (chess.WHITE, chess.BLACK)]
for _r in range(8):
    RANKS_ABOVE[chess.WHITE][_r] = 0
    RANKS_ABOVE[chess.BLACK][_r] = 0
    for _r2 in range(_r + 1, 8):
        RANKS_ABOVE[chess.WHITE][_r] |= _BB_RANKS[_r2]
    for _r2 in range(0, _r):
        RANKS_ABOVE[chess.BLACK][_r] |= _BB_RANKS[_r2]

FILE_AND_NEIGHBOURS = []
for _f in range(8):
    _mask = chess.BB_FILES[_f]
    if _f > 0:
        _mask |= chess.BB_FILES[_f - 1]
    if _f < 7:
        _mask |= chess.BB_FILES[_f + 1]
    FILE_AND_NEIGHBOURS.append(_mask)

#: Bonus for a passed pawn, indexed by how far it has advanced.
PASSED_MG = (0, 5, 10, 20, 35, 60, 95, 0)
PASSED_EG = (0, 10, 22, 38, 60, 100, 155, 0)

MOBILITY_WEIGHT = {
    chess.KNIGHT: (3, 3),
    chess.BISHOP: (3, 3),
    chess.ROOK: (2, 4),
    chess.QUEEN: (1, 2),
}


# Pre-combine material value + piece-square bonus into flat lookup tables so the
# hot loop does one list index per piece instead of a dict lookup plus addition.
MG_W, EG_W, MG_B, EG_B = {}, {}, {}, {}
for _pt in range(1, 7):
    MG_W[_pt] = tuple(MG_VALUE[_pt] + MG_PST[_pt][sq ^ 56] for sq in range(64))
    EG_W[_pt] = tuple(EG_VALUE[_pt] + EG_PST[_pt][sq ^ 56] for sq in range(64))
    MG_B[_pt] = tuple(MG_VALUE[_pt] + MG_PST[_pt][sq] for sq in range(64))
    EG_B[_pt] = tuple(EG_VALUE[_pt] + EG_PST[_pt][sq] for sq in range(64))

_PIECE_TYPES = (chess.PAWN, chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN, chess.KING)


def evaluate_white(board: chess.Board) -> int:
    """Static evaluation in centipawns, positive means White is better."""
    mg = 0
    eg = 0
    phase = 0
    scan = chess.scan_forward
    attacks_mask = board.attacks_mask
    mobility_weight = MOBILITY_WEIGHT

    # NOTE: a dict, not a tuple — chess.WHITE is True and would collide with
    # index 1 of a (white, black) tuple.
    pawns = {
        chess.WHITE: board.pawns & board.occupied_co[chess.WHITE],
        chess.BLACK: board.pawns & board.occupied_co[chess.BLACK],
    }

    # ---- material, piece squares, mobility ------------------------------- #
    for piece_type in _PIECE_TYPES:
        weight = mobility_weight.get(piece_type)
        white_bb = board.pieces_mask(piece_type, chess.WHITE)
        black_bb = board.pieces_mask(piece_type, chess.BLACK)
        if white_bb:
            mg_table, eg_table = MG_W[piece_type], EG_W[piece_type]
            for square in scan(white_bb):
                mg += mg_table[square]
                eg += eg_table[square]
            phase += PHASE_VALUE[piece_type] * white_bb.bit_count()
            if weight:
                wmg, weg = weight
                for square in scan(white_bb):
                    moves = attacks_mask(square).bit_count()
                    mg += wmg * moves
                    eg += weg * moves
        if black_bb:
            mg_table, eg_table = MG_B[piece_type], EG_B[piece_type]
            for square in scan(black_bb):
                mg -= mg_table[square]
                eg -= eg_table[square]
            phase += PHASE_VALUE[piece_type] * black_bb.bit_count()
            if weight:
                wmg, weg = weight
                for square in scan(black_bb):
                    moves = attacks_mask(square).bit_count()
                    mg -= wmg * moves
                    eg -= weg * moves

    # ---- pawn structure -------------------------------------------------- #
    for color in (chess.WHITE, chess.BLACK):
        sign = 1 if color == chess.WHITE else -1
        own_pawns = pawns[color]
        enemy_pawns = pawns[not color]
        if not own_pawns:
            continue

        counts = [0] * 8
        squares = list(scan(own_pawns))
        for square in squares:
            counts[square & 7] += 1

        for file_index, count in enumerate(counts):
            if count > 1:  # doubled / tripled pawns
                mg -= sign * 11 * (count - 1)
                eg -= sign * 22 * (count - 1)
            if count and (file_index == 0 or counts[file_index - 1] == 0) and (
                file_index == 7 or counts[file_index + 1] == 0
            ):  # isolated pawn
                mg -= sign * 14
                eg -= sign * 16

        for square in squares:
            rank = square >> 3
            neighbours = FILE_AND_NEIGHBOURS[square & 7]
            ahead = RANKS_ABOVE[color][rank] & neighbours
            if not (enemy_pawns & ahead):  # passed pawn
                advance = rank if color == chess.WHITE else 7 - rank
                mg += sign * PASSED_MG[advance]
                eg += sign * PASSED_EG[advance]

    # ---- bishop pair, rook placement ------------------------------------- #
    for color in (chess.WHITE, chess.BLACK):
        sign = 1 if color == chess.WHITE else -1
        if board.pieces_mask(chess.BISHOP, color).bit_count() >= 2:
            mg += sign * 22
            eg += sign * 46

        rooks = board.pieces_mask(chess.ROOK, color)
        if rooks:
            own = pawns[color]
            their = pawns[not color]
            for square in scan(rooks):
                file_mask = chess.BB_FILES[square & 7]
                if not (own & file_mask):
                    if not (their & file_mask):  # fully open file
                        mg += sign * 26
                        eg += sign * 12
                    else:  # half-open file
                        mg += sign * 12
                        eg += sign * 6
                if square >> 3 in (1, 6):  # rook on the seventh
                    mg += sign * 18
                    eg += sign * 26

    # ---- king safety (middlegame flavour) -------------------------------- #
    if phase >= 8:
        board_squares = chess.BB_SQUARES
        attackers_mask = board.attackers_mask
        for color in (chess.WHITE, chess.BLACK):
            sign = 1 if color == chess.WHITE else -1
            king_square = board.king(color)
            if king_square is None:
                continue
            own = pawns[color]
            king_rank = king_square >> 3
            king_file = king_square & 7
            forward = 1 if color == chess.WHITE else -1
            shield_rank = king_rank + forward
            shield = 0
            if 0 <= shield_rank <= 7:
                for file_index in (king_file - 1, king_file, king_file + 1):
                    if 0 <= file_index <= 7:
                        if own & board_squares[shield_rank * 8 + file_index]:
                            shield += 1
            mg += sign * 14 * shield
            if not (own & chess.BB_FILES[king_file]):
                mg -= sign * 22

            attackers = attackers_mask(not color, king_square).bit_count()
            if attackers:
                mg -= sign * (10 * attackers * attackers + 8 * attackers)

    if phase > MAX_PHASE:
        phase = MAX_PHASE
    # Taper, rounding half away from zero so the score stays mirror-symmetric
    # (floor division would round towards -infinity and break +x/-x mirroring).
    total = mg * phase + eg * (MAX_PHASE - phase)
    half = MAX_PHASE // 2
    if total >= 0:
        return (total + half) // MAX_PHASE
    return -((-total + half) // MAX_PHASE)


def evaluate_side(board: chess.Board) -> int:
    """Evaluation from the point of view of the side to move."""
    score = evaluate_white(board)
    return score if board.turn == chess.WHITE else -score


class EvalCache:
    """Tiny memo for evaluations, keyed by python-chess' incremental position key."""

    __slots__ = ("_data",)

    def __init__(self) -> None:
        self._data: dict = {}

    def get(self, board: chess.Board) -> int:
        key = _position_key(board)
        cached = self._data.get(key)
        if cached is not None:
            return cached
        score = evaluate_white(board)
        if len(self._data) > 400_000:
            self._data.clear()
        self._data[key] = score
        return score

    def clear(self) -> None:
        self._data.clear()


if hasattr(chess.Board, "_transposition_key"):
    def _position_key(board: chess.Board):
        return board._transposition_key()
else:  # pragma: no cover - depends on python-chess version
    def _position_key(board: chess.Board):
        import chess.polyglot

        return chess.polyglot.zobrist_hash(board)
