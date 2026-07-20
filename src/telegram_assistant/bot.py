from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import re
import unicodedata
from typing import cast

from telegram import (
    BotCommand,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from .availability import BusyInterval, find_available_slots
from .config import AppConfig, ConfigurationError
from .database import (
    EventNotFound,
    EventRecord,
    EventRepository,
    PendingActionInvalid,
    RateLimitStatus,
    RepositoryError,
)
from .domain import (
    AgendaQuery,
    DomainValidationError,
    EventDraft,
    EventUpdate,
    ParsedIntent,
    build_event_draft,
    build_updated_event,
    utc_now,
)
from .llm import (
    AgendaContextEvent,
    AgendaSnapshot,
    GroqCalendarParser,
    GroqTelemetry,
    LLMUnavailable,
)
from .quota import format_rate_limits_compact
from .security import SlidingWindowRateLimiter


logger = logging.getLogger(__name__)
_CALLBACK_PATTERN = re.compile(r"^(ok|no):([A-Za-z0-9_-]{20,30})$")
_FLOW_KEY = "agenda_flow_v4"
_DAY_WITH_MONTH_PATTERN = re.compile(
    r"\b(?:dia\s+)?([0-3]?\d)\s*[/.-]\s*([01]?\d)(?:\s*[/.-]\s*(\d{4}))?\b"
)
_DAY_ONLY_PATTERN = re.compile(r"\bdia\s+([0-3]?\d)\b", re.IGNORECASE)

_GENERIC_CREATE = {
    "agendar",
    "quero agendar",
    "criar compromisso",
    "criar um compromisso",
    "novo compromisso",
}
_GENERIC_UPDATE = {
    "editar",
    "quero editar",
    "alterar compromisso",
    "mudar compromisso",
}
_GENERIC_DELETE = {
    "excluir",
    "quero excluir",
    "apagar compromisso",
    "cancelar compromisso",
}


@dataclass(slots=True)
class Runtime:
    config: AppConfig
    repository: EventRepository
    parser: GroqCalendarParser
    limiter: SlidingWindowRateLimiter


def _runtime(context: CallbackContext) -> Runtime:
    return cast(Runtime, context.application.bot_data["runtime"])


def _normalize_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    cleaned = re.sub(r"[^a-z0-9\s]", " ", without_accents)
    return " ".join(cleaned.split())


def _local_datetime(value: datetime, runtime: Runtime) -> str:
    return value.astimezone(runtime.config.timezone).strftime("%d/%m/%Y às %H:%M")


def _confirmation_keyboard(token: str, *, destructive: bool = False) -> InlineKeyboardMarkup:
    confirm_label = "Excluir" if destructive else "Confirmar"
    cancel_label = "Manter" if destructive else "Cancelar"
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(confirm_label, callback_data=f"ok:{token}"),
            InlineKeyboardButton(cancel_label, callback_data=f"no:{token}"),
        ]]
    )


def _clear_flow(context: CallbackContext) -> None:
    context.user_data.pop(_FLOW_KEY, None)


def _save_flow(
    context: CallbackContext,
    *,
    mode: str,
    text: str,
    now: datetime,
    ttl_seconds: int,
    event_id: int | None = None,
) -> None:
    context.user_data[_FLOW_KEY] = {
        "mode": mode,
        "text": text[:3000],
        "event_id": event_id,
        "expires_at": (now + timedelta(seconds=ttl_seconds)).timestamp(),
    }


def _load_flow(context: CallbackContext, *, now: datetime) -> dict[str, object] | None:
    flow = context.user_data.get(_FLOW_KEY)
    if not isinstance(flow, dict):
        return None
    if not isinstance(flow.get("mode"), str):
        _clear_flow(context)
        return None
    expires_at = flow.get("expires_at")
    if not isinstance(expires_at, (int, float)) or expires_at <= now.timestamp():
        _clear_flow(context)
        return None
    return flow


def _combine_flow_text(previous: str, current: str) -> str:
    previous = previous.strip()
    current = current.strip()
    if not previous:
        return current
    return f"Informações anteriores: {previous}\nNova informação: {current}"


