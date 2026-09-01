from __future__ import annotations

from urllib.parse import quote

from app.collectors.base import BaseCollector, SourceUnavailable
from app.models import Item


class DouyinCollector(BaseCollector):
    source = "douyin"
    legacy_endpoint = (
        "https://aweme-hl.snssdk.com/aweme/v1/hot/search/list/?detail_list=1&os_api=23&device_type=MI%205s"
        "&device_platform=android&ssmix=a&manifest_version_code=860&dpi=320&version_code=860&version_name=8.6.0"
        "&app_name=aweme&ts=1577932778&openudid=c055533a0591b2dc&device_id=69918538596&resolution=810*1440"
        "&os_version=6.0.1&language=zh&device_brand=Xiaomi&app_type=normal&ac=wifi&update_version_code=8602"
        "&aid=1128&channel=tengxun_new&_rticket=1577932779592"
    )
    web_endpoint = "https://www.douyin.com/aweme/v1/web/hot/search/list/"

    @staticmethod
    def parse(payload: dict) -> list[Item]:
        rows = payload.get("data", {}).get("word_list", [])
        items: list[Item] = []
        for index, row in enumerate(rows, 1):
            title = str(row.get("word") or "").strip()
            if not title:
                continue
            covers = (row.get("word_cover") or {}).get("url_list") or []
            position = row.get("position")
            rank = int(position) if isinstance(position, (int, str)) and str(position).isdigit() and int(position) > 0 else index
            items.append(Item(
                source="douyin", rank=rank, title=title, title_zh=title,
                url=f"https://www.douyin.com/search/{quote(title)}",
                hot_value=str(row.get("hot_value")) if row.get("hot_value") is not None else None,
                thumbnail=covers[0] if covers else None,
                extra={"sentence_id": row.get("sentence_id")},
            ))
        return sorted(items, key=lambda item: item.rank)[:50]

    async def fetch(self) -> list[Item]:
        errors: list[str] = []
        for endpoint in (self.legacy_endpoint, self.web_endpoint):
            try:
                response = await self.request(endpoint, headers={"Referer": "https://www.douyin.com/"})
                items = self.parse(response.json())
                if items:
                    return items
                errors.append(f"{endpoint}: empty word_list")
            except Exception as exc:
                errors.append(str(exc))
        raise SourceUnavailable("; ".join(errors), status="degraded")
