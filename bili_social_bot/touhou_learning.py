from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .utils import ensure_parent, load_json, now_iso, save_json_atomic


DEFAULT_CATEGORIES = ["story", "character", "meme", "bilibili_meme", "fanwork", "music", "comment_style", "safety"]


@dataclass
class LearningEntry:
    id: str
    title: str
    content: str
    category: str = "meme"
    source: str = "manual"
    tags: list[str] | None = None
    official: bool = False
    confidence: str = "medium"
    created_at: str = ""
    updated_at: str = ""


class TouhouLearningDB:
    """Small local long-term memory for Touhou stories and memes.

    The DB stores summaries/notes written by the user or extracted by the bot.
    It intentionally stores *summaries and observations*, not large verbatim
    reposts of official texts, to stay compatible with Touhou usage guidelines.
    """

    def __init__(self, path: str = "data/touhou_learning.json", max_entries: int = 2000):
        self.path = path
        self.max_entries = max_entries
        self.data = load_json(path, self._default())
        if not isinstance(self.data, dict):
            self.data = self._default()
        self.data.setdefault("entries", [])
        self.data.setdefault("updated_at", now_iso())

    @staticmethod
    def _default() -> dict[str, Any]:
        return {"version": 1, "updated_at": now_iso(), "entries": []}

    def save(self) -> None:
        self.data["updated_at"] = now_iso()
        entries = self.data.setdefault("entries", [])
        if len(entries) > self.max_entries:
            del entries[:-self.max_entries]
        save_json_atomic(self.path, self.data)

    def add(self, title: str, content: str, category: str = "meme", source: str = "manual", tags: list[str] | None = None, official: bool = False, confidence: str = "medium") -> dict[str, Any]:
        title = str(title or "").strip()[:160]
        content = _clean_content(content)
        if not title:
            title = content[:40] or "未命名知识"
        if not content:
            raise ValueError("学习内容不能为空")
        category = category if category in DEFAULT_CATEGORIES else "meme"
        tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
        entry_id = _entry_id(title, content)
        entries = self.data.setdefault("entries", [])
        existing = next((x for x in entries if x.get("id") == entry_id), None)
        now = now_iso()
        if existing:
            existing.update({
                "title": title,
                "content": content,
                "category": category,
                "source": source,
                "tags": sorted(set((existing.get("tags") or []) + tags)),
                "official": bool(official),
                "confidence": confidence,
                "updated_at": now,
            })
            self.save()
            return existing
        item = {
            "id": entry_id,
            "title": title,
            "content": content,
            "category": category,
            "source": source,
            "tags": tags,
            "official": bool(official),
            "confidence": confidence,
            "created_at": now,
            "updated_at": now,
        }
        entries.append(item)
        self.save()
        return item

    def import_markdown(self, file_path: str, category: str = "meme", source: str = "file") -> int:
        p = Path(file_path)
        text = p.read_text(encoding="utf-8")
        chunks = _split_markdown(text)
        count = 0
        for title, content in chunks:
            if len(content.strip()) < 20:
                continue
            self.add(title=title, content=content, category=category, source=f"{source}:{p.name}", tags=_guess_tags(title + "\n" + content), confidence="medium")
            count += 1
        return count

    def search(self, query: str, limit: int = 8, categories: list[str] | None = None) -> list[dict[str, Any]]:
        query = str(query or "").strip()
        if not query:
            return []
        cats = set(categories or [])
        tokens = _tokens(query)
        scored: list[tuple[int, dict[str, Any]]] = []
        for item in self.data.get("entries", []):
            if cats and item.get("category") not in cats:
                continue
            hay = "\n".join([
                str(item.get("title") or ""),
                str(item.get("content") or ""),
                " ".join(item.get("tags") or []),
                str(item.get("category") or ""),
            ]).lower()
            score = 0
            for tok in tokens:
                if tok in hay:
                    score += 3 if tok in str(item.get("title") or "").lower() else 1
            # 主人新增的官方资料摘要应优先于普通评论区梗/二创学习条目，
            # 这样在回答设定、剧情、人物关系时先用可靠资料，再参考社区语境。
            if score and item.get("official"):
                score += 2
            source_text = str(item.get("source") or "")
            if score and "thwiki_popularity_poll" in source_text:
                score += 10
            if score and "owner_new_doc" in source_text:
                score += 3
            if score:
                scored.append((score, item))
        scored.sort(key=lambda x: (x[0], x[1].get("updated_at", "")), reverse=True)
        return [x[1] for x in scored[:limit]]

    def context_for(self, query: str, limit: int = 8, max_chars: int = 6000) -> str:
        hits = self.search(query, limit=limit)
        parts: list[str] = []
        for item in hits:
            flag = "官方资料摘要" if item.get("official") else "同人/社区知识"
            parts.append(
                f"【{flag}｜{item.get('category')}｜{item.get('title')}】\n"
                f"标签：{', '.join(item.get('tags') or [])}\n"
                f"内容：{str(item.get('content') or '')[:900]}"
            )
        return "\n\n".join(parts)[:max_chars]

    def summary(self, limit: int = 40, max_chars: int = 10000) -> str:
        entries = list(self.data.get("entries", []))[-limit:]
        parts = [f"【{x.get('category')}｜{x.get('title')}】{str(x.get('content') or '')[:240]}" for x in entries]
        return "\n".join(parts)[:max_chars]


def _clean_content(text: str) -> str:
    text = str(text or "").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:5000]


def _entry_id(title: str, content: str) -> str:
    import hashlib
    return hashlib.sha1((title.strip() + "\n" + content.strip()[:500]).encode("utf-8")).hexdigest()[:16]


def _tokens(query: str) -> list[str]:
    q = query.lower()
    # Keep CJK chunks and latin words.
    found = re.findall(r"[a-z0-9_+-]{2,}|[\u4e00-\u9fff]{2,}", q)
    extra = []
    for chunk in re.findall(r"[\u4e00-\u9fff]{4,}", q):
        extra.extend(chunk[i:i+2] for i in range(0, len(chunk)-1, 2))
    return list(dict.fromkeys(found + extra))


def _guess_tags(text: str) -> list[str]:
    candidates = [
        "古明地恋", "恋恋", "古明地觉", "博丽灵梦", "雾雨魔理沙", "十六夜咲夜", "蕾米莉亚", "芙兰朵露",
        "东方Project", "东方同人", "东方二创", "touhou", "地灵殿", "红魔乡", "妖妖梦", "永夜抄", "风神录",
        "无意识", "第三只眼", "电话", "菜刀", "弹幕", "同人音乐", "手书", "MMD", "梗",
    ]
    low = text.lower()
    return [c for c in candidates if c.lower() in low][:12]


def _split_markdown(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines()
    chunks: list[tuple[str, list[str]]] = []
    current_title = "导入知识"
    current: list[str] = []
    for line in lines:
        if line.startswith("#"):
            if current:
                chunks.append((current_title, current))
            current_title = line.lstrip("#").strip() or current_title
            current = []
        else:
            current.append(line)
    if current:
        chunks.append((current_title, current))
    return [(t, "\n".join(c).strip()) for t, c in chunks]
