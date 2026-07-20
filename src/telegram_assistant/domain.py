from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any
from zoneinfo import ZoneInfo


class DomainValidationError(ValueError):
    """Entrada fora do contrato de negócio."""


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ALLOWED_ACTIONS = {
    "create_event",
    "update_event",
    "delete_event",
    "list_events",
    "suggest_time",
    "rate_limits",
    "unknown",
}
_ALLOWED_PERIODS = {"morning", "afternoon", "evening", "any"}
REMINDER_OFFSETS_MINUTES: tuple[int, int] = (30, 15)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise DomainValidationError("Datetime sem timezone.")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def from_utc_text(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise DomainValidationError("Datetime persistido sem timezone.")
    return parsed.astimezone(timezone.utc)


def sanitize_title(value: Any) -> str:
    if not isinstance(value, str):
        raise DomainValidationError("Título deve ser texto.")

    title = _CONTROL_CHARACTERS.sub("", value).strip()
    title = " ".join(title.split())
    if not 1 <= len(title) <= 120:
        raise DomainValidationError("Título deve possuir entre 1 e 120 caracteres.")
    return title


@dataclass(frozen=True, slots=True)
class EventDraft:
    title: str
    starts_at: datetime
    duration_minutes: int
    # Mantido para compatibilidade com versões anteriores. A persistência cria
    # sempre dois lembretes fixos: 30 e 15 minutos antes.
    reminder_minutes: int = 30

    @property
    def ends_at(self) -> datetime:
        return self.starts_at + timedelta(minutes=self.duration_minutes)

    @property
    def reminder_at(self) -> datetime:
        return self.starts_at - timedelta(minutes=REMINDER_OFFSETS_MINUTES[0])

    @property
    def reminder_offsets(self) -> tuple[int, int]:
        return REMINDER_OFFSETS_MINUTES

    def to_payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "starts_at": self.starts_at.isoformat(timespec="seconds"),
            "duration_minutes": self.duration_minutes,
            "reminder_minutes": 30,
        }

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        now: datetime,
        local_timezone: ZoneInfo,
    ) -> "EventDraft":
        return build_event_draft(
            title=payload.get("title"),
            starts_at=payload.get("starts_at"),
            duration_minutes=payload.get("duration_minutes"),
            reminder_minutes=30,
            now=now,
            local_timezone=local_timezone,
        )


@dataclass(frozen=True, slots=True)
class EventUpdate:
    title: str | None
    starts_at: datetime | None
    duration_minutes: int | None

    @property
    def has_changes(self) -> bool:
        return any(
            value is not None
            for value in (self.title, self.starts_at, self.duration_minutes)
        )


@dataclass(frozen=True, slots=True)
class AgendaQuery:
    range_start: datetime
    range_end: datetime
    duration_minutes: int
    preferred_period: str
    result_limit: int


@dataclass(frozen=True, slots=True)
class ParsedIntent:
    action: str
    event: EventDraft | None
    update: EventUpdate | None
    query: AgendaQuery | None
    event_id: int | None
    target_title: str | None
    missing_fields: tuple[str, ...]


def _parse_datetime(value: Any, local_timezone: ZoneInfo) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError("Data/hora ausente.")

    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise DomainValidationError("Data/hora não está em ISO 8601.") from exc

    if parsed.tzinfo is None:
        raise DomainValidationError("Data/hora deve conter offset de timezone.")
    return parsed.astimezone(local_timezone)


def _parse_optional_datetime(value: Any, local_timezone: ZoneInfo) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(value, local_timezone)


