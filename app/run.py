from __future__ import annotations

import asyncio
import argparse
import json
import logging
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from app.collectors import (
    BilibiliCollector, ConfDeadlinesCollector, DouyinCollector, GitHubCollector, GongkaoCollector, NowcoderCollector,
    PapersCollector, TelegramCollector, WeiboCollector, XiaohongshuCollector, YouTubeCollector,
)
from app.collectors.base import BaseCollector, SourceUnavailable
from app.models import Item
from app.notify import notify_top20
from app.pipeline.cluster import cluster_items
from app.pipeline.processor import process_items
from app.pipeline.trends import export_trends
from app.pipeline.translator import create_translator
from app.store.database import Database

UTC = timezone.utc
from app.store.exporter import export_json

LOGGER = logging.getLogger("hot-gap")


def log_event(event: str, **fields: object) -> None:
    LOGGER.info(json.dumps({"event": event, **fields}, ensure_ascii=False))


async def collect_one(collector: BaseCollector) -> tuple[str, str, list[Item], int, str | None]:
    started = time.perf_counter()
    try:
        items = await collector.fetch()
        return collector.source, "ok", items, int((time.perf_counter() - started) * 1000), None
    except SourceUnavailable as exc:
        return collector.source, exc.status, [], int((time.perf_counter() - started) * 1000), str(exc)
    except Exception as exc:  # a single source must never stop the run
        return collector.source, "degraded", [], int((time.perf_counter() - started) * 1000), str(exc)


async def main(send_notifications: bool = False, retranslate: bool = False) -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    run_at = datetime.now(UTC).isoformat()
    database = Database()
    if retranslate:
        removed_translations, removed_summaries = database.clear_caches_except("zhipu")
        log_event("translation_cache_reset", provider="zhipu", translations=removed_translations, summaries=removed_summaries)
    translator = create_translator(database)
    collectors: list[BaseCollector] = [
        WeiboCollector(), BilibiliCollector(), GitHubCollector(), YouTubeCollector(),
        DouyinCollector(), TelegramCollector(), GongkaoCollector(),
        XiaohongshuCollector(), PapersCollector(), ConfDeadlinesCollector(), NowcoderCollector(),
    ]
    sources = [collector.source for collector in collectors]
    log_event("run_started", run_at=run_at, sources=sources)
    results = await asyncio.gather(*(collect_one(collector) for collector in collectors))

    successful_items = [item for _, status, items, _, _ in results if status == "ok" for item in items]
    try:
        await process_items(successful_items, translator)
    except Exception as exc:
        log_event("translation_degraded", error=str(exc))

    cluster_count = cluster_items(successful_items)

    for source, status, items, duration_ms, error in results:
        if status == "ok":
            database.save_source(run_at, source, items, duration_ms)
        else:
            database.save_status(run_at, source, status, duration_ms, error or "unknown error")
        log_event("source_finished", source=source, status=status, item_count=len(items), duration_ms=duration_ms, error=error)

    export_json(database, run_at, sources)
    trends = export_trends(database, run_at)
    if send_notifications:
        notification_status = await notify_top20(successful_items, database)
        log_event("notifications_finished", providers=notification_status)
    total = translator.cache_hits + translator.cache_misses
    hit_rate = round(translator.cache_hits / total, 3) if total else 1.0
    summary_total = translator.summary_cache_hits + translator.summary_cache_misses
    summary_hit_rate = round(translator.summary_cache_hits / summary_total, 3) if summary_total else 1.0
    log_event(
        "run_finished", item_count=len(successful_items), translator=translator.provider,
        translation_batches=translator.batch_count, translation_cache_hit_rate=hit_rate,
        summary_batches=translator.summary_batch_count, summary_cache_hit_rate=summary_hit_rate,
        cluster_count=cluster_count,
        trend_counts={key: len(value) for key, value in trends.items() if isinstance(value, list)},
    )
    database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect hot lists and export static snapshots")
    parser.add_argument("--notify", action="store_true", help="push the daily Top 20 after collection")
    parser.add_argument("--retranslate", action="store_true", help="discard non-Zhipu caches and translate again")
    arguments = parser.parse_args()
    asyncio.run(main(send_notifications=arguments.notify, retranslate=arguments.retranslate))
