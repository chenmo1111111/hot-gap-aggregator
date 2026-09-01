from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import quote, urljoin

import yaml
from selectolax.parser import HTMLParser

from app.collectors.base import BaseCollector, SourceUnavailable
from app.models import Item


class XiaohongshuCollector(BaseCollector):
    source = "xiaohongshu"

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path or os.getenv("XHS_KEYWORDS_CONFIG", "config/xhs_keywords.yaml"))

    def load_keywords(self) -> list[str]:
        if not self.config_path.exists():
            raise SourceUnavailable(f"XHS keyword config missing: {self.config_path}", status="degraded")
        data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or []
        keywords = [str(keyword).strip() for keyword in data if str(keyword).strip()]
        if not keywords:
            raise SourceUnavailable("XHS keyword list is empty", status="degraded")
        return keywords

    @staticmethod
    def parse_html(html: str, keyword: str) -> list[Item]:
        tree = HTMLParser(html)
        items: list[Item] = []
        for node in tree.css(".feeds-container .note-item"):
            title_node = node.css_first(".footer .title")
            cover_link = node.css_first("a.cover")
            if not title_node or not cover_link or not cover_link.attributes.get("href"):
                continue
            title = " ".join(title_node.text(separator=" ", strip=True).split())
            if not title:
                continue
            image = node.css_first(".cover img")
            thumbnail = None
            if image:
                thumbnail = image.attributes.get("src") or image.attributes.get("data-src")
            if not thumbnail:
                style = cover_link.attributes.get("style", "")
                match = re.search(r"url\(['\"]?([^'\")]+)", style)
                thumbnail = match.group(1) if match else None
            author = node.css_first(".name")
            like = node.css_first(".like-active") or node.css_first(".count")
            like_text = like.text(strip=True) if like else None
            items.append(Item(
                source="xiaohongshu", rank=0, title=title, title_zh=title,
                url=urljoin("https://www.xiaohongshu.com", cover_link.attributes["href"]),
                hot_value=like_text, thumbnail=thumbnail,
                extra={
                    "keyword": keyword, "author": author.text(strip=True) if author else None,
                    "likes": parse_like_count(like_text), "status": "keyword-radar",
                },
            ))
        items.sort(key=lambda item: int(item.extra["likes"]), reverse=True)
        for index, item in enumerate(items[:15], 1):
            item.rank = index
        return items[:15]

    async def fetch(self) -> list[Item]:
        keywords = self.load_keywords()
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise SourceUnavailable("Playwright is not installed", status="degraded") from exc
        items: list[Item] = []
        errors: list[str] = []
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": 1440, "height": 1200})
                for keyword in keywords:
                    try:
                        url = f"https://www.xiaohongshu.com/search_result?keyword={quote(keyword)}&source=web_explore_feed"
                        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                        await page.wait_for_selector(".note-item", timeout=30_000)
                        items.extend(self.parse_html(await page.content(), keyword))
                    except Exception as exc:
                        errors.append(f"{keyword}: {exc}")
                await browser.close()
        except Exception as exc:
            errors.append(str(exc))
        if not items:
            raise SourceUnavailable("XHS keyword radar failed: " + "; ".join(errors), status="degraded")
        return items


def parse_like_count(value: str | None) -> int:
    if not value:
        return 0
    cleaned = value.strip().lower().replace(",", "")
    try:
        if cleaned.endswith("万"):
            return int(float(cleaned[:-1]) * 10_000)
        if cleaned.endswith("k"):
            return int(float(cleaned[:-1]) * 1_000)
        return int(float(cleaned))
    except ValueError:
        return 0