def _validate_integer(
    name: str,
    value: Any,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(f"{name} deve ser inteiro.")
    if not minimum <= value <= maximum:
        raise DomainValidationError(f"{name} deve ficar entre {minimum} e {maximum}.")
    return value


def _parse_optional_positive_int(value: Any, *, name: str) -> int | None:
    if value is None:
        return None
    return _validate_integer(name, value, minimum=1, maximum=2_147_483_647)


def build_event_draft(
    *,
    title: Any,
    starts_at: Any,
    duration_minutes: Any,
    reminder_minutes: Any = 30,
    now: datetime,
    local_timezone: ZoneInfo,
) -> EventDraft:
    del reminder_minutes  # lembretes são política fixa do aplicativo

    if now.tzinfo is None:
        raise DomainValidationError("Relógio sem timezone.")

    clean_title = sanitize_title(title)
    local_start = _parse_datetime(starts_at, local_timezone)
    local_now = now.astimezone(local_timezone)

    if local_start <= local_now + timedelta(minutes=1):
        raise DomainValidationError("O compromisso precisa estar no futuro.")
    if local_start > local_now + timedelta(days=730):
        raise DomainValidationError("O compromisso excede o limite de dois anos.")

    duration = _validate_integer("Duração", duration_minutes, minimum=5, maximum=480)

    return EventDraft(
        title=clean_title,
        starts_at=local_start,
        duration_minutes=duration,
        reminder_minutes=30,
    )


def build_updated_event(
    *,
    current_title: str,
    current_starts_at: datetime,
    current_duration_minutes: int,
    update: EventUpdate,
    now: datetime,
    local_timezone: ZoneInfo,
) -> EventDraft:
    starts_at: str | datetime
    if update.starts_at is None:
        starts_at = current_starts_at
    else:
        starts_at = update.starts_at

    return build_event_draft(
        title=update.title if update.title is not None else current_title,
        starts_at=(
            starts_at.isoformat(timespec="seconds")
            if isinstance(starts_at, datetime)
            else starts_at
        ),
        duration_minutes=(
            update.duration_minutes
            if update.duration_minutes is not None
            else current_duration_minutes
        ),
        reminder_minutes=30,
        now=now,
        local_timezone=local_timezone,
    )


def _build_agenda_query(
    *,
    action: str,
    payload: dict[str, Any],
    now: datetime,
    local_timezone: ZoneInfo,
) -> AgendaQuery:
    local_now = now.astimezone(local_timezone)
    range_start = _parse_optional_datetime(payload["range_start"], local_timezone)
    range_end = _parse_optional_datetime(payload["range_end"], local_timezone)

    if action == "list_events":
        range_start = max(range_start or local_now, local_now)
        range_end = range_end or (range_start + timedelta(days=30))
        default_limit = 10
        maximum_window = timedelta(days=366)
    else:
        range_start = max(range_start or local_now, local_now)
        range_end = range_end or (range_start + timedelta(days=7))
        default_limit = 3
        maximum_window = timedelta(days=31)

    if range_end <= range_start:
        raise DomainValidationError("Intervalo da consulta é inválido.")
    if range_end - range_start > maximum_window:
        raise DomainValidationError("Intervalo da consulta excede o limite permitido.")

    raw_duration = payload["duration_minutes"]
    duration = (
        30
        if raw_duration is None
        else _validate_integer("Duração", raw_duration, minimum=5, maximum=480)
    )

    raw_period = payload["preferred_period"]
    preferred_period = "any" if raw_period is None else raw_period
    if preferred_period not in _ALLOWED_PERIODS:
        raise DomainValidationError("Período preferido inválido.")

    raw_limit = payload["result_limit"]
    result_limit = (
        default_limit
        if raw_limit is None
        else _validate_integer(
            "Quantidade de resultados", raw_limit, minimum=1, maximum=20
        )
    )

    return AgendaQuery(
        range_start=range_start,
        range_end=range_end,
        duration_minutes=duration,
        preferred_period=preferred_period,
        result_limit=result_limit,
    )


def parse_llm_payload(
    payload: Any,
    *,
    now: datetime,
    local_timezone: ZoneInfo,
) -> ParsedIntent:
    if not isinstance(payload, dict):
        raise DomainValidationError("Resposta da LLM não é um objeto.")

    allowed_keys = {
        "action",
        "event_id",
        "target_title",
        "title",
        "starts_at",
        "duration_minutes",
        "reminder_minutes",
        "range_start",
        "range_end",
        "preferred_period",
        "result_limit",
        "missing_fields",
    }
    if set(payload) != allowed_keys:
        raise DomainValidationError("Resposta da LLM possui campos inesperados.")

    action = payload["action"]
    if action not in _ALLOWED_ACTIONS:
        raise DomainValidationError("Ação da LLM não permitida.")

    missing_value = payload["missing_fields"]
    if not isinstance(missing_value, list) or any(
        not isinstance(item, str) for item in missing_value
    ):
        raise DomainValidationError("missing_fields inválido.")

    allowed_missing = {"title", "starts_at", "target_event", "changes"}
    missing_fields = tuple(dict.fromkeys(missing_value))
    if any(item not in allowed_missing for item in missing_fields):
        raise DomainValidationError("missing_fields contém valor não permitido.")

    event_id = _parse_optional_positive_int(payload["event_id"], name="ID do evento")
    target_title = (
        sanitize_title(payload["target_title"])
        if payload["target_title"] is not None
        else None
    )

    if action in {"unknown", "rate_limits"}:
        return ParsedIntent(
            action=action,
            event=None,
            update=None,
            query=None,
            event_id=None,
            target_title=None,
            missing_fields=(),
        )

    if action == "create_event":
        if missing_fields:
            return ParsedIntent(
                action=action,
                event=None,
                update=None,
                query=None,
                event_id=None,
                target_title=None,
                missing_fields=missing_fields,
            )
        event = build_event_draft(
            title=payload["title"],
            starts_at=payload["starts_at"],
            duration_minutes=payload["duration_minutes"] or 30,
            reminder_minutes=30,
            now=now,
            local_timezone=local_timezone,
        )
        return ParsedIntent(
            action=action,
            event=event,
            update=None,
            query=None,
            event_id=None,
            target_title=None,
            missing_fields=(),
        )

    if action == "update_event":
        title = sanitize_title(payload["title"]) if payload["title"] is not None else None
        starts_at = _parse_optional_datetime(payload["starts_at"], local_timezone)
        duration = (
            _validate_integer(
                "Duração", payload["duration_minutes"], minimum=5, maximum=480
            )
            if payload["duration_minutes"] is not None
            else None
        )
        update = EventUpdate(title=title, starts_at=starts_at, duration_minutes=duration)
        normalized_missing = list(missing_fields)
        if event_id is None and target_title is None and "target_event" not in normalized_missing:
            normalized_missing.append("target_event")
        if not update.has_changes and "changes" not in normalized_missing:
            normalized_missing.append("changes")
        return ParsedIntent(
            action=action,
            event=None,
            update=update,
            query=None,
            event_id=event_id,
            target_title=target_title,
            missing_fields=tuple(normalized_missing),
        )

    if action == "delete_event":
        normalized_missing = list(missing_fields)
        if event_id is None and target_title is None and "target_event" not in normalized_missing:
            normalized_missing.append("target_event")
        return ParsedIntent(
            action=action,
            event=None,
            update=None,
            query=None,
            event_id=event_id,
            target_title=target_title,
            missing_fields=tuple(normalized_missing),
        )

    query = _build_agenda_query(
        action=action,
        payload=payload,
        now=now,
        local_timezone=local_timezone,
    )
    return ParsedIntent(
        action=action,
        event=None,
        update=None,
        query=query,
        event_id=None,
        target_title=None,
        missing_fields=(),
    )
