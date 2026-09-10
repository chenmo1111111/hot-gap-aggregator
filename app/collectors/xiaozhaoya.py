"""Normalize and route a browser-captured Xiaozhaoya snapshot."""

from __future__ import annotations

import html
import re
from datetime import date
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

from app.models import Item
from app.pipeline.gongkao_filter import filter_title_noise_items, is_gov_domain


HOME_URL = "https://www.xiaozhaoya.com/home"
URL_PATTERN = re.compile(r"https?://[^\s，。；、<>\"']+", re.I)
DATE_PATTERN = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})")
GONGKAO_PATTERN = re.compile(r"事业单位|机关|政策性岗位|选调|公务员")
GOVERNMENT_TALENT_PATTERN = re.compile(
    r"(?:政府|人社|组织部|机关).{0,20}人才引进|人才引进.{0,20}(?:政府|人社|组织部|机关)"
)


def _text(value: object) -> str:
    if isinstance(value, (list, tuple, set)):
        return "、".join(part for item in value if (part := _text(item)))
    return str(value or "").strip()


def _list_text(value: object) -> str:
    text = _text(value)
    return re.sub(r"\s*[,，;；|/]\s*", "、", text).strip("、")


def _date(value: object) -> str:
    match = DATE_PATTERN.search(_text(value))
    if not match:
        return ""
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return ""


def _candidate_urls(value: object) -> list[str]:
    text = html.unescape(_text(value)).replace(r"\/", "/")
    candidates = [text] if text.startswith(("http://", "https://")) else URL_PATTERN.findall(text)
    return [candidate.rstrip(")]},.!?;:") for candidate in candidates]


def resolve_public_url(value: object, *, base_url: str = HOME_URL) -> str:
    """Return the first usable HTTP URL, rejecting the site's bare ``/`` placeholder."""
    candidates = _candidate_urls(value)
    text = _text(value)
    if not candidates and text.startswith("/") and text != "/":
        candidates = [urljoin(base_url, text)]
    for candidate in candidates:
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return candidate
    return ""


def _company_type(value: object) -> str:
    text = _text(value)
    if text == "事业单位":
        return "事业单位"
    if "央" in text:
        return "央企"
    if "国" in text:
        return "国企"
    if "外" in text:
        return "外企"
    if "银行" in text:
        return "银行"
    if "民" in text:
        return "民企"
    return "其他"


def is_gongkao_record(row: Mapping[str, Any]) -> bool:
    if _text(row.get("companyTypeName")) == "事业单位":
        return True
    text = " ".join(
        (_text(row.get("announcementTitle")), _text(row.get("sourceName")))
    )
    return bool(GONGKAO_PATTERN.search(text) or GOVERNMENT_TALENT_PATTERN.search(text))


def _exam_type(text: str) -> str:
    for marker, label in (
        ("选调", "选调生"),
        ("公务员", "公务员"),
        ("政策性岗位", "政策性岗位"),
        ("机关", "公务员"),
        ("事业单位", "事业单位"),
    ):
        if marker in text:
            return label
    return "事业单位" if "人才引进" in text else "其他"


