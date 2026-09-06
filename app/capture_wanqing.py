"""Capture the newest autumn-recruitment rows from a shared Feishu Base view.

The source Base is readable in a browser but does not grant export permission.
This collector uses a dedicated persistent Playwright browser profile, so the
user signs in once and no cookie is copied into the repository or environment.
The previous snapshot is preserved whenever capture or validation fails.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import gzip
import json
import logging
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode

import httpx
import yaml
from dotenv import load_dotenv


LOGGER = logging.getLogger(__name__)
ALLOWED_COMPANY_TYPES = {"央企", "国企", "民企", "外企", "银行", "事业单位", "其他"}


class CaptureError(RuntimeError):
    """Raised when the source cannot be captured safely."""


def decode_gzip_json(value: str) -> dict[str, Any]:
    try:
        decoded = gzip.decompress(base64.b64decode(value)).decode("utf-8")
        payload = json.loads(decoded)
    except (ValueError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureError("飞书返回了无法解析的压缩数据") from exc
    if not isinstance(payload, dict):
        raise CaptureError("飞书压缩数据不是 JSON 对象")
    return payload


def _normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)


def _option_maps(metadata: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for field_id, field in (metadata.get("fieldMap") or {}).items():
        if not isinstance(field, Mapping):
            continue
        prop = field.get("property") if isinstance(field.get("property"), Mapping) else {}
        options = prop.get("options") if isinstance(prop.get("options"), list) else []
        result[str(field_id)] = {
            str(option.get("id")): str(option.get("name") or "")
            for option in options
            if isinstance(option, Mapping) and option.get("id")
        }
    return result


def _raw_value(record: Mapping[str, Any], field_id: str) -> Any:
    cell = record.get(field_id)
    return cell.get("value") if isinstance(cell, Mapping) else None


def _cell_text(
    record: Mapping[str, Any], field_id: str, options: Mapping[str, Mapping[str, str]]
) -> str:
    value = _raw_value(record, field_id)
    option_names = options.get(field_id, {})
    if value is None:
        return ""
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(option_names.get(item, item))
            elif isinstance(item, Mapping):
                parts.append(str(item.get("link") or item.get("text") or ""))
        return "、".join(part for part in parts if part)
    if isinstance(value, str):
        return option_names.get(value, value)
    return str(value)


def _matches_option(record: Mapping[str, Any], field_id: str, accepted: set[str]) -> bool:
    value = _raw_value(record, field_id)
    values = value if isinstance(value, list) else [value]
    return any(str(item) in accepted for item in values if item is not None)


def build_snapshot(
    metadata: Mapping[str, Any],
    chunks: list[Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    fields = config["fields"]
    filters = config["filters"]
    view_id = str(config["view_id"])
    options = _option_maps(metadata)
    records: dict[str, Mapping[str, Any]] = {}
    ranks: dict[str, Any] = {}
    for chunk in chunks:
        for record_id, record in (chunk.get("recordMap") or {}).items():
            if isinstance(record, Mapping):
                records[str(record_id)] = record
        view_rank = (
            ((chunk.get("rankInfo") or {}).get("viewRankMap") or {}).get(view_id) or {}
        )
        ranks.update(view_rank.get("rankMap") or {})

    cohort_options = {str(value) for value in filters.get("cohort_options") or []}
    batch_options = {str(value) for value in filters.get("batch_options") or []}
    filtered = [
        (record_id, record)
        for record_id, record in records.items()
        if _matches_option(record, str(fields["cohort"]), cohort_options)
        and _matches_option(record, str(fields["batch"]), batch_options)
    ]
    updated_field = str(fields["updated_at"])
    filtered.sort(
        key=lambda pair: (
            -int(_raw_value(pair[1], updated_field) or 0),
            str(ranks.get(pair[0]) or ""),
        )
    )

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    max_items = int(config.get("max_items") or 500)
    for record_id, record in filtered:
        company = _cell_text(record, str(fields["company"]), options)
        position = _cell_text(record, str(fields["position"]), options)
        key = f"{_normalize(company)}|{_normalize(position)}"
        if not _normalize(company) or not _normalize(position) or key in seen:
            continue
        seen.add(key)

        company_type = _cell_text(record, str(fields["company_type"]), options)
        if company_type == "央国企":
            company_type = "国企"
        if company_type not in ALLOWED_COMPANY_TYPES:
            company_type = "其他"
        industry = "、".join(
            part
            for part in _cell_text(record, str(fields["industry"]), options).split("、")
            if part and part != "婉清学姐冲冲冲的店唯一正版"
        )
        notes = _cell_text(record, str(fields["notes"]), options)
        if notes in {"/", "小红书-婉清学姐冲冲冲的店唯一正版"}:
            notes = ""
        selected.append({
            "source_record_id": record_id,
            "company_name": company,
            "company_type": company_type,
            "industry": industry,
            "position": position,
            "location": _cell_text(record, str(fields["location"]), options),
            "education": _cell_text(record, str(fields["education"]), options),
            "cohort": _cell_text(record, str(fields["cohort"]), options),
            "deadline": _cell_text(record, str(fields["deadline"]), options),
            "written_test": _matches_option(
                record,
                str(fields["written_test"]),
                {str(value) for value in filters.get("written_test_yes_options") or []},
            ),
            "apply_url": _cell_text(record, str(fields["apply_url"]), options),
            "announcement_url": _cell_text(
                record, str(fields["announcement_url"]), options
            ),
            "notes": notes,
            "updated_at": int(_raw_value(record, updated_field) or 0),
            "upstream_source": "wanqing_feishu",
        })
        if len(selected) >= max_items:
            break

    minimum = int(config.get("min_items") or max_items)
    if len(selected) < minimum:
        raise CaptureError(
            f"安全校验失败：只得到 {len(selected)} 条，低于最低要求 {minimum} 条；旧快照已保留"
        )
    return {
        "generated_at": generated_at or datetime.now().astimezone().isoformat(),
        "source": "qiuzhao",
        "status": {
            "source": "qiuzhao",
            "item_count": len(selected),
            "upstream_source": "wanqing_feishu",
            "source_view": str(config.get("source_view") or "27届秋招🍁"),
            "source_total_records": len(records),
            "source_matched_records": len(filtered),
            "deduplicated_by": "normalize(company_name)|normalize(position)",
        },
        "items": selected,
    }


def load_config(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("婉清抓取配置必须是 YAML 对象")
    required = ("page_url", "api_origin", "app_token", "table_id", "view_id", "fields", "filters")
    missing = [name for name in required if not value.get(name)]
    if missing:
        raise ValueError(f"婉清抓取配置缺少：{', '.join(missing)}")
    return value


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _notify(message: str) -> None:
    providers: list[tuple[str, dict[str, Any]]] = []
    if webhook := os.getenv("FEISHU_WEBHOOK", "").strip():
        providers.append((webhook, {"msg_type": "text", "content": {"text": message}}))
    if bark_url := os.getenv("BARK_URL", "").strip():
        providers.append((bark_url, {"title": "秋招抓取连续失败", "body": message, "group": "hot-gap"}))
    for url, body in providers:
        try:
            httpx.post(url, json=body, timeout=10, follow_redirects=True).raise_for_status()
        except httpx.HTTPError as exc:
            LOGGER.warning("failed to send capture alert: %s", exc)


def _failure_count(path: Path) -> int:
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("count") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


async def capture(
    config: Mapping[str, Any],
    *,
    output_path: Path,
    profile_dir: Path,
    screenshot_dir: Path,
    headed: bool = False,
    login: bool = False,
) -> dict[str, Any]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise CaptureError("缺少 Playwright，请先安装 requirements.txt") from exc

    profile_dir.mkdir(parents=True, exist_ok=True)
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    screenshot_path = screenshot_dir / f"wanqing_{stamp}.png"
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=not (headed or login),
            viewport={"width": 1600, "height": 1000},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(str(config["page_url"]), wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_timeout(5_000)
            if login:
                print("请在弹出的专用浏览器中登录飞书并确认能看到秋招表，然后回到此窗口按 Enter。")
                await asyncio.to_thread(input)
            await page.screenshot(path=str(screenshot_path), full_page=False)

            app_token = str(config["app_token"])
            table_id = str(config["table_id"])
            view_id = str(config["view_id"])
            origin = str(config["api_origin"]).rstrip("/")
            meta_query = urlencode({
                "tableID": table_id,
                "viewID": view_id,
                "recordLimit": 20,
                "ondemandLimit": 200,
                "needBase": "true",
                "viewLazyLoad": "true",
                "ondemandVer": 2,
                "openType": 0,
                "noMissCS": "true",
                "optimizationFlag": 1,
                "removeFmlExtra": "true",
            })
            meta_url = f"{origin}/space/api/v1/bitable/{app_token}/clientvars?{meta_query}"
            meta_response = await context.request.get(meta_url, timeout=90_000)
            meta_envelope = await meta_response.json()
            if meta_response.status != 200 or meta_envelope.get("code") != 0:
                raise CaptureError(
                    f"飞书登录会话不可用（HTTP {meta_response.status}, "
                    f"code={meta_envelope.get('code')}）；请重新执行 --login"
                )
            encoded_table = (meta_envelope.get("data") or {}).get("table")
            if not encoded_table:
                raise CaptureError("飞书表结构响应缺少 data.table")
            metadata = decode_gzip_json(str(encoded_table))
            meta = metadata.get("meta") if isinstance(metadata.get("meta"), Mapping) else {}
            table_rev = (
                metadata.get("tableRev")
                or metadata.get("latestCSRev")
                or metadata.get("rev")
                or metadata.get("revision")
                or meta.get("rev")
            )
            record_count = int(metadata.get("recordCount") or 0)
            if table_rev is None or record_count <= 0:
                raise CaptureError("无法从飞书表结构读取 tableRev/recordCount")

            page_size = int(config.get("page_size") or 3000)
            chunks: list[dict[str, Any]] = []
            records_base = f"{origin}/space/api/v1/bitable/{app_token}/records"
            for offset in range(0, record_count, page_size):
                record_query = urlencode({
                    "tableId": table_id,
                    "viewId": view_id,
                    "tableRev": table_rev,
                    "depRev": "{}",
                    "viewLazyLoad": "true",
                    "offset": offset,
                    "limit": page_size,
                    "tableID": table_id,
                    "viewID": view_id,
                    "removeFmlExtra": "true",
                })
                response = await context.request.get(
                    f"{records_base}?{record_query}", timeout=90_000
                )
                envelope = await response.json()
                if response.status != 200 or envelope.get("code") != 0:
                    raise CaptureError(
                        f"读取 offset={offset} 失败（HTTP {response.status}, code={envelope.get('code')}）"
                    )
                encoded_records = (envelope.get("data") or {}).get("records")
                if not encoded_records:
                    raise CaptureError(f"offset={offset} 的响应缺少 data.records")
                chunks.append(decode_gzip_json(str(encoded_records)))
                await asyncio.sleep(0.2)

            payload = build_snapshot(metadata, chunks, config)
            _atomic_write(output_path, payload)
            return payload
        finally:
            await context.close()


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Capture 500 Qiuzhao rows from the shared Wanqing Feishu Base")
    parser.add_argument("--config", default=os.getenv("WANQING_CAPTURE_CONFIG", "config/wanqing_capture.yaml"))
    parser.add_argument("--output", default=None)
    parser.add_argument("--profile-dir", default=os.getenv("WANQING_PROFILE_DIR", ".wanqing-browser"))
    parser.add_argument("--state-dir", default=os.getenv("WANQING_STATE_DIR", ".wanqing-state"))
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--login", action="store_true", help="open the dedicated browser for one-time Feishu login")
    arguments = parser.parse_args()

    state_dir = Path(arguments.state_dir)
    output_path = Path(arguments.output) if arguments.output else Path(
        os.getenv("SITE_DATA_DIR", "/var/www/hot-gap/data")
    ) / "qiuzhao_wanqing.json"
    failure_path = state_dir / "capture_failures.json"
    try:
        config = load_config(Path(arguments.config))
        payload = asyncio.run(capture(
            config,
            output_path=output_path,
            profile_dir=Path(arguments.profile_dir),
            screenshot_dir=state_dir / "screenshots",
            headed=arguments.headed,
            login=arguments.login,
        ))
        _atomic_write(failure_path, {"count": 0})
        print(json.dumps({
            "event": "wanqing_captured",
            "item_count": len(payload["items"]),
            "source_total_records": payload["status"]["source_total_records"],
            "source_matched_records": payload["status"]["source_matched_records"],
            "output": str(output_path),
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        count = _failure_count(failure_path) + 1
        _atomic_write(failure_path, {"count": count, "last_error": str(exc)})
        LOGGER.exception("wanqing capture failed; previous snapshot preserved")
        if count >= 2 and (os.getenv("FEISHU_WEBHOOK") or os.getenv("BARK_URL")):
            _notify(f"婉清秋招抓取连续失败 {count} 次：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
