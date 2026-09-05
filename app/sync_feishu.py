from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv


LOGGER = logging.getLogger(__name__)
FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
CHINA_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
BATCH_SIZE = 500
BATCH_SLEEP_SECONDS = 0.5

GONGKAO_EXAM_TYPES = (
    "国考", "省考", "事业单位", "选调生", "教师", "医疗", "三支一扶", "公安",
    "军队文职", "国企", "银行", "其他",
)
QIUZHAO_COMPANY_TYPES = ("央企", "国企", "民企", "外企", "银行", "事业单位", "其他")
PROVINCES = {
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海", "江苏",
    "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西", "海南",
    "重庆", "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆", "兵团",
    "香港", "澳门", "台湾",
}

DEFAULT_GONGKAO_MAPPING = {
    "$sync_id": "同步ID",
    "$sync_time": "更新时间",
    "$region": "地区",
    "$exam_type": "招录类型",
    "title_zh|title|招录单位·公告": "招录单位·公告",
    "$recruit_count": "招录人数",
    "$signup_start": "报名开始",
    "$signup_end": "报名截止",
    "$days_left": "距截止天数",
    "$written_exam": "笔试时间",
    "$signup_status": "报名状态",
    "$announcement_link": "公告链接",
    "$fresh_graduate": "应届可报",
    "$source": "来源",
    "extra.notes|notes|备注": "备注",
}

DEFAULT_QIUZHAO_MAPPING = {
    "$sync_id": "同步ID",
    "$sync_time": "更新时间",
    "company_name|company|extra.company|公司名称": "公司名称",
    "$company_type": "企业性质",
    "industry|extra.industry|行业": "行业",
    "position|job|job_name|title_zh|title|招聘岗位": "招聘岗位",
    "location|work_location|city|extra.city|工作地点": "工作地点",
    "education|extra.education|学历要求": "学历要求",
    "cohort|graduation_year|extra.cohort|届次": "届次",
    "$deadline": "网申截止",
    "$days_left": "距截止天数",
    "$written_test": "是否笔试",
    "$apply_link": "投递链接",
    "$announcement_link": "公告链接",
    "$source": "来源",
    "notes|extra.notes|备注": "备注",
}


class FeishuAPIError(RuntimeError):
    """A Feishu HTTP or application-level API failure."""


class FeishuClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        api_base: str = FEISHU_API_BASE,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.api_base = api_base.rstrip("/")
        self._client = httpx.Client(timeout=timeout, follow_redirects=True, transport=transport)
        self._token: str | None = None
        self._token_expires_at = 0.0

    def __enter__(self) -> FeishuClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def tenant_access_token(self, *, force: bool = False) -> str:
        now = time.time()
        if not force and self._token and now < self._token_expires_at - 300:
            return self._token
        response = self._client.post(
            f"{self.api_base}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise FeishuAPIError(f"token endpoint returned HTTP {response.status_code} with invalid JSON") from exc
        if response.status_code >= 400 or int(payload.get("code", -1)) != 0:
            raise FeishuAPIError(
                f"token request failed: HTTP {response.status_code}, "
                f"code={payload.get('code')}, msg={payload.get('msg')}"
            )
        token = str(payload.get("tenant_access_token") or "")
        if not token:
            raise FeishuAPIError("token response did not contain tenant_access_token")
        self._token = token
        self._token_expires_at = now + max(0, int(payload.get("expire", 7200)))
        return token

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        refreshed = False
        last_error: Exception | None = None
        for attempt in range(3):
            token = self.tenant_access_token()
            try:
                response = self._client.request(
                    method,
                    f"{self.api_base}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise FeishuAPIError(f"Feishu request failed after retries: {exc}") from exc

            if response.status_code == 401 and not refreshed:
                refreshed = True
                self._token = None
                continue
            try:
                payload = response.json()
            except ValueError as exc:
                last_error = exc
                if response.status_code >= 500 and attempt < 2:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise FeishuAPIError(
                    f"Feishu returned HTTP {response.status_code} with invalid JSON"
                ) from exc

            code = int(payload.get("code", 0))
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                last_error = FeishuAPIError(
                    f"transient Feishu error: HTTP {response.status_code}, code={code}"
                )
                time.sleep(0.5 * (2**attempt))
                continue
            if response.status_code >= 400 or code != 0:
                raise FeishuAPIError(
                    f"Feishu request failed: HTTP {response.status_code}, "
                    f"code={payload.get('code')}, msg={payload.get('msg')}"
                )
            return payload
        raise FeishuAPIError(f"Feishu request failed after retries: {last_error}")

    def list_records(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            payload = self._request(
                "GET",
                f"/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                params=params,
            )
            data = payload.get("data") or {}
            records.extend(row for row in data.get("items") or [] if isinstance(row, dict))
            if not data.get("has_more"):
                return records
            page_token = str(data.get("page_token") or "")
            if not page_token:
                raise FeishuAPIError("record pagination says has_more but has no page_token")

    def batch_create(self, app_token: str, table_id: str, fields: list[dict[str, Any]]) -> None:
        self._request(
            "POST",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
            json={"records": [{"fields": _without_none(row)} for row in fields]},
        )

    def batch_update(self, app_token: str, table_id: str, records: list[dict[str, Any]]) -> None:
        self._request(
            "POST",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update",
            json={"records": records},
        )

    def batch_delete(self, app_token: str, table_id: str, record_ids: list[str]) -> None:
        self._request(
            "POST",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete",
            json={"records": record_ids},
        )


def _without_none(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not None}


def normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in text if character.isalnum())


def _get_path(row: Mapping[str, Any], path: str) -> Any:
    current: Any = row
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _coalesce(row: Mapping[str, Any], expression: str) -> Any:
    for path in expression.split("|"):
        value = _get_path(row, path.strip())
        if value not in (None, "", []):
            return value
    return None


def date_to_millis(value: object, *, tz: tzinfo = CHINA_TZ) -> int | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            stamp = int(float(value))
            return stamp if abs(stamp) > 10_000_000_000 else stamp * 1000
        text = str(value).strip()
        chinese = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日?", text)
        if chinese:
            parsed = datetime(*(int(part) for part in chinese.groups()), tzinfo=tz)
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz)
        return int(parsed.timestamp() * 1000)
    except (OverflowError, OSError, TypeError, ValueError):
        return None


def _date_value(value: object, *, tz: tzinfo = CHINA_TZ) -> date | None:
    millis = date_to_millis(value, tz=tz)
    if millis is None:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000, tz).date()
    except (OverflowError, OSError, ValueError):
        return None


