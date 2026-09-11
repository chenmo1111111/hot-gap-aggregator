import json
from datetime import date
from pathlib import Path

import yaml

from app.collectors.official_jobs import OfficialJobsCollector


ROOT = Path(__file__).resolve().parents[1]


def _sources() -> dict[str, dict]:
    payload = yaml.safe_load(
        (ROOT / "config" / "official_job_sources.yaml").read_text(encoding="utf-8")
    )
    return {source["name"]: source for source in payload["sources"]}


def test_ncss_real_api_fixture_parses_three_dated_jobs() -> None:
    source = _sources()["国家大学生就业服务平台"]
    payload = json.loads(
        (ROOT / "tests" / "fixtures" / "jobs" / source["fixture"]).read_text(
            encoding="utf-8"
        )
    )
    items = OfficialJobsCollector.parse_api(
        payload, source, keyword="算法", today=date(2026, 9, 11)
    )

    assert len(items) == 3
    assert all(item.published_at == "2026-09-11" for item in items)
    assert all(item.extra["subsource"] == "ncss" for item in items)
    assert all("算法" in item.extra["keywords_hit"] for item in items)
    assert items[0].url == (
        "https://cg.ncss.cn/student/jobs/8Y5ehaqTxLhBdyq6NzDwjd/detail.html"
    )


def test_chsi_real_html_fixture_parses_three_dated_announcements() -> None:
    source = _sources()["教育部人才服务网"]
    document = (
        ROOT / "tests" / "fixtures" / "jobs" / source["fixture"]
    ).read_text(encoding="utf-8")
    items = OfficialJobsCollector.parse_html(document, source, today=date(2026, 9, 11))

    assert len(items) == 3
    assert all(item.published_at and len(item.published_at) == 10 for item in items)
    assert all(item.extra["subsource"] == "chsi_talent" for item in items)
    assert items[0].extra["company"] == "浙江大学"
    assert items[0].url.startswith("https://jybzp.chsi.com.cn/home/bul/announcement/")


def test_official_job_sources_use_direct_http_engines_and_real_list_urls() -> None:
    sources = _sources()
    ncss = sources["国家大学生就业服务平台"]
    chsi = sources["教育部人才服务网"]

    assert ncss["engine"] == "api"
    assert ncss["list_url"] == "https://cg.ncss.cn/student/jobs/jobslist/ajax/"
    assert ncss["fields"]["date"] == "publishDate"
    assert chsi["engine"] == "html"
    assert set(chsi["list_urls"]) == {
        "https://jybzp.chsi.com.cn/home/bul/announcement?sourcetyp=school",
        "https://jybzp.chsi.com.cn/home/bul/announcement?sourcetyp=drus",
    }
