"""Synchronize simplified, read-only public Feishu Bitable tables.

The public tables intentionally contain no technical sync-ID/source fields.
Stable business keys are derived from announcement URLs for Gongkao and from
company plus position for Qiuzhao.  Schema initialization is allowed only
while a table has no non-empty records.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
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
    merge_qiuzhao_rows,
    normalize,
    normalize_company_type,
    normalize_exam_type,
    partition_gongkao_rows,
)
from app.pipeline.gongkao_classify import detail_category
from app.pipeline.gongkao_enrich import calculate_signup_status


LOGGER = logging.getLogger(__name__)

TEXT = 1
SINGLE_SELECT = 3
DATE = 5
CHECKBOX = 7
URL = 15

GONGKAO_SCHEMA: tuple[dict[str, Any], ...] = (
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
    {"field_name": "截止日期", "type": DATE, "property": {"date_formatter": "yyyy-MM-dd"}},
    {"field_name": "省份", "type": TEXT},
    {"field_name": "链接", "type": URL},
    {
        "field_name": "报名状态", "type": SINGLE_SELECT,
        "property": {"options": [{"name": name} for name in (
            "未开始", "报名中", "剩1天", "剩2天", "剩3天", "剩4天", "剩5天", "已截止",
        )]},
    },
    {"field_name": "距截止天数", "type": 2, "property": {"formatter": "0"}},
    {
        "field_name": "细分类别", "type": SINGLE_SELECT,
        "property": {"options": [{"name": name} for name in (
            "国企", "央企", "事业单位", "银行", "教师", "医疗", "公务员", "选调生",
            "三支一扶", "公安警察", "军队文职", "其它",
        )]},
    },
    {"field_name": "限户籍", "type": TEXT},
    {"field_name": "限专业", "type": TEXT},
    {"field_name": "学历要求", "type": TEXT},
    {"field_name": "限应届", "type": CHECKBOX},
    {"field_name": "服务期", "type": TEXT},
    {"field_name": "招录院校范围", "type": TEXT},
    {"field_name": "备注", "type": TEXT},
)
GONGKAO_DEPRECATED_FIELDS = ("笔试科目", "本校可报", "日期")

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
        "怎么用": "数据每天05:00代码自动同步粉笔全国全量+组织部选调，已滤掉企业校招、教师引进、博士后噪音。",
    },
    {"视图": "全部信息", "给谁看": "所有人", "怎么用": "查看全部考公考编机会，按首次收录降序浏览最新公告。"},
    {"视图": "今日必做", "给谁看": "所有人", "怎么用": "报名中且5天内截止的，每天先看这个。"},
    {"视图": "进行中", "给谁看": "正在报名的人", "怎么用": "只看尚未截止的机会，再按地区和学历筛选。"},
    {"视图": "本周截止", "给谁看": "容易错过截止时间的人", "怎么用": "集中处理未来7天内截止的报名。"},
    {"视图": "选调·本校可报", "给谁看": "研究生", "怎么用": "看「招录院校范围」并对照自己的学校，最终以公告原文为准。"},
    {"视图": "国企央企", "给谁看": "想进国企的人", "怎么用": "查看国企央企社会招聘；企业校园招聘已自动转入秋招表。"},
    {"视图": "事业单位", "给谁看": "备考事业编的人", "怎么用": "集中查看事业单位公告，优先核对学历、户籍和截止日期。"},
    {"视图": "银行", "给谁看": "想进银行的人", "怎么用": "查看银行社会招聘；银行校园招聘已自动转入秋招表。"},
    {"视图": "已结束", "给谁看": "需要复盘的人", "怎么用": "查看已截止公告，作为考情和往年时间参考。"},
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
    end = _coalesce(row, "extra.endSignUpTime|endSignUpTime|报名截止|截止日期")
    status, days_left = calculate_signup_status(
        _coalesce(row, "extra.startSignUpTime|startSignUpTime|报名开始"), end
    )
    status = str(extra.get("signup_status") or status or "").strip() or None
    days_left = extra.get("days_left") if extra.get("days_left") is not None else days_left
    extracted = str(extra.get("enrichment_status") or "") == "ok"
    limited_huji = _bool_value(extra.get("xian_huji"))
    limited_major = _bool_value(extra.get("xian_zhuanye"))
    category = str(extra.get("detail_category") or detail_category(row))
    fields = {
        "公告标题": str(title).strip(),
        "首次收录": date_to_millis(extra.get("first_seen")),
        "类别": normalize_exam_type(_coalesce(row, "extra.exam_type|exam_type|类别")),
        "招聘人数": _recruit_count(row) or "/",
        "截止日期": date_to_millis(end),
        "省份": str(extra.get("province") or row.get("province") or "全国").strip(),
        "链接": link,
        "报名状态": status,
        "距截止天数": days_left,
        "细分类别": category,
        "限户籍": (
            (extra.get("huji_shuoming") or "是") if limited_huji else ("不限" if extracted else "/")
        ),
        "限专业": (
            (extra.get("zhuanye_shuoming") or "是") if limited_major else ("不限" if extracted else "/")
        ),
        "学历要求": extra.get("xueli") or "/",
        "限应届": _bool_value(extra.get("xian_yingjie")),
        "服务期": extra.get("fuwu_qi") or "/",
        "招录院校范围": (
            str(extra.get("xuandiao_school_scope") or "名单见公告")
            if category == "选调生" and extracted else "/"
        ),
        "备注": _coalesce(row, "extra.notes|notes|备注") or "/",
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
            _coalesce(row, "apply_url|application_url|extra.apply_url|url|投递链接"),
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    source_by_key: dict[str, dict[str, Any]] = {}
    for fields in source_fields:
        key = key_fn(fields)
        if key:
            source_by_key[key] = fields

    existing_by_key: dict[str, dict[str, Any]] = {}
    deletes: list[str] = []
    for record in existing_records:
        fields = record.get("fields") if isinstance(record.get("fields"), Mapping) else {}
        key = key_fn(fields)
        record_id = str(record.get("record_id") or "")
        if not key or key in existing_by_key:
            if record_id:
                deletes.append(record_id)
            continue
        existing_by_key[key] = record

    creates: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    for key, fields in source_by_key.items():
        old = existing_by_key.get(key)
        if old is None:
            creates.append(fields)
            continue
        old_fields = old.get("fields") or {}
        if any(
            _public_comparable(old_fields.get(name)) != _public_comparable(value)
            for name, value in fields.items()
        ):
            updates.append({"record_id": old["record_id"], "fields": fields})

    deletes.extend(
        str(record["record_id"])
        for key, record in existing_by_key.items()
        if key not in source_by_key
        and (
            key in (force_delete_keys or set())
            or not (preserve_missing and preserve_missing(record.get("fields") or {}))
        )
    )
    return creates, updates, deletes


def slash_public_updates(
    source_fields: Iterable[dict[str, Any]],
    existing_records: Iterable[dict[str, Any]],
    key_fn: Callable[[Mapping[str, Any]], str],
) -> list[dict[str, Any]]:
    source_by_key = {key_fn(fields): fields for fields in source_fields if key_fn(fields)}
    updates: list[dict[str, Any]] = []
    for record in existing_records:
        old = record.get("fields") or {}
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
        force_delete_keys=force_delete_keys,
    )
    update_ids = {str(record["record_id"]) for record in updates}
    updates.extend(
        record for record in slash_public_updates(mapped, existing, key_fn)
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
        table = client.create_table(app_token, table_name, default_view_name="使用说明")
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
                rows = _load_items(data_dir / filename)
                force_delete_keys: set[str] | None = None
                if name == "gongkao_public":
                    rows, routed_rows, excluded_rows = partition_gongkao_rows(rows)
                    force_delete_keys = _gongkao_force_delete_keys((*routed_rows, *excluded_rows))
                    LOGGER.info(
                        "public gongkao routing: kept=%d routed_to_qiuzhao=%d excluded_noise=%d",
                        len(rows), len(routed_rows), len(excluded_rows),
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
