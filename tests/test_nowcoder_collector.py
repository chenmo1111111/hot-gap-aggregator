from pathlib import Path

import pytest

from app.collectors.base import SourceUnavailable
from app.collectors.nowcoder import NowcoderCollector


FIXTURE = Path(__file__).parent / "fixtures" / "nowcoder.xml"


def test_nowcoder_rss_fixture_prioritizes_keyword_hits() -> None:
    items = NowcoderCollector.parse(FIXTURE.read_text(encoding="utf-8"), ["秋招", "offer", "变卦"])
    assert items[0].extra["keyword_hit"] == ["秋招", "offer", "变卦"]
    assert items[0].summary_zh.startswith("楼主分享")
    assert items[0].published_at == "2026-09-03T08:00:00+00:00"


@pytest.mark.asyncio
async def test_nowcoder_fetch_sorts_hits_before_newer_plain_post(monkeypatch, tmp_path) -> None:
    config = tmp_path / "nowcoder.yaml"
    config.write_text("routes: [/nowcoder/hots/2]\nkeywords: [秋招, offer, 变卦]\n", encoding="utf-8")
    collector = NowcoderCollector(config)

    class Response:
        text = FIXTURE.read_text(encoding="utf-8")

    async def request(_url: str, **kwargs):
        assert kwargs["params"]["key"] == "fixture-secret"
        return Response()

    monkeypatch.setenv("RSSHUB_KEY", "fixture-secret")
    monkeypatch.setattr(collector, "request", request)
    items = await collector.fetch()
    assert [item.url.rsplit("/", 1)[-1] for item in items] == ["1001", "1002"]
    assert [item.rank for item in items] == [1, 2]


@pytest.mark.asyncio
async def test_nowcoder_degrades_when_rsshub_fails(monkeypatch, tmp_path) -> None:
    config = tmp_path / "nowcoder.yaml"
    config.write_text("routes: [/nowcoder/hots/2]\n", encoding="utf-8")
    collector = NowcoderCollector(config)

    async def request(_url: str, **_kwargs):
        raise RuntimeError("public RSSHub unavailable")

    monkeypatch.setattr(collector, "request", request)
    with pytest.raises(SourceUnavailable, match="RSSHub failed") as captured:
        await collector.fetch()
    assert captured.value.status == "degraded"
