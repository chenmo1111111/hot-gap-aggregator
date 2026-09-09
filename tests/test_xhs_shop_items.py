import json

import pytest

from app.watchers.xhs_shop_items import (
    XhsShopItems,
    extract_items_response,
    normalise_shop_item,
    page_payload,
)


def test_shop_item_mapping_and_page_payload() -> None:
    raw = {
        "item_id": "item-1",
        "item_name": "考研电子题库",
        "category_names": ["教育培训", "电子资源", "题库"],
        "buyable": True,
        "min_price": 990,
    }
    item = normalise_shop_item(raw)
    assert item == {
        "item_id": "item-1",
        "title": "考研电子题库",
        "category_path": "教育培训 > 电子资源 > 题库",
        "product_type": "虚拟",
        "status": "在售",
        "price": "990",
    }
    rows, total = extract_items_response({"data": {"items": [raw], "total": 81}})
    assert rows == [raw] and total == 81
    template = {"page_no": 1, "page_size": 20, "search_filter": {"buyable": True}}
    changed = page_payload(template, 2)
    assert changed["page_no"] == 2 and changed["page_size"] == 50
    assert template["page_no"] == 1


@pytest.mark.asyncio
async def test_shop_failure_preserves_previous_and_marks_stale(monkeypatch, tmp_path) -> None:
    path = tmp_path / "shop.json"
    previous = {
        "fetched_at": "2026-09-08T00:00:00+00:00",
        "shop_name": "测试店铺",
        "stale": False,
        "total": 1,
        "items": [{"item_id": "1", "title": "旧商品"}],
    }
    path.write_text(json.dumps(previous, ensure_ascii=False), encoding="utf-8")
    collector = XhsShopItems(path)

    async def fail(_url):
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(collector, "_fetch_all", fail)
    result = await collector.refresh("https://ark.example/list")
    assert result["stale"] is True
    assert result["items"] == previous["items"]
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["stale"] is True
    assert "last_attempt_at" in stored
