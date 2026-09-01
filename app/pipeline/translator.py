from __future__ import annotations

import asyncio
import logging
import os
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from app.store.database import Database

LOGGER = logging.getLogger(__name__)
SYSTEM_PROMPT = "简洁口语化，保留专有名词和缩写，只输出译文，每行一条，不加解释"


class Translator(ABC):
    """Cached batch translator contract shared by every provider."""

    provider = "base"

    def __init__(self, database: "Database") -> None:
        self.database = database
        self.batch_count = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.summary_batch_count = 0
        self.summary_cache_hits = 0
        self.summary_cache_misses = 0

    async def translate(self, texts: list[str]) -> dict[str, str]:
        ordered = list(dict.fromkeys(_clean(text) for text in texts if _clean(text)))
        cached = self.database.get_translations(ordered, self.provider)
        self.cache_hits += len(cached)
        missing = [text for text in ordered if text not in cached]
        self.cache_misses += len(missing)
        if not missing:
            return cached
        try:
            self.batch_count += 1
            fresh_values = await self.batch_translate(missing)
            if len(fresh_values) != len(missing):
                raise ValueError(f"provider returned {len(fresh_values)} translations for {len(missing)} inputs")
            fresh = {source: target.strip() or source for source, target in zip(missing, fresh_values, strict=True)}
            self.database.save_translations(fresh, self.provider)
            return {**cached, **fresh}
        except Exception as exc:
            LOGGER.warning("translation provider %s degraded: %s", self.provider, exc)
            return {**cached, **{text: text for text in missing}}

    @abstractmethod
    async def batch_translate(self, texts: list[str]) -> list[str]:
        raise NotImplementedError

    async def summarize(self, descriptions: list[str]) -> dict[str, str]:
        ordered = list(dict.fromkeys(_clean(text) for text in descriptions if _clean(text)))
        cached = self.database.get_summaries(ordered, self.provider)
        self.summary_cache_hits += len(cached)
        missing = [text for text in ordered if text not in cached]
        self.summary_cache_misses += len(missing)
        if not missing:
            return cached
        try:
            self.summary_batch_count += 1
            fresh_values = await self.batch_summarize(missing)
            if len(fresh_values) != len(missing):
                raise ValueError("summary line count mismatch")
            fresh = {source: target.strip() or source for source, target in zip(missing, fresh_values, strict=True)}
            self.database.save_summaries(fresh, self.provider)
            return {**cached, **fresh}
        except Exception as exc:
            LOGGER.warning("summary provider %s degraded: %s", self.provider, exc)
            return {**cached, **{text: text for text in missing}}

    async def batch_summarize(self, descriptions: list[str]) -> list[str]:
        return descriptions


class OpenAICompatibleTranslator(Translator):
    provider = "openai"

    def __init__(self, database: "Database", *, base_url: str, model: str, api_key: str | None, provider: str) -> None:
        super().__init__(database)
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.provider = provider
        self._last_batch_at = 0.0

    async def batch_translate(self, texts: list[str]) -> list[str]:
        if not self.api_key:
            key_name = "ZHIPU_API_KEY" if self.provider == "zhipu" else "OPENAI_API_KEY"
            raise RuntimeError(f"{key_name} is missing")
        numbered = "\n".join(f"{index + 1}. {text}" for index, text in enumerate(texts))
        content = await self._chat(SYSTEM_PROMPT, numbered, temperature=0.1)
        lines = [_strip_number(line) for line in content.splitlines() if line.strip()]
        if len(lines) != len(texts):
            raise ValueError("line count mismatch in translation response")
        return lines

    async def batch_summarize(self, descriptions: list[str]) -> list[str]:
        if not descriptions:
            return []
        if not self.api_key:
            raise RuntimeError("summary API key is missing")
        numbered = "\n".join(f"{index + 1}. {text}" for index, text in enumerate(descriptions))
        content = await self._chat(
            "每行压缩成一句中文，说明这是什么、为什么值得看；每条不超过40个汉字，只输出结果，每行一条。",
            numbered,
            temperature=0.2,
        )
        lines = [_strip_number(line) for line in content.splitlines() if line.strip()]
        if len(lines) != len(descriptions):
            raise ValueError("summary line count mismatch")
        return [line[:40] for line in lines]

    async def _chat(self, system: str, user: str, *, temperature: float) -> str:
        elapsed = time.monotonic() - self._last_batch_at
        if elapsed < 0.5:
            await asyncio.sleep(0.5 - elapsed)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        body = {
            "model": self.model,
            "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=45) as client:
            for attempt in range(3):
                try:
                    response = await client.post(f"{self.base_url}/chat/completions", headers=headers, json=body)
                    response.raise_for_status()
                    self._last_batch_at = time.monotonic()
                    return str(response.json()["choices"][0]["message"]["content"]).strip()
                except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                    last_error = exc
                    if attempt < 2:
                        retry_after = None
                        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                            retry_after = exc.response.headers.get("Retry-After")
                        await asyncio.sleep(float(retry_after) if retry_after and retry_after.isdigit() else 0.75 * (2**attempt))
        raise RuntimeError(f"chat request failed: {last_error}")


class FreeTranslator(Translator):
    provider = "free"

    async def batch_translate(self, texts: list[str]) -> list[str]:
        return await asyncio.to_thread(self._translate_sync, texts)

    @staticmethod
    def _translate_sync(texts: list[str]) -> list[str]:
        import translators as ts

        results: list[str] = []
        for text in texts:
            last_error: Exception | None = None
            for engine in ("google", "bing"):
                try:
                    translated = ts.translate_text(text, translator=engine, to_language="zh")
                    if translated:
                        results.append(str(translated))
                        break
                except Exception as exc:
                    last_error = exc
            else:
                raise RuntimeError(f"Google and Bing both failed: {last_error}")
        return results


class NoneTranslator(Translator):
    provider = "none"

    async def translate(self, texts: list[str]) -> dict[str, str]:
        ordered = list(dict.fromkeys(_clean(text) for text in texts if _clean(text)))
        return {text: text for text in ordered}

    async def batch_translate(self, texts: list[str]) -> list[str]:
        return texts


def create_translator(database: "Database") -> Translator:
    provider = os.getenv("TRANSLATOR", "zhipu").strip().lower()
    if provider == "zhipu":
        return OpenAICompatibleTranslator(
            database,
            base_url="https://open.bigmodel.cn/api/paas/v4",
            model="glm-4-flash",
            api_key=os.getenv("ZHIPU_API_KEY"),
            provider="zhipu",
        )
    if provider == "openai":
        return OpenAICompatibleTranslator(
            database,
            base_url=os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1",
            model=os.getenv("OPENAI_MODEL") or "gpt-4o-mini",
            api_key=os.getenv("OPENAI_API_KEY"),
            provider="openai",
        )
    if provider == "free":
        return FreeTranslator(database)
    if provider == "none":
        return NoneTranslator(database)
    LOGGER.warning("unknown TRANSLATOR=%s; translation disabled", provider)
    return NoneTranslator(database)


def _clean(text: str) -> str:
    return " ".join(str(text).split())


def _strip_number(line: str) -> str:
    stripped = line.strip()
    prefix, separator, remainder = stripped.partition(". ")
    return remainder if separator and prefix.isdigit() else stripped
