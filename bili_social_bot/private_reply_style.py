"""仅对指定私信收件人应用本地句尾偏好，不增加模型调用。"""
from __future__ import annotations

import json
import re

SENTENCE_SUFFIXES: dict[str, str] = {}
_PROTECTED = re.compile(r"https?://[^\s\u3000-\u9fff。！？]+|`[^`\n]*`|\[[^\]\n]{1,100}\]")
_ENDING = re.compile(r"(?P<body>.*?)(?P<punct>[。！？!?…．.]+[\"'”’」』）》）]*)(?P<space>\s*)$", re.S)


def _spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for match in _PROTECTED.finditer(text):
        end = match.end()
        if match.group().startswith(("http://", "https://")):
            end -= len(match.group()) - len(match.group().rstrip(".,!"))
        spans.append((match.start(), end))
    return spans


def _sentences(text: str) -> list[str]:
    spans = _spans(text)
    parts = []
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if any(left <= index < right for left, right in spans):
            index += 1
            continue
        if char not in "。！？!?…．.\n":
            index += 1
            continue
        if char == "." and 0 < index < len(text) - 1 and text[index - 1].isdigit() and text[index + 1].isdigit():
            index += 1
            continue
        # 连续的 ?!、省略号和右引号属于同一句。
        end = index + 1
        if char != "\n":
            while end < len(text) and text[end] in "。！？!?…．.\"'”’」』）》）":
                end += 1
        parts.append(text[start:end])
        start = index = end
    if start < len(text):
        parts.append(text[start:])
    return parts


def _add_suffix(part: str, suffix: str) -> str:
    match = _ENDING.fullmatch(part)
    if match:
        body, punctuation, space = match.group("body", "punct", "space")
    else:
        body = part.rstrip()
        punctuation = ""
        space = part[len(body):]
    if not body.strip():
        return part
    # 句尾已经是喵/喵～时不叠加，普通正文中出现“喵”不算句尾。
    if re.search(re.escape(suffix) + r"[～~\s]*$", body):
        return part
    spans = _spans(body)
    separator = " " if spans and spans[-1][1] == len(body) and body[spans[-1][0]:].startswith(("http://", "https://")) else ""
    return body + separator + suffix + punctuation + space


def _fits(text: str, max_bytes: int) -> bool:
    return len(text.encode("utf-8")) <= max_bytes and len(json.dumps({"content": text}).encode("utf-8")) <= 1900


def apply_private_reply_style(uid: str | int, text: str, *, max_bytes: int = 900) -> str:
    suffix = SENTENCE_SUFFIXES.get(str(uid).strip())
    if not suffix:
        return text
    text = str(text or "").strip()
    if not text:
        return text
    limit = min(900, max(30, int(max_bytes)))
    result = ""
    for part in _sentences(text):
        styled = _add_suffix(part, suffix)
        if _fits(result + styled, limit):
            result += styled
            continue
        # 优先保留完整句子；一条长句需裁剪时给“喵”与省略号预留空间。
        if result.strip():
            break
        left, right = 0, len(part)
        while left < right:
            middle = (left + right + 1) // 2
            candidate = part[:middle].rstrip() + "…" + suffix
            if _fits(candidate, limit):
                left = middle
            else:
                right = middle - 1
        cut = left
        for begin, end in _spans(part):
            if begin < cut < end:
                cut = begin
                break
        result = part[:cut].rstrip() + "…" + suffix
        break
    return result.rstrip()
