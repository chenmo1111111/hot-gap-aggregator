from __future__ import annotations

import sqlite3
from datetime import date

import httpx

from app.pipeline.gongkao_enrich import (
    DeepSeekExtractor,
    EnrichmentCache,
    _client_redirect_url,
    _likely_recruit_portal,
    calculate_signup_status,
    enrich_payload,
    extract_official_apply_url,
    extract_recruit_count,
    is_my_school_eligible,
    load_apply_url_overrides,
    load_apply_instructions,
    parse_extraction_json,
    strip_article_html,
    strip_webpage_html,
    url_cache_key,
)


def test_signup_status_covers_urgent_start_and_expiry() -> None:
    today = date(2026, 9, 7)
    assert calculate_signup_status("2026-09-08", "2026-09-20", today=today) == ("未开始", 13)
    assert calculate_signup_status("2026-09-01", "2026-09-12", today=today) == ("剩5天", 5)
    assert calculate_signup_status("2026-09-01", "2026-09-20", today=today) == ("报名中", 13)
    assert calculate_signup_status("2026-09-01", "2026-09-06", today=today) == ("已截止", 0)


def test_article_html_and_llm_json_are_normalized() -> None:
    html = "<style>ignore</style><div id='content'><p>笔试：行测和申论</p><script>x</script></div>"
    assert strip_article_html(html) == "笔试：行测和申论"
    result = parse_extraction_json('```json\n{"xian_huji":"true","xuandiao_school_scope":"双一流建设高校","zhaopin_renshu":"53人","record_kind":"公考"}\n```')
    assert "bishi_kemu" not in result
    assert result["xuandiao_school_scope"] == "双一流建设高校"
    assert result["xian_huji"] is True
    assert result["xian_zhuanye"] is False
    assert result["zhaopin_renshu"] == "53人"
    assert result["record_kind"] == "公考"

    government_html = "<header>菜单</header><div class='TRS_Editor'><p>招录公告正文，要求本科及以上学历并参加公共基础知识笔试。</p></div><footer>版权</footer>"
    assert strip_webpage_html(government_html).startswith("招录公告正文")
    assert url_cache_key("https://gov.example/a?id=1#top") == url_cache_key(
        "https://gov.example/a?id=1#other"
    )
    assert url_cache_key("http://127.0.0.1/private") == ""


def test_official_apply_url_extraction_rejects_fenbi_calendar() -> None:
    html = """
    <a href="https://www.fenbi.com/page/kaoshidetail/123">粉笔考试日历</a>
    <p>请登录 cms.hotjob.cn 查看职位并投递简历。</p>
    """
    assert extract_official_apply_url(html) == "https://cms.hotjob.cn"
    assert extract_official_apply_url(
        '<a href="https://www.fenbi.com/page/kaoshidetail/123">立即报名</a>'
    ) == ""
    assert extract_official_apply_url(
        "请前往https://career.cmbchina.com选择校园招聘并投递简历"
    ) == "https://career.cmbchina.com"
    assert _client_redirect_url(
        '<script>window.location.replace("https://career.example/apply")</script>',
        "https://search.example/result",
    ) == "https://career.example/apply"
    assert _likely_recruit_portal("https://img01.51jobcdn.com/logo.ico") is False
    assert _likely_recruit_portal("https://cnnc.zhiye.com/xiaoyuan") is True


def test_official_apply_url_extraction_prefers_labeled_application_link() -> None:
    html = """
    <a href="https://company.example/about">公司介绍</a>
    <a href="https://career.company.example/campus/apply">立即投递</a>
    """
    assert extract_official_apply_url(html) == "https://career.company.example/campus/apply"


def test_verified_apply_url_override_wins_without_network(tmp_path) -> None:
    config_path = tmp_path / "overrides.yaml"
    config_path.write_text(
        'overrides:\n  "123": "https://career.company.example/campus"\n'
        '  "bad": "https://www.fenbi.com/page/kaoshidetail/9"\n'
        'instructions:\n  "456": "发送简历至 hr@example.com"\n',
        encoding="utf-8",
    )
    overrides = load_apply_url_overrides(config_path)
    assert overrides == {"123": "https://career.company.example/campus"}
    assert load_apply_instructions(config_path) == {
        "456": "发送简历至 hr@example.com"
    }

    payload = {"items": [{
        "title": "某企业2027届校园招聘",
        "url": "https://www.fenbi.com/page/kaoshidetail/123",
        "extra": {"id": 123, "sub": "announcement", "record_kind": "秋招"},
    }]}
    cache = EnrichmentCache(tmp_path / "cache.db")
    try:
        enriched, stats = enrich_payload(
            payload,
            cache=cache,
            school_config={},
            extractor=None,
            apply_url_overrides=overrides,
            today=date(2026, 9, 9),
        )
    finally:
        cache.close()
    assert enriched["items"][0]["extra"]["apply_url"] == (
        "https://career.company.example/campus"
    )
    assert stats["apply_url_searched"] == 0
    assert stats["apply_url_overridden"] == 1


