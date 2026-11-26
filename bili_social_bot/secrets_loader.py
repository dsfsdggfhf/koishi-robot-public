"""把项目根目录 ``secrets.env`` 里的敏感配置注入环境变量。

设计目标
--------
1. 真正的密钥只保存在本地 ``secrets.env``（已被 .gitignore 排除），
   ``config.json`` 里只留 ``*_env`` 键名，这样配置文件即使被提交也不泄密。
2. 任何入口（``main.py`` / ``supervisor.py`` / ``tools/*``）只要经过
   :func:`bili_social_bot.config.load_config`，就会自动完成注入，
   不依赖启动它的是哪个父进程——supervisor 重启子进程时会重新读取。
3. 已经存在的真实环境变量优先，不会被文件里的值覆盖。

文件格式（KEY=VALUE，一行一个）::

    # 注释
    KOISHI_AI_API_KEY=sk-xxxx
    export KOISHI_PREMIUM_API_KEY='sk-yyyy'
"""
from __future__ import annotations

import os
from pathlib import Path

from .utils import project_root

SECRETS_FILE = "secrets.env"
_loaded = False


def _parse_line(line: str) -> tuple[str, str] | None:
    """把一行 ``KEY=VALUE`` 解析成键值对；无效行返回 ``None``。"""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("export "):
        text = text[len("export "):].strip()
    if "=" not in text:
        return None
    key, _, value = text.partition("=")
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    if not key:
        return None
    return key, value


def load_secrets(path: str | os.PathLike[str] | None = None, *, force: bool = False) -> int:
    """读取 ``secrets.env`` 并注入 ``os.environ``，返回本次注入的条目数。

    重复调用是安全的：首次之后直接返回 0（除非 ``force=True``）。
    任何异常都被吞掉——缺失或损坏的密钥文件不应该让机器人启动失败，
    后续的“缺少 api_key”检查会给出更明确的提示。
    """
    global _loaded
    if _loaded and not force:
        return 0
    target = Path(path) if path is not None else project_root() / SECRETS_FILE
    applied = 0
    try:
        if target.exists():
            raw = target.read_text(encoding="utf-8-sig")
            for line in raw.splitlines():
                parsed = _parse_line(line)
                if not parsed:
                    continue
                key, value = parsed
                if not value:
                    continue
                if os.environ.get(key):
                    # 真实环境变量优先，避免覆盖 CI / 手工 export 的调试值。
                    continue
                os.environ[key] = value
                applied += 1
    except Exception:
        pass
    _loaded = True
    return applied


def resolved(name: str | None, default: str = "") -> str:
    """取环境变量值（``None`` / 空名时返回 ``default``），便于调用方统一写法。"""
    key = str(name or "").strip()
    if not key:
        return default
    return os.environ.get(key, default).strip()
