import asyncio
from datetime import date
import json
from pathlib import Path
import ssl

import pytest
import yaml

from app.collectors.gongkao import NOTICE_WORDS as GONGKAO_NOTICE_WORDS
from app.collectors.gov_list import NOTICE_WORDS as GOV_NOTICE_WORDS
from app.collectors.gov_list import GovListCollector, _tls_verify


ROOT = Path(__file__).resolve().parents[1]


def test_parse_html_extracts_recent_official_rows_and_skips_old_rows() -> None:
    html = """
    <ul>
      <li><a href="/notice/new.html">吉林省直事业单位公开招聘工作人员公告</a><span>2026-09-09</span></li>
      <li><a href="/notice/old.html">吉林省事业单位公开招聘旧公告</a><span>2026-06-01</span></li>
    </ul>
    """
    source = {
        "name": "吉林省人社厅", "province": "吉林省", "category": "事业单位",
        "list_url": "https://hrss.jl.gov.cn/rsrc/sydwrsgl/gkzp/", "engine": "html",
    }
    items = GovListCollector.parse_html(html, source, today=date(2026, 9, 10))
    assert len(items) == 1
    assert items[0].url == "https://hrss.jl.gov.cn/notice/new.html"
    assert items[0].published_at == "2026-09-09"
    assert items[0].extra["province"] == "吉林"
    assert items[0].extra["government_source"] is True
    assert items[0].extra["source_site"] == "gov"


def test_tls_verify_builds_legacy_insecure_context_only_when_configured() -> None:
    assert _tls_verify({}) is True
    assert _tls_verify({"verify_tls": False}) is False
    context = _tls_verify({"verify_tls": False, "legacy_tls": True})
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False


def test_parse_html_honors_yaml_selectors() -> None:
    html = """
    <div class="entry"><a class="name" href="/a">军队文职人员公开招考公告</a><time>2026/09/08</time></div>
    """
    source = {
        "name": "军队人才网", "province": "全国", "category": "军队文职",
        "list_url": "https://81rc.81.cn/wzry/gzdt/", "item_selector": ".entry",
        "title_selector": ".name", "link_selector": ".name", "date_selector": "time",
    }
    items = GovListCollector.parse_html(html, source, today=date(2026, 9, 10))
    assert len(items) == 1
    assert items[0].extra["exam_type"] == "军队文职"


def test_notice_words_include_real_recruitment_variants_in_both_collectors() -> None:
    expected = {
        "公告", "招录", "招考", "招聘", "选调", "三支一扶", "文职", "军官", "警官",
        "引才", "引进人才", "招募", "选聘", "公开选聘", "定向招聘",
    }
    assert set(GOV_NOTICE_WORDS) == expected
    assert set(GONGKAO_NOTICE_WORDS) == expected

    html = "<ul>" + "".join(
        f'<li><a href="/{index}">某事业单位2026年{label}</a><span>2026-09-10</span></li>'
        for index, label in enumerate(("引才方案", "引进人才公告", "公开选聘工作人员", "定向招聘工作人员"))
    ) + "</ul>"
    source = {
        "name": "关键词测试", "province": "贵州", "category": "事业单位",
        "list_url": "https://example.gov.cn/list/", "item_selector": "li",
        "title_selector": "a", "link_selector": "a", "date_selector": "span",
    }
    assert len(GovListCollector.parse_html(html, source, today=date(2026, 9, 11))) == 4


def test_parse_html_can_read_compact_date_from_official_url() -> None:
    html = """
    <ul class="rows"><li><a href="./202609/t20260911_7212292.htm"
      title="福建省某事业单位2026年公开招聘高层次人才公告">招聘公告</a></li></ul>
    """
    source = {
        "name": "福建省人社厅", "province": "福建", "category": "事业单位",
        "list_url": "https://rst.fujian.gov.cn/topic/", "item_selector": "li",
        "title_selector": "a", "link_selector": "a", "date_selector": "span",
        "date_from_url": True,
    }
    items = GovListCollector.parse_html(html, source, today=date(2026, 9, 11))
    assert len(items) == 1
    assert items[0].published_at == "2026-09-11"


@pytest.mark.asyncio
async def test_two_level_source_discovers_latest_matching_topic(monkeypatch) -> None:
    collector = GovListCollector(ROOT / "config" / "gongkao_gov_sources.yaml")
    index = (ROOT / "tests" / "fixtures" / "gov" / "shaanxi_index.html").read_text(
        encoding="utf-8"
    )

    async def fake_html(source, url):
        return index

    monkeypatch.setattr(collector, "_html", fake_html)
    urls = await collector._resolved_source_urls({
        "name": "陕西省政府", "list_url": "http://www.shaanxi.gov.cn/xw/ztzl/zxzt/zkzl/",
        "discover_selector": 'a[href*="sydwzp"]',
        "discover_title_include": "事业单位.*公开招聘", "discover_limit": 1,
    })
    assert urls == [
        "http://www.shaanxi.gov.cn/xw/ztzl/zxzt/zkzl/2026/26sydwzp/"
    ]