def _forced_action_for_mode(mode: str) -> str:
    return {
        "create": "create_event",
        "update": "update_event",
        "delete": "delete_event",
    }[mode]


def _requested_day_range(
    text: str,
    *,
    now: datetime,
    runtime: Runtime,
) -> tuple[datetime, datetime] | None:
    local_now = now.astimezone(runtime.config.timezone)
    full_match = _DAY_WITH_MONTH_PATTERN.search(text)
    explicit_year = False

    if full_match:
        day = int(full_match.group(1))
        month = int(full_match.group(2))
        year = int(full_match.group(3) or local_now.year)
        explicit_year = full_match.group(3) is not None
    else:
        day_match = _DAY_ONLY_PATTERN.search(text)
        if not day_match:
            return None
        day = int(day_match.group(1))
        month = local_now.month
        year = local_now.year

    try:
        day_start = datetime(year, month, day, tzinfo=runtime.config.timezone)
    except ValueError:
        return None

    if not explicit_year and day_start.date() < local_now.date():
        if full_match:
            try:
                day_start = day_start.replace(year=year + 1)
            except ValueError:
                return None
        else:
            next_month = 1 if month == 12 else month + 1
            next_year = year + 1 if month == 12 else year
            try:
                day_start = datetime(
                    next_year,
                    next_month,
                    day,
                    tzinfo=runtime.config.timezone,
                )
            except ValueError:
                return None

    range_start = max(day_start, local_now)
    range_end = day_start + timedelta(days=1)
    return (range_start, range_end) if range_end > range_start else None


async def _get_rate_limit_status(runtime: Runtime) -> RateLimitStatus:
    return await asyncio.to_thread(
        runtime.repository.get_rate_limit_status,
        now=utc_now(),
        configured_rpm=runtime.config.groq_rpm_limit,
        configured_rpd=runtime.config.groq_rpd_limit,
        configured_tpm=runtime.config.groq_tpm_limit,
        configured_tpd=runtime.config.groq_tpd_limit,
    )


async def limits_footer_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if context.user_data.pop("skip_limits_footer_update_id", None) == update.update_id:
        return
    runtime = _runtime(context)
    chat = update.effective_chat
    if chat is None:
        return
    try:
        status = await _get_rate_limit_status(runtime)
        await context.bot.send_message(
            chat_id=chat.id,
            text=format_rate_limits_compact(status),
            protect_content=True,
        )
    except RepositoryError:
        logger.error("Falha ao montar rodapé de limites da Groq.")


async def _load_agenda_snapshot(runtime: Runtime, *, now: datetime) -> AgendaSnapshot:
    range_end = now + timedelta(days=runtime.config.llm_agenda_days)
    events = await asyncio.to_thread(
        runtime.repository.list_between,
        owner_user_id=runtime.config.authorized_user_id,
        range_start=now,
        range_end=range_end,
        limit=runtime.config.llm_agenda_max_events + 1,
    )
    truncated = len(events) > runtime.config.llm_agenda_max_events
    selected = events[: runtime.config.llm_agenda_max_events]
    return AgendaSnapshot(
        range_start=now,
        range_end=range_end,
        truncated=truncated,
        events=tuple(
            AgendaContextEvent(
                event_id=event.id,
                title=event.title,
                starts_at=event.starts_at,
                ends_at=event.ends_at,
            )
            for event in selected
        ),
    )


