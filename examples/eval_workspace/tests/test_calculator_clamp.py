from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from calculator import clamp


class ClampTests(unittest.TestCase):
    def test_clamps_below_inside_and_above_interval(self):
        self.assertEqual(clamp(-2, 0, 10), 0)
        self.assertEqual(clamp(4, 0, 10), 4)
        self.assertEqual(clamp(20, 0, 10), 10)

    def test_rejects_an_invalid_interval(self):
        with self.assertRaises(ValueError):
            clamp(4, 10, 0)


if __name__ == "__main__":
    unittest.main()
