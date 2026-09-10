from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import yaml
from dotenv import load_dotenv

from app.collectors.base import SourceUnavailable
from app.notify import notify_subsidy_alert
from app.store.database import Database


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc
CHINA_TZ = timezone(timedelta(hours=8))
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
DETAIL_URL_PATTERN = re.compile(
    r"(?P<url>(?:https?://[^\s\"'<>]+)?/rule/detail/[^/?#\s\"'<>]+/[^/?#\s\"'<>]+)"
)
ARTICLE_SELECTORS = (
    "article", "[class*='rule-detail']", "[class*='detail-content']",
    "[class*='article-content']", "[class*='detail']", "[class*='content']",
)
COOKIE_EXPIRED_MESSAGE = (
    "小红书规则监控 cookie 失效：请用 Cookie-Editor 重新导出 JSON，并在 Console "
    "复制 document.cookie，更新 XHS_SCHOOL_COOKIE_JSON / XHS_SCHOOL_COOKIE_DOC。"
)
Notifier = Callable[[dict[str, Any]], Awaitable[dict[str, str]]]
ShopRefresher = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ImpactAnalyzer = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]
ExternalDocsRefresher = Callable[[list[str], dict[str, Any]], Awaitable[list[dict[str, Any]]]]


class XhsCookieInvalid(SourceUnavailable):
    pass


def _normalise_text(value: str) -> str:
    return " ".join(value.split())


def parse_published_date(value: str) -> date:
    value = value.strip()
    for pattern in ("%Y年%m月%d日", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"invalid XHS rule published date: {value}")


def extract_rule_text_window(text: str) -> str:
    """Extract a stable rule span from body text when no content container exists."""
    content = _normalise_text(text)
    announced = ANNOUNCED_PATTERN.search(content)
    revised_matches = list(REVISED_PATTERN.finditer(content))
    if not announced or not revised_matches:
        raise ValueError("rule content container/date boundary not found")
    revised = revised_matches[-1]
    start = min(announced.start(), revised.start())
    end = max(announced.end(), revised.end())
    if end - start < 40:
        raise ValueError("rule content boundary is too short")
    return content[start:end]


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
            "name": name,
            "value": value,
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
        boundary = max(content.rfind("\n", 0, match.start()), content.rfind("日", 0, match.start()))
        prefix_area = content[boundary + 1:match.start()]
        prefix_match = re.search(r"关于(?:修订|新增|废止|征求意见|意见征集)[^《\n]{0,80}$", prefix_area)
        prefix = _normalise_text(prefix_match.group(0) if prefix_match else "")
        haystack = prefix or list_name
        kind = (
            "修订" if "修订" in haystack else
            "新增" if "新增" in haystack else
            "意见征集" if any(value in haystack for value in ("意见", "征集")) else list_name
        )
        seen.add(key)
        rows.append({"title": title, "date": date_text, "prefix": prefix, "kind": kind})
    return rows


def extract_external_links(text: str) -> list[str]:
    return list(dict.fromkeys(
        link.rstrip(".,") for link in re.findall(r"https?://[^\s<>\"'，。]+", text)
    ))


def extract_article_metadata(text: str) -> dict[str, Any]:
    content = _normalise_text(text)
    announced = ANNOUNCED_PATTERN.search(content)
    if not announced:
        raise ValueError("rule announcement/effective dates not found in content container")
    revised = REVISED_PATTERN.search(content)
    document = DOCUMENT_PATTERN.search(content)
    return {
        "announced_at": announced.group(1),
        "effective_at": announced.group(2),
        "revised_at": revised.group(2) if revised else "",
        "document_url": document.group(0).rstrip(".,") if document else "",
        "content_text": content,
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "external_links": extract_external_links(content),
    }


def detail_url_from_text(value: str, base_url: str = "https://school.xiaohongshu.com/") -> str:
    match = DETAIL_URL_PATTERN.search(value or "")
    if not match:
        return ""
    return urljoin(base_url, match.group("url")).split("?", 1)[0]


