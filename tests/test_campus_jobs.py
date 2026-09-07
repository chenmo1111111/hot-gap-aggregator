from pathlib import Path

import pytest

from app.store.database import Database
from app.watchers.campus_jobs import CampusJobsWatcher, decode_gxbys_embedded_html, parse_campus_html


FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_nefu_campus_list() -> None:
    entries = parse_campus_html((FIXTURES / "campus_jobs_initial.html").read_text(encoding="utf-8"), "https://nefu.gxbys.org.cn/campus")
    assert entries == [{"title": "示例科技2027届校园招聘公告", "url": "https://nefu.gxbys.org.cn/campus/view/id/1", "date": "2026-09-06"}]


def test_parse_current_gxbys_compressed_list() -> None:
    html = (FIXTURES / "campus_jobs_gxbys_compressed.html").read_text(encoding="utf-8")
    decoded = decode_gxbys_embedded_html(html)
    assert decoded and "东北林业大学2027届秋季校园招聘会" in decoded
    assert parse_campus_html(html, "https://nefu.gxbys.org.cn/campus") == [{
        "title": "东北林业大学2027届秋季校园招聘会",
        "url": "https://nefu.gxbys.org.cn/campus/view/id/70001",
        "date": "2026-09-07",
    }]


@pytest.mark.asyncio
async def test_campus_watcher_baseline_then_pushes_new_selection_to_both_feeds(monkeypatch, tmp_path) -> None:
    config = tmp_path / "campus.yaml"
    config.write_text("list_pages:\n  - {school: 东北林业大学, url: 'https://nefu.test/campus', city: 哈尔滨, province: 黑龙江}\ntitle_keywords: [招聘, 宣讲, 选调, 定向]\n", encoding="utf-8")
    database = Database(tmp_path / "server.db")
    sent = []

    async def notify(alert):
        sent.append(alert)
        return {"feishu": "ok"}

    watcher = CampusJobsWatcher(database, config, notifier=notify, alerts_path=tmp_path / "alerts.json")
    html = (FIXTURES / "campus_jobs_initial.html").read_text(encoding="utf-8")

    async def fetch(_url):
        return html

    monkeypatch.setattr(watcher, "_fetch_response", fetch)
    first = await watcher.run()
    assert first["list_pages"][0]["status"] == "baseline"
    assert len(watcher.latest_items) == 1
    assert sent == []

    html = (FIXTURES / "campus_jobs_updated.html").read_text(encoding="utf-8")
    second = await watcher.run()
    assert second["list_pages"][0]["status"] == "pushed"
    assert len(sent) == 1
    assert sent[0]["priority"] == "highest"
    assert len(watcher.latest_items) == 2
    assert len(watcher.latest_gongkao_items) == 1
    assert watcher.latest_gongkao_items[0].extra["exam_type"] == "选调生"
    database.close()
