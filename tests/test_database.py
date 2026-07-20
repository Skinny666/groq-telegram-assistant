from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest
from zoneinfo import ZoneInfo

from telegram_assistant.database import EventNotFound, EventRepository, PendingActionInvalid
from telegram_assistant.domain import build_event_draft


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.zone = ZoneInfo("America/Sao_Paulo")
        self.now = datetime(2026, 7, 20, 10, 0, tzinfo=self.zone)
        self.path = Path(self.tempdir.name) / "events.db"
        self.repository = EventRepository(self.path, self.zone)
        self.repository.initialize()
        self.event = build_event_draft(
            title="Reunião segura",
            starts_at="2026-07-21T15:00:00-03:00",
            duration_minutes=45,
            now=self.now,
            local_timezone=self.zone,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def create(self) -> int:
        token = self.repository.create_pending_event(
            owner_user_id=123,
            chat_id=123,
            event=self.event,
            now=self.now,
            ttl_seconds=600,
        )
        return self.repository.execute_pending(
            token=token,
            owner_user_id=123,
            chat_id=123,
            now=self.now + timedelta(seconds=10),
        ).event_id

    def test_token_is_hashed_and_replay_fails(self) -> None:
        token = self.repository.create_pending_event(
            owner_user_id=123,
            chat_id=123,
            event=self.event,
            now=self.now,
            ttl_seconds=600,
        )
        with sqlite3.connect(self.path) as connection:
            stored = connection.execute("SELECT token_hash FROM pending_actions").fetchone()[0]
        self.assertNotEqual(stored, token)
        self.assertEqual(len(stored), 64)
        self.repository.execute_pending(
            token=token,
            owner_user_id=123,
            chat_id=123,
            now=self.now + timedelta(seconds=10),
        )
        with self.assertRaises(PendingActionInvalid):
            self.repository.execute_pending(
                token=token,
                owner_user_id=123,
                chat_id=123,
                now=self.now + timedelta(seconds=20),
            )

    def test_confirmation_is_bound_to_owner(self) -> None:
        token = self.repository.create_pending_event(
            owner_user_id=123, chat_id=123, event=self.event, now=self.now, ttl_seconds=600
        )
        with self.assertRaises(PendingActionInvalid):
            self.repository.execute_pending(
                token=token,
                owner_user_id=999,
                chat_id=999,
                now=self.now + timedelta(seconds=10),
            )

    def test_create_produces_two_reminders(self) -> None:
        event_id = self.create()
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                "SELECT minutes_before FROM event_reminders WHERE event_id=? ORDER BY minutes_before DESC",
                (event_id,),
            ).fetchall()
        self.assertEqual([row[0] for row in rows], [30, 15])

    def test_due_reminders_are_claimed_once(self) -> None:
        event_id = self.create()
        due_time = self.event.starts_at - timedelta(minutes=30) + timedelta(seconds=1)
        first = self.repository.claim_due_reminders(now=due_time)
        second = self.repository.claim_due_reminders(now=due_time)
        self.assertEqual([item.event_id for item in first], [event_id])
        self.assertEqual(first[0].minutes_before, 30)
        self.assertEqual(second, [])

    def test_update_replaces_event_and_reminders(self) -> None:
        event_id = self.create()
        changed = build_event_draft(
            title="Reunião alterada",
            starts_at="2026-07-22T11:00:00-03:00",
            duration_minutes=60,
            now=self.now,
            local_timezone=self.zone,
        )
        token, _ = self.repository.create_pending_update(
            owner_user_id=123,
            chat_id=123,
            event_id=event_id,
            event=changed,
            now=self.now + timedelta(minutes=1),
            ttl_seconds=600,
        )
        result = self.repository.execute_pending(
            token=token,
            owner_user_id=123,
            chat_id=123,
            now=self.now + timedelta(minutes=2),
        )
        self.assertEqual(result.action, "update_event")
        stored = self.repository.get_event(owner_user_id=123, event_id=event_id)
        self.assertEqual(stored.title, "Reunião alterada")
        self.assertEqual(stored.duration_minutes, 60)
        with sqlite3.connect(self.path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM event_reminders WHERE event_id=?", (event_id,)
            ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_delete_requires_confirmation_and_removes_reminders(self) -> None:
        event_id = self.create()
        token, _ = self.repository.create_pending_delete(
            owner_user_id=123,
            chat_id=123,
            event_id=event_id,
            now=self.now + timedelta(minutes=1),
            ttl_seconds=600,
        )
        result = self.repository.execute_pending(
            token=token,
            owner_user_id=123,
            chat_id=123,
            now=self.now + timedelta(minutes=2),
        )
        self.assertEqual(result.action, "delete_event")
        with self.assertRaises(EventNotFound):
            self.repository.get_event(
                owner_user_id=123, event_id=event_id, only_scheduled=True
            )
        with sqlite3.connect(self.path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM event_reminders WHERE event_id=?", (event_id,)
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_list_between_returns_overlapping_event(self) -> None:
        self.create()
        events = self.repository.list_between(
            owner_user_id=123,
            range_start=self.event.starts_at + timedelta(minutes=10),
            range_end=self.event.ends_at + timedelta(minutes=10),
            limit=10,
        )
        self.assertEqual(len(events), 1)

    def test_rate_limit_status_combines_headers_and_local_usage(self) -> None:
        self.repository.record_groq_call(
            model="openai/gpt-oss-20b",
            requested_at=self.now,
            total_tokens=250,
            rpd_limit=1000,
            rpd_remaining=998,
            rpd_reset="23h",
            tpm_limit=8000,
            tpm_remaining=7600,
            tpm_reset="8s",
        )
        status = self.repository.get_rate_limit_status(
            now=self.now + timedelta(seconds=10),
            configured_rpm=30,
            configured_rpd=1000,
            configured_tpm=8000,
            configured_tpd=200000,
        )
        self.assertEqual(status.rpm_remaining_estimate, 29)
        self.assertEqual(status.rpd_remaining, 998)
        self.assertEqual(status.tpm_remaining, 7600)
        self.assertEqual(status.tpd_remaining_estimate, 199750)


if __name__ == "__main__":
    unittest.main()
