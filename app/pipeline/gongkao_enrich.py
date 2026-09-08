"""Incrementally enrich Gongkao records for the public Feishu table."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import logging
import os
import re
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import yaml
from dotenv import load_dotenv
from selectolax.parser import HTMLParser

from app.pipeline.gongkao_classify import detail_category, record_kind, record_kind_needs_llm
from app.sync_feishu import CHINA_TZ, _date_value, normalize_exam_type


LOGGER = logging.getLogger(__name__)
FENBI_DETAIL_URL = "https://hera-webapp.fenbi.com/api/website/article/detail"
FENBI_DETAIL_PARAMS = {
    "deviceType": "3",
    "app": "web",
    "av": "100",
    "hav": "100",
    "kav": "100",
    "client_context_id": "",
}
DEEPSEEK_SYSTEM_PROMPT = """从下面这段招考公告提取信息，只输出 JSON，字段：
xian_huji(是否限户籍：true/false)，huji_shuoming(限户籍的说明，如'限山东省户籍')，
xian_zhuanye(是否限专业：true/false)，zhuanye_shuoming(专业要求简述)，
xueli(学历要求，如'本科及以上'/'硕士'/'不限')，
xian_yingjie(是否限应届：true/false)，
fuwu_qi(有无最低服务年限，如'5年'/'无')，
zhaopin_renshu(招聘人数，如'53人'/'若干'/'200名'，公告没写填'/')，
record_kind(只能填'公考'或'秋招'：企业校园招聘填'秋招'，公务员/事业单位/选调/教师/医疗/军队文职及企业社会招聘填'公考')，
bei_zhu(其它关键限制一句话)"""
XUANDIAO_SCOPE_PROMPT = """
选调生公告还需输出字段：xuandiao_school_scope(招录院校范围，用一句可独立展示的话概括，例如'面向全国重点建设高校（含985/211/双一流）'、'面向本省高校'、'指定XX所高校（名单见公告）'、'双一流建设高校'、'不限'；无法判断填'名单见公告')"""
EXTRACTION_KEYS = (
    "xian_huji", "huji_shuoming", "xian_zhuanye", "zhuanye_shuoming",
    "xueli", "xian_yingjie", "fuwu_qi", "zhaopin_renshu", "record_kind",
    "bei_zhu", "xuandiao_school_scope",
)
ENRICHMENT_SCHEMA_VERSION = 3
WATCHER_SUBSOURCES = {"xuandiao", "scs", "campus"}
WEBPAGE_CONTENT_SELECTORS = (
    "article", "main", "#content", "#zoom", ".article-content", ".detail-content",
    ".pages_content", ".TRS_Editor", ".zwxl-article", ".article", ".content",
)
META_CHARSET_RE = re.compile(br"charset\s*=\s*['\"]?([a-zA-Z0-9_-]+)", re.I)


def calculate_signup_status(
    start: object, end: object, *, today: date | None = None
) -> tuple[str | None, int | None]:
    """Return the display status and non-negative days until deadline."""
    current = today or datetime.now(CHINA_TZ).date()
    start_date = _date_value(start)
    end_date = _date_value(end)
    days_left = max(0, (end_date - current).days) if end_date else None
    if start_date and start_date > current:
        return "未开始", days_left
    if end_date and end_date < current:
        return "已截止", 0
    if end_date and 1 <= (end_date - current).days <= 5:
        return f"剩{(end_date - current).days}天", days_left
    if start_date or end_date:
        return "报名中", days_left
    return None, None


def _normalized_school(value: object) -> str:
    return re.sub(r"[\s（）()·•.-]+", "", str(value or "")).casefold()


def load_school_config(path: str | Path) -> dict[str, Any]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("xuandiao school config must be a YAML mapping")
    return raw


def is_my_school_eligible(row: Mapping[str, Any], config: Mapping[str, Any]) -> bool:
    extra = row.get("extra") if isinstance(row.get("extra"), Mapping) else {}
    if normalize_exam_type(extra.get("exam_type") or row.get("exam_type")) != "选调生":
        return False
    my_school = config.get("my_school") if isinstance(config.get("my_school"), Mapping) else {}
    names = [my_school.get("name"), *(my_school.get("aliases") or [])]
    normalized_names = {_normalized_school(name) for name in names if name}
    province = str(extra.get("province") or row.get("province") or "").removesuffix("省").strip()
    title = str(row.get("title_zh") or row.get("title") or "")
    if "中央选调" in title:
        province = "中央选调"
    schools_by_province = config.get("schools_by_province")
    province_schools = (
        schools_by_province.get(province, []) if isinstance(schools_by_province, Mapping) else []
    )
    for school in province_schools or []:
        school_name = school.get("name") if isinstance(school, Mapping) else school
        if _normalized_school(school_name) in normalized_names:
            return True
    # An explicit list is authoritative. Keyword fallback is used only when
    # the latest notice did not publish a machine-readable school list.
    if province_schools:
        return False

    haystack = " ".join(str(value or "") for value in (
        title, row.get("summary_zh"), extra.get("notes"), *(extra.get("tags") or []),
    ))
    school_tags = [str(value) for value in (my_school.get("tags") or [])]
    default_rules = [str(value) for value in (config.get("default_rules") or [])]
    return any(rule in haystack and rule in school_tags for rule in default_rules)


def strip_article_html(html: str) -> str:
    tree = HTMLParser(html)
    for node in tree.css("script,style,noscript,.anti-crawler"):
        node.decompose()
    content = tree.css_first("#content") or tree.body
    if content is None:
        return ""
    text = content.text(separator="\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def strip_webpage_html(html: str) -> str:
    """Extract the most likely announcement body from a government HTML page."""
    tree = HTMLParser(html)
    for node in tree.css(
        "script,style,noscript,nav,footer,header,form,svg,.anti-crawler,.share,.toolbar"
    ):
        node.decompose()
    candidates: list[str] = []
    for selector in WEBPAGE_CONTENT_SELECTORS:
        node = tree.css_first(selector)
        if node is None:
            continue
        text = " ".join(node.text(separator=" ", strip=True).split())
        if len(text) >= 20:
            candidates.append(text)
    if candidates:
        return max(candidates, key=len)
    body = tree.body
    return " ".join(body.text(separator=" ", strip=True).split()) if body else ""


def _decode_web_response(response: httpx.Response) -> str:
    content = response.content
    declared = response.headers.get("content-type", "")
    header_match = re.search(r"charset=([a-zA-Z0-9_-]+)", declared, re.I)
    meta_match = META_CHARSET_RE.search(content[:4096])
    encodings = [
        header_match.group(1) if header_match else "",
        meta_match.group(1).decode("ascii", "ignore") if meta_match else "",
        "utf-8",
        "gb18030",
    ]
    for encoding in dict.fromkeys(value.lower() for value in encodings if value):
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


def _canonical_web_url(value: object) -> str:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.casefold()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            return ""
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path or "/", parsed.query, ""))


def url_cache_key(url: str) -> str:
    canonical = _canonical_web_url(url)
    if not canonical:
        return ""
    return "url:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_extraction_json(value: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        raw = dict(value)
    else:
        text = value.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
        candidate = fenced.group(1) if fenced else text[text.find("{") : text.rfind("}") + 1]
        raw = json.loads(candidate)
    result: dict[str, Any] = {}
    for key in EXTRACTION_KEYS:
        item = raw.get(key, False if key in {"xian_huji", "xian_zhuanye", "xian_yingjie"} else "")
        if key in {"xian_huji", "xian_zhuanye", "xian_yingjie"}:
            result[key] = item is True or str(item).strip().casefold() in {"true", "1", "是", "有"}
        elif key == "record_kind":
            result[key] = str(item).strip() if str(item).strip() in {"公考", "秋招"} else ""
        elif key == "zhaopin_renshu":
            result[key] = str(item or "/").strip() or "/"
        else:
            result[key] = str(item or "").strip()
    return result


class EnrichmentCache:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS gongkao_enrichment (
                article_id TEXT PRIMARY KEY,
                "提取JSON" TEXT NOT NULL,
                "提取时间" TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS gongkao_first_seen (
                record_key TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL,
                backfilled INTEGER NOT NULL DEFAULT 1
            );
        """)
        first_seen_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(gongkao_first_seen)")
        }
        if "backfilled" not in first_seen_columns:
            self.connection.execute(
                "ALTER TABLE gongkao_first_seen ADD COLUMN backfilled INTEGER NOT NULL DEFAULT 0"
            )
            self.connection.commit()

    def get(self, article_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            'SELECT article_id,"提取JSON","提取时间",source_hash,status,error '
            "FROM gongkao_enrichment WHERE article_id=?", (article_id,),
        ).fetchone()
        return dict(row) if row else None

    def put(
        self, article_id: str, source_hash: str, extracted: Mapping[str, Any],
        *, status: str = "ok", error: str = "",
    ) -> None:
        self.connection.execute(
            'INSERT OR REPLACE INTO gongkao_enrichment('
            'article_id,"提取JSON","提取时间",source_hash,status,error) VALUES(?,?,?,?,?,?)',
            (
                article_id, json.dumps(dict(extracted), ensure_ascii=False),
                datetime.now(CHINA_TZ).isoformat(), source_hash, status, error[:500],
            ),
        )
        self.connection.commit()

    def get_or_create_first_seen(
        self, record_key: str, first_seen: date, *, source_date: date | None = None
    ) -> str:
        value = (source_date or first_seen).isoformat()
        self.connection.execute(
            "INSERT OR IGNORE INTO gongkao_first_seen(record_key,first_seen,backfilled) VALUES(?,?,1)",
            (record_key, value),
        )
        row = self.connection.execute(
            "SELECT first_seen,backfilled FROM gongkao_first_seen WHERE record_key=?", (record_key,),
        ).fetchone()
        if row and not int(row["backfilled"]):
            replacement = source_date.isoformat() if source_date else str(row["first_seen"])
            self.connection.execute(
                "UPDATE gongkao_first_seen SET first_seen=?,backfilled=1 WHERE record_key=?",
                (replacement, record_key),
            )
            row = self.connection.execute(
                "SELECT first_seen,backfilled FROM gongkao_first_seen WHERE record_key=?", (record_key,),
            ).fetchone()
        self.connection.commit()
        return str(row["first_seen"] if row else value)

    def close(self) -> None:
        self.connection.close()


