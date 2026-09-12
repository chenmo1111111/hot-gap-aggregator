from app.pipeline.gongkao_classify import (
    detail_category,
    is_public_gongkao_noise,
    record_kind,
    record_kind_needs_llm,
)


def test_upstream_business_type_wins_over_misleading_company_keyword() -> None:
    row = {
        "title": "某集团所属研究院事业单位公开招聘工作人员公告",
        "extra": {"exam_type": "事业单位"},
    }
    assert detail_category(row) == "事业单位"


def test_numeric_fenbi_types_map_directly() -> None:
    assert detail_category({"title": "某考试", "extra": {"examType": 3}}) == "选调生"
    assert detail_category({"title": "某考试", "extra": {"type": 6}}) == "军队文职"
    assert detail_category({"title": "某考试", "extra": {"examType": 8}}) == "公安警察"
    assert detail_category({
        "title": "某考试", "extra": {"exam_type": "其他考试", "examType": 4},
    }) == "事业单位"


def test_central_soe_is_refined_only_after_upstream_guoqi_type() -> None:
    assert detail_category({
        "title": "中国建筑集团招聘公告", "extra": {"exam_type": "国企招聘"},
    }) == "央企"
    assert detail_category({
        "title": "地方建设集团事业单位招聘公告", "extra": {"exam_type": "事业单位"},
    }) == "事业单位"


def test_keywords_are_only_fallback_and_avoid_generic_company_guessing() -> None:
    assert detail_category({"title": "某有限公司招聘公告", "extra": {}}) == "其它"
    assert detail_category({"title": "市属事业单位公开招聘公告", "extra": {}}) == "事业单位"
    assert detail_category({
        "title": "某国有企业社区工作者招聘", "extra": {"exam_type": "社区工作者"},
    }) == "其它"


def test_enterprise_campus_recruitment_routes_to_qiuzhao() -> None:
    assert record_kind({
        "title": "中国移动辽宁分公司2027届校园招聘",
        "extra": {"exam_type": "国企招聘"},
    }) == "秋招"
    assert record_kind({
        "title": "中国工商银行黑龙江省分行2027年度校园招聘",
        "extra": {"exam_type": "银行"},
    }) == "秋招"
    assert record_kind({
        "title": "中船七一四所2027届校园招聘正式启动",
        "extra": {"exam_type": "事业单位", "businessType": 4},
    }) == "秋招"


def test_public_entities_and_company_social_recruitment_stay_in_gongkao() -> None:
    assert record_kind({
        "title": "山东大学2027届公开招聘工作人员公告",
        "extra": {"exam_type": "事业单位"},
    }) == "公考"
    assert record_kind({
        "title": "某国有集团社会招聘公告",
        "extra": {"exam_type": "国企招聘"},
    }) == "公考"
    for title in (
        "寿光市农业农村局公开招聘2026届公费农科毕业生公告",
        "荥阳市消防救援大队2026届公开招聘公告",
        "中国社科院考古研究所2027届招聘公告",
        "龙邦出入境边防检查站2027届警务辅助人员招聘公告",
    ):
        assert record_kind({"title": title, "extra": {"record_kind": "秋招"}}) == "公考"
    assert record_kind({
        "title": "大连市公安局2026年公开招聘警务辅助人员公告",
        "extra": {"businessType": 4, "exam_type": "招警", "record_kind": "秋招"},
    }) == "公考"


def test_ambiguous_campus_row_uses_llm_choice_and_fails_safe() -> None:
    row = {"title": "星辰计划2027届校园招聘", "extra": {}}
    assert record_kind_needs_llm(row) is True
    assert record_kind(row) == "公考"
    assert record_kind(row, llm_choice="秋招") == "秋招"


def test_narrow_public_noise_filter() -> None:
    assert is_public_gongkao_noise({"title": "某高校博士后招聘公告"}) is True
    assert is_public_gongkao_noise({"title": "某学校教师引进公告"}) is True
    assert is_public_gongkao_noise({"title": "某学校教师公开招聘公告"}) is False
