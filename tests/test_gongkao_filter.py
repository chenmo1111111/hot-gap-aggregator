from app.pipeline.gongkao_filter import assess_gongkao, filter_gongkao_items


def row(title: str, url: str, **extra):
    return {"title": title, "url": url, "extra": extra}


def test_marketing_title_and_fenbi_course_links_are_dropped() -> None:
    assert assess_gongkao(row("筑梦南粤师途5天直播", "https://example.gov.cn/x")).reason == "title_blacklist"
    assert assess_gongkao(row("事业单位公开招聘公告", "https://fenbi.com/spa/course/1")).reason == "link_blacklist"
    assert assess_gongkao(row("事业单位公开招聘拟聘用人员公示", "https://example.gov.cn/x")).reason == "not_open_opportunity"
    assert assess_gongkao(row("关于征集专项服务活动优质企业的公告", "https://example.gov.cn/x")).reason == "not_open_opportunity"


def test_blacklist_words_do_not_drop_legitimate_organization_names() -> None:
    titles = [
        "自治区福利彩票发行中心2026年面向社会公开招聘公告",
        "南昌市红谷滩区社会福利院公开招聘工作人员公告",
        "中国福利会托儿所公开招聘工作人员公告",
        "心理健康与智能评估分中心公开招聘专业技术人员公告",
    ]

    for title in titles:
        decision = assess_gongkao(row(
            title, "https://example.gov.cn/recruit", exam_type="事业单位",
        ))
        assert decision.action == "keep", (title, decision)


def test_government_or_structured_announcement_is_kept() -> None:
    official = row("吉林省直事业单位公开招聘公告", "https://hrss.jl.gov.cn/x", exam_type="事业单位")
    structured = row(
        "山东事业单位公开招聘公告", "https://www.fenbi.com/page/x",
        exam_type="事业单位", has_announcement_structure=True,
    )
    assert assess_gongkao(official).action == "keep"
    assert assess_gongkao(structured).action == "keep"


def test_unresolved_fenbi_discovery_is_review_only() -> None:
    unresolved = row(
        "山东事业单位公开招聘公告", "https://www.fenbi.com/page/x",
        exam_type="事业单位", has_announcement_structure=True,
        source_site="fenbi", official_link_unresolved=True,
    )
    decision = assess_gongkao(unresolved)
    assert decision.action == "review"
    assert decision.reason == "fenbi_without_verified_official_link"


def test_enterprise_campus_is_routed_and_other_is_reviewed() -> None:
    campus = row(
        "中国移动2027届校园招聘", "https://example.gov.cn/x",
        exam_type="国企", record_kind="秋招",
    )
    unknown = row("某项工作通知公告", "https://example.gov.cn/x")
    assert assess_gongkao(campus).action == "route_qiuzhao"
    assert assess_gongkao(unknown).action == "review"
    kept, stats, _ = filter_gongkao_items([unknown], keep_review=False)
    assert kept == []
    assert stats["review"] == 1
