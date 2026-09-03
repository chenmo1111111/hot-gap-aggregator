from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import yaml
from dotenv import load_dotenv
from selectolax.parser import HTMLParser

from app.notify import notify_subsidy_alert
from app.store.database import Database


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc
DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})(?:日)?")
META_CHARSET_RE = re.compile(br"charset\s*=\s*['\"]?([a-zA-Z0-9_-]+)", re.I)
MAIN_SELECTORS = (
    "main", "article", "#content", ".article-content", ".detail-content",
    ".content", ".TRS_Editor", ".zwxl-article", "body",
)
POLICY_PROMPT = """以下是某城市人才补贴政策页的旧版和新版。判断补贴金额、申领条件（学历/年龄/社保/落户）、申领时间窗口、名额有没有变化。有变化输出一句话说明“现在还能不能领、金额多少、截止什么时候”；没有输出 SKIP。

地区：{region}
政策：{name}

旧版：
{old}

新版：
{new}
"""
Judge = Callable[[str], Awaitable[str]]
Notifier = Callable[[dict[str, str]], Awaitable[dict[str, str]]]


def extract_main_text(html_text: str, selector: str | None = None) -> str:
    tree = HTMLParser(html_text)
    for node in tree.css("script,style,noscript,nav,footer,header,form,svg"):
        node.decompose()
    if selector:
        node = tree.css_first(selector)
        if node is None:
            raise ValueError(f"configured selector not found: {selector}")
        return " ".join(node.text(separator=" ", strip=True).split())
    candidates = [tree.css_first(candidate) for candidate in MAIN_SELECTORS]
    texts = [" ".join(node.text(separator=" ", strip=True).split()) for node in candidates if node is not None]
    return max(texts, key=len, default="")


def parse_list_html(html_text: str, base_url: str) -> list[dict[str, str]]:
    tree = HTMLParser(html_text)
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in tree.css("a[href]"):
        title = " ".join(anchor.text(separator=" ", strip=True).split())
        href = str(anchor.attributes.get("href") or "").strip()
        if len(title) < 5 or href.startswith(("javascript:", "#", "mailto:")):
            continue
        url = urljoin(base_url, href)
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        context = title
        parent = anchor.parent
        for _ in range(4):
            if parent is None:
                break
            candidate = " ".join(parent.text(separator=" ", strip=True).split())
            if len(candidate) <= 500:
                context = candidate
            if DATE_RE.search(candidate):
                context = candidate
                break
            parent = parent.parent
        match = DATE_RE.search(context) or DATE_RE.search(url)
        published = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}" if match else ""
        seen.add(url)
        entries.append({"title": title, "url": url, "date": published})
    return entries