def _bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().casefold() in {
        "1", "true", "yes", "y", "是", "有", "需要", "应届", "可报",
    }


def _link(url: object, text: str) -> dict[str, str] | None:
    value = str(url or "").strip()
    return {"text": text, "link": value} if value.startswith(("http://", "https://")) else None


def _normalize_province(value: object) -> str:
    province = str(value or "").strip()
    for suffix in ("壮族自治区", "回族自治区", "维吾尔自治区", "自治区", "特别行政区", "省"):
        if province.endswith(suffix):
            province = province[: -len(suffix)]
            break
    if province in {"北京市", "天津市", "上海市", "重庆市"}:
        province = province[:-1]
    return province or "全国"


def _clean_city(value: object, province: str, *, require_suffix: bool = False) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace(province + "省", "").replace(province, "")
    tokens = [part.strip() for part in re.split(r"[·/、,，;；\s]+", text) if part.strip()]
    for token in tokens:
        if token in {"全国", "不限", province, f"{province}市"}:
            continue
        if re.fullmatch(r"20\d{2}", token):
            continue
        if any(marker in token for marker in (
            "考试", "招考", "招聘", "公告", "公务员", "事业单位", "教师", "医疗",
            "国企", "银行", "编", "联考", "统考", "选调", "三支一扶", "军队文职",
        )):
            continue
        suffix_found = False
        for suffix in ("自治州", "地区", "市", "盟", "区", "县"):
            if token.endswith(suffix) and len(token) > len(suffix):
                token = token[: -len(suffix)]
                suffix_found = True
                break
        if require_suffix and not suffix_found:
            continue
        if 1 < len(token) <= 12:
            return token
    return None


def split_region(row: Mapping[str, Any]) -> str:
    province = _normalize_province(
        _coalesce(row, "extra.province|province|省份|地区")
    )
    extra = row.get("extra") if isinstance(row.get("extra"), Mapping) else {}
    candidates: list[Any] = []
    for expression in (
        "extra.city", "extra.location", "city", "location", "地点", "工作地点",
    ):
        value = _get_path(row, expression)
        if value not in (None, ""):
            candidates.extend(value if isinstance(value, list) else [value])
    city_hits = extra.get("city_focus_hit") if isinstance(extra, Mapping) else None
    if isinstance(city_hits, list):
        candidates.extend(city_hits)
    for candidate in candidates:
        city = _clean_city(candidate, province)
        if city:
            return f"{province}·{city}"
    tags = extra.get("tags") if isinstance(extra, Mapping) else None
    active_province = province
    if isinstance(tags, list):
        for tag in tags:
            tag_province = _normalize_province(tag)
            if tag_province in PROVINCES:
                active_province = tag_province
                continue
            if active_province == province:
                city = _clean_city(tag, province, require_suffix=True)
                if city:
                    return f"{province}·{city}"
    return province


