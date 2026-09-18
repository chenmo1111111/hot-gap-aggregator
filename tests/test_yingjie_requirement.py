from __future__ import annotations

import pytest

from app.pipeline.gongkao_enrich import parse_extraction_json
from app.pipeline.yingjie_requirement import (
    ALL_GRADUATES,
    CHECK_POSITIONS,
    ONLY_FRESH,
    PAST_ALLOWED,
    UNKNOWN,
    classify_yingjie_requirement,
)
from app.sync_feishu_public import GONGKAO_SCHEMA, map_public_gongkao


@pytest.mark.parametrize(("title", "expected"), [
    ("某市事业单位仅面向2027届高校毕业生招聘公告", ONLY_FRESH),
    ("某局仅限应届毕业生报考的招聘公告", ONLY_FRESH),
    ("黑龙江省2026年度定向选调应届优秀大学毕业生公告", ONLY_FRESH),
    ("某研究所招聘应届高校毕业生2人公告", ONLY_FRESH),
    ("某省事业单位应往届毕业生均可报考公告", ALL_GRADUATES),
    ("某区事业单位招聘公告（不限应届）", ALL_GRADUATES),
    ("某市事业单位部分岗位限应届毕业生招聘公告", CHECK_POSITIONS),
    ("某市事业单位部分岗位面向2027届高校毕业生招聘", CHECK_POSITIONS),
    ("某市事业单位2027年公开招聘公告", UNKNOWN),
])
def test_title_evidence_is_conservative(title: str, expected: str) -> None:
    assert classify_yingjie_requirement({"title": title}) == expected


def test_legacy_false_or_purchase_keyword_does_not_mean_everyone_can_apply() -> None:
    assert classify_yingjie_requirement({
        "title": "事业单位招聘公告",
        "extra": {"xian_yingjie": False, "fresh_graduate": True},
    }) == UNKNOWN
    assert classify_yingjie_requirement({
        "title": "事业单位招聘公告", "extra": {"xian_yingjie": True},
    }) == CHECK_POSITIONS


def test_explicit_notes_distinguish_past_graduate_eligibility_from_partial_limits() -> None:
    assert classify_yingjie_requirement({
        "title": "某地人才引进公告",
        "extra": {"bei_zhu": "本次引进应往届高校毕业生；试用期一年"},
    }) == PAST_ALLOWED
    assert classify_yingjie_requirement({
        "title": "某高校公开招聘公告",
        "extra": {"bei_zhu": "外省市户籍非应届毕业人员须持居住证"},
    }) == PAST_ALLOWED
    assert classify_yingjie_requirement({
        "title": "某区统一公开招聘公告",
        "extra": {"bei_zhu": "部分岗位可能限户籍、应届、党员，详见岗位表"},
    }) == CHECK_POSITIONS
    assert classify_yingjie_requirement({
        "title": "某学院公开招聘公告",
        "extra": {"bei_zhu": "在读非应届毕业生不得报考"},
    }) == UNKNOWN


def test_explicit_extraction_and_public_select_column() -> None:
    extracted = parse_extraction_json({"yingjie_requirement": "应往届均可"})
    assert extracted["yingjie_requirement"] == ALL_GRADUATES
    row = {
        "title": "普通招聘公告", "url": "https://example.com/notice",
        "extra": {"yingjie_requirement": extracted["yingjie_requirement"]},
    }
    assert map_public_gongkao(row)["应届要求"] == ALL_GRADUATES
    definition = next(field for field in GONGKAO_SCHEMA if field["field_name"] == "应届要求")
    assert definition["type"] == 3
    assert [option["name"] for option in definition["property"]["options"]] == [
        ONLY_FRESH, ALL_GRADUATES, PAST_ALLOWED, CHECK_POSITIONS, UNKNOWN,
    ]


def test_bad_llm_value_is_unknown_not_open_to_all() -> None:
    assert parse_extraction_json({"yingjie_requirement": "都能报"})["yingjie_requirement"] == UNKNOWN
