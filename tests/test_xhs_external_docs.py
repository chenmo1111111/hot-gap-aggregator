import json

import pytest

from app.watchers.xhs_external_docs import (
    XhsExternalDocs,
    compact_document_text,
    document_identity,
    extract_payload_strings,
    merge_tencent_doc_cookies,
    safe_document_url,
    validate_document_url,
)


def test_url_identity_and_redaction_never_keep_access_code() -> None:
    url = "https://doc.weixin.qq.com/sheet/demo?scode=secret-value&tab=000001"
    assert safe_document_url(url) == "https://doc.weixin.qq.com/sheet/demo?tab=000001"
    assert document_identity(url) == "doc.weixin.qq.com/sheet/demo?tab=000001"
    assert "secret-value" not in safe_document_url(url)
    validate_document_url(url)
    with pytest.raises(ValueError):
        validate_document_url("https://evil.example/sheet/demo?scode=secret-value")


def test_cookie_editor_json_is_restricted_to_tencent_domains() -> None:
    raw = json.dumps([
        {
            "name": "docs_session", "value": "secret", "domain": ".docs.qq.com",
            "path": "/", "httpOnly": True, "secure": True, "sameSite": "no_restriction",
            "expirationDate": 1_900_000_000,
        },
        {"name": "unrelated", "value": "drop", "domain": ".example.com", "path": "/"},
    ])
    assert merge_tencent_doc_cookies(raw) == [{
        "name": "docs_session", "value": "secret", "domain": ".docs.qq.com", "path": "/",
        "secure": True, "httpOnly": True, "sameSite": "None", "expires": 1_900_000_000,
    }]


def test_payload_text_extraction_and_focus_compaction() -> None:
    payload = {
        "data": {
            "rows": [
                {"cells": ["一级类目", "二级类目", "个人店是否可售"]},
                {"cells": ["教育", "电子资源", "定向准入"]},
            ],
            "encoded": json.dumps({"notice": "虚拟商品需要补充资质"}, ensure_ascii=False),
        },
    }
    candidates = extract_payload_strings(payload)
    text = compact_document_text(candidates, max_chars=200, focus_terms=["电子资源"])
    assert "电子资源" in text and "定向准入" in text and "虚拟商品" in text


@pytest.mark.asyncio
async def test_refresh_failure_preserves_previous_without_raw_access_url(monkeypatch, tmp_path) -> None:
    path = tmp_path / "external.json"
    collector = XhsExternalDocs(path)
    url = "https://doc.weixin.qq.com/sheet/demo?scode=secret-value&tab=000001"
    identity = document_identity(url)
    path.write_text(json.dumps({
        "documents": [{
            "source_id": identity,
            "source_url": safe_document_url(url),
            "title": "旧类目表",
            "content_text": "教育 > 电子资源 > 题库：个人店可售",
            "content_hash": "old-hash",
            "stale": False,
        }],
    }, ensure_ascii=False), encoding="utf-8")

    async def fail(_url, *, focus_terms):
        raise RuntimeError("navigation failed at ?scode=secret-value")

    monkeypatch.setattr(collector, "_fetch_document", fail)
    result = await collector.refresh([url])
    assert result[0]["stale"] is True
    assert "电子资源" in result[0]["content_text"]
    stored = path.read_text(encoding="utf-8")
    assert "secret-value" not in stored
    assert "scode" not in result[0]["error"].casefold()
