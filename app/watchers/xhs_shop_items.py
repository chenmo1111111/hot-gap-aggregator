from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.watchers.xhs_rule_watch import DESKTOP_USER_AGENT, merge_xhs_cookies


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc
SHOP_LIST_API = "https://ark.xiaohongshu.com/api/edith/product/search_item_v2"
DEFAULT_SHOP_MANAGE_URL = "https://ark.xiaohongshu.com/app-item/list/shelf"
PAGE_SIZE = 50


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return ""


def _category_path(raw: dict[str, Any]) -> str:
    direct = _first(
        raw, "category_path", "categoryPath", "category_name_path", "categoryNamePath",
        "category_names", "categoryNames", "category_name", "categoryName",
    )
    if isinstance(direct, list):
        return " > ".join(str(value) for value in direct if str(value).strip())
    if direct:
        return str(direct)

    candidates: list[str] = []
    for key, value in raw.items():
        lowered = key.casefold()
        if "categor" not in lowered and "类目" not in key:
            continue
        if isinstance(value, dict):
            nested = _first(value, "path", "name_path", "full_name", "name", "category_name")
            if nested:
                candidates.append(str(nested))
        elif isinstance(value, list):
            for row in value:
                if isinstance(row, dict):
                    nested = _first(row, "name", "category_name", "label")
                    if nested:
                        candidates.append(str(nested))
    if candidates:
        return " > ".join(dict.fromkeys(candidates))
    return str(_first(raw, "category_id", "categoryId"))


def _product_type(raw: dict[str, Any], title: str, category: str) -> str:
    explicit = _first(
        raw, "product_type", "productType", "item_type_name", "itemTypeName",
        "goods_type_name", "goodsTypeName", "virtual_type", "virtualType",
    )
    if explicit:
        return str(explicit)
    virtual_flag = _first(raw, "is_virtual", "isVirtual", "virtual_item", "virtualItem")
    if virtual_flag is True or str(virtual_flag).casefold() in {"true", "1", "virtual"}:
        return "虚拟"
    haystack = f"{title} {category}".casefold()
    if any(word in haystack for word in (
        "虚拟", "电子", "课程", "题库", "激活码", "充值", "会员", "软件", "数字",
    )):
        return "虚拟"
    return "实物"


def _price(raw: dict[str, Any]) -> str:
    value = _first(
        raw, "sale_price", "salePrice", "price", "min_price", "minPrice",
        "lowest_price", "lowestPrice", "max_price", "maxPrice",
    )
    if isinstance(value, dict):
        value = _first(value, "price", "amount", "value")
    if value == "":
        variants = raw.get("variants") or raw.get("skus") or []
        if isinstance(variants, list) and variants and isinstance(variants[0], dict):
            value = _first(variants[0], "sale_price", "salePrice", "price")
    return str(value)


def normalise_shop_item(raw: dict[str, Any]) -> dict[str, str]:
    title = str(_first(raw, "item_name", "itemName", "title", "name", "description"))
    category = _category_path(raw)
    buyable = raw.get("buyable")
    status = str(_first(raw, "sale_status_name", "saleStatusName", "status_name", "statusName"))
    if not status:
        status = "在售" if buyable is not False else "下架"
    return {
        "item_id": str(_first(raw, "item_id", "itemId", "goods_id", "goodsId", "id")),
        "title": title,
        "category_path": category,
        "product_type": _product_type(raw, title, category),
        "status": status,
        "price": _price(raw),
    }


def extract_items_response(payload: Any) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(payload, dict):
        raise ValueError("shop API response is not an object")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("shop API response has no data object")
    rows = data.get("items")
    if not isinstance(rows, list):
        raise ValueError("shop API response has no items array")
    total = data.get("total", len(rows))
    try:
        total_int = int(total)
    except (TypeError, ValueError):
        total_int = len(rows)
    return [row for row in rows if isinstance(row, dict)], total_int


def page_payload(template: dict[str, Any], page_no: int, page_size: int = PAGE_SIZE) -> dict[str, Any]:
    payload = copy.deepcopy(template)
    payload["page_no"] = page_no
    payload["page_size"] = page_size
    return payload


