"""MCP 市场端点冒烟检查（需先 `uv run core` 启动 8765）。

用法::

    set GSUID_LOCAL_TEST_TOKEN=...   # WebConsole 管理员 token（可选）
    uv run python eval/manual/mcp_market_check.py
"""

from __future__ import annotations

import os
import asyncio
from urllib.parse import urlencode

import httpx

BASE = os.environ.get("GSUID_CORE_BASE", "http://localhost:8765")
TOKEN = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "")


async def main() -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    async with httpx.AsyncClient(base_url=BASE, headers=headers, timeout=30.0) as client:
        resp = await client.get(f"/api/ai/mcp/market/entries?{urlencode({'page': 1, 'page_size': 10})}")
        print(f"[market/entries] {resp.status_code}")
        if resp.status_code != 200:
            print(resp.text[:300])
            return
        body = resp.json()
        data = body.get("data", {}) if isinstance(body, dict) else {}
        entries = data.get("entries", [])
        print(f"  total={data.get('total')} degraded={data.get('degraded')} items={len(entries)}")
        for item in entries[:5]:
            print(f"  - {item.get('qualified_name')} | {item.get('chinese_name') or item.get('name')}")

        logo = next((i.get("logo_url") for i in entries if i.get("logo_url")), "")
        if logo:
            resp = await client.get(f"/api/ai/mcp/market/logo?{urlencode({'u': logo})}")
            print(f"[market/logo] {resp.status_code} {resp.headers.get('content-type')}")

        first = entries[0].get("qualified_name") if entries else ""
        if first:
            resp = await client.get(f"/api/ai/mcp/market/entries/{first}")
            print(f"[market/detail] {resp.status_code}")

        resp = await client.get("/api/ai/mcp/list")
        print(f"[mcp/list] {resp.status_code}")
        if resp.status_code == 200:
            body = resp.json()
            configs = body.get("data", body) if isinstance(body, dict) else body
            if isinstance(configs, list):
                for cfg in configs:
                    print(
                        f"  - {cfg.get('config_id')} {cfg.get('name')} "
                        f"schema={cfg.get('schema_version')} market={bool(cfg.get('market_meta'))}"
                    )


if __name__ == "__main__":
    asyncio.run(main())