async def authorize_update(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    runtime = _runtime(context)
    user = update.effective_user
    chat = update.effective_chat
    authorized = (
        user is not None
        and chat is not None
        and chat.type == Chat.PRIVATE
        and user.id == runtime.config.authorized_user_id
        and chat.id == runtime.config.authorized_user_id
    )
    if not authorized:
        logger.warning("Atualização não autorizada descartada.")
        raise ApplicationHandlerStop

    if not runtime.limiter.allow():
        if update.callback_query is not None:
            await update.callback_query.answer(
                "Muitas solicitações. Aguarde um minuto.", show_alert=True
            )
        elif update.effective_message is not None:
            await update.effective_message.reply_text(
                "Muitas solicitações. Aguarde um minuto."
            )
        raise ApplicationHandlerStop


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        "Olá. Eu cuido da sua agenda.\n\n"
        "Você pode escrever normalmente:\n"
        "• Agende dentista amanhã às 15h\n"
        "• Mude o dentista para sexta às 10h\n"
        "• Exclua o compromisso com a Nadine\n"
        "• O que tenho semana que vem?\n"
        "• Tenho horário livre dia 27 à tarde?\n\n"
        "Todo compromisso recebe dois avisos: 30 e 15 minutos antes.\n\n"
        "Comandos:\n"
        "/agenda · /agendar · /editar · /excluir · /horarios · /limites"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is not None:
        await message.reply_text("Tudo funcionando normalmente.")


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    runtime = _runtime(context)
    now = utc_now()
    query = AgendaQuery(
        range_start=now,
        range_end=now + timedelta(days=30),
        duration_minutes=30,
        preferred_period="any",
        result_limit=20,
    )
    await _respond_with_events(message, runtime, query)


async def free_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    duration = 30
    if context.args:
        if len(context.args) != 1 or not context.args[0].isdigit():
            await message.reply_text("Exemplo: /horarios 45")
            return
        duration = int(context.args[0])
    if not 5 <= duration <= 480:
        await message.reply_text("Use uma duração entre 5 minutos e 8 horas.")
        return

    runtime = _runtime(context)
    now = utc_now()
    query = AgendaQuery(
        range_start=now,
        range_end=now + timedelta(days=7),
        duration_minutes=duration,
        preferred_period="any",
        result_limit=3,
    )
    await _respond_with_slots(message, runtime, query)


async def limits_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    context.user_data["skip_limits_footer_update_id"] = update.update_id
    await _respond_with_rate_limits(message, _runtime(context))


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return
    raw = message.text.partition(" ")[2].strip()
    now = utc_now()
    if not raw:
        _save_flow(
            context,
            mode="create",
            text="",
            now=now,
            ttl_seconds=_runtime(context).config.pending_action_ttl_seconds,
        )
        await message.reply_text("O que você quer agendar?")
        return
    await _process_text(update, context, raw, forced_action="create_event")


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return
    raw = message.text.partition(" ")[2].strip()
    now = utc_now()
    if not raw:
        _save_flow(
            context,
            mode="update",
            text="",
            now=now,
            ttl_seconds=_runtime(context).config.pending_action_ttl_seconds,
        )
        await message.reply_text("Qual compromisso você quer editar e o que deve mudar?")
        return

    forced_event_id: int | None = None
    match = re.match(r"^(\d+)\b\s*(.*)$", raw)
    if match:
        forced_event_id = int(match.group(1))
        raw = match.group(2).strip() or "alterar este compromisso"
    await _process_text(
        update,
        context,
        raw,
        forced_action="update_event",
        forced_event_id=forced_event_id,
    )


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return
    raw = message.text.partition(" ")[2].strip()
    now = utc_now()
    runtime = _runtime(context)
    if not raw:
        _save_flow(
            context,
            mode="delete",
            text="",
            now=now,
            ttl_seconds=runtime.config.pending_action_ttl_seconds,
        )
        await message.reply_text("Qual compromisso você quer excluir?")
        return
    if raw.isdigit():
        await _propose_delete(update, context, int(raw))
        return
    await _process_text(update, context, raw, forced_action="delete_event")


async def exact_schedule_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return
    runtime = _runtime(context)
    raw = message.text.partition(" ")[2].strip()
    try:
        date_part, duration_part, title_part = [
            part.strip() for part in raw.split("|", maxsplit=2)
        ]
        local_start = datetime.strptime(date_part, "%Y-%m-%d %H:%M").replace(
            tzinfo=runtime.config.timezone
        )
        event = build_event_draft(
            title=title_part,
            starts_at=local_start.isoformat(timespec="seconds"),
            duration_minutes=int(duration_part),
            reminder_minutes=30,
            now=utc_now(),
            local_timezone=runtime.config.timezone,
        )
    except (ValueError, DomainValidationError):
        await message.reply_text(
            "Use: /agendar_exato AAAA-MM-DD HH:MM | minutos | título"
        )
        return
    await _propose_create(update, context, event)


async def natural_language_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return

    text = message.text.strip()
    normalized = _normalize_text(text)
    now = utc_now()
    runtime = _runtime(context)

    if normalized in {"cancelar", "deixa pra la", "deixa para la"}:
        if _load_flow(context, now=now):
            _clear_flow(context)
            await message.reply_text("Tudo bem. Não fiz nenhuma alteração.")
            return

    flow = _load_flow(context, now=now)
    if flow is not None:
        mode = cast(str, flow["mode"])
        previous = cast(str, flow.get("text", ""))
        event_id = flow.get("event_id")
        combined = _combine_flow_text(previous, text)
        await _process_text(
            update,
            context,
            combined,
            forced_action=_forced_action_for_mode(mode),
            forced_event_id=event_id if isinstance(event_id, int) else None,
        )
        return

    if normalized in _GENERIC_CREATE:
        _save_flow(
            context,
            mode="create",
            text="",
            now=now,
            ttl_seconds=runtime.config.pending_action_ttl_seconds,
        )
        await message.reply_text("O que você quer agendar?")
        return
    if normalized in _GENERIC_UPDATE:
        _save_flow(
            context,
            mode="update",
            text="",
            now=now,
            ttl_seconds=runtime.config.pending_action_ttl_seconds,
        )
        await message.reply_text("Qual compromisso você quer editar e o que deve mudar?")
        return
    if normalized in _GENERIC_DELETE:
        _save_flow(
            context,
            mode="delete",
            text="",
            now=now,
            ttl_seconds=runtime.config.pending_action_ttl_seconds,
        )
        await message.reply_text("Qual compromisso você quer excluir?")
        return

    await _process_text(update, context, text)


async def _process_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    forced_action: str | None = None,
    forced_event_id: int | None = None,
) -> None:
    message = update.effective_message
    if message is None:
        return
    runtime = _runtime(context)
    if not 1 <= len(text) <= runtime.config.max_message_chars * 3:
        await message.reply_text("A mensagem ficou muito longa. Envie apenas os dados do compromisso.")
        return

    requested_at = utc_now()
    try:
        snapshot = await _load_agenda_snapshot(runtime, now=requested_at)
    except (RepositoryError, DomainValidationError, ValueError):
        logger.error("Falha ao carregar o contexto da agenda.")
        await message.reply_text("Não consegui consultar sua agenda agora.")
        return

    try:
        result = await runtime.parser.parse(
            text=text,
            now=requested_at,
            agenda_snapshot=snapshot,
            forced_action=forced_action,
        )
    except LLMUnavailable as exc:
        await _record_groq_telemetry(
            runtime,
            telemetry=exc.telemetry,
            fallback_requested_at=requested_at,
        )
        await message.reply_text(
            "A interpretação automática está indisponível agora. Tente novamente em alguns instantes."
        )
        return

    await _record_groq_telemetry(
        runtime,
        telemetry=result.telemetry,
        fallback_requested_at=requested_at,
    )
    intent = result.intent

    if forced_event_id is not None and intent.action in {"update_event", "delete_event"}:
        intent = ParsedIntent(
            action=intent.action,
            event=intent.event,
            update=intent.update,
            query=intent.query,
            event_id=forced_event_id,
            target_title=intent.target_title,
            missing_fields=tuple(
                field for field in intent.missing_fields if field != "target_event"
            ),
        )

    if intent.action == "create_event":
        if intent.event is not None:
            _clear_flow(context)
            await _propose_create(update, context, intent.event)
            return
        await _continue_missing_flow(context, message, intent, text, requested_at)
        return

    if intent.action == "update_event":
        target, ambiguous = await _resolve_target(runtime, intent)
        if target is None:
            await _save_target_flow_and_reply(
                context,
                message,
                mode="update",
                text=text,
                now=requested_at,
                ambiguous=ambiguous,
            )
            return
        if intent.update is None or not intent.update.has_changes:
            _save_flow(
                context,
                mode="update",
                text=text,
                event_id=target.id,
                now=requested_at,
                ttl_seconds=runtime.config.pending_action_ttl_seconds,
            )
            await message.reply_text("O que você quer mudar nesse compromisso?")
            return
        try:
            updated = build_updated_event(
                current_title=target.title,
                current_starts_at=target.starts_at,
                current_duration_minutes=target.duration_minutes,
                update=intent.update,
                now=requested_at,
                local_timezone=runtime.config.timezone,
            )
        except DomainValidationError as exc:
            await message.reply_text(str(exc))
            return
        _clear_flow(context)
        await _propose_update(update, context, target, updated)
        return

    if intent.action == "delete_event":
        target, ambiguous = await _resolve_target(runtime, intent)
        if target is None:
            await _save_target_flow_and_reply(
                context,
                message,
                mode="delete",
                text=text,
                now=requested_at,
                ambiguous=ambiguous,
            )
            return
        _clear_flow(context)
        await _propose_delete(update, context, target.id)
        return

    if intent.action == "list_events" and intent.query is not None:
        _clear_flow(context)
        await _respond_with_events(message, runtime, intent.query)
        return

    if intent.action == "suggest_time" and intent.query is not None:
        _clear_flow(context)
        query = intent.query
        explicit_range = _requested_day_range(text, now=requested_at, runtime=runtime)
        if explicit_range is not None:
            query = AgendaQuery(
                range_start=explicit_range[0],
                range_end=explicit_range[1],
                duration_minutes=query.duration_minutes,
                preferred_period=query.preferred_period,
                result_limit=query.result_limit,
            )
        await _respond_with_slots(message, runtime, query)
        return

    if intent.action == "rate_limits":
        context.user_data["skip_limits_footer_update_id"] = update.update_id
        await _respond_with_rate_limits(message, runtime)
        return

    await message.reply_text(
        "Não entendi. Você quer agendar, editar, excluir, consultar a agenda ou encontrar um horário livre?"
    )


