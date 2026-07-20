from __future__ import annotations

from datetime import datetime, timezone
import unittest

from telegram_assistant.database import RateLimitStatus
from telegram_assistant.quota import format_rate_limits_compact


class BotFormattingTests(unittest.TestCase):
    def test_compact_limits_are_human_readable(self) -> None:
        status = RateLimitStatus(
            observed_at=datetime.now(timezone.utc),
            rpm_limit=30,
            rpm_remaining_estimate=28,
            rpd_limit=1000,
            rpd_remaining=997,
            rpd_reset=None,
            tpm_limit=8000,
            tpm_remaining=7400,
            tpm_reset=None,
            tpd_limit=200000,
            tpd_remaining_estimate=198500,
            local_requests_last_minute=2,
            local_tokens_last_24_hours=1500,
        )
        text = format_rate_limits_compact(status)
        self.assertIn("~28 req/min", text)
        self.assertIn("997 req/dia", text)
        self.assertIn("7.400 tokens/min", text)


if __name__ == "__main__":
    unittest.main()