def test_non_url_application_instruction_is_added_to_extra(tmp_path) -> None:
    payload = {"items": [{
        "title": "某集团2027届校园招聘",
        "extra": {"id": 456, "record_kind": "秋招"},
    }]}
    cache = EnrichmentCache(tmp_path / "cache.db")
    try:
        enriched, stats = enrich_payload(
            payload,
            cache=cache,
            school_config={},
            extractor=None,
            apply_instructions={"456": "发送简历至 hr@example.com"},
            today=date(2026, 9, 9),
        )
    finally:
        cache.close()
    assert enriched["items"][0]["extra"]["apply_instruction"] == (
        "发送简历至 hr@example.com"
    )
    assert stats["apply_instruction_added"] == 1


def test_web_search_follows_information_result_to_official_portal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cn.bing.com":
            return httpx.Response(200, content="""<?xml version="1.0" encoding="utf-8"?>
            <rss><channel><item><title>招商证券2027校园招聘</title>
            <link>https://career.school.edu.cn/notice/1</link>
            <description>招商证券校园招聘公告</description></item></channel></rss>""".encode())
        return httpx.Response(
            200,
            text='<article>招商证券2027校园招聘，请登录 cms.hotjob.cn 投递简历。</article>',
            headers={"content-type": "text/html; charset=utf-8"},
        )

    extractor = DeepSeekExtractor("unused")
    extractor.client.close()
    extractor.client = httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    try:
        assert extractor.search_official_apply_url(
            "招商证券", "招商证券2027年校园招聘现已全面启动"
        ) == "https://cms.hotjob.cn"
    finally:
        extractor.close()


def test_selection_school_uses_explicit_list_before_fallback() -> None:
    config = {
        "schools_by_province": {"辽宁": ["东北林业大学"], "天津": ["天津大学"], "山东": []},
        "default_rules": ["双一流", "211"],
        "my_school": {"name": "东北林业大学", "aliases": ["东北林大"], "tags": ["211", "双一流"]},
    }
    base = {"title": "2026年定向选调公告", "extra": {"exam_type": "选调生", "province": "辽宁"}}
    assert is_my_school_eligible(base, config) is True
    assert is_my_school_eligible({**base, "extra": {**base["extra"], "province": "天津"}}, config) is False
    fallback = {"title": "面向双一流高校定向选调", "extra": {"exam_type": "选调生", "province": "山东"}}
    assert is_my_school_eligible(fallback, config) is True


class _Extractor:
    def __init__(self) -> None:
        self.fetches = 0
        self.extracts = 0

    def fetch_article(self, _article_id: str) -> str:
        self.fetches += 1
        return "这是一段长度足够的公告正文，明确写明本科及以上学历。"

    def fetch_url(self, _url: str) -> str:
        self.fetches += 1
        return "这是一段来自政府网站且长度足够的公告正文，明确写明本科及以上学历。"

    def extract(self, _text: str, *, include_xuandiao_scope: bool = False):
        self.extracts += 1
        return parse_extraction_json({
            "xueli": "本科及以上",
            "xian_zhuanye": False,
            "zhaopin_renshu": "25人",
            "record_kind": "公考",
            "xuandiao_school_scope": "面向全国双一流建设高校" if include_xuandiao_scope else "",
        })


def test_enrichment_is_incremental_and_cache_is_reused(tmp_path) -> None:
    payload = {"items": [{
        "title": "公告A", "url": "https://example.test/a",
        "extra": {"id": 123, "sub": "announcement", "exam_type": "事业单位",
                  "startSignUpTime": "2026-09-01", "endSignUpTime": "2026-09-10"},
    }]}
    config = {"schools_by_province": {}, "default_rules": [], "my_school": {}}
    cache = EnrichmentCache(tmp_path / "cache.db")
    extractor = _Extractor()
    try:
        first, first_stats = enrich_payload(
            payload, cache=cache, school_config=config, extractor=extractor,
            today=date(2026, 9, 7),
        )
        second, second_stats = enrich_payload(
            payload, cache=cache, school_config=config, extractor=extractor,
            today=date(2026, 9, 8),
        )
    finally:
        cache.close()
    assert first["items"][0]["extra"]["xueli"] == "本科及以上"
    assert first_stats["extracted"] == 1
    assert second_stats["cached"] == 1
    assert second["items"][0]["extra"]["signup_status"] == "剩2天"
    assert first["items"][0]["extra"]["first_seen"] == "2026-09-01"
    assert second["items"][0]["extra"]["first_seen"] == "2026-09-01"
    assert first["items"][0]["extra"]["recruit_count"] == "25人"
    assert first["items"][0]["extra"]["record_kind"] == "公考"
    assert (extractor.fetches, extractor.extracts) == (1, 1)


