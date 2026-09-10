"""Merge the published Gongkao feed with a captured Feishu Sheet snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

from app.pipeline.gongkao_filter import filter_gongkao_items, is_gov_domain


def _text(value: object) -> str:
    return str(value or "").strip()


def _normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", _text(value)).casefold()
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)


def _normalize_province(value: object) -> str:
    """Normalize equivalent province labels such as 内蒙古/内蒙古自治区."""
    text = _text(value)
    for suffix in (
        "壮族自治区", "回族自治区", "维吾尔自治区", "特别行政区", "自治区", "省", "市",
    ):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return _normalize(text)


def _extra(item: Mapping[str, Any]) -> Mapping[str, Any]:
    value = item.get("extra")
    return value if isinstance(value, Mapping) else {}


def _canonical_url(value: object) -> str:
    text = _text(value)
    if not text.startswith(("http://", "https://")):
        return ""
    parsed = urlsplit(text)
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(("", parsed.netloc.casefold(), path, parsed.query, ""))


def _identity_keys(item: Mapping[str, Any]) -> set[str]:
    extra = _extra(item)
    url = _canonical_url(
        item.get("url")
        or item.get("announcement_url")
        or extra.get("announcement_url")
        or extra.get("url")
    )
    title = _normalize(item.get("title_zh") or item.get("title"))
    province = _normalize_province(extra.get("province") or item.get("province"))
    keys = {f"url:{url}"} if url else set()
    if title:
        keys.add(f"title:{title}|{province}")
    return keys


def _has_value(value: object) -> bool:
    return value not in (None, "", [], {})


def _link_priority(item: Mapping[str, Any], default: int) -> int:
    """Rank links so an official government URL can never be overwritten.

    Priority: government source/domain > official watcher > captured Sheet >
    Fenbi/base feed.  The default identifies the ingestion path while the
    record itself can promote an official URL to the highest priority.
    """
    extra = _extra(item)
    url = item.get("url") or item.get("announcement_url") or extra.get("announcement_url")
    if extra.get("government_source") or is_gov_domain(url):
        return 3
    return default


def _prefer_external_record(
    existing: Mapping[str, Any],
    preferred: Mapping[str, Any],
    *,
    source_name: str,
) -> dict[str, Any]:
    """Keep the stable collected record while preferring its external source link.

    Fenbi often publishes the same notice as both an article and an exam-calendar
    entry.  The captured Sheet or an official watcher may contain the actual
    government announcement URL.  Preserve the existing ID (and therefore the
    enrichment cache/sync identity), but use the higher-priority source's URL and
    useful display fields.
    """
    result = dict(existing)
    for name in ("title", "title_zh", "url", "published_at", "summary", "summary_zh"):
        value = preferred.get(name)
        if _has_value(value):
            result[name] = value

    existing_extra = dict(_extra(existing))
    preferred_extra = dict(_extra(preferred))
    replaced_urls = [
        str(url).strip()
        for url in existing_extra.get("replaced_urls", [])
        if str(url).strip().startswith(("http://", "https://"))
    ] if isinstance(existing_extra.get("replaced_urls"), list) else []
    existing_url = str(existing.get("url") or "").strip()
    preferred_url = str(
        preferred.get("url") or preferred_extra.get("announcement_url") or ""
    ).strip()
    if (
        existing_url.startswith(("http://", "https://"))
        and preferred_url
        and _canonical_url(existing_url) != _canonical_url(preferred_url)
        and existing_url not in replaced_urls
    ):
        replaced_urls.append(existing_url)
    stable_id = existing_extra.get("id")
    stable_sub = existing_extra.get("sub")
    for name, value in preferred_extra.items():
        if _has_value(value):
            existing_extra[name] = value
    if _has_value(stable_id):
        existing_extra["id"] = stable_id
    if _has_value(stable_sub):
        existing_extra["sub"] = stable_sub
    preferred_id = preferred_extra.get("id")
    if _has_value(preferred_id) and preferred_id != stable_id:
        existing_extra[f"{source_name}_id"] = preferred_id
    if _has_value(preferred_url):
        existing_extra["announcement_url"] = preferred_url
    if replaced_urls:
        existing_extra["replaced_urls"] = replaced_urls
    existing_extra["preferred_link_source"] = source_name
    result["extra"] = existing_extra
    return result


def _items(payload: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    value = payload.get("items")
    if not isinstance(value, list):
        raise ValueError(f"{name} must contain an items list")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"{name} items[{index}] must be an object")
        result.append(dict(item))
    return result


def merge_gongkao_payloads(
    base_payload: Mapping[str, Any],
    sheet_payload: Mapping[str, Any] | None,
    server_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge CI, mainland watcher, and captured Sheet records without duplicates."""
    base_items = _items(base_payload, "gongkao.json")
    sheet_items = _items(sheet_payload, "gongkao_sheet.json") if sheet_payload else []
    server_items = _items(server_payload, "server-gongkao.json") if server_payload else []

    merged: list[dict[str, Any]] = []
    key_indices: dict[str, set[int]] = {}
    link_priorities: list[int] = []
    sheet_duplicate_count = 0
    sheet_merged_count = 0
    server_duplicate_count = 0
    server_merged_count = 0

    def remember(index: int, item: Mapping[str, Any]) -> None:
        for key in _identity_keys(item):
            key_indices.setdefault(key, set()).add(index)

    def matching_indices(item: Mapping[str, Any]) -> set[int]:
        return {
            index
            for key in _identity_keys(item)
            for index in key_indices.get(key, set())
        }
    # Retain the stable IDs/order of the base feed.  Higher-priority sources may
    # replace its display URL without discarding Fenbi's structured dates.
    for item in base_items:
        item["rank"] = len(merged) + 1
        merged.append(item)
        link_priorities.append(_link_priority(item, 0))
        remember(len(merged) - 1, item)
    # Mainland watcher records are official and take precedence over the
    # manually captured Sheet when both point at the same announcement.
    for item in server_items:
        incoming_priority = _link_priority(item, 2)
        matches = matching_indices(item)
        if matches:
            server_duplicate_count += 1
            changed = False
            for index in matches:
                if (
                    link_priorities[index] > incoming_priority
                    or link_priorities[index] == 3
                ):
                    continue
                merged[index] = _prefer_external_record(
                    merged[index], item, source_name="official_watcher"
                )
                link_priorities[index] = incoming_priority
                remember(index, merged[index])
                changed = True
            server_merged_count += int(changed)
            continue
        extra = dict(_extra(item))
        url = _canonical_url(item.get("url") or extra.get("announcement_url"))
        if not extra.get("id") and url:
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
            extra["id"] = f"watcher:{digest}"
        extra.setdefault("sub", "announcement")
        item["extra"] = extra
        item["rank"] = len(merged) + 1
        merged.append(item)
        link_priorities.append(incoming_priority)
        remember(len(merged) - 1, item)
    server_added_count = len(merged) - len(base_items)

    for item in sheet_items:
        incoming_priority = _link_priority(item, 1)
        matches = matching_indices(item)
        if matches:
            sheet_duplicate_count += 1
            changed = False
            for index in matches:
                if (
                    link_priorities[index] > incoming_priority
                    or link_priorities[index] == 3
                ):
                    continue
                merged[index] = _prefer_external_record(
                    merged[index], item, source_name="feishu_sheet"
                )
                link_priorities[index] = incoming_priority
                remember(index, merged[index])
                changed = True
            sheet_merged_count += int(changed)
            continue
        item["rank"] = len(merged) + 1
        merged.append(item)
        link_priorities.append(incoming_priority)
        remember(len(merged) - 1, item)

    filter_input_count = len(merged)
    merged, filter_stats, filtered_samples = filter_gongkao_items(merged, profile="site")
    for index, item in enumerate(merged, 1):
        item["rank"] = index

    base_status = base_payload.get("status")
    status = dict(base_status) if isinstance(base_status, Mapping) else {}
    status.update(
        {
            "source": "gongkao",
            "item_count": len(merged),
            "base_item_count": len(base_items),
            "sheet_item_count": len(sheet_items),
            "server_item_count": len(server_items),
            "server_added_count": server_added_count,
            "server_duplicate_count": server_duplicate_count,
            "server_merged_count": server_merged_count,
            "sheet_added_count": len(merged) - len(base_items) - server_added_count,
            "sheet_duplicate_count": sheet_duplicate_count,
            "sheet_merged_count": sheet_merged_count,
            "upstream_sources": [
                "gongkao",
                *(["gongkao_official"] if server_items else []),
                *(["feishu_sheet"] if sheet_items else []),
            ],
            "filter_input_count": filter_input_count,
            "filter_stats": filter_stats,
            "filtered_samples": filtered_samples,
        }
    )
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "source": "gongkao",
        "status": status,
        "items": merged,
    }


