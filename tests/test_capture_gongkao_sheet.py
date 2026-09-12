from __future__ import annotations

import base64
import gzip
import struct

import pytest

from app.capture_gongkao_sheet import (
    CaptureError,
    EXPECTED_HEADERS,
    INSTITUTION_HEADERS,
    build_snapshot,
    decode_cell_block,
)


def _varint(value: int) -> bytes:
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _field_varint(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _field_bytes(number: int, value: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def test_decode_cell_block_handles_number_string_and_rich_text() -> None:
    strings = ["事业单位", "https://example.com/notice"]
    rich_text = _field_varint(1, 1)
    content = _field_bytes(1, struct.pack("<d", 46269.0))
    content += b"".join(_field_bytes(2, value.encode()) for value in strings)
    content += _field_bytes(3, rich_text)

    cell_messages = [
        b"",
        _field_varint(1, 2) + _field_varint(2, 0),
        _field_varint(1, 3) + _field_varint(2, 0),
        _field_varint(1, 6) + _field_varint(2, 0),
    ]
    cells = _field_bytes(1, b"".join(_varint(value) for value in (1, 2, 3)))
    cells += b"".join(_field_bytes(2, value) for value in cell_messages)
    bounds = b"".join(
        (
            _field_varint(1, 0),
            _field_varint(2, 0),
            _field_varint(3, 1),
            _field_varint(4, 3),
        )
    )
    sheet = _field_bytes(1, bounds) + _field_bytes(2, content) + _field_bytes(6, cells)
    protobuf = _field_bytes(1, _field_bytes(2, _field_bytes(12, sheet)))
    encoded = base64.b64encode(gzip.compress(protobuf)).decode()

    decoded_bounds, rows = decode_cell_block(encoded)

    assert decoded_bounds == {"row_start": 0, "col_start": 0, "row_end": 1, "col_end": 3}
    assert rows == [[46269.0, "事业单位", "https://example.com/notice"]]


def test_build_snapshot_maps_dates_location_link_and_deduplicates() -> None:
    rows = [
        list(EXPECTED_HEADERS),
        [
            46269,
            "事业单位",
            "示例公告",
            12,
            46276,
            "济南市",
            "山东省",
            "本科及以上，应届可报",
            "https://example.com/notice",
            "联考",
        ],
        [
            46269,
            "事业单位",
            "重复公告",
            12,
            46276,
            "济南市",
            "山东省",
            "本科",
            "https://example.com/notice/",
            "",
        ],
    ]

    result = build_snapshot(rows, {"min_items": 1, "max_items": 100})

    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["published_at"] == "2026-09-04"
    assert item["extra"]["endSignUpTime"] == "2026-09-11"
    assert item["extra"]["city"] == "济南市"
    assert item["extra"]["fresh_graduate"] is True
    assert item["url"] == "https://example.com/notice"
    assert item["extra"]["id"].startswith("gongkao-sheet:")


def test_build_snapshot_rejects_changed_headers() -> None:
    with pytest.raises(CaptureError, match="未找到预期表头"):
        build_snapshot([["不是预期表头"]], {"min_items": 1})


def test_build_snapshot_maps_real_institution_sheet_schema_and_ignores_stale_deadline() -> None:
    rows = [
        list(INSTITUTION_HEADERS),
        [
            46276, "2026年滁州来安县农业农村局公开招聘工作人员3名公告", 3,
            "大专", "详见岗位表", "政府购买服务工作人员", "来安县农业农村局",
            "安徽省", "来安县", 46278, 46281, "原标题",
            "https://www.xiaozhaoya.com/UrlRedirect?urlId=154960",
        ],
        [
            46276, "绵阳市疾病预防控制中心2026年招聘公告", 3,
            "本科", "详见正文", "卫生执法监督协管员", "绵阳市疾病预防控制中心",
            "四川省", "绵阳市", "尽快投递", 45917, "原标题",
            "https://www.xiaozhaoya.com/UrlRedirect?urlId=154913",
        ],
    ]

    result = build_snapshot(rows, {"min_items": 1, "max_items": 100, "source_sheet": "事业单位"})

    assert len(result["items"]) == 2
    assert result["items"][0]["extra"]["unit"] == "来安县农业农村局"
    assert result["items"][0]["extra"]["endSignUpTime"] == "2026-09-16"
    assert result["items"][1]["extra"]["endSignUpTime"] == ""
