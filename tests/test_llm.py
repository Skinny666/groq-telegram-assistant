from __future__ import annotations

from datetime import datetime
import json
import unittest
from zoneinfo import ZoneInfo

import httpx

from telegram_assistant.llm import (
    AgendaContextEvent,
    AgendaSnapshot,
    build_request_payload,
    parse_rate_headers,
)


class LLMTests(unittest.TestCase):
    def test_parses_documented_rate_headers(self) -> None:
        parsed = parse_rate_headers(httpx.Headers({
            "x-ratelimit-limit-requests": "1000",
            "x-ratelimit-remaining-requests": "998",
            "x-ratelimit-limit-tokens": "8000",
            "x-ratelimit-remaining-tokens": "7421",
        }))
        self.assertEqual(parsed.rpd_remaining, 998)
        self.assertEqual(parsed.tpm_remaining, 7421)

    def test_request_payload_contains_snapshot_and_forced_action(self) -> None:
        zone = ZoneInfo("America/Sao_Paulo")
        now = datetime(2026, 7, 20, 10, 0, tzinfo=zone)
        snapshot = AgendaSnapshot(
            range_start=now,
            range_end=datetime(2026, 8, 19, 10, 0, tzinfo=zone),
            truncated=False,
            events=(AgendaContextEvent(
                event_id=12,
                title="Reunião com equipe",
                starts_at=datetime(2026, 7, 21, 15, 0, tzinfo=zone),
                ends_at=datetime(2026, 7, 21, 15, 45, tzinfo=zone),
            ),),
        )
        payload = build_request_payload(
            text="Mude para 16h",
            forced_action="update_event",
            now=now,
            local_timezone=zone,
            model="openai/gpt-oss-20b",
            agenda_snapshot=snapshot,
        )
        user_content = json.loads(payload["messages"][1]["content"])
        self.assertEqual(user_content["forced_action"], "update_event")
        self.assertEqual(user_content["agenda_snapshot"]["events"][0]["id"], 12)
        self.assertNotIn("owner_user_id", json.dumps(user_content))
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["max_completion_tokens"], 2048)

    def test_agenda_snapshot_rejects_more_than_one_hundred_events(self) -> None:
        zone = ZoneInfo("America/Sao_Paulo")
        now = datetime(2026, 7, 20, 10, 0, tzinfo=zone)
        event = AgendaContextEvent(
            event_id=1,
            title="Evento",
            starts_at=datetime(2026, 7, 21, 10, 0, tzinfo=zone),
            ends_at=datetime(2026, 7, 21, 10, 30, tzinfo=zone),
        )
        with self.assertRaises(ValueError):
            AgendaSnapshot(
                range_start=now,
                range_end=datetime(2026, 8, 19, 10, 0, tzinfo=zone),
                truncated=True,
                events=tuple(event for _ in range(101)),
            )


if __name__ == "__main__":
    unittest.main()
