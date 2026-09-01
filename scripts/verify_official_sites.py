from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import httpx
import yaml


def _curl_status(url: str) -> int | str:
    try:
        result = subprocess.run(
            ["curl", "-L", "-k", "-s", "-o", "NUL" if os.name == "nt" else "/dev/null", "-w", "%{http_code}", "--max-time", "15", url],
            check=False, capture_output=True, text=True, timeout=20,
        )
        return int(result.stdout) if result.stdout.isdigit() else "ERR"
    except Exception:
        return "ERR"


async def main() -> None:
    sites = yaml.safe_load(Path("data/gongkao_official_sites.yaml").read_text(encoding="utf-8"))
    headers = {"User-Agent": "Mozilla/5.0 (compatible; hot-gap-link-check/1.0)"}
    async with httpx.AsyncClient(timeout=12, follow_redirects=True, verify=False, headers=headers) as client:
        async def check(site: dict) -> tuple[str, str, int | str, str]:
            try:
                response = await client.get(site["url"])
                if response.status_code == 200:
                    return site["province"], site["name"], response.status_code, str(response.url)
                raise RuntimeError(f"HTTP {response.status_code}")
            except Exception as exc:
                status = await asyncio.to_thread(_curl_status, site["url"])
                return site["province"], site["name"], status, site["url"] if status == 200 else str(exc)

        results = await asyncio.gather(*(check(site) for site in sites))
    for province, name, status, final_url in results:
        print(f"{province}\t{status}\t{name}\t{final_url}")
    failures = [result for result in results if result[2] != 200]
    print(f"verified={len(results) - len(failures)}/{len(results)}")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
