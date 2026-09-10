"""Capture Xiaozhaoya rows through its already authenticated Vue application.

The wire response is intentionally not decoded here.  Xiaozhaoya decrypts it
inside the page, and this module reads only the resulting ``Home`` component
state from a dedicated persistent Playwright profile.  A failed run never
replaces the previous snapshot.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import random
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv

from app.collectors.xiaozhaoya import split_records


LOGGER = logging.getLogger(__name__)
HOME_URL = "https://www.xiaozhaoya.com/home"
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
CAPTCHA_SELECTORS = (
    "iframe[src*='captcha' i]",
    "iframe[src*='aliyun' i]",
    "iframe[src*='alicloud' i]",
    "iframe[src*='aliyuncs' i]",
    "#aliyunCaptcha",
    "[class*='captcha' i]",
)
CAPTCHA_URL_PATTERN = re.compile(r"captcha|verify|aliyun|risk", re.I)
DATE_PATTERN = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})")


class CaptureError(RuntimeError):
    """Raised when a new snapshot cannot safely replace the old one."""


class LoginRequired(CaptureError):
    """Raised when the persistent browser profile no longer has a session."""


class CaptchaBlocked(CaptureError):
    """Raised when Xiaozhaoya appears to have challenged the browser."""


def _text(value: object) -> str:
    return str(value or "").strip()


def _record_id(row: Mapping[str, Any]) -> str:
    return _text(row.get("recruitmentId"))


def _as_date(value: object) -> date | None:
    match = DATE_PATTERN.search(_text(value))
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def page_is_older_than(rows: Sequence[Mapping[str, Any]], cutoff: date) -> bool:
    """Return true only when every row has a valid update date before cutoff."""
    parsed = [_as_date(row.get("updateDate")) for row in rows]
    return bool(parsed) and all(value is not None and value < cutoff for value in parsed)


class IncrementalStopPolicy:
    """Track the two documented incremental stopping conditions."""

    def __init__(self, known_ids: set[str], *, cutoff: date) -> None:
        self.known_ids = known_ids
        self.cutoff = cutoff
        self.consecutive_known_pages = 0

    def observe(self, rows: Sequence[Mapping[str, Any]]) -> str | None:
        if page_is_older_than(rows, self.cutoff):
            return "older_than_cutoff"
        identifiers = [_record_id(row) for row in rows]
        all_known = bool(identifiers) and all(
            identifier and identifier in self.known_ids for identifier in identifiers
        )
        self.consecutive_known_pages = self.consecutive_known_pages + 1 if all_known else 0
        if self.consecutive_known_pages >= 2:
            return "two_known_pages"
        return None


def _read_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise CaptureError(f"旧校招鸭快照无法读取：{path}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise CaptureError(f"旧校招鸭快照格式无效：{path}")
    return value


def build_snapshot(
    captured_rows: Sequence[Mapping[str, Any]],
    *,
    total: int,
    previous: Mapping[str, Any] | None,
    full: bool,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a full durable snapshot from a complete or incremental capture."""
    combined: dict[str, dict[str, Any]] = {}
    if not full and previous:
        for row in previous.get("items") or []:
            if isinstance(row, Mapping) and (identifier := _record_id(row)):
                combined[identifier] = dict(row)
    for row in captured_rows:
        if identifier := _record_id(row):
            combined[identifier] = dict(row)
    items = sorted(
        combined.values(),
        key=lambda row: (_text(row.get("updateDate")), _record_id(row)),
        reverse=True,
    )
    previous_count = len(previous.get("items") or []) if previous else 0
    minimum = math.ceil(previous_count * 0.8)
    if previous_count and len(items) < minimum:
        raise CaptureError(
            f"安全校验失败：本次 {len(items)} 条，少于上次 {previous_count} 条的 80%；旧快照已保留"
        )
    if not items:
        raise CaptureError("校招鸭没有返回任何记录；旧快照已保留")
    return {
        "generated_at": generated_at or datetime.now().astimezone().isoformat(),
        "total": int(total or (previous or {}).get("total") or len(items)),
        "items": items,
    }


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _notify(title: str, message: str) -> None:
    providers: list[tuple[str, dict[str, Any]]] = []
    if webhook := os.getenv("FEISHU_WEBHOOK", "").strip():
        providers.append(
            (webhook, {"msg_type": "text", "content": {"text": f"{title}\n{message}"}})
        )
    if bark_url := os.getenv("BARK_URL", "").strip():
        providers.append(
            (bark_url, {"title": title, "body": message, "group": "hot-gap"})
        )
    for url, body in providers:
        try:
            httpx.post(url, json=body, timeout=10, follow_redirects=True).raise_for_status()
        except httpx.HTTPError as exc:
            LOGGER.warning("failed to send Xiaozhaoya alert: %s", exc)


