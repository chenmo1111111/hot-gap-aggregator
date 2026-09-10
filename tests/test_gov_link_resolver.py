from pathlib import Path

import httpx
import pytest

from app.models import Item
from app.pipeline.gov_link_resolver import (
    GovLinkResolver, OfficialDomainPolicy, ResolverCache,
    _title_similarity, parse_bing_results,
)


def test_bing_parser_and_official_domain_policy() -> None:
    html = '<li class="b_algo"><h2><a href="http://www.impta.com.cn/a">2027年度内蒙古自治区事业单位公开招聘工作人员公告</a></h2></li>'
    assert parse_bing_results(html) == [("2027年度内蒙古自治区事业单位公开招聘工作人员公告", "http://www.impta.com.cn/a")]
    policy = OfficialDomainPolicy({"suffixes": [".gov.cn"], "known_hosts": ["www.impta.com.cn"]})
    assert policy.allows("http://www.impta.com.cn/a")
    assert not policy.allows("https://www.fenbi.com/a")


def test_similarity_requires_close_title() -> None:
    assert _title_similarity("某省事业单位公开招聘公告（53人）", "某省事业单位公开招聘公告") >= 0.8
    assert _title_similarity("某省事业单位公开招聘公告", "完全无关的课程直播") < 0.8


@pytest.mark.asyncio
async def test_resolver_replaces_fenbi_url_and_caches_success(tmp_path: Path) -> None:
    calls: list[str] = []

    async def request(url: str, **kwargs) -> httpx.Response:
        calls.append(url)
        if "bing.com" in url:
            body = '<li class="b_algo"><h2><a href="http://www.impta.com.cn/original.asp">2027年度内蒙古自治区事业单位公开招聘工作人员公告</a></h2></li>'
        else:
            body = '<html><h1>2027年度内蒙古自治区事业单位公开招聘工作人员公告</h1></html>'
        return httpx.Response(200, text=body, request=httpx.Request("GET", url))

    config = tmp_path / "domains.yaml"
    config.write_text("minimum_title_similarity: 0.8\nsearch_interval_seconds: 0\nknown_hosts: [www.impta.com.cn]\n", encoding="utf-8")
    cache = tmp_path / "resolver.db"
    item = Item("gongkao", 1, "2027年度内蒙古自治区事业单位公开招聘工作人员公告（53人）", "", "https://www.fenbi.com/page/exam-timeline-detail/1", extra={"source_site": "fenbi"})
    resolver = GovLinkResolver(config, cache, request=request)
    await resolver.resolve_items([item])
    resolver.close()
    assert item.url == "http://www.impta.com.cn/original.asp"
    assert item.extra["source_site"] == "resolved"

    second = GovLinkResolver(config, cache, request=request)
    cached = await second._resolve("2027年度内蒙古自治区事业单位公开招聘工作人员公告（53人）")
    second.close()
    assert cached is not None
    assert calls.count("https://cn.bing.com/search") == 1


def test_negative_cache_is_retained(tmp_path: Path) -> None:
    cache = ResolverCache(tmp_path / "resolver.db", ttl_hours=24)
    cache.put("没有找到的公告", status="not_found")
    assert cache.get("没有找到的公告")["status"] == "not_found"
    cache.close()


@pytest.mark.asyncio
async def test_unresolved_fenbi_item_is_marked_for_review(tmp_path: Path) -> None:
    async def request(url: str, **kwargs) -> httpx.Response:
        return httpx.Response(200, text="<html></html>", request=httpx.Request("GET", url))

    config = tmp_path / "domains.yaml"
    config.write_text("search_interval_seconds: 0.001\nknown_hosts: [www.impta.com.cn]\n", encoding="utf-8")
    item = Item("gongkao", 1, "没有官网结果的事业单位招聘公告", "", "https://www.fenbi.com/a", extra={"source_site": "fenbi"})
    resolver = GovLinkResolver(config, tmp_path / "resolver.db", request=request)
    await resolver.resolve_items([item])
    resolver.close()
    assert item.extra["official_link_unresolved"] is True
