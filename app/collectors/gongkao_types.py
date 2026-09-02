from __future__ import annotations


TYPE_LABELS = {
    1: "国考", 2: "省考", 3: "选调生", 4: "事业单位",
    5: "三支一扶", 6: "军队文职", 7: "医疗卫生", 8: "公安招警",
    9: "国企招聘", 10: "教师招聘", 11: "社区工作者", 12: "银行招聘",
    13: "农信社", 14: "国企招聘", 15: "社会招聘", 16: "事业单位",
    17: "公开遴选", 18: "法检系统", 19: "其他考试", 20: "校园招聘",
}

KNOWN_PROVINCES = {
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海", "江苏",
    "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西", "海南",
    "重庆", "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆", "兵团",
}

TYPE_TAG_PRIORITY = (
    "公务员", "选调生", "事业单位", "教师", "医疗", "军队文职", "三支一扶", "社区工作者",
    "银行", "农信社", "国企", "高校", "私企",
)


def title_type(title: object) -> str | None:
    value = str(title or "")
    if any(marker in value for marker in ("中央选调", "定向选调", "选调生", "选调")):
        return "选调生"
    if any(marker in value for marker in ("国考", "国家公务员", "中央机关及其直属机构")):
        return "国考"
    return None


def timeline_type(code: object, title: object = "") -> str:
    inferred = title_type(title)
    if inferred:
        return inferred
    try:
        return TYPE_LABELS.get(int(code), "其他考试")
    except (TypeError, ValueError):
        return "其他考试"


def article_type(tags: list[dict], title: object = "") -> str:
    names = [str(tag.get("name") or "") for tag in tags if tag.get("type") == 2]
    if not names:
        names = [str(tag.get("name") or "") for tag in tags]
    inferred = title_type(f"{title} {' '.join(names)}")
    if inferred:
        return inferred
    if any("省考" in name for name in names):
        return "省考"
    if any("公务员" in name for name in names):
        return "公务员考试"
    if any("遴选" in name for name in names):
        return "公开遴选"
    for preferred in TYPE_TAG_PRIORITY:
        if any(preferred in name for name in names):
            if preferred == "高校":
                return "教师招聘"
            if preferred == "私企":
                return "社会招聘"
            return preferred
    return names[0] if names else "其他考试"


def article_province(tags: list[dict]) -> str:
    for tag in tags:
        name = str(tag.get("name") or "")
        if tag.get("type") == 1 and tag.get("level") == 0 and name in KNOWN_PROVINCES:
            return name
    return "全国"
