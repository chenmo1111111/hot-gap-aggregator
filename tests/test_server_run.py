import json
from datetime import UTC, datetime

from app.models import Item
from app.server_run import merge_scs_into_site, merge_xuandiao_into_site


def test_merge_scs_writes_sidecar_without_touching_deployed_json(tmp_path) -> None:
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
    assert json.loads((tmp_path / "gongkao.json").read_text(encoding="utf-8")) == gongkao_payload
    assert json.loads((tmp_path / "all.json").read_text(encoding="utf-8")) == all_payload
    sidecar = json.loads((tmp_path / "server-gongkao.json").read_text(encoding="utf-8"))
    assert [item["url"] for item in sidecar["items"]] == ["https://scs.test/1"]
    assert sidecar["status"]["item_count"] == 1
    assert sidecar["subsources"]["scs"]["status"] == "ok"


def test_merge_xuandiao_sidecar_preserves_scs_and_replaces_previous_xuandiao(tmp_path) -> None:
    scs = Item(source="gongkao", rank=1, title="国考公告", title_zh="国考公告", url="https://scs.test/1", extra={"subsource": "scs"})
    merge_scs_into_site(tmp_path, [scs], datetime(2026, 9, 3, tzinfo=UTC).isoformat())
    old = Item(source="gongkao", rank=1, title="旧选调", title_zh="旧选调", url="https://old.test/", extra={"subsource": "xuandiao", "province": "山东"})
    merge_xuandiao_into_site(tmp_path, [old], datetime(2026, 9, 3, 1, tzinfo=UTC).isoformat())
    notice = Item(source="gongkao", rank=1, title="选调生公告", title_zh="选调生公告", url="https://new.test/", extra={"subsource": "xuandiao", "exam_type": "选调生"})
    merge_xuandiao_into_site(tmp_path, [notice], datetime(2026, 9, 3, tzinfo=UTC).isoformat())
    merged = json.loads((tmp_path / "server-gongkao.json").read_text(encoding="utf-8"))
    assert [item["url"] for item in merged["items"]] == ["https://new.test/", "https://scs.test/1"]
    assert merged["subsources"]["xuandiao"]["status"] == "ok"


def test_merge_xuandiao_preserves_failed_regions_from_previous_sidecar(tmp_path) -> None:
    old = Item(source="gongkao", rank=1, title="辽宁旧公告", title_zh="辽宁旧公告", url="https://old.test/liaoning", extra={"subsource": "xuandiao", "province": "辽宁"})
    merge_xuandiao_into_site(tmp_path, [old], "2026-09-03T00:00:00+00:00")
    fresh = Item(source="gongkao", rank=1, title="山东新公告", title_zh="山东新公告", url="https://new.test/shandong", extra={"subsource": "xuandiao", "province": "山东"})
    merge_xuandiao_into_site(tmp_path, [fresh], "2026-09-03T06:00:00+00:00", {"辽宁"})
    merged = json.loads((tmp_path / "server-gongkao.json").read_text(encoding="utf-8"))
    assert [item["url"] for item in merged["items"]] == ["https://new.test/shandong", "https://old.test/liaoning"]
