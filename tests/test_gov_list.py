from datetime import date

from app.collectors.gov_list import GovListCollector


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
