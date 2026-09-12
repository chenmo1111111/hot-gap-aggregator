"""Synchronize simplified public Feishu Bitable tables.

Stable business keys are derived from announcement URLs for Gongkao and from
company plus position for Qiuzhao. A hidden ``来源`` field protects rows marked
``手动`` while allowing automatic retention cleanup.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import time
from datetime import datetime
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from app.sync_feishu import (
    BATCH_SLEEP_SECONDS,
    BATCH_SIZE,
    CHINA_TZ,
    FeishuAPIError,
    FeishuClient,
    _bool_value,
    _cell_text,
    _coalesce,
    _comparable,
    _link,
    _load_items,
    _recruit_count,
    date_to_millis,
    actionable_apply_url,
    merge_qiuzhao_rows,
    ensure_gongkao_review_view,
    normalize,
    normalize_company_type,
    normalize_exam_type,
    partition_gongkao_rows,
)
from app.pipeline.gongkao_classify import detail_category
from app.pipeline.gongkao_enrich import calculate_signup_status
from app.pipeline.prune import filter_current_public_gongkao, load_retention


LOGGER = logging.getLogger(__name__)

TEXT = 1
SINGLE_SELECT = 3
DATE = 5
CHECKBOX = 7
URL = 15

GONGKAO_SCHEMA: tuple[dict[str, Any], ...] = (
    # Feishu requires the text primary field to stay first.  Public views can
    # visually place 首次收录 before it; DEPLOY_FEISHU_SYNC.md documents that step.
    {"field_name": "公告标题", "type": TEXT},
    {"field_name": "首次收录", "type": DATE, "property": {"date_formatter": "yyyy-MM-dd"}},
    {
        "field_name": "类别",
        "type": SINGLE_SELECT,
        "property": {"options": [{"name": name} for name in (
            "国考", "省考", "事业单位", "选调生", "教师", "医疗", "三支一扶",
            "公安", "军队文职", "国企", "银行", "其他",
        )]},
    },
    {"field_name": "招聘人数", "type": TEXT},
    {"field_name": "最低学历", "type": TEXT},
    {"field_name": "报名开始", "type": DATE, "property": {"date_formatter": "yyyy-MM-dd"}},
    {"field_name": "报名截止", "type": DATE, "property": {"date_formatter": "yyyy-MM-dd"}},
    {
        "field_name": "报名状态", "type": SINGLE_SELECT,
        "property": {"options": [{"name": name} for name in (
            "未开始", "报名中", "剩1天", "剩2天", "剩3天", "剩4天", "剩5天", "已截止",
        )]},
    },
    {"field_name": "省份", "type": TEXT},
    {"field_name": "城市", "type": TEXT},
    {"field_name": "单位名称", "type": TEXT},
    {"field_name": "岗位性质", "type": TEXT},
    {"field_name": "限户籍", "type": TEXT},
    {"field_name": "限专业", "type": TEXT},
    {"field_name": "应届", "type": CHECKBOX},
    {"field_name": "服务期", "type": TEXT},
    {"field_name": "招录院校范围", "type": TEXT},
    {"field_name": "备注", "type": TEXT},
    {"field_name": "链接", "type": URL},
    {"field_name": "备用链接", "type": URL},
    {"field_name": "疑似重复", "type": CHECKBOX},
    {"field_name": "可能重复于", "type": TEXT},
    {"field_name": "同步ID", "type": TEXT},
    {
        "field_name": "来源", "type": SINGLE_SELECT,
        "property": {"options": [{"name": "自动"}, {"name": "手动"}]},
    },
)
GONGKAO_DEPRECATED_FIELDS = (
    "笔试科目", "本校可报", "日期", "截止日期", "距截止天数", "细分类别",
    "学历要求", "限应届",
)

QIUZHAO_SCHEMA: tuple[dict[str, Any], ...] = (
    {"field_name": "公司名称", "type": TEXT},
    {"field_name": "日期", "type": DATE, "property": {"date_formatter": "yyyy-MM-dd"}},
    {
        "field_name": "企业性质",
        "type": SINGLE_SELECT,
        "property": {"options": [{"name": name} for name in (
            "央企", "国企", "民企", "外企", "银行", "事业单位", "其他",
        )]},
    },
    {"field_name": "行业", "type": TEXT},
    {"field_name": "招聘岗位", "type": TEXT},
    {"field_name": "工作地点", "type": TEXT},
    {"field_name": "学历要求", "type": TEXT},
    {"field_name": "届次", "type": TEXT},
    {"field_name": "是否笔试", "type": CHECKBOX},
    {"field_name": "投递链接", "type": URL},
    {"field_name": "公告链接", "type": URL},
    {"field_name": "备注", "type": TEXT},
)

INSTRUCTIONS_SCHEMA: tuple[dict[str, Any], ...] = (
    {"field_name": "视图", "type": TEXT},
    {"field_name": "给谁看", "type": TEXT},
    {"field_name": "怎么用", "type": TEXT},
)
INSTRUCTIONS_ROWS: tuple[dict[str, str], ...] = (
    {
        "视图": "总说明", "给谁看": "所有人",
        "怎么用": "政府一手源每3小时采集并同步，工作日重点更新；粉笔只作结构化补充。数据用于辅助报考，一切以公告原文为准。",
    },
    {"视图": "更新频率", "给谁看": "所有人", "怎么用": "每3小时刷新一次；工作日晚间会再次同步当天新增公告。"},
    {"视图": "筛选省份", "给谁看": "按地区报考的人", "怎么用": "先筛省份，再筛城市；部分省级或全国公告的城市可能为空。"},
    {"视图": "专业与岗位表", "给谁看": "所有人", "怎么用": "限专业和最低学历是公告级摘要，最终资格必须以原文岗位表为准。"},
    {"视图": "首次收录", "给谁看": "找最新公告的人", "怎么用": "等于政府页面真实发布日期，不是脚本运行日期。"},
    {"视图": "报名状态", "给谁看": "所有人", "怎么用": "未开始=尚未开放；报名中=可报名；剩N天=1至5天截止；已截止=报名结束。"},
    {"视图": "链接", "给谁看": "准备报名的人", "怎么用": "优先直达政府或招录单位原文；报名入口与资格条件以原文为准。"},
    {"视图": "数据范围", "给谁看": "所有人", "怎么用": "覆盖公务员、事业单位、选调、三支一扶、公安和军队文职；企业校园招聘自动转入秋招表。"},
    {"视图": "免责声明", "给谁看": "所有人", "怎么用": "本表只用于信息聚合和辅助投递，不构成报考资格判断或录用承诺。"},
    {"视图": "空白字段", "给谁看": "所有人", "怎么用": "城市、人数或限制条件为空表示原文未明确或尚未完成提取，请直接查看公告及岗位表。"},
)


def _batches(rows: list[Any], size: int = BATCH_SIZE) -> Iterable[list[Any]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _has_value(value: object) -> bool:
    if value in (None, "", []):
        return False
    if isinstance(value, Mapping):
        return any(_has_value(item) for item in value.values())
    return True


def ensure_public_schema(
    client: FeishuClient,
    app_token: str,
    table_id: str,
    schema: tuple[dict[str, Any], ...],
    *,
    deprecated_fields: tuple[str, ...] = (),
) -> bool:
    """Ensure a display-only schema, adding fields without touching live data."""
    current = client.list_fields(app_token, table_id)
    expected_names = [str(field["field_name"]) for field in schema]
    by_name = {str(field.get("field_name") or ""): field for field in current}
    exact = all(
        name in by_name and int(by_name[name].get("type") or 0) == int(definition["type"])
        for name, definition in zip(expected_names, schema)
    ) and bool(by_name.get(expected_names[0], {}).get("is_primary"))
    deprecated_present = [name for name in deprecated_fields if name in by_name]
    if exact and not deprecated_present:
        return False

    records = client.list_records(app_token, table_id)
    has_data = any(
        any(_has_value(value) for value in (record.get("fields") or {}).values())
        for record in records
    )

    if has_data:
        primary = next((field for field in current if field.get("is_primary")), None)
        if not primary or str(primary.get("field_name") or "") != expected_names[0]:
            raise FeishuAPIError(f"公开表 {table_id} 主字段不符合预期，拒绝自动修改")
        for definition in schema:
            name = str(definition["field_name"])
            present = by_name.get(name)
            if present and int(present.get("type") or 0) != int(definition["type"]):
                raise FeishuAPIError(f"公开表字段 {name} 类型不符合预期，拒绝自动修改")
        missing = [definition for definition in schema if definition["field_name"] not in by_name]
        for definition in missing:
            client.create_field(app_token, table_id, definition)
        for name in deprecated_present:
            field_id = str(by_name[name].get("field_id") or "")
            if field_id and not by_name[name].get("is_primary"):
                client.delete_field(app_token, table_id, field_id)
        return bool(missing or deprecated_present)

    primary = next((field for field in current if field.get("is_primary")), None)
    if not primary:
        raise FeishuAPIError(f"公开表 {table_id} 找不到主字段")
    primary_id = str(primary.get("field_id") or "")
    client.update_field(app_token, table_id, primary_id, schema[0])

    # Remove only the unused fields of this confirmed-empty presentation table.
    for field in current:
        field_id = str(field.get("field_id") or "")
        if field_id and field_id != primary_id:
            client.delete_field(app_token, table_id, field_id)
    for definition in schema[1:]:
        client.create_field(app_token, table_id, definition)

    if records:
        batches = list(_batches([str(record["record_id"]) for record in records]))
        for index, batch in enumerate(batches):
            client.batch_delete(app_token, table_id, batch)
            if index < len(batches) - 1:
                time.sleep(BATCH_SLEEP_SECONDS)
    return True


def map_public_gongkao(row: Mapping[str, Any]) -> dict[str, Any]:
    extra = row.get("extra") if isinstance(row.get("extra"), Mapping) else {}
    url = _coalesce(row, "url|announcement_url|公告链接")
    title = _coalesce(row, "title_zh|title|招录单位·公告")
    link = _link(url, "查看公告")
    if not title or not link:
        raise ValueError("公考公开记录缺少公告标题或链接")
    start = _coalesce(row, "extra.startSignUpTime|startSignUpTime|报名开始")
    end = _coalesce(row, "extra.endSignUpTime|endSignUpTime|报名截止|截止日期")
    status, days_left = calculate_signup_status(
        start, end
    )
    status = str(extra.get("signup_status") or status or "").strip() or None
    days_left = extra.get("days_left") if extra.get("days_left") is not None else days_left
    extracted = str(extra.get("enrichment_status") or "") == "ok"
    limited_huji = _bool_value(extra.get("xian_huji"))
    limited_major = _bool_value(extra.get("xian_zhuanye"))
    category = str(extra.get("detail_category") or detail_category(row))
    raw_province = str(extra.get("province") or row.get("province") or "全国").strip()
    if raw_province in {"详见正文", "见正文", "待定", "未知", "/", "-"}:
        raw_province = "全国"
    province = re.sub(
        r"(?:壮族|回族|维吾尔)?自治区$|特别行政区$|省$|市$", "", raw_province,
    ) or "全国"
    identifier = str(extra.get("id") or row.get("id") or "").strip()
    if not identifier:
        identifier = "url:" + hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:24]
    fields = {
        "公告标题": str(title).strip(),
        "首次收录": date_to_millis(extra.get("first_seen")),
        "类别": normalize_exam_type(_coalesce(row, "extra.exam_type|exam_type|类别")),
        "招聘人数": _recruit_count(row) or "/",
        "最低学历": _coalesce(row, "extra.xueli|extra.education|education|学历要求") or "/",
        "报名开始": date_to_millis(start),
        "报名截止": date_to_millis(end),
        "报名状态": status,
        "省份": province,
        # Feishu drops an empty text cell on read-back.  Writing ``""`` here
        # therefore made every city-less row look changed on every sync.
        "城市": _coalesce(row, "extra.city|city|城市") or "/",
        "单位名称": _coalesce(row, "extra.unit|unit|company|单位名称") or "/",
        "岗位性质": _coalesce(row, "extra.position_nature|position_nature|岗位性质") or "/",
        "限户籍": (
            (extra.get("huji_shuoming") or "是") if limited_huji else ("不限" if extracted else "/")
        ),
        "限专业": (
            (extra.get("zhuanye_shuoming") or "是") if limited_major else ("不限" if extracted else "/")
        ),
        "应届": _bool_value(extra.get("xian_yingjie")),
        "服务期": extra.get("fuwu_qi") or "/",
        "招录院校范围": (
            str(extra.get("xuandiao_school_scope") or "名单见公告")
            if category == "选调生" and extracted else "/"
        ),
        "备注": _coalesce(row, "extra.bei_zhu|extra.notes|notes|备注") or "/",
        "链接": link,
        "备用链接": _link(
            next(
                (
                    str(value).strip() for value in extra.get("backup_urls", [])
                    if str(value).strip().startswith(("http://", "https://"))
                ),
                "",
            ) if isinstance(extra.get("backup_urls"), list) else "",
            "备用公告",
        ),
        "疑似重复": bool(extra.get("dup_suspect")),
        "可能重复于": str(extra.get("possible_duplicate_of") or "").strip() or "/",
        "同步ID": identifier,
        "来源": "自动",
    }
    return fields


def map_public_qiuzhao(row: Mapping[str, Any]) -> dict[str, Any]:
    company = _coalesce(row, "company_name|company|extra.company|公司名称")
    position = _coalesce(row, "position|job|job_name|title_zh|title|招聘岗位")
    if not normalize(company) or not normalize(position):
        raise ValueError("秋招公开记录缺少公司名称或招聘岗位")
    return {
        "公司名称": str(company).strip(),
        "日期": date_to_millis(_coalesce(row, "updated_at|date|日期")),
        "企业性质": normalize_company_type(
            _coalesce(row, "company_type|enterprise_type|extra.company_type|企业性质")
        ),
        "行业": _coalesce(row, "industry|extra.industry|行业") or "/",
        "招聘岗位": str(position).strip(),
        "工作地点": _coalesce(row, "location|work_location|city|extra.city|工作地点") or "/",
        "学历要求": _coalesce(row, "education|extra.education|学历要求") or "/",
        "届次": _coalesce(row, "cohort|graduation_year|extra.cohort|届次") or "/",
        "是否笔试": _bool_value(
            _coalesce(row, "written_test|has_written_test|extra.written_test|是否笔试")
        ),
        "投递链接": _link(
            actionable_apply_url(
                _coalesce(row, "apply_url|application_url|extra.apply_url|投递链接")
            ),
            "立即投递",
        ),
        "公告链接": _link(
            _coalesce(row, "announcement_url|source_url|extra.announcement_url|url|公告链接"),
            "查看公告",
        ),
        "备注": _coalesce(row, "notes|extra.notes|备注") or "/",
    }


def _link_url(value: object) -> str:
    if isinstance(value, Mapping):
        return str(value.get("link") or "").strip().rstrip("/")
    if isinstance(value, list):
        return next((_link_url(item) for item in value if _link_url(item)), "")
    text = str(value or "").strip()
    return text.rstrip("/") if text.startswith(("http://", "https://")) else ""


def gongkao_key(fields: Mapping[str, Any]) -> str:
    url = _link_url(fields.get("链接"))
    return f"url:{url.casefold()}" if url else ""


def qiuzhao_key(fields: Mapping[str, Any]) -> str:
    company = normalize(_cell_text(fields.get("公司名称")))
    position = normalize(_cell_text(fields.get("招聘岗位")))
    return f"job:{company}|{position}" if company and position else ""


def _public_comparable(value: object) -> object:
    url = _link_url(value)
    return ("url", url) if url else _comparable(value)


def diff_public_records(
    source_fields: Iterable[dict[str, Any]],
    existing_records: Iterable[dict[str, Any]],
    key_fn: Callable[[Mapping[str, Any]], str],
    preserve_missing: Callable[[Mapping[str, Any]], bool] | None = None,
    force_delete_keys: set[str] | None = None,
    source_field: str | None = None,
    known_auto_keys: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    source_by_key: dict[str, dict[str, Any]] = {}
    for fields in source_fields:
        key = key_fn(fields)
        if key:
            source_by_key[key] = fields

    existing_groups: dict[str, list[dict[str, Any]]] = {}
    deletes: list[str] = []
    for record in existing_records:
        fields = record.get("fields") if isinstance(record.get("fields"), Mapping) else {}
        key = key_fn(fields)
        record_id = str(record.get("record_id") or "")
        manual = bool(source_field and _cell_text(fields.get(source_field)).strip() == "手动")
        if not key:
            if record_id and not manual:
                deletes.append(record_id)
            continue
        existing_groups.setdefault(key, []).append(record)

    existing_by_key: dict[str, dict[str, Any]] = {}
    for key, records in existing_groups.items():
        manual_rows = [
            record for record in records
            if source_field
            and _cell_text((record.get("fields") or {}).get(source_field)).strip() == "手动"
        ]
        keep = manual_rows[0] if manual_rows else records[0]
        existing_by_key[key] = keep
        for duplicate in records:
            if duplicate is keep:
                continue
            duplicate_fields = duplicate.get("fields") or {}
            duplicate_manual = bool(
                source_field and _cell_text(duplicate_fields.get(source_field)).strip() == "手动"
            )
            if not duplicate_manual and duplicate.get("record_id"):
                deletes.append(str(duplicate["record_id"]))

    creates: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    for key, fields in source_by_key.items():
        old = existing_by_key.get(key)
        if old is None:
            creates.append(fields)
            continue
        old_fields = old.get("fields") or {}
        if source_field and _cell_text(old_fields.get(source_field)).strip() == "手动":
            continue
        if any(
            _public_comparable(old_fields.get(name)) != _public_comparable(value)
            for name, value in fields.items()
        ):
            updates.append({"record_id": old["record_id"], "fields": fields})

    for key, record in existing_by_key.items():
        if key in source_by_key:
            continue
        fields = record.get("fields") or {}
        source_value = _cell_text(fields.get(source_field)).strip() if source_field else ""
        if source_value == "手动":
            continue
        forced = key in (force_delete_keys or set())
        if source_field:
            should_delete = forced or source_value == "自动" or key in (known_auto_keys or set())
        else:
            should_delete = forced or not (preserve_missing and preserve_missing(fields))
        if should_delete and record.get("record_id"):
            deletes.append(str(record["record_id"]))
    return creates, updates, deletes


def slash_public_updates(
    source_fields: Iterable[dict[str, Any]],
    existing_records: Iterable[dict[str, Any]],
    key_fn: Callable[[Mapping[str, Any]], str],
    source_field: str | None = None,
) -> list[dict[str, Any]]:
    source_by_key = {key_fn(fields): fields for fields in source_fields if key_fn(fields)}
    updates: list[dict[str, Any]] = []
    for record in existing_records:
        old = record.get("fields") or {}
        if source_field and _cell_text(old.get(source_field)).strip() == "手动":
            continue
        desired = source_by_key.get(key_fn(old))
        if desired and any(
            value == "/" and _cell_text(old.get(name)).strip() == ""
            for name, value in desired.items()
        ):
            updates.append({"record_id": record["record_id"], "fields": desired})
    return updates


def sync_public_table(
    client: FeishuClient,
    app_token: str,
    table_id: str,
    rows: list[Mapping[str, Any]],
    mapper: Callable[[Mapping[str, Any]], dict[str, Any]],
    key_fn: Callable[[Mapping[str, Any]], str],
    preserve_missing: Callable[[Mapping[str, Any]], bool] | None = None,
    force_delete_keys: set[str] | None = None,
    source_field: str | None = None,
    known_auto_keys: set[str] | None = None,
) -> dict[str, int]:
    mapped: list[dict[str, Any]] = []
    skipped = 0
    for index, row in enumerate(rows, 1):
        try:
            mapped.append(mapper(row))
        except (TypeError, ValueError) as exc:
            skipped += 1
            LOGGER.warning("skip invalid public source row %d: %s", index, exc)
    existing = client.list_records(app_token, table_id)
    creates, updates, deletes = diff_public_records(
        mapped, existing, key_fn, preserve_missing=preserve_missing,
        force_delete_keys=force_delete_keys, source_field=source_field,
        known_auto_keys=known_auto_keys,
    )
    update_ids = {str(record["record_id"]) for record in updates}
    updates.extend(
        record for record in slash_public_updates(mapped, existing, key_fn, source_field)
        if str(record["record_id"]) not in update_ids
    )
    operations = [
        *((client.batch_create, batch) for batch in _batches(creates)),
        *((client.batch_update, batch) for batch in _batches(updates)),
        *((client.batch_delete, batch) for batch in _batches(deletes)),
    ]
    for index, (operation, batch) in enumerate(operations):
        operation(app_token, table_id, batch)
        if index < len(operations) - 1:
            time.sleep(BATCH_SLEEP_SECONDS)
    return {
        "source": len(rows),
        "created": len(creates),
        "updated": len(updates),
        "deleted": len(deletes),
        "skipped": skipped,
    }


def load_public_config(path: str | Path) -> dict[str, Any]:
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError("public sync config must be a YAML mapping")
    for name in ("app_token", "gongkao_table_id", "qiuzhao_table_id"):
        if not str(value.get(name) or "").strip():
            raise ValueError(f"public sync config is missing {name}")
    return value


def instruction_key(fields: Mapping[str, Any]) -> str:
    value = normalize(_cell_text(fields.get("视图")))
    return f"view:{value}" if value else ""


def ensure_instructions_table(
    client: FeishuClient, app_token: str, *, table_name: str = "使用说明"
) -> tuple[str, dict[str, int]]:
    table = next(
        (item for item in client.list_tables(app_token) if str(item.get("name") or "") == table_name),
        None,
    )
    created = table is None
    if table is None:
        table = client.create_table(app_token, table_name)
    table_id = str(table.get("table_id") or "")
    if not table_id:
        raise FeishuAPIError(f"找不到数据表 {table_name} 的 table_id")
    initialized = ensure_public_schema(client, app_token, table_id, INSTRUCTIONS_SCHEMA)
    result = sync_public_table(
        client,
        app_token,
        table_id,
        list(INSTRUCTIONS_ROWS),
        lambda row: dict(row),
        instruction_key,
        preserve_missing=lambda _fields: True,
    )
    result["table_created"] = int(created)
    result["schema_initialized"] = int(initialized)
    return table_id, result


def _gongkao_force_delete_keys(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for row in rows:
        url = _link_url(_coalesce(row, "url|announcement_url|extra.announcement_url|链接"))
        if url:
            keys.add(f"url:{url.casefold()}")
    return keys


def _replaced_gongkao_link_keys(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    """Return stale links superseded by an official watcher or Sheet URL."""
    keys: set[str] = set()
    for row in rows:
        extra = row.get("extra") if isinstance(row.get("extra"), Mapping) else {}
        replaced = [
            *(extra.get("replaced_urls") if isinstance(extra.get("replaced_urls"), list) else []),
            *(extra.get("backup_urls") if isinstance(extra.get("backup_urls"), list) else []),
        ]
        for value in replaced:
            url = _link_url(value)
            if url:
                keys.add(f"url:{url.casefold()}")
    return keys


def run(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Sync simplified public Feishu tables")
    parser.add_argument(
        "--config",
        default=os.getenv("FEISHU_PUBLIC_SYNC_CONFIG", "config/feishu_public_sync.yaml"),
    )
    parser.add_argument("--data-dir", default=os.getenv("SITE_DATA_DIR", "/var/www/hot-gap/data"))
    arguments = parser.parse_args(argv)

    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        LOGGER.error("FEISHU_APP_ID and FEISHU_APP_SECRET are required")
        return 2
    try:
        config = load_public_config(arguments.config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        LOGGER.error("cannot load public Feishu sync config: %s", exc)
        return 2

    data_dir = Path(arguments.data_dir)
    app_token = str(config["app_token"])
    sources = config.get("sources") if isinstance(config.get("sources"), Mapping) else {}
    gongkao_source = sources.get("gongkao") if isinstance(sources.get("gongkao"), Mapping) else {}
    qiuzhao_source = sources.get("qiuzhao") if isinstance(sources.get("qiuzhao"), Mapping) else {}
    jobs = (
        (
            "gongkao_public",
            str(config["gongkao_table_id"]),
            GONGKAO_SCHEMA,
            str(gongkao_source.get("file") or "gongkao_enriched.json"),
            map_public_gongkao,
            gongkao_key,
            lambda fields: _cell_text(fields.get("报名状态")).strip() == "已截止",
            GONGKAO_DEPRECATED_FIELDS,
        ),
        (
            "qiuzhao_public",
            str(config["qiuzhao_table_id"]),
            QIUZHAO_SCHEMA,
            str(qiuzhao_source.get("file") or "qiuzhao.json"),
            map_public_qiuzhao,
            qiuzhao_key,
            None,
            (),
        ),
    )
    failed = False
    with FeishuClient(app_id, app_secret) as client:
        for name, table_id, schema, filename, mapper, key_fn, preserve_missing, deprecated_fields in jobs:
            try:
                initialized = ensure_public_schema(
                    client, app_token, table_id, schema,
                    deprecated_fields=deprecated_fields,
                )
                if name == "gongkao_public":
                    created_view = ensure_gongkao_review_view(client, app_token, table_id)
                    if created_view:
                        LOGGER.info("public gongkao suspect review view created")
                rows = _load_items(data_dir / filename)
                force_delete_keys: set[str] | None = None
                source_field: str | None = None
                known_auto_keys: set[str] | None = None
                if name == "gongkao_public":
                    rows, routed_rows, excluded_rows = partition_gongkao_rows(rows)
                    all_current_rows = list(rows)
                    known_auto_keys = set()
                    for row in all_current_rows:
                        try:
                            key = gongkao_key(map_public_gongkao(row))
                        except (TypeError, ValueError):
                            continue
                        if key:
                            known_auto_keys.add(key)
                    rows, expired_count = filter_current_public_gongkao(
                        rows, load_retention(), today=datetime.now(CHINA_TZ).date(),
                    )
                    source_field = "来源"
                    force_delete_keys = _gongkao_force_delete_keys((*routed_rows, *excluded_rows))
                    force_delete_keys.update(_replaced_gongkao_link_keys(rows))
                    LOGGER.info(
                        "public gongkao routing: kept=%d routed_to_qiuzhao=%d excluded_noise=%d",
                        len(rows), len(routed_rows), len(excluded_rows),
                    )
                    LOGGER.info(
                        "public gongkao retention: input=%d kept=%d expired=%d",
                        len(all_current_rows), len(rows), expired_count,
                    )
                else:
                    gongkao_filename = str(gongkao_source.get("file") or "gongkao_enriched.json")
                    try:
                        raw_gongkao = _load_items(data_dir / gongkao_filename)
                    except Exception as exc:
                        LOGGER.warning("cannot load public Gongkao routes for Qiuzhao sync: %s", exc)
                    else:
                        _, routed_rows, _ = partition_gongkao_rows(raw_gongkao)
                        rows = merge_qiuzhao_rows(rows, routed_rows)
                result = sync_public_table(
                    client, app_token, table_id, rows, mapper, key_fn,
                    preserve_missing=preserve_missing,
                    force_delete_keys=force_delete_keys,
                    source_field=source_field,
                    known_auto_keys=known_auto_keys,
                )
                result["schema_initialized"] = int(initialized)
                LOGGER.info("%s sync complete: %s", name, result)
            except Exception:
                failed = True
                LOGGER.exception("%s sync failed", name)
        try:
            instructions_table_id, result = ensure_instructions_table(
                client,
                app_token,
                table_name=str(config.get("instructions_table_name") or "使用说明"),
            )
            LOGGER.info(
                "instructions sync complete: table_id=%s result=%s",
                instructions_table_id,
                result,
            )
        except Exception:
            failed = True
            LOGGER.exception("instructions sync failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
