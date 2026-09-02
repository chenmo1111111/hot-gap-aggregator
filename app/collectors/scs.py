from __future__ import annotations

import re
from datetime import UTC, datetime

from app.collectors.base import BaseCollector, SourceUnavailable
from app.models import Item


SCS_CONSTANTS_URL = "http://dl.scs.gov.cn/pp/gkweb/core/web/ui/js/core/core-constant.js"
SCS_ARTICLES_URL = "http://dl.scs.gov.cn/api/gkhome/article/{exam_id}"
SCS_DETAIL_BASE = "http://bm.scs.gov.cn/pp/gkweb/core/web/ui/business/article/articledetail.html"
IMPORTANT_MARKERS = ("公告", "报名", "大纲")


class SCSCollector(BaseCollector):
    """Official national civil-service notices, intended to run from a mainland server."""

    source = "gongkao"
    timeout = 20.0

    @staticmethod
    def discover_exam_id(javascript: str) -> str:
        match = re.search(r"neu\.hb01Id\s*=.*?\?\s*[\"'][^\"']*[\"']\s*:\s*[\"']([^\"']+)", javascript)
        if not match:
            match = re.search(r"neu\.hb01Id\s*=\s*[\"']([^\"']+)", javascript)
        return match.group(1) if match else ""

    @staticmethod
    def parse(payload: dict) -> list[Item]:
        items: list[Item] = []
        seen: set[str] = set()
        for group in payload.get("articleGroupList", []):
            if not isinstance(group, dict):
                continue
            for row in group.get("articleList", []):
                if not isinstance(row, dict):
                    continue
                article_id = str(row.get("id") or "").strip()
                title = " ".join(str(row.get("articleTitle") or "").split())
                if not article_id or article_id in seen or not any(marker in title for marker in IMPORTANT_MARKERS):
                    continue
                seen.add(article_id)
                column_id = str(row.get("cmsArticleColumnId") or "")
                parent_id = str(row.get("parentColumnId") or "")
                external = str(row.get("articleUrl") or "").strip()
                url = external if str(row.get("articleType")) == "2" and external else (
                    f"{SCS_DETAIL_BASE}?ArticleId={article_id}&id={parent_id}&eid={column_id}"
                )
                items.append(Item(
                    source="gongkao", rank=0, title=title, title_zh=title, url=url,
                    hot_value="国家公务员局", published_at=_timestamp(row.get("pstrtime")),
                    extra={
                        "id": f"scs:{article_id}", "sub": "announcement", "subsource": "scs",
                        "province": "全国", "exam_type": "国考", "article_id": article_id,
                        "column_id": column_id, "parent_column_id": parent_id,
                    },
                ))
        items.sort(key=lambda item: item.published_at or "", reverse=True)
        for rank, item in enumerate(items, 1):
            item.rank = rank
        return items

    async def fetch(self) -> list[Item]:
        constants = await self.request(SCS_CONSTANTS_URL, headers={"Accept": "application/javascript"})
        exam_id = self.discover_exam_id(constants.text)
        if not exam_id:
            raise SourceUnavailable("SCS current exam id was not found", status="degraded")
        response = await self.request(SCS_ARTICLES_URL.format(exam_id=exam_id), headers={"Accept": "application/json"})
        items = self.parse(response.json())
        if not items:
            raise SourceUnavailable("SCS returned no important notices", status="degraded")
        return items


def _timestamp(value: object) -> str | None:
    if value in (None, ""):
        return None
    try:
        stamp = int(value)
        if stamp > 10_000_000_000:
            stamp //= 1000
        return datetime.fromtimestamp(stamp, UTC).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value)
