import pytest

from app.collectors.base import SourceUnavailable
from app.collectors.jobs import JobsCollector
from app.collectors.guopin import GuopinCollector
from app.collectors.job_radar import JobRadarCollector
from app.models import Item


class Provider:
    source = "jobs"

    def __init__(self, result):
        self.result = result

    async def fetch(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.asyncio
async def test_jobs_collector_combines_sources_and_isolates_provider_failure() -> None:
    shared = Item(source="jobs", rank=1, title="算法工程师", title_zh="算法工程师", url="https://a", published_at="2026-09-06", extra={"company": "公司", "keywords_hit": ["算法"], "subsource": "tencent"})
    duplicate = Item(source="jobs", rank=1, title="算法工程师", title_zh="算法工程师", url="https://b", published_at="2026-09-06", extra={"company": "公司", "keywords_hit": ["生物"], "subsource": "yingjiesheng"})
    collector = JobsCollector([Provider([shared]), Provider([duplicate]), Provider(SourceUnavailable("offline"))])
    items = await collector.fetch()
    assert len(items) == 1
    assert items[0].extra["keywords_hit"] == ["算法", "生物"]
    assert items[0].rank == 1


@pytest.mark.asyncio
async def test_jobs_collector_reuses_shared_title_noise_filter() -> None:
    useful = Item(
        source="jobs", rank=1, title="算法工程师", title_zh="算法工程师",
        url="https://jobs.example/apply", extra={"company": "示例公司"},
    )
    noisy = Item(
        source="jobs", rank=2, title="示例公司空中宣讲会", title_zh="示例公司空中宣讲会",
        url="https://jobs.example/talk", extra={"company": "示例公司"},
    )
    collector = JobsCollector([Provider([useful, noisy])])
    items = await collector.fetch()
    assert items == [useful]
    assert collector.filter_stats == {"input": 2, "kept": 1, "noise_dropped": 1}
    assert collector.filtered_samples[0]["title"] == "示例公司空中宣讲会"


def test_jobs_collector_skips_yingjiesheng_when_server_owns_it(monkeypatch) -> None:
    monkeypatch.setenv("YINGJIESHENG_ON_SERVER", "true")
    providers = JobsCollector().providers
    assert [type(provider) for provider in providers] == [JobRadarCollector, GuopinCollector]
