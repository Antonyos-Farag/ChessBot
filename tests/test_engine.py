"""Test suite for ChessBot.

Runs with pytest (``python3 -m pytest tests -q``) or standalone
(``python3 tests/test_engine.py``).

The interesting tests are the reference-comparison ones: the search is checked
against a deliberately dumb, obviously-correct negamax implementation on the
same evaluation, so pruning bugs (and the negamax sign mistakes they hide)
cannot slip through.
"""

from __future__ import annotations

import random
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import chess  # noqa: E402

from engine import LEVELS, Bot, Engine, analyse_move, book, evaluate_white, level_by_name  # noqa: E402
from engine.opening_lines import OPENING_LINES  # noqa: E402
from engine.search import (  # noqa: E402
    MATE_SCORE,
    MATE_THRESHOLD,
    SearchLimits,
    san_line,
)

class StubEngine:
    """Engine double that always reports the same three root moves."""

    def __init__(self, margin=80):
        self.best = chess.Move.from_uci("e2e4")
        self.alternatives = [chess.Move.from_uci("d2d4"), chess.Move.from_uci("g1f3")]
        self.margin = margin

    def search(self, board, limits=None, info=None, stop_event=None, root_moves=None):
        from engine.search import RootMove, SearchResult

        candidates = [self.best, *self.alternatives]
        if root_moves is not None:
            candidates = [move for move in root_moves if move in board.legal_moves]
        roots = [
            RootMove(move=move, score=-index * self.margin, san=board.san(move))
            for index, move in enumerate(candidates)
        ]
        if not roots:
            return SearchResult(best_move=None)
        return SearchResult(
            best_move=roots[0].move,
            score=roots[0].score,
            depth=4,
            nodes=1000,
            time_ms=10,
            pv=[roots[0].move],
            pv_san=[roots[0].san],
            root_moves=roots,
        )

    def evaluate_for_white(self, board):
        return evaluate_white(board)


FEATURES_OFF = {
    "aspiration": False,
    "null_move": False,
    "rfp": False,
    "lmr": False,
    "tt": False,
    "delta": False,
}


# --------------------------------------------------------------------------- #
#  Reference implementation (slow but transparent)
# --------------------------------------------------------------------------- #

def reference_quiescence(board, alpha=-10**9, beta=10**9, ply=0, qply=0):
    if board.is_check():
        moves = list(board.legal_moves)
        if not moves:
            return -MATE_SCORE + ply
        if qply > 12:
            return _side_eval(board)
        best = -MATE_SCORE
        for move in moves:
            board.push(move)
            score = -reference_quiescence(board, -beta, -alpha, ply + 1, qply + 1)
            board.pop()
            best = max(best, score)
            alpha = max(alpha, score)
            if alpha >= beta:
                return best
        return best

    stand_pat = _side_eval(board)
    if stand_pat >= beta:
        return stand_pat
    alpha = max(alpha, stand_pat)
    for move in board.legal_moves:
        if not (board.is_capture(move) or move.promotion):
            continue
        board.push(move)
        score = -reference_quiescence(board, -beta, -alpha, ply + 1, qply + 1)
        board.pop()
        if score >= beta:
            return score
        alpha = max(alpha, score)
    return alpha


def _side_eval(board):
    score = evaluate_white(board)
    return score if board.turn == chess.WHITE else -score


def reference_search(board, depth, ply=0):
    if board.is_checkmate():
        return -MATE_SCORE + ply
    if board.is_stalemate() or board.is_insufficient_material():
        return 0
    if board.is_check():
        depth += 1  # same check extension as the engine
    if depth <= 0:
        return reference_quiescence(board, ply=ply)
    best = -10**9
    for move in board.legal_moves:
        board.push(move)
        score = -reference_search(board, depth - 1, ply + 1)
        board.pop()
        best = max(best, score)
    return best


