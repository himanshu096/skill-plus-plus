"""Unit tests for tests/evals/locator.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from locator import parse


class ParseEmptyInput(unittest.TestCase):
    def test_empty_string_returns_empty_dict(self):
        self.assertEqual(parse(""), {})


if __name__ == "__main__":
    unittest.main()
