from __future__ import annotations

import json
import struct
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _png_size(path: Path) -> tuple[int, int]:
    content = path.read_bytes()
    assert content[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", content[16:24])


def test_android_manifest_meets_samsung_installability_requirements() -> None:
    manifest = json.loads((ROOT / "public/manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] and manifest["short_name"]
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    icons = manifest["icons"]
    assert any(icon["sizes"] == "192x192" for icon in icons)
    assert any(icon["sizes"] == "512x512" and "maskable" in icon["purpose"] for icon in icons)
    assert _png_size(ROOT / "public/icon-192.png") == (192, 192)
    assert _png_size(ROOT / "public/icon-maskable-512.png") == (512, 512)


def test_page_and_service_worker_use_json_manifest() -> None:
    index = (ROOT / "web/index.html").read_text(encoding="utf-8")
    worker = (ROOT / "public/sw.js").read_text(encoding="utf-8")
    assert 'rel="manifest" href="/manifest.json?v=3"' in index
    assert "/manifest.json?v=3" in worker
    assert "hot-gap-v9" in worker
