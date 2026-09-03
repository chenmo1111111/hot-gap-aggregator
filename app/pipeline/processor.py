from __future__ import annotations

import os

from app.models import Item
from app.pipeline.language import is_chinese
from app.pipeline.translator import Translator


async def process_items(items: list[Item], translator: Translator) -> list[Item]:
    translation_inputs: list[str] = []
    for item in items:
        translate_title = item.source in {"youtube", "papers"} or (item.source in {"telegram", "feed"} and item.extra.get("translate", item.source == "telegram"))
        if translate_title and not is_chinese(item.title):
            translation_inputs.append(item.title)
        description = str(item.extra.get("description") or "").strip()
        translate_description = item.source in {"youtube", "github", "papers"} or (item.source == "feed" and item.extra.get("translate", False))
        if translate_description and description and not is_chinese(description):
            translation_inputs.append(description)

    translations = await translator.translate(translation_inputs)
    for item in items:
        translate_title = item.source in {"youtube", "papers"} or (item.source in {"telegram", "feed"} and item.extra.get("translate", item.source == "telegram"))
        if translate_title and not is_chinese(item.title):
            item.title_zh = translations.get(item.title, item.title)
        else:
            item.title_zh = item.title
        description = str(item.extra.get("description") or "").strip()
        if description:
            item.summary_zh = translations.get(description, description)
            if item.source == "papers":
                item.summary_zh = item.summary_zh[:400]

    if os.getenv("ENABLE_SUMMARY", "true").lower() == "true":
        summary_inputs: dict[str, list[Item]] = {}
        for item in items:
            description = str(item.extra.get("description") or "").strip()
            if item.source in {"github", "youtube"}:
                summary_inputs.setdefault(f"{item.title}\n{description}".strip(), []).append(item)
        summaries = await translator.summarize(list(summary_inputs))
        for source_text, matched_items in summary_inputs.items():
            for item in matched_items:
                item.summary_zh = summaries.get(source_text, item.summary_zh or source_text)[:40]
    return items
