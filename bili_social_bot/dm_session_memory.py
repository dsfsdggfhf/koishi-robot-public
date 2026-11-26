"""Private, bounded, process-only conversation and image continuity cache.

No disk writes, network access, raw URL retention or model calls. Only confirmed
sent replies belong in add_confirmed_reply; unknown delivery must never call it.
"""
from __future__ import annotations

import hashlib
import math
import time
from datetime import datetime
from typing import Any, Iterable


def _number(value: Any) -> float:
    try:
        result = float(value or 0)
        if result > 1e14:
            result /= 1e6
        elif result > 1e11:
            result /= 1000
        return result if result >= 0 and math.isfinite(result) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _sequence(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _clip(value: Any, length: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= length:
        return text
    head = (length - 1) * 2 // 3
    return text[:head] + "…" + text[-(length - head - 1):]


def _image_key(urls: Iterable[str], task_key: str = "") -> str:
    # Respect source ordering: different selected images must not share a key.
    if isinstance(urls, str):
        urls = [urls]
    values = list(dict.fromkeys(str(url).strip() for url in urls if str(url).strip()))
    if not values:
        return ""
    return hashlib.sha256(("\0".join(values) + ("\0任务:" + task_key if task_key else "")).encode("utf-8")).hexdigest()


class DMSessionMemory:
    def __init__(self, *, max_users: int = 200, reply_limit: int = 6,
                 reply_ttl_seconds: int = 1800, image_limit: int = 3,
                 image_ttl_seconds: int = 86400, image_note_seconds: int = 900):
        self.max_users = max(1, min(1000, int(max_users)))
        self.reply_limit = max(1, min(6, int(reply_limit)))
        self.reply_ttl_seconds = max(1, min(1800, int(reply_ttl_seconds)))
        self.image_limit = max(1, min(3, int(image_limit)))
        self.image_ttl_seconds = max(1, min(86400, int(image_ttl_seconds)))
        self.image_note_seconds = max(1, min(900, int(image_note_seconds)))
        self._users: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _now(value: float | None) -> float:
        return time.time() if value is None else _number(value)

    def _prune(self, now: float) -> None:
        for uid, data in list(self._users.items()):
            data["replies"] = [row for row in data["replies"]
                               if 0 <= now - row["saved_at"] <= self.reply_ttl_seconds]
            data["images"] = [row for row in data["images"]
                              if 0 <= now - row["saved_at"] <= self.image_ttl_seconds]
            if not data["replies"] and not data["images"]:
                self._users.pop(uid, None)

    def _for_write(self, uid: str, now: float) -> dict[str, Any]:
        self._prune(now)
        data = self._users.setdefault(uid, {"replies": [], "images": [], "last_used": now})
        data["last_used"] = now
        if len(self._users) > self.max_users:
            oldest = min(self._users, key=lambda user: self._users[user]["last_used"])
            self._users.pop(oldest, None)
        return data

    def add_confirmed_reply(self, uid: str | int, msg_key: Any, incoming: str, reply: str,
                            timestamp: float | None = None, *, now: float | None = None,
                            delivery_status: str = "sent", incoming_seq: int = 0) -> bool:
        """Invoke after confirmed send only. A status can explicitly guard callers."""
        uid, reply = str(uid).strip(), _clip(reply, 900)
        if not uid or not reply or delivery_status != "sent":
            return False
        clock = self._now(now if now is not None else timestamp)
        sent_at = _number(timestamp) if timestamp is not None else clock
        if not sent_at:
            sent_at = clock
        key = str(msg_key or "")
        data = self._for_write(uid, clock)
        rows = data["replies"]
        if key:
            rows[:] = [row for row in rows if row["incoming_key"] != key]
        rows.append({"uid": uid, "action": "dm_auto_reply", "delivery_status": "sent",
                     "incoming_key": key, "incoming_seq": _sequence(incoming_seq),
                     "incoming": " ".join(str(incoming or "").split())[:200], "reply": reply,
                     "time": datetime.fromtimestamp(sent_at).isoformat(), "saved_at": clock})
        del rows[:-self.reply_limit]
        return True

    def confirmed_replies(self, uid: str | int, now: float | None = None) -> list[dict[str, Any]]:
        clock = self._now(now)
        self._prune(clock)
        return [{key: value for key, value in row.items() if key != "saved_at"}
                for row in self._users.get(str(uid).strip(), {}).get("replies", [])]

    def remember_image(self, uid: str | int, urls: Iterable[str], context: str, *,
                       sequence: int = 0, timestamp: float | None = None,
                       now: float | None = None, task_key: str = "") -> bool:
        uid, key, raw_context = str(uid).strip(), _image_key(urls, task_key), str(context or "")
        if (not uid or not key or not raw_context.strip() or any(marker in raw_context for marker in
                ("未获得可靠视觉描述", "未能看清", "图片分析失败", "图片理解失败", "未得到可靠图片描述"))):
            return False
        context = _clip(raw_context, 600)
        clock = self._now(now)
        observed_at = _number(timestamp) if timestamp is not None else clock
        data = self._for_write(uid, clock)
        rows = data["images"]
        rows[:] = [row for row in rows if row["key"] != key]
        rows.append({"key": key, "source_key": _image_key(urls), "task_key": task_key,
                     "context": context, "sequence": _sequence(sequence),
                     "timestamp": observed_at, "saved_at": clock})
        del rows[:-self.image_limit]
        return True

    def find_image(self, uid: str | int, urls: Iterable[str], now: float | None = None, *, task_key: str = "", allow_general_reuse: bool = False) -> str:
        clock, key = self._now(now), _image_key(urls, task_key)
        self._prune(clock)
        if not key:
            return ""
        for row in reversed(self._users.get(str(uid).strip(), {}).get("images", [])):
            if row["key"] == key:
                return row["context"]
        # Broad follow-ups such as "这张好看吗" can use existing whole-image
        # observations. Explicit reading/UI/detail tasks require their own key.
        if allow_general_reuse and task_key == "general|whole":
            source_key = _image_key(urls)
            for row in reversed(self._users.get(str(uid).strip(), {}).get("images", [])):
                if row.get("source_key") == source_key and str(row.get("task_key") or "").endswith("|whole"):
                    return row["context"]
        return ""

    def latest_image_note(self, uid: str | int, *, current_seq: int = 0,
                          current_timestamp: float | None = None,
                          now: float | None = None, task_key: str = "",
                          allow_general_reuse: bool = False) -> str:
        clock = self._now(now)
        self._prune(clock)
        seq, current_ts = _sequence(current_seq), _number(current_timestamp)
        for row in reversed(self._users.get(str(uid).strip(), {}).get("images", [])):
            if task_key and row.get("task_key") != task_key:
                if not (allow_general_reuse and task_key == "general|whole" and
                        str(row.get("task_key") or "").endswith("|whole")):
                    continue
            image_seq, image_ts = row["sequence"], row["timestamp"]
            if seq > 0 and image_seq > 0:
                if image_seq >= seq:
                    continue
                if current_ts and image_ts and image_ts >= current_ts:
                    continue
            elif not current_ts or not image_ts or image_ts >= current_ts:
                continue
            reference_ts = current_ts or clock
            if not image_ts or not 0 < reference_ts - image_ts <= self.image_note_seconds:
                continue
            return row["context"]
        return ""
