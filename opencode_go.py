#!/usr/bin/env python3
"""OpenCode Go 订阅简单工具（替代 TUI 的 /connect + /models）

用法（在项目目录下运行）：
    python opencode_go.py models          列出 Go 订阅可用模型（等价 /models）
    python opencode_go.py usage           查看订阅额度（5小时/周/月）
    python opencode_go.py test [模型]     测试某个模型能否调用（默认 hy3）
    python opencode_go.py chat "你好"     用订阅模型快速对话一句

API key 自动从 config.json 读取；也可用环境变量 OPENCODE_API_KEY 覆盖。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

BASE = "https://opencode.ai/zen/go/v1"


def get_key() -> str:
    key = os.environ.get("OPENCODE_API_KEY", "").strip()
    if key:
        return key
    cfg_path = Path(__file__).parent / "config.json"
    if cfg_path.exists():
        try:
            ai = json.loads(cfg_path.read_text(encoding="utf-8")).get("ai") or {}
            key = str(ai.get("api_key") or "").strip()
            if key:
                return key
        except Exception:
            pass
    print("未找到 API key：请在 config.json 的 ai.api_key 配置，或设置环境变量 OPENCODE_API_KEY")
    sys.exit(1)


async def fetch(url: str, key: str, payload: dict | None = None):
    import httpx
    headers = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        if payload is None:
            r = await client.get(url, headers=headers)
        else:
            headers["Content-Type"] = "application/json"
            r = await client.post(url, headers=headers, json=payload)
        return r


def fmt_usage(u: dict) -> str:
    lines = []
    for name, key in (("5小时窗口", "rolling"), ("周额度", "weekly"), ("月额度", "monthly")):
        d = u.get("usage", {}).get(key) or {}
        status = d.get("status", "?")
        pct = d.get("percent", 0)
        icon = "✅" if status == "ok" else "⚠️"
        lines.append(f"  {icon} {name}: {status} {pct}%  (重置于 {d.get('resetsAt', '?')})")
    return "\n".join(lines)


async def cmd_models(key: str) -> None:
    r = await fetch(f"{BASE}/models", key)
    if r.status_code != 200:
        print(f"失败 HTTP {r.status_code}: {r.text[:300]}")
        return
    data = r.json().get("data") or []
    print(f"Go 订阅可用模型（共 {len(data)} 个）：")
    for i in range(0, len(data), 4):
        print("  " + "  ".join(m["id"].ljust(20) for m in data[i:i + 4]))


async def cmd_usage(key: str) -> None:
    r = await fetch(f"{BASE}/usage", key)
    if r.status_code != 200:
        print(f"失败 HTTP {r.status_code}: {r.text[:300]}")
        return
    print("Go 订阅额度：")
    print(fmt_usage(r.json()))


async def cmd_test(key: str, model: str) -> None:
    payload = {"model": model, "messages": [{"role": "user", "content": "回复：ok"}], "max_tokens": 10}
    r = await fetch(f"{BASE}/chat/completions", key, payload)
    if r.status_code == 200:
        content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
        print(f"✅ {model} 调用成功: {content}")
    else:
        try:
            err = r.json().get("error", {}).get("message", r.text[:200])
        except Exception:
            err = r.text[:200]
        print(f"❌ {model} 调用失败 HTTP {r.status_code}: {err}")


async def cmd_chat(key: str, text: str) -> None:
    payload = {"model": "hy3", "messages": [{"role": "user", "content": text}], "max_tokens": 300}
    r = await fetch(f"{BASE}/chat/completions", key, payload)
    if r.status_code == 200:
        content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
        print(f"hy3: {content}")
    else:
        try:
            err = r.json().get("error", {}).get("message", r.text[:200])
        except Exception:
            err = r.text[:200]
        print(f"❌ 调用失败 HTTP {r.status_code}: {err}")


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "usage"
    key = get_key()
    if cmd == "models":
        asyncio.run(cmd_models(key))
    elif cmd == "usage":
        asyncio.run(cmd_usage(key))
    elif cmd == "test":
        asyncio.run(cmd_test(key, args[1] if len(args) > 1 else "hy3"))
    elif cmd == "chat":
        text = " ".join(args[1:]) or "你好，用一句话介绍你自己"
        asyncio.run(cmd_chat(key, text))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
