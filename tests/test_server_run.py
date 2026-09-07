import json
from datetime import UTC, datetime

import pytest

from app.models import Item
from app import server_run
from app.server_run import (
    merge_campus_jobs_into_site, merge_scs_into_site,
    merge_xuandiao_into_site, merge_yingjiesheng_jobs_into_site,
    run_xhs_rules, write_server_heartbeat,
)
from app.store.database import Database


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


def test_merge_campus_jobs_uses_server_sidecar_without_touching_ci_jobs(tmp_path) -> None:
    ci = {"source": "jobs", "items": [{"source": "jobs", "url": "https://ci.test/1"}]}
    (tmp_path / "jobs.json").write_text(json.dumps(ci), encoding="utf-8")
    campus = Item(source="jobs", rank=1, title="高校招聘", title_zh="高校招聘", url="https://nefu.test/1", extra={"subsource": "campus", "school": "东北林业大学"})
    merge_campus_jobs_into_site(tmp_path, [campus], "2026-09-07T00:00:00+00:00")
    assert json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8")) == ci
    sidecar = json.loads((tmp_path / "server-jobs.json").read_text(encoding="utf-8"))
    assert sidecar["items"][0]["url"] == "https://nefu.test/1"
    assert sidecar["subsources"]["campus"]["item_count"] == 1


def test_merge_yingjiesheng_preserves_campus_and_replaces_owned_rows(tmp_path) -> None:
    campus = Item(source="jobs", rank=1, title="高校招聘", title_zh="高校招聘", url="https://nefu.test/1", extra={"subsource": "campus"})
    merge_campus_jobs_into_site(tmp_path, [campus], "2026-09-07T00:00:00+00:00")
    old = Item(source="jobs", rank=1, title="旧应届生", title_zh="旧应届生", url="https://yjs.test/old", extra={"subsource": "yingjiesheng"})
    merge_yingjiesheng_jobs_into_site(tmp_path, [old], "2026-09-07T01:00:00+00:00")
    fresh = Item(source="jobs", rank=1, title="海投岗位", title_zh="海投岗位", url="https://haitou.test/new", extra={"subsource": "haitou"})
    merge_yingjiesheng_jobs_into_site(tmp_path, [fresh], "2026-09-07T02:00:00+00:00")
    sidecar = json.loads((tmp_path / "server-jobs.json").read_text(encoding="utf-8"))
    assert [row["url"] for row in sidecar["items"]] == ["https://haitou.test/new", "https://nefu.test/1"]
    assert sidecar["subsources"]["yingjiesheng"]["item_count"] == 1


def test_write_server_heartbeat_is_atomic_and_uses_requested_time(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SERVER_HEARTBEAT_PATH", raising=False)
    path = write_server_heartbeat(tmp_path, "2026-09-07T03:00:00+00:00")
    assert path == tmp_path / "server-heartbeat.txt"
    assert path.read_text(encoding="utf-8") == "2026-09-07T03:00:00+00:00"
    assert not (tmp_path / "server-heartbeat.txt.tmp").exists()


@pytest.mark.asyncio
async def test_xhs_rule_job_failure_is_isolated(monkeypatch, tmp_path) -> None:
    class BrokenWatcher:
        def __init__(self, _database):
            pass

        async def run(self):
            raise RuntimeError("page changed")

    monkeypatch.setattr(server_run, "XhsRuleWatcher", BrokenWatcher)
    database = Database(tmp_path / "server.db")
    assert await run_xhs_rules(database) == {"status": "degraded", "error": "page changed"}
    database.close()
