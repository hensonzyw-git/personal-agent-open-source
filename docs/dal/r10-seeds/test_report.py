"""Behaviour-lock tests for sandbox.report.

Green at seed time and must stay green: task 3 extracts the duplicated
formatting logic in ``report.py`` into one shared function. These tests pin
the observable output of both report paths so the refactor cannot change
behaviour. The coder may read this file but must not modify it (allowed_paths
is locked to ``sandbox/report.py`` for task 3).
"""

import io
import unittest
from contextlib import redirect_stdout

from sandbox import report


class ReportBehaviourLockTest(unittest.TestCase):
    def _render(self, func, rows):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            func(rows)
        return buffer.getvalue()

    def test_expense_report_totals_and_lines(self):
        out = self._render(
            report.print_expense_report,
            [("coffee", "18.40"), ("book", "52.00")],
        )
        self.assertIn("coffee: 18.40", out)
        self.assertIn("book: 52.00", out)
        self.assertIn("TOTAL: 70.40", out)

    def test_expense_report_empty_rows_still_prints_total(self):
        out = self._render(report.print_expense_report, [])
        self.assertIn("TOTAL: 0.00", out)

    def test_income_report_totals_and_lines(self):
        out = self._render(
            report.print_income_report,
            [("salary", "12000.00"), ("refund", "35.5")],
        )
        self.assertIn("salary: 12000.00", out)
        self.assertIn("refund: 35.50", out)
        self.assertIn("TOTAL: 12035.50", out)

    def test_income_report_empty_rows_still_prints_total(self):
        out = self._render(report.print_income_report, [])
        self.assertIn("TOTAL: 0.00", out)

    def test_total_format_is_two_decimal_places(self):
        out = self._render(
            report.print_expense_report, [("x", "0.005")]
        )
        # 0.005 rounds to 0.00 under the seeded formatting; the lock is that
        # the format is exactly two decimals, not the rounding rule.
        self.assertRegex(out, r"TOTAL: \d+\.\d{2}\n")


if __name__ == "__main__":
    unittest.main()