HOME_STATE_SCRIPT = """
() => {
  const root = document.querySelector('#app')?.__vue__;
  const stack = root ? [root] : [];
  const seen = new Set();
  while (stack.length) {
    const component = stack.pop();
    if (!component || seen.has(component)) continue;
    seen.add(component);
    if (component.$options?.name === 'Home') {
      return {
        found: true,
        currentPage: Number(component.currentPage ?? component.$data?.currentPage ?? 0),
        total: Number(component.total ?? component.$data?.total ?? 0),
        rows: JSON.parse(JSON.stringify(component.recruitmentList ?? component.$data?.recruitmentList ?? [])),
      };
    }
    for (const child of component.$children || []) stack.push(child);
  }
  return {found: false, currentPage: 0, total: 0, rows: []};
}
"""

FETCH_PAGE_SCRIPT = """
async (pageNumber) => {
  const root = document.querySelector('#app')?.__vue__;
  const stack = root ? [root] : [];
  const seen = new Set();
  let home = null;
  while (stack.length) {
    const component = stack.pop();
    if (!component || seen.has(component)) continue;
    seen.add(component);
    if (component.$options?.name === 'Home') { home = component; break; }
    for (const child of component.$children || []) stack.push(child);
  }
  if (!home || typeof home.fetchData !== 'function') {
    throw new Error('Xiaozhaoya Home component was not found');
  }
  home.currentPage = pageNumber;
  if (home.$data) home.$data.currentPage = pageNumber;
  await home.fetchData();
  return true;
}
"""


async def _captcha_visible(page: Any) -> bool:
    if CAPTCHA_URL_PATTERN.search(page.url):
        return True
    for selector in CAPTCHA_SELECTORS:
        locator = page.locator(selector)
        if await locator.count() and await locator.first.is_visible():
            return True
    return any(CAPTCHA_URL_PATTERN.search(frame.url or "") for frame in page.frames[1:])


async def _wait_for_home(page: Any, *, login: bool) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + (600 if login else 90)
    while asyncio.get_running_loop().time() < deadline:
        if await _captcha_visible(page):
            raise CaptchaBlocked("校招鸭抓取被验证码拦截")
        state = await page.evaluate(HOME_STATE_SCRIPT)
        if state.get("found") and isinstance(state.get("rows"), list) and state["rows"]:
            return state
        await page.wait_for_timeout(1_000)
    host = (urlsplit(page.url).hostname or "").casefold()
    if "login" in page.url.casefold() or host != "www.xiaozhaoya.com":
        raise LoginRequired("校招鸭登录态已失效，请用 --login 重新登录")
    raise LoginRequired("未找到有数据的校招鸭 Home 组件，请用 --login 重新登录")


async def _fetch_page(
    page: Any, page_number: int, *, previous_signature: tuple[str, ...], delay: float
) -> dict[str, Any]:
    try:
        await asyncio.wait_for(page.evaluate(FETCH_PAGE_SCRIPT, page_number), timeout=60)
    except TimeoutError as exc:
        raise CaptchaBlocked("校招鸭 recruitmentList 长时间未更新") from exc
    except Exception as exc:
        if await _captcha_visible(page):
            raise CaptchaBlocked("校招鸭抓取被验证码拦截") from exc
        raise CaptureError(f"校招鸭第 {page_number} 页 fetchData 失败") from exc
    await page.wait_for_timeout(round(delay * 1_000))
    deadline = asyncio.get_running_loop().time() + 25
    while asyncio.get_running_loop().time() < deadline:
        if await _captcha_visible(page):
            raise CaptchaBlocked("校招鸭抓取被验证码拦截")
        state = await page.evaluate(HOME_STATE_SCRIPT)
        rows = state.get("rows") if isinstance(state, dict) else None
        signature = tuple(
            _record_id(row) for row in rows or [] if isinstance(row, Mapping)
        )
        if (
            state.get("found")
            and int(state.get("currentPage") or 0) == page_number
            and rows
            and signature != previous_signature
        ):
            return state
        await page.wait_for_timeout(1_000)
    raise CaptchaBlocked("校招鸭 recruitmentList 长时间未更新")


