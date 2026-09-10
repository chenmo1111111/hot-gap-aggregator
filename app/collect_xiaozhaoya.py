"""Capture Xiaozhaoya public opportunities into durable merge snapshots."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from app.collectors.xiaozhaoya import XiaozhaoyaCollector


def _atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


async def run(config: str | Path, data_dir: str | Path) -> dict[str, int]:
    qiuzhao, gongkao = await XiaozhaoyaCollector(config).collect()
    generated_at = datetime.now().astimezone().isoformat()
    target = Path(data_dir)
    _atomic(target / "xiaozhaoya_qiuzhao.json", {
        "generated_at": generated_at, "source": "xiaozhaoya",
        "status": {"status": "ok", "item_count": len(qiuzhao), "upstream_source": "xiaozhaoya"},
        "items": qiuzhao,
    })
    _atomic(target / "xiaozhaoya_gongkao.json", {
        "generated_at": generated_at, "source": "xiaozhaoya",
        "status": {"status": "ok", "item_count": len(gongkao), "upstream_source": "xiaozhaoya"},
        "items": gongkao,
    })
    return {"qiuzhao": len(qiuzhao), "gongkao": len(gongkao)}


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/xiaozhaoya.yaml")
    parser.add_argument("--data-dir", default=os.getenv("SITE_DATA_DIR", "/var/www/hot-gap/data"))
    args = parser.parse_args(argv)
    result = asyncio.run(run(args.config, args.data_dir))
    print(json.dumps({"event": "xiaozhaoya_collected", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