async def _continue_missing_flow(
    context: CallbackContext,
    message: object,
    intent: ParsedIntent,
    text: str,
    now: datetime,
) -> None:
    runtime = _runtime(context)
    _save_flow(
        context,
        mode="create",
        text=text,
        now=now,
        ttl_seconds=runtime.config.pending_action_ttl_seconds,
    )
    reply_text = getattr(message, "reply_text")
    missing = set(intent.missing_fields)
    if {"title", "starts_at"}.issubset(missing):
        await reply_text("O que devo agendar e para qual dia e horário?")
    elif "title" in missing:
        await reply_text("O que devo colocar na agenda?")
    else:
        await reply_text("Qual dia e horário devo usar?")


async def _resolve_target(
    runtime: Runtime,
    intent: ParsedIntent,
) -> tuple[EventRecord | None, list[EventRecord]]:
    if intent.event_id is not None:
        try:
            return (
                await asyncio.to_thread(
                    runtime.repository.get_event,
                    owner_user_id=runtime.config.authorized_user_id,
                    event_id=intent.event_id,
                    only_scheduled=True,
                ),
                [],
            )
        except EventNotFound:
            return None, []

    if not intent.target_title:
        return None, []

    events = await asyncio.to_thread(
        runtime.repository.list_upcoming,
        owner_user_id=runtime.config.authorized_user_id,
        now=utc_now(),
        limit=100,
    )
    needle = _normalize_text(intent.target_title)
    matches = [event for event in events if needle in _normalize_text(event.title)]
    if len(matches) == 1:
        return matches[0], []
    return None, matches[:10]