async def capture(
    *,
    output_path: Path,
    previous_path: Path,
    profile_dir: Path,
    screenshot_dir: Path,
    full: bool = False,
    login: bool = False,
    headed: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise CaptureError("缺少 Playwright，请先安装 requirements.txt") from exc

    previous = _read_snapshot(previous_path)
    known_ids = {
        _record_id(row)
        for row in (previous or {}).get("items") or []
        if isinstance(row, Mapping) and _record_id(row)
    }
    profile_dir.mkdir(parents=True, exist_ok=True)
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    screenshot_path = screenshot_dir / f"xiaozhaoya_{stamp}.png"
    captured: list[dict[str, Any]] = []
    stop_reason = "all_pages"

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=not (login or headed),
            viewport={"width": 1600, "height": 1000},
            user_agent=DESKTOP_UA,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=120_000)
            if login:
                print("请在弹出的专用浏览器中登录校招鸭；检测到首页数据后会自动继续。")
            initial = await _wait_for_home(page, login=login)
            if login:
                print("已检测到校招鸭登录态，开始抓取。")
            await page.screenshot(path=str(screenshot_path), full_page=False)
            total = int(initial.get("total") or 0)
            total_pages = max(1, math.ceil(total / 12))
            policy = IncrementalStopPolicy(
                known_ids, cutoff=date.today() - timedelta(days=3)
            )
            previous_signature: tuple[str, ...] = ()
            for page_number in range(1, total_pages + 1):
                if page_number == 1:
                    state = initial
                else:
                    delay = 3.0 if full else random.uniform(1.5, 2.5)
                    state = await _fetch_page(
                        page,
                        page_number,
                        previous_signature=previous_signature,
                        delay=delay,
                    )
                rows = [dict(row) for row in state.get("rows") or [] if isinstance(row, Mapping)]
                if not rows:
                    stop_reason = "empty_page"
                    break
                captured.extend(rows)
                previous_signature = tuple(_record_id(row) for row in rows)
                if not full and (reason := policy.observe(rows)):
                    stop_reason = reason
                    break
            payload = build_snapshot(
                captured, total=total, previous=previous, full=full
            )
            _atomic_write(output_path, payload)
            return payload, {
                "pages": math.ceil(len(captured) / 12),
                "captured_items": len({_record_id(row) for row in captured}),
                "stop_reason": stop_reason,
            }
        finally:
            await context.close()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Capture Xiaozhaoya with a persistent Playwright profile"
    )
    parser.add_argument("--output", default=None)
    parser.add_argument("--previous", default=None)
    parser.add_argument(
        "--profile-dir", default=os.getenv("WANQING_PROFILE_DIR", ".wanqing-browser")
    )
    parser.add_argument(
        "--state-dir", default=os.getenv("WANQING_STATE_DIR", ".wanqing-state")
    )
    parser.add_argument("--full", action="store_true", help="manually backfill every page")
    parser.add_argument("--login", action="store_true", help="open the profile for one-time login")
    parser.add_argument("--headed", action="store_true")
    arguments = parser.parse_args(argv)

    state_dir = Path(arguments.state_dir)
    output_path = Path(arguments.output) if arguments.output else state_dir / "xiaozhaoya.json"
    previous_path = Path(arguments.previous) if arguments.previous else state_dir / "xiaozhaoya.json"
    try:
        payload, status = asyncio.run(
            capture(
                output_path=output_path,
                previous_path=previous_path,
                profile_dir=Path(arguments.profile_dir),
                screenshot_dir=state_dir / "screenshots",
                full=arguments.full,
                login=arguments.login,
                headed=arguments.headed,
            )
        )
        qiuzhao, gongkao = split_records(payload["items"])
        print(
            json.dumps(
                {
                    "event": "xiaozhaoya_captured",
                    **status,
                    "snapshot_items": len(payload["items"]),
                    "site_total": payload["total"],
                    "qiuzhao": len(qiuzhao),
                    "gongkao": len(gongkao),
                    "captcha_blocked": False,
                    "output": str(output_path),
                },
                ensure_ascii=False,
            )
        )
        return 0
    except CaptchaBlocked as exc:
        LOGGER.exception("Xiaozhaoya capture blocked; previous snapshot preserved")
        _notify("校招鸭抓取被验证码拦截", str(exc))
        return 1
    except LoginRequired as exc:
        LOGGER.exception("Xiaozhaoya login required; previous snapshot preserved")
        _notify("校招鸭需要重新登录", str(exc))
        return 1
    except Exception as exc:
        LOGGER.exception("Xiaozhaoya capture failed; previous snapshot preserved")
        _notify("校招鸭抓取失败", str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
