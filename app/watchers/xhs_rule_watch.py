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

import httpx
import yaml
from dotenv import load_dotenv

from app.collectors.base import SourceUnavailable
from app.notify import notify_subsidy_alert
from app.store.database import Database


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
LIST_PATTERN = re.compile(
    r"《(?P<title>[^》\n]+)》(?:(?!《).){0,500}?"
    r"(?P<date>20\d{2}年\d{2}月\d{2}日)",
    re.S,
)
ANNOUNCED_PATTERN = re.compile(
    r"本规则于\s*([\d-]+)\s*公示\s*[，,]\s*([\d-]+)\s*生效"
)
REVISED_PATTERN = re.compile(
    r"本规则于\s*([\d-]+)\s*首次生效\s*[，,]\s*([\d-]+)\s*修订"
)
DOCUMENT_PATTERN = re.compile(r"https://doc\.weixin\.qq\.com/[^\s<>\"'，。]+")
RULE_CHANGE_PROMPT = """这是小红书电商规则的变化（旧版/新版）。判断是否涉及：虚拟商品/虚拟卡券/电子资源/激活码/知识付费/账号充值/生活娱乐充值/网络工具 类目的：店铺类型限制（个人店、普通企业店能否经营）、类目准入方式（是否改邀约/定准）、新增资质门槛、冻结下架清退规则。命中→一句话说明影响+建议动作；否则→SKIP。

规则名：{name}

旧版：
{old}

新版：
{new}
"""
COOKIE_EXPIRED_MESSAGE = (
    "小红书规则监控 cookie 失效：请用 Cookie-Editor 重新导出 JSON，并在 Console "
    "复制 document.cookie，更新 XHS_SCHOOL_COOKIE_JSON / XHS_SCHOOL_COOKIE_DOC。"
)
Notifier = Callable[[dict[str, str]], Awaitable[dict[str, str]]]
Judge = Callable[[str], Awaitable[str]]


class XhsCookieInvalid(SourceUnavailable):
    pass


def _normalise_text(value: str) -> str:
    return " ".join(value.split())