async def _save_target_flow_and_reply(
    context: CallbackContext,
    message: object,
    *,
    mode: str,
    text: str,
    now: datetime,
    ambiguous: list[EventRecord],
) -> None:
    runtime = _runtime(context)
    _save_flow(
        context,
        mode=mode,
        text=text,
        now=now,
        ttl_seconds=runtime.config.pending_action_ttl_seconds,
    )
    reply_text = getattr(message, "reply_text")
    if ambiguous:
        lines = ["Encontrei mais de um compromisso. Responda com o número:"]
        for event in ambiguous:
            lines.append(f"\n#{event.id} — {event.title}\n{_local_datetime(event.starts_at, runtime)}")
        await reply_text("\n".join(lines))
    else:
        verb = "editar" if mode == "update" else "excluir"
        await reply_text(f"Qual compromisso você quer {verb}? Você pode informar o número ou o nome.")


async def _record_groq_telemetry(
    runtime: Runtime,
    *,
    telemetry: GroqTelemetry | None,
    fallback_requested_at: datetime,
) -> None:
    if telemetry is None:
        requested_at = fallback_requested_at
        total_tokens = 0
        rpd_limit = rpd_remaining = tpm_limit = tpm_remaining = None
        rpd_reset = tpm_reset = None
    else:
        requested_at = telemetry.requested_at
        total_tokens = telemetry.total_tokens
        headers = telemetry.rate_headers
        rpd_limit = headers.rpd_limit
        rpd_remaining = headers.rpd_remaining
        rpd_reset = headers.rpd_reset
        tpm_limit = headers.tpm_limit
        tpm_remaining = headers.tpm_remaining
        tpm_reset = headers.tpm_reset
    try:
        await asyncio.to_thread(
            runtime.repository.record_groq_call,
            model=runtime.config.groq_model,
            requested_at=requested_at,
            total_tokens=total_tokens,
            rpd_limit=rpd_limit,
            rpd_remaining=rpd_remaining,
            rpd_reset=rpd_reset,
            tpm_limit=tpm_limit,
            tpm_remaining=tpm_remaining,
            tpm_reset=tpm_reset,
        )
    except RepositoryError:
        logger.error("Falha ao persistir telemetria da Groq.")


