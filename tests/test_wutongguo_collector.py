from pathlib import Path

from app.collectors.wutongguo import parse_wutongguo_html


FIXTURES = Path(__file__).parent / "fixtures"


def test_wutongguo_fixture_maps_job_and_filters_city() -> None:
    items = parse_wutongguo_html(
        (FIXTURES / "wutongguo_jobs.html").read_text(encoding="utf-8"),
        "生物信息", ["沈阳"], 20,
    )
    assert len(items) == 1
    assert items[0].title == "计算生物工程师"
    assert items[0].url == "https://www.wutongguo.com/job/7788.html"
    assert items[0].extra["company"] == "中国生物信息研究院"
    assert items[0].extra["deadline"] == "2026-11-01"
