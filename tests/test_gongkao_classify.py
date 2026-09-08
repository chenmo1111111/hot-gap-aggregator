from app.pipeline.gongkao_classify import detail_category


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
