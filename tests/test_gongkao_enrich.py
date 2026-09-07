from __future__ import annotations

from datetime import date

from app.pipeline.gongkao_enrich import (
    EnrichmentCache,
    calculate_signup_status,
    enrich_payload,
    is_my_school_eligible,
    parse_extraction_json,
    strip_article_html,
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
    result = parse_extraction_json('```json\n{"bishi_kemu":"行测+申论","xian_huji":"true"}\n```')
    assert result["bishi_kemu"] == "行测+申论"
    assert result["xian_huji"] is True
    assert result["xian_zhuanye"] is False


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

    def extract(self, _text: str):
        self.extracts += 1
        return parse_extraction_json({"xueli": "本科及以上", "xian_zhuanye": False})


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
            today=date(2026, 9, 7),
        )
    finally:
        cache.close()
    assert first["items"][0]["extra"]["xueli"] == "本科及以上"
    assert first_stats["extracted"] == 1
    assert second_stats["cached"] == 1
    assert second["items"][0]["extra"]["signup_status"] == "剩3天"
    assert (extractor.fetches, extractor.extracts) == (1, 1)
