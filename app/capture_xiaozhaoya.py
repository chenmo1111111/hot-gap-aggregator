"""One-time full backfill from the shared Xiaozhaoya Feishu Base.

The Base is a durable foundation layer, not a scheduled source. This command
enumerates every visible Base table, captures every table that contains the
required recruitment columns, and writes one raw ``xiaozhaoya.json`` snapshot.
The dedicated Playwright profile is reused; cookies are never exported.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlencode, urlsplit

import yaml
from dotenv import load_dotenv

from app.capture_wanqing import CaptureError, _cell_text, _option_maps, decode_gzip_json
from app.collectors.xiaozhaoya import split_records


LOGGER = logging.getLogger(__name__)
REQUIRED_COLUMNS = {"公司名称", "招聘岗位"}
TOTAL_VIEW_NAMES = ("🔥网申总表", "网申总表", "总表")
DETAIL_ID_RE = re.compile(r"(?:[?&]id=|/detail/)(\d+)", re.I)
RAW_FIELDS = {
    "companyName": "公司名称",
    "companyTypeName": "企业性质",
    "industryName": "行业分类",
    "batchNameList": "批次",
    "jobTitle": "招聘岗位",
    "educationLevelNameList": "学历要求",
    "graduationYearList": "届次",
    "majorRequirements": "专业要求",
    "cityNameList": "工作地点",
    "updateDate": "更新时间",
    "applicationDeadline": "截止时间",
    "sourceName": "公告来源",
    "announcementLink": "公告链接",
    "applicationMethod": "投递方式",
    "hasWrittenTest": "是否笔试",
    "companyProfile": "备注",
    "referralCode": "内推码",
}


def _text(value: object) -> str:
    return str(value or "").strip()


def load_config(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("校招鸭 Base 抓取配置必须是 YAML 对象")
    page_url = _text(os.getenv("XIAOZHAOYA_FEISHU_BASE_URL") or value.get("page_url"))
    if not page_url:
        raise ValueError(
            "缺少 XIAOZHAOYA_FEISHU_BASE_URL；真实 Base URL 只应放在本地/服务器 .env"
        )
    parsed = urlsplit(page_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    try:
        base_index = path_parts.index("base")
        app_token = path_parts[base_index + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError("XIAOZHAOYA_FEISHU_BASE_URL 不是有效的飞书 Base URL") from exc
    query = parse_qs(parsed.query)
    value["page_url"] = page_url
    value["app_token"] = app_token
    value.setdefault("initial_table_id", (query.get("table") or [""])[0])
    value.setdefault("initial_view_id", (query.get("view") or [""])[0])
    required = ("page_url", "api_origin", "app_token", "initial_table_id")
    missing = [name for name in required if not value.get(name)]
    if missing:
        raise ValueError(f"校招鸭 Base 抓取配置缺少：{', '.join(missing)}")
    return value


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _fields_by_name(metadata: Mapping[str, Any]) -> dict[str, str]:
    return {
        _text(field.get("name")): _text(field_id)
        for field_id, field in (metadata.get("fieldMap") or {}).items()
        if isinstance(field, Mapping) and _text(field.get("name"))
    }


def _view_entries(metadata: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [
        (_text(view_id), _text(view.get("name")))
        for view_id, view in (metadata.get("viewMap") or {}).items()
        if isinstance(view, Mapping) and _text(view_id)
    ]


def _select_view(metadata: Mapping[str, Any]) -> tuple[str, str]:
    views = _view_entries(metadata)
    for wanted in TOTAL_VIEW_NAMES:
        for view_id, name in views:
            if name == wanted:
                return view_id, name
    if not views:
        raise CaptureError("招聘表没有可读取的视图")
    return views[0]


def _table_entries(base: Mapping[str, Any]) -> list[tuple[str, str]]:
    infos = base.get("blockInfos") if isinstance(base.get("blockInfos"), Mapping) else {}
    entries: list[tuple[str, str]] = []
    for block_id in base.get("blocks") or []:
        identifier = _text(block_id)
        info = infos.get(identifier) if isinstance(infos, Mapping) else None
        if not identifier.startswith("tbl") or not isinstance(info, Mapping):
            continue
        entries.append((identifier, _text(info.get("name")) or identifier))
    return entries


def _recruitment_id(announcement_url: str, record_id: str) -> str:
    match = DETAIL_ID_RE.search(announcement_url)
    if match:
        return match.group(1)
    try:
        query_id = (parse_qs(urlsplit(announcement_url).query).get("id") or [""])[0]
    except ValueError:
        query_id = ""
    return _text(query_id) or record_id


def _announcement_title(company: str, position: str) -> str:
    if "公告" in position:
        return position
    return f"{company}招聘" if company else position


def build_snapshot(
    tables: Sequence[tuple[str, str, Mapping[str, Any], Sequence[Mapping[str, Any]]]],
    *,
    discovered_tables: Sequence[Mapping[str, Any]] | None = None,
    minimum_items: int = 1,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Map complete Base record chunks to the established raw snapshot schema."""
    mapped: dict[str, dict[str, Any]] = {}
    source_records = 0
    invalid_records = 0
    source_views: dict[str, list[str]] = {}

    for table_id, table_name, metadata, chunks in tables:
        fields = _fields_by_name(metadata)
        missing = sorted(REQUIRED_COLUMNS - fields.keys())
        if missing:
            raise CaptureError(f"{table_name} 缺少招聘字段：{', '.join(missing)}")
        options = _option_maps(metadata)
        records: dict[str, Mapping[str, Any]] = {}
        for chunk in chunks:
            for record_id, record in (chunk.get("recordMap") or {}).items():
                if isinstance(record, Mapping):
                    records[_text(record_id)] = record
        source_records += len(records)
        source_views[table_name] = [name for _, name in _view_entries(metadata)]

        for record_id, record in records.items():
            raw = {
                target: _cell_text(record, fields.get(column, ""), options)
                if fields.get(column)
                else ""
                for target, column in RAW_FIELDS.items()
            }
            company = _text(raw["companyName"])
            position = _text(raw["jobTitle"])
            if not company or not position:
                invalid_records += 1
                continue
            announcement_url = _text(raw["announcementLink"])
            identifier = _recruitment_id(announcement_url, record_id)
            raw.update(
                {
                    "recruitmentId": identifier,
                    "fullName": company,
                    "announcementDate": _text(raw["updateDate"]),
                    # Batch labels such as "秋招专场" are metadata, not event
                    # titles. Appending them would trip the shared event-noise gate.
                    "announcementTitle": _announcement_title(company, position),
                    "sourceTableId": table_id,
                    "sourceTableName": table_name,
                    "sourceRecordId": record_id,
                }
            )
            mapped[identifier] = raw

    items = sorted(
        mapped.values(),
        key=lambda row: (_text(row.get("updateDate")), _text(row.get("recruitmentId"))),
        reverse=True,
    )
    if len(items) < minimum_items:
        raise CaptureError(
            f"安全校验失败：只得到 {len(items)} 条，低于最低要求 {minimum_items} 条"
        )
    return {
        "generated_at": generated_at or datetime.now().astimezone().isoformat(),
        "source": "xiaozhaoya_feishu_base",
        "total": len(items),
        "status": {
            "mode": "one_time_foundation_backfill",
            "scheduled": False,
            "item_count": len(items),
            "source_record_count": source_records,
            "invalid_record_count": invalid_records,
            "deduplicated_count": source_records - invalid_records - len(items),
            "captured_table_count": len(tables),
            "captured_tables": [table_name for _, table_name, _, _ in tables],
            "source_views": source_views,
            "discovered_tables": list(discovered_tables or []),
            "deduplicated_by": "recruitmentId (xiaozhaoya detail id; Base record id fallback)",
        },
        "items": items,
    }