def normalize_exam_type(value: object) -> str:
    text = str(value or "").strip()
    aliases = (
        ("选调", "选调生"), ("国考", "国考"), ("国家公务员", "国考"),
        ("省考", "省考"), ("公务员", "省考"), ("事业", "事业单位"),
        ("教师", "教师"), ("高校", "教师"), ("医疗", "医疗"),
        ("卫生", "医疗"), ("三支一扶", "三支一扶"), ("公安", "公安"),
        ("招警", "公安"), ("军队文职", "军队文职"), ("国企", "国企"),
        ("银行", "银行"), ("农信", "银行"),
    )
    if text in GONGKAO_EXAM_TYPES:
        return text
    for marker, normalized in aliases:
        if marker in text:
            return normalized
    return "其他"


def normalize_company_type(value: object) -> str:
    text = str(value or "").strip()
    if text in QIUZHAO_COMPANY_TYPES:
        return text
    for marker, normalized in (
        ("央企", "央企"), ("中央企业", "央企"), ("国有", "国企"),
        ("国企", "国企"), ("民营", "民企"), ("民企", "民企"),
        ("外资", "外企"), ("外企", "外企"), ("银行", "银行"),
        ("事业", "事业单位"),
    ):
        if marker in text:
            return normalized
    return "其他"


def _days_left(deadline: object, today: date) -> int | None:
    end = _date_value(deadline)
    return max(0, (end - today).days) if end else None


def _signup_status(start: object, end: object, written: object, today: date) -> str | None:
    start_date, end_date, written_date = (
        _date_value(start), _date_value(end), _date_value(written)
    )
    if start_date and today < start_date:
        return "未开始"
    if end_date and today > end_date:
        return "待笔试" if written_date and today <= written_date else "已截止"
    if (not start_date or today >= start_date) and (not end_date or today <= end_date):
        return "报名中" if start_date or end_date else None
    return None


def _recruit_count(row: Mapping[str, Any]) -> str | None:
    explicit = _coalesce(
        row,
        "extra.recruit_count|extra.recruitment_count|extra.headcount|recruit_count|招录人数",
    )
    if explicit not in (None, ""):
        return str(explicit)
    summary = str(_coalesce(row, "summary_zh|summary") or "")
    match = re.search(r"(?:招考|招录|招聘)人数[：:]?\s*([0-9,，]+)", summary)
    return match.group(1).replace(",", "").replace("，", "") if match else None


def _apply_mapping(
    row: Mapping[str, Any],
    mapping: Mapping[str, str],
    derived: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        target: derived.get(source) if source.startswith("$") else _coalesce(row, source)
        for source, target in mapping.items()
    }


