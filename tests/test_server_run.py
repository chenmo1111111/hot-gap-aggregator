import json
from datetime import UTC, datetime

from app.models import Item
from app.server_run import merge_scs_into_site, merge_xuandiao_into_site


def test_merge_scs_into_deployed_json(tmp_path) -> None:
    all_payload = {
        "generated_at": "old", "sources": [{"source": "gongkao", "status": "ok", "item_count": 1}],
        "items": [{"source": "gongkao", "rank": 1, "url": "https://fenbi.test/1", "extra": {"sub": "timeline"}}],
    }
    gongkao_payload = {
        "generated_at": "old", "status": {"source": "gongkao", "status": "ok", "item_count": 1},
        "items": all_payload["items"],
    }
    (tmp_path / "all.json").write_text(json.dumps(all_payload), encoding="utf-8")
    (tmp_path / "gongkao.json").write_text(json.dumps(gongkao_payload), encoding="utf-8")
    official = Item(
        source="gongkao", rank=1, title="国考公告", title_zh="国考公告", url="https://scs.test/1",
        extra={"subsource": "scs", "exam_type": "国考"},
    )
    merge_scs_into_site(tmp_path, [official], datetime(2026, 9, 3, tzinfo=UTC).isoformat())
    merged = json.loads((tmp_path / "gongkao.json").read_text(encoding="utf-8"))
    assert [item["url"] for item in merged["items"]] == ["https://scs.test/1", "https://fenbi.test/1"]
    assert merged["status"]["item_count"] == 2
    combined = json.loads((tmp_path / "all.json").read_text(encoding="utf-8"))
    assert any(item.get("extra", {}).get("subsource") == "scs" for item in combined["items"])


def test_merge_xuandiao_preserves_scs_and_replaces_previous_xuandiao(tmp_path) -> None:
    items = [
        {"source": "gongkao", "rank": 1, "url": "https://scs.test/1", "extra": {"subsource": "scs"}},
        {"source": "gongkao", "rank": 2, "url": "https://old.test/", "extra": {"subsource": "xuandiao"}},
    ]
    (tmp_path / "all.json").write_text(json.dumps({"generated_at": "old", "sources": [{"source": "gongkao", "status": "ok", "item_count": 2}], "items": items}), encoding="utf-8")
    (tmp_path / "gongkao.json").write_text(json.dumps({"generated_at": "old", "status": {"source": "gongkao", "status": "ok", "item_count": 2}, "items": items}), encoding="utf-8")
    notice = Item(source="gongkao", rank=1, title="选调生公告", title_zh="选调生公告", url="https://new.test/", extra={"subsource": "xuandiao", "exam_type": "选调生"})
    merge_xuandiao_into_site(tmp_path, [notice], datetime(2026, 9, 3, tzinfo=UTC).isoformat())
    merged = json.loads((tmp_path / "gongkao.json").read_text(encoding="utf-8"))
    assert [item["url"] for item in merged["items"]] == ["https://new.test/", "https://scs.test/1"]
    assert merged["status"]["server_xuandiao"] == "ok"
