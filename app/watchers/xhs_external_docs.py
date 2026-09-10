from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
ALLOWED_DOCUMENT_HOSTS = {"doc.weixin.qq.com", "docs.qq.com"}
ALLOWED_COOKIE_SUFFIXES = (".qq.com", ".weixin.qq.com")
SAFE_QUERY_FIELDS = {"tab"}
DEFAULT_FOCUS_TERMS = (
    "类目", "可售", "店铺类型", "个人店", "企业店", "旗舰店", "专卖店", "专营店",
    "定向准入", "邀约", "资质", "电子资源", "数字商品", "虚拟商品", "教育", "课程",
    "题库", "学习资料", "激活码", "知识付费", "下线", "禁售", "清退",
)
DATA_PATH_HINTS = (
    "/sheet/", "/doc/", "/cgi-bin/online_docs/", "/doc/info", "clientvars", "snapshot",
    "workbook", "collab", "load", "sheet", "pad",
)
IGNORED_PATH_HINTS = (
    "/report/", "/components/", "/assets/", "/login", "/user_info", "/favicon",
)
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
TAG_PATTERN = re.compile(r"<[^>]+>")
SPACE_PATTERN = re.compile(r"[ \t\r\f\v]+")


def safe_document_url(url: str) -> str:
    """Remove access-capability parameters before logging, caching or model use."""
    parts = urlsplit(str(url or "").strip())
    safe_query = [
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.casefold() in SAFE_QUERY_FIELDS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(safe_query), ""))


def document_identity(url: str) -> str:
    parts = urlsplit(str(url or "").strip())
    host = (parts.hostname or "").casefold()
    path = re.sub(r"/+", "/", parts.path).rstrip("/")
    tab = next((
        value for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.casefold() == "tab"
    ), "")
    return f"{host}{path}?tab={tab}" if tab else f"{host}{path}"


def validate_document_url(url: str) -> None:
    parts = urlsplit(str(url or "").strip())
    if parts.scheme != "https" or (parts.hostname or "").casefold() not in ALLOWED_DOCUMENT_HOSTS:
        raise ValueError("unsupported external document host")