class DeepSeekExtractor:
    def __init__(
        self, api_key: str, *, base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat", timeout: float = 45,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = httpx.Client(timeout=timeout, follow_redirects=True)
        self.last_apply_url = ""

    def fetch_article(self, article_id: str) -> str:
        params = {**FENBI_DETAIL_PARAMS, "id": article_id}
        response = self.client.get(FENBI_DETAIL_URL, params=params)
        response.raise_for_status()
        html = response.content.decode("utf-8", errors="replace")
        tree = HTMLParser(html)
        apply_node = tree.css_first("a.register-button[href]") or tree.css_first("a.position-button[href]")
        self.last_apply_url = str(apply_node.attributes.get("href") or "") if apply_node else ""
        return strip_article_html(html)

    def fetch_url(self, url: str) -> str:
        canonical = _canonical_web_url(url)
        if not canonical:
            raise ValueError("公告 URL 无效或不是公网 HTTP(S) 地址")
        self.last_apply_url = ""
        response = self.client.get(
            canonical,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; hot-gap-gongkao-enrich/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        response.raise_for_status()
        if not _canonical_web_url(str(response.url)):
            raise ValueError("公告 URL 重定向到非公网地址")
        content_type = response.headers.get("content-type", "").casefold()
        if response.content.startswith(b"%PDF") or any(
            marker in content_type
            for marker in ("application/pdf", "application/octet-stream", "application/msword")
        ):
            raise ValueError(f"公告不是可直接解析的 HTML 页面: {content_type or 'unknown'}")
        return strip_webpage_html(_decode_web_response(response))

    def extract(self, text: str, *, include_xuandiao_scope: bool = False) -> dict[str, Any]:
        system_prompt = DEEPSEEK_SYSTEM_PROMPT + (
            XUANDIAO_SCOPE_PROMPT if include_xuandiao_scope else ""
        )
        response = self.client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text[:60000]},
                ],
            },
        )
        response.raise_for_status()
        payload = response.json()
        return parse_extraction_json(payload["choices"][0]["message"]["content"])

    def close(self) -> None:
        self.client.close()