class XhsShopItems:
    def __init__(
        self, snapshot_path: str | Path = "data/xhs_shop_items.json", *, timeout_seconds: float = 30,
    ) -> None:
        self.snapshot_path = Path(snapshot_path)
        self.timeout_seconds = timeout_seconds

    async def refresh(self, shop_manage_url: str = DEFAULT_SHOP_MANAGE_URL) -> dict[str, Any]:
        try:
            snapshot = await self._fetch_all(shop_manage_url or DEFAULT_SHOP_MANAGE_URL)
            self._write(snapshot)
            return snapshot
        except Exception as exc:
            message = str(exc).splitlines()[0][:240]
            LOGGER.warning("xhs shop items refresh degraded: %s", message)
            snapshot = self._read_previous()
            snapshot.update({
                "stale": True,
                "last_attempt_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "error": message,
            })
            self._write(snapshot)
            return snapshot

    async def _fetch_all(self, shop_manage_url: str) -> dict[str, Any]:
        cookies = merge_xhs_cookies(
            os.getenv("XHS_SCHOOL_COOKIE_JSON", ""), os.getenv("XHS_SCHOOL_COOKIE_DOC", ""),
        )
        if not cookies:
            raise RuntimeError("missing XHS cookies")
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed") from exc

        capture: dict[str, Any] = {}
        captured = asyncio.Event()
        shop_name = ""
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent=DESKTOP_USER_AGENT, viewport={"width": 1440, "height": 900},
                )
                await context.add_cookies(cookies)
                page = await context.new_page()

                async def on_response(response: Any) -> None:
                    nonlocal shop_name
                    if response.url.endswith("/api/edith/seller/info/v2"):
                        try:
                            seller = (await response.json()).get("data") or {}
                            shop_name = str(seller.get("seller_name") or seller.get("name") or "")
                        except Exception:
                            pass
                    if response.url != SHOP_LIST_API or captured.is_set():
                        return
                    try:
                        body = response.request.post_data_json
                        first_payload = await response.json()
                        if isinstance(body, dict):
                            capture.update({
                                "url": response.url,
                                "body": body,
                                "headers": await response.request.all_headers(),
                                "first_payload": first_payload,
                            })
                            captured.set()
                    except Exception as exc:
                        LOGGER.debug("xhs shop API capture skipped: %s", exc)

                page.on("response", on_response)
                await page.goto(shop_manage_url, wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(3_000)
                if "login" in page.url.casefold() or "oia" in page.url.casefold():
                    raise RuntimeError("XHS shop cookie expired")
                try:
                    await asyncio.wait_for(captured.wait(), timeout=self.timeout_seconds)
                except TimeoutError as exc:
                    raise RuntimeError("shop list API request was not observed") from exc
                browser_cookies = await context.cookies("https://ark.xiaohongshu.com/")
            finally:
                await browser.close()

        first_rows, total = extract_items_response(capture["first_payload"])
        template = capture["body"]
        first_page = int(template.get("page_no") or 1)
        collected = list(first_rows)
        seen = {str(_first(row, "item_id", "itemId", "id")) for row in first_rows}
        if len(collected) < total:
            http_cookies = httpx.Cookies()
            for cookie in browser_cookies:
                http_cookies.set(
                    cookie["name"], cookie["value"], domain=cookie.get("domain"), path=cookie.get("path", "/"),
                )
            headers = {
                key: value for key, value in capture["headers"].items()
                if key.casefold() not in {
                    "cookie", "content-length", "host", "connection", "accept-encoding",
                } and not key.casefold().startswith("sec-")
            }
            async with httpx.AsyncClient(
                cookies=http_cookies, headers=headers, timeout=self.timeout_seconds, follow_redirects=True,
            ) as client:
                page_no = first_page + 1
                while len(collected) < total:
                    response = await client.post(capture["url"], json=page_payload(template, page_no))
                    response.raise_for_status()
                    rows, response_total = extract_items_response(response.json())
                    total = max(total, response_total)
                    if not rows:
                        break
                    added = 0
                    for row in rows:
                        row_id = str(_first(row, "item_id", "itemId", "id"))
                        if row_id and row_id in seen:
                            continue
                        seen.add(row_id)
                        collected.append(row)
                        added += 1
                    if not added:
                        break
                    page_no += 1
                    if page_no - first_page > 200:
                        raise RuntimeError("shop list pagination exceeded safety limit")

        items = [normalise_shop_item(row) for row in collected if row.get("buyable") is not False]
        items = [row for row in items if row["item_id"] and row["title"]]
        return {
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "shop_name": shop_name,
            "stale": False,
            "source_url": shop_manage_url,
            "api_url": capture["url"],
            "total": len(items),
            "items": items,
        }

    def _read_previous(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload.setdefault("items", [])
                payload.setdefault("shop_name", "")
                return payload
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return {"fetched_at": "", "shop_name": "", "items": [], "total": 0}

    def _write(self, payload: dict[str, Any]) -> None:
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.snapshot_path.with_suffix(self.snapshot_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.snapshot_path)
