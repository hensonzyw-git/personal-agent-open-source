"""Regression tests for sandbox.amounts.round_cny_cent.

Red at seed time: ``round_cny_cent`` rejects amounts with more than two
decimal places only when the fractional part has fewer than two digits
(off-by-one guard), so ``"18.401"`` returns ``"18.40"`` instead of raising.
Task 4 fixes the defect and keeps every test here green. The regression case
for the defect is ``test_more_than_two_decimal_places_is_rejected``.
"""

import unittest

from sandbox.amounts import round_cny_cent


class RoundCnyCentTest(unittest.TestCase):
    def test_plain_decimal(self):
        self.assertEqual(round_cny_cent("18.4"), "18.40")

    def test_integer_string(self):
        self.assertEqual(round_cny_cent("18"), "18.00")

    def test_two_decimal_places_is_accepted(self):
        self.assertEqual(round_cny_cent("18.40"), "18.40")

    def test_more_than_two_decimal_places_is_rejected(self):
        with self.assertRaises(ValueError):
            round_cny_cent("18.401")

    def test_empty_string_is_rejected(self):
        with self.assertRaises(ValueError):
            round_cny_cent("")

    def test_non_numeric_is_rejected(self):
        with self.assertRaises(ValueError):
            round_cny_cent("abc")

    def test_negative_is_rejected(self):
        with self.assertRaises(ValueError):
            round_cny_cent("-5")


if __name__ == "__main__":
    unittest.main()