def merge_tencent_doc_cookies(json_text: str) -> list[dict[str, Any]]:
    """Convert a Cookie-Editor JSON export to a restricted Playwright cookie list."""
    try:
        decoded = json.loads(json_text or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("TENCENT_DOC_COOKIE_JSON is not valid JSON") from exc
    if not isinstance(decoded, list):
        raise ValueError("TENCENT_DOC_COOKIE_JSON must be a JSON array")

    cookies: list[dict[str, Any]] = []
    for raw in decoded:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        value = str(raw.get("value") or "")
        domain = str(raw.get("domain") or "").strip().casefold()
        canonical_domain = domain if domain.startswith(".") else f".{domain}"
        if not name or not domain or not canonical_domain.endswith(ALLOWED_COOKIE_SUFFIXES):
            continue
        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
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
        cookies.append(cookie)
    return cookies


def _clean_candidate(value: str) -> str:
    text = html.unescape(TAG_PATTERN.sub(" ", str(value or "")))
    lines = []
    for raw_line in text.splitlines():
        line = SPACE_PATTERN.sub(" ", raw_line).strip()
        if not line or line.startswith(("http://", "https://")):
            continue
        if len(line) > 8_000:
            line = line[:8_000]
        lines.append(line)
    return "\n".join(lines)


def extract_payload_strings(payload: Any, *, max_depth: int = 12) -> list[str]:
    """Extract human-readable values from observed Tencent Docs JSON responses."""
    found: list[str] = []
    seen: set[str] = set()

    def visit(value: Any, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(value, dict):
            for child in value.values():
                visit(child, depth + 1)
            return
        if isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
            return
        if not isinstance(value, str):
            return
        stripped = value.strip()
        if stripped.startswith(("{", "[")) and len(stripped) <= 5_000_000:
            try:
                nested = json.loads(stripped)
            except json.JSONDecodeError:
                pass
            else:
                visit(nested, depth + 1)
                return
        cleaned = _clean_candidate(value)
        if len(cleaned) < 2 or cleaned in seen:
            return
        if not re.search(r"[\u3400-\u9fff]", cleaned) and len(cleaned) < 20:
            return
        seen.add(cleaned)
        found.append(cleaned)

    visit(payload, 0)
    return found


def compact_document_text(
    candidates: list[str], *, max_chars: int = 80_000, focus_terms: list[str] | None = None,
) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        for raw_line in candidate.splitlines():
            line = SPACE_PATTERN.sub(" ", raw_line).strip()
            if len(line) < 2 or line in seen:
                continue
            seen.add(line)
            lines.append(line)
    if not lines:
        return ""
    if sum(len(line) + 1 for line in lines) <= max_chars:
        return "\n".join(lines)

    terms = [term.casefold() for term in [*DEFAULT_FOCUS_TERMS, *(focus_terms or [])] if term]
    selected_indexes = set(range(min(80, len(lines))))
    for index, line in enumerate(lines):
        lowered = line.casefold()
        if any(term in lowered for term in terms):
            selected_indexes.update(range(max(0, index - 3), min(len(lines), index + 4)))
    selected = [lines[index] for index in sorted(selected_indexes)]
    output: list[str] = []
    size = 0
    for line in selected:
        if size + len(line) + 1 > max_chars:
            break
        output.append(line)
        size += len(line) + 1
    return "\n".join(output)


def _response_is_interesting(url: str, content_type: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    path = parts.path.casefold()
    if host not in ALLOWED_DOCUMENT_HOSTS or any(hint in path for hint in IGNORED_PATH_HINTS):
        return False
    lowered_type = content_type.casefold()
    return (
        "json" in lowered_type
        or "octet-stream" in lowered_type
        or any(hint in path for hint in DATA_PATH_HINTS)
    )


def _safe_failure(exc: Exception) -> str:
    lowered = str(exc).casefold()
    if "login" in lowered or "cookie" in lowered:
        return "Tencent Docs login required or cookie expired"
    if "timeout" in lowered:
        return "Tencent Docs request timed out"
    return f"Tencent document fetch failed ({type(exc).__name__})"


class XhsExternalDocs:
    def __init__(
        self,
        snapshot_path: str | Path = "data/xhs_external_docs.json",
        *,
        timeout_seconds: float = 45,
        max_chars: int = 80_000,
    ) -> None:
        self.snapshot_path = Path(snapshot_path)
        self.timeout_seconds = timeout_seconds
        self.max_chars = max_chars

    async def refresh(
        self, urls: list[str], *, focus_terms: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        previous = self._read_previous()
        results: list[dict[str, Any]] = []
        for url in dict.fromkeys(str(value).strip() for value in urls if str(value).strip()):
            identity = document_identity(url)
            try:
                validate_document_url(url)
                document = await self._fetch_document(url, focus_terms=focus_terms or [])
            except Exception as exc:
                error = _safe_failure(exc)
                LOGGER.warning(
                    "xhs external document refresh degraded: document=%s error=%s",
                    hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12], error,
                )
                document = dict(previous.get(identity) or {})
                document.update({
                    "source_id": identity,
                    "source_url": safe_document_url(url),
                    "stale": True,
                    "last_attempt_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "error": error,
                })
                document.setdefault("title", "")
                document.setdefault("content_text", "")
                document.setdefault("content_hash", "")
            results.append(document)
        self._write(results)
        return results

    async def _fetch_document(self, url: str, *, focus_terms: list[str]) -> dict[str, Any]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed") from exc

        cookies = merge_tencent_doc_cookies(os.getenv("TENCENT_DOC_COOKIE_JSON", ""))
        response_candidates: list[str] = []
        pending: set[asyncio.Task[None]] = set()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent=DESKTOP_USER_AGENT, viewport={"width": 1440, "height": 900},
                )
                if cookies:
                    await context.add_cookies(cookies)
                page = await context.new_page()

                async def inspect_response(response: Any) -> None:
                    content_type = response.headers.get("content-type", "")
                    if not _response_is_interesting(response.url, content_type):
                        return
                    try:
                        body = await response.body()
                    except Exception:
                        return
                    if not body or len(body) > 20_000_000:
                        return
                    payload: Any | None = None
                    try:
                        payload = json.loads(body)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        try:
                            decoded = body.decode("utf-8", errors="ignore")
                        except Exception:
                            return
                        if re.search(r"[\u3400-\u9fff]", decoded):
                            response_candidates.append(decoded)
                    if payload is not None:
                        response_candidates.extend(extract_payload_strings(payload))

                def schedule_response(response: Any) -> None:
                    task = asyncio.create_task(inspect_response(response))
                    pending.add(task)
                    task.add_done_callback(pending.discard)

                page.on("response", schedule_response)
                await page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout_seconds * 1000))
                await page.wait_for_timeout(10_000)
                for _ in range(6):
                    await page.keyboard.press("End")
                    await page.wait_for_timeout(250)
                if pending:
                    await asyncio.gather(*list(pending), return_exceptions=True)

                frame_texts: list[str] = []
                for frame in page.frames:
                    try:
                        text = await frame.locator("body").inner_text(timeout=3_000)
                    except Exception:
                        continue
                    if text:
                        frame_texts.append(text)
                title = (await page.title()).strip()
                combined_for_login = " ".join(frame_texts).casefold()
                final_host = (urlsplit(page.url).hostname or "").casefold()
                if (
                    final_host in {"login.work.weixin.qq.com", "open.work.weixin.qq.com"}
                    or "企业微信登录" in combined_for_login
                    or "微信扫码登录" in combined_for_login
                ):
                    raise RuntimeError("Tencent Docs login required or cookie expired")
                content = compact_document_text(
                    [*frame_texts, *response_candidates],
                    max_chars=self.max_chars,
                    focus_terms=focus_terms,
                )
                meaningful = len(re.findall(r"[\u3400-\u9fff]", content))
                if meaningful < 40:
                    raise RuntimeError("Tencent document content was not observed")
            finally:
                await browser.close()

        return {
            "source_id": document_identity(url),
            "source_url": safe_document_url(url),
            "title": title,
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "stale": False,
            "content_text": content,
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }

    def _read_previous(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        documents = payload.get("documents") if isinstance(payload, dict) else None
        if not isinstance(documents, list):
            return {}
        return {
            str(row.get("source_id")): row
            for row in documents if isinstance(row, dict) and row.get("source_id")
        }

    def _write(self, documents: list[dict[str, Any]]) -> None:
        payload = {
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "documents": documents,
        }
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.snapshot_path.with_suffix(self.snapshot_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.snapshot_path)