def test_parse_html_can_make_stable_landing_links_for_click_only_lists() -> None:
    html = """
    <div class="item"><span class="title">南方电网2026年校园招聘公告</span><div class="con"></div><div class="date">2026-09-08 10:00:00</div></div>
    <div class="item"><span class="title">南方电网2026年社会招聘公告</span><div class="con"></div><div class="date">2026-09-07 10:00:00</div></div>
    """
    source = {
        "name": "南方电网", "province": "全国", "category": "央企事业编",
        "list_url": "https://zhaopin.csg.cn/#/notice-list", "item_selector": ".item",
        "title_selector": ".title", "link_selector": ".con", "date_selector": ".date",
        "date_format": "%Y-%m-%d %H:%M:%S", "synthetic_link": True,
    }
    items = GovListCollector.parse_html(html, source, today=date(2026, 9, 10))
    assert len(items) == 2
    assert items[0].url.startswith("https://zhaopin.csg.cn/#notice-")
    assert items[0].url != items[1].url


def test_parse_html_can_make_unique_links_for_shared_landing_page() -> None:
    html = """
    <div class="row"><a href="/announcements">国家电网北京公司2026年招聘公告</a><time>2026-09-08</time></div>
    <div class="row"><a href="/announcements">国家电网天津公司2026年招聘公告</a><time>2026-09-07</time></div>
    """
    source = {
        "name": "国家电网", "province": "全国", "category": "央企事业编",
        "list_url": "https://zhaopin.sgcc.com.cn/home.html", "item_selector": ".row",
        "title_selector": "a", "link_selector": "a", "date_selector": "time",
        "synthetic_link": True, "synthetic_link_always": True,
    }
    items = GovListCollector.parse_html(html, source, today=date(2026, 9, 10))
    assert len(items) == 2
    assert items[0].url != items[1].url


def test_four_javascript_portals_require_rendered_content() -> None:
    rows = (yaml.safe_load(
        (ROOT / "config" / "gongkao_gov_sources.yaml").read_text(encoding="utf-8")
    ) or {})["sources"]
    sources = {
        source["name"]: source for source in rows
        if source["name"] in {
            "人社部-中央事业单位公开招聘", "国家电网招聘",
            "南方电网招聘", "中国铁路人才招聘网",
        }
    }
    assert len(sources) == 4
    assert "事业单位公开招聘服务平台" not in {
        source["name"] for source in rows
    }
    public_jobs = next(source for source in rows if source["name"] == "中国公共招聘网")
    assert public_jobs["list_url"] == (
        "http://job.mohrss.gov.cn/cjobs/institution/listInstitution"
    )
    assert public_jobs["engine"] == "html"
    assert public_jobs["item_selector"] == "tr"
    assert public_jobs["date_selector"] == "td:nth-child(3)"
    assert all(source["engine"] == "playwright" for source in sources.values())
    assert all(source["playwright_wait_until"] == "networkidle" for source in sources.values())
    assert all(source.get("playwright_ready_selector") for source in sources.values())
    assert sources["南方电网招聘"]["playwright_click_text"] == "招聘公告"
    assert sources["中国铁路人才招聘网"]["playwright_page_size"] == "50"

    challenge = "<html><script>window.$_ts='challenge'</script><body>招聘公告 2026-09-10</body></html>"
    for source in sources.values():
        assert GovListCollector.parse_html(challenge, source, today=date(2026, 9, 10)) == []


@pytest.mark.asyncio
async def test_playwright_sources_are_strictly_serial(monkeypatch) -> None:
    collector = GovListCollector(ROOT / "config" / "gongkao_gov_sources.yaml")
    collector.request_interval = 0
    active = 0
    max_active = 0

    async def fake_html(source, url):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.01)
            return (
                '<li><a href="/notice">事业单位公开招聘工作人员公告</a>'
                '<span>2026-09-10</span></li>'
            )
        finally:
            active -= 1

    monkeypatch.setattr(collector, "_html", fake_html)
    sources = [
        {
            "name": f"playwright-{index}", "engine": "playwright",
            "list_url": f"https://example.com/{index}/", "province": "全国",
            "category": "事业单位", "item_selector": "li",
            "title_selector": "a", "link_selector": "a", "date_selector": "span",
        }
        for index in range(5)
    ]

    results = await asyncio.gather(*(collector._fetch_one(source) for source in sources))

    assert max_active == 1
    assert all(len(items) == 1 and error is None for _, items, error in results)


