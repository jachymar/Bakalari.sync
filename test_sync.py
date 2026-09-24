import unittest

import sync


class SpecialEventGroupingTests(unittest.TestCase):
    def test_group_special_event_ignores_school_breaks(self):
        atoms = [
            ({"Name": "Exkurze", "Description": "Exkurze", "SubjectId": 1, "RoomId": 5}, "Exkurze", ("08:00", "08:45")),
            ({"Name": "Exkurze", "Description": "Exkurze", "SubjectId": 1, "RoomId": 5}, "Exkurze", ("08:50", "09:35")),
            ({"Name": "Exkurze", "Description": "Exkurze", "SubjectId": 1, "RoomId": 5}, "Exkurze", ("09:40", "10:25")),
        ]

        group, next_index = sync.group_special_event_atoms(atoms, 0)

        self.assertEqual(next_index, 3)
        self.assertEqual(len(group), 3)
        self.assertEqual(group[-1][2][1], "10:25")

    def test_special_events_are_green_and_cancellations_skip_day(self):
        self.assertEqual(sync.event_color_for("", "Exkurze"), "2")
        self.assertEqual(sync.event_color_for("Suplování", ""), "6")
        self.assertTrue(sync.is_day_cancelled({"IsHoliday": True}))
        self.assertTrue(sync.is_day_cancelled({"Name": "Svátek"}))
        self.assertFalse(sync.is_day_cancelled({"Name": "Normální den"}))


if __name__ == "__main__":
    unittest.main()
