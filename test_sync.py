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
        self.assertEqual(sync.event_color_for("Suplování", ""), "8")
        self.assertEqual(sync.event_color_for("", "", "omluveno"), "4")
        self.assertEqual(sync.event_color_for("", "", "skolni"), "7")
        self.assertTrue(sync.is_day_cancelled({"IsHoliday": True}))
        self.assertTrue(sync.is_day_cancelled({"Name": "Svátek"}))
        self.assertFalse(sync.is_day_cancelled({"Name": "Normální den"}))

    def test_next_week_uses_permanent_calendar(self):
        reference_date = sync.datetime.strptime("2026-09-30", "%Y-%m-%d").date()
        self.assertEqual(
            sync.select_calendar_id("2026-10-05", "current-calendar", "permanent-calendar", reference_date),
            "permanent-calendar",
        )
        self.assertEqual(
            sync.select_calendar_id("2026-10-04", "current-calendar", "permanent-calendar", reference_date),
            "current-calendar",
        )

    def test_determine_target_calendar_switches_between_main_and_archive(self):
        today = sync.datetime.strptime("2026-10-15", "%Y-%m-%d").date() # Thursday
        main_start = sync.datetime.strptime("2026-10-05", "%Y-%m-%d").date() # Previous week monday
        main_end = sync.datetime.strptime("2026-10-26", "%Y-%m-%d").date()
        
        self.assertEqual(
            sync.determine_target_calendar("2026-10-15", "current", "perm", "archive", today, main_start, main_end),
            "current"
        )
        self.assertEqual(
            sync.determine_target_calendar("2026-09-30", "current", "perm", "archive", today, main_start, main_end),
            "archive"
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

    def test_event_id_is_stable_when_summary_or_subject_changes(self):
        today = sync.datetime.now(sync.ZoneInfo("Europe/Prague")).strftime("%Y-%m-%d")
        first_event_id = sync._event_id(today, "08:00", "Matematika (A101)")
        second_event_id = sync._event_id(today, "08:00", "Český jazyk (A102)")
        self.assertEqual(first_event_id, second_event_id)

    def test_is_event_unchanged_detects_all_differences(self):
        existing = {
            "summary": "Matematika",
            "description": "Vyučuje: Novák",
            "location": "A1",
            "colorId": "8",
            "start": {"dateTime": "2026-11-06T08:00:00+01:00"},
            "end": {"dateTime": "2026-11-06T08:45:00+01:00"}
        }
        new_ev = existing.copy()
        
        self.assertTrue(sync.is_event_unchanged(existing, new_ev))
        
        # Change color
        new_ev["colorId"] = "4"
        self.assertFalse(sync.is_event_unchanged(existing, new_ev))
        
        # Revert color, change time
        new_ev["colorId"] = "8"
        new_ev["start"] = {"dateTime": "2026-11-06T08:05:00+01:00"}
        self.assertFalse(sync.is_event_unchanged(existing, new_ev))

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