def _source_hash(item: Mapping[str, Any]) -> str:
    extra = item.get("extra") if isinstance(item.get("extra"), Mapping) else {}
    stable = {
        "schema_version": ENRICHMENT_SCHEMA_VERSION,
        "title": item.get("title_zh") or item.get("title"),
        "url": item.get("url"),
        "summary": item.get("summary_zh") or item.get("summary"),
        "published_at": item.get("published_at"),
        "start": extra.get("startSignUpTime"),
        "end": extra.get("endSignUpTime"),
    }
    value = json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _first_seen_key(item: Mapping[str, Any], extra: Mapping[str, Any]) -> str:
    identifier = str(extra.get("id") or item.get("id") or "").strip()
    if identifier:
        kind = str(extra.get("subsource") or extra.get("sub") or item.get("source") or "gongkao")
        return f"{kind}:{identifier}"
    url_key = url_cache_key(
        str(item.get("url") or item.get("announcement_url") or extra.get("announcement_url") or "")
    )
    if url_key:
        return url_key
    stable = "|".join(str(value or "").strip() for value in (
        item.get("title_zh") or item.get("title"), extra.get("province") or item.get("province"),
    ))
    return "fallback:" + hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _first_seen_source_date(item: Mapping[str, Any], extra: Mapping[str, Any]) -> date | None:
    candidates = [
        _date_value(item.get("published_at")),
        _date_value(extra.get("updateTime")),
        _date_value(extra.get("enrollStartTime")),
        _date_value(extra.get("startSignUpTime")),
        _date_value(item.get("报名开始")),
    ]
    values = [value for value in candidates if value is not None]
    return min(values) if values else None