def write_gongkao(data_dir: str | Path) -> dict[str, Any]:
    target = Path(data_dir)
    base_path = target / "gongkao.json"
    sheet_path = target / "gongkao_sheet.json"
    server_path = target / "server-gongkao.json"
    base_payload = json.loads(base_path.read_text(encoding="utf-8"))
    if not isinstance(base_payload, dict):
        raise ValueError("gongkao.json must contain a JSON object")
    xiaozhaoya_path = target / "xiaozhaoya_gongkao.json"
    xiaozhaoya_count = 0
    if xiaozhaoya_path.exists():
        value = json.loads(xiaozhaoya_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("xiaozhaoya_gongkao.json must contain a JSON object")
        xiaozhaoya_items = _items(value, "xiaozhaoya_gongkao.json")
        xiaozhaoya_count = len(xiaozhaoya_items)
        base_payload = {
            **base_payload,
            "items": [*_items(base_payload, "gongkao.json"), *xiaozhaoya_items],
        }
    sheet_payload: dict[str, Any] | None = None
    if sheet_path.exists():
        value = json.loads(sheet_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("gongkao_sheet.json must contain a JSON object")
        sheet_payload = value

    server_payload: dict[str, Any] | None = None
    if server_path.exists():
        value = json.loads(server_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("server-gongkao.json must contain a JSON object")
        server_payload = value

    output = merge_gongkao_payloads(base_payload, sheet_payload, server_payload)
    output["status"]["xiaozhaoya_item_count"] = xiaozhaoya_count
    if xiaozhaoya_count:
        output["status"]["upstream_sources"] = [
            *output["status"]["upstream_sources"], "xiaozhaoya",
        ]
    destination = target / "gongkao_feishu.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return output


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Merge gongkao.json with gongkao_sheet.json")
    parser.add_argument("--data-dir", default=os.getenv("SITE_DATA_DIR", "/var/www/hot-gap/data"))
    arguments = parser.parse_args()
    output = write_gongkao(arguments.data_dir)
    print(
        json.dumps(
            {
                "event": "gongkao_exported",
                "item_count": len(output["items"]),
                "sheet_added_count": output["status"]["sheet_added_count"],
                "sheet_merged_count": output["status"]["sheet_merged_count"],
                "server_added_count": output["status"]["server_added_count"],
                "server_merged_count": output["status"]["server_merged_count"],
                "xiaozhaoya_item_count": output["status"].get("xiaozhaoya_item_count", 0),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
