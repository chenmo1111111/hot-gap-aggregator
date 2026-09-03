import json
from pathlib import Path

import pytest

from app.collectors.base import SourceUnavailable
from app.collectors.job_radar import JobRadarCollector


FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_tencent_mapping() -> None:
    item = JobRadarCollector.parse_tencent(load("jobs_tencent.json"), "单细胞", 15)[0]
    assert item.title == "单细胞算法研究员"
    assert item.url == "https://careers.tencent.com/position_detail.php?id=1001"
    assert item.extra == {"company": "腾讯", "city": "深圳", "keywords_hit": ["单细胞"]}
    assert item.published_at and item.published_at.startswith("2026-09-02")


def test_bytedance_mapping_strips_html_and_builds_detail_url() -> None:
    item = JobRadarCollector.parse_bytedance(load("jobs_bytedance.json"), "蛋白质设计", 15)[0]
    assert item.extra["company"] == "字节跳动"
    assert item.extra["city"] == "北京"
    assert item.summary_zh == "负责蛋白质设计与分子大模型。"
    assert item.url.endswith("/74900001/detail")


@pytest.mark.asyncio
async def test_job_fetch_merges_keyword_hits_and_survives_bytedance_failure(monkeypatch, tmp_path) -> None:
    config = tmp_path / "jobs.yaml"
    config.write_text("keywords: [单细胞, 生物信息]\nper_keyword_limit: 15\n", encoding="utf-8")
    collector = JobRadarCollector(config)

    async def tencent(keywords, limit):
        first = JobRadarCollector.parse_tencent(load("jobs_tencent.json"), keywords[0], limit)
        again = JobRadarCollector.parse_tencent(load("jobs_tencent.json"), keywords[1], limit)
        return first + again

    async def bytedance(_keywords, _limit):
        raise SourceUnavailable("risk control", status="degraded")

    monkeypatch.setattr(collector, "_fetch_tencent", tencent)
    monkeypatch.setattr(collector, "_fetch_bytedance", bytedance)
    items = await collector.fetch()
    assert len(items) == 1
    assert items[0].rank == 1
    assert items[0].extra["keywords_hit"] == ["单细胞", "生物信息"]


@pytest.mark.asyncio
async def test_job_fetch_degrades_only_when_both_providers_fail(monkeypatch, tmp_path) -> None:
    config = tmp_path / "jobs.yaml"
    config.write_text("keywords: [单细胞]\n", encoding="utf-8")
    collector = JobRadarCollector(config)

    async def fail(*_args):
        raise RuntimeError("offline")

    monkeypatch.setattr(collector, "_fetch_tencent", fail)
    monkeypatch.setattr(collector, "_fetch_bytedance", fail)
    with pytest.raises(SourceUnavailable, match="All job providers failed"):
        await collector.fetch()
