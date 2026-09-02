import httpx
import pytest

from app.pipeline import translator as translator_module
from app.pipeline.translator import NoneTranslator, OpenAICompatibleTranslator, Translator, create_translator
from app.store.database import Database


class FakeTranslator(Translator):
    provider = "fake"

    async def batch_translate(self, texts: list[str]) -> list[str]:
        return [f"中：{text}" for text in texts]

    async def batch_summarize(self, texts: list[str]) -> list[str]:
        return [f"点评：{text}"[:40] for text in texts]


@pytest.mark.asyncio
async def test_translator_batches_once_and_uses_sha_cache(tmp_path) -> None:
    database = Database(tmp_path / "hot.db")
    translator = FakeTranslator(database)
    first = await translator.translate(["hello", "world", "hello"])
    second = await translator.translate(["hello"])
    assert first["hello"] == "中：hello"
    assert second["hello"] == "中：hello"
    assert translator.batch_count == 1
    assert translator.cache_hits == 1
    database.close()


def test_translator_provider_none(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TRANSLATOR", "none")
    database = Database(tmp_path / "hot.db")
    assert isinstance(create_translator(database), NoneTranslator)
    database.close()


def test_api_key_log_does_not_expose_key_material(caplog, tmp_path) -> None:
    database = Database(tmp_path / "redacted.db")
    secret = "sk-sensitive-prefix-and-secret-value"
    with caplog.at_level("INFO"):
        OpenAICompatibleTranslator(
            database, base_url="https://example.invalid/v1", model="test-model",
            api_key=secret, provider="test-provider",
        )
    assert "test-provider key loaded" in caplog.text
    assert secret[:8] not in caplog.text
    assert "len=" not in caplog.text
    database.close()


@pytest.mark.asyncio
async def test_summary_uses_provider_cache(tmp_path) -> None:
    database = Database(tmp_path / "summary.db")
    translator = FakeTranslator(database)
    first = await translator.summarize(["A project\nWorth seeing"])
    second = await translator.summarize(["A project\nWorth seeing"])
    assert first == second
    assert translator.summary_batch_count == 1
    assert translator.summary_cache_hits == 1
    database.close()


@pytest.mark.asyncio
async def test_zhipu_endpoint_headers_model_and_429_retry(monkeypatch, tmp_path) -> None:
    requests: list[tuple[str, dict, dict]] = []

    class FakeResponse:
        def __init__(self, status: int, payload: dict | None = None) -> None:
            self.status_code = status
            self.headers = {"Retry-After": "0"}
            self._payload = payload or {}
            self.request = httpx.Request("POST", "https://open.bigmodel.cn")

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("limited", request=self.request, response=self)

        def json(self) -> dict:
            return self._payload

    class FakeClient:
        def __init__(self, **_: object) -> None: pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_: object) -> None: return None
        async def post(self, url: str, *, headers: dict, json: dict):
            requests.append((url, headers, json))
            if len(requests) == 1:
                return FakeResponse(429)
            return FakeResponse(200, {"choices": [{"message": {"content": "1. 你好"}}]})

    async def no_sleep(_: float) -> None: return None
    monkeypatch.setattr(translator_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(translator_module.asyncio, "sleep", no_sleep)
    database = Database(tmp_path / "zhipu.db")
    translator = OpenAICompatibleTranslator(
        database, base_url="https://open.bigmodel.cn/api/paas/v4", model="glm-4-flash",
        api_key="test-key", provider="zhipu",
    )
    result = await translator.translate(["hello"])
    assert result["hello"] == "你好"
    assert len(requests) == 2
    assert requests[0][0] == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    assert requests[0][1]["Authorization"] == "Bearer test-key"
    assert requests[0][2]["model"] == "glm-4-flash"
    database.close()


def test_retranslate_removes_other_provider_caches(tmp_path) -> None:
    database = Database(tmp_path / "cache.db")
    database.save_translations({"old": "旧"}, "free")
    database.save_translations({"keep": "保留"}, "zhipu")
    database.save_summaries({"old summary": "旧摘要"}, "openai")
    assert database.clear_caches_except("zhipu") == (1, 1)
    assert database.get_translations(["old"], "free") == {}
    assert database.get_translations(["keep"], "zhipu") == {"keep": "保留"}
    database.close()
