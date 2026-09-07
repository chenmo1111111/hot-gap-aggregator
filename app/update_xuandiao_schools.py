"""Re-extract targeted-selection university lists from configured notices."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv
from selectolax.parser import HTMLParser

from app.pipeline.gongkao_enrich import DeepSeekExtractor


def document_text(content: bytes, content_type: str, url: str) -> str:
    lowered = url.lower()
    if "wordprocessingml" in content_type or lowered.endswith(".docx"):
        with zipfile.ZipFile(BytesIO(content)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        return re.sub(r"<[^>]+>", "", xml)
    if "pdf" in content_type or lowered.endswith(".pdf"):
        executable = shutil.which("pdftotext")
        if not executable:
            raise RuntimeError("PDF source requires the pdftotext executable")
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.pdf"
            target = Path(folder) / "source.txt"
            source.write_bytes(content)
            subprocess.run([executable, "-layout", str(source), str(target)], check=True)
            return target.read_text(encoding="utf-8", errors="replace")
    html = content.decode("utf-8", errors="replace")
    tree = HTMLParser(html)
    for node in tree.css("script,style,noscript"):
        node.decompose()
    return (tree.body or tree.root).text(separator="\n", strip=True)


def parse_school_list(value: str) -> list[str]:
    candidate = value[value.find("{") : value.rfind("}") + 1]
    parsed = json.loads(candidate)
    schools = parsed.get("schools") if isinstance(parsed, dict) else None
    if not isinstance(schools, list):
        raise ValueError("LLM result has no schools list")
    seen: set[str] = set()
    result: list[str] = []
    for value in schools:
        name = str(value or "").strip()
        key = re.sub(r"\s+", "", name)
        if name and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Refresh xuandiao school lists")
    parser.add_argument("--config", default="config/xuandiao_schools.yaml")
    parser.add_argument("--province", action="append")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    path = Path(args.config)
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources = config.get("sources") or {}
    targets = set(args.province or sources.keys())
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY is required")
    extractor = DeepSeekExtractor(
        api_key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
    )
    client = httpx.Client(timeout=45, follow_redirects=True)
    changes: dict[str, list[str]] = {}
    try:
        for province, metadata in sources.items():
            if province not in targets or not isinstance(metadata, dict):
                continue
            url = str(metadata.get("source_url") or "").strip()
            if not url:
                continue
            response = client.get(url)
            response.raise_for_status()
            text = document_text(response.content, response.headers.get("content-type", ""), url)
            prompt = (
                f"从下面的{province}选调公告或附件中提取所有明确列出的可报高校名称。"
                "不要推测、不要补全，只输出 JSON：{\"schools\":[\"学校1\",\"学校2\"]}。"
                "若没有明确完整名单，返回空数组。\n\n" + text[:80000]
            )
            response = extractor.client.post(
                f"{extractor.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {extractor.api_key}"},
                json={"model": extractor.model, "temperature": 0, "response_format": {"type": "json_object"},
                      "messages": [{"role": "user", "content": prompt}]},
            )
            response.raise_for_status()
            schools = parse_school_list(response.json()["choices"][0]["message"]["content"])
            if schools:
                changes[str(province)] = schools
        if not args.dry_run and changes:
            config.setdefault("schools_by_province", {}).update(changes)
            path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
        print(json.dumps({"updated": changes, "dry_run": args.dry_run}, ensure_ascii=False))
        return 0
    finally:
        client.close()
        extractor.close()


if __name__ == "__main__":
    raise SystemExit(main())
