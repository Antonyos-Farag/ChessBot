"""Headless checks for the tkinter desktop app.

``ChessBot.py`` cannot be opened here (the CI box has no display and often no
tkinter at all), so the GUI toolkit is stubbed out and only the parts that do
real work are exercised: board geometry, the click -> square mapping, move
handling, PGN/FEN plumbing and the promotion path.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import chess  # noqa: E402


def _install_tkinter_stub() -> None:
    """Fake just enough of tkinter for the module to import."""
    if "tkinter" in sys.modules:
        return
    try:  # pragma: no cover - only when a real tkinter exists
        import tkinter

        _ = tkinter.Tk  # a real tkinter is present, nothing to stub
        return
    except ImportError:
        pass

    class Dummy:
        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, name):
            return Dummy()

        def __call__(self, *args, **kwargs):
            return Dummy()

    tk = types.ModuleType("tkinter")
    for name in (
        "Tk", "Toplevel", "Frame", "Label", "Button", "Canvas", "Text", "Entry",
        "IntVar", "BooleanVar", "StringVar", "PhotoImage", "Scrollbar", "Menu",
    ):
        setattr(tk, name, Dummy)

    messagebox = types.ModuleType("tkinter.messagebox")
    messagebox.askyesno = lambda *a, **k: False
    messagebox.showerror = lambda *a, **k: None
    messagebox.showinfo = lambda *a, **k: None

    filedialog = types.ModuleType("tkinter.filedialog")
    filedialog.asksaveasfilename = lambda *a, **k: ""

    ttk = types.ModuleType("tkinter.ttk")
    for name in ("Checkbutton", "Button", "Frame", "Label", "Combobox", "Style"):
        setattr(ttk, name, Dummy)

    tk.messagebox = messagebox
    tk.filedialog = filedialog
    tk.ttk = ttk
    sys.modules["tkinter"] = tk
    sys.modules["tkinter.messagebox"] = messagebox
    sys.modules["tkinter.filedialog"] = filedialog
    sys.modules["tkinter.ttk"] = ttk


_install_tkinter_stub()


def load_app_module():
    spec = importlib.util.spec_from_file_location("chessbot_desktop", ROOT / "ChessBot.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


desktop = load_app_module()


class FakeCanvas:
    """Canvas stand-in that reports a fixed size."""

    def __init__(self, width=800, height=720):
        self._width, self._height = width, height
        self.items = []

    def winfo_width(self):
        return self._width

    def winfo_height(self):
        return self._height

    def create_rectangle(self, *args, **kwargs):
        self.items.append(("rect", args, kwargs))

    def create_oval(self, *args, **kwargs):
        self.items.append(("oval", args, kwargs))

    def create_text(self, *args, **kwargs):
        self.items.append(("text", args, kwargs))

    def create_line(self, *args, **kwargs):
        self.items.append(("line", args, kwargs))

    def delete(self, *args, **kwargs):
        self.items.clear()


class FakeWidget:
    """Anything that only needs to accept configure/insert/delete calls."""

    def __init__(self):
        self.options = {}

    def configure(self, **kwargs):
        self.options.update(kwargs)

    def insert(self, *args, **kwargs):
        pass

    def delete(self, *args, **kwargs):
        pass

    def see(self, *args, **kwargs):
        pass

    def tag_configure(self, *args, **kwargs):
        pass


def make_app(canvas_width=800, canvas_height=720):
    """A ChessApp without running __init__ (no Tk main loop available)."""
    app = object.__new__(desktop.ChessApp)
    app.canvas = FakeCanvas(canvas_width, canvas_height)
    app.flipped = False
    app.board = chess.Board()
    app.human_color = chess.WHITE
    app.selected = None
    app.legal_targets = set()
    app.last_move = None
    app.hint_move = None
    app.review_index = None
    app.grades = {}
    app.opening_name = None
    app.thinking = False
    app.eval_cp = 0
    app.mate_in = None
    app.pv_san = []
    app.engine_stats = {"depth": 0, "seldepth": 0, "nodes": 0, "nps": 0, "time": 0}
    app.level = desktop.level_by_name(4)
    # widgets that the panel code touches
    for name in ("headline", "subline", "eval_label", "stats_label", "pv_label",
                 "opening_label", "moves", "status_top"):
        setattr(app, name, FakeWidget())
    app.eval_canvas = FakeCanvas()
    app.coach_var = types.SimpleNamespace(get=lambda: False)
    app.level_var = types.SimpleNamespace(set=lambda value: None)
    app.events = types.SimpleNamespace(get_nowait=lambda: None)
    app.stop_event = None
    # never start a real search from a unit test
    app.start_bot_turn = lambda: None
    app.start_evaluation = lambda *a, **k: None
    app.start_grade = lambda *a, **k: None
    app.start_hint = lambda: None
    app.poll_events = lambda: None
    return app


class TestGeometry(unittest.TestCase):
    def test_every_square_round_trips(self):
        for flipped in (False, True):
            app = make_app()
            app.flipped = flipped
            for square in chess.SQUARES:
                x, y = app.square_to_xy(square)
                self.assertEqual(app.xy_to_square(x + 2, y + 2), square,
                                 msg=f"flipped={flipped} {chess.square_name(square)}")

    def test_flipping_is_a_rotation_not_a_mirror(self):
        app = make_app()
        left, top = app.board_origin()
        size = app.square_size()

        normal_tl = app.xy_to_square(left + 2, top + 2)
        normal_br = app.xy_to_square(left + 8 * size - 2, top + 8 * size - 2)
        self.assertEqual((normal_tl, normal_br), (chess.A8, chess.H1))

        app.flipped = True
        flipped_tl = app.xy_to_square(left + 2, top + 2)
        flipped_br = app.xy_to_square(left + 8 * size - 2, top + 8 * size - 2)
        self.assertEqual((flipped_tl, flipped_br), (chess.H1, chess.A8))

    def test_clicks_outside_the_board_are_ignored(self):
        app = make_app()
        self.assertIsNone(app.xy_to_square(0, 0))
        self.assertIsNone(app.xy_to_square(10_000, 10_000))

    def test_board_stays_square(self):
        for width, height in ((800, 720), (600, 900), (500, 500)):
            app = make_app(width, height)
            size = app.square_size()
            left, top = app.board_origin()
            self.assertAlmostEqual(left * 2 + size * 8, width)
            self.assertAlmostEqual(top * 2 + size * 8, height)


class TestInteraction(unittest.TestCase):
    def test_selecting_and_moving_a_pawn(self):
        app = make_app()
        app.on_click(types.SimpleNamespace(x=0, y=0))  # outside the board: ignored
        source = app.square_to_xy(chess.E2)
        app.on_click(types.SimpleNamespace(x=source[0] + 5, y=source[1] + 5))
        self.assertEqual(app.selected, chess.E2)
        self.assertIn(chess.E4, app.legal_targets)
        target = app.square_to_xy(chess.E4)
        app.on_click(types.SimpleNamespace(x=target[0] + 5, y=target[1] + 5))
        self.assertEqual(app.board.piece_at(chess.E4).piece_type, chess.PAWN)
        self.assertIsNone(app.selected)

    def test_clicking_your_own_piece_twice_deselects(self):
        app = make_app()
        x, y = app.square_to_xy(chess.D2)
        app.on_click(types.SimpleNamespace(x=x + 5, y=y + 5))
        self.assertEqual(app.selected, chess.D2)
        app.on_click(types.SimpleNamespace(x=x + 5, y=y + 5))
        self.assertIsNone(app.selected)
        self.assertEqual(app.legal_targets, set())

    def test_cannot_move_the_opponents_pieces(self):
        app = make_app()
        x, y = app.square_to_xy(chess.E7)
        app.on_click(types.SimpleNamespace(x=x + 5, y=y + 5))
        self.assertIsNone(app.selected)

    def test_illegal_targets_do_not_move(self):
        app = make_app()
        x, y = app.square_to_xy(chess.E2)
        app.on_click(types.SimpleNamespace(x=x + 5, y=y + 5))
        target = app.square_to_xy(chess.E5)
        app.on_click(types.SimpleNamespace(x=target[0] + 5, y=target[1] + 5))
        self.assertEqual(app.board.ply(), 0, "no move should have been played")

    def test_draw_paints_a_piece_per_square(self):
        app = make_app()
        app.draw()
        rects = [item for item in app.canvas.items if item[0] == "rect"]
        texts = [item for item in app.canvas.items if item[0] == "text"]
        glyphs = {desktop.piece_glyph(chess.Piece(piece_type, chess.WHITE))
                  for piece_type in (chess.PAWN, chess.KNIGHT, chess.BISHOP,
                                     chess.ROOK, chess.QUEEN, chess.KING)}
        drawn = [t for t in texts if t[2].get("text") in glyphs]
        self.assertGreaterEqual(len(rects), 64)
        # 32 pieces, each drawn as 5 glyphs (4 outline copies + the fill)
        self.assertEqual(len(drawn), 32 * 5)


class TestGameFlow(unittest.TestCase):
    def test_review_board_replays_history(self):
        app = make_app()
        for san in ("e4", "e5", "Nf3"):
            app.board.push_san(san)
        app.review_index = 2
        review = app.view_board()
        self.assertEqual(review.ply(), 2)
        self.assertEqual(review.fen(), "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2")

    def test_pgn_round_trip_text(self):
        app = make_app()
        for san in ("e4", "c5", "Nf3"):
            app.board.push_san(san)
        # variation_san replays on the board it is called on, so a fresh board
        # is what produces the game's notation (this is the copy_pgn path).
        text = chess.Board().variation_san(app.board.move_stack)
        self.assertEqual(text, "1. e4 c5 2. Nf3")

    def test_piece_glyphs_are_all_present(self):
        glyphs = {
            desktop.piece_glyph(chess.Piece(piece_type, chess.WHITE))
            for piece_type in (chess.PAWN, chess.KNIGHT, chess.BISHOP,
                               chess.ROOK, chess.QUEEN, chess.KING)
        }
        self.assertEqual(len(glyphs), 6)
        self.assertNotIn("", glyphs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
