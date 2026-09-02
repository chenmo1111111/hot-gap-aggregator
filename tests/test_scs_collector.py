import json
from pathlib import Path

import pytest

from app.collectors.scs import SCSCollector


FIXTURES = Path(__file__).parent / "fixtures"


def test_scs_discovers_current_exam_id() -> None:
    script = 'neu.examSelect=!1,neu.hb01Id=neu.examSelect?"":"8a81-current",neu.ahb015="中央机关"'
    assert SCSCollector.discover_exam_id(script) == "8a81-current"


def test_scs_fixture_keeps_only_important_notices() -> None:
    payload = json.loads((FIXTURES / "scs_articles.json").read_text(encoding="utf-8"))
    items = SCSCollector.parse(payload)
    assert [item.extra["article_id"] for item in items] == ["notice-1", "outline-1"]
    assert all(item.source == "gongkao" for item in items)
    assert all(item.extra["subsource"] == "scs" for item in items)
    assert all(item.extra["exam_type"] == "国考" for item in items)
    assert items[0].url.startswith("http://bm.scs.gov.cn/")
    assert items[1].url == "https://example.test/outline"


@pytest.mark.asyncio
async def test_scs_fetch_uses_discovered_official_endpoint(monkeypatch) -> None:
    collector = SCSCollector()
    calls: list[str] = []

    class Response:
        def __init__(self, text: str = "", payload: dict | None = None) -> None:
            self.text = text
            self._payload = payload

        def json(self) -> dict:
            return self._payload or {}

    payload = json.loads((FIXTURES / "scs_articles.json").read_text(encoding="utf-8"))

    async def request(url: str, **_kwargs):
        calls.append(url)
        if url.endswith("core-constant.js"):
            return Response('neu.hb01Id=neu.examSelect?"":"exam-2027"')
        return Response(payload=payload)

    monkeypatch.setattr(collector, "request", request)
    items = await collector.fetch()
    assert calls[1].endswith("/exam-2027")
    assert len(items) == 2
