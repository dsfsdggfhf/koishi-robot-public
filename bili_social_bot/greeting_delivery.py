"""早晚安的等待计算与限流暂停；不改变原有发送最小间隔。"""
from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path


def is_greeting_risk_error(error: BaseException) -> bool:
    """检查包装后的异常原因；单个收件人拒收和消息过长不当成账号限流。"""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).lower()
        code = str(getattr(current, "code", ""))
        if code in {"412", "429", "-509", "-352", "21026", "21016"}:
            return True
        if any(term in text for term in (
            "请求过于频繁", "发送过于频繁", "操作太频繁", "操作频繁",
            "频率过快", "验证码", "风控", "risk control", "rate limit",
            "too many requests", "captcha",
        )):
            return True
        if re.search(r"(?<!\d)(?:HTTP\s*)?(?:412|429|-509|-352|21026|21016)(?!\d)", text, re.I):
            return True
        current = current.__cause__ or current.__context__
    return False


def greeting_wait_remaining(target_gap: float, last_text_at: float | None, now: float) -> float:
    """表情包和其它等待已经消耗了间隔，只补足剩余时间。"""
    gap = max(0.0, float(target_gap))
    if last_text_at is None:
        return gap
    elapsed = max(0.0, float(now) - float(last_text_at))
    return max(0.0, gap - elapsed)


def _read_pause(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("问候暂停状态格式无效")
    until = float(data.get("until", 0))
    if not math.isfinite(until):
        raise ValueError("问候暂停时间无效")
    return data


def greeting_pause_remaining(path: Path, now: float | None = None) -> float:
    current = time.time() if now is None else float(now)
    try:
        return max(0.0, float(_read_pause(path).get("until", 0)) - current)
    except (OSError, ValueError, TypeError):
        # 状态损坏时停止发送，避免重启清空保护；由日志提示人工检查。
        return 600.0


def pause_scheduled_greetings(path: Path, now: float | None = None) -> int:
    current = time.time() if now is None else float(now)
    try:
        prior = _read_pause(path)
    except (OSError, ValueError, TypeError):
        prior = {}
    try:
        last_at = float(prior.get("last_at", 0))
        strikes = int(prior.get("strikes", 0)) if current - last_at < 86400 else 0
    except (TypeError, ValueError, OverflowError):
        strikes = 0
    strikes = min(5, max(0, strikes) + 1)
    seconds = min(7200, 600 * 2 ** (strikes - 1))
    until = max(float(prior.get("until", 0)), current + seconds)
    data = {"until": until, "last_at": current, "strikes": strikes, "reason": "platform_rate_or_verification"}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return int(until - current)