async def _respond_with_events(message: object, runtime: Runtime, query: AgendaQuery) -> None:
    reply_text = getattr(message, "reply_text")
    events = await asyncio.to_thread(
        runtime.repository.list_between,
        owner_user_id=runtime.config.authorized_user_id,
        range_start=query.range_start,
        range_end=query.range_end,
        limit=query.result_limit,
    )
    if not events:
        await reply_text("Você não tem compromissos agendados nesse período.")
        return
    lines = ["Sua agenda"]
    for index, event in enumerate(events, start=1):
        lines.append(
            f"\n{index}. #{event.id} — {event.title}\n"
            f"   {_local_datetime(event.starts_at, runtime)}"
        )
    await reply_text("\n".join(lines))


async def _respond_with_slots(message: object, runtime: Runtime, query: AgendaQuery) -> None:
    reply_text = getattr(message, "reply_text")
    events = await asyncio.to_thread(
        runtime.repository.list_between,
        owner_user_id=runtime.config.authorized_user_id,
        range_start=query.range_start,
        range_end=query.range_end,
        limit=500,
    )
    busy = [BusyInterval(starts_at=e.starts_at, ends_at=e.ends_at) for e in events]
    slots = find_available_slots(
        range_start=query.range_start,
        range_end=query.range_end,
        duration_minutes=query.duration_minutes,
        preferred_period=query.preferred_period,
        result_limit=query.result_limit,
        busy_intervals=busy,
        local_timezone=runtime.config.timezone,
        workday_start=runtime.config.workday_start,
        workday_end=runtime.config.workday_end,
        interval_minutes=runtime.config.slot_interval_minutes,
        buffer_minutes=runtime.config.event_buffer_minutes,
        minimum_lead_minutes=runtime.config.minimum_lead_minutes,
        now=utc_now(),
    )
    if not slots:
        await reply_text("Não encontrei um horário livre nesse período.")
        return
    lines = [f"Horários livres para {query.duration_minutes} minutos:"]
    for index, slot in enumerate(slots, start=1):
        lines.append(f"\n{index}. {_local_datetime(slot.starts_at, runtime)}")
    await reply_text("\n".join(lines))


async def _respond_with_rate_limits(message: object, runtime: Runtime) -> None:
    status = await _get_rate_limit_status(runtime)
    await getattr(message, "reply_text")(_format_rate_limits(status, runtime))


