from __future__ import annotations

import json
from pathlib import Path

from app.pipeline.gongkao_dedup import deduplicate_gongkao_items, title_similarity


FIXTURE = Path(__file__).parent / "fixtures" / "gongkao_dedup_cases.json"


def _case(name: str) -> list[dict]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return payload[name]


def test_exact_cross_source_merge_preserves_backup_link_and_sheet_fields() -> None:
    source = _case("exact_shandong_business")

    rows, report = deduplicate_gongkao_items(source)

    assert len(rows) == 1
    assert report.exact_merged_count == 1
    row = rows[0]
    assert row["extra"]["unit"] == "山东商报社"
    assert row["extra"]["recruit_count"] == "2人"
    assert set(row["extra"]["backup_urls"]) == {
        source[0]["url"], source[1]["url"],
    } - {row["url"]}
    assert len(row["extra"]["merged_sources"]) == 2
    assert "title" in row["extra"]["merge_conflicts"]
    assert "合并保留差异" in row["extra"]["notes"]


def test_prefecture_normalization_and_evidence_merge_pingyuan_case() -> None:
    source = _case("fuzzy_pingyuan")

    assert title_similarity(source[0]["title"], source[1]["title"]) >= 0.9
    rows, report = deduplicate_gongkao_items(source)

    assert len(rows) == 1
    assert report.fuzzy_merged_count == 1
    assert report.merged_samples[-1]["evidence_matches"] == [
        "recruit_count", "signup_start", "signup_end",
    ]
    assert set(rows[0]["extra"]["backup_urls"]) == {
        source[0]["url"], source[1]["url"],
    } - {rows[0]["url"]}
    assert "title" in rows[0]["extra"]["merge_conflicts"]


def test_similar_titles_with_conflicting_evidence_are_only_marked_suspect() -> None:
    source = _case("suspect_conflict")

    similarity = title_similarity(source[0]["title"], source[1]["title"])
    assert 0.7 <= similarity < 0.9
    rows, report = deduplicate_gongkao_items(source)

    assert len(rows) == 2
    assert report.auto_merged_count == 0
    assert report.suspect_pair_count == 1
    assert all(row["extra"]["dup_suspect"] is True for row in rows)
    assert rows[0]["extra"]["possible_duplicate_of"] == "sheet:b"
    assert rows[1]["extra"]["possible_duplicate_of"] == "gov:a"
    assert all(
        row["extra"]["duplicate_evidence_conflicts"] == ["recruit_count"]
        for row in rows
    )


def test_same_boilerplate_different_institutes_do_not_merge_on_shared_deadline() -> None:
    rows, report = deduplicate_gongkao_items([
        {
            "title": "黑龙江省科学院微生物研究所2026年度公开招聘博士专业人员公告",
            "published_at": "2026-09-10",
            "url": "https://example.gov.cn/microbiology",
            "extra": {
                "id": "gov:microbiology", "province": "黑龙江",
                "endSignUpTime": "2026-09-30", "source_site": "gov",
            },
        },
        {
            "title": "2026年黑龙江省科学院智能制造研究所公开招聘博士专业人员5人公告",
            "published_at": "2026-09-11",
            "url": "https://example.test/manufacturing",
            "extra": {
                "id": "sheet:manufacturing", "province": "黑龙江省",
                "endSignUpTime": "2026-09-30", "upstream_source": "gongkao_sheet",
            },
        },
    ])

    assert len(rows) == 2
    assert report.auto_merged_count == 0
