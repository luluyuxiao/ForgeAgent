from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from calculator import total_price


class TotalPriceTests(unittest.TestCase):
    def test_multiplies_before_rounding(self):
        self.assertEqual(total_price(12.345, 3), 37.04)
        self.assertEqual(total_price(4.0, 0), 0.0)

    def test_rejects_negative_quantity(self):
        with self.assertRaises(ValueError):
            total_price(4.0, -1)


if __name__ == "__main__":
    unittest.main()