def record_to_item(row: Mapping[str, Any], *, rank: int) -> tuple[str, Item]:
    """Convert one original record to the shared Item model and select its dataset."""
    identifier = _text(row.get("recruitmentId"))
    company = _text(row.get("companyName") or row.get("fullName"))
    position = _list_text(row.get("jobTitle"))
    announcement_title = _text(row.get("announcementTitle"))
    title = announcement_title or position or company
    application_url = resolve_public_url(row.get("applicationMethod"))
    announcement_url = resolve_public_url(row.get("announcementLink"))
    target_url = application_url or announcement_url
    missing_link = not target_url
    if missing_link:
        target_url = HOME_URL
    published = _date(row.get("announcementDate") or row.get("updateDate"))
    city = _list_text(row.get("cityNameList"))
    education = _list_text(row.get("educationLevelNameList"))
    cohort = _list_text(row.get("graduationYearList"))
    major = _list_text(row.get("majorRequirements"))
    source_name = _text(row.get("sourceName"))
    notes = "；".join(
        part
        for part in (
            _text(row.get("companyProfile")),
            _text(row.get("jobContent")),
            _text(row.get("workAddress")),
            "原记录未提供有效投递或公告链接，请在校招鸭站内检索" if missing_link else "",
        )
        if part
    )
    raw_extra = {
        "id": f"xiaozhaoya:{identifier}",
        "source_site": "xiaozhaoya",
        "source_label": "校招鸭",
        "upstream_source": "xiaozhaoya",
        "recruitment_id": identifier,
        "company": company,
        "company_full_name": _text(row.get("fullName")),
        "company_type": _company_type(row.get("companyTypeName")),
        "industry": _text(row.get("industryName")),
        "job_category": _list_text(row.get("jobCategoryNameList")),
        "position": position,
        "city": city,
        "education": education,
        "cohort": cohort,
        "major": major,
        "deadline": _date(row.get("applicationDeadline")),
        "announcement_url": announcement_url,
        "application_method": application_url,
        "application_method_raw": _text(row.get("applicationMethod")),
        "written_test": row.get("hasWrittenTest"),
        "recruit_count": row.get("recruitmentCount"),
        "work_address": _text(row.get("workAddress")),
        "salary": _text(row.get("salary")),
        "source_name": source_name,
        "notes": notes,
        "missing_external_url": missing_link,
    }
    if is_gongkao_record(row):
        classification_text = f"{announcement_title} {source_name}"
        raw_extra.update(
            {
                "sub": "announcement",
                "unit": company,
                "exam_type": _exam_type(classification_text),
                "endSignUpTime": _date(row.get("applicationDeadline")) or None,
                "has_announcement_structure": bool(
                    published
                    or _date(row.get("applicationDeadline"))
                    or row.get("recruitmentCount")
                ),
                "government_source": is_gov_domain(target_url),
            }
        )
        return "gongkao", Item(
            source="gongkao",
            rank=rank,
            title=title,
            title_zh=title,
            url=target_url,
            published_at=published or None,
            summary_zh=notes or None,
            extra=raw_extra,
        )
    return "qiuzhao", Item(
        source="jobs",
        rank=rank,
        title=title,
        title_zh=title,
        url=target_url,
        published_at=published or None,
        summary_zh=notes or None,
        extra=raw_extra,
    )


def _qiuzhao_row(item: Item) -> dict[str, Any]:
    extra = item.extra
    written = extra.get("written_test")
    written_test = written if isinstance(written, bool) else "笔试" in _text(written)
    return {
        "company_name": _text(extra.get("company")),
        "company_type": _text(extra.get("company_type")),
        "industry": _text(extra.get("industry")),
        "job_category": _text(extra.get("job_category")),
        "position": _text(extra.get("position")) or item.title,
        "location": _text(extra.get("city")),
        "education": _text(extra.get("education")),
        "major": _text(extra.get("major")),
        "cohort": _text(extra.get("cohort")),
        "deadline": _text(extra.get("deadline")),
        "written_test": written_test,
        "apply_url": item.url,
        "announcement_url": _text(extra.get("announcement_url")) or item.url,
        "notes": _text(extra.get("notes")),
        "updated_at": item.published_at or "",
        "source_record_id": _text(extra.get("id")),
        "source_label": "校招鸭",
        "upstream_source": "xiaozhaoya",
        "extra": {
            "source_site": "xiaozhaoya",
            "source_name": _text(extra.get("source_name")),
            "salary": _text(extra.get("salary")),
            "work_address": _text(extra.get("work_address")),
            "missing_external_url": bool(extra.get("missing_external_url")),
        },
    }


def split_records(
    records: list[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert, de-duplicate, noise-filter, and route raw snapshot rows."""
    converted: list[tuple[str, Item]] = []
    seen: set[str] = set()
    for row in records:
        identifier = _text(row.get("recruitmentId"))
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        converted.append(record_to_item(row, rank=len(converted) + 1))

    filtered_items, _, _ = filter_title_noise_items([item for _, item in converted])
    kept = {id(item) for item in filtered_items}
    qiuzhao: list[dict[str, Any]] = []
    gongkao: list[dict[str, Any]] = []
    for kind, item in converted:
        if id(item) not in kept:
            continue
        if kind == "gongkao":
            item.rank = len(gongkao) + 1
            gongkao.append(item.to_dict())
        else:
            item.rank = len(qiuzhao) + 1
            qiuzhao.append(_qiuzhao_row(item))
    return qiuzhao, gongkao


__all__ = [
    "HOME_URL",
    "is_gongkao_record",
    "record_to_item",
    "resolve_public_url",
    "split_records",
]
