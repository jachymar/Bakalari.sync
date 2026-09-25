import unittest
from unittest import mock

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
        base_zone = sync.ZoneInfo("Europe/Prague")
        today = sync.datetime.now(base_zone)
        today_str = today.strftime("%Y-%m-%d")
        lesson_starts = {
            "Days": [
                {
                    "Date": today_str,
                    "Atoms": [
                        {"HourId": 1, "Date": today_str, "StartTime": "08:00", "EndTime": "08:45"},
                        {"HourId": 2, "Date": today_str, "StartTime": "09:00", "EndTime": "09:45"},
                    ],
                }
            ]
        }
        self.assertTrue(sync.should_sync_now(lesson_starts, today.replace(hour=8, minute=20, second=0, microsecond=0).isoformat()))
        self.assertTrue(sync.should_sync_now(lesson_starts, today.replace(hour=8, minute=25, second=0, microsecond=0).isoformat()))
        self.assertFalse(sync.should_sync_now(lesson_starts, today.replace(hour=8, minute=0, second=0, microsecond=0).isoformat()))
        self.assertFalse(sync.should_sync_now(lesson_starts, today.replace(hour=8, minute=15, second=0, microsecond=0).isoformat()))

    def test_event_id_is_stable_when_summary_changes(self):
        today = sync.datetime.now(sync.ZoneInfo("Europe/Prague")).strftime("%Y-%m-%d")
        subject = "Matematika"
        first_event_id = sync._event_id(today, "08:00", "Matematika (A101)")
        second_event_id = sync._event_id(today, "08:00", "Matematika (A102)")
        self.assertEqual(first_event_id, second_event_id)
        self.assertEqual(first_event_id, sync._event_id(today, "08:00", subject))

    def test_event_title_contains_room_and_location_contains_class_name(self):
        atom = {
            "ClassId": 7,
            "Class": {"Name": "3.A"},
            "RoomId": 5,
        }
        room_mapping = {"c 101": "101"}
        classes = {7: "3.A"}

        self.assertEqual(sync.extract_class_name(atom, classes), "3.A")
        self.assertEqual(sync.build_event_summary("Matematika", "101"), "Matematika (101)")
        self.assertEqual(sync.build_event_location("3.A", "101"), "3.A")

    def test_event_description_omits_room_name_from_note(self):
        description = sync.build_event_description(
            teacher_name="Mgr. Nováková",
            change_info="",
            absence_status=None,
            room_name="Učebna 1D",
            room_number="101",
        )

        self.assertIn("Mgr. Nováková", description)
        self.assertNotIn("Vyučuje:", description)
        self.assertNotIn("Učebna:", description)

    def test_extract_teacher_name_prefers_teacher_data(self):
        atom = {
            "Teacher": {"Name": "Mgr. Nováková"},
            "RoomId": 5,
        }
        self.assertEqual(sync.extract_teacher_name(atom), "Mgr. Nováková")
        self.assertEqual(sync.extract_teacher_name({"Teacher": "Pavel Horák"}), "Pavel Horák")
        self.assertIsNone(sync.extract_teacher_name({}))

    def test_execute_with_backoff_retries_rate_limits(self):
        attempts = {"count": 0}

        class FakeRequest:
            def execute(self):
                attempts["count"] += 1
                if attempts["count"] < 3:
                    raise sync.HttpError(
                        type("Resp", (), {"status": 429, "reason": "Too Many Requests"})(),
                        b'{"error": {"errors": [{"reason": "rateLimitExceeded"}]}}',
                    )
                return {"ok": True}

        with mock.patch.object(sync.time, "sleep") as sleep_mock:
            result = sync.execute_with_backoff(FakeRequest(), attempts=4)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(attempts["count"], 3)
        self.assertEqual(sleep_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