def extract_recruit_count(row: Mapping[str, Any]) -> str:
    """Extract a conservative headcount from already-collected text."""
    extra = row.get("extra") if isinstance(row.get("extra"), Mapping) else {}
    for value in (
        extra.get("recruit_count"), extra.get("recruitment_count"), extra.get("headcount"),
        extra.get("zhaopin_renshu"), row.get("recruit_count"), row.get("招录人数"),
    ):
        text = str(value or "").strip()
        if text and text != "/":
            return text
    haystack = " ".join(str(value or "") for value in (
        row.get("title_zh"), row.get("title"), row.get("summary_zh"), row.get("summary"),
    ))
    match = re.search(
        r"(?:计划)?(?:招录|招聘|招考)(?:工作人员|人员|岗位)?\s*([0-9]{1,5}|若干)\s*([人名])",
        haystack,
    )
    return f"{match.group(1)}{match.group(2)}" if match else ""


def _enrichment_source(
    item: Mapping[str, Any], extra: Mapping[str, Any]
) -> tuple[str, str, str] | None:
    article_id = str(extra.get("id") or "").strip()
    if article_id.isdigit() and str(extra.get("sub") or "") == "announcement":
        # Keep the legacy numeric cache key so existing Fenbi extractions remain reusable.
        return "fenbi", article_id, article_id
    subsource = str(extra.get("subsource") or "").strip().casefold()
    if subsource not in WATCHER_SUBSOURCES and not record_kind_needs_llm(item):
        return None
    url = _canonical_web_url(
        item.get("url") or item.get("announcement_url") or extra.get("announcement_url")
    )
    cache_key = url_cache_key(url)
    return ("url", cache_key, url) if cache_key else None


def _merge_extracted(extra: dict[str, Any], extracted: Mapping[str, Any], status: str) -> None:
    for key in EXTRACTION_KEYS:
        if key in extracted:
            extra[key] = extracted[key]
    extra["enrichment_status"] = status
    if extracted.get("_apply_url"):
        extra["apply_url"] = str(extracted["_apply_url"])


