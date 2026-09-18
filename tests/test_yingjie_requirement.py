from __future__ import annotations

import pytest

from app.pipeline.gongkao_enrich import parse_extraction_json
from app.pipeline.yingjie_requirement import (
    ALL_GRADUATES,
    CHECK_POSITIONS,
    ONLY_FRESH,
    UNKNOWN,
    classify_yingjie_requirement,
)
from app.sync_feishu_public import GONGKAO_SCHEMA, map_public_gongkao


@pytest.mark.parametrize(("title", "expected"), [
    ("某市事业单位仅面向2027届高校毕业生招聘公告", ONLY_FRESH),
    ("某局仅限应届毕业生报考的招聘公告", ONLY_FRESH),
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
        ONLY_FRESH, ALL_GRADUATES, CHECK_POSITIONS, UNKNOWN,
    ]


def test_bad_llm_value_is_unknown_not_open_to_all() -> None:
    assert parse_extraction_json({"yingjie_requirement": "都能报"})["yingjie_requirement"] == UNKNOWN
