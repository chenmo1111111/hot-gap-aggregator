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
SUMMARY_PROMPT = "每行压缩成一句中文，说明这是什么、为什么值得看；每条不超过40个汉字，只输出结果，每行一条。"


class Translator(ABC):
    """Cached batch translator contract shared by every provider.

    ``translate`` / ``summarize`` return a mapping keyed by the *original*
    input strings (callers pass raw titles/descriptions and look results up
    with those same strings). Cache storage is keyed by the whitespace-cleaned
    form so minor formatting differences still hit.
    """

    provider = "base"

    def __init__(self, database: "Database", fallback: "Translator | None" = None) -> None:
        self.database = database
        self.fallback = fallback
        self.batch_count = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.summary_batch_count = 0
        self.summary_cache_hits = 0
        self.summary_cache_misses = 0

    async def translate(self, texts: list[str]) -> dict[str, str]:
        return await self._run(
            texts,
            get_cache=self.database.get_translations,
            save_cache=self.database.save_translations,
            call=self.batch_translate,
            counters=("batch_count", "cache_hits", "cache_misses"),
            kind="translation",
        )

    async def summarize(self, descriptions: list[str]) -> dict[str, str]:
        return await self._run(
            descriptions,
            get_cache=self.database.get_summaries,
            save_cache=self.database.save_summaries,
            call=self.batch_summarize,
            counters=("summary_batch_count", "summary_cache_hits", "summary_cache_misses"),
            kind="summary",
        )

    async def _run(self, texts, *, get_cache, save_cache, call, counters, kind) -> dict[str, str]:
        pairs = [(text, _clean(text)) for text in texts if _clean(text)]
        cleaned = list(dict.fromkeys(clean for _, clean in pairs))
        if not cleaned:
            return {}
        cached = get_cache(cleaned, self.provider)
        setattr(self, counters[1], getattr(self, counters[1]) + len(cached))
        missing = [clean for clean in cleaned if clean not in cached]
        setattr(self, counters[2], getattr(self, counters[2]) + len(missing))
        resolved = dict(cached)
        if missing:
            fresh = await self._fetch(missing, call, save_cache, counters[0], kind)
            resolved.update(fresh)
        return {original: resolved.get(clean, original) for original, clean in pairs}

    async def _fetch(self, missing, call, save_cache, batch_counter, kind) -> dict[str, str]:
        try:
            setattr(self, batch_counter, getattr(self, batch_counter) + 1)
            values = await call(missing)
            if len(values) != len(missing):
                raise ValueError(f"provider returned {len(values)} results for {len(missing)} inputs")
            fresh = {src: (val.strip() or src) for src, val in zip(missing, values, strict=True)}
            save_cache(fresh, self.provider)
            return fresh
        except Exception as exc:  # noqa: BLE001 - degrade, never crash the run
            LOGGER.warning("%s provider %s degraded: %s", kind, self.provider, exc)
        if self.fallback is not None:
            try:
                fallback_call = self.fallback.batch_translate if kind == "translation" else self.fallback.batch_summarize
                values = await fallback_call(missing)
                if len(values) == len(missing):
                    fresh = {src: (val.strip() or src) for src, val in zip(missing, values, strict=True)}
                    # Cache under the fallback's own name, not ours: once the
                    # primary provider recovers it should retry and win, rather
                    # than keep serving lower-quality fallback text forever.
                    save_cache(fresh, self.fallback.provider)
                    LOGGER.info("%s recovered via fallback %s", kind, self.fallback.provider)
                    return fresh
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("%s fallback %s also failed: %s", kind, self.fallback.provider, exc)
        return {text: text for text in missing}

    @abstractmethod
    async def batch_translate(self, texts: list[str]) -> list[str]:
        raise NotImplementedError

    async def batch_summarize(self, descriptions: list[str]) -> list[str]:
        return descriptions


class OpenAICompatibleTranslator(Translator):
    provider = "openai"

    def __init__(
        self, database: "Database", *, base_url: str, model: str, api_key: str | None,
        provider: str, fallback: "Translator | None" = None,
    ) -> None:
        super().__init__(database, fallback=fallback)
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.provider = provider
        self._last_batch_at = 0.0

    async def batch_translate(self, texts: list[str]) -> list[str]:
        return await self._numbered_or_individual(texts, SYSTEM_PROMPT, temperature=0.1)

    async def batch_summarize(self, descriptions: list[str]) -> list[str]:
        if not descriptions:
            return []
        lines = await self._numbered_or_individual(descriptions, SUMMARY_PROMPT, temperature=0.2)
        return [line[:40] for line in lines]

    async def _numbered_or_individual(self, texts: list[str], system: str, *, temperature: float) -> list[str]:
        if not self.api_key:
            key_name = "ZHIPU_API_KEY" if self.provider == "zhipu" else "OPENAI_API_KEY"
            raise RuntimeError(f"{key_name} is missing")
        numbered = "\n".join(f"{index + 1}. {_clean(text)}" for index, text in enumerate(texts))
        content = await self._chat(system, numbered, temperature=temperature)
        lines = [_strip_number(line) for line in content.splitlines() if line.strip()]
        if len(lines) == len(texts):
            return lines
        # The model wrapped or merged lines - fall back to one request per item.
        LOGGER.info("%s: batch line count %d != %d, retrying per item", self.provider, len(lines), len(texts))
        results: list[str] = []
        for text in texts:
            reply = await self._chat(system, _clean(text), temperature=temperature)
            first = next((_strip_number(line) for line in reply.splitlines() if line.strip()), text)
            results.append(first)
        return results

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
                    if isinstance(exc, httpx.HTTPStatusError):
                        body_text = getattr(exc.response, "text", "") or ""
                        LOGGER.warning("%s HTTP %s: %s", self.provider, exc.response.status_code, body_text[:300])
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

    async def batch_summarize(self, descriptions: list[str]) -> list[str]:
        # No LLM here - a Chinese translation of the description is the summary.
        translated = await asyncio.to_thread(self._translate_sync, descriptions)
        return [line[:40] for line in translated]

    @staticmethod
    def _translate_sync(texts: list[str]) -> list[str]:
        import translators as ts

        results: list[str] = []
        for text in texts:
            last_error: Exception | None = None
            for engine in ("google", "bing"):
                try:
                    translated = ts.translate_text(_clean(text), translator=engine, to_language="zh")
                    if translated:
                        results.append(str(translated))
                        break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
            else:
                raise RuntimeError(f"Google and Bing both failed: {last_error}")
        return results


class NoneTranslator(Translator):
    provider = "none"

    async def batch_translate(self, texts: list[str]) -> list[str]:
        return list(texts)


def create_translator(database: "Database") -> Translator:
    provider = os.getenv("TRANSLATOR", "zhipu").strip().lower()
    if provider == "zhipu":
        return OpenAICompatibleTranslator(
            database,
            base_url="https://open.bigmodel.cn/api/paas/v4",
            model=os.getenv("ZHIPU_MODEL") or "glm-4-flash",
            api_key=os.getenv("ZHIPU_API_KEY"),
            provider="zhipu",
            fallback=FreeTranslator(database),
        )
    if provider == "openai":
        return OpenAICompatibleTranslator(
            database,
            base_url=os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1",
            model=os.getenv("OPENAI_MODEL") or "gpt-4o-mini",
            api_key=os.getenv("OPENAI_API_KEY"),
            provider="openai",
            fallback=FreeTranslator(database),
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