@pytest.mark.asyncio
async def test_html_sources_keep_the_existing_parallelism(monkeypatch) -> None:
    collector = GovListCollector(ROOT / "config" / "gongkao_gov_sources.yaml")
    active = 0
    max_active = 0

    async def fake_html(source, url):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.01)
            return (
                '<li><a href="/notice">事业单位公开招聘工作人员公告</a>'
                '<span>2026-09-10</span></li>'
            )
        finally:
            active -= 1

    monkeypatch.setattr(collector, "_html", fake_html)
    sources = [
        {
            "name": f"html-{index}", "engine": "html",
            "list_url": f"https://example.com/{index}/", "province": "全国",
            "category": "事业单位", "item_selector": "li",
            "title_selector": "a", "link_selector": "a", "date_selector": "span",
        }
        for index in range(5)
    ]

    results = await asyncio.gather(*(collector._fetch_one(source) for source in sources))

    assert max_active == 5
    assert all(len(items) == 1 and error is None for _, items, error in results)


@pytest.mark.asyncio
async def test_playwright_timeout_releases_slot_and_next_source_completes(monkeypatch) -> None:
    collector = GovListCollector(ROOT / "config" / "gongkao_gov_sources.yaml")
    assert collector.playwright_source_timeout == 60.0
    collector.playwright_source_timeout = 0.02
    cancelled = asyncio.Event()

    async def fake_html(source, url):
        if source["name"] == "hung":
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return (
            '<li><a href="/notice">事业单位公开招聘工作人员公告</a>'
            '<span>2026-09-10</span></li>'
        )

    monkeypatch.setattr(collector, "_html", fake_html)
    common = {
        "engine": "playwright", "province": "全国", "category": "事业单位",
        "item_selector": "li", "title_selector": "a", "link_selector": "a",
        "date_selector": "span",
    }
    hung = {**common, "name": "hung", "list_url": "https://example.com/hung/"}
    healthy = {**common, "name": "healthy", "list_url": "https://example.com/healthy/"}
    monkeypatch.setattr(collector, "load_sources", lambda: [hung, healthy])
    monkeypatch.setattr(collector, "load_seed_items", lambda: [])

    items = await collector.fetch()

    assert cancelled.is_set()
    assert collector.stats["hung"] == {
        "count": 0, "last_7_days": 0, "last_30_days": 0,
        "error": "playwright source timed out after 0.02s",
    }
    assert collector.stats["healthy"]["count"] == 1
    assert collector.stats["healthy"]["error"] == ""
    assert len(items) == 1


def test_seed_items_keep_official_links_for_blocked_portals(tmp_path) -> None:
    config = tmp_path / "gov.yaml"
    config.write_text("""
seed_items:
  - title: 湖南省2026年省直事业单位第四次公开招聘工作人员公告
    published_at: 2026-09-08
    url: https://rst.hunan.gov.cn/notice/1.html
    source: 湖南省人社厅
    province: 湖南省
    category: 事业单位
sources: []
""", encoding="utf-8")
    items = GovListCollector(config).load_seed_items()
    assert len(items) == 1
    assert items[0].url == "https://rst.hunan.gov.cn/notice/1.html"
    assert items[0].extra["province"] == "湖南"


def test_seed_items_can_be_disabled_for_acceptance(monkeypatch) -> None:
    monkeypatch.setenv("GONGKAO_DISABLE_SEEDS", "true")
    collector = GovListCollector(ROOT / "config" / "gongkao_gov_sources.yaml")
    assert collector.load_seed_items() == []


def test_server_refresh_always_disables_seed_items() -> None:
    script = (ROOT / "deploy" / "server" / "hot-gap-feishu-refresh").read_text(encoding="utf-8")
    assert "export GONGKAO_DISABLE_SEEDS=true" in script


def test_server_refresh_cleans_only_stale_marked_playwright_processes() -> None:
    script = (ROOT / "deploy" / "server" / "hot-gap-feishu-refresh").read_text(encoding="utf-8")
    marker = GovListCollector.playwright_process_marker
    assert marker in script
    assert "HOT_GAP_PLAYWRIGHT_MAX_AGE_SECONDS:-600" in script
    assert 'command_line" == *"chromium"*"--headless"*' in script
    assert (
        "\ncleanup_stale_hot_gap_playwright\n"
        "refresh_stage=collect_gongkao\n"
        ".venv/bin/python -m app.collect_gongkao\n"
    ) in script


