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
        self.assertEqual(sync.event_color_for("Suplování", ""), None)
        self.assertTrue(sync.is_day_cancelled({"IsHoliday": True}))
        self.assertTrue(sync.is_day_cancelled({"Name": "Svátek"}))
        self.assertFalse(sync.is_day_cancelled({"Name": "Normální den"}))

    def test_next_week_uses_permanent_calendar(self):
        future_date = (sync.datetime.now(sync.ZoneInfo("Europe/Prague")) + sync.timedelta(days=8)).strftime("%Y-%m-%d")
        self.assertEqual(
            sync.select_calendar_id(future_date, "current-calendar", "permanent-calendar"),
            "permanent-calendar",
        )
        today_date = sync.datetime.now(sync.ZoneInfo("Europe/Prague")).strftime("%Y-%m-%d")
        self.assertEqual(
            sync.select_calendar_id(today_date, "current-calendar", "permanent-calendar"),
            "current-calendar",
        )

    def test_should_sync_20_minutes_after_lesson_starts(self):
        today = sync.datetime.now(sync.ZoneInfo("Europe/Prague")).strftime("%Y-%m-%d")
        lesson_starts = {
            "Days": [
                {
                    "Date": f"{today}",
                    "Atoms": [
                        {"HourId": 1, "Date": f"{today}", "StartTime": "08:00", "EndTime": "08:45"},
                        {"HourId": 2, "Date": f"{today}", "StartTime": "09:00", "EndTime": "09:45"},
                    ],
                }
            ]
        }
        self.assertTrue(sync.should_sync_now(lesson_starts, f"{today}T08:20:00+01:00"))
        self.assertFalse(sync.should_sync_now(lesson_starts, f"{today}T08:00:00+01:00"))
        self.assertFalse(sync.should_sync_now(lesson_starts, f"{today}T09:10:00+01:00"))


if __name__ == "__main__":
    unittest.main()