def parse_hebei_home_menu(payload: dict[str, Any], detail_template: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for group in payload.get("data", []) if isinstance(payload, dict) else []:
        for tab in group.get("announcementTabCommonVOList", []) if isinstance(group, dict) else []:
            if str(tab.get("tabName") or "") != "通知公告":
                continue
            for row in tab.get("articleList", []) or []:
                title, article_id = str(row.get("title") or "").strip(), str(row.get("id") or "").strip()
                if not title or not article_id:
                    continue
                url = str(row.get("jumpLink") or "").strip() or detail_template.format(id=article_id)
                entries.append({"title": title, "url": url, "date": str(row.get("publishedDate") or "")[:10]})
    return entries


def decode_response(response: httpx.Response) -> str:
    content = response.content
    declared = response.headers.get("content-type", "")
    header_match = re.search(r"charset=([a-zA-Z0-9_-]+)", declared, re.I)
    meta_match = META_CHARSET_RE.search(content[:4096])
    encodings = [header_match.group(1) if header_match else "", meta_match.group(1).decode("ascii", "ignore") if meta_match else "", "utf-8", "gb18030"]
    for encoding in dict.fromkeys(value.lower() for value in encodings if value):
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


class SubsidyWatcher:
    timeout = 15.0
    retries = 2

    def __init__(
        self, database: Database, config_path: str | Path | None = None, *,
        judge: Judge | None = None, notifier: Notifier | None = None,
        alerts_path: str | Path | None = None,
    ) -> None:
        self.database = database
        self.config_path = Path(config_path or os.getenv("SUBSIDY_SOURCES_CONFIG", "config/subsidy_sources.yaml"))
        self.judge = judge or self._llm_judge
        self.notifier = notifier or notify_subsidy_alert
        self._custom_notifier = notifier is not None
        default_data_dir = Path(os.getenv("SERVER_SITE_DATA_DIR", "public/data"))
        self.alerts_path = Path(alerts_path) if alerts_path else default_data_dir / "alerts.json"

    def load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {"list_pages": [], "policy_pages": []}
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("subsidy sources config must be a mapping")
        if "pages" in raw and "policy_pages" not in raw:
            raw["policy_pages"] = [
                {**page, "region": page.get("region") or page.get("city")}
                for page in raw.get("pages", []) if isinstance(page, dict)
            ]
        return raw

    async def run(self) -> dict[str, list[dict[str, str]]]:
        config = self.load_config()
        keywords = [str(value).casefold() for value in config.get("title_keywords", []) if str(value).strip()]
        list_results: list[dict[str, str]] = []
        policy_results: list[dict[str, str]] = []
        for page in config.get("list_pages", []):
            if not isinstance(page, dict) or not str(page.get("url") or "").startswith(("http://", "https://")):
                continue
            try:
                list_results.append(await self.check_list_page(page, keywords))
            except Exception as exc:
                LOGGER.warning("subsidy list degraded (%s): %s", page.get("region"), exc)
                list_results.append({"region": str(page.get("region") or ""), "status": "degraded", "error": str(exc)})
        for page in config.get("policy_pages", []):
            if not isinstance(page, dict) or not str(page.get("url") or "").startswith(("http://", "https://")):
                continue
            try:
                policy_results.append(await self.check_policy_page(page))
            except Exception as exc:
                LOGGER.warning("subsidy policy degraded (%s): %s", page.get("name"), exc)
                policy_results.append({"region": str(page.get("region") or ""), "name": str(page.get("name") or ""), "status": "degraded", "error": str(exc)})
        return {"list_pages": list_results, "policy_pages": policy_results}

    async def check_list_page(self, page: dict[str, Any], keywords: list[str]) -> dict[str, str]:
        region, url = str(page.get("region") or ""), str(page["url"])
        response = await self._fetch_response(url)
        if str(page.get("format") or "") == "hebei_home_menu":
            payload = response.json() if isinstance(response, httpx.Response) else json.loads(str(response))
            template = str(page.get("detail_url_template") or "https://rst.hebei.gov.cn/pageWarp?isId={id}&id=1")
            entries = parse_hebei_home_menu(payload, template)
        else:
            html_text = decode_response(response) if isinstance(response, httpx.Response) else str(response)
            entries = parse_list_html(html_text, url)
        matched = [entry for entry in entries if any(keyword in entry["title"].casefold() for keyword in keywords)]
        watch_key = f"subsidy:list:{region}:{url}"
        digest = hashlib.sha256(json.dumps(matched, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        previous = self.database.get_watcher_state(watch_key)
        event_pairs = [(f"subsidy:list:{region}:{entry['url']}", entry) for entry in matched]
        if previous is None:
            self.database.mark_push_events([key for key, _ in event_pairs])
            self.database.save_watcher_state(watch_key, digest, json.dumps(matched, ensure_ascii=False))
            return {"region": region, "status": "baseline", "item_count": str(len(matched))}

        unseen = self.database.unseen_push_events([key for key, _ in event_pairs])
        pushed = 0
        for event_key, entry in event_pairs:
            if event_key not in unseen:
                continue
            alert = self._alert(region, entry["title"], entry["url"], entry.get("date", ""), "公告")
            if await self._deliver(alert):
                self.database.mark_push_events([event_key])
                pushed += 1
        self.database.save_watcher_state(watch_key, digest, json.dumps(matched, ensure_ascii=False))
        return {"region": region, "status": "pushed" if pushed else "unchanged", "item_count": str(len(matched)), "pushed": str(pushed)}

    async def check_policy_page(self, page: dict[str, Any]) -> dict[str, str]:
        region = str(page.get("region") or page.get("city") or "")
        name, url = str(page.get("name") or ""), str(page["url"])
        fetched = await self._fetch_response(url)
        html_text = decode_response(fetched) if isinstance(fetched, httpx.Response) else str(fetched)
        content = extract_main_text(html_text, str(page.get("selector") or "") or None)
        if len(content) < 40:
            raise ValueError("extracted policy text is too short")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        watch_key = f"subsidy:policy:{region}:{name}:{url}"
        previous = self.database.get_watcher_state(watch_key)
        if previous is None:
            self.database.save_watcher_state(watch_key, content_hash, content)
            return {"region": region, "name": name, "status": "baseline"}
        if previous["content_hash"] == content_hash:
            return {"region": region, "name": name, "status": "unchanged"}
        event_key = f"{watch_key}:{content_hash}"
        if event_key not in self.database.unseen_push_events([event_key]):
            self.database.save_watcher_state(watch_key, content_hash, content)
            return {"region": region, "name": name, "status": "already-pushed"}
        prompt = POLICY_PROMPT.format(region=region, name=name, old=previous["content_text"][-8000:], new=content[-8000:])
        verdict = " ".join((await self.judge(prompt)).split()).strip()
        if not verdict or verdict.upper() == "SKIP":
            self.database.save_watcher_state(watch_key, content_hash, content)
            return {"region": region, "name": name, "status": "changed-no-policy-diff"}
        alert = self._alert(region, name, url, datetime.now(UTC).date().isoformat(), "政策变动", verdict)
        if await self._deliver(alert):
            self.database.mark_push_events([event_key])
            self.database.save_watcher_state(watch_key, content_hash, content)
            return {"region": region, "name": name, "status": "pushed", "message": alert["message"]}
        return {"region": region, "name": name, "status": "notification-degraded"}

    async def _deliver(self, alert: dict[str, str]) -> bool:
        if self._custom_notifier or os.getenv("FEISHU_WEBHOOK") or os.getenv("BARK_URL"):
            statuses = await self.notifier(alert)
            return any(status == "ok" for status in statuses.values())
        self._write_alert(alert)
        return True

    def _write_alert(self, alert: dict[str, str]) -> None:
        self.alerts_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = json.loads(self.alerts_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            payload = {"generated_at": "", "items": []}
        items = [item for item in payload.get("items", []) if item.get("id") != alert["id"]]
        payload = {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "items": [alert, *items][:100]}
        temporary = self.alerts_path.with_suffix(self.alerts_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.alerts_path)

    @staticmethod
    def _alert(region: str, title: str, url: str, published: str, kind: str, detail: str = "") -> dict[str, str]:
        created = datetime.now(UTC).isoformat(timespec="seconds")
        tag = f"【补贴预警·{region}】"
        message = f"{tag}{title}{f'｜{detail}' if detail else ''}｜{url}｜{published or created[:10]}"
        return {
            "id": hashlib.sha256(f"{region}:{url}:{kind}:{detail}".encode("utf-8")).hexdigest()[:20],
            "tag": tag, "region": region, "type": kind, "title": title, "url": url,
            "date": published or created[:10], "summary": detail, "message": message, "created_at": created,
        }

    async def _fetch_response(self, url: str) -> httpx.Response:
        last_error: Exception | None = None
        headers = {"User-Agent": "Mozilla/5.0 (compatible; hot-gap-policy-watcher/1.0)"}
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers=headers) as client:
            for attempt in range(self.retries + 1):
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    return response
                except httpx.HTTPError as exc:
                    last_error = exc
                    if attempt < self.retries:
                        await asyncio.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"subsidy page request failed: {last_error}")

    async def _llm_judge(self, prompt: str) -> str:
        key = os.getenv("ZHIPU_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("ZHIPU_API_KEY or DEEPSEEK_API_KEY is required for policy diff judgment")
        zhipu = bool(os.getenv("ZHIPU_API_KEY"))
        url = "https://open.bigmodel.cn/api/paas/v4/chat/completions" if zhipu else os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/") + "/chat/completions"
        model = "glm-4-flash" if zhipu else os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
            for attempt in range(3):
                try:
                    response = await client.post(url, headers={"Authorization": f"Bearer {key}"}, json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1})
                    response.raise_for_status()
                    return str(response.json()["choices"][0]["message"]["content"])
                except (httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
                    last_error = exc
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"policy diff model failed: {last_error}")


async def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    database = Database(os.getenv("SERVER_DATABASE", "data/server.db"))
    try:
        LOGGER.info("subsidy watcher: %s", await SubsidyWatcher(database).run())
    finally:
        database.close()


if __name__ == "__main__":
    asyncio.run(main())
