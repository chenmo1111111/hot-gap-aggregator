from __future__ import annotations

from datetime import UTC, datetime

from app.collectors.base import BaseCollector
from app.models import Item


class BilibiliCollector(BaseCollector):
    source = "bilibili"
    endpoint = "https://api.bilibili.com/x/web-interface/popular"

    @staticmethod
    def parse(payload: dict) -> list[Item]:
        rows = payload.get("data", {}).get("list", [])
        items: list[Item] = []
        for index, row in enumerate(rows, 1):
            title = str(row.get("title") or "").strip()
            bvid = row.get("bvid")
            if not title or not bvid:
                continue
            items.append(Item(
                source="bilibili",
                rank=index,
                title=title,
                title_zh=title,
                url=row.get("short_link_v2") or f"https://www.bilibili.com/video/{bvid}",
                hot_value=str(row.get("stat", {}).get("view")) if row.get("stat", {}).get("view") is not None else None,
                thumbnail=row.get("pic"),
                published_at=datetime.fromtimestamp(row["pubdate"], UTC).isoformat() if row.get("pubdate") else None,
                extra={"bvid": bvid, "owner": row.get("owner", {}).get("name")},
            ))
        return items[:30]

    async def fetch(self) -> list[Item]:
        response = await self.request(self.endpoint, params={"ps": 30, "pn": 1})
        return self.parse(response.json())
