"""Conservative, announcement-level fresh-graduate eligibility labels.

An announcement can contain positions with different conditions.  An absent
restriction (or the old ``xian_yingjie=False`` extraction) is not evidence that
previous graduates may apply to every position.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


ONLY_FRESH = "仅应届"
ALL_GRADUATES = "应往届均可"
CHECK_POSITIONS = "需核对岗位表"
UNKNOWN = "未明确"
OPTIONS = (ONLY_FRESH, ALL_GRADUATES, CHECK_POSITIONS, UNKNOWN)

_MIXED = re.compile(
    r"(?:部分|部分岗位|部分职位|个别岗位|部分计划).{0,12}(?:仅限|限|面向|招录|招聘).{0,8}(?:应届|20\d{2}届)"
    r"|应届.{0,10}(?:与|和|及).{0,10}非应届.{0,10}(?:岗位|职位)"
)
_ALL = re.compile(
    r"应往届(?:毕业生)?(?:均可|皆可|均可报|均可报名|均可报考)"
    r"|(?:应届|往届)(?:生|毕业生)?.{0,4}(?:和|与|及|、).{0,4}(?:往届|应届)(?:生|毕业生)?.{0,6}(?:均可|皆可|均可报|不限)"
    r"|不限应届|不限制应届|不要求应届|往届(?:生|毕业生)?(?:亦|也|均)?可(?:报考|报名|应聘|投递)"
)
_ONLY = re.compile(
    r"(?:仅限|只限|限|仅面向|只面向|仅招|只招|专招|定向招聘|专项招聘).{0,10}(?:应届|20\d{2}届)"
    r"|(?:应届|20\d{2}届).{0,10}(?:专场招聘|专项招聘|专招)"
    r"|面向.{0,12}(?:20\d{2}届|应届)(?:普通)?(?:高校)?毕业生(?:招聘|招录|的招聘)?"
)


def normalize_yingjie_requirement(value: object) -> str:
    """Accept only the four public labels; never treat a boolean as a label."""
    if isinstance(value, bool):
        return UNKNOWN
    label = str(value or "").strip()
    aliases = {
        "不限应届": ALL_GRADUATES,
        "不限": ALL_GRADUATES,
        "仅限应届": ONLY_FRESH,
        "部分岗位限应届": CHECK_POSITIONS,
        "分岗位要求": CHECK_POSITIONS,
        "需看岗位表": CHECK_POSITIONS,
    }
    label = aliases.get(label, label)
    return label if label in OPTIONS else UNKNOWN


def classify_yingjie_requirement(row: Mapping[str, Any]) -> str:
    """Classify only explicit evidence; unknown remains a visible filter value."""
    extra = row.get("extra") if isinstance(row.get("extra"), Mapping) else {}
    explicit = normalize_yingjie_requirement(extra.get("yingjie_requirement"))
    if explicit != UNKNOWN:
        return explicit

    # The title is available across government, Fenbi and purchased-table rows.
    # Free-form notes may contain copied examples or another position's rules.
    title = str(row.get("title_zh") or row.get("title") or row.get("招录单位·公告") or "")
    title = re.sub(r"\s+", "", title)
    if _MIXED.search(title):
        return CHECK_POSITIONS
    if _ALL.search(title):
        return ALL_GRADUATES
    if _ONLY.search(title):
        return ONLY_FRESH

    # Legacy extraction only signals *some* fresh-graduate restriction.  It
    # cannot prove that all positions in the announcement are fresh-only.
    if extra.get("xian_yingjie") is True:
        return CHECK_POSITIONS
    return UNKNOWN
