"""Tests for sandbox.calc.parse_amount.

Red at seed time: ``parse_amount`` does not exist yet. Task 2 makes these
pass. These tests are part of the seed commit, visible to the coder — the
task is exactly "make this failing suite green by implementing the function
the tests describe".
"""

import unittest

from sandbox.calc import parse_amount


class ParseAmountTest(unittest.TestCase):
    def test_plain_decimal(self):
        self.assertEqual(str(parse_amount("18.4")), "18.40")

    def test_integer_string(self):
        self.assertEqual(str(parse_amount("18")), "18.00")

    def test_leading_and_trailing_whitespace_is_stripped(self):
        self.assertEqual(str(parse_amount("  3.5  ")), "3.50")

    def test_more_than_two_decimal_places_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_amount("18.401")

    def test_empty_string_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_amount("")

    def test_non_numeric_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_amount("abc")

    def test_negative_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_amount("-5")


if __name__ == "__main__":
    unittest.main()