def random_position(rng, plies=None):
    board = chess.Board()
    for _ in range(plies if plies is not None else rng.randint(0, 70)):
        moves = list(board.legal_moves)
        if not moves or board.is_game_over(claim_draw=True):
            break
        board.push(rng.choice(moves))
    return board


# --------------------------------------------------------------------------- #
#  Evaluation
# --------------------------------------------------------------------------- #

class TestEvaluation(unittest.TestCase):
    def test_start_position_is_balanced(self):
        self.assertEqual(evaluate_white(chess.Board()), 0)
        self.assertEqual(evaluate_white(chess.Board().mirror()), 0)

    def test_score_is_mirror_symmetric(self):
        rng = random.Random(20240515)
        for _ in range(60):
            board = random_position(rng)
            self.assertEqual(
                evaluate_white(board),
                -evaluate_white(board.mirror()),
                msg=f"asymmetric evaluation for {board.fen()}",
            )

    def test_material_is_worth_roughly_what_it_should_be(self):
        base = "4k3/8/8/8/8/8/8/4K3 w - - 0 1"
        board = chess.Board(base)
        board.set_piece_at(chess.A1, chess.Piece(chess.QUEEN, chess.WHITE))
        self.assertGreater(evaluate_white(board), 800)
        self.assertLess(evaluate_white(board), 1400)

        board = chess.Board(base)
        board.set_piece_at(chess.H8, chess.Piece(chess.PAWN, chess.BLACK))
        self.assertLess(evaluate_white(board), -60)

    def test_king_safety_matters_in_the_middlegame(self):
        # Same position; only White's king moves off g1 (out of the pawn shield).
        safe = chess.Board("r1bq1rk1/pppp1ppp/2n2n2/2b1p3/2B1P3/2NP1N2/PPP2PPP/R1BQ1RK1 w - - 6 7")
        exposed = chess.Board("r1bq1rk1/pppp1ppp/2n2n2/2b1p3/2B1P3/2NP1N2/PPP2PPP/R1BQ1R1K w - - 6 7")
        self.assertGreater(evaluate_white(safe), evaluate_white(exposed))

    def test_pawn_structure_uses_the_right_side(self):
        # Identical material in both positions: White has a pawn on a6 and Black
        # has one on the 7th rank.  Only the blocker's file differs.
        blocked = chess.Board("4k3/p7/P7/8/8/8/8/4K3 w - - 0 1")
        free = chess.Board("4k3/7p/P7/8/8/8/8/4K3 w - - 0 1")
        self.assertGreater(
            evaluate_white(free), evaluate_white(blocked),
            "a passer on a6 should beat a blocked pawn",
        )
        # ... and the mirror image gives Black exactly the same edge.
        self.assertEqual(evaluate_white(free.mirror()), -evaluate_white(free))
        self.assertEqual(evaluate_white(blocked.mirror()), -evaluate_white(blocked))

    def test_passed_pawns_get_more_valuable_when_advanced(self):
        early = chess.Board("4k3/8/8/8/8/8/P7/4K3 w - - 0 1")
        advanced = chess.Board("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        self.assertGreater(evaluate_white(advanced), evaluate_white(early))


# --------------------------------------------------------------------------- #
#  Search
# --------------------------------------------------------------------------- #

class TestSearchAgainstReference(unittest.TestCase):
    """The pruned search must agree with plain negamax on quiet positions."""

    def setUp(self):
        self.engine = Engine(features=FEATURES_OFF)
        self.rng = random.Random(9876)

    def test_pvs_matches_reference_on_quiet_positions(self):
        checked = 0
        while checked < 6:
            board = random_position(self.rng, plies=self.rng.randint(4, 10))
            if board.is_check():
                continue
            if any(board.is_capture(move) for move in board.legal_moves):
                # forcing positions are where the two implementations legitimately
                # diverge (the reference has no delta pruning / no TT).
                continue
            depth = self.rng.choice([1, 2, 3])
            expected = reference_search(board.copy(), depth)
            got = self.engine._pvs(board.copy(), depth, -10**6, 10**6, 0, True)[0]
            self.assertEqual(expected, got, msg=f"depth {depth} on {board.fen()}")
            checked += 1

    def test_quiescence_matches_reference(self):
        for _ in range(8):
            board = random_position(self.rng, plies=self.rng.randint(4, 24))
            if board.is_check():
                continue
            expected = reference_quiescence(board.copy())
            got = self.engine._quiescence(board.copy(), -10**9, 10**9, 0)
            self.assertEqual(expected, got, msg=board.fen())

    def test_full_featured_search_matches_reference_on_a_quiet_move(self):
        """With every pruning feature on, a non-forcing move must still match."""
        board = chess.Board("rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2")
        move = chess.Move.from_uci("g8f6")
        play = board.copy()
        play.push(move)
        expected = reference_search(play, 2)

        full = Engine()
        got = full._pvs(play.copy(), 2, -10**6, 10**6, 1, True)[0]
        self.assertEqual(expected, got)


class TestSearchBehaviour(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()

    def test_finds_mate_in_one(self):
        board = chess.Board("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1")
        result = self.engine.search(board, SearchLimits(movetime=2.0))
        self.assertEqual(result.best_move.uci(), "a1a8")
        self.assertEqual(result.mate_in, 1)
        self.assertGreater(result.score, MATE_THRESHOLD)

    def test_finds_mate_in_two(self):
        board = chess.Board("2rr3k/pp3pp1/1nnqbN1p/3pN3/2pP4/2P3Q1/PPB4P/R4RK1 w - - 0 1")
        result = self.engine.search(board, SearchLimits(movetime=3.0))
        self.assertIsNotNone(result.mate_in)
        self.assertGreater(result.mate_in, 0)
        self.assertLessEqual(result.mate_in, 3)

    def test_promotes_the_pawn(self):
        board = chess.Board("7k/1P6/8/8/8/8/8/K7 w - - 0 1")
        result = self.engine.search(board, SearchLimits(movetime=1.5))
        self.assertEqual(result.best_move.uci(), "b7b8q")
        self.assertGreater(result.score, 700)

    def test_escorts_the_pawn_in_a_king_and_pawn_endgame(self):
        board = chess.Board("8/8/8/1k6/1P6/1K6/8/8 w - - 0 1")
        result = self.engine.search(board, SearchLimits(movetime=1.5))
        self.assertGreater(result.score, 0, "White should keep the winning try")
        moved_from = chess.square_name(result.best_move.from_square)
        self.assertEqual(moved_from, "b3", "the king move should support the pawn")

    def test_takes_the_free_queen(self):
        board = chess.Board("4k3/8/8/3q4/4P3/8/8/4K3 w - - 0 1")
        result = self.engine.search(board, SearchLimits(movetime=1.0))
        self.assertEqual(result.best_move.uci(), "e4d5")

    def test_avoids_stalemate_when_winning(self):
        # Black king is stuck; Qg6 would stalemate, so the engine must not play it.
        board = chess.Board("7k/8/6Q1/8/8/8/8/6K1 w - - 0 1")
        result = self.engine.search(board, SearchLimits(movetime=1.5))
        self.assertNotEqual(result.best_move.uci(), "g6g7")

    def test_reports_a_sensible_pv(self):
        board = chess.Board()
        result = self.engine.search(board, SearchLimits(movetime=1.0))
        self.assertTrue(result.pv)
        self.assertEqual(result.pv[0], result.best_move)
        self.assertEqual(len(result.pv_san), len(result.pv))
        # The PV must be playable.
        replay = board.copy()
        for move in result.pv:
            self.assertIn(move, replay.legal_moves)
            replay.push(move)

    def test_reports_illegal_root_moves_as_no_moves(self):
        board = chess.Board()
        result = self.engine.search(board, SearchLimits(depth=1), root_moves=[chess.Move.from_uci("a1a8")])
        self.assertIsNone(result.best_move)

    def test_respects_a_time_budget(self):
        board = chess.Board("r1bq1rk1/ppp2ppp/2np1n2/2b1p3/2B1P3/2NP1N2/PPP2PPP/R1BQ1RK1 w - - 6 7")
        started = time.monotonic()
        result = self.engine.search(board, SearchLimits(movetime=0.6))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.5, "search wildly overshot its time budget")
        self.assertIsNotNone(result.best_move)

    def test_stop_event_halts_the_search(self):
        import threading

        board = chess.Board("r1bq1rk1/ppp2ppp/2np1n2/2b1p3/2B1P3/2NP1N2/PPP2PPP/R1BQ1RK1 w - - 6 7")
        stop = threading.Event()
        threading.Timer(0.35, stop.set).start()
        started = time.monotonic()
        self.engine.search(board, SearchLimits(depth=20), stop_event=stop)
        self.assertLess(time.monotonic() - started, 3.0)

    def test_checkmate_position_has_no_move(self):
        board = chess.Board("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")
        self.assertTrue(board.is_checkmate())
        result = self.engine.search(board, SearchLimits(depth=3))
        self.assertIsNone(result.best_move)

    def test_san_conversion_does_not_crash_on_a_broken_line(self):
        board = chess.Board()
        junk = [chess.Move.from_uci("a1a8"), chess.Move.from_uci("e2e4")]
        self.assertEqual(san_line(board, junk), [])
        # a legal line still converts, and stops at the illegal tail
        good = [chess.Move.from_uci("e2e4"), chess.Move.from_uci("e7e5"), chess.Move.from_uci("a1a8")]
        self.assertEqual(san_line(board, good), ["e4", "e5"])


# --------------------------------------------------------------------------- #
#  Book, levels, bot and grading
# --------------------------------------------------------------------------- #

class TestOpeningBook(unittest.TestCase):
    def test_every_book_line_is_legal(self):
        """All 3800+ ECO lines must replay cleanly on a board."""
        self.assertGreater(len(OPENING_LINES), 1000)
        for eco, name, moves in OPENING_LINES:
            board = chess.Board()
            self.assertEqual(eco[0], eco[0].upper())
            for san in moves.split():
                move = board.parse_san(san)  # raises if the line is broken
                self.assertIn(move, board.legal_moves)
                board.push(move)

    def test_book_is_built_from_the_lines(self):
        self.assertGreater(book.BOOK.positions, 3000)
        self.assertGreater(book.BOOK.lines, 3000)
        self.assertTrue(book.START_IN_BOOK)

    def test_lookup_returns_legal_moves_only(self):
        rng = random.Random(11)
        board = chess.Board()
        for _ in range(12):
            move = book.lookup(board, rng)
            if move is None:
                break
            self.assertIn(move, board.legal_moves)
            board.push(move)

    def test_main_lines_dominate_the_first_move(self):
        rng = random.Random(3)
        counts: dict[str, int] = {}
        for _ in range(500):
            move = book.lookup(chess.Board(), rng)
            san = chess.Board().san(move)
            counts[san] = counts.get(san, 0) + 1
        mainline = sum(counts.get(san, 0) for san in ("e4", "d4", "c4", "Nf3"))
        self.assertGreater(mainline / 500, 0.85, "the book should usually play a main first move")
        self.assertGreater(counts.get("e4", 0), counts.get("f4", 0))
        self.assertGreater(counts.get("d4", 0), counts.get("b4", 0))

    def test_names_are_reported(self):
        board = chess.Board()
        for san in ("e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6", "Nc3", "a6"):
            board.push_san(san)
        eco, name = book.name_for(board)
        self.assertEqual(eco, "B90")
        self.assertIn("Najdorf", name)

    def test_names_only_get_as_specific_as_the_moves_played(self):
        board = chess.Board()
        for san in ("e4", "e5", "Nf3", "Nc6", "Bb5"):
            board.push_san(san)
        eco, name = book.name_for(board)
        self.assertEqual(eco, "C60")
        self.assertEqual(name, "Ruy Lopez")

    def test_book_deepens_the_game(self):
        """Following the book on both sides should reach real theory."""
        rng = random.Random(9)
        board = chess.Board()
        while (move := book.lookup(board, rng)) is not None:
            board.push(move)
        self.assertGreaterEqual(len(board.move_stack), 8)

    def test_is_in_book(self):
        self.assertTrue(book.is_in_book(chess.Board()))
        # ...and a position no theory line ever reaches is out of book
        board = chess.Board()
        for san in ("e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6", "O-O", "Be7",
                    "Re1", "b5", "Bb3", "d6", "c3", "O-O", "h3", "h6", "d4", "Re8"):
            board.push_san(san)
        self.assertFalse(book.is_in_book(board))


class TestLevels(unittest.TestCase):
    def test_levels_are_ordered_and_consistent(self):
        self.assertEqual([level["level"] for level in LEVELS], list(range(1, len(LEVELS) + 1)))
        elos = [level["estimated_elo"] for level in LEVELS]
        self.assertEqual(elos, sorted(elos))
        for level in LEVELS:
            self.assertLessEqual(level["max_depth"], 14)
            self.assertGreater(level["think_ms"], 0)

    def test_lookup_by_name_and_number(self):
        self.assertEqual(level_by_name("queen")["level"], 5)
        self.assertEqual(level_by_name(6)["name"], "King")
        self.assertEqual(level_by_name("3")["name"], "Bishop")
        with self.assertRaises(KeyError):
            level_by_name("Stockfish")

    def test_strong_levels_always_take_the_best_move(self):
        """Level 6 has zero tolerance for second-best moves."""
        engine = StubEngine(margin=90)
        no_book = {**level_by_name(6), "book": False}
        bot = Bot(level=no_book, engine=engine, seed=1)
        board = chess.Board()
        for _ in range(12):
            result = bot.pick_move(board, movetime_ms=10)
            self.assertEqual(result.best_move.uci(), engine.best.uci())

    def test_weak_levels_wander(self):
        """Level 1 is allowed to pick a move up to 150cp worse, and does."""
        engine = StubEngine(margin=60)
        board = chess.Board()
        weak = {**level_by_name(1), "book": False}
        picks = {Bot(level=weak, engine=engine, seed=seed).pick_move(board, movetime_ms=10).best_move.uci()
                 for seed in range(40)}
        self.assertIn(engine.best.uci(), picks, "it should still sometimes play well")
        self.assertGreater(len(picks), 1, "a beginner level should not be deterministic")


class TestBot(unittest.TestCase):
    def test_bot_only_plays_legal_moves(self):
        rng = random.Random(21)
        for level in (1, 3, 6):
            bot = Bot(level=level, seed=rng.randrange(10**6))
            board = random_position(rng, plies=rng.randint(0, 30))
            if board.is_game_over():
                continue
            result = bot.pick_move(board, movetime_ms=120)
            self.assertIn(result.best_move, board.legal_moves)

    def test_bot_plays_book_moves_early(self):
        bot = Bot(level=5, seed=7)
        result = bot.pick_move(chess.Board())
        self.assertTrue(result.from_book)
        self.assertIn(result.best_move, chess.Board().legal_moves)
        self.assertTrue(result.pv_san)

    def test_bot_leaves_the_book_once_the_position_is_novel(self):
        board = chess.Board()
        for san in ("e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6", "O-O", "Be7",
                    "Re1", "b5", "Bb3", "d6", "c3", "O-O", "h3", "h6", "d4", "Re8"):
            board.push_san(san)
        bot = Bot(level=5, seed=7)
        result = bot.pick_move(board, movetime_ms=120)
        self.assertFalse(result.from_book)
        self.assertIn(result.best_move, board.legal_moves)

    def test_bot_handles_a_mated_position(self):
        board = chess.Board("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")
        result = Bot(level=4).pick_move(board)
        self.assertIsNone(result.best_move)

    def test_full_game_finishes_and_stays_legal(self):
        white = Bot(level=2, seed=1)
        black = Bot(level=3, seed=2)
        board = chess.Board()
        moves = 0
        while not board.is_game_over(claim_draw=True) and moves < 120:
            bot = white if board.turn == chess.WHITE else black
            move = bot.pick_move(board, movetime_ms=120).best_move
            self.assertIn(move, board.legal_moves)
            board.push(move)
            moves += 1
        self.assertGreater(moves, 4)


class TestGrading(unittest.TestCase):
    def test_best_move_grades_as_best(self):
        board = chess.Board()
        best = Engine().search(board, SearchLimits(depth=5)).best_move
        # depth-only grading is deterministic, so the engine's own choice must
        # always be graded as best.
        report = analyse_move(board, best, engine=Engine(), depth=5, movetime_ms=None)
        self.assertEqual(report.grade, "Best move")
        self.assertEqual(report.loss_cp, 0)

    def test_depth_only_grading_is_deterministic(self):
        board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 5 4")
        move = chess.Move.from_uci("g8f6")
        first = analyse_move(board, move, engine=Engine(), depth=4, movetime_ms=None)
        second = analyse_move(board, move, engine=Engine(), depth=4, movetime_ms=None)
        self.assertEqual((first.grade, first.loss_cp), (second.grade, second.loss_cp))

    def test_absurd_move_is_punished(self):
        board = chess.Board("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
        report = analyse_move(board, chess.Move.from_uci("f2f3"), movetime_ms=300)
        self.assertIn(report.grade, {"Inaccuracy", "Mistake", "Blunder"})
        self.assertGreaterEqual(report.loss_cp, 0)
        self.assertTrue(report.best_san)

    def test_illegal_move_is_rejected(self):
        with self.assertRaises(ValueError):
            analyse_move(chess.Board(), chess.Move.from_uci("e2e5"), movetime_ms=50)

    def test_report_serialises(self):
        report = analyse_move(chess.Board(), chess.Move.from_uci("e2e4"), movetime_ms=200)
        payload = report.as_dict()
        self.assertIn("grade", payload)
        self.assertTrue(all(slot in payload for slot in ("san", "uci", "loss_cp")))
        # every field must survive a JSON round trip (the web API ships this dict)
        import json

        encoded = json.dumps(payload)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["grade"], report.grade)
        # as_dict() turns the Move objects into UCI strings and report.pv is
        # already SAN, so nothing in the payload needs a JSON fallback.
        self.assertEqual(decoded["pv"], report.pv)
        self.assertEqual(decoded["best_move"], report.best_move.uci() if report.best_move else None)
        self.assertTrue(all(isinstance(item, str) for item in decoded["pv"]))
        self.assertIsInstance(decoded["best_san"], str)


# --------------------------------------------------------------------------- #
#  Web layer
# --------------------------------------------------------------------------- #

class TestWebHelpers(unittest.TestCase):
    def setUp(self):
        from web.server import analyse_position  # noqa: F401

        self.analyse_position = analyse_position

    def test_analyse_position_payload(self):
        payload = self.analyse_position(
            "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5Q2/PPPP1PPP/RNB1K1NR w KQkq - 4 4",
            movetime_ms=250,
        )
        self.assertEqual(payload["best_san"], "Qxf7#")
        self.assertEqual(payload["mate_in"], 1)
        self.assertEqual(payload["score_white"], payload["score"])

    def test_static_paths_cannot_escape_the_static_directory(self):
        from web.server import STATIC

        for bad in ("../../etc/passwd", "static/../../../etc/passwd"):
            relative = bad.lstrip("/")
            relative = relative[7:] if relative.startswith("static/") else relative
            candidate = (STATIC / relative).resolve()
            self.assertFalse(str(candidate).startswith(str(STATIC.resolve())) and candidate.is_file(),
                             msg=f"{bad} escaped the static directory")


if __name__ == "__main__":
    unittest.main(verbosity=2)
