from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv
from selectolax.parser import HTMLParser

from app.notify import notify_priority_alert
from app.store.database import Database


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc
POLICY_PROMPT = """以下是某城市人才补贴政策页的旧版和新版。判断补贴金额、申领条件（学历/年龄/社保/落户）、申领时间窗口、名额有没有变化。有变化输出一句话说明“现在还能不能领、金额多少、截止什么时候”；没有输出 SKIP。

城市：{city}
政策：{name}

旧版：
{old}

新版：
{new}
"""
MAIN_SELECTORS = (
    "main", "article", "#content", ".article-content", ".detail-content",
    ".content", ".TRS_Editor", ".zwxl-article", "body",
)
Judge = Callable[[str], Awaitable[str]]
Notifier = Callable[[str, str], Awaitable[dict[str, str]]]


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


class CitySubsidyWatcher:
    timeout = 15.0
    retries = 2

    def __init__(
        self, database: Database, config_path: str | Path | None = None,
        *, judge: Judge | None = None, notifier: Notifier | None = None,
    ) -> None:
        self.database = database
        self.config_path = Path(config_path or os.getenv("CITY_SUBSIDY_CONFIG", "config/city_subsidy.yaml"))
        self.judge = judge or self._llm_judge
        self.notifier = notifier or (lambda text, title: notify_priority_alert(text, title))

    def load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {"pages": []}
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("city subsidy config must be a mapping")
        return raw

    async def run(self) -> list[dict[str, str]]:
        pages = self.load_config().get("pages", [])
        results: list[dict[str, str]] = []
        for page in pages:
            if not isinstance(page, dict) or not str(page.get("url") or "").startswith(("http://", "https://")):
                continue
            try:
                results.append(await self.check_page(page))
            except Exception as exc:
                LOGGER.warning("city subsidy page failed (%s): %s", page.get("name"), exc)
                results.append({"city": str(page.get("city") or ""), "name": str(page.get("name") or ""), "status": "degraded", "error": str(exc)})
        return results

    async def check_page(self, page: dict[str, Any]) -> dict[str, str]:
        city, name, url = str(page.get("city") or ""), str(page.get("name") or ""), str(page["url"])
        html_text = await self._fetch(url)
        content = extract_main_text(html_text, str(page.get("selector") or "") or None)
        if len(content) < 40:
            raise ValueError("extracted policy text is too short")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        watch_key = f"city_subsidy:{city}:{name}:{url}"
        previous = self.database.get_watcher_state(watch_key)
        if previous is None:
            self.database.save_watcher_state(watch_key, content_hash, content)
            return {"city": city, "name": name, "status": "baseline"}
        if previous["content_hash"] == content_hash:
            return {"city": city, "name": name, "status": "unchanged"}

        event_key = f"{watch_key}:{content_hash}"
        if event_key not in self.database.unseen_push_events([event_key]):
            self.database.save_watcher_state(watch_key, content_hash, content)
            return {"city": city, "name": name, "status": "already-pushed"}
        prompt = POLICY_PROMPT.format(city=city, name=name, old=previous["content_text"][-8000:], new=content[-8000:])
        verdict = " ".join((await self.judge(prompt)).split()).strip()
        if not verdict or verdict.upper() == "SKIP":
            self.database.save_watcher_state(watch_key, content_hash, content)
            return {"city": city, "name": name, "status": "changed-no-policy-diff"}

        checked_at = datetime.now(UTC).isoformat(timespec="seconds")
        message = f"【补贴变动】{city}·{name}｜{verdict}｜{url}｜{checked_at}"
        provider_status = await self.notifier(message, "城市人才补贴变动")
        if any(status == "ok" for status in provider_status.values()):
            self.database.mark_push_events([event_key])
            self.database.save_watcher_state(watch_key, content_hash, content)
            return {"city": city, "name": name, "status": "pushed", "message": message}
        return {"city": city, "name": name, "status": "notification-degraded"}

    async def _fetch(self, url: str) -> str:
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 hot-gap-policy-watcher"}) as client:
            for attempt in range(self.retries + 1):
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    return response.text
                except httpx.HTTPError as exc:
                    last_error = exc
                    if attempt < self.retries:
                        await asyncio.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"policy page request failed: {last_error}")

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
                    response = await client.post(
                        url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1},
                    )
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
        results = await CitySubsidyWatcher(database).run()
        LOGGER.info("city subsidy watcher: %s", results)
    finally:
        database.close()


if __name__ == "__main__":
    asyncio.run(main())
