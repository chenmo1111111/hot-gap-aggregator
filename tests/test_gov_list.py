from datetime import date
import json
from pathlib import Path

import pytest
import yaml

from app.collectors.gov_list import GovListCollector


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
