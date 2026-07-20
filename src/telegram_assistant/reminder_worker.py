from __future__ import annotations

import asyncio
from datetime import datetime, tzinfo
import logging
import math

from .config import AppConfig, ConfigurationError
from .database import EventRepository, RepositoryError
from .domain import utc_now
from .telegram_api import TelegramDeliveryError, send_message


logger = logging.getLogger(__name__)


def _reminder_text(*, title: str, starts_at: datetime, now: datetime, timezone: tzinfo) -> str:
    local_start = starts_at.astimezone(timezone)
    remaining = max(1, math.ceil((starts_at - now).total_seconds() / 60))
    return (
        f"Seu compromisso começa em {remaining} minutos.\n\n"
        f"{title}\n"
        f"{local_start.strftime('%d/%m/%Y às %H:%M')}"
    )


async def run_once() -> int:
    config = AppConfig.load(require_llm=False)
    repository = EventRepository(config.database_path, config.timezone)
    repository.initialize()

    now = utc_now()
    reminders = await asyncio.to_thread(
        repository.claim_due_reminders,
        now=now,
        stale_after_minutes=10,
        limit=20,
    )

    failures = 0
    for reminder in reminders:
        text = _reminder_text(
            title=reminder.title,
            starts_at=reminder.starts_at,
            now=now,
            timezone=config.timezone,
        )
        try:
            await send_message(
                bot_token=config.telegram_token,
                chat_id=config.authorized_user_id,
                text=text,
            )
        except TelegramDeliveryError as exc:
            failures += 1
            logger.warning(
                "Falha ao enviar lembrete %s do evento %s.",
                reminder.reminder_id,
                reminder.event_id,
            )
            await asyncio.to_thread(
                repository.mark_reminder_failure,
                reminder_id=reminder.reminder_id,
                now=utc_now(),
                retry_after_seconds=exc.retry_after_seconds,
            )
        else:
            await asyncio.to_thread(
                repository.mark_reminder_sent,
                reminder_id=reminder.reminder_id,
                now=utc_now(),
            )

    await asyncio.to_thread(repository.purge_expired_pending, now=utc_now())
    return 1 if failures else 0


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    try:
        exit_code = asyncio.run(run_once())
    except (ConfigurationError, RepositoryError) as exc:
        logger.error("Worker não executado: %s", type(exc).__name__)
        raise SystemExit(2) from exc
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