def merge_xhs_cookies(json_text: str, document_cookie: str) -> list[dict[str, Any]]:
    """Merge Cookie-Editor JSON and document.cookie without logging secrets."""
    try:
        decoded = json.loads(json_text or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("XHS_SCHOOL_COOKIE_JSON is not valid JSON") from exc
    if not isinstance(decoded, list):
        raise ValueError("XHS_SCHOOL_COOKIE_JSON must be a JSON array")

    json_cookies: list[dict[str, Any]] = []
    json_names: set[str] = set()
    for raw in decoded:
        if not isinstance(raw, dict):
            continue
        name, value = str(raw.get("name") or "").strip(), str(raw.get("value") or "")
        if not name:
            continue
        cookie: dict[str, Any] = {
            "name": name, "value": value,
            "domain": str(raw.get("domain") or ".xiaohongshu.com"),
            "path": str(raw.get("path") or "/"),
            "secure": True,
            "httpOnly": bool(raw.get("httpOnly", False)),
        }
        same_site = raw.get("sameSite")
        same_site_map = {
            "strict": "Strict", "lax": "Lax", "none": "None",
            "no_restriction": "None", "unspecified": None,
        }
        if same_site is not None:
            normalised = same_site_map.get(str(same_site).casefold(), str(same_site).title())
            if normalised in {"Strict", "Lax", "None"}:
                cookie["sameSite"] = normalised
        expires = raw.get("expirationDate", raw.get("expires"))
        if expires not in (None, "", 0, -1):
            try:
                cookie["expires"] = int(float(expires))
            except (TypeError, ValueError, OverflowError):
                pass
        json_names.add(name)
        json_cookies.append(cookie)

    doc_cookies: list[dict[str, Any]] = []
    for part in (document_cookie or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if not name or name in json_names:
            continue
        doc_cookies.append({
            "name": name, "value": value, "domain": ".xiaohongshu.com",
            "path": "/", "secure": True,
        })
    return json_cookies + doc_cookies


def parse_rule_list_text(text: str, list_name: str) -> list[dict[str, str]]:
    content = text.split("规则列表", 1)[-1] if "规则列表" in text else text
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in LIST_PATTERN.finditer(content):
        title = _normalise_text(match.group("title"))
        date_text = match.group("date")
        key = title, date_text
        if not title or key in seen:
            continue
        # Prefix sits immediately before the title in the rendered row. Bound
        # the lookup at the preceding line/date so another row cannot leak in.
        boundary = max(content.rfind("\n", 0, match.start()), content.rfind("日", 0, match.start()))
        prefix_area = content[boundary + 1:match.start()]
        prefix_match = re.search(r"关于(?:修订|新增|废止|征求意见|意见征集)[^《\n]{0,80}$", prefix_area)
        prefix = _normalise_text(prefix_match.group(0) if prefix_match else "")
        haystack = prefix or list_name
        kind = "修订" if "修订" in haystack else "新增" if "新增" in haystack else "意见征集" if any(value in haystack for value in ("意见", "征集")) else list_name
        seen.add(key)
        rows.append({"title": title, "date": date_text, "prefix": prefix, "kind": kind})
    return rows


def extract_article_metadata(text: str) -> dict[str, str]:
    content = _normalise_text(text)
    announced = ANNOUNCED_PATTERN.search(content)
    revised = REVISED_PATTERN.search(content)
    document = DOCUMENT_PATTERN.search(content)
    return {
        "announced_at": announced.group(1) if announced else "",
        "effective_at": announced.group(2) if announced else "",
        "revised_at": revised.group(2) if revised else "",
        "document_url": document.group(0).rstrip(".,") if document else "",
        "content_text": content,
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def article_changed(previous: dict[str, Any], current: dict[str, str]) -> bool:
    return any(
        str(previous.get(field) or "") != str(current.get(field) or "")
        for field in ("announced_at", "effective_at", "revised_at", "document_url", "content_hash")
    )


class XhsRuleWatcher:
    def __init__(
        self, database: Database, config_path: str | Path | None = None, *,
        judge: Judge | None = None, notifier: Notifier | None = None,
        alerts_path: str | Path | None = None,
    ) -> None:
        self.database = database
        self.config_path = Path(config_path or os.getenv("XHS_RULE_WATCH_CONFIG", "config/xhs_rule_watch.yaml"))
        self.judge = judge or self._llm_judge
        self.notifier = notifier or notify_subsidy_alert
        self._custom_notifier = notifier is not None
        default_data_dir = Path(os.getenv("SERVER_SITE_DATA_DIR", "public/data"))
        self.alerts_path = Path(alerts_path) if alerts_path else default_data_dir / "alerts.json"

    def load_config(self) -> dict[str, Any]:
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("xhs rule watch config must be a mapping")
        return raw

    async def run(self) -> dict[str, Any]:
        config = self.load_config()
        try:
            scraped = await self._scrape(config)
        except XhsCookieInvalid as exc:
            delivered = await self._notify_cookie_failure(str(exc))
            return {"status": "degraded", "error": str(exc), "cookie_alert": "sent" if delivered else "degraded"}
        except SourceUnavailable as exc:
            return {"status": "degraded", "error": str(exc)}

        keywords = [str(value).casefold() for value in config.get("title_keywords", []) if str(value).strip()]
        list_reports = []
        for page, text in scraped["lists"]:
            list_reports.append(await self._process_list(page, text, keywords))
        article_reports = []
        for article, text in scraped["articles"]:
            article_reports.append(await self._process_article(article, text))
        return {"status": "ok", "list_pages": list_reports, "watch_articles": article_reports}

    async def _scrape(self, config: dict[str, Any]) -> dict[str, list[tuple[dict[str, Any], str]]]:
        try:
            cookies = merge_xhs_cookies(
                os.getenv("XHS_SCHOOL_COOKIE_JSON", ""), os.getenv("XHS_SCHOOL_COOKIE_DOC", ""),
            )
        except ValueError as exc:
            raise XhsCookieInvalid(str(exc), status="degraded") from exc
        if not cookies:
            raise XhsCookieInvalid("missing XHS school cookies", status="degraded")
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise SourceUnavailable("Playwright is not installed", status="degraded") from exc

        output: dict[str, list[tuple[dict[str, Any], str]]] = {"lists": [], "articles": []}
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent=DESKTOP_USER_AGENT, viewport={"width": 1440, "height": 900},
                )
                await context.add_cookies(cookies)
                page = await context.new_page()
                targets = [("lists", row) for row in config.get("list_pages", [])] + [
                    ("articles", row) for row in config.get("watch_articles", [])
                ]
                for section, target in targets:
                    if not isinstance(target, dict) or not str(target.get("url") or "").startswith("https://"):
                        continue
                    try:
                        text = await self._page_text(page, str(target["url"]))
                        output[section].append((target, text))
                    except SourceUnavailable:
                        raise
                    except Exception as exc:
                        LOGGER.warning("xhs rule page degraded (%s): %s", target.get("name"), str(exc).splitlines()[0])
            finally:
                await browser.close()
        if not output["lists"] and not output["articles"]:
            raise SourceUnavailable("all XHS rule pages failed", status="degraded")
        return output

    @staticmethod
    async def _page_text(page: Any, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                await page.goto(url, wait_until="networkidle", timeout=30_000)
                await page.wait_for_timeout(2_500)
                text = await page.inner_text("body")
                if "oia" in page.url.casefold() or ("下载" in text and "app" in text.casefold()):
                    raise XhsCookieInvalid("XHS school cookie expired or redirected to app", status="degraded")
                if "上岸上岸" not in text and "您好" not in text:
                    raise XhsCookieInvalid("XHS school login verification failed", status="degraded")
                return text
            except XhsCookieInvalid:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"XHS rule page failed after retries: {last_error}")

    async def _process_list(self, page: dict[str, Any], text: str, keywords: list[str]) -> dict[str, Any]:
        name, url = str(page.get("name") or "规则列表"), str(page.get("url") or "")
        candidates = [
            row for row in parse_rule_list_text(text, name)
            if any(keyword in row["title"].casefold() for keyword in keywords)
        ]
        event_pairs = [
            (f"xhs-rule:list:{name}:{row['title']}:{row['date']}", row) for row in candidates
        ]
        unseen = self.database.unseen_push_events([key for key, _ in event_pairs])
        pushed = 0
        for event_key, row in event_pairs:
            if event_key not in unseen:
                continue
            alert = self._alert(
                title=f"《{row['title']}》", url=url, kind=row["kind"],
                summary=f"{row['date']} · {row['prefix'] or name}", priority="normal",
            )
            if await self._deliver(alert):
                self.database.mark_push_events([event_key])
                pushed += 1
        return {"name": name, "status": "pushed" if pushed else "unchanged", "item_count": len(candidates), "pushed": pushed}

    async def _process_article(self, article: dict[str, Any], text: str) -> dict[str, Any]:
        name, url = str(article.get("name") or "规则正文"), str(article.get("url") or "")
        current = extract_article_metadata(text)
        previous = self.database.get_xhs_rule_snapshot(url)
        if previous is None:
            self._save_article(url, current)
            return {"name": name, "status": "baseline"}
        if not article_changed(previous, current):
            self._save_article(url, current)
            return {"name": name, "status": "unchanged"}

        version_hash = hashlib.sha256(json.dumps(
            {field: current[field] for field in ("announced_at", "effective_at", "revised_at", "document_url", "content_hash")},
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")).hexdigest()
        event_key = f"xhs-rule:article:{url}:{version_hash[:16]}"
        if event_key not in self.database.unseen_push_events([event_key]):
            self._save_article(url, current)
            return {"name": name, "status": "already-pushed"}
        prompt = RULE_CHANGE_PROMPT.format(
            name=name, old=str(previous.get("content_text") or "")[-8_000:],
            new=current["content_text"][-8_000:],
        )
        try:
            verdict = _normalise_text(await self.judge(prompt))
        except Exception as exc:
            LOGGER.warning("xhs rule model degraded (%s): %s", name, exc)
            verdict = "模型判断失败，请人工核对规则变化并检查相关类目。"
        if not verdict or verdict.casefold() == "skip":
            self._save_article(url, current)
            return {"name": name, "status": "changed-skip"}
        alert = self._alert(
            title=name, url=url, kind="规则更新", summary=verdict, priority="highest",
        )
        if await self._deliver(alert):
            self.database.mark_push_events([event_key])
            self._save_article(url, current)
            return {"name": name, "status": "pushed", "summary": verdict}
        return {"name": name, "status": "notification-degraded"}

    def _save_article(self, url: str, current: dict[str, str]) -> None:
        self.database.save_xhs_rule_snapshot(
            url, announced_at=current["announced_at"], effective_at=current["effective_at"],
            revised_at=current["revised_at"], document_url=current["document_url"],
            content_hash=current["content_hash"], content_text=current["content_text"],
        )

    async def _notify_cookie_failure(self, reason: str) -> bool:
        fingerprint = hashlib.sha256((
            os.getenv("XHS_SCHOOL_COOKIE_JSON", "") + os.getenv("XHS_SCHOOL_COOKIE_DOC", "")
        ).encode("utf-8")).hexdigest()[:16]
        event_key = f"xhs-rule:cookie-invalid:{fingerprint}"
        if event_key not in self.database.unseen_push_events([event_key]):
            return True
        alert = self._alert(
            title="Cookie 已失效", url="https://school.xiaohongshu.com/", kind="Cookie 失效",
            summary=f"{COOKIE_EXPIRED_MESSAGE}（{reason}）", priority="highest",
        )
        if await self._deliver(alert):
            self.database.mark_push_events([event_key])
            return True
        return False

    async def _deliver(self, alert: dict[str, str]) -> bool:
        site_written = True
        try:
            self._write_alert(alert)
        except Exception as exc:
            site_written = False
            LOGGER.warning("XHS rule alert site write failed: %s", exc)

        if self._custom_notifier or os.getenv("FEISHU_WEBHOOK") or os.getenv("BARK_URL"):
            status = await self.notifier(alert)
            return site_written and any(value == "ok" for value in status.values())
        return site_written

    def _write_alert(self, alert: dict[str, str]) -> None:
        self.alerts_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = json.loads(self.alerts_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            payload = {"generated_at": "", "items": []}
        items = [row for row in payload.get("items", []) if row.get("id") != alert["id"]]
        payload = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "items": [alert, *items][:100],
        }
        temporary = self.alerts_path.with_suffix(self.alerts_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.alerts_path)

    @staticmethod
    def _alert(title: str, url: str, kind: str, summary: str, priority: str) -> dict[str, str]:
        created = datetime.now(UTC).isoformat(timespec="seconds")
        tag = f"【小红书规则·{kind}】"
        message = f"{tag}{title}｜{summary}｜{url}｜{created}"
        return {
            "id": hashlib.sha256(message.encode("utf-8")).hexdigest()[:20],
            "tag": tag, "category_label": "小红书规则", "region": kind,
            "type": kind, "title": title, "url": url, "date": created[:10],
            "summary": summary, "message": message, "priority": priority,
            "created_at": created,
        }

    async def _llm_judge(self, prompt: str) -> str:
        zhipu_key, deepseek_key = os.getenv("ZHIPU_API_KEY"), os.getenv("DEEPSEEK_API_KEY")
        key = zhipu_key or deepseek_key
        if not key:
            raise RuntimeError("ZHIPU_API_KEY or DEEPSEEK_API_KEY is required")
        zhipu = bool(zhipu_key)
        endpoint = "https://open.bigmodel.cn/api/paas/v4/chat/completions" if zhipu else os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/") + "/chat/completions"
        model = "glm-4-flash" if zhipu else os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
            for attempt in range(3):
                try:
                    response = await client.post(
                        endpoint, headers={"Authorization": f"Bearer {key}"},
                        json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1},
                    )
                    response.raise_for_status()
                    return str(response.json()["choices"][0]["message"]["content"])
                except (httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
                    last_error = exc
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"xhs rule model failed: {last_error}")


async def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    database = Database(os.getenv("SERVER_DATABASE", "data/server.db"))
    try:
        result = await XhsRuleWatcher(database).run()
        LOGGER.info(json.dumps({"event": "xhs_rule_watch_finished", **result}, ensure_ascii=False))
    finally:
        database.close()


if __name__ == "__main__":
    asyncio.run(main())