class XhsRuleWatcher:
    def __init__(
        self,
        database: Database,
        config_path: str | Path | None = None,
        *,
        notifier: Notifier | None = None,
        alerts_path: str | Path | None = None,
        shop_refresher: ShopRefresher | None = None,
        impact_analyzer: ImpactAnalyzer | None = None,
        external_docs_refresher: ExternalDocsRefresher | None = None,
        today_provider: Callable[[], date] | None = None,
    ) -> None:
        self.database = database
        self.config_path = Path(config_path or os.getenv("XHS_RULE_WATCH_CONFIG", "config/xhs_rule_watch.yaml"))
        self.notifier = notifier or notify_subsidy_alert
        self._custom_notifier = notifier is not None
        self.shop_refresher = shop_refresher
        self.impact_analyzer = impact_analyzer
        self.external_docs_refresher = external_docs_refresher
        self.today_provider = today_provider or (lambda: datetime.now(CHINA_TZ).date())
        self._runtime_config: dict[str, Any] = {}
        default_data_dir = Path(os.getenv("SERVER_SITE_DATA_DIR", "public/data"))
        self.alerts_path = Path(alerts_path) if alerts_path else default_data_dir / "alerts.json"

    def load_config(self) -> dict[str, Any]:
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("xhs rule watch config must be a mapping")
        return raw

    async def run(self) -> dict[str, Any]:
        config = self.load_config()
        self._runtime_config = config
        today = self.today_provider()
        keywords = [str(value).casefold() for value in config.get("title_keywords", []) if str(value).strip()]
        try:
            rules, list_reports, lists_complete = await self._scrape_rule_lists(config, keywords)
        except XhsCookieInvalid as exc:
            delivered = await self._notify_cookie_failure(str(exc))
            return {"status": "degraded", "error": str(exc), "cookie_alert": "sent" if delivered else "degraded"}
        except SourceUnavailable as exc:
            return {"status": "degraded", "error": str(exc)}

        unique_rules = list({(row["rule_id"], row["published_at"]): row for row in rules}.values())
        keys = [(row["rule_id"], row["published_at"]) for row in unique_rules]
        last_run = self.database.get_xhs_rule_last_successful_run()
        if last_run is None:
            self.database.mark_xhs_rule_notifications(keys)
            if lists_complete:
                self.database.save_xhs_rule_last_successful_run(today.isoformat())
            LOGGER.info("xhs rule watcher cold-start baseline: %s rules", len(keys))
            return {
                "status": "ok" if lists_complete else "degraded",
                "mode": "baseline",
                "list_pages": list_reports,
                "rules_seen": len(keys),
                "notified": 0,
            }

        try:
            window_start = date.fromisoformat(last_run) - timedelta(days=2)
        except ValueError:
            LOGGER.warning("invalid xhs last_successful_run=%s; using today", last_run)
            window_start = today
        eligible = [
            row for row in unique_rules
            if window_start <= parse_published_date(row["published_at"]) <= today
        ]
        unseen = self.database.unseen_xhs_rule_notifications([
            (row["rule_id"], row["published_at"]) for row in eligible
        ])
        pending = [row for row in eligible if (row["rule_id"], row["published_at"]) in unseen]

        reports: list[dict[str, Any]] = []
        all_processed = lists_complete
        for rule in sorted(pending, key=lambda row: row["published_at"]):
            try:
                report = await self._process_published_rule(rule)
            except Exception as exc:
                LOGGER.warning("xhs published rule processing degraded (%s): %s", rule["title"], str(exc).splitlines()[0])
                report = {"name": rule["title"], "status": "degraded", "error": str(exc).splitlines()[0]}
            reports.append(report)
            if report.get("status") == "pushed":
                self.database.mark_xhs_rule_notifications([(rule["rule_id"], rule["published_at"])])
            else:
                all_processed = False

        if all_processed:
            self.database.save_xhs_rule_last_successful_run(today.isoformat())
        return {
            "status": "ok" if all_processed else "degraded",
            "mode": "notify",
            "list_pages": list_reports,
            "last_successful_run": self.database.get_xhs_rule_last_successful_run(),
            "window_start": window_start.isoformat(),
            "rules_seen": len(unique_rules),
            "eligible": len(eligible),
            "pending": len(pending),
            "notified": sum(report.get("status") == "pushed" for report in reports),
            "rules": reports,
        }

    async def _scrape_rule_lists(
        self, config: dict[str, Any], keywords: list[str],
    ) -> tuple[list[dict[str, str]], list[dict[str, Any]], bool]:
        cookies = self._cookies()
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise SourceUnavailable("Playwright is not installed", status="degraded") from exc

        known_urls = {
            _normalise_text(str(row.get("name") or "")): str(row.get("url") or "")
            for row in config.get("watch_articles", []) if isinstance(row, dict)
        }
        output: list[dict[str, str]] = []
        reports: list[dict[str, Any]] = []
        complete = True
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent=DESKTOP_USER_AGENT, viewport={"width": 1440, "height": 900},
                )
                await context.add_cookies(cookies)
                page = await context.new_page()
                for target in config.get("list_pages", []):
                    if not isinstance(target, dict) or not str(target.get("url") or "").startswith("https://"):
                        continue
                    name, list_url = str(target.get("name") or "规则列表"), str(target["url"])
                    try:
                        text = await self._page_text(page, list_url)
                        rows = [
                            row for row in parse_rule_list_text(text, name)
                            if not keywords or any(keyword in row["title"].casefold() for keyword in keywords)
                        ]
                        unresolved = 0
                        for row in rows:
                            detail_url = known_urls.get(row["title"], "")
                            if not detail_url:
                                detail_url = await self._resolve_rule_detail_url(page, row["title"], list_url)
                            if not detail_url:
                                unresolved += 1
                                LOGGER.warning("xhs rule detail id unresolved: %s (%s)", row["title"], name)
                                continue
                            output.append({
                                **row,
                                "published_at": parse_published_date(row["date"]).isoformat(),
                                "rule_id": self._rule_id(detail_url),
                                "url": detail_url,
                                "list_name": name,
                                "list_url": list_url,
                            })
                        reports.append({
                            "name": name, "status": "ok" if not unresolved else "degraded",
                            "item_count": len(rows), "resolved": len(rows) - unresolved,
                            "unresolved": unresolved,
                        })
                        complete = complete and unresolved == 0
                    except XhsCookieInvalid:
                        raise
                    except Exception as exc:
                        complete = False
                        reports.append({"name": name, "status": "degraded", "error": str(exc).splitlines()[0]})
                        LOGGER.warning("xhs rule list degraded (%s): %s", name, str(exc).splitlines()[0])
            finally:
                await browser.close()
        if not reports:
            raise SourceUnavailable("all XHS rule list pages failed", status="degraded")
        return output, reports, complete

    async def _resolve_rule_detail_url(self, page: Any, title: str, list_url: str) -> str:
        locator = page.get_by_text(title, exact=False).first
        if await locator.count() == 0:
            return ""
        try:
            evidence = await locator.evaluate("""element => {
                const values = [];
                let node = element;
                for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
                    values.push(node.outerHTML.slice(0, 12000));
                    for (const name of node.getAttributeNames()) values.push(node.getAttribute(name) || '');
                }
                return values.join('\n');
            }""")
            resolved = detail_url_from_text(str(evidence), list_url)
            if resolved:
                return resolved
        except Exception:
            pass

        context = page.context
        previous_pages = set(context.pages)
        original_url = page.url
        try:
            await locator.click(timeout=5_000)
            await page.wait_for_timeout(1_000)
            candidates = [page.url]
            new_pages = [candidate for candidate in context.pages if candidate not in previous_pages]
            candidates.extend(candidate.url for candidate in new_pages)
            for candidate in candidates:
                resolved = detail_url_from_text(candidate, list_url)
                if resolved:
                    return resolved
        except Exception as exc:
            LOGGER.debug("xhs rule detail click resolution failed (%s): %s", title, exc)
        finally:
            for candidate in context.pages:
                if candidate not in previous_pages:
                    await candidate.close()
            if page.url != original_url:
                await self._page_text(page, list_url)
        return ""

    async def _scrape(self, config: dict[str, Any]) -> dict[str, list[tuple[Any, ...]]]:
        cookies = self._cookies()
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise SourceUnavailable("Playwright is not installed", status="degraded") from exc

        output: dict[str, list[tuple[Any, ...]]] = {"lists": [], "articles": []}
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent=DESKTOP_USER_AGENT, viewport={"width": 1440, "height": 900},
                )
                await context.add_cookies(cookies)
                page = await context.new_page()
                for target in config.get("watch_articles", []):
                    if not isinstance(target, dict) or not str(target.get("url") or "").startswith("https://"):
                        continue
                    try:
                        text = await self._page_article_text(page, str(target["url"]))
                        output["articles"].append((target, text))
                    except XhsCookieInvalid:
                        raise
                    except Exception as exc:
                        LOGGER.warning("xhs rule article degraded (%s): %s", target.get("name"), str(exc).splitlines()[0])
                        output["articles"].append((target, None, str(exc).splitlines()[0]))
            finally:
                await browser.close()
        if not output["articles"]:
            raise SourceUnavailable("all XHS rule article pages failed", status="degraded")
        return output

    def _cookies(self) -> list[dict[str, Any]]:
        try:
            cookies = merge_xhs_cookies(
                os.getenv("XHS_SCHOOL_COOKIE_JSON", ""), os.getenv("XHS_SCHOOL_COOKIE_DOC", ""),
            )
        except ValueError as exc:
            raise XhsCookieInvalid(str(exc), status="degraded") from exc
        if not cookies:
            raise XhsCookieInvalid("missing XHS school cookies", status="degraded")
        return cookies

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

    @classmethod
    async def _page_article_text(cls, page: Any, url: str) -> str:
        body_text = await cls._page_text(page, url)
        candidates: list[str] = []
        for selector in ARTICLE_SELECTORS:
            locator = page.locator(selector)
            for index in range(min(await locator.count(), 20)):
                try:
                    text = _normalise_text(await locator.nth(index).inner_text())
                except Exception:
                    continue
                if len(text) >= 80 and ANNOUNCED_PATTERN.search(text):
                    candidates.append(text)
        if candidates:
            complete = [
                text for text in candidates
                if len(text) >= 120 and (REVISED_PATTERN.search(text) or DOCUMENT_PATTERN.search(text))
            ]
            content = min(complete or candidates, key=len)
            extract_article_metadata(content)
            return content
        content = extract_rule_text_window(body_text)
        extract_article_metadata(content)
        return content

    async def _process_published_rule(self, rule: dict[str, str]) -> dict[str, Any]:
        article = {"name": rule["title"], "url": rule["url"]}
        scraped = await self._scrape({"watch_articles": [article]})
        capture = scraped["articles"][0] if scraped.get("articles") else None
        if not capture or capture[1] is None:
            raise RuntimeError("published rule content capture failed")
        current = extract_article_metadata(capture[1])
        self._save_article(rule["url"], current)
        title, summary, priority, analysis, shop_snapshot = await self._run_impact_analysis(
            article, current, published_at=rule["published_at"], manual=False,
        )
        alert = self._alert(
            title=title,
            url=rule["url"],
            kind="新规则",
            summary=summary,
            priority=priority,
            extra={
                "impact_analysis": analysis,
                "shop_item_count": len(shop_snapshot.get("items") or []),
                "rule_id": rule["rule_id"],
                "published_at": rule["published_at"],
            },
        )
        stable_key = f"xhs-rule:published:{rule['rule_id']}:{rule['published_at']}"
        alert["id"] = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:20]
        if await self._deliver(alert):
            return {"name": rule["title"], "status": "pushed", "summary": summary}
        return {"name": rule["title"], "status": "notification-degraded"}

    async def _run_impact_analysis(
        self,
        article: dict[str, Any],
        current: dict[str, Any],
        *,
        published_at: str,
        manual: bool,
    ) -> tuple[str, str, str, dict[str, Any], dict[str, Any]]:
        impact_config = self._runtime_config.get("impact_analysis") or {}
        if not isinstance(impact_config, dict):
            impact_config = {}
        shop_manage_url = str(
            impact_config.get("shop_manage_url")
            or "https://ark.xiaohongshu.com/app-item/list/shelf"
        )
        try:
            if self.shop_refresher is not None:
                shop_snapshot = await self.shop_refresher(impact_config)
            else:
                from app.watchers.xhs_shop_items import XhsShopItems

                snapshot_path = os.getenv("XHS_SHOP_ITEMS_PATH", "data/xhs_shop_items.json")
                shop_snapshot = await XhsShopItems(snapshot_path).refresh(shop_manage_url)
        except Exception as exc:
            LOGGER.warning("xhs shop refresh failed; continuing with empty stale snapshot: %s", exc)
            shop_snapshot = {"stale": True, "items": [], "error": str(exc).splitlines()[0]}

        rule = {
            "name": str(article.get("name") or "规则正文"),
            "col_id": self._rule_id(str(article.get("url") or "")),
            "url": str(article.get("url") or ""),
            "published_at": published_at,
            "announced_at": current["announced_at"],
            "effective_at": current["effective_at"],
            "content_text": current["content_text"],
            "external_links": list(current.get("external_links") or []),
            "manual_trigger": manual,
        }
        external_config = impact_config.get("external_documents") or {}
        if not isinstance(external_config, dict):
            external_config = {}
        if rule["external_links"] and external_config.get("enabled", False):
            focus_terms = [
                value.strip()
                for item in shop_snapshot.get("items") or []
                if isinstance(item, dict)
                for value in re.split(
                    r"[>／/、|·]+",
                    f"{item.get('category_path') or ''}>{item.get('title') or ''}",
                )
                if value.strip()
            ]
            settings = dict(external_config)
            settings["focus_terms"] = focus_terms[:80]
            try:
                if self.external_docs_refresher is not None:
                    external_documents = await self.external_docs_refresher(
                        rule["external_links"], settings,
                    )
                else:
                    from app.watchers.xhs_external_docs import XhsExternalDocs

                    snapshot_path = os.getenv(
                        "XHS_EXTERNAL_DOCS_PATH",
                        str(external_config.get("snapshot_path") or "data/xhs_external_docs.json"),
                    )
                    collector = XhsExternalDocs(
                        snapshot_path,
                        timeout_seconds=float(external_config.get("timeout_seconds") or 45),
                        max_chars=int(external_config.get("max_chars") or 80_000),
                    )
                    external_documents = await collector.refresh(
                        rule["external_links"], focus_terms=focus_terms,
                    )
                rule["external_documents"] = external_documents
            except Exception as exc:
                LOGGER.warning(
                    "xhs external document refresh failed; continuing with manual review: %s",
                    type(exc).__name__,
                )
                rule["external_documents"] = []
        try:
            if self.impact_analyzer is not None:
                analysis = await self.impact_analyzer(rule, shop_snapshot)
            else:
                from app.pipeline.xhs_rule_impact import DeepSeekRuleImpactAnalyzer

                analysis = await DeepSeekRuleImpactAnalyzer().analyze(rule, shop_snapshot)
        except Exception as exc:
            LOGGER.warning("xhs rule impact analysis failed; pushing review fallback: %s", exc)
            from app.pipeline.xhs_rule_impact import (
                fallback_analysis,
                normalise_analysis,
                unresolved_external_links,
            )

            analysis = normalise_analysis(
                fallback_analysis(current["effective_at"]),
                shop_snapshot,
                unresolved_external_links(rule),
                current["effective_at"],
            )

        item_count = len(shop_snapshot.get("items") or [])
        urgent_days = int(impact_config.get("urgent_within_days") or 7)
        if manual:
            from app.pipeline.xhs_rule_impact import format_impact_notification

            title, summary, priority = format_impact_notification(
                rule["name"], published_at or "未标注", published_at or "未标注",
                rule["effective_at"] or "未标注", analysis, item_count,
                urgent_within_days=urgent_days, manual=True,
            )
        else:
            from app.pipeline.xhs_rule_impact import format_new_rule_notification

            title, summary, priority = format_new_rule_notification(
                rule["name"], published_at or "未标注", rule["effective_at"] or "未标注",
                analysis, item_count, urgent_within_days=urgent_days,
            )
        return title, summary, priority, analysis, shop_snapshot

    async def analyze(self, rule_id: str, *, push: bool = False) -> dict[str, Any]:
        """Analyze a current rule; only --push delivers a notification."""
        config = self.load_config()
        self._runtime_config = config
        normalised = rule_id.strip().strip("/")
        article = next((
            row for row in config.get("watch_articles", [])
            if isinstance(row, dict) and self._rule_id(str(row.get("url") or "")) == normalised
        ), None)
        if article is None:
            article = {
                "name": f"规则 {normalised}",
                "url": f"https://school.xiaohongshu.com/rule/detail/{normalised}",
            }
        scraped = await self._scrape({"watch_articles": [article]})
        capture = scraped.get("articles", [])[0] if scraped.get("articles") else None
        if not capture or capture[1] is None:
            raise RuntimeError("manual rule content capture failed")
        current = extract_article_metadata(capture[1])
        published_at = current["announced_at"] or self.today_provider().isoformat()
        title, summary, priority, analysis, shop_snapshot = await self._run_impact_analysis(
            article, current, published_at=published_at, manual=True,
        )
        delivered = False
        if push:
            alert = self._alert(
                title=title, url=str(article["url"]), kind="手动触发·规则影响分析",
                summary=summary, priority=priority,
                extra={"impact_analysis": analysis, "shop_item_count": len(shop_snapshot.get("items") or [])},
            )
            delivered = await self._deliver(alert)
        return {
            "status": "pushed" if push and delivered else "notification-degraded" if push else "analyzed",
            "manual": True,
            "push_requested": push,
            "rule_id": normalised,
            "shop_item_count": len(shop_snapshot.get("items") or []),
            "shop_snapshot_stale": bool(shop_snapshot.get("stale")),
            "analysis": analysis,
        }

    @staticmethod
    def _rule_id(url: str) -> str:
        path = urlparse(url).path
        match = re.search(r"/rule/detail/([^/]+/[^/]+)", path)
        return match.group(1) if match else path.rstrip("/").rsplit("/", 1)[-1]

    def _save_article(self, url: str, current: dict[str, Any]) -> None:
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

    async def _deliver(self, alert: dict[str, Any]) -> bool:
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

    def _write_alert(self, alert: dict[str, Any]) -> None:
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
    def _alert(
        title: str, url: str, kind: str, summary: str, priority: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        created = datetime.now(UTC).isoformat(timespec="seconds")
        tag = f"【小红书规则·{kind}】"
        message = f"{tag}{title}｜{summary}｜{url}｜{created}"
        alert: dict[str, Any] = {
            "id": hashlib.sha256(message.encode("utf-8")).hexdigest()[:20],
            "tag": tag, "category_label": "小红书规则", "region": kind,
            "type": kind, "title": title, "url": url, "date": created[:10],
            "summary": summary, "message": message, "priority": priority,
            "created_at": created,
        }
        if extra:
            alert.update(extra)
        return alert


async def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Watch newly published Xiaohongshu rules and analyze shop impact")
    parser.add_argument("--analyze", metavar="COL/ID", help="analyze a current rule without sending by default")
    parser.add_argument("--push", action="store_true", help="push a manual analysis to Feishu and alerts.json")
    arguments = parser.parse_args(argv)
    if arguments.push and not arguments.analyze:
        parser.error("--push requires --analyze COL/ID")
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    database = Database(os.getenv("SERVER_DATABASE", "data/server.db"))
    try:
        watcher = XhsRuleWatcher(database)
        result = await watcher.analyze(arguments.analyze, push=arguments.push) if arguments.analyze else await watcher.run()
        output = json.dumps({"event": "xhs_rule_watch_finished", **result}, ensure_ascii=False)
        if arguments.analyze:
            print(output)
        else:
            LOGGER.info(output)
    finally:
        database.close()


if __name__ == "__main__":
    asyncio.run(main())
