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
            _cache_translated(save_cache, fresh, self.provider)
            return fresh
        except Exception as exc:  # noqa: BLE001 - degrade, never crash the run
            LOGGER.warning("%s provider %s degraded: %s", kind, self.provider, exc)
        if self.fallback is not None:
            try:
                # Call the fallback's full pipeline so its own cache + fallback
                # chain apply (zhipu -> deepseek -> free). `missing` is already
                # cleaned, so keys round-trip unchanged.
                method = self.fallback.translate if kind == "translation" else self.fallback.summarize
                fresh = await method(missing)
                recovered = sum(1 for src, dst in fresh.items() if dst != src)
                LOGGER.info("%s: %d/%d recovered via %s", kind, recovered, len(missing), self.fallback.provider)
                return fresh
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("%s fallback chain from %s failed: %s", kind, self.fallback.provider, exc)
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
        self.api_key = (api_key or "").strip() or None
        self.provider = provider
        self._last_batch_at = 0.0
        if self.api_key:
            LOGGER.info("%s key loaded: len=%d prefix=%s", provider, len(self.api_key), self.api_key[:8])
        else:
            LOGGER.warning("%s key missing (env not set)", provider)

    async def batch_translate(self, texts: list[str]) -> list[str]:
        return await self._numbered_or_individual(texts, SYSTEM_PROMPT, temperature=0.1)

    async def batch_summarize(self, descriptions: list[str]) -> list[str]:
        if not descriptions:
            return []
        lines = await self._numbered_or_individual(descriptions, SUMMARY_PROMPT, temperature=0.2)
        return [line[:40] for line in lines]

    chunk_size = 40

    async def _numbered_or_individual(self, texts: list[str], system: str, *, temperature: float) -> list[str]:
        if not self.api_key:
            raise RuntimeError(f"{self.provider.upper()}_API_KEY is missing")
        out: list[str] = []
        for start in range(0, len(texts), self.chunk_size):
            chunk = texts[start:start + self.chunk_size]
            numbered = "\n".join(f"{i + 1}. {_clean(text)}" for i, text in enumerate(chunk))
            content = await self._chat(system, numbered, temperature=temperature)
            lines = [_strip_number(line) for line in content.splitlines() if line.strip()]
            if len(lines) == len(chunk):
                out.extend(lines)
                continue
            # The model wrapped or merged lines - fall back to one request per item.
            LOGGER.info("%s: chunk line count %d != %d, retrying per item", self.provider, len(lines), len(chunk))
            for text in chunk:
                reply = await self._chat(system, _clean(text), temperature=temperature)
                out.append(next((_strip_number(line) for line in reply.splitlines() if line.strip()), text))
        return out

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
    """Keyless fallback translation.

    Tries the MyMemory API (a real endpoint, tolerant of datacenter IPs) and
    then Google's public endpoint. One request per text with limited
    concurrency; a failure leaves that text unchanged rather than aborting the
    batch, and untranslated items are not cached so they retry next run.
    """

    provider = "free"
    mymemory = "https://api.mymemory.translated.net/get"
    google = "https://translate.googleapis.com/translate_a/single"
    concurrency = 4

    async def batch_translate(self, texts: list[str]) -> list[str]:
        cleaned = [_clean(text) for text in texts]
        gate = asyncio.Semaphore(self.concurrency)
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "Mozilla/5.0"}) as client:
            async def one(text: str) -> str:
                async with gate:
                    return await self._translate_one(client, text)
            return list(await asyncio.gather(*(one(text) for text in cleaned)))

    async def batch_summarize(self, descriptions: list[str]) -> list[str]:
        return [line[:40] for line in await self.batch_translate(descriptions)]

    async def _translate_one(self, client: httpx.AsyncClient, text: str) -> str:
        for attempt in (self._via_mymemory, self._via_google):
            try:
                result = await attempt(client, text)
                if result and result != text:
                    return result
            except (httpx.HTTPError, ValueError, IndexError, TypeError, KeyError):
                continue
        return text

    async def _via_mymemory(self, client: httpx.AsyncClient, text: str) -> str:
        response = await client.get(self.mymemory, params={"q": text[:500], "langpair": "en|zh-CN"})
        response.raise_for_status()
        payload = response.json()
        if int(payload.get("responseStatus", 0)) != 200:
            return ""
        translated = str(payload.get("responseData", {}).get("translatedText") or "")
        return "" if "MYMEMORY WARNING" in translated.upper() else translated.strip()

    async def _via_google(self, client: httpx.AsyncClient, text: str) -> str:
        response = await client.get(self.google, params={
            "client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text,
        })
        response.raise_for_status()
        segments = response.json()[0] or []
        return "".join(str(segment[0]) for segment in segments if segment and segment[0]).strip()


