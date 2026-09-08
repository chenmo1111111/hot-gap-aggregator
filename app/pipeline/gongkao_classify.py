"""Stable Gongkao subcategory classification.

Upstream Fenbi types are authoritative. Text keywords are used only when the
upstream type is absent or explicitly means "other".
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.collectors.gongkao_types import TYPE_LABELS


OTHER_TYPES = {"", "其他", "其它", "其他考试", "其它考试", "未知", "不限", "none", "null"}


def _extra(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("extra")
    return value if isinstance(value, Mapping) else {}


def _upstream_type(row: Mapping[str, Any]) -> str:
    extra = _extra(row)
    fallback = ""
    for value in (
        extra.get("exam_type"), extra.get("examType"), extra.get("type"),
        row.get("exam_type"), row.get("examType"), row.get("type"), row.get("招录类型"),
    ):
        if value in (None, ""):
            continue
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            candidate = str(value).strip()
        else:
            candidate = TYPE_LABELS.get(numeric, "其他考试")
        if candidate.casefold() not in OTHER_TYPES:
            return candidate
        fallback = fallback or candidate
    return fallback


def _is_central_soe(text: str) -> bool:
    if any(marker in text for marker in ("央企", "中央企业", "中央直属企业", "国务院国资委")):
        return True
    return bool(re.search(r"(?:^|[：:\s])(?:中国|中核|中铁|中建|中交|中电|中航|中车|中粮)[^，。；]{0,24}(?:集团|公司|局|院)", text))


def _direct_category(upstream_type: str, text: str) -> str | None:
    value = upstream_type.strip()
    if value.casefold() in OTHER_TYPES:
        return None
    if "事业单位" in value or "事业编" in value:
        return "事业单位"
    if any(marker in value for marker in ("国企", "国有企业")):
        return "央企" if _is_central_soe(text) else "国企"
    if any(marker in value for marker in ("军队文职", "部队文职")):
        return "军队文职"
    if any(marker in value for marker in ("公安", "警察", "招警", "人民警察")):
        return "公安警察"
    if any(marker in value for marker in ("选调生", "选调")):
        return "选调生"
    if any(marker in value for marker in ("三支一扶", "三支")):
        return "三支一扶"
    if any(marker in value for marker in ("银行", "农信社", "信用社")):
        return "银行"
    if any(marker in value for marker in ("教师", "教育招聘")):
        return "教师"
    if any(marker in value for marker in ("医疗", "卫生")):
        return "医疗"
    if any(marker in value for marker in ("公务员", "省考", "国考", "公开遴选", "法检")):
        return "公务员"
    return None


def _fallback_category(text: str) -> str:
    if any(marker in text for marker in ("军队文职", "部队文职")):
        return "军队文职"
    if any(marker in text for marker in ("定向选调", "中央选调", "选调生", "选调公告")):
        return "选调生"
    if any(marker in text for marker in ("公安招警", "公安机关", "人民警察", "警务辅助")):
        return "公安警察"
    if any(marker in text for marker in ("三支一扶", "三支计划")):
        return "三支一扶"
    if any(marker in text for marker in ("银行", "农信社", "信用社", "村镇银行")):
        return "银行"
    if any(marker in text for marker in ("事业单位", "事业编")):
        return "事业单位"
    if any(marker in text for marker in ("教师招聘", "教师岗", "教育系统招聘")):
        return "教师"
    if any(marker in text for marker in ("医疗卫生", "医院招聘", "卫生系统招聘")):
        return "医疗"
    if any(marker in text for marker in ("公务员", "国家公考", "省考", "公开遴选")):
        return "公务员"
    if _is_central_soe(text):
        return "央企"
    if any(marker in text for marker in ("国有企业", "国企招聘", "国资委所属")):
        return "国企"
    return "其它"


def detail_category(row: Mapping[str, Any]) -> str:
    extra = _extra(row)
    tags = extra.get("tags") if isinstance(extra.get("tags"), list) else []
    text = " ".join(str(value or "") for value in (
        row.get("unit"), row.get("company"), row.get("招录单位·公告"),
        row.get("title_zh"), row.get("title"), extra.get("unit"), *tags,
    ))
    upstream_type = _upstream_type(row)
    direct = _direct_category(upstream_type, text)
    if direct:
        return direct
    if upstream_type.strip().casefold() not in OTHER_TYPES:
        return "其它"
    return _fallback_category(text)
