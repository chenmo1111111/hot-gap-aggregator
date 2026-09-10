"""Split the raw S1 Xiaozhaoya snapshot into the two merge feeds."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from app.collectors.xiaozhaoya import split_records


def _text(value: object) -> str:
    return str(value or "").strip()


def _atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def run(input_path: str | Path, data_dir: str | Path) -> dict[str, int]:
    source = Path(input_path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError(f"{source} must contain an items list")
    qiuzhao, gongkao = split_records(payload["items"])
    generated_at = _text(payload.get("generated_at")) or datetime.now().astimezone().isoformat()
    target = Path(data_dir)
    common_status = {
        "status": "ok",
        "upstream_source": "xiaozhaoya",
        "source_total": int(payload.get("total") or len(payload["items"])),
    }
    _atomic(
        target / "xiaozhaoya_qiuzhao.json",
        {
            "generated_at": generated_at,
            "source": "xiaozhaoya",
            "status": {**common_status, "item_count": len(qiuzhao)},
            "items": qiuzhao,
        },
    )
    _atomic(
        target / "xiaozhaoya_gongkao.json",
        {
            "generated_at": generated_at,
            "source": "xiaozhaoya",
            "status": {**common_status, "item_count": len(gongkao)},
            "items": gongkao,
        },
    )
    return {"qiuzhao": len(qiuzhao), "gongkao": len(gongkao)}


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=os.getenv("SITE_DATA_DIR", "/var/www/hot-gap/data"))
    parser.add_argument("--input", default=None)
    args = parser.parse_args(argv)
    input_path = Path(args.input) if args.input else Path(args.data_dir) / "xiaozhaoya.json"
    result = run(input_path, args.data_dir)
    print(json.dumps({"event": "xiaozhaoya_split", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
