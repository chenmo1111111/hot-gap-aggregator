"""Keep Gongkao announcement links browser-safe."""

from __future__ import annotations

import re
from typing import Any, Mapping


FENBI_INTERNAL_ARTICLE = re.compile(
    r"^https?://hera-webapp\.fenbi\.com/api/(?:website/)?article/detail"
    r"\?(?:[^#]*&)?id=(\d+)(?:[&#]|$)",
    re.IGNORECASE,
)


def fenbi_public_article_url(article_id: object) -> str:
    value = str(article_id or "").strip()
    if not value.isdigit():
        return ""
    return f"https://www.fenbi.com/page/fenxiaozhaokaodetail/3/1239/{value}"


def browser_safe_announcement_url(
    value: object, extra: Mapping[str, Any] | None = None,
) -> str:
    """Replace Fenbi internal API endpoints with browser-facing links."""

    url = str(value or "").strip()
    metadata = extra if isinstance(extra, Mapping) else {}
    match = FENBI_INTERNAL_ARTICLE.match(url)
    if not match:
        return url
    source_url = str(metadata.get("source_url") or "").strip()
    if source_url.startswith(("http://", "https://")):
        return source_url
    return fenbi_public_article_url(metadata.get("id") or match.group(1))