def test_server_refresh_alerts_on_any_failed_stage() -> None:
    script = (ROOT / "deploy" / "server" / "hot-gap-feishu-refresh").read_text(
        encoding="utf-8"
    )
    assert "set -Eeuo pipefail" in script
    assert "trap 'notify_refresh_failure" in script
    assert "refresh-alert-b64" in script
    assert '--record "$persistent_dir/refresh-failures.jsonl"' in script
    assert "refresh_stage=sync_feishu\n.venv/bin/python -m app.sync_feishu" in script
    assert "refresh_stage=sync_feishu_public" in script
    assert "/usr/bin/flock -E 200 -w 300" in script


def test_html_source_rejects_empty_selectors(tmp_path) -> None:
    config = tmp_path / "invalid.yaml"
    config.write_text("sources:\n  - name: invalid\n    list_url: https://example.com\n    engine: html\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty selectors"):
        GovListCollector(config).load_sources()


def test_disabled_sources_are_not_loaded(tmp_path) -> None:
    config = tmp_path / "sources.yaml"
    config.write_text("""
sources:
  - name: disabled
    enabled: false
    list_url: https://example.com/disabled
    engine: html
  - name: active
    list_url: https://example.com/active
    engine: html
    item_selector: li
    title_selector: a
    link_selector: a
    date_selector: span
""", encoding="utf-8")
    assert [source["name"] for source in GovListCollector(config).load_sources()] == ["active"]


def test_config_covers_all_provincial_regions_and_replaces_obsolete_urls() -> None:
    text = (ROOT / "config" / "gongkao_gov_sources.yaml").read_text(encoding="utf-8")
    rows = (yaml.safe_load(text) or {})["sources"]
    covered = {str(source.get("province")) for source in rows}
    provinces = {
        "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海", "江苏",
        "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西",
        "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
    }
    assert provinces <= covered
    assert "新疆生产建设兵团" in covered
    for obsolete in (
        "hl.lss.gov.cn", "col/col45194", "col/col47831", "202.61.89.231",
        "c100481/flm_list", "sydwgkzp2024", "def/def/index_1_1459",
    ):
        assert obsolete not in text


def test_corrected_province_urls_and_site_specific_parsers_are_configured() -> None:
    rows = (yaml.safe_load(
        (ROOT / "config" / "gongkao_gov_sources.yaml").read_text(encoding="utf-8")
    ) or {})["sources"]
    sources = {source["name"]: source for source in rows}
    expected_urls = {
        "福建省人社厅-招聘入口": "https://rst.fujian.gov.cn/zw/ztzl/zxzt/sydwrczp/",
        "湖北省人社厅-招聘公告": "https://rst.hubei.gov.cn/bmdt/ztzl/ywzl/hbsszsydwgkzp/",
        "西藏自治区人社厅-招聘公告": "http://hrss.xizang.gov.cn/zpxx/",
        "广东省人社厅-事业单位招聘公告": "https://hrss.gd.gov.cn/zwgk/sydwzp/",
        "广西人事考试网": "https://www.gxpta.com.cn/ksxm/sydwzpks/",
    }
    for name, url in expected_urls.items():
        assert sources[name]["list_url"] == url
    assert "/sydwzp/zpgg/" in sources["广东省人社厅-事业单位招聘公告"]["item_selector"]
    assert sources["安徽省人社厅-事业单位公开招聘专栏"]["date_selector"] == "span.right.date"
    shaanxi = sources["陕西省政府-招考招录"]
    assert shaanxi["discover_selector"] == 'a[href*="sydwzp"]'
    assert shaanxi["discover_limit"] == 1


@pytest.mark.parametrize(
    "source",
    (yaml.safe_load((ROOT / "config" / "gongkao_gov_sources.yaml").read_text(encoding="utf-8")) or {})["sources"],
    ids=lambda source: source["name"],
)
def test_each_real_gov_fixture_parses_three_dated_rows(source) -> None:
    fixture_name = source.get("fixture")
    if not fixture_name:
        pytest.xfail(str(source.get("verification_note") or "real fixture unavailable"))
    fixture = ROOT / "tests" / "fixtures" / "gov" / fixture_name
    if not fixture.exists():
        pytest.xfail(str(source.get("verification_note") or "real fixture unavailable"))
    fixture_source = dict(source)
    fixture_source["recent_days"] = max(730, int(source.get("recent_days") or 45))
    if str(fixture.suffix).casefold() == ".json":
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        items = GovListCollector.parse_api(payload, fixture_source, today=date(2026, 9, 10))
    else:
        html = fixture.read_text(encoding="utf-8")
        items = GovListCollector.parse_html(html, fixture_source, today=date(2026, 9, 10))
    assert len(items) >= 3
    assert all(item.published_at and len(item.published_at) == 10 for item in items)
