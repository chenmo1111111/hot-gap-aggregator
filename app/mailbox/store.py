from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


VALID_STATUSES = {"pending", "done", "expired", "needs_review"}
UTC = timezone.utc


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def mailbox_database_path() -> Path:
    configured = os.getenv("MAIL_DEADLINES_DB", "").strip()
    if configured:
        return Path(configured)
    sync_path = Path(os.getenv("SYNC_DB_PATH", "./sync.db"))
    return sync_path.with_name("mail-deadlines.db")


class MailboxStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else mailbox_database_path()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS mail_deadlines (
                    message_id TEXT PRIMARY KEY,
                    company TEXT NOT NULL,
                    type TEXT NOT NULL,
                    deadline_at TEXT,
                    action_url TEXT,
                    summary TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'done', 'expired', 'needs_review')
                    ),
                    created_at TEXT NOT NULL,
                    subject TEXT NOT NULL DEFAULT '',
                    sender TEXT NOT NULL DEFAULT '',
                    deadline_kind TEXT NOT NULL DEFAULT 'unknown',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_mail_deadlines_status_deadline
                    ON mail_deadlines(status, deadline_at);
                CREATE TABLE IF NOT EXISTS mailbox_messages (
                    message_id TEXT PRIMARY KEY,
                    imap_uid INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    processed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mailbox_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mail_notifications (
                    message_id TEXT NOT NULL,
                    threshold TEXT NOT NULL,
                    notified_at TEXT NOT NULL,
                    PRIMARY KEY(message_id, threshold),
                    FOREIGN KEY(message_id) REFERENCES mail_deadlines(message_id)
                        ON DELETE CASCADE
                );
                """
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def get_state(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM mailbox_state WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row is not None else None

    def set_state(self, key: str, value: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO mailbox_state(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

    def is_processed(self, message_id: str) -> bool:
        return self.processed_outcome(message_id) is not None

    def processed_outcome(self, message_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT outcome FROM mailbox_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return str(row["outcome"]) if row is not None else None

    def mark_processed(self, message_id: str, imap_uid: int, outcome: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO mailbox_messages(
                    message_id, imap_uid, outcome, processed_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    imap_uid = excluded.imap_uid,
                    outcome = excluded.outcome,
                    processed_at = excluded.processed_at
                """,
                (message_id, imap_uid, outcome, utc_now()),
            )

    def add_deadline(self, record: Mapping[str, Any], *, imap_uid: int) -> bool:
        status = str(record.get("status") or "needs_review")
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid mail deadline status: {status}")
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO mail_deadlines(
                    message_id, company, type, deadline_at, action_url, summary,
                    received_at, status, created_at, subject, sender,
                    deadline_kind, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record["message_id"]),
                    str(record.get("company") or "待确认"),
                    str(record.get("type") or "其他"),
                    record.get("deadline_at"),
                    record.get("action_url"),
                    str(record.get("summary") or "请查看原邮件"),
                    str(record["received_at"]),
                    status,
                    now,
                    str(record.get("subject") or ""),
                    str(record.get("sender") or ""),
                    str(record.get("deadline_kind") or "unknown"),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO mailbox_messages(
                    message_id, imap_uid, outcome, processed_at
                ) VALUES (?, ?, 'deadline', ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    imap_uid = excluded.imap_uid,
                    outcome = excluded.outcome,
                    processed_at = excluded.processed_at
                """,
                (str(record["message_id"]), imap_uid, now),
            )
        return cursor.rowcount > 0

    def list_deadlines(self, *, include_history: bool = False) -> list[dict[str, Any]]:
        if include_history:
            where = "1 = 1"
        else:
            where = "status IN ('pending', 'needs_review')"
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT message_id, company, type, deadline_at, action_url, summary,
                       received_at, status, created_at, subject, sender,
                       deadline_kind, updated_at
                FROM mail_deadlines
                WHERE {where}
                ORDER BY
                    CASE WHEN status = 'needs_review' THEN 1 ELSE 0 END,
                    CASE WHEN deadline_at IS NULL THEN 1 ELSE 0 END,
                    deadline_at ASC,
                    received_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def set_status(self, message_id: str, status: str) -> bool:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid mail deadline status: {status}")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE mail_deadlines SET status = ?, updated_at = ? WHERE message_id = ?",
                (status, utc_now(), message_id),
            )
        return cursor.rowcount > 0

    def expire_due(self, now: datetime) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE mail_deadlines
                SET status = 'expired', updated_at = ?
                WHERE status = 'pending' AND deadline_at IS NOT NULL AND deadline_at <= ?
                """,
                (utc_now(), now.astimezone(UTC).isoformat()),
            )
        return cursor.rowcount

    def pending_with_deadlines(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT message_id, company, type, deadline_at, summary
                FROM mail_deadlines
                WHERE status = 'pending' AND deadline_at IS NOT NULL
                ORDER BY deadline_at
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def notification_sent(self, message_id: str, threshold: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM mail_notifications
                WHERE message_id = ? AND threshold = ?
                """,
                (message_id, threshold),
            ).fetchone()
        return row is not None

    def record_notification(self, message_id: str, threshold: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO mail_notifications(
                    message_id, threshold, notified_at
                ) VALUES (?, ?, ?)
                """,
                (message_id, threshold, utc_now()),
            )


def initialize_mailbox_database() -> None:
    MailboxStore().initialize()
