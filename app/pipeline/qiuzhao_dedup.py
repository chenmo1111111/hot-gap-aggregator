"""Cross-source Qiuzhao deduplication with lossless field enrichment."""

from __future__ import annotations

import copy
import re
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping

from app.pipeline.gongkao_dedup import title_similarity


def _text(value: object) -> str:
    return str(value or "").strip()


def _has(value: object) -> bool:
    return value not in (None, "", [], {}, "/", "未知", "待定")


def normalize_company(value: object) -> str:
    text = unicodedata.normalize("NFKC", _text(value)).casefold()
    text = re.sub(r"(?:股份)?有限公司|有限责任公司|集团(?:股份)?|公司$", "", text)
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)


def normalize_position(value: object) -> str:
    text = unicodedata.normalize("NFKC", _text(value)).casefold()
    text = re.sub(r"20\d{2}届?", "", text)
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)


def _position_variants(row: Mapping[str, Any]) -> list[str]:
    extra = row.get("extra") if isinstance(row.get("extra"), Mapping) else {}
    values = extra.get("position_list") if isinstance(extra.get("position_list"), list) else []
    variants = [_text(value) for value in values if _text(value)]
    if not variants:
        variants = [
            part.strip() for part in re.split(r"[、,，/／;；|｜\n]+", _text(row.get("position")))
            if part.strip()
        ]
    return variants or [_text(row.get("position"))]


