"""Track first-observed Gongkao/Qiuzhao volume and alert on low daily intake."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import httpx
from dotenv import load_dotenv

CHINA_TZ = timezone(timedelta(hours=8))


def _read_items(path: Path) -> list[Mapping[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("items") if isinstance(payload, Mapping) else payload
    return [row for row in (rows or []) if isinstance(row, Mapping)]


def _date(value: object) -> date | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            stamp = int(value)
            if stamp > 10_000_000_000:
                stamp //= 1000
            return datetime.fromtimestamp(stamp, CHINA_TZ).date()
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).date()
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _extra(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("extra")
    return value if isinstance(value, Mapping) else {}


def _gongkao_id(row: Mapping[str, Any]) -> str:
    extra = _extra(row)
    return str(extra.get("id") or row.get("url") or "").strip()


def _qiuzhao_id(row: Mapping[str, Any]) -> str:
    explicit = str(row.get("source_record_id") or "").strip()
    if explicit:
        return explicit
    value = "|".join(str(row.get(name) or "").strip().casefold() for name in ("company_name", "position"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value != "|" else ""


def _load_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else fallback
    except (OSError, ValueError, json.JSONDecodeError):
        return fallback


def _atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def update_daily_volume(
    gongkao: list[Mapping[str, Any]], qiuzhao: list[Mapping[str, Any]], *,
    today: date, state_path: Path, output_path: Path,
) -> dict[str, Any]:
    state = _load_json(state_path, {"qiuzhao_first_seen": {}, "alerts_sent": []})
    qiuzhao_seen = state.setdefault("qiuzhao_first_seen", {})
    if not isinstance(qiuzhao_seen, dict):
        qiuzhao_seen = state["qiuzhao_first_seen"] = {}
    for row in qiuzhao:
        key = _qiuzhao_id(row)
        if not key or key in qiuzhao_seen:
            continue
        source_date = _date(
            row.get("first_seen") or row.get("published_at") or row.get("updated_at")
            or _extra(row).get("first_seen")
        )
        qiuzhao_seen[key] = (source_date or today).isoformat()

    gongkao_new = sum(
        1 for row in gongkao
        if _gongkao_id(row) and _date(_extra(row).get("first_seen") or row.get("published_at")) == today
    )
    qiuzhao_new = sum(1 for value in qiuzhao_seen.values() if _date(value) == today)
    output = _load_json(output_path, {"history": []})
    history = [entry for entry in output.get("history", []) if isinstance(entry, dict) and entry.get("date") != today.isoformat()]
    entry = {
        "date": today.isoformat(), "gongkao_new": gongkao_new,
        "qiuzhao_new": qiuzhao_new, "recorded_at": datetime.now().astimezone().isoformat(),
    }
    history.append(entry)
    history = sorted(history, key=lambda row: str(row.get("date")))[-30:]
    _atomic(state_path, state)
    _atomic(output_path, {"generated_at": entry["recorded_at"], "history": history})
    return {**entry, "history_days": len(history)}


def maybe_alert(result: Mapping[str, Any], *, state_path: Path, current_hour: int) -> bool:
    load_dotenv()
    if current_hour < int(os.getenv("DAILY_VOLUME_ALERT_AFTER_HOUR", "8")):
        return False
    day = str(result["date"])
    state = _load_json(state_path, {"alerts_sent": []})
    sent = set(str(value) for value in state.get("alerts_sent", []))
    if day in sent:
        return False
    warnings: list[str] = []
    if int(result["qiuzhao_new"]) < 20:
        warnings.append(f"秋招今日新增 {result['qiuzhao_new']} 条（低于20），请检查采集源")
    if int(result["gongkao_new"]) < 3:
        warnings.append(
            f"公考今日新增 {result['gongkao_new']} 条（低于3）；事业编淡季可能正常，连续5天为0再重点处理"
        )
    if not warnings:
        return False
    webhook = os.getenv("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        return False
    response = httpx.post(
        webhook,
        json={"msg_type": "text", "content": {"text": "每日更新量提醒\n" + "\n".join(warnings)}},
        timeout=10, follow_redirects=True,
    )
    response.raise_for_status()
    state["alerts_sent"] = sorted((*sent, day))[-30:]
    _atomic(state_path, state)
    return True


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=os.getenv("SITE_DATA_DIR", "/var/www/hot-gap/data"))
    parser.add_argument("--state", default=os.getenv("DAILY_VOLUME_STATE", "/var/lib/hot-gap/daily-volume-state.json"))
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)
    now = datetime.now().astimezone()
    result = update_daily_volume(
        _read_items(data_dir / "gongkao_enriched.json"),
        _read_items(data_dir / "qiuzhao.json"),
        today=now.date(), state_path=Path(args.state), output_path=data_dir / "daily-volume.json",
    )
    result["alert_sent"] = maybe_alert(result, state_path=Path(args.state), current_hour=now.hour)
    print(json.dumps({"event": "daily_volume", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
