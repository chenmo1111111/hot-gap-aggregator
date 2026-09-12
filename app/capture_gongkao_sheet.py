"""Capture a read-only Feishu Sheets tab as a Gongkao snapshot.

The source spreadsheet does not grant OpenAPI/export access.  This collector
uses the same dedicated persistent browser profile as the Wanqing collector,
reuses only the live page's CSRF/member identifiers in memory, and never
exports cookies.  Feishu's protobuf cell block is decoded locally.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import gzip
import hashlib
import json
import logging
import os
import re
import struct
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import httpx
import yaml
from dotenv import load_dotenv


LOGGER = logging.getLogger(__name__)
EXCEL_EPOCH = date(1899, 12, 30)
EXPECTED_HEADERS = (
    "日期",
    "类别",
    "公告标题",
    "招聘人数",
    "截止日期",
    "工作地点",
    "所属省份",
    "学历要求",
    "公告链接",
    "备注",
)
INSTITUTION_HEADERS = (
    "发布时间",
    "招聘公告",
    "招聘人数",
    "最低学历要求",
    "专业要求",
    "岗位",
    "单位名称",
    "省份",
    "城市",
    "开始日期",
    "截止日期",
    "原标题",
    "信息来源",
)


class CaptureError(RuntimeError):
    """Raised when the source cannot be captured or validated safely."""


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if offset >= len(data) or shift >= 70:
            raise CaptureError("飞书单元格块包含无效 varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7


def _parse_fields(data: bytes) -> list[tuple[int, int, object]]:
    fields: list[tuple[int, int, object]] = []
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        number, wire = key >> 3, key & 7
        if number <= 0:
            raise CaptureError("飞书单元格块包含无效字段号")
        if wire == 0:
            value, offset = _read_varint(data, offset)
        elif wire == 1:
            value = data[offset : offset + 8]
            offset += 8
        elif wire == 2:
            length, offset = _read_varint(data, offset)
            value = data[offset : offset + length]
            if len(value) != length:
                raise CaptureError("飞书单元格块被截断")
            offset += length
        elif wire == 5:
            value = data[offset : offset + 4]
            offset += 4
        else:
            raise CaptureError(f"飞书单元格块使用不支持的 wire type {wire}")
        fields.append((number, wire, value))
    return fields


def _length_fields(data: bytes, number: int) -> list[bytes]:
    return [
        bytes(value)
        for field_number, wire, value in _parse_fields(data)
        if field_number == number and wire == 2
    ]


def _first_length_field(data: bytes, number: int) -> bytes:
    values = _length_fields(data, number)
    if not values:
        raise CaptureError(f"飞书单元格块缺少字段 {number}")
    return values[0]


def _varint_field(data: bytes, number: int, default: int = 0) -> int:
    for field_number, wire, value in _parse_fields(data):
        if field_number == number and wire == 0:
            return int(value)
    return default


def _packed_varints(data: bytes) -> list[int]:
    values: list[int] = []
    offset = 0
    while offset < len(data):
        value, offset = _read_varint(data, offset)
        values.append(value)
    return values


def _decode_gzip_base64(value: str) -> bytes:
    try:
        return gzip.decompress(base64.b64decode(value))
    except (ValueError, OSError) as exc:
        raise CaptureError("飞书返回了无法解析的压缩数据") from exc


def decode_cell_block(encoded: str) -> tuple[dict[str, int], list[list[object]]]:
    """Decode one Feishu Sheets cell block without generated protobuf classes."""
    block = _decode_gzip_base64(encoded)
    root = _first_length_field(block, 1)
    envelope = _first_length_field(root, 2)
    sheet = _first_length_field(envelope, 12)

    range_message = _first_length_field(sheet, 1)
    bounds = {
        "row_start": _varint_field(range_message, 1),
        "col_start": _varint_field(range_message, 2),
        "row_end": _varint_field(range_message, 3),
        "col_end": _varint_field(range_message, 4),
    }
    content = _first_length_field(sheet, 2)
    cells = _first_length_field(sheet, 6)
    number_bytes = _first_length_field(content, 1)
    if len(number_bytes) % 8:
        raise CaptureError("飞书数值表长度异常")
    numbers = struct.unpack(f"<{len(number_bytes) // 8}d", number_bytes)
    strings = [value.decode("utf-8") for value in _length_fields(content, 2)]
    rich_texts = _length_fields(content, 3)
    cell_messages = _length_fields(cells, 2)
    indices = _packed_varints(_first_length_field(cells, 1))

    row_count = bounds["row_end"] - bounds["row_start"]
    col_count = bounds["col_end"] - bounds["col_start"]
    if row_count <= 0 or col_count <= 0 or len(indices) != row_count * col_count:
        raise CaptureError(
            f"飞书单元格索引尺寸异常：{len(indices)} != {row_count}x{col_count}"
        )

    def decode_cell(message_index: int) -> object:
        if message_index == 0:
            return ""
        if message_index >= len(cell_messages):
            raise CaptureError("飞书单元格引用越界")
        message = cell_messages[message_index]
        value_type = _varint_field(message, 1)
        value_index = _varint_field(message, 2)
        try:
            if value_type == 2:
                return numbers[value_index]
            if value_type == 3:
                return strings[value_index]
            if value_type == 6:
                rich_text = rich_texts[value_index]
                return strings[_varint_field(rich_text, 1)]
        except (IndexError, UnicodeDecodeError) as exc:
            raise CaptureError("飞书单元格值引用越界或编码异常") from exc
        return ""

    rows = [
        [decode_cell(indices[row * col_count + col]) for col in range(col_count)]
        for row in range(row_count)
    ]
    return bounds, rows


def decode_sheet_rows(
    envelope: Mapping[str, Any], sheet_id: str, *, allow_partial_blocks: bool = False
) -> list[list[object]]:
    data = envelope.get("data") if isinstance(envelope.get("data"), Mapping) else {}
    snapshot = data.get("snapshot") if isinstance(data.get("snapshot"), Mapping) else {}
    blocks = snapshot.get("blocks") if isinstance(snapshot.get("blocks"), Mapping) else {}
    if not blocks:
        raise CaptureError("飞书表格响应缺少单元格块")

    block_meta_raw = snapshot.get("gzipBlockMeta")
    if not isinstance(block_meta_raw, str):
        raise CaptureError("飞书表格响应缺少块索引")
    try:
        block_meta = json.loads(_decode_gzip_base64(block_meta_raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureError("飞书块索引无法解析") from exc
    sheet_meta = block_meta.get(sheet_id) if isinstance(block_meta, Mapping) else None
    metas = sheet_meta.get("cellBlockMetas") if isinstance(sheet_meta, Mapping) else None
    if not isinstance(metas, list) or not metas:
        raise CaptureError(f"飞书块索引不含目标页签 {sheet_id}")

    rows_by_number: dict[int, list[object]] = {}
    for meta in metas:
        if not isinstance(meta, Mapping):
            continue
        block_id = str(meta.get("blockId") or "")
        encoded = blocks.get(block_id)
        if not isinstance(encoded, str) and allow_partial_blocks:
            continue
        if not isinstance(encoded, str):
            raise CaptureError(f"飞书响应缺少块 {block_id}")
        bounds, rows = decode_cell_block(encoded)
        meta_range = meta.get("range") if isinstance(meta.get("range"), Mapping) else {}
        row_start = int(meta_range.get("rowStart", bounds["row_start"]))
        for offset, row in enumerate(rows):
            rows_by_number[row_start + offset] = row
    if not rows_by_number:
        raise CaptureError("目标页签没有可解析行")
    return [rows_by_number[number] for number in sorted(rows_by_number)]


def _sheet_block_metas(envelope: Mapping[str, Any], sheet_id: str) -> list[Mapping[str, Any]]:
    data = envelope.get("data") if isinstance(envelope.get("data"), Mapping) else {}
    snapshot = data.get("snapshot") if isinstance(data.get("snapshot"), Mapping) else {}
    raw = snapshot.get("gzipBlockMeta")
    if not isinstance(raw, str):
        raise CaptureError("飞书表格响应缺少块索引")
    try:
        block_meta = json.loads(_decode_gzip_base64(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureError("飞书块索引无法解析") from exc
    sheet_meta = block_meta.get(sheet_id) if isinstance(block_meta, Mapping) else None
    metas = sheet_meta.get("cellBlockMetas") if isinstance(sheet_meta, Mapping) else None
    if not isinstance(metas, list) or not metas:
        raise CaptureError(f"飞书块索引不含目标页签 {sheet_id}")
    return [meta for meta in metas if isinstance(meta, Mapping)]


def _normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _excel_date(value: object) -> str:
    if isinstance(value, (int, float)) and 30_000 <= value <= 80_000:
        return (EXCEL_EPOCH + timedelta(days=int(value))).isoformat()
    text = _text(value)
    for pattern in (r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?",):
        match = re.fullmatch(pattern, text)
        if match:
            try:
                return date(*(int(part) for part in match.groups())).isoformat()
            except ValueError:
                return ""
    return ""


def _url_key(value: object) -> str:
    url = _text(value)
    if not url.startswith(("http://", "https://")):
        return ""
    return re.sub(r"^https?://", "", url.split("#", 1)[0].rstrip("/").casefold())


def _city_hint(location: str, province: str) -> str:
    if not location or any(marker in location for marker in ("详见", "正文", "招聘单位", "不限", "全国")):
        return ""
    for token in re.split(r"[、,，/;；\s]+", location):
        token = token.strip()
        if not token or token in {province, province.removesuffix("省")}:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]{2,10}(?:市|州|地区|盟|县|区)", token):
            return token
    return ""


def build_snapshot(
    rows: list[list[object]],
    config: Mapping[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    header_index: int | None = None
    schema = ""
    for index, row in enumerate(rows):
        values = tuple(_text(value) for value in row)
        if values[: len(EXPECTED_HEADERS)] == EXPECTED_HEADERS:
            header_index, schema = index, "major_exams"
            break
        if values[: len(INSTITUTION_HEADERS)] == INSTITUTION_HEADERS:
            header_index, schema = index, "institutions"
            break
    if header_index is None:
        raise CaptureError("目标页签字段发生变化，未找到预期表头；旧快照已保留")

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source_row, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if schema == "institutions":
            padded = [*row[: len(INSTITUTION_HEADERS)], *([""] * len(INSTITUTION_HEADERS))]
            published = _excel_date(padded[0])
            category = "事业单位"
            title = _text(padded[1])
            recruit_count = _text(padded[2])
            education = _text(padded[3])
            major = _text(padded[4])
            position = _text(padded[5])
            unit = _text(padded[6])
            province = _text(padded[7]) or "全国"
            city = _text(padded[8])
            signup_start = _excel_date(padded[9])
            deadline = _excel_date(padded[10])
            original_title = _text(padded[11])
            url = _text(padded[12])
            source_note = _text(padded[13]) if len(padded) > 13 else ""
            if published and deadline and deadline < published:
                deadline = ""
            if published and signup_start and signup_start < published:
                signup_start = ""
            location = city or province
        else:
            padded = [*row[: len(EXPECTED_HEADERS)], *([""] * len(EXPECTED_HEADERS))]
            published = _excel_date(padded[0])
            category = _text(padded[1])
            title = _text(padded[2])
            recruit_count = _text(padded[3])
            deadline = _excel_date(padded[4])
            location = _text(padded[5])
            province = _text(padded[6]) or "全国"
            education = _text(padded[7])
            url = _text(padded[8])
            source_note = _text(padded[9])
            major = position = unit = signup_start = original_title = ""
        if not title or not _url_key(url):
            continue
        dedupe_key = _url_key(url) or f"{_normalize(title)}|{_normalize(province)}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        sync_hash = hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:24]
        note_parts = [source_note]
        if published:
            note_parts.append(f"公告日期：{published}")
        if location:
            note_parts.append(f"工作地点：{location}")
        if education:
            note_parts.append(f"学历要求：{education}")
        city = city if schema == "institutions" else _city_hint(location, province)
        selected.append(
            {
                "source": "gongkao",
                "rank": len(selected) + 1,
                "title": title,
                "title_zh": title,
                "url": url,
                "published_at": published,
                "summary": "；".join(part for part in note_parts if part),
                "source_label": "购买表-公考",
                "extra": {
                    "id": f"gongkao-sheet:{sync_hash}",
                    "subsource": "feishu_sheet",
                    "source_row": source_row,
                    "exam_type": category,
                    "province": province,
                    "city": city,
                    "unit": unit or None,
                    "position": position or None,
                    "major": major or None,
                    "recruit_count": recruit_count,
                    "startSignUpTime": signup_start or None,
                    "endSignUpTime": deadline,
                    "original_title": original_title or None,
                    "fresh_graduate": "应届" in f"{title}{source_note}{education}",
                    "notes": "；".join(part for part in note_parts if part),
                    "source_label": "购买表-公考",
                    "upstream_source": "feishu_sheet",
                },
            }
        )
        if len(selected) >= int(config.get("max_items") or 2_000):
            break

    minimum = int(config.get("min_items") or 500)
    if len(selected) < minimum:
        raise CaptureError(
            f"安全校验失败：只得到 {len(selected)} 条，低于最低要求 {minimum} 条；旧快照已保留"
        )
    return {
        "generated_at": generated_at or datetime.now().astimezone().isoformat(),
        "source": "gongkao",
        "status": {
            "source": "gongkao",
            "item_count": len(selected),
            "upstream_source": "feishu_sheet",
            "source_label": "购买表-公考",
            "source_sheet": str(config.get("source_sheet") or "公务员&事业单位等重大考试专栏"),
            "source_rows": max(0, len(rows) - header_index - 1),
            "deduplicated_by": "announcement_url",
        },
        "items": selected,
    }


def load_config(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("公考表抓取配置必须是 YAML 对象")
    required = ("page_url", "api_url", "token", "sheet_id")
    missing = [name for name in required if not value.get(name)]
    if missing:
        raise ValueError(f"公考表抓取配置缺少：{', '.join(missing)}")
    return value


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _failure_count(path: Path) -> int:
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("count") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def _notify(message: str) -> None:
    providers: list[tuple[str, dict[str, Any]]] = []
    if webhook := os.getenv("FEISHU_WEBHOOK", "").strip():
        providers.append((webhook, {"msg_type": "text", "content": {"text": message}}))
    if bark_url := os.getenv("BARK_URL", "").strip():
        providers.append((bark_url, {"title": "公考表抓取连续失败", "body": message, "group": "hot-gap"}))
    for url, body in providers:
        try:
            httpx.post(url, json=body, timeout=10, follow_redirects=True).raise_for_status()
        except httpx.HTTPError as exc:
            LOGGER.warning("failed to send Gongkao sheet alert: %s", exc)


async def capture(
    config: Mapping[str, Any],
    *,
    output_path: Path,
    profile_dir: Path,
    screenshot_dir: Path,
    login: bool = False,
    headless: bool = False,
) -> dict[str, Any]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise CaptureError("缺少 Playwright，请先安装 requirements.txt") from exc

    profile_dir.mkdir(parents=True, exist_ok=True)
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    screenshot_path = screenshot_dir / f"gongkao_sheet_{stamp}.png"
    state = {"csrf": "", "member_id": ""}

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless and not login,
            viewport={"width": 1600, "height": 1000},
            args=[] if login or headless else ["--start-minimized"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        async def inspect_request(request: Any) -> None:
            if "member_id=" in request.url:
                state["member_id"] = request.url.split("member_id=", 1)[1].split("&", 1)[0]
            if not state["csrf"] and request.method == "POST":
                headers = await request.all_headers()
                for name in ("x-csrftoken", "x-csrf-token", "x-xsrf-token"):
                    if headers.get(name):
                        state["csrf"] = headers[name]
                        break

        page.on("request", inspect_request)
        try:
            page_url = str(config["page_url"])
            await page.goto(page_url, wait_until="domcontentloaded", timeout=120_000)
            deadline = asyncio.get_running_loop().time() + (600 if login else 120)
            while True:
                await page.wait_for_timeout(2_000)
                title = await page.title()
                if "没有权限访问" in title:
                    switch = page.get_by_text("切换并访问", exact=True)
                    if await switch.count() and await switch.first.is_visible():
                        await switch.first.click()
                        await page.wait_for_timeout(5_000)
                        continue
                if page.url.startswith("https://my.feishu.cn/sheets/") and "没有权限访问" not in title:
                    break
                if not login and "accounts/page/login" in page.url:
                    raise CaptureError("飞书个人版登录已失效，请重新执行 --login")
                if asyncio.get_running_loop().time() >= deadline:
                    raise CaptureError("等待可访问的飞书公考表超时")
            if login:
                print("已检测到可访问账号，正在验证完整公考表数据。")

            state["member_id"] = ""
            await page.goto(page_url, wait_until="domcontentloaded", timeout=120_000)
            wait_until = asyncio.get_running_loop().time() + 90
            while (not state["csrf"] or not state["member_id"]) and asyncio.get_running_loop().time() < wait_until:
                await page.wait_for_timeout(1_000)
            if not state["csrf"] or not state["member_id"]:
                raise CaptureError("无法取得飞书实时协作会话参数")
            await page.screenshot(path=str(screenshot_path), full_page=False)

            request_payload = {
                "sheetRange": {"sheetId": str(config["sheet_id"])},
                "openType": 0,
                "memberId": int(state["member_id"]),
                "token": str(config["token"]),
                "schemaVersion": 9,
                "clientVersion": "v0.0.1",
            }
            response = await context.request.post(
                str(config["api_url"]),
                data=request_payload,
                headers={"Referer": page_url, "x-csrftoken": state["csrf"]},
                timeout=120_000,
            )
            try:
                envelope = await response.json()
            except json.JSONDecodeError as exc:
                raise CaptureError(f"飞书完整表接口返回非 JSON（HTTP {response.status}）") from exc
            if response.status != 200 or envelope.get("code") != 0:
                raise CaptureError(
                    f"飞书完整表接口失败（HTTP {response.status}, code={envelope.get('code')}, msg={envelope.get('msg')})"
                )
            if config.get("fetch_all_blocks"):
                sheet_id = str(config["sheet_id"])
                metas = _sheet_block_metas(envelope, sheet_id)
                snapshot = envelope["data"]["snapshot"]
                blocks = snapshot["blocks"]
                # The endpoint caps one response at roughly ten blocks. Fetch
                # bounded groups and merge them into the original envelope.
                for offset in range(0, len(metas), 8):
                    chunk = metas[offset : offset + 8]
                    expected = {str(meta.get("blockId") or "") for meta in chunk}
                    if expected.issubset(blocks):
                        continue
                    ranges = [meta.get("range") for meta in chunk if isinstance(meta.get("range"), Mapping)]
                    if not ranges:
                        continue
                    cell_range = {
                        "rowStart": min(int(value.get("rowStart") or 0) for value in ranges),
                        "rowEnd": max(int(value.get("rowEnd") or 0) for value in ranges),
                        "colStart": min(int(value.get("colStart") or 0) for value in ranges),
                        "colEnd": max(int(value.get("colEnd") or 0) for value in ranges),
                    }
                    ranged_payload = {
                        **request_payload,
                        "sheetRange": {"sheetId": sheet_id, "range": cell_range},
                    }
                    ranged_response = await context.request.post(
                        str(config["api_url"]),
                        data=ranged_payload,
                        headers={"Referer": page_url, "x-csrftoken": state["csrf"]},
                        timeout=120_000,
                    )
                    ranged_envelope = await ranged_response.json()
                    if ranged_response.status != 200 or ranged_envelope.get("code") != 0:
                        raise CaptureError(
                            f"飞书分块接口失败（HTTP {ranged_response.status}, code={ranged_envelope.get('code')}）"
                        )
                    ranged_snapshot = (ranged_envelope.get("data") or {}).get("snapshot") or {}
                    for block_id, encoded in (ranged_snapshot.get("blocks") or {}).items():
                        blocks.setdefault(block_id, encoded)
                    await asyncio.sleep(0.1)
            rows = decode_sheet_rows(
                envelope,
                str(config["sheet_id"]),
                allow_partial_blocks=bool(config.get("allow_partial_blocks")),
            )
            payload = build_snapshot(rows, config)
            _atomic_write(output_path, payload)
            return payload
        finally:
            await context.close()


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Capture a Gongkao snapshot from a shared Feishu Sheet")
    parser.add_argument(
        "--config",
        default=os.getenv("GONGKAO_SHEET_CAPTURE_CONFIG", "config/gongkao_sheet_capture.yaml"),
    )
    parser.add_argument("--output", default=None)
    parser.add_argument("--profile-dir", default=os.getenv("WANQING_PROFILE_DIR", ".wanqing-browser"))
    parser.add_argument("--state-dir", default=os.getenv("WANQING_STATE_DIR", ".wanqing-state"))
    parser.add_argument("--login", action="store_true")
    parser.add_argument("--headless", action="store_true")
    arguments = parser.parse_args()

    state_dir = Path(arguments.state_dir)
    output_path = Path(arguments.output) if arguments.output else Path(
        os.getenv("SITE_DATA_DIR", "/var/www/hot-gap/data")
    ) / "gongkao_sheet.json"
    failure_path = state_dir / "gongkao_sheet_capture_failures.json"
    try:
        config = load_config(Path(arguments.config))
        payload = asyncio.run(
            capture(
                config,
                output_path=output_path,
                profile_dir=Path(arguments.profile_dir),
                screenshot_dir=state_dir / "screenshots",
                login=arguments.login,
                headless=arguments.headless,
            )
        )
        _atomic_write(failure_path, {"count": 0})
        print(
            json.dumps(
                {
                    "event": "gongkao_sheet_captured",
                    "item_count": len(payload["items"]),
                    "source_rows": payload["status"]["source_rows"],
                    "output": str(output_path),
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:
        count = _failure_count(failure_path) + 1
        _atomic_write(failure_path, {"count": count, "last_error": str(exc)})
        LOGGER.exception("Gongkao sheet capture failed; previous snapshot preserved")
        if os.getenv("CAPTURE_RUNNER_MANAGED") != "1" and count >= 2 and (os.getenv("FEISHU_WEBHOOK") or os.getenv("BARK_URL")):
            _notify(f"飞书公考表抓取连续失败 {count} 次：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
