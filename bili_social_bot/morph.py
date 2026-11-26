from __future__ import annotations

import random
import re
from datetime import datetime
from typing import Any


def current_morph(config: dict[str, Any] | None = None, now: datetime | None = None) -> str:
    """Return the current bot form based on weekday.

    周一/三/五 = "normal"（本体，原封不动）
    周二/四/六 = "fumo"（FUMO 形态，句尾随机加 fumo/fumofumo）
    周日 = "normal"（默认本体）

    尊重 enabled 开关：enabled=false 时一律 normal（彻底关闭 fumo 形态）。
    """
    cfg = config or {}
    if not cfg.get("enabled", True):
        return "normal"
    now = now or datetime.now()
    wd = now.isoweekday()  # 1=周一 ... 7=周日
    fumo_days = {int(x) for x in cfg.get("fumo_days") or [2, 4, 6]}
    if wd in fumo_days:
        return "fumo"
    return "normal"


def apply_morph(text: str, morph: str, config: dict[str, Any] | None = None) -> str:
    """Apply morph to outbound text.

    FUMO 形态：在句末少量、自然地追加 fumo/fumofumo，让周二/四/六有可见形态；
    本体形态：原样返回。

    尊重 enabled 开关：enabled=false 时直接原样返回（不追加 fumo）。
    """
    cfg = config or {}
    if not cfg.get("enabled", True):
        return text
    morph = (morph or "").lower()
    if morph != "fumo":
        return text
    # 只有显式允许时才外显 fumo 口癖；这样可在配置层区分“fumo 知识”与“fumo 形态”。
    if not bool(cfg.get("allow_fumo_suffix", False)):
        return text
    suffixes = [str(x) for x in cfg.get("fumo_suffixes") or ["fumo", "fumofumo"] if str(x)]
    if not suffixes:
        suffixes = ["fumo", "fumofumo"]
    try:
        probability = float(cfg.get("fumo_sentence_probability", 0.45) or 0.45)
    except Exception:
        probability = 0.45
    try:
        max_suffixes = int(cfg.get("fumo_max_suffixes", 2) or 2)
    except Exception:
        max_suffixes = 2
    return _append_fumo(text, suffixes, probability=max(0.0, min(1.0, probability)), max_suffixes=max(1, max_suffixes))


def _append_fumo(text: str, suffixes: list[str], *, probability: float = 0.45, max_suffixes: int = 2) -> str:
    text = str(text or "").rstrip()
    if not text:
        return text
    # 保护 URL、B站表情标签和常见颜文字括号块，避免出现 (´fumofumo;ωfumo;`) 这类污染。
    placeholders: list[str] = []

    def _guard(match: re.Match) -> str:
        placeholders.append(match.group(0))
        return f"\u0001{len(placeholders) - 1}\u0001"

    guarded = re.sub(r"https?://\S+", _guard, text)
    guarded = re.sub(r"\[[^\[\]\n]{1,40}\]", _guard, guarded)
    guarded = re.sub(r"[（(][^\n（）()]{1,24}[）)]", _guard, guarded)

    # 只在极少数句末标点（。！？!?；;）前插入；概率未命中就不加，避免几乎每句都口癖化。
    punct_matches = list(re.finditer(r"[。！？!?；;]", guarded))
    chosen: set[int] = set()
    for m in punct_matches:
        if len(chosen) >= max_suffixes:
            break
        if random.random() <= probability:
            chosen.add(m.start())

    def _insert(match: re.Match) -> str:
        if match.start() not in chosen:
            return match.group(0)
        # 标点前已有 fumo/fumofumo 时不重复追加。
        prefix = guarded[max(0, match.start() - 12):match.start()].lower()
        if prefix.rstrip().endswith(("fumo", "fumofumo")):
            return match.group(0)
        return random.choice(suffixes) + match.group(0)

    result = re.sub(r"[。！？!?；;]", _insert, guarded)

    # 如果整段没有句末标点，也不再保底追加；fumo 应该是偶尔露出，不是每条必带。

    # 还原被保护内容。
    for i, value in enumerate(placeholders):
        result = result.replace(f"\u0001{i}\u0001", value)
    return result
