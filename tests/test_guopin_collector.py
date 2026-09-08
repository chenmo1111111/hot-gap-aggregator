import json
from pathlib import Path

import pytest

from app.collectors.guopin import GuopinCollector, parse_guopin


def test_guopin_mapping_and_central_soe_detection() -> None:
    payload = json.loads((Path(__file__).parent / "fixtures" / "guopin_jobs.json").read_text(encoding="utf-8"))
    items = parse_guopin(payload, "生物", ["北京"], 25)
    assert len(items) == 1
    item = items[0]
    assert item.title == "生物信息算法工程师"
    assert item.extra["company"] == "中央示例集团"
    assert item.extra["education"] == "硕士"
    assert item.extra["city"] == "北京-海淀区"
    assert item.extra["recruitment_type"] == "校园招聘"
    assert item.extra["is_central_soe"] is True
    assert item.summary_zh == "负责单细胞数据分析与算法开发"
    assert item.url == "https://www.iguopin.com/job/detail?id=gp-001&source=campus"


@pytest.mark.asyncio
async def test_guopin_fetch_paginates_until_query_limit(tmp_path, monkeypatch) -> None:
    config = tmp_path / "guopin.yaml"
    config.write_text(
        "api_url: https://api.test/jobs\nkeywords: [工程]\nprovinces: [北京]\n"
        "job_nature: []\nlookback_days: 3650\npage_size: 2\nmax_pages: 3\nper_query_limit: 3\n",
        encoding="utf-8",
    )
    calls: list[int] = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    async def fake_post(_url, body):
        page = body["search"]["page"]
        calls.append(page)
        count = 2 if page == 1 else 1
        rows = [{
            "job_id": f"job-{page}-{index}", "job_name": f"工程师 {page}-{index}",
            "company_name": "示例集团", "district_list": [{"area_cn": "北京"}],
            "publish_time": "2026-09-08 10:00:00",
        } for index in range(count)]
        return Response({"data": {"list": rows}})

    collector = GuopinCollector(config)
    monkeypatch.setattr(collector, "_post", fake_post)
    items = await collector.fetch()

    assert calls == [1, 2]
    assert len(items) == 3
