from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
from zoneinfo import ZoneInfo

from telegram_assistant.database import EventRepository


class MigrationTests(unittest.TestCase):
    def test_version_two_is_migrated_with_event_and_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        owner_user_id INTEGER NOT NULL,
                        chat_id INTEGER NOT NULL,
                        title TEXT NOT NULL,
                        starts_at TEXT NOT NULL,
                        ends_at TEXT NOT NULL,
                        reminder_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        reminder_sent_at TEXT,
                        reminder_claimed_at TEXT,
                        reminder_next_attempt_at TEXT,
                        reminder_retry_count INTEGER NOT NULL DEFAULT 0,
                        reminder_failed_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE pending_actions (
                        token_hash TEXT PRIMARY KEY,
                        owner_user_id INTEGER NOT NULL,
                        chat_id INTEGER NOT NULL,
                        action TEXT NOT NULL CHECK(action IN ('create_event','cancel_event')),
                        payload_json TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        consumed_at TEXT,
                        created_at TEXT NOT NULL
                    );
                    INSERT INTO events (
                        owner_user_id, chat_id, title, starts_at, ends_at,
                        reminder_at, status, created_at, updated_at
                    ) VALUES (
                        123, 123, 'Evento legado',
                        '2030-07-21T18:00:00Z', '2030-07-21T18:30:00Z',
                        '2030-07-21T17:30:00Z', 'scheduled',
                        '2026-07-20T13:00:00Z', '2026-07-20T13:00:00Z'
                    );
                    PRAGMA user_version = 2;
                    """
                )

            EventRepository(path, ZoneInfo("America/Sao_Paulo")).initialize()
            with sqlite3.connect(path) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                title = connection.execute("SELECT title FROM events").fetchone()[0]
                reminders = connection.execute(
                    "SELECT minutes_before FROM event_reminders ORDER BY minutes_before DESC"
                ).fetchall()
                pending_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name='pending_actions'"
                ).fetchone()[0]

            self.assertEqual(version, 3)
            self.assertEqual(title, "Evento legado")
            self.assertEqual([row[0] for row in reminders], [30, 15])
            self.assertIn("update_event", pending_sql)
            self.assertIn("delete_event", pending_sql)


if __name__ == "__main__":
    unittest.main()
