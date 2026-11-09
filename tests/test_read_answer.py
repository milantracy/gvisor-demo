"""read_answer: escape sequences ahead of the typed key are dropped."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gvdemo import ui  # noqa: E402


def answer(line):
    with mock.patch.object(ui.console, "input", return_value=line):
        return ui.read_answer("")


class ReadAnswerTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(answer("r"), "r")
        self.assertEqual(answer(" B "), "b")
        self.assertEqual(answer(""), "")

    def test_sgr_mouse_reports(self):
        self.assertEqual(answer("\x1b[<35;10;5M\x1b[<35;11;5Mr"), "r")
        self.assertEqual(answer("\x1b[<0;1;1m q"), "q")

    def test_terminal_answers(self):
        self.assertEqual(answer("\x1bP1$r0m\x1b\\r"), "r")  # DECRQSS reply
        self.assertEqual(answer("\x1b]11;rgb:0/0/0\x07b"), "b")  # OSC, BEL

    def test_junk_only_is_enter(self):
        self.assertEqual(answer("\x1b[<35;10;5M "), "")

    def test_end_of_input_is_quit(self):
        with mock.patch.object(ui.console, "input", side_effect=EOFError):
            self.assertEqual(ui.read_answer(""), "q")


if __name__ == "__main__":
    unittest.main()
