"""Capture the purchased Shasha Feishu Base into a normalized Qiuzhao snapshot."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import yaml
from dotenv import load_dotenv

from app.capture_wanqing import (
    CaptureError,
    _atomic_write,
    _cell_text,
    _failure_count,
    _option_maps,
    _raw_value,
    decode_gzip_json,
)

LOGGER = logging.getLogger(__name__)
SOURCE_LABEL = "购买表-鲨鲨"
UPSTREAM_SOURCE = "shasha_feishu"


def _field_ids(metadata: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(field.get("name") or "").strip(): str(field_id)
        for field_id, field in (metadata.get("fieldMap") or {}).items()
        if isinstance(field, Mapping) and str(field.get("name") or "").strip()
    }


def _date_text(value: object) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if text.isdigit():
        stamp = int(text)
        if stamp > 10_000_000_000:
            stamp //= 1000
        try:
            return datetime.fromtimestamp(stamp, timezone.utc).astimezone().date().isoformat()
        except (OSError, OverflowError, ValueError):
            return ""
    return text


def _company_type(industry: str) -> str:
    tags = set(filter(None, re.split(r"[、,，/]+", industry)))
    if tags & {"国央企", "央国企"}:
        return "国企"
    if "外企" in tags:
        return "外企"
    if "事业单位" in tags:
        return "事业单位"
    return "其他"


def _industry_tag(industry: str) -> str:
    values: list[str] = []
    for raw in re.split(r"[、,，]+", industry):
        value = raw.strip()
        if not value or value in {"国央企", "央国企", "外企", "事业单位"}:
            continue
        if value in {"互联网", "科技", "互联网/人工智能"}:
            value = "互联网/科技"
        elif value in {"金融", "保险"}:
            value = "金融银行"
        if value not in values:
            values.append(value)
    return "、".join(values)


def infer_written_test_exempt(*values: object) -> tuple[str, str]:
    """Return (label, provenance); unknown remains blank and is never invented."""
    text = " ".join(str(value or "") for value in values)
    if re.search(r"(?:无需|免|无)笔试|免机考|直通面试", text):
        return "免笔试", "keyword"
    if re.search(r"仅测评|只有测评|仅需测评", text):
        return "仅测评", "keyword"
    if re.search(r"需要笔试|需笔试|笔试环节", text):
        return "需要笔试", "keyword"
    return "", ""


async def enrich_unknown_written_test(items: list[dict[str, Any]]) -> int:
    """Optionally classify a bounded blank subset with DeepSeek; failures stay blank."""
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    limit = int(os.getenv("SHASHA_DEEPSEEK_MAX_ITEMS", "200") or 200)
    candidates = [item for item in items if not item.get("written_test_requirement")][:max(0, limit)]
    if not api_key or not candidates:
        return 0
    endpoint = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/") + "/chat/completions"
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    changed = 0
    async with httpx.AsyncClient(timeout=45) as client:
        for start in range(0, len(candidates), 25):
            chunk = candidates[start:start + 25]
            prompt = "\n".join(
                f"{index + 1}. 公司={item.get('company_name','')}；岗位={item.get('position','')}；备注={item.get('notes','')}"
                for index, item in enumerate(chunk)
            )
            try:
                response = await client.post(endpoint, headers={"Authorization": f"Bearer {api_key}"}, json={
                    "model": model, "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": "判断招聘信息是否明确免笔试。只输出JSON对象，键为从1开始的序号，值只能是含免笔试、免笔试、仅测评、需要笔试或空字符串；证据不足必须空字符串。"},
                        {"role": "user", "content": prompt},
                    ],
                })
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                values = json.loads(content)
            except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                LOGGER.warning("DeepSeek 免笔试兜底失败，保留空值：%s", exc)
                continue
            for index, item in enumerate(chunk, 1):
                label = str(values.get(str(index)) or "").strip()
                if label not in {"含免笔试", "免笔试", "仅测评", "需要笔试"}:
                    continue
                item["written_test_requirement"] = label
                item["written_test"] = label == "需要笔试"
                item["written_test_exempt"] = label in {"含免笔试", "免笔试", "仅测评"}
                item["written_test_exempt_source"] = "deepseek"
                changed += 1
    return changed


def build_snapshot(
    metadata: Mapping[str, Any], chunks: list[Mapping[str, Any]], config: Mapping[str, Any],
    *, generated_at: str | None = None,
) -> dict[str, Any]:
    fields = _field_ids(metadata)
    required = [str(name) for name in config.get("required_fields") or []]
    missing = [name for name in required if name not in fields]
    if missing:
        raise CaptureError("鲨鲨来源表缺少必需字段：" + "、".join(missing))
    options = _option_maps(metadata)
    records: dict[str, Mapping[str, Any]] = {}
    for chunk in chunks:
        records.update({str(key): value for key, value in (chunk.get("recordMap") or {}).items() if isinstance(value, Mapping)})

    def text(record: Mapping[str, Any], name: str) -> str:
        return _cell_text(record, fields[name], options) if name in fields else ""

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    provenance_counts: dict[str, int] = {"source_field": 0, "keyword": 0, "deepseek": 0, "blank": 0}
    for record_id, record in records.items():
        company, position = text(record, "公司").strip(), text(record, "岗位").strip()
        if not company or not position:
            continue
        key = re.sub(r"\W+", "", company.casefold()) + "|" + re.sub(r"\W+", "", position.casefold())
        if key in seen:
            continue
        seen.add(key)
        source_written = text(record, "是否免笔试").strip()
        if source_written in {"含免笔试", "需要笔试", "仅测评"}:
            written_label, provenance = source_written, "source_field"
        else:
            written_label, provenance = infer_written_test_exempt(
                position, text(record, "招聘亮点"), text(record, "备注")
            )
        # The optional DeepSeek fallback is deliberately deferred to the
        # enrichment pass; capture stays deterministic and never fabricates a
        # value when the source and explicit wording are both silent.
        provenance_counts[provenance or "blank"] += 1
        industry_raw = text(record, "公司行业")
        deadline = text(record, "截止日期").strip()
        recruitment_stage = text(record, "招聘类型").strip()
        start_date = _date_text(_raw_value(record, fields["开始时间"]))
        deadline_month = ""
        match = re.search(r"(?:(20\d{2})[-/.年])?(\d{1,2})(?:[-/.月])", deadline)
        if match:
            year = match.group(1) or (start_date[:4] if start_date else "")
            deadline_month = f"{year}-{int(match.group(2)):02d}" if year else f"{int(match.group(2)):02d}月"
        items.append({
            "source_record_id": f"shasha:{record_id}",
            "company_name": company,
            "company_type": _company_type(industry_raw),
            "industry": _industry_tag(industry_raw),
            "industry_raw": industry_raw,
            "recruitment_stage": recruitment_stage,
            "position": position,
            "location": text(record, "工作地点"),
            "education": text(record, "学历要求"),
            "cohort": text(record, "届次"),
            "deadline": deadline,
            "deadline_month": deadline_month,
            "written_test": written_label == "需要笔试",
            "written_test_requirement": written_label,
            "written_test_exempt": written_label in {"含免笔试", "免笔试", "仅测评"} if written_label else None,
            "written_test_exempt_source": provenance,
            "apply_url": text(record, "投递链接"),
            "announcement_url": text(record, "公告链接"),
            "notes": text(record, "备注"),
            "updated_at": start_date,
            "source_label": SOURCE_LABEL,
            "upstream_source": UPSTREAM_SOURCE,
        })
        if len(items) >= int(config.get("max_items") or 20_000):
            break
    minimum = int(config.get("min_items") or 8_000)
    if len(items) < minimum:
        raise CaptureError(f"安全校验失败：只得到 {len(items)} 条，低于最低要求 {minimum} 条；旧快照已保留")
    return {
        "generated_at": generated_at or datetime.now().astimezone().isoformat(),
        "source": "qiuzhao",
        "status": {
            "source": "qiuzhao", "status": "ok", "item_count": len(items),
            "upstream_source": UPSTREAM_SOURCE, "source_label": SOURCE_LABEL,
            "source_view": str(config.get("source_view") or "校招总表"),
            "source_total_records": len(records), "written_test_provenance": provenance_counts,
            "deduplicated_by": "normalize(company_name)|normalize(position)",
        },
        "items": items,
    }


def load_config(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("鲨鲨抓取配置必须是 YAML 对象")
    source_url = os.getenv("SHASHA_FEISHU_BASE_URL", "").strip()
    if not source_url:
        raise ValueError("SHASHA_FEISHU_BASE_URL 未配置；真实地址只允许放在本地 .env")
    parsed = urlsplit(source_url)
    match = re.search(r"/base/([^/?#]+)", parsed.path)
    query = parse_qs(parsed.query)
    if not match or not query.get("table") or not query.get("view"):
        raise ValueError("SHASHA_FEISHU_BASE_URL 缺少 Base token、table 或 view")
    return {
        **value,
        "page_url": source_url,
        "api_origin": f"{parsed.scheme}://{parsed.netloc}",
        "app_token": match.group(1),
        "table_id": query["table"][0],
        "view_id": query["view"][0],
    }


async def capture(
    config: Mapping[str, Any], *, output_path: Path, profile_dir: Path,
    screenshot_dir: Path, headed: bool = False, login: bool = False,
) -> dict[str, Any]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise CaptureError("缺少 Playwright，请先安装 requirements.txt") from exc
    profile_dir.mkdir(parents=True, exist_ok=True)
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir), headless=not (headed or login), viewport={"width": 1600, "height": 1000},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(str(config["page_url"]), wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_timeout(5_000)
            if login:
                print("请在专用浏览器中登录飞书并确认能看到鲨鲨校招表，然后回到此窗口按 Enter。")
                await asyncio.to_thread(input)
            await page.screenshot(path=str(screenshot_dir / f"shasha_{stamp}.png"), full_page=False)
            origin, token = str(config["api_origin"]).rstrip("/"), str(config["app_token"])
            table_id, view_id = str(config["table_id"]), str(config["view_id"])
            meta_query = urlencode({"tableID": table_id, "viewID": view_id, "recordLimit": 20, "ondemandLimit": 200, "needBase": "true", "viewLazyLoad": "true", "ondemandVer": 2, "openType": 0, "noMissCS": "true", "optimizationFlag": 1, "removeFmlExtra": "true"})
            response = await context.request.get(f"{origin}/space/api/v1/bitable/{token}/clientvars?{meta_query}", timeout=90_000)
            envelope = await response.json()
            if response.status != 200 or envelope.get("code") != 0:
                raise CaptureError(f"飞书登录会话不可用（HTTP {response.status}, code={envelope.get('code')}）；请重新执行 --login")
            metadata = decode_gzip_json(str((envelope.get("data") or {}).get("table") or ""))
            table_rev = metadata.get("tableRev") or metadata.get("latestCSRev") or metadata.get("rev")
            record_count = int(metadata.get("recordCount") or 0)
            if table_rev is None or record_count <= 0:
                raise CaptureError("无法从飞书表结构读取 tableRev/recordCount")
            chunks: list[dict[str, Any]] = []
            page_size = int(config.get("page_size") or 3000)
            for offset in range(0, record_count, page_size):
                query = urlencode({"tableId": table_id, "viewId": view_id, "tableRev": table_rev, "depRev": "{}", "viewLazyLoad": "true", "offset": offset, "limit": page_size, "tableID": table_id, "viewID": view_id, "removeFmlExtra": "true"})
                result = await context.request.get(f"{origin}/space/api/v1/bitable/{token}/records?{query}", timeout=90_000)
                body = await result.json()
                if result.status != 200 or body.get("code") != 0:
                    raise CaptureError(f"读取 offset={offset} 失败（HTTP {result.status}, code={body.get('code')}）")
                chunks.append(decode_gzip_json(str((body.get("data") or {}).get("records") or "")))
                await asyncio.sleep(0.2)
            payload = build_snapshot(metadata, chunks, config)
            await enrich_unknown_written_test(payload["items"])
            payload["status"]["written_test_provenance"] = {
                name: sum(item.get("written_test_exempt_source") == name for item in payload["items"])
                for name in ("source_field", "keyword", "deepseek", "blank")
            }
            payload["status"]["written_test_provenance"]["blank"] = sum(
                not item.get("written_test_requirement") for item in payload["items"]
            )
            _atomic_write(output_path, payload)
            return payload
        finally:
            await context.close()


def _notify(message: str) -> None:
    for url, body in (
        (os.getenv("FEISHU_WEBHOOK", "").strip(), {"msg_type": "text", "content": {"text": message}}),
        (os.getenv("BARK_URL", "").strip(), {"title": "鲨鲨购买表抓取失败", "body": message, "group": "hot-gap"}),
    ):
        if not url:
            continue
        try:
            httpx.post(url, json=body, timeout=10, follow_redirects=True).raise_for_status()
        except httpx.HTTPError as exc:
            LOGGER.warning("failed to send capture alert: %s", exc)


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Capture the Shasha purchased Feishu Base")
    parser.add_argument("--config", default=os.getenv("SHASHA_CAPTURE_CONFIG", "config/shasha_capture.yaml"))
    parser.add_argument("--output", default=None)
    parser.add_argument("--profile-dir", default=os.getenv("SHASHA_PROFILE_DIR", ".shasha-browser"))
    parser.add_argument("--state-dir", default=os.getenv("SHASHA_STATE_DIR", ".shasha-state"))
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--login", action="store_true")
    args = parser.parse_args()
    state_dir = Path(args.state_dir)
    output = Path(args.output) if args.output else Path(os.getenv("SITE_DATA_DIR", "/var/www/hot-gap/data")) / "qiuzhao_shasha.json"
    failure_path = state_dir / "capture_failures.json"
    try:
        payload = asyncio.run(capture(load_config(Path(args.config)), output_path=output, profile_dir=Path(args.profile_dir), screenshot_dir=state_dir / "screenshots", headed=args.headed, login=args.login))
        _atomic_write(failure_path, {"count": 0})
        print(json.dumps({"event": "shasha_captured", "item_count": len(payload["items"]), "source_total_records": payload["status"]["source_total_records"], "written_test_provenance": payload["status"]["written_test_provenance"], "output": str(output)}, ensure_ascii=False))
        return 0
    except Exception as exc:
        count = _failure_count(failure_path) + 1
        _atomic_write(failure_path, {"count": count, "last_error": str(exc)})
        LOGGER.exception("shasha capture failed; previous snapshot preserved")
        if os.getenv("CAPTURE_RUNNER_MANAGED") != "1" and count >= 2:
            _notify(f"鲨鲨购买表抓取连续失败 {count} 次：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