def map_gongkao(
    row: Mapping[str, Any],
    field_mapping: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(CHINA_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CHINA_TZ)
    today = current.astimezone(CHINA_TZ).date()
    start = _coalesce(row, "extra.startSignUpTime|startSignUpTime|报名开始")
    end = _coalesce(row, "extra.endSignUpTime|endSignUpTime|报名截止")
    written = _coalesce(row, "extra.startWriteTime|startWriteTime|笔试时间")
    url = _coalesce(row, "url|announcement_url|公告链接")
    signup_url = _coalesce(row, "extra.signup_url|extra.apply_url|signup_url|apply_url|报名入口")
    fresh = _coalesce(row, "extra.fresh_graduate|extra.graduate|fresh_graduate|应届可报")
    derived = {
        "$sync_id": str(_coalesce(row, "extra.id|id") or "").strip(),
        "$sync_time": int(current.timestamp() * 1000),
        "$region": split_region(row),
        "$exam_type": normalize_exam_type(_coalesce(row, "extra.exam_type|exam_type|招录类型")),
        "$recruit_count": _recruit_count(row),
        "$signup_start": date_to_millis(start),
        "$signup_end": date_to_millis(end),
        "$days_left": _days_left(end, today),
        "$written_exam": date_to_millis(written),
        "$signup_status": _signup_status(start, end, written, today),
        "$signup_link": _link(signup_url, "报名入口"),
        "$announcement_link": _link(url, "查看公告"),
        "$fresh_graduate": _bool_value(fresh),
        "$source": "自动",
    }
    fields = _apply_mapping(row, field_mapping or DEFAULT_GONGKAO_MAPPING, derived)
    if not fields.get((field_mapping or DEFAULT_GONGKAO_MAPPING).get("$sync_id", "同步ID")):
        raise ValueError("公考记录缺少 extra.id，无法生成同步ID")
    return fields


def map_qiuzhao(
    row: Mapping[str, Any],
    field_mapping: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(CHINA_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CHINA_TZ)
    today = current.astimezone(CHINA_TZ).date()
    company = _coalesce(row, "company_name|company|extra.company|公司名称")
    position = _coalesce(row, "position|job|job_name|title_zh|title|招聘岗位")
    deadline = _coalesce(
        row, "deadline|application_deadline|end_time|extra.deadline|网申截止"
    )
    apply_url = _coalesce(row, "apply_url|application_url|extra.apply_url|url|投递链接")
    announcement_url = _coalesce(
        row, "announcement_url|source_url|extra.announcement_url|url|公告链接"
    )
    written = _coalesce(row, "written_test|has_written_test|extra.written_test|是否笔试")
    sync_id = f"{normalize(company)}|{normalize(position)}"
    if sync_id == "|" or not normalize(company) or not normalize(position):
        raise ValueError("秋招记录缺少公司名称或招聘岗位，无法生成同步ID")
    derived = {
        "$sync_id": sync_id,
        "$sync_time": int(current.timestamp() * 1000),
        "$company_type": normalize_company_type(
            _coalesce(row, "company_type|enterprise_type|extra.company_type|企业性质")
        ),
        "$deadline": date_to_millis(deadline),
        "$days_left": _days_left(deadline, today),
        "$written_test": _bool_value(written),
        "$apply_link": _link(apply_url, "立即投递"),
        "$announcement_link": _link(announcement_url, "查看公告"),
        "$source": "自动",
    }
    return _apply_mapping(row, field_mapping or DEFAULT_QIUZHAO_MAPPING, derived)


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_cell_text(item) for item in value)
    if isinstance(value, Mapping):
        return str(value.get("text") or value.get("name") or value.get("value") or "")
    return str(value)


def _comparable(value: object) -> object:
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _comparable(item)) for key, item in value.items()))
    if isinstance(value, list):
        if all(isinstance(item, Mapping) and "text" in item for item in value):
            return "".join(str(item.get("text") or "") for item in value)
        return tuple(_comparable(item) for item in value)
    return value


