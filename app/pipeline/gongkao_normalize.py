"""Shared normalization for cross-source Gongkao matching."""

from __future__ import annotations

import re
import unicodedata


PROVINCE_ALIASES = {
    "北京市": "北京", "天津市": "天津", "上海市": "上海", "重庆市": "重庆",
    "内蒙古自治区": "内蒙古", "广西壮族自治区": "广西", "西藏自治区": "西藏",
    "宁夏回族自治区": "宁夏", "新疆维吾尔自治区": "新疆",
}

TRAILING_NOISE = re.compile(
    r"(?:[（(]\s*(?:招聘|招录)?\s*\d+\s*[人名]?\s*[）)])$|"
    r"(?:[-—_ ]*(?:招聘|招录)?\s*\d+\s*[人名])$|"
    r"(?:\s*[【\[].*?[】\]])$|(?:\s*[丨|].*)$",
    re.I,
)
MARKETING_SUFFIX = re.compile(
    r"[-—_:：\s]*(?:筑梦南粤师途\d+天直播|直播(?:课|解析)?|课程(?:推荐)?|备考(?:指导|礼包)).*$",
    re.I,
)


def normalize_province(value: object) -> str:
    text = " ".join(str(value or "全国").split())
    if text in PROVINCE_ALIASES:
        return PROVINCE_ALIASES[text]
    text = re.sub(r"(?:壮族|回族|维吾尔)?自治区$|特别行政区$|省$|市$", "", text)
    return text or "全国"


def clean_official_title(value: object) -> str:
    text = unicodedata.normalize("NFKC", " ".join(str(value or "").split())).strip()
    text = MARKETING_SUFFIX.sub("", text).strip(" -—_|丨:：")
    previous = None
    while text and text != previous:
        previous = text
        text = TRAILING_NOISE.sub("", text).strip(" -—_|丨:：")
    return text


def normalize_notice_title(value: object) -> str:
    return re.sub(r"[^\w]+", "", clean_official_title(value).casefold())
