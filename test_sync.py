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

    def test_cancelled_atom_detection_handles_bakalari_change_types(self):
        self.assertTrue(
            sync._is_cancelled(
                {"SubjectId": None, "Change": {"ChangeType": "Canceled", "Description": "Vyjmuto z rozvrhu"}},
                "Hodina",
            )
        )
        self.assertTrue(
            sync._is_cancelled(
                {"SubjectId": None, "Change": {"ChangeType": "Removed", "TypeAbbrev": "Zruš"}},
                "Hodina",
            )
        )
        self.assertFalse(
            sync._is_cancelled(
                {"SubjectId": 5, "Change": {"ChangeType": "RoomChanged", "Description": "Přesun do učebny 102"}},
                "Matematika",
            )
        )

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

    def test_week_window_rolls_over_on_monday(self):
        thursday = sync.datetime.strptime("2026-10-01", "%Y-%m-%d").date()
        friday = sync.datetime.strptime("2026-10-02", "%Y-%m-%d").date()
        sunday = sync.datetime.strptime("2026-10-04", "%Y-%m-%d").date()
        monday = sync.datetime.strptime("2026-10-05", "%Y-%m-%d").date()
        next_friday = sync.datetime.strptime("2026-10-09", "%Y-%m-%d").date()

        # Pondělí až čtvrtek: generuje se aktuální + příští týden (14 dní)
        self.assertEqual(
            sync.week_window(thursday),
            (
                sync.datetime.strptime("2026-09-28", "%Y-%m-%d").date(),
                sync.datetime.strptime("2026-10-12", "%Y-%m-%d").date(),
            ),
        )
        self.assertEqual(
            sync.week_window(monday),
            (
                monday,
                sync.datetime.strptime("2026-10-19", "%Y-%m-%d").date(),
            ),
        )

        # Pátek až neděle: generuje se aktuální + 2 týdny dopředu (21 dní)
        self.assertEqual(
            sync.week_window(friday),
            (
                sync.datetime.strptime("2026-09-28", "%Y-%m-%d").date(),
                sync.datetime.strptime("2026-10-19", "%Y-%m-%d").date(),
            ),
        )
        self.assertEqual(
            sync.week_window(sunday),
            (
                sync.datetime.strptime("2026-09-28", "%Y-%m-%d").date(),
                sync.datetime.strptime("2026-10-19", "%Y-%m-%d").date(),
            ),
        )

        # Mazání předchozího týdne: od pondělí do čtvrtka zůstává minulý týden, smaže se až v pátek
        self.assertEqual(
            sync.cleanup_cutoff_date(monday),
            sync.datetime.strptime("2026-09-28", "%Y-%m-%d").date(),
        )
        self.assertEqual(
            sync.cleanup_cutoff_date(next_friday),
            sync.datetime.strptime("2026-10-05", "%Y-%m-%d").date(),
        )

    def test_expired_event_cleanup_only_deletes_managed_events(self):
        class FakeRequest:
            def __init__(self, result):
                self.result = result

            def execute(self):
                return self.result

        class FakeEvents:
            def __init__(self):
                self.deleted = []

            def list(self, **kwargs):
                return FakeRequest({
                    "items": [
                        {"id": "0123456789abcdef0123456789abcdef"},
                        {"id": "personal-event"},
                    ]
                })

            def delete(self, **kwargs):
                self.deleted.append(kwargs)
                return FakeRequest({})

        events = FakeEvents()

        class FakeService:
            def events(self):
                return events

        week_start = sync.datetime.strptime("2026-10-05", "%Y-%m-%d").date()
        deleted = sync.delete_expired_events(FakeService(), ["calendar"], week_start)

        self.assertEqual(deleted, 1)
        self.assertEqual(events.deleted, [{"calendarId": "calendar", "eventId": "0123456789abcdef0123456789abcdef"}])

    def test_stale_window_cleanup_deletes_cancelled_or_replaced_events(self):
        class FakeRequest:
            def __init__(self, result):
                self.result = result

            def execute(self):
                return self.result

        class FakeEvents:
            def __init__(self):
                self.deleted = []

            def list(self, **kwargs):
                return FakeRequest({
                    "items": [
                        {"id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
                        {"id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
                        {"id": "personal-event"},
                    ]
                })

            def delete(self, **kwargs):
                self.deleted.append(kwargs)
                return FakeRequest({})

        events = FakeEvents()

        class FakeService:
            def events(self):
                return events

        week_start = sync.datetime.strptime("2026-10-05", "%Y-%m-%d").date()
        window_end = sync.datetime.strptime("2026-10-19", "%Y-%m-%d").date()
        active_by_cal = {"calendar": {"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}

        deleted = sync.delete_stale_window_events(
            FakeService(), ["calendar"], week_start, window_end, active_by_cal
        )

        self.assertEqual(deleted, 1)
        self.assertEqual(
            events.deleted,
            [{"calendarId": "calendar", "eventId": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}],
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
        cancelled_event_id = sync._event_id(today, "08:00", "Hodina")

        self.assertEqual(first_event_id, second_event_id)
        self.assertEqual(first_event_id, cancelled_event_id)

    def test_event_title_contains_room_and_location_uses_real_room_name(self):
        atom = {
            "ClassId": 7,
            "Class": {"Name": "3.A"},
            "RoomId": 5,
        }
        classes = {7: "3.A"}

        self.assertEqual(sync.extract_class_name(atom, classes), "3.A")
        self.assertEqual(sync.build_event_summary("Matematika", "101"), "Matematika (101)")
        self.assertEqual(sync.build_event_location("3.A", "101", "A1"), "A1 - 101")
        self.assertEqual(sync.build_event_location("3.A", "101"), "101")

    def test_event_description_omits_room_name_from_note(self):
        description = sync.build_event_description(
            teacher_name="Mgr. Novák",
            change_info="",
            absence_status=None,
            room_name="Učebna 1D",
            room_number="101",
        )

        self.assertIn("Mgr. Novák", description)
        self.assertNotIn("Poznámka:", description)
        self.assertNotIn("Vyučuje:", description)
        self.assertNotIn("Učebna:", description)

    def test_extract_teacher_name_prefers_teacher_data(self):
        atom = {
            "Teacher": {"Name": "Mgr. Novák"},
            "RoomId": 5,
        }

        self.assertEqual(sync.extract_teacher_name(atom), "Mgr. Novák")
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
