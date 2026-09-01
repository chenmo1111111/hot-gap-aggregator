from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from app.store.database import Database, text_hash


def rank_history(database: Database, source: str, url: str, generated_at: str) -> list[dict]:
    hashed = text_hash(f"{source}:{url.strip().lower()}")
    cutoff = (date.fromisoformat(generated_at[:10]) - timedelta(days=6)).isoformat()
    rows = database.connection.execute(
        "SELECT run_at,rank FROM snapshots WHERE source=? AND item_hash=? AND substr(run_at,1,10)>=? ORDER BY run_at DESC LIMIT 30",
        (source, hashed, cutoff),
    ).fetchall()
    return [{"run_at": row["run_at"], "rank": int(row["rank"])} for row in reversed(rows)]


def derive_trends(database: Database, generated_at: str) -> dict:
    today = date.fromisoformat(generated_at[:10])
    yesterday = today - timedelta(days=1)
    rising: list[dict] = []
    new_today: list[dict] = []
    dropped: list[dict] = []
    current_payloads: list[dict] = []

    sources = [row["source"] for row in database.connection.execute("SELECT DISTINCT source FROM snapshots")]
    for source in sources:
        latest_row = database.connection.execute(
            "SELECT MAX(run_at) AS run_at FROM snapshots WHERE source=?", (source,),
        ).fetchone()
        latest_run = latest_row["run_at"] if latest_row else None
        if not latest_run:
            continue
        previous_row = database.connection.execute(
            "SELECT MAX(run_at) AS run_at FROM snapshots WHERE source=? AND run_at<?", (source, latest_run),
        ).fetchone()
        previous_run = previous_row["run_at"] if previous_row else None
        previous_ranks = _ranks_at(database, source, previous_run) if previous_run else {}
        latest_rows = database.connection.execute(
            "SELECT item_hash,rank,payload FROM snapshots WHERE source=? AND run_at=?", (source, latest_run),
        ).fetchall()
        latest_hashes = {row["item_hash"] for row in latest_rows}
        for row in latest_rows:
            payload = _payload(database, source, row["item_hash"], row["payload"])
            if not payload:
                continue
            payload["rank_history"] = rank_history(database, source, payload["url"], generated_at)
            current_payloads.append(payload)
            previous_rank = previous_ranks.get(row["item_hash"])
            if previous_rank is not None and previous_rank - int(row["rank"]) > 0:
                payload["rank_delta"] = previous_rank - int(row["rank"])
                rising.append(payload)
            first_day = database.connection.execute(
                "SELECT MIN(substr(run_at,1,10)) AS day FROM snapshots WHERE source=? AND item_hash=?",
                (source, row["item_hash"]),
            ).fetchone()["day"]
            if first_day == today.isoformat():
                new_today.append(payload)

        if latest_run[:10] == today.isoformat():
            yesterday_run_row = database.connection.execute(
                "SELECT MAX(run_at) AS run_at FROM snapshots WHERE source=? AND substr(run_at,1,10)=?",
                (source, yesterday.isoformat()),
            ).fetchone()
            yesterday_run = yesterday_run_row["run_at"] if yesterday_run_row else None
            if yesterday_run:
                for row in database.connection.execute(
                    "SELECT item_hash,rank,payload FROM snapshots WHERE source=? AND run_at=?", (source, yesterday_run),
                ):
                    if row["item_hash"] in latest_hashes:
                        continue
                    payload = _payload(database, source, row["item_hash"], row["payload"])
                    if payload:
                        payload["rank_history"] = rank_history(database, source, payload["url"], generated_at)
                        dropped.append(payload)

    rising.sort(key=lambda item: int(item.get("rank_delta") or 0), reverse=True)
    new_today.sort(key=lambda item: (item.get("rank", 999), item.get("source", "")))
    dropped.sort(key=lambda item: (item.get("rank", 999), item.get("source", "")))
    longest = sorted(current_payloads, key=lambda item: (-int(item.get("days_on_board") or 0), item.get("rank", 999)))
    return {
        "generated_at": generated_at,
        "rising": rising[:20],
        "new_today": new_today[:20],
        "dropped": dropped[:20],
        "longest_on_board": longest[:20],
    }


def export_trends(database: Database, generated_at: str, output_dir: str | Path = "public/data") -> dict:
    payload = derive_trends(database, generated_at)
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    (target / "trends.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _ranks_at(database: Database, source: str, run_at: str) -> dict[str, int]:
    return {
        row["item_hash"]: int(row["rank"])
        for row in database.connection.execute(
            "SELECT item_hash,rank FROM snapshots WHERE source=? AND run_at=?", (source, run_at),
        )
    }


def _payload(database: Database, source: str, hashed: str, raw: str | None) -> dict | None:
    if raw:
        return json.loads(raw)
    current = database.connection.execute(
        "SELECT payload FROM items WHERE source=? AND item_hash=?", (source, hashed),
    ).fetchone()
    return json.loads(current["payload"]) if current else None