def _format_rate_limits(status: RateLimitStatus, runtime: Runtime) -> str:
    rpd = str(status.rpd_remaining) if status.rpd_remaining is not None else "indisponível"
    tpm = str(status.tpm_remaining) if status.tpm_remaining is not None else "indisponível"
    return (
        "Uso detalhado da Groq\n\n"
        f"Requisições: ~{status.rpm_remaining_estimate}/min · {rpd}/dia\n"
        f"Tokens: {tpm}/min · ~{status.tpd_remaining_estimate}/dia\n\n"
        "Os valores por minuto e tokens por dia são estimativas locais."
    )


async def _propose_create(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    event: EventDraft,
) -> None:
    message = update.effective_message
    if message is None:
        return
    runtime = _runtime(context)
    token = await asyncio.to_thread(
        runtime.repository.create_pending_event,
        owner_user_id=runtime.config.authorized_user_id,
        chat_id=runtime.config.authorized_user_id,
        event=event,
        now=utc_now(),
        ttl_seconds=runtime.config.pending_action_ttl_seconds,
    )
    await message.reply_text(
        "Confirmar compromisso?\n\n"
        f"{event.title}\n"
        f"{_local_datetime(event.starts_at, runtime)}\n"
        f"Duração: {event.duration_minutes} minutos\n\n"
        "Avisos: 30 e 15 minutos antes",
        reply_markup=_confirmation_keyboard(token),
    )


async def _propose_update(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    current: EventRecord,
    changed: EventDraft,
) -> None:
    message = update.effective_message
    if message is None:
        return
    runtime = _runtime(context)
    token, _ = await asyncio.to_thread(
        runtime.repository.create_pending_update,
        owner_user_id=runtime.config.authorized_user_id,
        chat_id=runtime.config.authorized_user_id,
        event_id=current.id,
        event=changed,
        now=utc_now(),
        ttl_seconds=runtime.config.pending_action_ttl_seconds,
    )
    await message.reply_text(
        "Confirmar alteração?\n\n"
        "Antes:\n"
        f"{current.title}\n{_local_datetime(current.starts_at, runtime)}\n\n"
        "Depois:\n"
        f"{changed.title}\n{_local_datetime(changed.starts_at, runtime)}\n"
        f"Duração: {changed.duration_minutes} minutos\n\n"
        "Os avisos serão recalculados para 30 e 15 minutos antes.",
        reply_markup=_confirmation_keyboard(token),
    )


async def _propose_delete(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    event_id: int,
) -> None:
    message = update.effective_message
    if message is None:
        return
    runtime = _runtime(context)
    try:
        token, event = await asyncio.to_thread(
            runtime.repository.create_pending_delete,
            owner_user_id=runtime.config.authorized_user_id,
            chat_id=runtime.config.authorized_user_id,
            event_id=event_id,
            now=utc_now(),
            ttl_seconds=runtime.config.pending_action_ttl_seconds,
        )
    except EventNotFound:
        await message.reply_text("Não encontrei esse compromisso.")
        return
    await message.reply_text(
        "Confirmar exclusão?\n\n"
        f"#{event.id} — {event.title}\n"
        f"{_local_datetime(event.starts_at, runtime)}",
        reply_markup=_confirmation_keyboard(token, destructive=True),
    )


