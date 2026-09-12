"""Conservative cross-source deduplication for Gongkao announcements.

Exact title identities are merged first.  The fuzzy pass is restricted to
cross-source rows in the same province whose first-observed dates are within
three days, so the comparison stays bounded and unrelated notices survive.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit


PREFECTURE_PREFIXES = (
    "德州", "济南", "青岛", "潍坊", "淄博", "烟台", "临沂", "聊城", "滨州",
    "菏泽", "济宁", "泰安", "威海", "日照", "东营", "枣庄",
    "石家庄", "保定", "唐山", "邯郸", "邢台", "沧州", "衡水", "廊坊",
    "太原", "晋中", "大同", "临汾", "运城", "长治", "晋城",
    "沈阳", "大连", "鞍山", "抚顺", "本溪", "丹东", "锦州", "营口",
)

PROVINCE_PREFIXES = (
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "广西", "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西",
    "甘肃", "青海", "宁夏", "新疆", "兵团",
)

EVIDENCE_FIELDS = {
    "recruit_count": ("extra.recruit_count", "extra.zhaopin_renshu", "招聘人数", "招录人数"),
    "signup_start": ("extra.startSignUpTime", "extra.baoming_kaishi", "报名开始"),
    "signup_end": ("extra.endSignUpTime", "extra.baoming_jiezhi", "报名截止", "截止日期"),
}

IMPORTANT_TOP_LEVEL = (
    "title", "title_zh", "url", "published_at", "summary", "summary_zh",
)
IMPORTANT_EXTRA = (
    "province", "city", "unit", "position", "recruit_count", "zhaopin_renshu",
    "startSignUpTime", "endSignUpTime", "xueli", "education", "notes", "bei_zhu",
    "first_seen", "exam_type", "announcement_url",
)
NOTE_CONFLICT_FIELDS = {
    "title", "title_zh", "published_at", "summary", "summary_zh",
    "province", "city", "unit", "position", "recruit_count", "zhaopin_renshu",
    "startSignUpTime", "endSignUpTime", "xueli", "education", "notes", "bei_zhu",
    "major", "location", "position_nature", "exam_type",
}


@dataclass(slots=True)
class DedupReport:
    exact_merged_count: int = 0
    fuzzy_merged_count: int = 0
    suspect_pair_count: int = 0
    merged_by_origin: dict[str, int] = field(default_factory=dict)
    merged_samples: list[dict[str, Any]] = field(default_factory=list)
    suspect_samples: list[dict[str, Any]] = field(default_factory=list)

    @property
    def auto_merged_count(self) -> int:
        return self.exact_merged_count + self.fuzzy_merged_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "exact_merged_count": self.exact_merged_count,
            "fuzzy_merged_count": self.fuzzy_merged_count,
            "auto_merged_count": self.auto_merged_count,
            "suspect_pair_count": self.suspect_pair_count,
            "merged_by_origin": dict(self.merged_by_origin),
            "merged_samples": list(self.merged_samples),
            "suspect_samples": list(self.suspect_samples),
        }


def _text(value: object) -> str:
    return str(value or "").strip()


def _extra(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("extra")
    return value if isinstance(value, Mapping) else {}


def _has_value(value: object) -> bool:
    if value in (None, "", [], {}):
        return False
    return not (isinstance(value, str) and value.strip() in {"", "/", "-", "未知", "待定"})


def _canonical_url(value: object) -> str:
    text = _text(value)
    if not text.startswith(("http://", "https://")):
        return ""
    parsed = urlsplit(text)
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path.rstrip("/") or "/", parsed.query, ""))


def _url_identity(value: object) -> str:
    """Return a scheme-insensitive URL identity while preserving the real URL elsewhere."""
    canonical = _canonical_url(value)
    if not canonical:
        return ""
    parsed = urlsplit(canonical)
    return urlunsplit(("", parsed.netloc, parsed.path, parsed.query, ""))


def _province(row: Mapping[str, Any]) -> str:
    raw = _text(_extra(row).get("province") or row.get("province"))
    return re.sub(r"(?:壮族|回族|维吾尔)?自治区$|特别行政区$|省$|市$", "", raw)


def normalize_exact_title(value: object) -> str:
    text = unicodedata.normalize("NFKC", _text(value)).casefold()
    years = sorted(set(re.findall(r"20\d{2}", text)))
    text = re.sub(r"20\d{2}年(?:度)?", "", text)
    text = re.sub(r"[（(]\s*\d+\s*[人名]?\s*[）)]\s*$", "", text)
    normalized = re.sub(r"[^\w]+", "", text, flags=re.UNICODE)
    return normalized + ("|" + ",".join(years) if years else "")


def normalize_fuzzy_title(value: object) -> str:
    text = unicodedata.normalize("NFKC", _text(value)).casefold()
    text = re.sub(r"20\d{2}年(?:度)?", "", text)
    text = re.sub(r"[（(]\s*\d+\s*[人名]?\s*[）)]", "", text)
    for prefecture in PREFECTURE_PREFIXES:
        text = re.sub(
            rf"{re.escape(prefecture)}市?(?=[\u4e00-\u9fff]{{1,5}}(?:县|区|旗|市))",
            "", text,
        )
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)


def _subject_prefix(value: object) -> str:
    text = normalize_fuzzy_title(value)
    marker = re.search(r"公开(?:招聘|招考|招录|选聘)|招聘|引进|招募|选聘|招录|选调", text)
    if not marker:
        return ""
    subject = text[:marker.start()]
    subject = re.sub(
        rf"^(?:{'|'.join(map(re.escape, PROVINCE_PREFIXES))})(?:省|市|(?:壮族|回族|维吾尔)?自治区)?",
        "",
        subject,
    )
    subject = re.sub(
        r"(?:年度|上半年|下半年)*(?:所属|直属|部分)*(?:机关|事业单位|单位)$",
        "",
        subject,
    )
    return subject


def title_similarity(left: object, right: object) -> float:
    a = normalize_fuzzy_title(left)
    b = normalize_fuzzy_title(right)
    if not a or not b:
        return 0.0
    score = SequenceMatcher(None, a, b, autojunk=False).ratio()
    left_subject, right_subject = _subject_prefix(left), _subject_prefix(right)
    if len(left_subject) >= 3 and len(right_subject) >= 3:
        subject_score = SequenceMatcher(
            None, left_subject, right_subject, autojunk=False,
        ).ratio()
        # Boilerplate such as “公开招聘博士专业人员公告” must not make two
        # different institutes look like one announcement merely because they
        # share a deadline.  Exact/near-exact subjects remain unaffected.
        if subject_score < 0.7:
            score = min(score, subject_score)
    return score


def _parse_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or _text(value).isdigit():
            stamp = int(value)
            if stamp > 10_000_000_000:
                stamp //= 1000
            return datetime.fromtimestamp(stamp, timezone.utc).date()
        return datetime.fromisoformat(_text(value).replace("Z", "+00:00")).date()
    except (OSError, OverflowError, TypeError, ValueError):
        match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", _text(value))
        if not match:
            return None
        try:
            return date(*(int(part) for part in match.groups()))
        except ValueError:
            return None


def _first_seen(row: Mapping[str, Any]) -> date | None:
    extra = _extra(row)
    return _parse_date(extra.get("first_seen") or row.get("published_at") or extra.get("issueTime"))


def _path(row: Mapping[str, Any], path: str) -> object:
    value: object = row
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _evidence_value(row: Mapping[str, Any], field: str) -> str:
    for path in EVIDENCE_FIELDS[field]:
        value = _path(row, path)
        if not _has_value(value):
            continue
        if field == "recruit_count":
            number = re.search(r"\d+|若干", _text(value).replace(",", "").replace("，", ""))
            return number.group(0) if number else _text(value)
        parsed = _parse_date(value)
        return parsed.isoformat() if parsed else _text(value)
    return ""


def compare_evidence(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    matches: list[str] = []
    conflicts: list[str] = []
    for field in EVIDENCE_FIELDS:
        a = _evidence_value(left, field)
        b = _evidence_value(right, field)
        if not a or not b:
            continue
        (matches if a == b else conflicts).append(field)
    return matches, conflicts


def _origin(row: Mapping[str, Any]) -> str:
    extra = _extra(row)
    explicit = _text(extra.get("dedup_origin"))
    if explicit:
        return explicit
    if extra.get("government_source") or _text(extra.get("source_site")).casefold() in {"gov", "government"}:
        return "government"
    upstream = _text(extra.get("upstream_source") or extra.get("subsource"))
    if upstream:
        return upstream
    label = _text(row.get("source_label"))
    if label:
        return label
    return _text(extra.get("source_site") or row.get("source")) or "unknown"


def _origins(row: Mapping[str, Any]) -> set[str]:
    value = _extra(row).get("merged_origins")
    result = {_origin(row)}
    if isinstance(value, list):
        result.update(_text(item) for item in value if _text(item))
    return result


def _sync_id(row: Mapping[str, Any]) -> str:
    extra = _extra(row)
    explicit = _text(extra.get("id") or row.get("id"))
    if explicit:
        return explicit
    url = _url_identity(row.get("url") or extra.get("announcement_url"))
    return "url:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24] if url else ""


def _exact_keys(row: Mapping[str, Any]) -> set[str]:
    extra = _extra(row)
    keys: set[str] = set()
    # Do not merge on URL alone: purchased platforms sometimes reuse a generic
    # landing page for unrelated notices.  URLs are still preserved as fields.
    recruitment_id = _text(extra.get("recruitment_id"))
    if recruitment_id:
        keys.add("recruitment:" + recruitment_id)
    title = normalize_exact_title(row.get("title_zh") or row.get("title"))
    province = _province(row)
    if title and province:
        keys.add(f"title:{province}|{title}")
    return keys


def _completeness(row: Mapping[str, Any]) -> tuple[int, int]:
    extra = _extra(row)
    count = sum(_has_value(row.get(name)) for name in IMPORTANT_TOP_LEVEL)
    count += sum(_has_value(extra.get(name)) for name in IMPORTANT_EXTRA)
    # The task requires the row with fewer blanks to be the primary.  Source
    # authority is only a tie breaker; every non-primary URL remains available.
    authority = int(extra.get("government_source") is True or _text(extra.get("source_site")).casefold() in {"gov", "government"})
    return count, authority


def _json_value(value: object) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return _text(value)


def _append_conflict(conflicts: dict[str, list[Any]], name: str, value: object) -> None:
    if not _has_value(value):
        return
    bucket = conflicts.setdefault(name, [])
    if all(_json_value(item) != _json_value(value) for item in bucket):
        bucket.append(copy.deepcopy(value))


def _merge_group(rows: list[Mapping[str, Any]], *, kind: str, similarity: float) -> dict[str, Any]:
    ordered = sorted(enumerate(rows), key=lambda pair: (_completeness(pair[1]), -pair[0]), reverse=True)
    primary = copy.deepcopy(dict(ordered[0][1]))
    primary_extra = dict(_extra(primary))
    conflicts: dict[str, list[Any]] = {
        str(name): list(values) for name, values in (primary_extra.get("merge_conflicts") or {}).items()
        if isinstance(values, list)
    } if isinstance(primary_extra.get("merge_conflicts"), Mapping) else {}

    primary_url = _canonical_url(primary.get("url") or primary_extra.get("announcement_url"))
    backup_urls: list[str] = []
    merged_sources: list[dict[str, str]] = []
    pool_counts: Counter[str] = Counter()
    first_seen_values: list[date] = []
    notes: list[str] = []

    for row in rows:
        extra = _extra(row)
        pool_counts[_text(extra.get("dedup_pool")) or "base"] += 1
        seen_date = _first_seen(row)
        if seen_date:
            first_seen_values.append(seen_date)
        for value in (extra.get("notes"), row.get("notes")):
            text = _text(value)
            if text and text not in notes:
                notes.append(text)
        for value in (
            row.get("url"), row.get("announcement_url"), extra.get("announcement_url"),
            *(extra.get("backup_urls") if isinstance(extra.get("backup_urls"), list) else []),
        ):
            url = _canonical_url(value)
            if url and url != primary_url and url not in backup_urls:
                backup_urls.append(url)
        merged_sources.append({
            "origin": _origin(row), "id": _sync_id(row),
            "pool": _text(extra.get("dedup_pool")) or "base",
            "title": _text(row.get("title_zh") or row.get("title")),
            "url": _text(row.get("url") or extra.get("announcement_url")),
        })

    for _, row in ordered[1:]:
        for name, value in row.items():
            if name == "extra":
                continue
            if not _has_value(primary.get(name)) and _has_value(value):
                primary[name] = copy.deepcopy(value)
            elif _has_value(value) and _json_value(primary.get(name)) != _json_value(value):
                _append_conflict(conflicts, name, value)
        for name, value in _extra(row).items():
            if name in {"backup_urls", "merge_conflicts", "merged_sources", "merged_origins", "notes", "first_seen"}:
                continue
            if not _has_value(primary_extra.get(name)) and _has_value(value):
                primary_extra[name] = copy.deepcopy(value)
            elif _has_value(value) and _json_value(primary_extra.get(name)) != _json_value(value):
                _append_conflict(conflicts, name, value)

    if first_seen_values:
        primary_extra["first_seen"] = min(first_seen_values).isoformat()
    if backup_urls:
        primary_extra["backup_urls"] = backup_urls
        if len(backup_urls) > 1:
            notes.append("其他备用链接：" + "；".join(backup_urls[1:]))
    primary_extra["merged_origins"] = sorted({source["origin"] for source in merged_sources})
    primary_extra["merged_pool_counts"] = dict(pool_counts)
    primary_extra["merged_sources"] = merged_sources
    primary_extra["dedup_merged"] = True
    primary_extra["dedup_merge_kind"] = kind
    primary_extra["dedup_similarity"] = round(similarity, 4)
    if conflicts:
        primary_extra["merge_conflicts"] = conflicts
        visible: list[str] = []
        for name, values in conflicts.items():
            if name in NOTE_CONFLICT_FIELDS:
                visible.append(f"{name}备用值：{'；'.join(_json_value(value) for value in values)}")
        if visible:
            conflict_note = "合并保留差异：" + "；".join(visible)
            if conflict_note not in notes:
                notes.append(conflict_note)
    if notes:
        primary_extra["notes"] = "；".join(notes)
    primary["extra"] = primary_extra
    return primary


def _groups(count: int, pairs: Iterable[tuple[int, int]]) -> list[list[int]]:
    parent = list(range(count))

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    for left, right in pairs:
        union(left, right)
    grouped: dict[int, list[int]] = defaultdict(list)
    for index in range(count):
        grouped[find(index)].append(index)
    return list(grouped.values())


def _merge_exact(rows: list[dict[str, Any]], report: DedupReport) -> list[dict[str, Any]]:
    first_by_key: dict[str, int] = {}
    pairs: list[tuple[int, int]] = []
    for index, row in enumerate(rows):
        for key in _exact_keys(row):
            if key in first_by_key:
                pairs.append((first_by_key[key], index))
            else:
                first_by_key[key] = index
    output: list[dict[str, Any]] = []
    for group in _groups(len(rows), pairs):
        members = [rows[index] for index in group]
        if len(members) == 1:
            output.append(members[0])
            continue
        report.exact_merged_count += len(members) - 1
        merged = _merge_group(members, kind="exact", similarity=1.0)
        primary_origin = _origin(merged)
        remaining_primary = 1
        for row in members:
            origin = _origin(row)
            if origin == primary_origin and remaining_primary:
                remaining_primary -= 1
                continue
            report.merged_by_origin[origin] = report.merged_by_origin.get(origin, 0) + 1
        report.merged_samples.append({"kind": "exact", "titles": [_text(row.get("title_zh") or row.get("title")) for row in members]})
        output.append(merged)
    return output


def _mark_suspect(row: dict[str, Any], other: Mapping[str, Any], similarity: float, conflicts: list[str]) -> None:
    extra = dict(_extra(row))
    other_id = _sync_id(other)
    current = [_text(value) for value in _text(extra.get("possible_duplicate_of")).split(",") if _text(value)]
    if other_id and other_id not in current:
        current.append(other_id)
    extra["dup_suspect"] = True
    extra["possible_duplicate_of"] = ",".join(current)
    extra["duplicate_similarity"] = max(float(extra.get("duplicate_similarity") or 0), round(similarity, 4))
    if conflicts:
        existing = extra.get("duplicate_evidence_conflicts")
        existing_values = existing if isinstance(existing, list) else []
        extra["duplicate_evidence_conflicts"] = sorted(set((*existing_values, *conflicts)))
    row["extra"] = extra


def deduplicate_gongkao_items(items: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], DedupReport]:
    rows = [copy.deepcopy(dict(item)) for item in items]
    report = DedupReport()
    exact_rows = _merge_exact(rows, report)

    by_province: dict[str, list[tuple[int, date]]] = defaultdict(list)
    for index, row in enumerate(exact_rows):
        province = _province(row)
        seen = _first_seen(row)
        if province and seen:
            by_province[province].append((index, seen))

    auto_pairs: list[tuple[int, int]] = []
    suspect_pairs: list[tuple[int, int, float, list[str]]] = []
    for candidates in by_province.values():
        candidates.sort(key=lambda pair: pair[1])
        for offset, (left_index, left_date) in enumerate(candidates):
            for right_index, right_date in candidates[offset + 1:]:
                if (right_date - left_date).days > 3:
                    break
                left, right = exact_rows[left_index], exact_rows[right_index]
                if _origins(left) == _origins(right):
                    continue
                similarity = title_similarity(
                    left.get("title_zh") or left.get("title"),
                    right.get("title_zh") or right.get("title"),
                )
                if similarity < 0.7:
                    continue
                evidence_matches, evidence_conflicts = compare_evidence(left, right)
                if similarity >= 0.9 or evidence_matches:
                    auto_pairs.append((left_index, right_index))
                    report.merged_samples.append({
                        "kind": "fuzzy", "similarity": round(similarity, 4),
                        "evidence_matches": evidence_matches,
                        "titles": [_text(left.get("title_zh") or left.get("title")), _text(right.get("title_zh") or right.get("title"))],
                    })
                else:
                    suspect_pairs.append((left_index, right_index, similarity, evidence_conflicts))

    groups = _groups(len(exact_rows), auto_pairs)
    group_for: dict[int, int] = {}
    fuzzy_rows: list[dict[str, Any]] = []
    for group_number, group in enumerate(groups):
        for index in group:
            group_for[index] = group_number
        members = [exact_rows[index] for index in group]
        if len(members) > 1:
            report.fuzzy_merged_count += len(members) - 1
            merged = _merge_group(
                members, kind="fuzzy",
                similarity=max(title_similarity(
                    left.get("title_zh") or left.get("title"),
                    right.get("title_zh") or right.get("title"),
                ) for position, left in enumerate(members) for right in members[position + 1:]),
            )
            fuzzy_rows.append(merged)
            primary_origin = _origin(merged)
            for row in members:
                origin = _origin(row)
                if origin != primary_origin:
                    report.merged_by_origin[origin] = report.merged_by_origin.get(origin, 0) + 1
        else:
            fuzzy_rows.append(members[0])

    seen_suspects: set[tuple[int, int]] = set()
    for left_index, right_index, similarity, conflicts in suspect_pairs:
        left_group, right_group = group_for[left_index], group_for[right_index]
        if left_group == right_group:
            continue
        pair = tuple(sorted((left_group, right_group)))
        if pair in seen_suspects:
            continue
        seen_suspects.add(pair)
        _mark_suspect(fuzzy_rows[left_group], fuzzy_rows[right_group], similarity, conflicts)
        _mark_suspect(fuzzy_rows[right_group], fuzzy_rows[left_group], similarity, conflicts)
        report.suspect_pair_count += 1
        report.suspect_samples.append({
            "similarity": round(similarity, 4), "evidence_conflicts": conflicts,
            "titles": [
                _text(fuzzy_rows[left_group].get("title_zh") or fuzzy_rows[left_group].get("title")),
                _text(fuzzy_rows[right_group].get("title_zh") or fuzzy_rows[right_group].get("title")),
            ],
        })

    report.merged_samples = [
        *[sample for sample in report.merged_samples if sample.get("kind") == "exact"][:10],
        *[sample for sample in report.merged_samples if sample.get("kind") == "fuzzy"][:10],
    ]
    report.suspect_samples = report.suspect_samples[:20]
    return fuzzy_rows, report


__all__ = [
    "DedupReport", "compare_evidence", "deduplicate_gongkao_items",
    "normalize_exact_title", "normalize_fuzzy_title", "title_similarity",
]
