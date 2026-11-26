"""Comment notification positioning and conversational reply validation."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from .safety import looks_like_analysis, validate_comment, validate_message


def positive_int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def resolve_comment_position(item):
    oid = positive_int(item.get("subject_id"))
    root = positive_int(item.get("root_id")) or positive_int(item.get("target_id"))
    source = positive_int(item.get("source_id"))
    native = str(item.get("native_uri") or "")
    query = parse_qs(urlparse(native).query)
    if not root:
        root = positive_int((query.get("comment_root_id") or [0])[0])
    secondary = positive_int((query.get("comment_secondary_id") or [0])[0])
    explicit_video = (str(item.get("type") or "").lower() == "video"
                      or str(item.get("business") or "") == "视频"
                      or bool(re.match(r"bilibili://video/\d+", native)))
    if not oid and explicit_video:
        match = re.match(r"bilibili://video/(\d+)", native)
        if match:
            oid = positive_int(match.group(1))
    if not root and explicit_video:
        root = source
    return oid, root, source or secondary or root


def validate_conversation_reply(text, forbidden_words, marker="", require_marker=True, *, preserve_quotes=False):
    ok, reason, safe = validate_comment(text, forbidden_words, marker=marker, require_marker=require_marker)
    # 保留禁用词、注入、长度、空泛模板检查；对话不用视频点评的字数/细节配额。
    if not ok and reason in {"缺少具体内容细节", "正文信息量过低"}:
        # 对话中的直接答案可以很短；仍拦截纯表情/音符，保留全部安全审核。
        body = re.sub(r"\[[^\]]*\]|[♪♬♩♫]", "", safe)
        if not any(ch.isalnum() for ch in body):
            return False, "正文信息量过低", safe
        if not preserve_quotes and looks_like_analysis(safe):
            return False, "回复包含分析式文本", safe
        return validate_message(safe, forbidden_words, marker=marker, require_marker=require_marker)
    return ok, reason, safe
