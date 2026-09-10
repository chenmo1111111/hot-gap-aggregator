"""Trust and noise gate shared by every Gongkao ingestion path.

The collector keeps review rows in its snapshot so they can be audited, while
the public Feishu partition drops them by default.  Enterprise campus records
are retained and explicitly routed to Qiuzhao.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import urlsplit


TITLE_BLACKLIST = re.compile(
    r"直播|回放|讲座|公开课|训练营|冲刺班|系统班|刷题|每日一练|资料|讲义|题库|"
    r"图书|礼包|福利|打卡|进群|领取|优惠|特惠|密训|模考|估分|夸夸|超大杯|"
    r"默写表|时政积累|通勤",
    re.I,
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
    r"拟录用|拟聘用|录用名单|聘用名单|成绩(?:公告|查询|公示)|资格复审|"
    r"面试名单|体检名单|递补|征集.{0,20}企业|招聘会邀请函|选聘法律顾问",
    re.I,
)
STRUCTURED_FIELDS = (
    "startSignUpTime", "endSignUpTime", "startWriteTime", "recruit_count",
    "position_count", "baoming_kaishi", "baoming_jiezhi", "岗位表",
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


def _has_marketing_title(title: str) -> bool:
    """Apply marketing keywords without dropping legitimate organization names."""
    candidate = TITLE_BLACKLIST_EXEMPTIONS.sub("", title)
    return TITLE_BLACKLIST.search(candidate) is not None


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


def assess_gongkao(row: Mapping[str, Any]) -> FilterDecision:
    title = _title(row)
    url = _url(row)
    if _has_marketing_title(title):
        return FilterDecision("drop", "title_blacklist")
    if NON_OPPORTUNITY.search(title):
        return FilterDecision("drop", "not_open_opportunity")
    if LINK_BLACKLIST.search(url):
        return FilterDecision("drop", "link_blacklist")

    from app.pipeline.gongkao_classify import detail_category, record_kind

    extra = _extra(row)
    if (
        str(extra.get("source_site") or "").casefold() == "fenbi"
        and extra.get("official_link_unresolved") is True
    ):
        return FilterDecision("review", "fenbi_without_verified_official_link")
    kind = record_kind(row, llm_choice=extra.get("record_kind"))
    if kind == "秋招":
        return FilterDecision("route_qiuzhao", "enterprise_campus")

    trusted = is_gov_domain(url) or has_structured_announcement(row)
    if not trusted:
        return FilterDecision("drop", "untrusted_without_structure")

    if detail_category(row) == "其它":
        return FilterDecision("review", "category_other")
    return FilterDecision("keep", "trusted_announcement")


def filter_gongkao_items(
    rows: Iterable[T], *, keep_review: bool = True,
) -> tuple[list[T], dict[str, int], list[dict[str, str]]]:
    kept: list[T] = []
    stats = {"input": 0, "kept": 0, "routed": 0, "review": 0, "dropped": 0}
    samples: list[dict[str, str]] = []
    for row in rows:
        stats["input"] += 1
        mapped = _mapping(row)
        decision = assess_gongkao(mapped)
        _set_extra(row, "filter_action", decision.action)
        _set_extra(row, "filter_reason", decision.reason)
        _set_extra(row, "needs_review", decision.action == "review")
        if decision.action == "drop":
            stats["dropped"] += 1
            if len(samples) < 10:
                samples.append({"title": _title(mapped), "reason": decision.reason, "url": _url(mapped)})
            continue
        if decision.action == "route_qiuzhao":
            stats["routed"] += 1
        elif decision.action == "review":
            stats["review"] += 1
            if not keep_review:
                continue
        else:
            stats["kept"] += 1
        kept.append(row)
    return kept, stats, samples
