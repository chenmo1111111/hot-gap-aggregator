from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.models import Item


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def item_hash(item: Item) -> str:
    stable_url = item.url.strip().lower()
    return text_hash(f"{item.source}:{stable_url}")


class Database:
    def __init__(self, path: str | Path = "data/hot.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS items (
                source TEXT NOT NULL, item_hash TEXT NOT NULL, rank INTEGER NOT NULL,
                payload TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (source, item_hash)
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                run_at TEXT NOT NULL, source TEXT NOT NULL, item_hash TEXT NOT NULL, rank INTEGER NOT NULL,
                PRIMARY KEY (run_at, source, item_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_item ON snapshots(source, item_hash, run_at);
            CREATE INDEX IF NOT EXISTS idx_snapshots_source_run ON snapshots(source, run_at);
            CREATE TABLE IF NOT EXISTS source_runs (
                run_at TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,
                item_count INTEGER NOT NULL, duration_ms INTEGER NOT NULL, error TEXT,
                PRIMARY KEY (run_at, source)
            );
            CREATE TABLE IF NOT EXISTS summaries (
                text_hash TEXT NOT NULL, provider TEXT NOT NULL, source_text TEXT NOT NULL,
                summary_text TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY (text_hash, provider)
            );
            CREATE TABLE IF NOT EXISTS gongkao_push_log (
                event_key TEXT PRIMARY KEY, pushed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS watcher_states (
                watch_key TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
                content_text TEXT NOT NULL, checked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS push_log (
                event_key TEXT PRIMARY KEY, pushed_at TEXT NOT NULL
            );
        """)
        snapshot_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(snapshots)")}
        if "payload" not in snapshot_columns:
            self.connection.execute("ALTER TABLE snapshots ADD COLUMN payload TEXT")
        self._migrate_translations()
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_summaries_provider ON summaries(provider, text_hash)")
        self.connection.execute("PRAGMA optimize")
        self.connection.commit()

    def _migrate_translations(self) -> None:
        columns = self.connection.execute("PRAGMA table_info(translations)").fetchall()
        if not columns:
            self.connection.execute("""
                CREATE TABLE translations (
                    text_hash TEXT NOT NULL, provider TEXT NOT NULL, source_text TEXT NOT NULL,
                    translated_text TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (text_hash, provider)
                )
            """)
            return
        if "provider" in {row["name"] for row in columns}:
            return
        self.connection.executescript("""
            CREATE TABLE translations_v2 (
                text_hash TEXT NOT NULL, provider TEXT NOT NULL, source_text TEXT NOT NULL,
                translated_text TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY (text_hash, provider)
            );
            INSERT INTO translations_v2(text_hash,provider,source_text,translated_text,created_at)
            SELECT text_hash,'legacy',source_text,translated_text,created_at FROM translations;
            DROP TABLE translations;
            ALTER TABLE translations_v2 RENAME TO translations;
        """)

    def get_translations(self, texts: list[str], provider: str = "legacy") -> dict[str, str]:
        if not texts:
            return {}
        hashes = [text_hash(text) for text in texts]
        placeholders = ",".join("?" for _ in hashes)
        rows = self.connection.execute(
            f"SELECT source_text, translated_text FROM translations WHERE provider=? AND text_hash IN ({placeholders})",
            [provider, *hashes],
        ).fetchall()
        return {row["source_text"]: row["translated_text"] for row in rows}

    def save_translations(self, translations: dict[str, str], provider: str = "legacy") -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.executemany(
            "INSERT OR REPLACE INTO translations(text_hash,provider,source_text,translated_text,created_at) VALUES(?,?,?,?,?)",
            [(text_hash(source), provider, source, target, now) for source, target in translations.items()],
        )
        self.connection.commit()

    def get_summaries(self, texts: list[str], provider: str) -> dict[str, str]:
        if not texts:
            return {}
        hashes = [text_hash(text) for text in texts]
        placeholders = ",".join("?" for _ in hashes)
        rows = self.connection.execute(
            f"SELECT source_text,summary_text FROM summaries WHERE provider=? AND text_hash IN ({placeholders})",
            [provider, *hashes],
        ).fetchall()
        return {row["source_text"]: row["summary_text"] for row in rows}

    def save_summaries(self, summaries: dict[str, str], provider: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.executemany(
            "INSERT OR REPLACE INTO summaries(text_hash,provider,source_text,summary_text,created_at) VALUES(?,?,?,?,?)",
            [(text_hash(source), provider, source, target, now) for source, target in summaries.items()],
        )
        self.connection.commit()

    def clear_caches_except(self, provider: str) -> tuple[int, int]:
        with self.connection:
            translations = self.connection.execute("DELETE FROM translations WHERE provider != ?", (provider,)).rowcount
            summaries = self.connection.execute("DELETE FROM summaries WHERE provider != ?", (provider,)).rowcount
        return translations, summaries

    def unseen_gongkao_events(self, event_keys: list[str]) -> set[str]:
        if not event_keys:
            return set()
        placeholders = ",".join("?" for _ in event_keys)
        seen = {
            row["event_key"]
            for row in self.connection.execute(
                f"SELECT event_key FROM gongkao_push_log WHERE event_key IN ({placeholders})", event_keys,
            )
        }
        return set(event_keys) - seen

    def mark_gongkao_events(self, event_keys: list[str]) -> None:
        if not event_keys:
            return
        now = datetime.now(UTC).isoformat()
        self.connection.executemany(
            "INSERT OR IGNORE INTO gongkao_push_log(event_key,pushed_at) VALUES(?,?)",
            [(key, now) for key in event_keys],
        )
        self.connection.commit()

    def get_watcher_state(self, watch_key: str) -> dict | None:
        row = self.connection.execute(
            "SELECT watch_key,content_hash,content_text,checked_at FROM watcher_states WHERE watch_key=?",
            (watch_key,),
        ).fetchone()
        return dict(row) if row else None

    def save_watcher_state(self, watch_key: str, content_hash: str, content_text: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            "INSERT OR REPLACE INTO watcher_states(watch_key,content_hash,content_text,checked_at) VALUES(?,?,?,?)",
            (watch_key, content_hash, content_text, now),
        )
        self.connection.commit()

    def unseen_push_events(self, event_keys: list[str]) -> set[str]:
        if not event_keys:
            return set()
        placeholders = ",".join("?" for _ in event_keys)
        seen = {
            row["event_key"] for row in self.connection.execute(
                f"SELECT event_key FROM push_log WHERE event_key IN ({placeholders})", event_keys,
            )
        }
        return set(event_keys) - seen

    def mark_push_events(self, event_keys: list[str]) -> None:
        if not event_keys:
            return
        now = datetime.now(UTC).isoformat()
        self.connection.executemany(
            "INSERT OR IGNORE INTO push_log(event_key,pushed_at) VALUES(?,?)",
            [(key, now) for key in event_keys],
        )
        self.connection.commit()

    def save_source(self, run_at: str, source: str, items: list[Item], duration_ms: int) -> None:
        previous_run_row = self.connection.execute(
            "SELECT MAX(run_at) AS run_at FROM snapshots WHERE source=? AND run_at<?", (source, run_at),
        ).fetchone()
        previous_run = previous_run_row["run_at"] if previous_run_row else None
        previous_ranks: dict[str, int] = {}
        if previous_run:
            previous_ranks = {
                row["item_hash"]: int(row["rank"])
                for row in self.connection.execute(
                    "SELECT item_hash,rank FROM snapshots WHERE source=? AND run_at=?", (source, previous_run),
                )
            }
        current_day = date.fromisoformat(run_at[:10])
        with self.connection:
            self.connection.execute("DELETE FROM items WHERE source = ?", (source,))
            for item in items:
                hashed = item_hash(item)
                previous_rank = previous_ranks.get(hashed)
                item.is_new = previous_rank is None
                item.rank_delta = "new" if previous_rank is None else previous_rank - item.rank
                item.days_on_board = self._continuous_days(source, hashed, current_day)
                self.connection.execute(
                    "INSERT INTO items(source,item_hash,rank,payload,updated_at) VALUES(?,?,?,?,?)",
                    (source, hashed, item.rank, json.dumps(item.to_dict(), ensure_ascii=False), run_at),
                )
                self.connection.execute(
                    "INSERT OR REPLACE INTO snapshots(run_at,source,item_hash,rank,payload) VALUES(?,?,?,?,?)",
                    (run_at, source, hashed, item.rank, json.dumps(item.to_dict(), ensure_ascii=False)),
                )
            self.connection.execute(
                "INSERT OR REPLACE INTO source_runs(run_at,source,status,item_count,duration_ms,error) VALUES(?,?,?,?,?,NULL)",
                (run_at, source, "ok", len(items), duration_ms),
            )

    def _continuous_days(self, source: str, hashed: str, current_day: date) -> int:
        rows = self.connection.execute(
            "SELECT DISTINCT substr(run_at,1,10) AS day FROM snapshots WHERE source=? AND item_hash=?",
            (source, hashed),
        ).fetchall()
        appearances = {date.fromisoformat(row["day"]) for row in rows}
        appearances.add(current_day)
        count = 0
        cursor = current_day
        while cursor in appearances:
            count += 1
            cursor -= timedelta(days=1)
        return count

    def save_status(self, run_at: str, source: str, status: str, duration_ms: int, error: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO source_runs(run_at,source,status,item_count,duration_ms,error) VALUES(?,?,?,?,?,?)",
            (run_at, source, status, 0, duration_ms, error[:500]),
        )
        self.connection.commit()

    def current_items(self, source: str | None = None) -> list[dict]:
        query = "SELECT payload FROM items"
        params: tuple[str, ...] = ()
        if source:
            query += " WHERE source=?"
            params = (source,)
        query += " ORDER BY rank, source"
        return [json.loads(row["payload"]) for row in self.connection.execute(query, params)]

    def latest_statuses(self, sources: list[str]) -> list[dict]:
        statuses: list[dict] = []
        for source in sources:
            row = self.connection.execute(
                "SELECT source,status,item_count,duration_ms,error,run_at FROM source_runs WHERE source=? ORDER BY run_at DESC LIMIT 1",
                (source,),
            ).fetchone()
            statuses.append(dict(row) if row else {"source": source, "status": "not_run", "item_count": 0})
        return statuses

    def close(self) -> None:
        self.connection.close()
