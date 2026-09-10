"""Safety gate and alert helper for the two browser-captured paid tables."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import httpx
from dotenv import load_dotenv


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise ValueError(f"invalid snapshot: {path}")
    return value


def _normalize(value: object) -> str:
    return re.sub(r"[^\w]+", "", str(value or "").casefold())


def item_id(row: Mapping[str, Any], kind: str) -> str:
    extra = row.get("extra") if isinstance(row.get("extra"), Mapping) else {}
    if kind == "qiuzhao":
        return str(row.get("source_record_id") or "").strip() or (
            f"{_normalize(row.get('company_name'))}|{_normalize(row.get('position'))}"
        )
    return str(extra.get("id") or row.get("url") or "").strip()


def validate_and_commit(
    candidate: Path, stable: Path, *, kind: str, disappearance_log: Path,
) -> dict[str, int]:
    fresh = _read(candidate)
    previous = _read(stable) if stable.exists() else {"items": []}
    new_items = fresh["items"]
    old_items = previous["items"]
    minimum = math.ceil(len(old_items) * 0.9) if old_items else 1
    if len(new_items) < minimum:
        raise ValueError(
            f"{kind} count dropped from {len(old_items)} to {len(new_items)}; "
            f"minimum allowed is {minimum} (90%)"
        )
    old_by_id = {item_id(row, kind): row for row in old_items if isinstance(row, Mapping)}
    new_ids = {item_id(row, kind) for row in new_items if isinstance(row, Mapping)}
    missing = [row for key, row in old_by_id.items() if key and key not in new_ids]
    if missing:
        disappearance_log.parent.mkdir(parents=True, exist_ok=True)
        with disappearance_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "at": datetime.now().astimezone().isoformat(), "kind": kind,
                "previous_count": len(old_items), "current_count": len(new_items),
                "missing_count": len(missing),
                "items": [{
                    "id": item_id(row, kind),
                    "title": row.get("title") or row.get("position") or "",
                    "company": row.get("company_name") or "",
                    "url": row.get("url") or row.get("announcement_url") or "",
                } for row in missing],
            }, ensure_ascii=False) + "\n")
    stable.parent.mkdir(parents=True, exist_ok=True)
    candidate.replace(stable)
    return {"previous": len(old_items), "current": len(new_items), "missing": len(missing)}


def update_state(path: Path, *, success: bool, reason: str = "") -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        state = {}
    now = datetime.now().astimezone().isoformat()
    if success:
        state.update({"consecutive_failures": 0, "last_success": now, "last_error": ""})
    else:
        state.update({
            "consecutive_failures": int(state.get("consecutive_failures") or 0) + 1,
            "last_failure": now, "last_error": reason,
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return state


def notify_feishu(title: str, message: str) -> bool:
    load_dotenv()
    webhook = os.getenv("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        return False
    response = httpx.post(
        webhook,
        json={"msg_type": "text", "content": {"text": f"{title}\n{message}"}},
        timeout=10,
        follow_redirects=True,
    )
    response.raise_for_status()
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--kind", choices=("qiuzhao", "gongkao"), required=True)
    validate.add_argument("--candidate", type=Path, required=True)
    validate.add_argument("--stable", type=Path, required=True)
    validate.add_argument("--disappearance-log", type=Path, required=True)
    state = commands.add_parser("state")
    state.add_argument("--path", type=Path, required=True)
    state.add_argument("--success", action="store_true")
    state.add_argument("--reason", default="")
    alert = commands.add_parser("alert")
    alert.add_argument("--title", required=True)
    alert.add_argument("--message", required=True)
    args = parser.parse_args(argv)
    if args.command == "validate":
        print(json.dumps(validate_and_commit(
            args.candidate, args.stable, kind=args.kind,
            disappearance_log=args.disappearance_log,
        ), ensure_ascii=False))
    elif args.command == "state":
        print(json.dumps(update_state(
            args.path, success=args.success, reason=args.reason,
        ), ensure_ascii=False))
    else:
        print(json.dumps({"feishu_sent": notify_feishu(args.title, args.message)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
