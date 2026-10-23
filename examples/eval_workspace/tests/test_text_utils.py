from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from text_utils import slugify


class SlugifyTests(unittest.TestCase):
    def test_normalizes_words_and_punctuation(self):
        self.assertEqual(slugify("  Hello, Forge Agent!  "), "hello-forge-agent")
        self.assertEqual(slugify("Already---Split"), "already-split")
        self.assertEqual(slugify("___"), "")


if __name__ == "__main__":
    unittest.main()
