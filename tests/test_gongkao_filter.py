from datetime import date

from app.pipeline.gongkao_filter import (
    assess_gongkao,
    filter_gongkao_items,
    title_noise_reason,
)


def row(title: str, url: str, **extra):
    return {"title": title, "url": url, "extra": extra}


def test_marketing_title_and_fenbi_course_links_are_dropped() -> None:
    assert assess_gongkao(row("筑梦南粤师途5天直播", "https://example.gov.cn/x")).reason == "title_noise"
    assert assess_gongkao(row("事业单位公开招聘公告", "https://fenbi.com/spa/course/1")).reason == "link_blacklist"
    assert assess_gongkao(row("事业单位公开招聘拟聘用人员公示", "https://example.gov.cn/x")).reason == "title_noise"
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


def test_shared_title_noise_terms_and_sample_accounting() -> None:
    noisy = [
        row("某高校秋季双选会通知", "https://example.gov.cn/1"),
        row("事业单位招聘名单公示", "https://example.gov.cn/2"),
        row("公务员考试成绩查询入口", "https://example.gov.cn/3"),
    ]
    kept, stats, samples = filter_gongkao_items(noisy)
    assert kept == []
    assert stats["dropped"] == stats["noise_dropped"] == 3
    assert [sample["title"] for sample in samples] == [item["title"] for item in noisy]


def test_all_requested_recruitment_event_terms_are_noise() -> None:
    for term in (
        "宣讲会", "宣讲", "双选会", "招聘会", "空中宣讲", "校园行",
        "校招行程", "校园宣讲", "专场招聘会", "名企双选",
    ):
        assert title_noise_reason(row(f"某企业{term}", "https://example.com/job")) == "title_noise"


def test_post_selection_wording_is_shared_title_noise() -> None:
    assert title_noise_reason(row("三支一扶拟招募人员公示", "https://example.gov.cn/4")) == "title_noise"
    assert title_noise_reason(row("事业单位公开招聘体检安排", "https://example.gov.cn/5")) == "title_noise"
    assert title_noise_reason(row("人才博览会引才拟聘人员公示", "https://example.gov.cn/6")) == "title_noise"


def test_transfer_notice_requires_an_explicit_future_signup_deadline() -> None:
    future = row(
        "事业单位公开招聘调剂公告", "https://example.gov.cn/future",
        exam_type="事业单位", endSignUpTime="2026-09-12",
    )
    expired = row(
        "事业单位公开招聘调剂公告", "https://example.gov.cn/expired",
        exam_type="事业单位", endSignUpTime="2026-09-09",
    )
    unknown = row("事业单位公开招聘调剂公告", "https://example.gov.cn/unknown")
    assert title_noise_reason(future, today=date(2026, 9, 10)) is None
    assert title_noise_reason(expired, today=date(2026, 9, 10)) == "title_noise"
    assert title_noise_reason(unknown, today=date(2026, 9, 10)) == "title_noise"


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


def test_site_keeps_fenbi_timeline_but_feishu_rejects_it() -> None:
    timeline = row(
        "某省事业单位考试日历", "https://www.fenbi.com/page/kaoshidetail/123",
        source_site="fenbi", sub="timeline", endSignUpTime="2026-10-01",
    )
    assert assess_gongkao(timeline, profile="site").action == "review"
    assert assess_gongkao(timeline, profile="feishu").reason == "fenbi_timeline_without_official_link"
    site_rows, _, _ = filter_gongkao_items([timeline], profile="site")
    table_rows, _, _ = filter_gongkao_items([timeline], profile="feishu")
    assert site_rows == [timeline]
    assert table_rows == []


def test_feishu_accepts_only_information_rich_fenbi_announcements() -> None:
    rich = row(
        "事业单位公开招聘公告", "https://hera-webapp.fenbi.com/api/website/article/detail?id=1",
        source_site="fenbi", sub="announcement", recruit_count="53",
    )
    empty = row(
        "事业单位公开招聘公告", "https://hera-webapp.fenbi.com/api/website/article/detail?id=2",
        source_site="fenbi", sub="announcement",
    )
    assert assess_gongkao(rich, profile="feishu").reason == "fenbi_announcement_with_information"
    assert assess_gongkao(empty, profile="feishu").action == "review"


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