def test_watcher_url_enrichment_uses_hashed_url_cache(tmp_path) -> None:
    payload = {"items": [{
        "title": "辽宁定向选调公告",
        "url": "https://gov.example/xuandiao?id=9#notice",
        "extra": {"id": "watcher:9", "sub": "announcement", "subsource": "xuandiao",
                  "exam_type": "选调生", "province": "辽宁"},
    }, {
        "title": "手工表格公告",
        "url": "https://sheet.example/notice",
        "extra": {"id": "gongkao-sheet:1", "sub": "announcement"},
    }]}
    config = {"schools_by_province": {}, "default_rules": [], "my_school": {}}
    cache = EnrichmentCache(tmp_path / "cache.db")
    extractor = _Extractor()
    try:
        first, first_stats = enrich_payload(
            payload, cache=cache, school_config=config, extractor=extractor,
            today=date(2026, 9, 8),
        )
        second, second_stats = enrich_payload(
            payload, cache=cache, school_config=config, extractor=extractor,
            today=date(2026, 9, 8),
        )
    finally:
        cache.close()

    assert first_stats["url_extracted"] == 1
    assert first_stats["skipped"] == 1
    assert second_stats["cached"] == 1
    assert first["items"][0]["extra"]["xueli"] == "本科及以上"
    assert first["items"][0]["extra"]["xuandiao_school_scope"] == "面向全国双一流建设高校"
    assert second["items"][1]["extra"]["enrichment_status"] == "未提取"
    assert (extractor.fetches, extractor.extracts) == (1, 1)


def test_individual_page_fetch_failures_do_not_disable_later_rows(tmp_path) -> None:
    class FlakyExtractor(_Extractor):
        def fetch_article(self, article_id: str) -> str:
            self.fetches += 1
            if article_id in {"1", "2", "3"}:
                raise RuntimeError("site blocked this page")
            return "第四条公告网页正文正常，内容长度足够用于公告要点提取。"

    payload = {"items": [
        {"title": f"公告{identifier}", "url": f"https://fenbi.example/{identifier}",
         "extra": {"id": identifier, "sub": "announcement"}}
        for identifier in (1, 2, 3, 4)
    ]}
    config = {"schools_by_province": {}, "default_rules": [], "my_school": {}}
    cache = EnrichmentCache(tmp_path / "cache.db")
    extractor = FlakyExtractor()
    try:
        enriched, stats = enrich_payload(
            payload, cache=cache, school_config=config, extractor=extractor,
            today=date(2026, 9, 8),
        )
    finally:
        cache.close()

    assert stats["fetch_failed"] == 3
    assert stats["fenbi_extracted"] == 1
    assert enriched["items"][-1]["extra"]["enrichment_status"] == "ok"
    assert extractor.fetches == 4


def test_first_seen_uses_earliest_source_date_and_backfills_legacy_row_once(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE gongkao_first_seen(record_key TEXT PRIMARY KEY, first_seen TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO gongkao_first_seen(record_key,first_seen) VALUES(?,?)",
        ("announcement:99", "2026-09-08"),
    )
    connection.commit()
    connection.close()

    cache = EnrichmentCache(path)
    try:
        assert cache.get_or_create_first_seen(
            "announcement:99", date(2026, 9, 8), source_date=date(2026, 8, 31)
        ) == "2026-08-31"
        assert cache.get_or_create_first_seen(
            "announcement:99", date(2026, 9, 9), source_date=date(2026, 8, 20)
        ) == "2026-08-31"
    finally:
        cache.close()


def test_recruit_count_regex_avoids_cohort_year_and_keeps_unit() -> None:
    assert extract_recruit_count({"title": "2027届校园招聘，计划招聘53人"}) == "53人"
    assert extract_recruit_count({"summary": "本次招录200名工作人员"}) == "200名"
    assert extract_recruit_count({"title": "2027届校园招聘"}) == ""
