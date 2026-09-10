"""Trust and noise gate shared by every Gongkao ingestion path.

The collector keeps review rows in its snapshot so they can be audited, while
the public Feishu partition drops them by default.  Enterprise campus records
are retained and explicitly routed to Qiuzhao.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, TypeVar
from urllib.parse import urlsplit


TITLE_NOISE_TERMS = (
    "空中宣讲", "专场招聘", "校招行程", "福利发放", "报名入口", "操作指南", "温馨提示",
    "名单公示", "拟录用", "拟聘用", "拟录取", "拟引进", "拟考察", "资格复审",
    "资格审查", "资格确认", "成绩公布", "成绩查询", "笔试成绩", "面试成绩", "面试公告",
    "面试通知", "体检公告", "体检通知", "考察公告", "递补公告", "递补通知", "违纪违规",
    "取消资格", "延期公告", "更正公告", "调剂公告", "双选会", "招聘会", "宣讲会",
    "拟招募", "体检安排",
    "宣讲", "校园行", "名企双选", "直播", "回放", "讲座", "公开课", "训练营", "冲刺班", "刷题",
    "资料", "讲义", "题库", "图书", "礼包", "打卡", "进群", "领取", "准考证",
    # Existing high-noise Fenbi marketing phrases remain covered by the shared gate.
    "系统班", "每日一练", "优惠", "特惠", "密训", "模考", "估分", "夸夸", "超大杯",
    "默写表", "时政积累", "通勤", "福利",
)
TITLE_NOISE_BLACKLIST = re.compile(
    "|".join(re.escape(term) for term in sorted(TITLE_NOISE_TERMS, key=len, reverse=True)), re.I,
)
TITLE_BLACKLIST_EXEMPTIONS = re.compile(
    r"福利彩票|(?:社会)?福利院|中国福利会|评估分(?:中心|分中心|部|院)",
    re.I,
)
LINK_BLACKLIST = re.compile(
    r"(?:^|//)(?:www\.)?fenbi\.com/(?:spa|kaoyan)(?:/|\?|$)|"
    r"(?:live|zhibo|course|kecheng|classroom|mall)[./_-]",
    re.I,
)
NON_OPPORTUNITY = re.compile(
    r"录用名单|聘用名单|成绩公示|面试名单|体检名单|递补|"
    r"征集.{0,20}企业|招聘会邀请函|选聘法律顾问",
    re.I,
)
STRUCTURED_FIELDS = (
    "startSignUpTime", "endSignUpTime", "startWriteTime", "recruit_count",
    "position_count", "baoming_kaishi", "baoming_jiezhi", "岗位表",
)
PUBLIC_INFORMATION_FIELDS = (
    "startSignUpTime", "endSignUpTime", "baoming_kaishi", "baoming_jiezhi",
    "signup_start", "signup_deadline", "application_deadline", "registration_deadline",
    "recruit_count", "position_count", "zhaopin_renshu", "招聘人数", "岗位数",
)
OFFICIAL_HOSTS = {
    "www.impta.com.cn", "impta.com.cn", "81rc.81.cn", "jobs.cas.cn",
    "www.hbrc.com.cn", "hbrc.com.cn", "www.cpta.com.cn", "cpta.com.cn",
}


@dataclass(frozen=True)
class FilterDecision:
    action: str  # keep | route_qiuzhao | review | drop
    reason: str


T = TypeVar("T")


def _mapping(row: Any) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    to_dict = getattr(row, "to_dict", None)
    return to_dict() if callable(to_dict) else {}


def _extra(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("extra")
    return value if isinstance(value, Mapping) else {}


def _title(row: Mapping[str, Any]) -> str:
    return str(row.get("title_zh") or row.get("title") or row.get("公告标题") or "").strip()


def _url(row: Mapping[str, Any]) -> str:
    extra = _extra(row)
    return str(
        row.get("url") or row.get("announcement_url") or extra.get("announcement_url")
        or extra.get("url") or row.get("链接") or ""
    ).strip()


DEADLINE_FIELDS = (
    "endSignUpTime", "baoming_jiezhi", "signup_deadline", "application_deadline",
    "registration_deadline", "deadline", "报名截止", "报名结束",
)
DATE_PATTERN = re.compile(r"(?<!\d)(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?")
CHINA_TZ = timezone(timedelta(hours=8))


def _as_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        try:
            stamp = int(value)
            if stamp > 10_000_000_000:
                stamp //= 1000
            return datetime.fromtimestamp(stamp, CHINA_TZ).date()
        except (OSError, OverflowError, ValueError):
            return None
    match = DATE_PATTERN.search(str(value))
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def _has_future_signup_deadline(row: Mapping[str, Any], *, today: date) -> bool:
    extra = _extra(row)
    values = [row.get(field) for field in DEADLINE_FIELDS]
    values.extend(extra.get(field) for field in DEADLINE_FIELDS)
    title = _title(row)
    title_deadline = re.search(
        r"(?:报名(?:截止|结束)|截止日期).{0,16}(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)",
        title,
    )
    if title_deadline:
        values.append(title_deadline.group(1))
    return any((parsed := _as_date(value)) is not None and parsed >= today for value in values)


def title_noise_reason(row: Any, *, today: date | None = None) -> str | None:
    """Return the shared public-feed noise reason, preserving live transfer notices."""
    mapped = _mapping(row)
    candidate = TITLE_BLACKLIST_EXEMPTIONS.sub("", _title(mapped))
    matches = list(TITLE_NOISE_BLACKLIST.finditer(candidate))
    if not matches:
        return None
    current = today or datetime.now(CHINA_TZ).date()
    if all(match.group(0).casefold() == "调剂公告" for match in matches):
        if _has_future_signup_deadline(mapped, today=current):
            return None
    return "title_noise"


def filter_title_noise_items(
    rows: Iterable[T], *, today: date | None = None, sample_limit: int = 15,
) -> tuple[list[T], dict[str, int], list[dict[str, str]]]:
    """Apply the same title-noise policy to non-Gongkao feeds such as jobs."""
    kept: list[T] = []
    stats = {"input": 0, "kept": 0, "noise_dropped": 0}
    samples: list[dict[str, str]] = []
    for row in rows:
        stats["input"] += 1
        mapped = _mapping(row)
        reason = title_noise_reason(mapped, today=today)
        if reason:
            stats["noise_dropped"] += 1
            _set_extra(row, "filter_action", "drop")
            _set_extra(row, "filter_reason", reason)
            if len(samples) < sample_limit:
                samples.append({"title": _title(mapped), "reason": reason, "url": _url(mapped)})
            continue
        stats["kept"] += 1
        kept.append(row)
    return kept, stats, samples


def is_gov_domain(url: object) -> bool:
    try:
        host = (urlsplit(str(url or "")).hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    return (
        host.endswith(".gov.cn") or host == "gov.cn" or host.endswith(".cas.cn")
        or host in OFFICIAL_HOSTS
    )


def has_structured_announcement(row: Mapping[str, Any]) -> bool:
    extra = _extra(row)
    if extra.get("has_announcement_structure") or extra.get("government_source"):
        return True
    return any(extra.get(name) not in (None, "", [], {}) for name in STRUCTURED_FIELDS)


def _set_extra(row: Any, name: str, value: object) -> None:
    if isinstance(row, Mapping):
        extra = row.get("extra")
        if not isinstance(extra, dict):
            extra = dict(extra) if isinstance(extra, Mapping) else {}
            try:
                row["extra"] = extra  # type: ignore[index]
            except TypeError:
                return
        extra[name] = value
        return
    extra = getattr(row, "extra", None)
    if isinstance(extra, dict):
        extra[name] = value


def is_fenbi_timeline(row: Mapping[str, Any]) -> bool:
    extra = _extra(row)
    url = _url(row)
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        path = parsed.path.casefold()
    except ValueError:
        host = path = ""
    is_fenbi = host == "fenbi.com" or host.endswith(".fenbi.com")
    return (
        (str(extra.get("sub") or "").casefold() == "timeline" and (not url or is_fenbi))
        or (is_fenbi and any(marker in path for marker in (
            "/page/kaoshidetail/", "/page/exam-timeline-detail/",
        )))
    )


def has_public_information(row: Mapping[str, Any]) -> bool:
    extra = _extra(row)
    return any(
        value not in (None, "", [], {}, 0, "0")
        for name in PUBLIC_INFORMATION_FIELDS
        for value in (row.get(name), extra.get(name))
    )


def _is_fenbi(row: Mapping[str, Any]) -> bool:
    extra = _extra(row)
    try:
        host = (urlsplit(_url(row)).hostname or "").casefold()
    except ValueError:
        host = ""
    return str(extra.get("source_site") or "").casefold() == "fenbi" or (
        host == "fenbi.com" or host.endswith(".fenbi.com")
    )


def _is_purchased_gongkao(row: Mapping[str, Any]) -> bool:
    extra = _extra(row)
    values = (
        row.get("source_label"), extra.get("source_label"), extra.get("upstream_source"),
        extra.get("source_site"), extra.get("preferred_link_source"),
    )
    return any(str(value or "").casefold() in {
        "购买表-公考", "feishu_sheet", "gongkao_sheet",
    } for value in values)


def assess_gongkao(row: Mapping[str, Any], *, profile: str = "site") -> FilterDecision:
    title = _title(row)
    url = _url(row)
    if title_noise_reason(row):
        return FilterDecision("drop", "title_noise")
    if NON_OPPORTUNITY.search(title):
        return FilterDecision("drop", "not_open_opportunity")
    if LINK_BLACKLIST.search(url):
        return FilterDecision("drop", "link_blacklist")

    from app.pipeline.gongkao_classify import detail_category, record_kind

    extra = _extra(row)
    kind = record_kind(row, llm_choice=extra.get("record_kind"))
    if kind == "秋招":
        return FilterDecision("route_qiuzhao", "enterprise_campus")

    if profile == "feishu":
        if is_fenbi_timeline(row):
            return FilterDecision("review", "fenbi_timeline_without_official_link")
        if _is_fenbi(row):
            if str(extra.get("sub") or "").casefold() == "announcement" and has_public_information(row):
                return FilterDecision("keep", "fenbi_announcement_with_information")
            return FilterDecision("review", "fenbi_without_public_information")
        if is_gov_domain(url) or _is_purchased_gongkao(row):
            if detail_category(row) == "其它":
                return FilterDecision("review", "category_other")
            return FilterDecision("keep", "trusted_announcement")
        return FilterDecision("review", "nonofficial_without_verified_link")

    # The website is the discovery layer: Fenbi timelines and incomplete
    # announcements remain visible, while obviously noisy/non-opportunity rows
    # were already removed above.
    if _is_fenbi(row):
        if is_fenbi_timeline(row):
            return FilterDecision("review", "fenbi_timeline_site_only")
        if extra.get("official_link_unresolved") is True:
            return FilterDecision("review", "fenbi_without_verified_official_link")
        if detail_category(row) == "其它":
            return FilterDecision("review", "category_other")
        return FilterDecision("keep", "fenbi_discovery")

    trusted = is_gov_domain(url) or has_structured_announcement(row)
    if not trusted:
        return FilterDecision("drop", "untrusted_without_structure")

    if detail_category(row) == "其它":
        return FilterDecision("review", "category_other")
    return FilterDecision("keep", "trusted_announcement")


def filter_gongkao_items(
    rows: Iterable[T], *, profile: str = "site", keep_review: bool | None = None,
) -> tuple[list[T], dict[str, int], list[dict[str, str]]]:
    if profile not in {"site", "feishu"}:
        raise ValueError("profile must be 'site' or 'feishu'")
    include_review = profile == "site" if keep_review is None else keep_review
    kept: list[T] = []
    stats = {
        "input": 0, "kept": 0, "routed": 0, "review": 0, "dropped": 0,
        "noise_dropped": 0,
    }
    samples: list[dict[str, str]] = []
    for row in rows:
        stats["input"] += 1
        mapped = _mapping(row)
        decision = assess_gongkao(mapped, profile=profile)
        _set_extra(row, "filter_action", decision.action)
        _set_extra(row, "filter_reason", decision.reason)
        _set_extra(row, "needs_review", decision.action == "review")
        if decision.action == "drop":
            stats["dropped"] += 1
            if decision.reason == "title_noise":
                stats["noise_dropped"] += 1
            if len(samples) < 15:
                samples.append({"title": _title(mapped), "reason": decision.reason, "url": _url(mapped)})
            continue
        if decision.action == "route_qiuzhao":
            stats["routed"] += 1
        elif decision.action == "review":
            stats["review"] += 1
            if not include_review:
                continue
        else:
            stats["kept"] += 1
        kept.append(row)
    return kept, stats, samples
