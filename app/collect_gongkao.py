"""Collect only the Gongkao feed on S1 and atomically publish gongkao.json."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from dotenv import load_dotenv

from app.collectors.gongkao import GongkaoCollector


LOGGER = logging.getLogger(__name__)


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
        suffix=".tmp", delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.chmod(0o644)
    temporary.replace(path)


async def collect_gongkao(data_dir: str | Path | None = None) -> dict:
    target = Path(data_dir or os.getenv("SITE_DATA_DIR", "site/public/data")) / "gongkao.json"
    if str(os.getenv("GONGKAO_ON_SERVER") or "").strip().casefold() in {
        "1", "true", "yes", "on",
    }:
        if target.exists():
            return json.loads(target.read_text(encoding="utf-8"))
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "gongkao",
            "status": {"source": "gongkao", "status": "skipped", "item_count": 0},
            "items": [],
        }
    collector = GongkaoCollector()
    items = await collector.fetch()
    generated_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "generated_at": generated_at,
        "source": "gongkao",
        "status": {
            "source": "gongkao", "status": "ok", "item_count": len(items),
            "filter": collector.filter_stats,
            "filtered_samples": collector.filtered_samples,
            "source_counts": collector.source_counts,
            "government_sources": collector.gov_collector.stats,
        },
        "items": [item.to_dict() for item in items],
    }
    _write_atomic(target, payload)
    return payload


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    payload = asyncio.run(collect_gongkao())
    status = payload["status"]
    print(json.dumps({
        "event": "gongkao_collected", "item_count": status["item_count"],
        "source_counts": status["source_counts"], "filter": status["filter"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
