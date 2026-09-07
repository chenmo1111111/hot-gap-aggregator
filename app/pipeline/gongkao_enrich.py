"""Incrementally enrich Gongkao records for the public Feishu table."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv
from selectolax.parser import HTMLParser

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
bishi_kemu(笔试科目，如'行测+申论'/'公共基础知识'/'专业课'，没写填'')，
xian_huji(是否限户籍：true/false)，huji_shuoming(限户籍的说明，如'限山东省户籍')，
xian_zhuanye(是否限专业：true/false)，zhuanye_shuoming(专业要求简述)，
xueli(学历要求，如'本科及以上'/'硕士'/'不限')，
xian_yingjie(是否限应届：true/false)，
fuwu_qi(有无最低服务年限，如'5年'/'无')，
bei_zhu(其它关键限制一句话)"""
EXTRACTION_KEYS = (
    "bishi_kemu", "xian_huji", "huji_shuoming", "xian_zhuanye",
    "zhuanye_shuoming", "xueli", "xian_yingjie", "fuwu_qi", "bei_zhu",
)


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


def detail_category(row: Mapping[str, Any]) -> str:
    extra = row.get("extra") if isinstance(row.get("extra"), Mapping) else {}
    exam_type = normalize_exam_type(extra.get("exam_type") or row.get("exam_type"))
    tags = extra.get("tags") if isinstance(extra.get("tags"), list) else []
    text = " ".join(str(value or "") for value in (
        row.get("title_zh"), row.get("title"), extra.get("exam_type"), *tags,
    ))
    if any(marker in text for marker in ("银行", "农信社", "信用社", "村镇银行")):
        return "银行"
    if any(marker in text for marker in ("央企", "中央企业", "中央直属")):
        return "央企"
    if exam_type == "国企" or any(marker in text for marker in ("国企", "国有企业", "国资委")):
        return "国企"
    if exam_type == "事业单位" or "事业单位" in text or "事业编" in text:
        return "事业单位"
    return {
        "教师": "教师", "医疗": "医疗", "选调生": "选调", "三支一扶": "三支一扶",
        "国考": "公务员", "省考": "公务员", "公安": "公务员", "军队文职": "公务员",
    }.get(exam_type, "其他")


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
        """)

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

    def extract(self, text: str) -> dict[str, Any]:
        response = self.client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": DEEPSEEK_SYSTEM_PROMPT},
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
        "title": item.get("title_zh") or item.get("title"),
        "url": item.get("url"),
        "summary": item.get("summary_zh") or item.get("summary"),
        "published_at": item.get("published_at"),
        "start": extra.get("startSignUpTime"),
        "end": extra.get("endSignUpTime"),
    }
    value = json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
    stats = {"items": len(source_items), "extracted": 0, "cached": 0, "failed": 0, "skipped": 0}
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
        extra["my_school_eligible"] = is_my_school_eligible(item, school_config)

        article_id = str(extra.get("id") or "").strip()
        source_hash = _source_hash(item)
        cached = cache.get(article_id) if article_id else None
        if cached and cached["source_hash"] == source_hash and (
            cached["status"] == "ok" or not retry_failed
        ):
            _merge_extracted(extra, json.loads(cached["提取JSON"]), str(cached["status"]))
            stats["cached"] += 1
        elif not article_id.isdigit() or str(extra.get("sub") or "") != "announcement":
            extra["enrichment_status"] = "未提取"
            stats["skipped"] += 1
        elif not extraction_available or extractor is None:
            extra["enrichment_status"] = "未提取"
            stats["skipped"] += 1
        else:
            try:
                article_text = extractor.fetch_article(article_id)
                if len(article_text) < 20:
                    raise ValueError("粉笔公告正文为空或过短")
                extracted = extractor.extract(article_text)
                apply_url = str(getattr(extractor, "last_apply_url", "") or "").strip()
                if apply_url:
                    extracted["_apply_url"] = apply_url
                cache.put(article_id, source_hash, extracted)
                _merge_extracted(extra, extracted, "ok")
                stats["extracted"] += 1
                failure_streak = 0
            except Exception as exc:
                LOGGER.warning("announcement enrichment failed for %s: %s", article_id, exc)
                cache.put(article_id, source_hash, {}, status="未提取", error=str(exc))
                extra["enrichment_status"] = "未提取"
                stats["failed"] += 1
                failure_streak += 1
                if failure_streak >= 3:
                    extraction_available = False
                    LOGGER.warning("DeepSeek/Fenbi unavailable after 3 consecutive failures; skip remaining uncached rows")
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
