from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass
class FreeModelGuardResult:
    """Result of strict local validation for free-model generated outbound text.

    The guard is intentionally conservative for B站自动评论：宁可跳过，也不要把
    模型的英文思考、约束复述、半截内容发出去。
    """

    ok: bool
    text: str
    reason: str = ""
    raw: str = ""


# 常见“思考/约束泄露”片段。命中即拒绝，不尝试抢救。
_META_LEAK_MARKERS = [
    "the user wants",
    "constraints:",
    "constraint:",
    "we need",
    "we should",
    "the comment",
    "analysis:",
    "reasoning:",
    "system:",
    "assistant:",
    "user:",
    "i need to",
    "need to write",
    "final answer",
    "用户想要",
    "用户要求",
    "要求：",
    "约束：",
    "限制：",
    "只输出",
    "禁止英文",
    "禁止解释",
    "不要解释",
    "帖子内容：",
    "原始输出",
]

_THINK_RE = re.compile(r"<think>.*?</think>", re.I | re.S)
_CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
# 两个及以上连续英文字母才算英文泄露；这样不会误杀“B站”。
_ENGLISH_WORD_RE = re.compile(r"[A-Za-z]{2,}")


def _normalize_text(raw: str, *, first_line_only: bool = True) -> str:
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _THINK_RE.sub("", text)
    text = _CODE_FENCE_RE.sub("", text)
    text = text.strip().strip('"“”')
    # 去掉常见列表符号/前缀，但不把明显泄露的“评论：xxx”当作可用内容抢救。
    lines: list[str] = []
    for line in text.split("\n"):
        s = line.strip().strip('"“”')
        s = re.sub(r"^[-*•\d.、\s]+", "", s).strip()
        if s:
            lines.append(s)
    if not lines:
        return ""
    # 评论/短文本只取第一行，避免模型输出多段说明；早晚安帖子保留全文。
    return lines[0] if first_line_only else "\n".join(lines)


def _chinese_count(text: str) -> int:
    return len(_CHINESE_RE.findall(text or ""))


def guard_free_model_output(
    raw: str,
    *,
    kind: str = "comment",
    min_chinese_chars: int = 8,
    max_chars: int = 90,
    reject_english: bool = True,
    allowed_english_words: Iterable[str] | None = None,
    forbidden_words: Iterable[str] | None = None,
) -> FreeModelGuardResult:
    """Strictly validate a free-model output before any B站 outbound action.

    Defaults are tuned for automatic 小站评论. For public greeting posts, callers can
    raise max_chars and disable reject_english if they explicitly want to allow words
    like "Project/Koishi".
    """

    raw_text = str(raw or "")
    if not raw_text.strip():
        return FreeModelGuardResult(False, "", "empty_output", raw_text)

    low = raw_text.lower()
    for marker in _META_LEAK_MARKERS:
        if marker.lower() in low:
            return FreeModelGuardResult(False, "", f"meta_leak:{marker}", raw_text)

    text = _normalize_text(raw_text, first_line_only=(kind in {"comment", "station_comment"}))
    if not text:
        return FreeModelGuardResult(False, "", "empty_after_clean", raw_text)

    if reject_english:
        allowed = {str(x or "").strip().lower() for x in (allowed_english_words or []) if str(x or "").strip()}
        found_words = [m.group(0) for m in _ENGLISH_WORD_RE.finditer(text)]
        bad_words = [w for w in found_words if w.lower() not in allowed]
        if bad_words:
            return FreeModelGuardResult(False, "", "english_detected:" + ",".join(bad_words[:5]), raw_text)

    cjk = _chinese_count(text)
    if cjk <= 0:
        return FreeModelGuardResult(False, "", "no_chinese", raw_text)
    if cjk < int(min_chinese_chars or 0):
        return FreeModelGuardResult(False, "", f"too_short:{cjk}", raw_text)

    for word in forbidden_words or []:
        w = str(word or "").strip()
        if w and w in text:
            return FreeModelGuardResult(False, "", f"forbidden_word:{w}", raw_text)

    max_chars = int(max_chars or 0)
    if max_chars > 0 and len(text) > max_chars:
        # 自动评论宁可不发，不截断；截断可能改变语义或留下半句。
        return FreeModelGuardResult(False, "", f"too_long:{len(text)}", raw_text)

    return FreeModelGuardResult(True, text, "", raw_text)
