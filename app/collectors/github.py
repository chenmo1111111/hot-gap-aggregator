from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from app.collectors.base import BaseCollector
from app.models import Item


class GitHubCollector(BaseCollector):
    source = "github"
    endpoint = "https://github.com/trending?since=daily"

    @staticmethod
    def parse(html: str) -> list[Item]:
        tree = HTMLParser(html)
        items: list[Item] = []
        for index, article in enumerate(tree.css("article.Box-row"), 1):
            link = article.css_first("h2 a")
            if not link or not link.attributes.get("href"):
                continue
            repo = link.attributes["href"].strip("/")
            description_node = article.css_first("p")
            language_node = article.css_first("[itemprop='programmingLanguage']")
            description = description_node.text(strip=True) if description_node else ""
            text = " ".join(article.text(separator=" ", strip=True).split())
            match = re.search(r"([\d,]+)\s+stars?\s+today", text, flags=re.I)
            hot_value = f"{match.group(1)} stars today" if match else None
            items.append(Item(
                source="github",
                rank=index,
                title=repo,
                title_zh=repo,
                url=f"https://github.com/{repo}",
                hot_value=hot_value,
                extra={
                    "description": description,
                    "language": language_node.text(strip=True) if language_node else None,
                },
            ))
        return items[:30]

    async def fetch(self) -> list[Item]:
        response = await self.request(self.endpoint, headers={"Accept": "text/html"})
        return self.parse(response.text)

