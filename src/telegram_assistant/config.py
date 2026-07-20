from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
import os
from pathlib import Path
import stat
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigurationError(RuntimeError):
    """Configuração ausente, insegura ou inválida."""


def _read_systemd_credential(name: str) -> str:
    credentials_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if not credentials_dir:
        raise ConfigurationError(
            "CREDENTIALS_DIRECTORY ausente. Em produção, use LoadCredential= do systemd."
        )

    base = Path(credentials_dir)
    path = base / name

    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Credencial obrigatória ausente: {name}") from exc

    if stat.S_ISLNK(metadata.st_mode):
        raise ConfigurationError(f"Credencial não pode ser symlink: {name}")
    if not stat.S_ISREG(metadata.st_mode):
        raise ConfigurationError(f"Credencial não é arquivo regular: {name}")
    # LoadCredential= protege a cópia temporária pelo diretório privado do systemd.
    if metadata.st_mode & 0o022:
        raise ConfigurationError(
            f"Credencial gravável por grupo ou outros: {name}"
        )
    if metadata.st_mode & 0o111:
        raise ConfigurationError(
            f"Credencial marcada como executável: {name}"
        )

    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ConfigurationError(f"Credencial vazia: {name}")
    if len(value) > 4096:
        raise ConfigurationError(f"Credencial excede o limite: {name}")
    return value


def _read_development_secret(credential_name: str, environment_name: str) -> str:
    if os.environ.get("APP_ENV", "development") == "production":
        return _read_systemd_credential(credential_name)

    value = os.environ.get(environment_name, "").strip()
    if not value:
        raise ConfigurationError(
            f"Defina {environment_name} no desenvolvimento ou use credenciais do systemd."
        )
    return value


def _read_bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} deve ser inteiro.") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} deve ficar entre {minimum} e {maximum}.")
    return value


def _read_clock(name: str, default: str) -> time:
    raw = os.environ.get(name, default).strip()
    try:
        hour_text, minute_text = raw.split(":", maxsplit=1)
        parsed = time(hour=int(hour_text), minute=int(minute_text))
    except (ValueError, TypeError) as exc:
        raise ConfigurationError(f"{name} deve usar HH:MM.") from exc
    return parsed


@dataclass(frozen=True, slots=True)
class AppConfig:
    telegram_token: str = field(repr=False)
    authorized_user_id: int
    database_path: Path
    timezone: ZoneInfo
    groq_api_key: str | None = field(default=None, repr=False)

    groq_url: str = "https://api.groq.com/openai/v1/chat/completions"
    groq_model: str = "openai/gpt-oss-20b"
    max_message_chars: int = 1000
    pending_action_ttl_seconds: int = 600

    # Contexto máximo enviado à LLM em cada interação de linguagem natural.
    llm_agenda_days: int = 30
    llm_agenda_max_events: int = 50

    workday_start: time = time(9, 0)
    workday_end: time = time(18, 0)
    slot_interval_minutes: int = 15
    event_buffer_minutes: int = 15
    minimum_lead_minutes: int = 30

    # Defaults atuais do plano gratuito para openai/gpt-oss-20b.
    # São configuráveis porque o provedor pode alterá-los.
    groq_rpm_limit: int = 30
    groq_rpd_limit: int = 1000
    groq_tpm_limit: int = 8000
    groq_tpd_limit: int = 200000

    @classmethod
    def load(cls, *, require_llm: bool) -> "AppConfig":
        telegram_token = _read_development_secret(
            "telegram_token", "TELEGRAM_BOT_TOKEN"
        )
        if any(character.isspace() for character in telegram_token):
            raise ConfigurationError("Token do Telegram contém espaço em branco.")
        if not 20 <= len(telegram_token) <= 256:
            raise ConfigurationError("Token do Telegram possui tamanho inválido.")

        user_id_text = _read_development_secret(
            "authorized_user_id", "AUTHORIZED_TELEGRAM_USER_ID"
        )
        try:
            authorized_user_id = int(user_id_text)
        except ValueError as exc:
            raise ConfigurationError(
                "AUTHORIZED_TELEGRAM_USER_ID deve ser inteiro."
            ) from exc
        if authorized_user_id <= 0:
            raise ConfigurationError("AUTHORIZED_TELEGRAM_USER_ID deve ser positivo.")

        database_path = Path(
            os.environ.get("APP_DB_PATH", "/var/lib/telegram-assistant/events.db")
        )
        if not database_path.is_absolute():
            raise ConfigurationError("APP_DB_PATH deve ser absoluto.")

        timezone_name = os.environ.get("APP_TIMEZONE", "America/Sao_Paulo")
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ConfigurationError(f"Timezone inválido: {timezone_name}") from exc

        workday_start = _read_clock("APP_WORKDAY_START", "09:00")
        workday_end = _read_clock("APP_WORKDAY_END", "18:00")
        if workday_start >= workday_end:
            raise ConfigurationError("APP_WORKDAY_START deve ser anterior ao fim.")

        groq_api_key: str | None = None
        if require_llm:
            groq_api_key = _read_development_secret("groq_api_key", "GROQ_API_KEY")
            if any(character.isspace() for character in groq_api_key):
                raise ConfigurationError("Chave Groq contém espaço em branco.")
            if len(groq_api_key) < 20:
                raise ConfigurationError("Chave Groq possui tamanho inválido.")

        return cls(
            telegram_token=telegram_token,
            authorized_user_id=authorized_user_id,
            database_path=database_path,
            timezone=timezone,
            groq_api_key=groq_api_key,
            max_message_chars=_read_bounded_int(
                "APP_MAX_MESSAGE_CHARS", 1000, 100, 4000
            ),
            pending_action_ttl_seconds=_read_bounded_int(
                "APP_PENDING_TTL_SECONDS", 600, 60, 1800
            ),
            llm_agenda_days=_read_bounded_int("APP_LLM_AGENDA_DAYS", 30, 1, 90),
            llm_agenda_max_events=_read_bounded_int(
                "APP_LLM_AGENDA_MAX_EVENTS", 50, 1, 100
            ),
            workday_start=workday_start,
            workday_end=workday_end,
            slot_interval_minutes=_read_bounded_int(
                "APP_SLOT_INTERVAL_MINUTES", 15, 5, 60
            ),
            event_buffer_minutes=_read_bounded_int(
                "APP_EVENT_BUFFER_MINUTES", 15, 0, 120
            ),
            minimum_lead_minutes=_read_bounded_int(
                "APP_MINIMUM_LEAD_MINUTES", 30, 0, 1440
            ),
            groq_rpm_limit=_read_bounded_int("GROQ_RPM_LIMIT", 30, 1, 1_000_000),
            groq_rpd_limit=_read_bounded_int("GROQ_RPD_LIMIT", 1000, 1, 10_000_000),
            groq_tpm_limit=_read_bounded_int("GROQ_TPM_LIMIT", 8000, 1, 100_000_000),
            groq_tpd_limit=_read_bounded_int(
                "GROQ_TPD_LIMIT", 200000, 1, 1_000_000_000
            ),
        )
