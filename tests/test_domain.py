from __future__ import annotations

from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from telegram_assistant.domain import (
    DomainValidationError,
    build_event_draft,
    build_updated_event,
    parse_llm_payload,
)


class DomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.zone = ZoneInfo("America/Sao_Paulo")
        self.now = datetime(2026, 7, 20, 10, 0, tzinfo=self.zone)

    def payload(self, **changes: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "action": "unknown",
            "event_id": None,
            "target_title": None,
            "title": None,
            "starts_at": None,
            "duration_minutes": None,
            "reminder_minutes": None,
            "range_start": None,
            "range_end": None,
            "preferred_period": None,
            "result_limit": None,
            "missing_fields": [],
        }
        payload.update(changes)
        return payload

    def test_builds_valid_event_with_fixed_reminders(self) -> None:
        event = build_event_draft(
            title="  Reunião   com equipe  ",
            starts_at="2026-07-21T15:00:00-03:00",
            duration_minutes=45,
            reminder_minutes=5,
            now=self.now,
            local_timezone=self.zone,
        )
        self.assertEqual(event.title, "Reunião com equipe")
        self.assertEqual(event.duration_minutes, 45)
        self.assertEqual(event.reminder_offsets, (30, 15))

    def test_rejects_past_event(self) -> None:
        with self.assertRaises(DomainValidationError):
            build_event_draft(
                title="Evento",
                starts_at="2026-07-20T09:00:00-03:00",
                duration_minutes=30,
                now=self.now,
                local_timezone=self.zone,
            )

    def test_rejects_datetime_without_offset(self) -> None:
        with self.assertRaises(DomainValidationError):
            build_event_draft(
                title="Evento",
                starts_at="2026-07-21T09:00:00",
                duration_minutes=30,
                now=self.now,
                local_timezone=self.zone,
            )

    def test_rejects_extra_llm_fields(self) -> None:
        payload = self.payload(
            action="create_event",
            title="Evento",
            starts_at="2026-07-21T09:00:00-03:00",
            duration_minutes=30,
            reminder_minutes=30,
        )
        payload["shell"] = "cat /etc/passwd"
        with self.assertRaises(DomainValidationError):
            parse_llm_payload(payload, now=self.now, local_timezone=self.zone)

    def test_unknown_action_does_not_create_event(self) -> None:
        intent = parse_llm_payload(
            self.payload(), now=self.now, local_timezone=self.zone
        )
        self.assertEqual(intent.action, "unknown")
        self.assertIsNone(intent.event)
        self.assertIsNone(intent.query)

    def test_list_events_uses_safe_defaults(self) -> None:
        intent = parse_llm_payload(
            self.payload(action="list_events"),
            now=self.now,
            local_timezone=self.zone,
        )
        assert intent.query is not None
        self.assertEqual(intent.query.result_limit, 10)
        self.assertEqual((intent.query.range_end - intent.query.range_start).days, 30)

    def test_suggest_time_extracts_constraints(self) -> None:
        intent = parse_llm_payload(
            self.payload(
                action="suggest_time",
                duration_minutes=45,
                preferred_period="afternoon",
                result_limit=3,
                range_start="2026-07-21T00:00:00-03:00",
                range_end="2026-07-25T23:59:00-03:00",
            ),
            now=self.now,
            local_timezone=self.zone,
        )
        assert intent.query is not None
        self.assertEqual(intent.query.duration_minutes, 45)
        self.assertEqual(intent.query.preferred_period, "afternoon")

    def test_update_extracts_target_and_changes(self) -> None:
        intent = parse_llm_payload(
            self.payload(
                action="update_event",
                event_id=4,
                starts_at="2026-07-22T11:00:00-03:00",
            ),
            now=self.now,
            local_timezone=self.zone,
        )
        self.assertEqual(intent.event_id, 4)
        assert intent.update is not None
        self.assertTrue(intent.update.has_changes)
        self.assertEqual(intent.missing_fields, ())

    def test_update_requires_change(self) -> None:
        intent = parse_llm_payload(
            self.payload(action="update_event", event_id=4),
            now=self.now,
            local_timezone=self.zone,
        )
        self.assertIn("changes", intent.missing_fields)

    def test_delete_requires_target(self) -> None:
        intent = parse_llm_payload(
            self.payload(action="delete_event"),
            now=self.now,
            local_timezone=self.zone,
        )
        self.assertIn("target_event", intent.missing_fields)

    def test_build_updated_event_preserves_unchanged_fields(self) -> None:
        current = build_event_draft(
            title="Reunião",
            starts_at="2026-07-21T15:00:00-03:00",
            duration_minutes=30,
            now=self.now,
            local_timezone=self.zone,
        )
        assert parse_llm_payload(
            self.payload(
                action="update_event",
                event_id=1,
                duration_minutes=60,
            ),
            now=self.now,
            local_timezone=self.zone,
        ).update is not None
        update = parse_llm_payload(
            self.payload(action="update_event", event_id=1, duration_minutes=60),
            now=self.now,
            local_timezone=self.zone,
        ).update
        assert update is not None
        changed = build_updated_event(
            current_title=current.title,
            current_starts_at=current.starts_at,
            current_duration_minutes=current.duration_minutes,
            update=update,
            now=self.now,
            local_timezone=self.zone,
        )
        self.assertEqual(changed.title, "Reunião")
        self.assertEqual(changed.duration_minutes, 60)


if __name__ == "__main__":
    unittest.main()