def _clientvars_query(table_id: str, view_id: str = "", *, need_base: bool) -> str:
    values: dict[str, object] = {
        "tableID": table_id,
        "recordLimit": 20,
        "ondemandLimit": 200,
        "needBase": str(need_base).lower(),
        "viewLazyLoad": "true",
        "ondemandVer": 2,
        "openType": 0,
        "noMissCS": "true",
        "optimizationFlag": 1,
        "removeFmlExtra": "true",
    }
    if view_id:
        values["viewID"] = view_id
    return urlencode(values)


async def _get_clientvars(
    request: Any,
    origin: str,
    app_token: str,
    table_id: str,
    view_id: str = "",
    *,
    need_base: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    query = _clientvars_query(table_id, view_id, need_base=need_base)
    response = await request.get(
        f"{origin}/space/api/v1/bitable/{app_token}/clientvars?{query}", timeout=90_000
    )
    try:
        envelope = await response.json()
    except Exception as exc:
        raise CaptureError(f"飞书 clientvars 返回非 JSON（HTTP {response.status}）") from exc
    if response.status != 200 or envelope.get("code") != 0:
        raise CaptureError(
            f"飞书登录会话不可用（HTTP {response.status}, code={envelope.get('code')}）；"
            "请执行 python -m app.capture_xiaozhaoya --login"
        )
    data = envelope.get("data") if isinstance(envelope.get("data"), Mapping) else {}
    encoded_table = data.get("table")
    if not encoded_table:
        raise CaptureError(f"飞书表 {table_id} 的响应缺少 data.table")
    table = decode_gzip_json(_text(encoded_table))
    base = (
        decode_gzip_json(_text(data.get("base")))
        if need_base and data.get("base")
        else None
    )
    return table, base


async def _record_chunks(
    request: Any,
    origin: str,
    app_token: str,
    table_id: str,
    view_id: str,
    table_rev: object,
    record_count: int,
    page_size: int,
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    endpoint = f"{origin}/space/api/v1/bitable/{app_token}/records"
    for offset in range(0, record_count, page_size):
        query = urlencode(
            {
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
            }
        )
        response = await request.get(f"{endpoint}?{query}", timeout=120_000)
        envelope = await response.json()
        if response.status != 200 or envelope.get("code") != 0:
            raise CaptureError(
                f"读取 {table_id} offset={offset} 失败"
                f"（HTTP {response.status}, code={envelope.get('code')}）"
            )
        encoded = (envelope.get("data") or {}).get("records")
        if not encoded:
            raise CaptureError(f"{table_id} offset={offset} 的响应缺少 data.records")
        chunks.append(decode_gzip_json(_text(encoded)))
        await asyncio.sleep(0.2)
    return chunks


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
    screenshot_path = screenshot_dir / f"xiaozhaoya_base_{stamp}.png"
    origin = _text(config["api_origin"]).rstrip("/")
    app_token = _text(config["app_token"])
    initial_table = _text(config["initial_table_id"])
    initial_view = _text(config.get("initial_view_id"))

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=not (headed or login),
            viewport={"width": 1600, "height": 1000},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(
                _text(config["page_url"]), wait_until="domcontentloaded", timeout=120_000
            )
            await page.wait_for_timeout(5_000)
            if login:
                print("请在弹出的专用浏览器中登录飞书，确认能看到校招鸭 Base，然后回到此窗口按 Enter。")
                await asyncio.to_thread(input)
            await page.screenshot(path=str(screenshot_path), full_page=False)

            _, base = await _get_clientvars(
                context.request, origin, app_token, initial_table, initial_view, need_base=True
            )
            if not base:
                raise CaptureError("飞书响应缺少 Base 表清单")

            captured: list[
                tuple[str, str, Mapping[str, Any], Sequence[Mapping[str, Any]]]
            ] = []
            discovered: list[dict[str, Any]] = []
            page_size = int(config.get("page_size") or 3000)
            for table_id, table_name in _table_entries(base):
                metadata, _ = await _get_clientvars(
                    context.request, origin, app_token, table_id, need_base=False
                )
                fields = _fields_by_name(metadata)
                record_count = int(metadata.get("recordCount") or 0)
                entry: dict[str, Any] = {
                    "id": table_id,
                    "name": table_name,
                    "record_count": record_count,
                    "views": [name for _, name in _view_entries(metadata)],
                }
                if not REQUIRED_COLUMNS.issubset(fields):
                    entry["status"] = "skipped_non_recruitment_table"
                    discovered.append(entry)
                    continue
                view_id, view_name = _select_view(metadata)
                entry.update({"status": "captured", "capture_view": view_name})
                discovered.append(entry)
                table_rev = metadata.get("latestCSRev")
                if table_rev is None or record_count <= 0:
                    raise CaptureError(f"无法从 {table_name} 读取 latestCSRev/recordCount")
                chunks = await _record_chunks(
                    context.request,
                    origin,
                    app_token,
                    table_id,
                    view_id,
                    table_rev,
                    record_count,
                    page_size,
                )
                captured.append((table_id, table_name, metadata, chunks))

            if not captured:
                raise CaptureError("Base 中没有包含公司名称和招聘岗位的数据表")
            payload = build_snapshot(
                captured,
                discovered_tables=discovered,
                minimum_items=int(config.get("minimum_items") or 1000),
            )
            _atomic_write(output_path, payload)
            return payload
        finally:
            await context.close()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="One-time full backfill from the Xiaozhaoya Feishu Base"
    )
    parser.add_argument(
        "--config", default=os.getenv("XIAOZHAOYA_CAPTURE_CONFIG", "config/xiaozhaoya.yaml")
    )
    parser.add_argument("--output", default=None)
    parser.add_argument(
        "--profile-dir", default=os.getenv("WANQING_PROFILE_DIR", ".wanqing-browser")
    )
    parser.add_argument(
        "--state-dir", default=os.getenv("WANQING_STATE_DIR", ".wanqing-state")
    )
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--login", action="store_true", help="open the dedicated browser for one-time Feishu login"
    )
    arguments = parser.parse_args(argv)

    state_dir = Path(arguments.state_dir)
    output_path = Path(arguments.output) if arguments.output else state_dir / "xiaozhaoya.json"
    try:
        config = load_config(Path(arguments.config))
        payload = asyncio.run(
            capture(
                config,
                output_path=output_path,
                profile_dir=Path(arguments.profile_dir),
                screenshot_dir=state_dir / "screenshots",
                headed=arguments.headed,
                login=arguments.login,
            )
        )
        qiuzhao, gongkao = split_records(payload["items"])
        print(
            json.dumps(
                {
                    "event": "xiaozhaoya_base_backfilled",
                    "mode": "one_time_foundation_backfill",
                    "scheduled": False,
                    "item_count": len(payload["items"]),
                    "qiuzhao": len(qiuzhao),
                    "gongkao": len(gongkao),
                    "captured_tables": payload["status"]["captured_tables"],
                    "output": str(output_path),
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception:
        LOGGER.exception("Xiaozhaoya one-time Base backfill failed; existing snapshot preserved")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
