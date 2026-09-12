"""Classify and route normalized records imported from purchased tables."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Mapping


ENTERPRISE_TYPES = {"央企", "国企", "央国企", "民企", "外企", "银行"}
PUBLIC_TYPES = {"事业单位", "政府机关", "机关事业单位", "科研院所"}
ENTERPRISE_SUFFIX = re.compile(r"(?:有限责任公司|股份有限公司|有限公司|股份公司|集团公司|集团)$")
PUBLIC_SECURITY = re.compile(
    r"公安局|公安机关|消防救援(?:总队|支队|大队)|海关(?:所属)?事业单位|"
    r"出入境边防检查站|检察院|法院"
)
GOVERNMENT_ENTITY = re.compile(
    r"(?:农业农村|人力资源和社会保障|市场监督管理|自然资源|机关事务管理|"
    r"卫生健康|教育|财政|民政|交通运输|文化和旅游|生态环境)(?:厅|局|委员会)|"
    r"人民政府|党委|省委|市委|区委|县委|事业单位"
)
RESEARCH_ENTITY = re.compile(
    r"中国科学院|中科院|中国社会科学院|中国社科院|中国农业科学院|中国农科院|"
    r"(?:研究所|研究院)$"
)
EDUCATION_MEDICAL_ENTITY = re.compile(r"(?:大学|学院|医院|疾病预防控制中心|疾控中心)$")
CENTRAL_SOE_INSTITUTE = re.compile(
    r"(?:中船|中航|中电|中核|航天|兵器).{0,12}(?:[一二三四五六七八九〇零\d]{2,4}所|研究所)$"
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _extra(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("extra")
    return value if isinstance(value, Mapping) else {}


def _field(row: Mapping[str, Any], *names: str) -> str:
    extra = _extra(row)
    for name in names:
        value = extra.get(name.removeprefix("extra.")) if name.startswith("extra.") else row.get(name)
        if text := _text(value):
            return text
    return ""


def classify_purchased_row(row: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(record_kind, meaningful_type_label)`` for a purchased row."""
    company = _field(
        row, "company_name", "company", "companyName", "fullName", "extra.company", "extra.unit"
    )
    company_type = _field(row, "company_type", "extra.company_type", "companyTypeName")
    text = " ".join(
        value
        for value in (
            company,
            _field(row, "position", "title_zh", "title", "jobTitle"),
            _field(row, "notes", "summary_zh", "summary", "announcementTitle"),
        )
        if value
    )

    if CENTRAL_SOE_INSTITUTE.search(company):
        return "秋招", "央企下属单位"
    if PUBLIC_SECURITY.search(text):
        return "公考", "机关事业单位"
    if GOVERNMENT_ENTITY.search(text):
        return "公考", "机关事业单位"
    if RESEARCH_ENTITY.search(company) and not ENTERPRISE_SUFFIX.search(company):
        return "公考", "科研院所"
    if EDUCATION_MEDICAL_ENTITY.search(company) and not ENTERPRISE_SUFFIX.search(company):
        return "公考", "机关事业单位"
    if company_type in PUBLIC_TYPES:
        return "公考", "科研院所" if company_type == "科研院所" else "机关事业单位"
    if company_type in ENTERPRISE_TYPES:
        return "秋招", "国企" if company_type == "央国企" else company_type
    return "秋招", "机构性质待核"


def _published(value: object) -> str | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            stamp = int(value)
            if stamp > 10_000_000_000:
                stamp //= 1000
            return datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def purchased_row_to_gongkao(
    row: Mapping[str, Any], *, source: str, label: str
) -> dict[str, Any]:
    company = _field(row, "company_name", "company", "extra.company", "extra.unit")
    position = _field(row, "position", "title_zh", "title", "jobTitle")
    title = f"{company}｜{position}" if company and company not in position else position or company
    url = _field(row, "announcement_url", "apply_url", "url", "extra.announcement_url")
    record_id = _field(row, "source_record_id", "extra.id", "recruitmentId")
    if not record_id:
        identity = f"{source}|{company.casefold()}|{position.casefold()}"
        record_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    deadline = _field(row, "deadline", "extra.deadline", "extra.endSignUpTime")
    published_at = _published(row.get("updated_at") or row.get("published_at"))
    exam_type = "公安警察" if PUBLIC_SECURITY.search(f"{company} {position}") else "事业单位"
    return {
        "source": "gongkao",
        "title": title,
        "title_zh": title,
        "url": url,
        "published_at": published_at,
        "summary": _field(row, "notes", "summary_zh", "summary") or None,
        "source_label": "购买表-公考分流",
        "extra": {
            "id": f"purchased:{source}:{record_id}",
            "sub": "announcement",
            "subsource": source,
            "upstream_source": source,
            "source_label": "购买表-公考分流",
            "record_kind": "公考",
            "unit": company,
            "position": position,
            "exam_type": exam_type,
            "organization_type": label,
            "endSignUpTime": deadline or None,
            "has_announcement_structure": bool(published_at or deadline or url),
        },
    }


def partition_purchased_rows(
    rows: list[dict[str, Any]], *, source: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    qiuzhao: list[dict[str, Any]] = []
    gongkao: list[dict[str, Any]] = []
    labels: dict[str, int] = {}
    for original in rows:
        row = dict(original)
        kind, label = classify_purchased_row(row)
        labels[label] = labels.get(label, 0) + 1
        if kind == "公考":
            gongkao.append(purchased_row_to_gongkao(row, source=source, label=label))
            continue
        row["company_type"] = label
        extra = dict(_extra(row))
        extra["record_kind"] = "秋招"
        extra["organization_type"] = label
        row["extra"] = extra
        qiuzhao.append(row)
    return qiuzhao, gongkao, labels


__all__ = [
    "classify_purchased_row",
    "partition_purchased_rows",
    "purchased_row_to_gongkao",
]
