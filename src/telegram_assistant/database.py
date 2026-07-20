from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from .domain import (
    EventDraft,
    REMINDER_OFFSETS_MINUTES,
    from_utc_text,
    to_utc_text,
)


class RepositoryError(RuntimeError):
    """Falha controlada na persistência."""


class PendingActionInvalid(RepositoryError):
    """Ação ausente, expirada, consumida ou vinculada a outro usuário."""


class EventNotFound(RepositoryError):
    """Evento não localizado no escopo do usuário."""


@dataclass(frozen=True, slots=True)
class EventRecord:
    id: int
    title: str
    starts_at: datetime
    ends_at: datetime
    reminder_at: datetime
    status: str

    @property
    def duration_minutes(self) -> int:
        return max(1, int((self.ends_at - self.starts_at).total_seconds() // 60))


@dataclass(frozen=True, slots=True)
class ReminderRecord:
    reminder_id: int
    event_id: int
    title: str
    starts_at: datetime
    minutes_before: int
    due_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    action: str
    event_id: int
    title: str
    starts_at: datetime | None


@dataclass(frozen=True, slots=True)
class RateLimitStatus:
    observed_at: datetime | None
    rpm_limit: int
    rpm_remaining_estimate: int
    rpd_limit: int
    rpd_remaining: int | None
    rpd_reset: str | None
    tpm_limit: int
    tpm_remaining: int | None
    tpm_reset: str | None
    tpd_limit: int
    tpd_remaining_estimate: int
    local_requests_last_minute: int
    local_tokens_last_24_hours: int


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class EventRepository:
    def __init__(self, path: Path, local_timezone: ZoneInfo) -> None:
        if not path.is_absolute():
            raise ValueError("Database path deve ser absoluto.")
        self._path = path
        self._local_timezone = local_timezone

    def _connect(self) -> sqlite3.Connection:
        if self._path.exists() and self._path.is_symlink():
            raise RepositoryError("Banco não pode ser symlink.")

        connection = sqlite3.connect(
            self._path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            self._create_base_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._migrate_pending_actions(connection)
                self._create_reminder_schema(connection)
                self._backfill_reminders(connection)
                connection.execute("PRAGMA user_version = 3")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _create_base_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 120),
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                reminder_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('scheduled', 'cancelled')),
                reminder_sent_at TEXT,
                reminder_claimed_at TEXT,
                reminder_next_attempt_at TEXT,
                reminder_retry_count INTEGER NOT NULL DEFAULT 0
                    CHECK(reminder_retry_count >= 0),
                reminder_failed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_events_owner_start
                ON events(owner_user_id, status, starts_at);

            CREATE TABLE IF NOT EXISTS groq_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requested_at TEXT NOT NULL,
                total_tokens INTEGER NOT NULL DEFAULT 0 CHECK(total_tokens >= 0)
            );

            CREATE INDEX IF NOT EXISTS idx_groq_usage_requested_at
                ON groq_usage(requested_at);

            CREATE TABLE IF NOT EXISTS groq_rate_snapshot (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                model TEXT NOT NULL CHECK(length(model) BETWEEN 1 AND 128),
                observed_at TEXT NOT NULL,
                rpd_limit INTEGER,
                rpd_remaining INTEGER,
                rpd_reset TEXT,
                tpm_limit INTEGER,
                tpm_remaining INTEGER,
                tpm_reset TEXT
            );
            """
        )

    @staticmethod
    def _create_pending_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE pending_actions (
                token_hash TEXT PRIMARY KEY CHECK(length(token_hash) = 64),
                owner_user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                action TEXT NOT NULL CHECK(
                    action IN (
                        'create_event',
                        'update_event',
                        'delete_event',
                        'cancel_event'
                    )
                ),
                payload_json TEXT NOT NULL CHECK(length(payload_json) <= 4096),
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_expiration "
            "ON pending_actions(expires_at, consumed_at)"
        )

    def _migrate_pending_actions(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='pending_actions'"
        ).fetchone()
        if row is None:
            self._create_pending_table(connection)
            return

        sql = str(row["sql"] or "")
        if "update_event" in sql and "delete_event" in sql:
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_pending_expiration "
                "ON pending_actions(expires_at, consumed_at)"
            )
            return

        connection.execute("ALTER TABLE pending_actions RENAME TO pending_actions_v2")
        self._create_pending_table(connection)
        connection.execute(
            """
            INSERT INTO pending_actions (
                token_hash, owner_user_id, chat_id, action, payload_json,
                expires_at, consumed_at, created_at
            )
            SELECT
                token_hash,
                owner_user_id,
                chat_id,
                CASE WHEN action = 'cancel_event' THEN 'delete_event' ELSE action END,
                payload_json,
                expires_at,
                consumed_at,
                created_at
            FROM pending_actions_v2
            WHERE action IN ('create_event', 'cancel_event')
            """
        )
        connection.execute("DROP TABLE pending_actions_v2")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_expiration "
            "ON pending_actions(expires_at, consumed_at)"
        )

    @staticmethod
    def _create_reminder_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS event_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                minutes_before INTEGER NOT NULL CHECK(minutes_before IN (15, 30)),
                due_at TEXT NOT NULL,
                sent_at TEXT,
                claimed_at TEXT,
                next_attempt_at TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
                failed_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(event_id, minutes_before)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_reminders_due "
            "ON event_reminders(sent_at, failed_at, due_at)"
        )

    def _backfill_reminders(self, connection: sqlite3.Connection) -> None:
        now = datetime.now(timezone.utc)
        rows = connection.execute(
            """
            SELECT e.id, e.starts_at
            FROM events e
            WHERE e.status = 'scheduled'
              AND e.starts_at > ?
              AND NOT EXISTS (
                    SELECT 1 FROM event_reminders r WHERE r.event_id = e.id
              )
            """,
            (to_utc_text(now),),
        ).fetchall()
        for row in rows:
            self._insert_reminders(
                connection,
                event_id=int(row["id"]),
                starts_at=from_utc_text(row["starts_at"]),
                now=now,
            )

    @staticmethod
    def _insert_reminders(
        connection: sqlite3.Connection,
        *,
        event_id: int,
        starts_at: datetime,
        now: datetime,
    ) -> None:
        if starts_at <= now + timedelta(minutes=1):
            return

        schedule: list[tuple[int, datetime]] = []
        due_30 = starts_at - timedelta(minutes=30)
        due_15 = starts_at - timedelta(minutes=15)

        if due_30 > now:
            schedule = [(30, due_30), (15, due_15)]
        elif due_15 > now:
            # O primeiro horário já passou: entrega o primeiro aviso assim que o
            # worker executar e preserva o segundo no horário correto.
            schedule = [(30, now), (15, due_15)]
        else:
            # Muito próximo do compromisso: um único aviso imediato evita duas
            # notificações iguais no mesmo instante.
            schedule = [(15, now)]

        created_at = to_utc_text(now)
        connection.executemany(
            """
            INSERT OR REPLACE INTO event_reminders (
                event_id, minutes_before, due_at, sent_at, claimed_at,
                next_attempt_at, retry_count, failed_at, created_at
            ) VALUES (?, ?, ?, NULL, NULL, NULL, 0, NULL, ?)
            """,
            [
                (event_id, minutes, to_utc_text(due_at), created_at)
                for minutes, due_at in schedule
            ],
        )

    def create_pending_event(
        self,
        *,
        owner_user_id: int,
        chat_id: int,
        event: EventDraft,
        now: datetime,
        ttl_seconds: int,
    ) -> str:
        return self._create_pending(
            owner_user_id=owner_user_id,
            chat_id=chat_id,
            action="create_event",
            payload=event.to_payload(),
            now=now,
            ttl_seconds=ttl_seconds,
        )

    def create_pending_update(
        self,
        *,
        owner_user_id: int,
        chat_id: int,
        event_id: int,
        event: EventDraft,
        now: datetime,
        ttl_seconds: int,
    ) -> tuple[str, EventRecord]:
        current = self.get_event(
            owner_user_id=owner_user_id,
            event_id=event_id,
            only_scheduled=True,
        )
        payload = {"event_id": event_id, "event": event.to_payload()}
        token = self._create_pending(
            owner_user_id=owner_user_id,
            chat_id=chat_id,
            action="update_event",
            payload=payload,
            now=now,
            ttl_seconds=ttl_seconds,
        )
        return token, current

    def create_pending_delete(
        self,
        *,
        owner_user_id: int,
        chat_id: int,
        event_id: int,
        now: datetime,
        ttl_seconds: int,
    ) -> tuple[str, EventRecord]:
        event = self.get_event(
            owner_user_id=owner_user_id,
            event_id=event_id,
            only_scheduled=True,
        )
        token = self._create_pending(
            owner_user_id=owner_user_id,
            chat_id=chat_id,
            action="delete_event",
            payload={"event_id": event_id},
            now=now,
            ttl_seconds=ttl_seconds,
        )
        return token, event

    # Alias para instalações e testes de versões anteriores.
    def create_pending_cancel(self, **kwargs: Any) -> tuple[str, EventRecord]:
        return self.create_pending_delete(**kwargs)

    def _create_pending(
        self,
        *,
        owner_user_id: int,
        chat_id: int,
        action: str,
        payload: dict[str, Any],
        now: datetime,
        ttl_seconds: int,
    ) -> str:
        if action not in {"create_event", "update_event", "delete_event"}:
            raise RepositoryError("Ação pendente não permitida.")
        if not 60 <= ttl_seconds <= 1800:
            raise RepositoryError("TTL pendente fora do limite.")

        token = secrets.token_urlsafe(16)
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(payload_json.encode("utf-8")) > 4096:
            raise RepositoryError("Payload pendente excede o limite.")

        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO pending_actions (
                    token_hash, owner_user_id, chat_id, action, payload_json,
                    expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _token_hash(token),
                    owner_user_id,
                    chat_id,
                    action,
                    payload_json,
                    to_utc_text(now + timedelta(seconds=ttl_seconds)),
                    to_utc_text(now),
                ),
            )
        return token

    def discard_pending(
        self,
        *,
        token: str,
        owner_user_id: int,
        chat_id: int,
        now: datetime,
    ) -> bool:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE pending_actions
                SET consumed_at = ?
                WHERE token_hash = ?
                  AND owner_user_id = ?
                  AND chat_id = ?
                  AND consumed_at IS NULL
                  AND expires_at > ?
                """,
                (
                    to_utc_text(now),
                    _token_hash(token),
                    owner_user_id,
                    chat_id,
                    to_utc_text(now),
                ),
            )
            return cursor.rowcount == 1

    def execute_pending(
        self,
        *,
        token: str,
        owner_user_id: int,
        chat_id: int,
        now: datetime,
    ) -> ExecutionResult:
        now_text = to_utc_text(now)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT action, payload_json
                    FROM pending_actions
                    WHERE token_hash = ?
                      AND owner_user_id = ?
                      AND chat_id = ?
                      AND consumed_at IS NULL
                      AND expires_at > ?
                    """,
                    (_token_hash(token), owner_user_id, chat_id, now_text),
                ).fetchone()
                if row is None:
                    raise PendingActionInvalid("Confirmação inválida ou expirada.")

                try:
                    payload = json.loads(row["payload_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise PendingActionInvalid("Payload pendente inválido.") from exc
                if not isinstance(payload, dict):
                    raise PendingActionInvalid("Payload pendente inválido.")

                action = str(row["action"])
                if action == "create_event":
                    result = self._execute_create(
                        connection,
                        owner_user_id=owner_user_id,
                        chat_id=chat_id,
                        payload=payload,
                        now=now,
                    )
                elif action == "update_event":
                    result = self._execute_update(
                        connection,
                        owner_user_id=owner_user_id,
                        payload=payload,
                        now=now,
                    )
                elif action in {"delete_event", "cancel_event"}:
                    result = self._execute_delete(
                        connection,
                        owner_user_id=owner_user_id,
                        payload=payload,
                        now=now,
                    )
                else:
                    raise PendingActionInvalid("Ação pendente não suportada.")

                cursor = connection.execute(
                    """
                    UPDATE pending_actions
                    SET consumed_at = ?
                    WHERE token_hash = ? AND consumed_at IS NULL
                    """,
                    (now_text, _token_hash(token)),
                )
                if cursor.rowcount != 1:
                    raise PendingActionInvalid("Confirmação já utilizada.")
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def _execute_create(
        self,
        connection: sqlite3.Connection,
        *,
        owner_user_id: int,
        chat_id: int,
        payload: dict[str, Any],
        now: datetime,
    ) -> ExecutionResult:
        event = EventDraft.from_payload(
            payload,
            now=now,
            local_timezone=self._local_timezone,
        )
        now_text = to_utc_text(now)
        cursor = connection.execute(
            """
            INSERT INTO events (
                owner_user_id, chat_id, title, starts_at, ends_at,
                reminder_at, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?, ?)
            """,
            (
                owner_user_id,
                chat_id,
                event.title,
                to_utc_text(event.starts_at),
                to_utc_text(event.ends_at),
                to_utc_text(event.reminder_at),
                now_text,
                now_text,
            ),
        )
        event_id = int(cursor.lastrowid)
        self._insert_reminders(
            connection,
            event_id=event_id,
            starts_at=event.starts_at,
            now=now,
        )
        return ExecutionResult(
            action="create_event",
            event_id=event_id,
            title=event.title,
            starts_at=event.starts_at,
        )

    def _execute_update(
        self,
        connection: sqlite3.Connection,
        *,
        owner_user_id: int,
        payload: dict[str, Any],
        now: datetime,
    ) -> ExecutionResult:
        event_id = payload.get("event_id")
        event_payload = payload.get("event")
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
            raise PendingActionInvalid("ID de atualização inválido.")
        if not isinstance(event_payload, dict):
            raise PendingActionInvalid("Evento atualizado inválido.")

        current = connection.execute(
            """
            SELECT id FROM events
            WHERE id = ? AND owner_user_id = ? AND status = 'scheduled'
            """,
            (event_id, owner_user_id),
        ).fetchone()
        if current is None:
            raise EventNotFound("Compromisso não encontrado.")

        event = EventDraft.from_payload(
            event_payload,
            now=now,
            local_timezone=self._local_timezone,
        )
        now_text = to_utc_text(now)
        connection.execute(
            """
            UPDATE events
            SET title = ?, starts_at = ?, ends_at = ?, reminder_at = ?,
                reminder_sent_at = NULL, reminder_claimed_at = NULL,
                reminder_next_attempt_at = NULL, reminder_retry_count = 0,
                reminder_failed_at = NULL, updated_at = ?
            WHERE id = ? AND owner_user_id = ? AND status = 'scheduled'
            """,
            (
                event.title,
                to_utc_text(event.starts_at),
                to_utc_text(event.ends_at),
                to_utc_text(event.reminder_at),
                now_text,
                event_id,
                owner_user_id,
            ),
        )
        connection.execute("DELETE FROM event_reminders WHERE event_id = ?", (event_id,))
        self._insert_reminders(
            connection,
            event_id=event_id,
            starts_at=event.starts_at,
            now=now,
        )
        return ExecutionResult(
            action="update_event",
            event_id=event_id,
            title=event.title,
            starts_at=event.starts_at,
        )

    @staticmethod
    def _execute_delete(
        connection: sqlite3.Connection,
        *,
        owner_user_id: int,
        payload: dict[str, Any],
        now: datetime,
    ) -> ExecutionResult:
        event_id = payload.get("event_id")
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
            raise PendingActionInvalid("ID de exclusão inválido.")

        row = connection.execute(
            """
            SELECT title, starts_at FROM events
            WHERE id = ? AND owner_user_id = ? AND status = 'scheduled'
            """,
            (event_id, owner_user_id),
        ).fetchone()
        if row is None:
            raise EventNotFound("Compromisso não encontrado.")

        connection.execute(
            """
            UPDATE events
            SET status = 'cancelled', updated_at = ?
            WHERE id = ? AND owner_user_id = ? AND status = 'scheduled'
            """,
            (to_utc_text(now), event_id, owner_user_id),
        )
        connection.execute("DELETE FROM event_reminders WHERE event_id = ?", (event_id,))
        return ExecutionResult(
            action="delete_event",
            event_id=event_id,
            title=row["title"],
            starts_at=from_utc_text(row["starts_at"]),
        )

    def get_event(
        self,
        *,
        owner_user_id: int,
        event_id: int,
        only_scheduled: bool = False,
    ) -> EventRecord:
        query = (
            "SELECT id, title, starts_at, ends_at, reminder_at, status "
            "FROM events WHERE id = ? AND owner_user_id = ?"
        )
        parameters: list[Any] = [event_id, owner_user_id]
        if only_scheduled:
            query += " AND status = 'scheduled'"

        with closing(self._connect()) as connection:
            row = connection.execute(query, parameters).fetchone()
        if row is None:
            raise EventNotFound("Compromisso não encontrado.")
        return self._row_to_event(row)

    def list_upcoming(
        self,
        *,
        owner_user_id: int,
        now: datetime,
        limit: int = 20,
    ) -> list[EventRecord]:
        return self.list_between(
            owner_user_id=owner_user_id,
            range_start=now,
            range_end=now + timedelta(days=366),
            limit=limit,
        )

    def list_between(
        self,
        *,
        owner_user_id: int,
        range_start: datetime,
        range_end: datetime,
        limit: int,
    ) -> list[EventRecord]:
        if range_start.tzinfo is None or range_end.tzinfo is None:
            raise RepositoryError("Intervalo sem timezone.")
        if range_end <= range_start:
            raise RepositoryError("Intervalo inválido.")
        safe_limit = max(1, min(limit, 500))

        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, title, starts_at, ends_at, reminder_at, status
                FROM events
                WHERE owner_user_id = ?
                  AND status = 'scheduled'
                  AND starts_at < ?
                  AND ends_at > ?
                ORDER BY starts_at ASC
                LIMIT ?
                """,
                (
                    owner_user_id,
                    to_utc_text(range_end),
                    to_utc_text(range_start),
                    safe_limit,
                ),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def record_groq_call(
        self,
        *,
        model: str,
        requested_at: datetime,
        total_tokens: int,
        rpd_limit: int | None,
        rpd_remaining: int | None,
        rpd_reset: str | None,
        tpm_limit: int | None,
        tpm_remaining: int | None,
        tpm_reset: str | None,
    ) -> None:
        if requested_at.tzinfo is None:
            raise RepositoryError("Telemetria sem timezone.")
        if not 1 <= len(model) <= 128:
            raise RepositoryError("Modelo inválido na telemetria.")
        if isinstance(total_tokens, bool) or not 0 <= total_tokens <= 10_000_000:
            raise RepositoryError("Tokens inválidos na telemetria.")

        def bounded(value: int | None) -> int | None:
            if value is None:
                return None
            if isinstance(value, bool) or not 0 <= value <= 1_000_000_000:
                return None
            return value

        def safe_reset(value: str | None) -> str | None:
            if value is None or not 1 <= len(value) <= 64:
                return None
            return value

        observed_at = to_utc_text(requested_at)
        normalized_headers = (
            bounded(rpd_limit),
            bounded(rpd_remaining),
            safe_reset(rpd_reset),
            bounded(tpm_limit),
            bounded(tpm_remaining),
            safe_reset(tpm_reset),
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO groq_usage (requested_at, total_tokens) VALUES (?, ?)",
                    (observed_at, total_tokens),
                )
                if any(value is not None for value in normalized_headers):
                    connection.execute(
                        """
                        INSERT INTO groq_rate_snapshot (
                            id, model, observed_at, rpd_limit, rpd_remaining,
                            rpd_reset, tpm_limit, tpm_remaining, tpm_reset
                        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            model = excluded.model,
                            observed_at = excluded.observed_at,
                            rpd_limit = COALESCE(excluded.rpd_limit, groq_rate_snapshot.rpd_limit),
                            rpd_remaining = COALESCE(excluded.rpd_remaining, groq_rate_snapshot.rpd_remaining),
                            rpd_reset = COALESCE(excluded.rpd_reset, groq_rate_snapshot.rpd_reset),
                            tpm_limit = COALESCE(excluded.tpm_limit, groq_rate_snapshot.tpm_limit),
                            tpm_remaining = COALESCE(excluded.tpm_remaining, groq_rate_snapshot.tpm_remaining),
                            tpm_reset = COALESCE(excluded.tpm_reset, groq_rate_snapshot.tpm_reset)
                        """,
                        (model, observed_at, *normalized_headers),
                    )
                connection.execute(
                    "DELETE FROM groq_usage WHERE requested_at < ?",
                    (to_utc_text(requested_at - timedelta(days=8)),),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def get_rate_limit_status(
        self,
        *,
        now: datetime,
        configured_rpm: int,
        configured_rpd: int,
        configured_tpm: int,
        configured_tpd: int,
    ) -> RateLimitStatus:
        if now.tzinfo is None:
            raise RepositoryError("Relógio sem timezone.")

        with closing(self._connect()) as connection:
            usage = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN requested_at >= ? THEN 1 ELSE 0 END) AS requests_last_minute,
                    SUM(CASE WHEN requested_at >= ? THEN total_tokens ELSE 0 END) AS tokens_last_24_hours
                FROM groq_usage
                WHERE requested_at >= ?
                """,
                (
                    to_utc_text(now - timedelta(minutes=1)),
                    to_utc_text(now - timedelta(hours=24)),
                    to_utc_text(now - timedelta(hours=24)),
                ),
            ).fetchone()
            snapshot = connection.execute(
                """
                SELECT observed_at, rpd_limit, rpd_remaining, rpd_reset,
                       tpm_limit, tpm_remaining, tpm_reset
                FROM groq_rate_snapshot WHERE id = 1
                """
            ).fetchone()

        local_requests = int(usage["requests_last_minute"] or 0)
        local_tokens_day = int(usage["tokens_last_24_hours"] or 0)
        observed_at = from_utc_text(snapshot["observed_at"]) if snapshot else None
        return RateLimitStatus(
            observed_at=observed_at,
            rpm_limit=configured_rpm,
            rpm_remaining_estimate=max(configured_rpm - local_requests, 0),
            rpd_limit=(
                int(snapshot["rpd_limit"])
                if snapshot and snapshot["rpd_limit"] is not None
                else configured_rpd
            ),
            rpd_remaining=(
                int(snapshot["rpd_remaining"])
                if snapshot and snapshot["rpd_remaining"] is not None
                else None
            ),
            rpd_reset=snapshot["rpd_reset"] if snapshot else None,
            tpm_limit=(
                int(snapshot["tpm_limit"])
                if snapshot and snapshot["tpm_limit"] is not None
                else configured_tpm
            ),
            tpm_remaining=(
                int(snapshot["tpm_remaining"])
                if snapshot and snapshot["tpm_remaining"] is not None
                else None
            ),
            tpm_reset=snapshot["tpm_reset"] if snapshot else None,
            tpd_limit=configured_tpd,
            tpd_remaining_estimate=max(configured_tpd - local_tokens_day, 0),
            local_requests_last_minute=local_requests,
            local_tokens_last_24_hours=local_tokens_day,
        )

    def claim_due_reminders(
        self,
        *,
        now: datetime,
        stale_after_minutes: int = 10,
        limit: int = 20,
    ) -> list[ReminderRecord]:
        now_text = to_utc_text(now)
        stale_before = to_utc_text(now - timedelta(minutes=stale_after_minutes))
        safe_limit = max(1, min(limit, 100))

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    """
                    SELECT
                        r.id AS reminder_id,
                        r.event_id,
                        r.minutes_before,
                        r.due_at,
                        e.title,
                        e.starts_at
                    FROM event_reminders r
                    JOIN events e ON e.id = r.event_id
                    WHERE e.status = 'scheduled'
                      AND e.starts_at > ?
                      AND r.sent_at IS NULL
                      AND r.failed_at IS NULL
                      AND r.retry_count < 8
                      AND r.due_at <= ?
                      AND (r.next_attempt_at IS NULL OR r.next_attempt_at <= ?)
                      AND (r.claimed_at IS NULL OR r.claimed_at <= ?)
                    ORDER BY r.due_at ASC
                    LIMIT ?
                    """,
                    (now_text, now_text, now_text, stale_before, safe_limit),
                ).fetchall()

                ids = [int(row["reminder_id"]) for row in rows]
                if ids:
                    connection.executemany(
                        "UPDATE event_reminders SET claimed_at = ? WHERE id = ?",
                        [(now_text, reminder_id) for reminder_id in ids],
                    )
                connection.commit()
                return [self._row_to_reminder(row) for row in rows]
            except Exception:
                connection.rollback()
                raise

    def mark_reminder_sent(self, *, reminder_id: int, now: datetime) -> None:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE event_reminders
                SET sent_at = ?, claimed_at = NULL, next_attempt_at = NULL
                WHERE id = ? AND sent_at IS NULL
                """,
                (to_utc_text(now), reminder_id),
            )
            if cursor.rowcount != 1:
                raise EventNotFound("Lembrete não pode ser marcado como enviado.")

    def mark_reminder_failure(
        self,
        *,
        reminder_id: int,
        now: datetime,
        retry_after_seconds: int | None = None,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT retry_count FROM event_reminders
                    WHERE id = ? AND sent_at IS NULL
                    """,
                    (reminder_id,),
                ).fetchone()
                if row is None:
                    raise EventNotFound("Lembrete não localizado.")

                retries = int(row["retry_count"]) + 1
                exhausted = retries >= 8
                delay = retry_after_seconds or min(30 * (2 ** (retries - 1)), 1800)
                next_attempt = now + timedelta(seconds=max(30, min(delay, 3600)))
                connection.execute(
                    """
                    UPDATE event_reminders
                    SET retry_count = ?, claimed_at = NULL,
                        next_attempt_at = ?, failed_at = ?
                    WHERE id = ?
                    """,
                    (
                        retries,
                        None if exhausted else to_utc_text(next_attempt),
                        to_utc_text(now) if exhausted else None,
                        reminder_id,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def purge_expired_pending(self, *, now: datetime) -> int:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                DELETE FROM pending_actions
                WHERE expires_at <= ? OR consumed_at IS NOT NULL
                """,
                (to_utc_text(now - timedelta(days=1)),),
            )
            return cursor.rowcount

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> EventRecord:
        return EventRecord(
            id=int(row["id"]),
            title=row["title"],
            starts_at=from_utc_text(row["starts_at"]),
            ends_at=from_utc_text(row["ends_at"]),
            reminder_at=from_utc_text(row["reminder_at"]),
            status=row["status"],
        )

    @staticmethod
    def _row_to_reminder(row: sqlite3.Row) -> ReminderRecord:
        return ReminderRecord(
            reminder_id=int(row["reminder_id"]),
            event_id=int(row["event_id"]),
            title=row["title"],
            starts_at=from_utc_text(row["starts_at"]),
            minutes_before=int(row["minutes_before"]),
            due_at=from_utc_text(row["due_at"]),
        )
