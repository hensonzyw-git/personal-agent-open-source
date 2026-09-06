"""Compatibility-lock tests for sandbox.record.ExpenseRecord.

Green at seed time for the three existing-shape cases; the trip_tag cases
are red until task 5 adds the field. Task 5 adds the optional field
``trip_tag`` without breaking any existing three-field construction, and
these tests pin both the old shape and the new shape.
"""

import unittest

from sandbox.record import ExpenseRecord


class ExpenseRecordCompatibilityTest(unittest.TestCase):
    def test_existing_three_field_construction_still_works(self):
        record = ExpenseRecord("r-0001", "coffee", "18.40")
        self.assertEqual(record.record_id, "r-0001")
        self.assertEqual(record.name, "coffee")
        self.assertEqual(record.amount_cny, "18.40")

    def test_keyword_construction_still_works(self):
        record = ExpenseRecord(record_id="r-0002", name="book", amount_cny="52.00")
        self.assertEqual(record.name, "book")

    def test_trip_tag_defaults_to_none(self):
        record = ExpenseRecord("r-0003", "taxi", "18.40")
        self.assertIsNone(record.trip_tag)

    def test_trip_tag_can_be_set(self):
        record = ExpenseRecord("r-0004", "taxi", "18.40", trip_tag="dalian")
        self.assertEqual(record.trip_tag, "dalian")

    def test_asdict_contains_new_key_with_none_default(self):
        from dataclasses import asdict

        data = asdict(ExpenseRecord("r-0005", "taxi", "18.40"))
        self.assertEqual(
            data,
            {
                "record_id": "r-0005",
                "name": "taxi",
                "amount_cny": "18.40",
                "trip_tag": None,
            },
        )


if __name__ == "__main__":
    unittest.main()
