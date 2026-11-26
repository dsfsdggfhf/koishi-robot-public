from __future__ import annotations

from typing import Any, Awaitable, Callable

from .bili_client import VideoItem


DEFAULT_BLOCKED_COMMENT_TAG_KEYWORDS = [
    "战争", "反战", "战地", "战火", "军事冲突", "俄乌", "巴以", "以色列", "巴勒斯坦", "加沙", "乌克兰",
]


def comment_tag_safety_config(data: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict((data or {}).get("comment_tag_safety") or {})
    cfg.setdefault("enabled", True)
    cfg.setdefault("blocked_keywords", DEFAULT_BLOCKED_COMMENT_TAG_KEYWORDS)
    cfg.setdefault("also_check_title_desc", False)
    cfg.setdefault("block_on_tag_fetch_error", False)
    return cfg


async def blocked_by_comment_tags(
    item: VideoItem,
    *,
    config: dict[str, Any] | None,
    fetch_tags: Callable[[str], Awaitable[list[str]]],
    logger=None,
) -> tuple[bool, str]:
    """Return whether active video comments should be skipped for sensitive tags.

    This is intentionally scoped to outbound active video comments. It does not
    affect DMs, @ replies, health checks, or small-station reads.
    """
    cfg = comment_tag_safety_config(config)
    if not cfg.get("enabled", True):
        return False, ""
    keywords = [str(x).strip().lower() for x in (cfg.get("blocked_keywords") or []) if str(x).strip()]
    if not keywords:
        return False, ""

    tags = list(item.tags or [])
    if not tags and item.bvid:
        try:
            tags = await fetch_tags(item.bvid)
            item.tags = tags
        except Exception as exc:
            if cfg.get("block_on_tag_fetch_error", False):
                return True, f"标签获取失败，保守跳过：{exc}"
            if logger:
                logger(f"获取视频标签失败，按已有信息继续检查 {item.bvid}：{exc}", "WARN")

    tag_hay = " ".join(tags).lower()
    matched = [k for k in keywords if k and k in tag_hay]
    if matched:
        return True, "命中禁止评论标签关键词：" + "、".join(matched)

    if cfg.get("also_check_title_desc", False):
        hay = f"{item.title}\n{item.desc}".lower()
        matched_text = [k for k in keywords if k and k in hay]
        if matched_text:
            return True, "标题/简介命中禁止评论关键词：" + "、".join(matched_text)
    return False, ""
