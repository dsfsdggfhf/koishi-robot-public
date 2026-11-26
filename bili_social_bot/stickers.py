from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from .utils import resolve_path


class LocalStickerPicker:
    """本地表情包随机选择器。

    设计原则：
    - 只读取本地文件，不调用模型，不增加 token 消耗；
    - 为空目录/文件过大/格式不支持时返回 None，不影响文字发送；
    - 支持排除 UID（默认排除主人管理员），避免给主人私信附带随机表情。
    """

    DEFAULT_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

    def __init__(self, config: dict[str, Any] | None, owner_uid: str = ""):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.directory = resolve_path(str(cfg.get("directory") or "data/emoji_packs"))
        try:
            self.probability = float(cfg.get("probability", 0.30))
        except Exception:
            self.probability = 0.30
        self.probability = min(1.0, max(0.0, self.probability))
        try:
            self.max_bytes = int(cfg.get("max_bytes") or 5 * 1024 * 1024)
        except Exception:
            self.max_bytes = 5 * 1024 * 1024
        exts = cfg.get("extensions") or list(self.DEFAULT_EXTS)
        self.extensions = {str(x).lower() if str(x).startswith(".") else "." + str(x).lower() for x in exts}
        self.comments = bool(cfg.get("comments", True))
        self.private_messages = bool(cfg.get("private_messages", True))
        excluded = {str(x).strip() for x in (cfg.get("exclude_uids") or []) if str(x).strip()}
        if owner_uid:
            excluded.add(str(owner_uid).strip())
        self.exclude_uids = excluded

    def available_files(self) -> list[Path]:
        if not self.enabled or not self.directory.exists() or not self.directory.is_dir():
            return []
        out: list[Path] = []
        for p in self.directory.rglob("*"):
            try:
                if not p.is_file():
                    continue
                if p.suffix.lower() not in self.extensions:
                    continue
                if self.max_bytes > 0 and p.stat().st_size > self.max_bytes:
                    continue
                out.append(p)
            except Exception:
                continue
        return out

    def choose(self, channel: str, target_uid: str = "", probability_override: float | None = None,
               allow_excluded: bool = False) -> Path | None:
        """按概率为指定通道选择一张本地表情包。allow_excluded=True 时忽略 exclude_uids
        （生日小故事等个人化私信可允许给主人也带表情）。"""
        if not self.enabled:
            return None
        channel = str(channel or "").lower()
        if channel in {"comment", "comments", "reply", "at", "dynamic"}:
            if not self.comments:
                return None
        elif channel in {"dm", "private", "private_message"}:
            if not self.private_messages:
                return None
            if not allow_excluded and str(target_uid or "").strip() in self.exclude_uids:
                return None
        else:
            return None
        prob = self.probability if probability_override is None else float(probability_override)
        prob = min(1.0, max(0.0, prob))
        if prob <= 0 or random.random() > prob:
            return None
        files = self.available_files()
        if not files:
            return None
        return random.choice(files)