def _position_similarity(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    return max(
        max(
            title_similarity(left_value, right_value),
            SequenceMatcher(
                None, normalize_position(left_value), normalize_position(right_value), autojunk=False,
            ).ratio(),
        )
        for left_value in _position_variants(left)
        for right_value in _position_variants(right)
    )


def _origin(row: Mapping[str, Any]) -> str:
    return _text(row.get("upstream_source") or row.get("source_label")) or "unknown"


@dataclass
class QiuzhaoDedupReport:
    input_count: int = 0
    output_count: int = 0
    exact_merged_count: int = 0
    fuzzy_merged_count: int = 0
    enriched_by_origin: dict[str, int] = field(default_factory=dict)
    new_by_origin: dict[str, int] = field(default_factory=dict)
    samples: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _evidence(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    matches: list[str] = []
    conflicts: list[str] = []
    for name in ("deadline", "cohort", "location", "recruitment_stage"):
        a, b = _text(left.get(name)), _text(right.get(name))
        if not a or not b:
            continue
        if a == b:
            matches.append(name)
        else:
            conflicts.append(name)
    return matches, conflicts


def _append_unique(bucket: list[Any], value: object) -> None:
    if _has(value) and all(str(item) != str(value) for item in bucket):
        bucket.append(copy.deepcopy(value))


def merge_qiuzhao_rows(primary: Mapping[str, Any], secondary: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Keep the existing row primary, fill blanks, and retain every conflict."""
    merged = copy.deepcopy(dict(primary))
    extra = dict(merged.get("extra") if isinstance(merged.get("extra"), Mapping) else {})
    conflicts = dict(extra.get("merge_conflicts") if isinstance(extra.get("merge_conflicts"), Mapping) else {})
    changed = False
    url_names = {"apply_url": "backup_apply_urls", "announcement_url": "backup_announcement_urls"}
    for name, value in secondary.items():
        if name == "extra":
            continue
        if not _has(merged.get(name)) and _has(value):
            merged[name] = copy.deepcopy(value)
            changed = True
        elif _has(value) and str(merged.get(name)) != str(value):
            if name in url_names:
                bucket = list(extra.get(url_names[name]) or [])
                _append_unique(bucket, value)
                extra[url_names[name]] = bucket
            else:
                bucket = list(conflicts.get(name) or [])
                _append_unique(bucket, value)
                conflicts[name] = bucket
    secondary_extra = secondary.get("extra") if isinstance(secondary.get("extra"), Mapping) else {}
    for name, value in secondary_extra.items():
        if not _has(extra.get(name)) and _has(value):
            extra[name] = copy.deepcopy(value)
            changed = True
        elif _has(value) and str(extra.get(name)) != str(value):
            bucket = list(conflicts.get(f"extra.{name}") or [])
            _append_unique(bucket, value)
            conflicts[f"extra.{name}"] = bucket
    sources = list(extra.get("merged_sources") or [])
    for row in (primary, secondary):
        source = {
            "origin": _origin(row), "source_record_id": _text(row.get("source_record_id")),
            "company": _text(row.get("company_name")), "position": _text(row.get("position")),
            "apply_url": _text(row.get("apply_url")), "announcement_url": _text(row.get("announcement_url")),
        }
        if source not in sources:
            sources.append(source)
    extra["merged_sources"] = sources
    extra["merged_origins"] = list(dict.fromkeys(source["origin"] for source in sources))
    if conflicts:
        extra["merge_conflicts"] = conflicts
    merged["extra"] = extra
    return merged, changed


def _candidate_buckets(rows: list[dict[str, Any]]) -> dict[str, list[int]]:
    buckets: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        company = normalize_company(row.get("company_name"))
        if company:
            buckets[company].append(index)
    return buckets


def deduplicate_qiuzhao_items(
    items: Iterable[Mapping[str, Any]], *, new_origin: str = "shasha_feishu",
) -> tuple[list[dict[str, Any]], QiuzhaoDedupReport]:
    rows = [copy.deepcopy(dict(row)) for row in items]
    report = QiuzhaoDedupReport(input_count=len(rows))
    output: list[dict[str, Any]] = []
    exact: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (normalize_company(row.get("company_name")), normalize_position(row.get("position")))
        if key[0] and key[1] and key in exact:
            index = exact[key]
            output[index], changed = merge_qiuzhao_rows(output[index], row)
            report.exact_merged_count += 1
            origin = _origin(row)
            report.enriched_by_origin[origin] = report.enriched_by_origin.get(origin, 0) + 1
            if len(report.samples) < 10:
                report.samples.append({"kind": "exact", "company": row.get("company_name"), "position": row.get("position"), "filled_missing_fields": changed})
            continue
        exact[key] = len(output)
        output.append(row)

    # Reuse the proven Gongkao title-similarity implementation for positions.
    # Company buckets keep the pass near-linear on 20k+ rows.
    removed: set[int] = set()
    for candidates in _candidate_buckets(output).values():
        for offset, left_index in enumerate(candidates):
            if left_index in removed:
                continue
            for right_index in candidates[offset + 1:]:
                if right_index in removed or _origin(output[left_index]) == _origin(output[right_index]):
                    continue
                left_position = output[left_index].get("position")
                right_position = output[right_index].get("position")
                similarity = _position_similarity(output[left_index], output[right_index])
                if similarity < 0.7:
                    continue
                matches, conflicts = _evidence(output[left_index], output[right_index])
                if similarity < 0.9 and (not matches or conflicts):
                    continue
                secondary = output[right_index]
                output[left_index], changed = merge_qiuzhao_rows(output[left_index], secondary)
                removed.add(right_index)
                report.fuzzy_merged_count += 1
                origin = _origin(secondary)
                report.enriched_by_origin[origin] = report.enriched_by_origin.get(origin, 0) + 1
                if len(report.samples) < 10:
                    report.samples.append({"kind": "fuzzy", "company": secondary.get("company_name"), "positions": [output[left_index].get("position"), secondary.get("position")], "similarity": round(similarity, 4), "evidence": matches, "filled_missing_fields": changed})
    final = [row for index, row in enumerate(output) if index not in removed]
    # Report standalone rows for every source so a new source does not need a
    # bespoke second dedup pass just to obtain intake statistics.
    for row in final:
        origin = _origin(row)
        report.new_by_origin[origin] = report.new_by_origin.get(origin, 0) + 1
    for origin in {_origin(row) for row in rows}:
        report.new_by_origin.setdefault(origin, 0)
        report.enriched_by_origin.setdefault(origin, 0)
    report.new_by_origin.setdefault(new_origin, 0)
    report.output_count = len(final)
    return final, report


__all__ = [
    "QiuzhaoDedupReport", "deduplicate_qiuzhao_items", "merge_qiuzhao_rows",
    "normalize_company", "normalize_position",
]