def enrich_payload(
    payload: Mapping[str, Any], *, cache: EnrichmentCache,
    school_config: Mapping[str, Any], extractor: DeepSeekExtractor | None,
    today: date | None = None, retry_failed: bool = False,
) -> tuple[dict[str, Any], dict[str, int]]:
    output = dict(payload)
    source_items = payload.get("items")
    if not isinstance(source_items, list):
        raise ValueError("gongkao payload must contain an items list")
    stats = {
        "items": len(source_items), "extracted": 0, "fenbi_extracted": 0,
        "url_extracted": 0, "cached": 0, "failed": 0, "fetch_failed": 0,
        "llm_failed": 0, "skipped": 0,
    }
    items: list[dict[str, Any]] = []
    failure_streak = 0
    extraction_available = extractor is not None
    for source_item in source_items:
        if not isinstance(source_item, Mapping):
            continue
        item = dict(source_item)
        extra = dict(item.get("extra")) if isinstance(item.get("extra"), Mapping) else {}
        status, days = calculate_signup_status(
            extra.get("startSignUpTime") or item.get("报名开始"),
            extra.get("endSignUpTime") or item.get("报名截止") or item.get("截止日期"),
            today=today,
        )
        extra["signup_status"] = status
        extra["days_left"] = days
        extra["detail_category"] = detail_category(item)
        if count := extract_recruit_count(item):
            extra["recruit_count"] = count
        extra["record_kind"] = record_kind(item, llm_choice=extra.get("record_kind"))
        first_seen_date = today or datetime.now(CHINA_TZ).date()
        extra["first_seen"] = cache.get_or_create_first_seen(
            _first_seen_key(item, extra), first_seen_date,
            source_date=_first_seen_source_date(item, extra),
        )

        enrichment_source = _enrichment_source(item, extra)
        source_hash = _source_hash(item)
        cache_key = enrichment_source[1] if enrichment_source else ""
        cached = cache.get(cache_key) if cache_key else None
        if cached and cached["source_hash"] == source_hash and (
            cached["status"] == "ok" or not retry_failed
        ):
            _merge_extracted(extra, json.loads(cached["提取JSON"]), str(cached["status"]))
            stats["cached"] += 1
        elif enrichment_source is None:
            extra["enrichment_status"] = "未提取"
            stats["skipped"] += 1
        elif not extraction_available or extractor is None:
            extra["enrichment_status"] = "未提取"
            stats["skipped"] += 1
        else:
            source_kind, cache_key, source_value = enrichment_source
            try:
                article_text = (
                    extractor.fetch_article(source_value)
                    if source_kind == "fenbi"
                    else extractor.fetch_url(source_value)
                )
                if len(article_text) < 20:
                    raise ValueError("公告正文为空或过短")
            except Exception as exc:
                # A single government site can be unavailable, block robots,
                # or publish a non-HTML attachment. It must not disable other
                # domains or the later Feishu sync.
                LOGGER.warning("%s announcement fetch failed for %s: %s", source_kind, source_value, exc)
                cache.put(cache_key, source_hash, {}, status="未提取", error=str(exc))
                extra["enrichment_status"] = "未提取"
                stats["failed"] += 1
                stats["fetch_failed"] += 1
                item["extra"] = extra
                items.append(item)
                continue
            try:
                extracted = extractor.extract(
                    article_text,
                    include_xuandiao_scope=extra["detail_category"] == "选调生",
                )
                apply_url = str(getattr(extractor, "last_apply_url", "") or "").strip()
                if apply_url:
                    extracted["_apply_url"] = apply_url
                cache.put(cache_key, source_hash, extracted)
                _merge_extracted(extra, extracted, "ok")
                if count := extract_recruit_count({**item, "extra": extra}):
                    extra["recruit_count"] = count
                stats["extracted"] += 1
                stats[f"{source_kind}_extracted"] += 1
                failure_streak = 0
            except Exception as exc:
                LOGGER.warning("DeepSeek enrichment failed for %s: %s", source_value, exc)
                cache.put(cache_key, source_hash, {}, status="未提取", error=str(exc))
                extra["enrichment_status"] = "未提取"
                stats["failed"] += 1
                stats["llm_failed"] += 1
                failure_streak += 1
                if failure_streak >= 3:
                    extraction_available = False
                    LOGGER.warning("DeepSeek/announcement source unavailable after 3 consecutive failures; skip remaining uncached rows")
        extra["record_kind"] = record_kind(item, llm_choice=extra.get("record_kind"))
        item["extra"] = extra
        items.append(item)
    output["items"] = items
    output["generated_at"] = datetime.now(CHINA_TZ).isoformat()
    return output, stats


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Incrementally enrich Gongkao records")
    parser.add_argument("--data-dir", default=os.getenv("SITE_DATA_DIR", "/var/www/hot-gap/data"))
    parser.add_argument("--input", default="gongkao_feishu.json")
    parser.add_argument("--output", default="gongkao_enriched.json")
    parser.add_argument("--cache", default=os.getenv("GONGKAO_ENRICH_CACHE", "/var/lib/hot-gap/gongkao-enrichment.db"))
    parser.add_argument("--schools", default=os.getenv("XUANDIAO_SCHOOLS_CONFIG", "config/xuandiao_schools.yaml"))
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)
    payload = json.loads((data_dir / args.input).read_text(encoding="utf-8"))
    school_config = load_school_config(args.schools)
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    extractor = DeepSeekExtractor(
        api_key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
    ) if api_key else None
    cache = EnrichmentCache(args.cache)
    try:
        enriched, stats = enrich_payload(
            payload, cache=cache, school_config=school_config, extractor=extractor,
            retry_failed=args.retry_failed,
        )
    finally:
        cache.close()
        if extractor:
            extractor.close()
    destination = data_dir / args.output
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(enriched, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    print(json.dumps({"event": "gongkao_enriched", **stats}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
