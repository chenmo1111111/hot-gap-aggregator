from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken


UTC = timezone.utc
ALLOWED_CATEGORIES = {"面试笔试类", "工作相关", "广告推广", "其他"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def inbox_database_path() -> Path:
    configured = os.getenv("MAIL_INBOX_DB", "").strip()
    if configured:
        return Path(configured)
    sync_path = Path(os.getenv("SYNC_DB_PATH", "./sync.db"))
    return sync_path.with_name("mail-inbox.db")


class TokenCipher:
    def __init__(self, key: str | None = None) -> None:
        raw = (key or os.getenv("MAIL_TOKEN_ENCRYPTION_KEY", "")).strip().encode("ascii")
        if not raw:
            raise RuntimeError("MAIL_TOKEN_ENCRYPTION_KEY is required for Gmail authorization")
        try:
            self.fernet = Fernet(raw)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("MAIL_TOKEN_ENCRYPTION_KEY must be a Fernet key") from exc

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self.fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise RuntimeError("stored Gmail token cannot be decrypted") from exc


class InboxStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else inbox_database_path()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS inbox_accounts (
                    account_id TEXT PRIMARY KEY, provider TEXT NOT NULL,
                    label TEXT NOT NULL, address TEXT NOT NULL,
                    refresh_token_encrypted TEXT, uid_validity TEXT,
                    last_uid INTEGER, last_synced_at TEXT, last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inbox_messages (
                    account_id TEXT NOT NULL, message_key TEXT NOT NULL,
                    provider_uid TEXT NOT NULL, message_id TEXT NOT NULL,
                    sender TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
                    received_at TEXT NOT NULL, attachments_json TEXT NOT NULL DEFAULT '[]',
                    category TEXT, deadline_linked INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, message_key)
                );
                CREATE INDEX IF NOT EXISTS idx_inbox_messages_received
                    ON inbox_messages(received_at DESC);
                CREATE INDEX IF NOT EXISTS idx_inbox_messages_category
                    ON inbox_messages(category, received_at DESC);
            """)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def upsert_account(self, account_id: str, provider: str, label: str, address: str) -> None:
        with self.connect() as connection:
            connection.execute("""
                INSERT INTO inbox_accounts(account_id, provider, label, address, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET provider=excluded.provider,
                  label=excluded.label, address=excluded.address, updated_at=excluded.updated_at
            """, (account_id, provider, label, address, utc_now()))

    def account_state(self, account_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM inbox_accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
        return dict(row) if row else {}

    def save_refresh_token(self, account_id: str, encrypted_token: str) -> None:
        with self.connect() as connection:
            connection.execute("""
                UPDATE inbox_accounts SET refresh_token_encrypted = ?, last_error = NULL,
                    updated_at = ? WHERE account_id = ?
            """, (encrypted_token, utc_now(), account_id))

    def update_imap_state(self, account_id: str, uid_validity: str, last_uid: int) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("""
                UPDATE inbox_accounts SET uid_validity=?, last_uid=?, last_synced_at=?,
                    last_error=NULL, updated_at=? WHERE account_id=?
            """, (uid_validity, last_uid, now, now, account_id))

    def record_sync(self, account_id: str, error: str | None = None) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("""
                UPDATE inbox_accounts SET last_synced_at=?, last_error=?, updated_at=?
                WHERE account_id=?
            """, (now, error, now, account_id))

    def has_message(self, account_id: str, message_key: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM inbox_messages WHERE account_id=? AND message_key=?",
                (account_id, message_key),
            ).fetchone()
        return row is not None

    def add_message(self, record: Mapping[str, Any]) -> bool:
        category = record.get("category")
        if category is not None and category not in ALLOWED_CATEGORIES:
            category = None
        with self.connect() as connection:
            cursor = connection.execute("""
                INSERT OR IGNORE INTO inbox_messages(
                    account_id, message_key, provider_uid, message_id, sender, subject,
                    body, received_at, attachments_json, category, deadline_linked, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record["account_id"], record["message_key"], str(record["provider_uid"]),
                record.get("message_id") or record["message_key"], record.get("sender") or "",
                record.get("subject") or "(无主题)", record.get("body") or "",
                record["received_at"], json.dumps(record.get("attachments") or [], ensure_ascii=False),
                category, 1 if record.get("deadline_linked") else 0, utc_now(),
            ))
        return cursor.rowcount > 0

    def list_messages(self, *, account_id: str = "", category: str = "", query: str = "",
                      limit: int = 100, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if account_id:
            clauses.append("m.account_id = ?")
            params.append(account_id)
        if category:
            clauses.append("m.category = ?")
            params.append(category)
        if query:
            clauses.append("(m.subject LIKE ? ESCAPE '\\' OR m.body LIKE ? ESCAPE '\\')")
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend([f"%{escaped}%", f"%{escaped}%"])
        where = " AND ".join(clauses) if clauses else "1=1"
        with self.connect() as connection:
            total = int(connection.execute(
                f"SELECT COUNT(*) FROM inbox_messages m WHERE {where}", params
            ).fetchone()[0])
            rows = connection.execute(f"""
                SELECT m.account_id, a.label account_label, m.message_key, m.sender,
                       m.subject, substr(m.body, 1, 240) snippet, m.received_at,
                       m.attachments_json, m.category, m.deadline_linked
                FROM inbox_messages m JOIN inbox_accounts a USING(account_id)
                WHERE {where} ORDER BY m.received_at DESC LIMIT ? OFFSET ?
            """, [*params, min(max(limit, 1), 200), max(offset, 0)]).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["attachments"] = json.loads(item.pop("attachments_json"))
            item["deadline_linked"] = bool(item["deadline_linked"])
            items.append(item)
        return items, total

    def get_message(self, account_id: str, message_key: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("""
                SELECT m.*, a.label account_label FROM inbox_messages m
                JOIN inbox_accounts a USING(account_id)
                WHERE m.account_id=? AND m.message_key=?
            """, (account_id, message_key)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["attachments"] = json.loads(item.pop("attachments_json"))
        item["deadline_linked"] = bool(item["deadline_linked"])
        return item

    def prune(self, retention_days: int, *, now: datetime | None = None) -> int:
        cutoff = (now or datetime.now(UTC)) - timedelta(days=max(retention_days, 1))
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM inbox_messages WHERE received_at < ?", (cutoff.isoformat(),)
            )
        return cursor.rowcount


def initialize_inbox_database() -> None:
    InboxStore().initialize()
