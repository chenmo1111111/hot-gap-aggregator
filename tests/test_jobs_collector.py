import pytest

from app.collectors.base import SourceUnavailable
from app.collectors.jobs import JobsCollector
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
