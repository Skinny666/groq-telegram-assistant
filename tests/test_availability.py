from __future__ import annotations

from datetime import datetime, time
import unittest
from zoneinfo import ZoneInfo

from telegram_assistant.availability import BusyInterval, find_available_slots


class AvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.zone = ZoneInfo("America/Sao_Paulo")
        self.now = datetime(2026, 7, 20, 8, 0, tzinfo=self.zone)  # segunda-feira

    def find(self, busy: list[BusyInterval], preferred: str = "any"):
        return find_available_slots(
            range_start=datetime(2026, 7, 20, 8, 0, tzinfo=self.zone),
            range_end=datetime(2026, 7, 21, 18, 0, tzinfo=self.zone),
            duration_minutes=30,
            preferred_period=preferred,
            result_limit=3,
            busy_intervals=busy,
            local_timezone=self.zone,
            workday_start=time(9, 0),
            workday_end=time(18, 0),
            interval_minutes=15,
            buffer_minutes=15,
            minimum_lead_minutes=30,
            now=self.now,
        )

    def test_avoids_busy_event_and_buffer(self) -> None:
        busy = [
            BusyInterval(
                starts_at=datetime(2026, 7, 20, 10, 0, tzinfo=self.zone),
                ends_at=datetime(2026, 7, 20, 11, 0, tzinfo=self.zone),
            )
        ]
        slots = self.find(busy)
        for slot in slots:
            self.assertFalse(
                slot.starts_at < datetime(2026, 7, 20, 11, 15, tzinfo=self.zone)
                and slot.ends_at > datetime(2026, 7, 20, 9, 45, tzinfo=self.zone)
            )

    def test_afternoon_preference_is_respected(self) -> None:
        slots = self.find([], preferred="afternoon")
        self.assertTrue(slots)
        self.assertGreaterEqual(slots[0].starts_at.hour, 12)
        self.assertLess(slots[0].starts_at.hour, 17)

    def test_weekend_is_skipped(self) -> None:
        slots = find_available_slots(
            range_start=datetime(2026, 7, 25, 8, 0, tzinfo=self.zone),  # sábado
            range_end=datetime(2026, 7, 27, 18, 0, tzinfo=self.zone),
            duration_minutes=30,
            preferred_period="any",
            result_limit=1,
            busy_intervals=[],
            local_timezone=self.zone,
            workday_start=time(9, 0),
            workday_end=time(18, 0),
            interval_minutes=15,
            buffer_minutes=15,
            minimum_lead_minutes=0,
            now=datetime(2026, 7, 25, 7, 0, tzinfo=self.zone),
        )
        self.assertEqual(slots[0].starts_at.weekday(), 0)


if __name__ == "__main__":
    unittest.main()
