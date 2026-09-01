from __future__ import annotations

from urllib.parse import quote

from app.collectors.base import BaseCollector
from app.models import Item


class WeiboCollector(BaseCollector):
    source = "weibo"
    endpoint = "https://weibo.com/ajax/side/hotSearch"

    @staticmethod
    def parse(payload: dict) -> list[Item]:
        rows = payload.get("data", {}).get("realtime", [])
        items: list[Item] = []
        for row in rows:
            title = str(row.get("word") or "").strip()
            if not title or row.get("is_ad"):
                continue
            items.append(Item(
                source="weibo",
                rank=len(items) + 1,
                title=title,
                title_zh=title,
                url=f"https://s.weibo.com/weibo?q={quote(f'#{title}#')}",
                hot_value=str(row["num"]) if row.get("num") is not None else None,
                extra={"category": row.get("category")},
            ))
        return items[:30]

    async def fetch(self) -> list[Item]:
        response = await self.request(self.endpoint, headers={"Referer": "https://weibo.com/"})
        return self.parse(response.json())

