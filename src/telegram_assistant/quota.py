from __future__ import annotations

from .database import RateLimitStatus


def _number(value: int | None) -> str:
    if value is None:
        return "?"
    return f"{value:,}".replace(",", ".")


def format_rate_limits_compact(status: RateLimitStatus) -> str:
    return (
        "Uso da Groq\n"
        f"~{_number(status.rpm_remaining_estimate)} req/min · "
        f"{_number(status.rpd_remaining)} req/dia\n"
        f"{_number(status.tpm_remaining)} tokens/min · "
        f"~{_number(status.tpd_remaining_estimate)} tokens/dia"
    )