async def callback_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not isinstance(query.data, str):
        return
    match = _CALLBACK_PATTERN.fullmatch(query.data)
    if match is None:
        await query.answer("Ação inválida.", show_alert=True)
        return
    await query.answer()
    decision, token = match.groups()
    runtime = _runtime(context)
    now = utc_now()

    if decision == "no":
        discarded = await asyncio.to_thread(
            runtime.repository.discard_pending,
            token=token,
            owner_user_id=runtime.config.authorized_user_id,
            chat_id=runtime.config.authorized_user_id,
            now=now,
        )
        await query.edit_message_text(
            "Tudo bem. Não fiz nenhuma alteração."
            if discarded
            else "Essa confirmação expirou. Envie o pedido novamente."
        )
        return

    try:
        result = await asyncio.to_thread(
            runtime.repository.execute_pending,
            token=token,
            owner_user_id=runtime.config.authorized_user_id,
            chat_id=runtime.config.authorized_user_id,
            now=now,
        )
    except PendingActionInvalid:
        await query.edit_message_text("Essa confirmação expirou ou já foi usada.")
        return
    except EventNotFound:
        await query.edit_message_text("Compromisso não encontrado ou já alterado.")
        return
    except RepositoryError:
        logger.error("Falha controlada ao executar ação pendente.")
        await query.edit_message_text("Não foi possível concluir a operação.")
        return

    if result.action == "create_event":
        text = "Compromisso agendado."
    elif result.action == "update_event":
        text = "Compromisso atualizado."
    else:
        await query.edit_message_text(f"Compromisso #{result.event_id} excluído.")
        return
    await query.edit_message_text(
        f"{text}\n\n#{result.event_id} — {result.title}\n"
        f"{_local_datetime(cast(datetime, result.starts_at), runtime)}\n\n"
        "Avisos: 30 e 15 minutos antes"
    )


async def unsupported_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is not None:
        await message.reply_text("Envie uma mensagem de texto.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error_name = type(context.error).__name__ if context.error else "UnknownError"
    logger.error("Erro não tratado no bot: %s", error_name)


async def post_init(application: Application) -> None:
    await application.bot.delete_webhook(drop_pending_updates=False)
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Ajuda"),
            BotCommand("agenda", "Ver compromissos"),
            BotCommand("agendar", "Criar compromisso"),
            BotCommand("editar", "Editar compromisso"),
            BotCommand("excluir", "Excluir compromisso"),
            BotCommand("horarios", "Encontrar horários livres"),
            BotCommand("limites", "Ver uso da Groq"),
        ]
    )


def build_application(config: AppConfig) -> Application:
    repository = EventRepository(config.database_path, config.timezone)
    repository.initialize()
    if config.groq_api_key is None:
        raise ConfigurationError("Chave Groq obrigatória para o bot.")

    runtime = Runtime(
        config=config,
        repository=repository,
        parser=GroqCalendarParser(
            api_key=config.groq_api_key,
            url=config.groq_url,
            model=config.groq_model,
            local_timezone=config.timezone,
        ),
        limiter=SlidingWindowRateLimiter(),
    )

    application = (
        Application.builder()
        .token(config.telegram_token)
        .concurrent_updates(False)
        .connect_timeout(5.0)
        .read_timeout(35.0)
        .write_timeout(10.0)
        .pool_timeout(5.0)
        .post_init(post_init)
        .build()
    )
    application.bot_data["runtime"] = runtime

    application.add_handler(TypeHandler(Update, authorize_update), group=-1)
    application.add_handler(CommandHandler(["start", "ajuda"], start_command), group=0)
    application.add_handler(CommandHandler("status", status_command), group=0)
    application.add_handler(CommandHandler(["agenda", "listar"], list_command), group=0)
    application.add_handler(CommandHandler(["horarios", "livre"], free_command), group=0)
    application.add_handler(CommandHandler("limites", limits_command), group=0)
    application.add_handler(CommandHandler("agendar", schedule_command), group=0)
    application.add_handler(CommandHandler("editar", edit_command), group=0)
    application.add_handler(CommandHandler(["excluir", "cancelar"], delete_command), group=0)
    application.add_handler(CommandHandler("agendar_exato", exact_schedule_command), group=0)
    application.add_handler(CallbackQueryHandler(callback_action), group=0)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, natural_language_message),
        group=0,
    )
    application.add_handler(MessageHandler(filters.ALL, unsupported_message), group=0)
    application.add_handler(TypeHandler(Update, limits_footer_handler), group=100)
    application.add_error_handler(error_handler)
    return application


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    try:
        config = AppConfig.load(require_llm=True)
        application = build_application(config)
        application.run_polling(
            poll_interval=0.5,
            timeout=30,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=False,
            close_loop=True,
        )
    except ConfigurationError as exc:
        logger.critical("Configuração inválida: %s", exc)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