def diff_records(
    source_records: Iterable[dict[str, Any]],
    existing_records: Iterable[dict[str, Any]],
    *,
    sync_id_field: str = "同步ID",
    source_field: str = "来源",
    updated_at_field: str = "更新时间",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    source_by_id: dict[str, dict[str, Any]] = {}
    for fields in source_records:
        sync_id = _cell_text(fields.get(sync_id_field)).strip()
        if not sync_id:
            raise ValueError(f"source record has no {sync_id_field}")
        source_by_id[sync_id] = fields

    auto_by_id: dict[str, dict[str, Any]] = {}
    automatic_records: list[dict[str, Any]] = []
    for record in existing_records:
        fields = record.get("fields") or {}
        if _cell_text(fields.get(source_field)).strip() != "自动":
            continue
        automatic_records.append(record)
        sync_id = _cell_text(fields.get(sync_id_field)).strip()
        if sync_id and sync_id not in auto_by_id:
            auto_by_id[sync_id] = record

    creates: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    for sync_id, fields in source_by_id.items():
        old = auto_by_id.get(sync_id)
        if old is None:
            creates.append(fields)
            continue
        old_fields = old.get("fields") or {}
        compared_names = set(fields) - {updated_at_field}
        changed = any(
            _comparable(old_fields.get(name)) != _comparable(fields.get(name))
            for name in compared_names
        )
        if changed:
            updates.append({"record_id": old["record_id"], "fields": fields})

    deletes = [
        str(record["record_id"])
        for record in automatic_records
        if _cell_text((record.get("fields") or {}).get(sync_id_field)).strip() not in source_by_id
    ]
    return creates, updates, deletes


def _batches(rows: list[Any], size: int = BATCH_SIZE) -> Iterable[list[Any]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def sync_table(
    client: FeishuClient,
    app_token: str,
    table_id: str,
    source_rows: list[Mapping[str, Any]],
    mapper: Any,
    field_mapping: Mapping[str, str],
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    mapped: list[dict[str, Any]] = []
    skipped = 0
    for index, row in enumerate(source_rows, 1):
        try:
            mapped.append(mapper(row, field_mapping, now=now))
        except (TypeError, ValueError) as exc:
            skipped += 1
            LOGGER.warning("skip invalid source row %d: %s", index, exc)
    sync_id_field = field_mapping["$sync_id"]
    source_field = field_mapping["$source"]
    updated_at_field = field_mapping["$sync_time"]
    existing = client.list_records(app_token, table_id)
    creates, updates, deletes = diff_records(
        mapped,
        existing,
        sync_id_field=sync_id_field,
        source_field=source_field,
        updated_at_field=updated_at_field,
    )
    write_batches = [
        *((client.batch_create, batch) for batch in _batches(creates)),
        *((client.batch_update, batch) for batch in _batches(updates)),
        *((client.batch_delete, batch) for batch in _batches(deletes)),
    ]
    for index, (operation, batch) in enumerate(write_batches):
        operation(app_token, table_id, batch)
        if index < len(write_batches) - 1:
            time.sleep(BATCH_SLEEP_SECONDS)
    return {
        "source": len(source_rows), "created": len(creates), "updated": len(updates),
        "deleted": len(deletes), "skipped": skipped,
    }


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("feishu_sync config must be a YAML mapping")
    for name in ("app_token", "gongkao_table_id", "qiuzhao_table_id"):
        if not str(raw.get(name) or "").strip():
            raise ValueError(f"config is missing {name}")
    sources = raw.get("sources")
    if not isinstance(sources, dict):
        raise ValueError("config is missing sources mapping")
    for name in ("gongkao", "qiuzhao"):
        section = sources.get(name)
        if not isinstance(section, dict) or not isinstance(section.get("field_mapping"), dict):
            raise ValueError(f"config is missing sources.{name}.field_mapping")
        for required in ("$sync_id", "$sync_time", "$source"):
            if required not in section["field_mapping"]:
                raise ValueError(f"sources.{name}.field_mapping is missing {required}")
    return raw


def _load_items(path: Path) -> list[Mapping[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError(f"{path} must contain a JSON list or an object with an items list")
    return [row for row in rows if isinstance(row, Mapping)]


def _alert(message: str) -> None:
    webhook, bark_url = os.getenv("FEISHU_WEBHOOK"), os.getenv("BARK_URL")
    providers = []
    if webhook:
        providers.append((webhook, {"msg_type": "text", "content": {"text": message}}))
    if bark_url:
        providers.append((bark_url, {"title": "飞书同步连续失败", "body": message, "group": "hot-gap"}))
    for url, payload in providers:
        try:
            response = httpx.post(url, json=payload, timeout=10, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            LOGGER.warning("failed to send sync alert: %s", exc)


def _load_failure_state(path: Path) -> dict[str, int]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {str(key): int(value) for key, value in raw.items()}
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def _save_failure_state(path: Path, state: Mapping[str, int]) -> None:
    try:
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("cannot persist Feishu sync failure state: %s", exc)


def run() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        LOGGER.error("FEISHU_APP_ID and FEISHU_APP_SECRET are required")
        return 2
    config_path = Path(os.getenv("FEISHU_SYNC_CONFIG", "config/feishu_sync.yaml"))
    data_dir = Path(os.getenv("SITE_DATA_DIR", "/var/www/hot-gap/data"))
    try:
        config = load_config(config_path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        LOGGER.error("cannot load Feishu sync config: %s", exc)
        return 2

    failure_path = data_dir / ".feishu_sync_failures.json"
    failures = _load_failure_state(failure_path)
    app_token = str(config["app_token"])
    jobs = (
        ("gongkao", str(config["gongkao_table_id"]), map_gongkao),
        ("qiuzhao", str(config["qiuzhao_table_id"]), map_qiuzhao),
    )
    failed = False
    with FeishuClient(app_id, app_secret) as client:
        for name, table_id, mapper in jobs:
            section = config["sources"][name]
            filename = str(section.get("file") or f"{name}.json")
            try:
                rows = _load_items(data_dir / filename)
                result = sync_table(
                    client, app_token, table_id, rows, mapper, section["field_mapping"]
                )
                failures[name] = 0
                LOGGER.info("%s sync complete: %s", name, result)
            except Exception as exc:  # keep the other table independent
                failed = True
                failures[name] = failures.get(name, 0) + 1
                LOGGER.exception("%s sync failed", name)
                if failures[name] >= 2 and (os.getenv("FEISHU_WEBHOOK") or os.getenv("BARK_URL")):
                    _alert(f"{name} 同步连续失败 {failures[name]} 次：{exc}")
    _save_failure_state(failure_path, failures)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