class NoneTranslator(Translator):
    provider = "none"

    async def batch_translate(self, texts: list[str]) -> list[str]:
        return list(texts)


def _wrap(database: "Database", *, base_url: str, model_env: str, model_default: str,
          key: str | None, provider: str, fallback: Translator) -> Translator:
    key = (key or "").strip()
    if not key:
        return fallback
    return OpenAICompatibleTranslator(
        database, base_url=base_url, model=os.getenv(model_env) or model_default,
        api_key=key, provider=provider, fallback=fallback,
    )


# One layer per provider, each keyed by its env var. Ordered worst -> best so the
# outermost (best/most reachable) is tried first; missing keys are skipped.
_LAYERS = [
    dict(base_url="https://open.bigmodel.cn/api/paas/v4", model_env="ZHIPU_MODEL",
         model_default="glm-4-flash", env="ZHIPU_API_KEY", provider="zhipu"),
    dict(base_url=os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com", model_env="DEEPSEEK_MODEL",
         model_default="deepseek-chat", env="DEEPSEEK_API_KEY", provider="deepseek"),
    dict(base_url=os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1", model_env="OPENAI_MODEL",
         model_default="gpt-4o-mini", env="OPENAI_API_KEY", provider="openai"),
    # GitHub Models: reachable from GitHub Actions, free, no separate signup.
    dict(base_url=os.getenv("GH_MODELS_BASE_URL") or "https://models.github.ai/inference", model_env="GH_MODELS_MODEL",
         model_default="openai/gpt-4o-mini", env="GH_MODELS_TOKEN", provider="github"),
]


def create_translator(database: "Database") -> Translator:
    provider = os.getenv("TRANSLATOR", "auto").strip().lower()
    chain: Translator = FreeTranslator(database)
    for layer in _LAYERS:
        key = os.getenv(layer["env"]) or (os.getenv("GITHUB_TOKEN") if layer["env"] == "GH_MODELS_TOKEN" else None)
        chain = _wrap(
            database, base_url=layer["base_url"], model_env=layer["model_env"],
            model_default=layer["model_default"], key=key, provider=layer["provider"], fallback=chain,
        )
    if provider in {"auto", ""}:
        return chain
    if provider == "none":
        return NoneTranslator(database)
    if provider == "free":
        return FreeTranslator(database)
    # Force one provider as the entry point; it still falls back down the chain.
    node: Translator | None = chain
    while node is not None:
        if getattr(node, "provider", None) == provider:
            return node
        node = getattr(node, "fallback", None)
    LOGGER.warning("TRANSLATOR=%s has no key configured; using auto chain", provider)
    return chain


def _cache_translated(save_cache, mapping: dict[str, str], provider: str) -> None:
    """Persist only rows that actually changed - an identity result means the
    provider failed for that item, so it should be retried on the next run."""
    changed = {src: dst for src, dst in mapping.items() if dst != src}
    if changed:
        save_cache(changed, provider)


def _clean(text: str) -> str:
    return " ".join(str(text).split())


def _strip_number(line: str) -> str:
    stripped = line.strip()
    prefix, separator, remainder = stripped.partition(". ")
    return remainder if separator and prefix.isdigit() else stripped
