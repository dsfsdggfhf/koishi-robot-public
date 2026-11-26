from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .utils import load_json, now_iso, resolve_path, save_json_atomic


_COMMENTING_STALE_HOURS = 6
_LOCK_STALE_SECONDS = 120
_LOCK_WAIT_SECONDS = 15
_INTERACTIVE_SENDING_STALE_SECONDS = 300
_INTERACTIVE_TERMINAL = {"sent", "blocked", "ignored", "old", "invalid", "unknown"}
_GREETING_SENDING_STALE_MINUTES = 15
_FAN_THANKS_SENDING_STALE_MINUTES = 30
_GREETING_TERMINAL_MAPS = ("sent_map", "blocked_map", "abandoned_map", "delivery_unknown_map")
_HISTORY_TEXT_FIELDS = {"text", "reply", "incoming", "command", "body", "message", "content"}
_HISTORY_SECRET_RE = re.compile(
    r"(?i)(?:sk|rk)-[A-Za-z0-9_-]{8,}|(?:SESSDATA|bili_jct|ac_time_value|buvid3)\s*[:=]\s*[^,;\s]+"
)


def _history_safe_item(item: Any) -> dict[str, Any]:
    """Keep runtime history useful for metrics without retaining message bodies."""
    if not isinstance(item, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in item.items():
        key_s = str(key)
        if isinstance(value, str):
            redacted = _HISTORY_SECRET_RE.sub("[REDACTED]", value)
            if key_s.lower() in _HISTORY_TEXT_FIELDS and redacted:
                redacted = (
                    f"[文本已脱敏 chars={len(redacted)} "
                    f"bytes={len(redacted.encode('utf-8', errors='replace'))}]"
                )
            out[key] = redacted
        else:
            out[key] = value
    return out


def interactive_status_done(status: Any) -> bool:
    """Whether an interactive_replied status means the item is already handled.

    'sending:<ts>' 只在时间戳新鲜时算“进行中”；一旦超过
    _INTERACTIVE_SENDING_STALE_SECONDS（默认 5 分钟；前一个进程死在发送与标记之间），
    按已处理处理，避免重启/并发补回复把同一条评论/私信再发一遍。
    解析失败一律视为已处理（宁可漏一条，也不重复发）。
    """
    if not status:
        return False
    if status in _INTERACTIVE_TERMINAL:
        return True
    if isinstance(status, str) and status.startswith("sending:"):
        ts = status.split(":", 1)[1]
        try:
            dt = datetime.fromisoformat(ts)
        except Exception:
            return True
        return datetime.now() - dt > timedelta(seconds=_INTERACTIVE_SENDING_STALE_SECONDS)
    return False  # 旧的裸 "sending"（仅内存）或未知状态 → 未处理


def interactive_status_claimable(status: Any) -> bool:
    """是否允许本进程重新占用该互动键（发送前占位判定）。

    - 终态 / 过期 sending → 不可占用（已处理）。
    - 新鲜 sending:<ts> → 不可占用（另一个进程正在发送，避免重复回复）。
    - 空 / failed / 旧版裸 sending → 可占用（重试或首次处理）。
    - unknown 是投递结果不明的终态，必须不可占用，避免重复私信。
    """
    if interactive_status_done(status):
        return False
    if isinstance(status, str) and status.startswith("sending:"):
        return False  # 新鲜和过期发送占位都不得抢占。
    return True


def default_state() -> dict[str, Any]:
    return {
        "last_action_at": "",
        "daily": {},
        "commented_videos": {},
        "commenting_videos": {},
        "shared_videos": {},
        "shared_pending": {},
        "comment_delivery_unknown": {},
        "fan_thanks_sent": {},
        "fan_thanks_pending": {},
        "fan_thanks_unknown": {},
        "interactive_replied": {},
        "interactive_last_active": "",
        "unfollowed_cache": {},
        "owner_notify_ts": {},
        "greeting_batch": {},
        "greeting_quiet": [],
        "rule_runs": {},
        "history": [],
    }


def _today_key() -> str:
    try:
        from .scheduler import current_time
        return current_time().date().isoformat()
    except Exception:
        return date.today().isoformat()


def _parse_dt(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _is_fresh_ts(value: Any, *, hours: int = _COMMENTING_STALE_HOURS) -> bool:
    dt = _parse_dt(value)
    if dt is None:
        return False
    return datetime.now() - dt < timedelta(hours=hours)


def _is_fresh_greeting_ts(value: Any) -> bool:
    dt = _parse_dt(value)
    return dt is not None and datetime.now() - dt < timedelta(minutes=_GREETING_SENDING_STALE_MINUTES)


def _ensure_defaults(data: Any) -> dict[str, Any]:
    out = data if isinstance(data, dict) else default_state()
    for key, value in default_state().items():
        out.setdefault(key, value.copy() if isinstance(value, dict) else list(value) if isinstance(value, list) else value)
    return out


def _merge_history(a: list[Any], b: list[Any]) -> list[Any]:
    """Merge append-only history without dropping records from another process."""
    out: list[Any] = []
    seen: set[str] = set()
    for item in list(a or []) + list(b or []):
        safe_item = _history_safe_item(item) if isinstance(item, dict) else item
        try:
            key = repr(sorted(safe_item.items())) if isinstance(safe_item, dict) else repr(safe_item)
        except Exception:
            key = repr(safe_item)
        if key in seen:
            continue
        seen.add(key)
        out.append(safe_item)
    out.sort(key=lambda x: str(x.get("time", "")) if isinstance(x, dict) else "")
    return out[-1000:]


def _merge_status_map(a: Any, b: Any, *, terminal: set[str] | None = None) -> dict[str, Any]:
    terminal = terminal or set()
    out: dict[str, Any] = {}
    for source in (a or {}, b or {}):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            key_s = str(key)
            old = out.get(key_s)
            old_s = str(old or "")
            value_s = str(value or "")
            if old is None:
                out[key_s] = value
            elif old_s in terminal:
                # 主人要求私信尽量每条都回：dm 的 ignored/old/invalid/failed
                # 不再作为不可覆盖终态；unknown 表示投递结果不明，可能已经发出，必须保持终态避免重复私信。
                if key_s.startswith("dm:") and old_s in {"ignored", "old", "invalid", "failed"}:
                    # 允许 DM 的旧失败/忽略状态被新一轮 sending/sent 覆盖；但不要让过期 sending
                    # 从旧内存快照倒灌回来，把刚清理成 failed 的私信再次卡住。
                    if value_s.startswith("sending:") and interactive_status_done(value):
                        continue
                    out[key_s] = value
                else:
                    continue
            elif old_s.startswith("sending:") and interactive_status_done(old):
                # 磁盘上是过期的 sending 占位（超过90秒）：说明占位进程早已死掉或
                # 状态从未被推进，允许被本进程的 failed/sent/unknown 覆盖，
                # 否则“保证回复”每轮都会在 claim 处被旧 sending 卡死。
                out[key_s] = value
            elif value_s in terminal:
                out[key_s] = value
            elif value_s.startswith("sending:") and not old_s.startswith("sending:"):
                out[key_s] = value
            elif value_s > old_s:
                out[key_s] = value
    return out


def _merge_greeting_entry(a: Any, b: Any) -> dict[str, Any]:
    left = a if isinstance(a, dict) else {}
    right = b if isinstance(b, dict) else {}
    left_date = str(left.get("date") or "")
    right_date = str(right.get("date") or "")
    base = dict(right) if right_date > left_date else dict(left)
    if left_date != right_date:
        return base
    for key in ("sent_map", "blocked_map", "abandoned_map", "delivery_unknown_map", "failed_map"):
        base[key] = _merge_status_map(left.get(key), right.get(key))
    fail_count: dict[str, int] = {}
    for source in (left.get("fail_count") or {}, right.get("fail_count") or {}):
        if isinstance(source, dict):
            for uid, count in source.items():
                try:
                    fail_count[str(uid)] = max(fail_count.get(str(uid), 0), int(count or 0))
                except Exception:
                    pass
    base["fail_count"] = fail_count
    sending: dict[str, Any] = {}
    for source in (left.get("sending_map") or {}, right.get("sending_map") or {}):
        if isinstance(source, dict):
            for uid, stamp in source.items():
                if _is_fresh_greeting_ts(stamp):
                    sending[str(uid)] = stamp
    base["sending_map"] = sending
    left_snapshot = left.get("recipients_snapshot") if isinstance(left.get("recipients_snapshot"), list) else []
    right_snapshot = right.get("recipients_snapshot") if isinstance(right.get("recipients_snapshot"), list) else []
    if left.get("recipients_snapshot_verified_mutual") and right.get("recipients_snapshot_verified_mutual"):
        base["recipients_snapshot"] = right_snapshot or left_snapshot
        base["recipients_snapshot_verified_mutual"] = True
    elif left.get("recipients_snapshot_verified_mutual") and not right_snapshot:
        base["recipients_snapshot"] = left_snapshot
        base["recipients_snapshot_verified_mutual"] = True
    else:
        base["recipients_snapshot"] = right_snapshot or left_snapshot
        base["recipients_snapshot_verified_mutual"] = False
    snapshot_ids = {
        str(row.get("mid") or "").strip()
        for row in (base.get("recipients_snapshot") or [])
        if isinstance(row, dict) and str(row.get("mid") or "").strip()
    }
    terminal_ids: set[str] = set()
    for map_name in _GREETING_TERMINAL_MAPS:
        for uid, day in (base.get(map_name) or {}).items():
            if str(day or "") == left_date:
                terminal_ids.add(str(uid))
    if snapshot_ids and base.get("recipients_snapshot_verified_mutual"):
        # Derive completion from the merged terminal maps.  Using
        # left.done AND right.done made a stale process keep the batch false
        # forever, which woke the 45-second watchdog and repeatedly refreshed
        # complete relation lists after every recipient was already handled.
        base["done"] = snapshot_ids.issubset(terminal_ids)
    else:
        base["done"] = bool(left.get("done")) and bool(right.get("done"))
    return base


def _merge_state(latest: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Conservative cross-process merge for state saves.

    The bot can be restarted while another instance is still finishing a long
    video-analysis/comment round.  A plain atomic replace protects JSON integrity
    but can still overwrite newer counters with an older in-memory snapshot.  This
    merge keeps monotonic safety data (daily counters, commented videos, history)
    from both sides so the daily active-comment cap cannot be reset accidentally.
    """
    latest = _ensure_defaults(latest)
    current = _ensure_defaults(current)
    merged = dict(latest)

    # Preserve ordinary non-safety fields from the caller, then restore the
    # safety/append-only fields below with conflict-aware logic.
    for key, value in current.items():
        if key not in {
            "daily", "commented_videos", "commenting_videos", "shared_videos",
            "shared_pending", "comment_delivery_unknown", "interactive_replied",
            "fan_thanks_sent", "fan_thanks_pending", "fan_thanks_unknown",
            "interactive_last_active", "unfollowed_cache", "owner_notify_ts",
            "greeting_batch", "greeting_quiet", "rule_runs", "history",
        }:
            merged[key] = value

    daily: dict[str, dict[str, int]] = {}
    for source in (latest.get("daily") or {}, current.get("daily") or {}):
        if not isinstance(source, dict):
            continue
        for day, bucket in source.items():
            if not isinstance(bucket, dict):
                continue
            dst = daily.setdefault(str(day), {})
            for counter in {"comments", "shares"} | {str(k) for k in bucket.keys()}:
                try:
                    val = int(bucket.get(counter, 0) or 0)
                except Exception:
                    val = 0
                dst[counter] = max(int(dst.get(counter, 0) or 0), val)
    merged["daily"] = daily

    commented = {}
    commented.update(latest.get("commented_videos") or {})
    commented.update(current.get("commented_videos") or {})
    merged["commented_videos"] = commented

    commenting = _merge_status_map(latest.get("commenting_videos"), current.get("commenting_videos"))

    shared = {}
    shared.update(latest.get("shared_videos") or {})
    shared.update(current.get("shared_videos") or {})
    merged["shared_videos"] = shared
    pending = _merge_status_map(latest.get("shared_pending"), current.get("shared_pending"))
    for key in list(pending):
        if key in shared:
            pending.pop(key, None)
    merged["shared_pending"] = pending
    merged["comment_delivery_unknown"] = _merge_status_map(
        latest.get("comment_delivery_unknown"), current.get("comment_delivery_unknown")
    )
    for bvid, claim in list(commenting.items()):
        if bvid in merged["commented_videos"] or bvid in merged["comment_delivery_unknown"]:
            commenting.pop(bvid)
        elif not _is_fresh_ts(claim):
            # A worker may have crashed after Bilibili accepted the write.
            merged["comment_delivery_unknown"][bvid] = str(claim or now_iso())
            commenting.pop(bvid)
    merged["commenting_videos"] = commenting
    merged["fan_thanks_sent"] = _merge_status_map(
        latest.get("fan_thanks_sent"), current.get("fan_thanks_sent")
    )
    merged["fan_thanks_unknown"] = _merge_status_map(
        latest.get("fan_thanks_unknown"), current.get("fan_thanks_unknown")
    )
    fan_pending = _merge_status_map(
        latest.get("fan_thanks_pending"), current.get("fan_thanks_pending")
    )
    for uid in list(fan_pending):
        if uid in merged["fan_thanks_sent"] or uid in merged["fan_thanks_unknown"]:
            fan_pending.pop(uid, None)
    merged["fan_thanks_pending"] = fan_pending
    merged["interactive_replied"] = _merge_status_map(
        latest.get("interactive_replied"), current.get("interactive_replied"),
        terminal=_INTERACTIVE_TERMINAL,
    )
    merged["interactive_last_active"] = max(
        str(latest.get("interactive_last_active") or ""),
        str(current.get("interactive_last_active") or ""),
    )
    merged["unfollowed_cache"] = _merge_status_map(
        latest.get("unfollowed_cache"), current.get("unfollowed_cache")
    )
    merged["owner_notify_ts"] = _merge_status_map(
        latest.get("owner_notify_ts"), current.get("owner_notify_ts")
    )
    greeting_batch: dict[str, Any] = {}
    for kind in set((latest.get("greeting_batch") or {}).keys()) | set((current.get("greeting_batch") or {}).keys()):
        greeting_batch[str(kind)] = _merge_greeting_entry(
            (latest.get("greeting_batch") or {}).get(kind),
            (current.get("greeting_batch") or {}).get(kind),
        )
    merged["greeting_batch"] = greeting_batch
    quiet = []
    for value in list(latest.get("greeting_quiet") or []) + list(current.get("greeting_quiet") or []):
        value = str(value).strip()
        if value and value not in quiet:
            quiet.append(value)
    merged["greeting_quiet"] = quiet

    rule_runs = dict(latest.get("rule_runs") or {})
    for key, value in (current.get("rule_runs") or {}).items():
        old = str(rule_runs.get(key, ""))
        new = str(value or "")
        rule_runs[key] = max(old, new)
    merged["rule_runs"] = rule_runs

    merged["history"] = _merge_history(latest.get("history") or [], current.get("history") or [])
    merged["last_action_at"] = max(str(latest.get("last_action_at", "")), str(current.get("last_action_at", "")))
    # Explicit claim completion must survive another process's stale snapshot.
    releases = {}
    for source in (latest, current):
        for key, row in (source.get('interactive_finalized_claims') or {}).items():
            if isinstance(row, dict) and row.get('status') in {'failed','unknown','sent','blocked'} and str(row.get('claim','')) >= str(releases.get(key,{}).get('claim','')):
                releases[key] = dict(row)
    merged['interactive_finalized_claims'] = releases
    replied = merged.setdefault('interactive_replied', {})
    for key, row in releases.items():
        value = str(replied.get(key) or '')
        if value.startswith('sending:') and value <= str(row.get('claim') or ''):
            replied[key] = row['status']
    return merged


class BotState:
    def __init__(self, path: str):
        self.path = path
        data = load_json(path, default_state())
        self.data = _ensure_defaults(data)

    @property
    def _path(self) -> Path:
        return resolve_path(self.path)

    @property
    def _lock_path(self) -> Path:
        return self._path.with_suffix(self._path.suffix + ".lock")

    @contextmanager
    def _file_lock(self):
        lock = self._lock_path
        lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + _LOCK_WAIT_SECONDS
        fd: int | None = None
        while True:
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(fd, f"pid={os.getpid()} ts={now_iso()}\n".encode("utf-8", errors="ignore"))
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > _LOCK_STALE_SECONDS:
                        lock.unlink(missing_ok=True)
                        continue
                except Exception:
                    pass
                if time.time() >= deadline:
                    raise TimeoutError(f"等待状态锁超时：{lock}")
                time.sleep(0.1)
        try:
            yield
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
            try:
                lock.unlink(missing_ok=True)
            except Exception:
                pass

    def _load_latest_unlocked(self) -> dict[str, Any]:
        return _ensure_defaults(load_json(self.path, default_state()))

    def save(self) -> None:
        with self._file_lock():
            latest = self._load_latest_unlocked()
            merged = _merge_state(latest, self.data)
            save_json_atomic(self.path, merged)
            self.data = merged

    def today_bucket(self) -> dict[str, int]:
        today = _today_key()
        daily = self.data.setdefault("daily", {})
        bucket = daily.setdefault(today, {"comments": 0, "shares": 0})
        # 清理太旧的每日计数，避免文件无限增长
        for key in list(daily.keys()):
            if key != today:
                daily.pop(key, None)
        return bucket

    def can_comment_today(self, limit: int) -> bool:
        with self._file_lock():
            latest = self._load_latest_unlocked()
            self.data = _merge_state(latest, self.data)
            return int(self.data.setdefault("daily", {}).setdefault(_today_key(), {"comments": 0, "shares": 0}).get("comments", 0) or 0) < limit

    def comments_today(self) -> int:
        """今天已发评论数（与 can_comment_today 同一份计数，供日志与诊断用）。"""
        try:
            with self._file_lock():
                latest = self._load_latest_unlocked()
                self.data = _merge_state(latest, self.data)
                return int(
                    self.data.setdefault("daily", {})
                    .setdefault(_today_key(), {"comments": 0, "shares": 0})
                    .get("comments", 0) or 0
                )
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # 私信：按用户每天发多少条（用于限制刷屏/内容异常的用户）
    #
    # 为什么不放在 state.json 里：run 与 chat-run 两个进程都在写 state.json，
    # 各自用内存里的副本整体覆盖，实测这里写进去的计数 **15 秒内就被抹掉**
    # （2026-09-14 实测：写 1 → 15s 后变 None）。所以单独存一个文件，
    # 只有本模块读写它，天然避开了跨进程互相覆盖。
    # ------------------------------------------------------------------
    @property
    def _dm_daily_path(self) -> Path:
        # 注意：self.path 是 str（不是 Path），必须显式转换
        return Path(self.path).parent / "dm_daily.json"

    @contextmanager
    def _dm_daily_lock(self):
        lock = Path(self.path).parent / "dm_daily.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + 10
        fd: int | None = None
        while True:
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(fd, f"pid={os.getpid()} ts={now_iso()}\n".encode("utf-8", "ignore"))
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > 30:
                        lock.unlink(missing_ok=True)
                        continue
                except Exception:
                    pass
                if time.time() >= deadline:
                    raise TimeoutError(f"等待私信计数锁超时：{lock}")
                time.sleep(0.05)
        try:
            yield
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
            try:
                lock.unlink(missing_ok=True)
            except Exception:
                pass

    def _dm_daily_load(self) -> dict[str, Any]:
        # 注意：state.py 没有 import json（一直用 utils.load_json），这里也别直接 json.loads
        data = load_json(self._dm_daily_path, {})
        return data if isinstance(data, dict) else {}

    def dm_sent_today(self, uid: str | int) -> int:
        """今天已经给这个用户发过多少条私信。"""
        uid = str(uid or "").strip()
        if not uid.isdigit():
            return 0
        try:
            with self._dm_daily_lock():
                data = self._dm_daily_load()
            bucket = (data.get("users") or {}).get(uid) or {}
            return int(bucket.get(_today_key(), 0) or 0)
        except Exception:
            return 0

    def add_dm_sent(self, uid: str | int, n: int = 1) -> int:
        """给某用户累加今日私信计数（只保留最近 7 天）。返回累加后的值。"""
        uid = str(uid or "").strip()
        if not uid.isdigit() or int(n) <= 0:
            return 0
        try:
            with self._dm_daily_lock():
                data = self._dm_daily_load()
                users = data.setdefault("users", {})
                bucket = users.setdefault(uid, {})
                key = _today_key()
                bucket[key] = int(bucket.get(key, 0) or 0) + int(n)
                for u, b in list(users.items()):
                    if not isinstance(b, dict):
                        users[u] = {}
                        continue
                    keep = sorted(b.keys())[-7:]
                    users[u] = {k: b[k] for k in keep}
                data["updated_at"] = now_iso()
                save_json_atomic(self._dm_daily_path, data)
                return int(bucket[key])
        except Exception:
            return 0

    def dm_sent_today_all(self) -> dict[str, int]:
        """今天每个用户各发了多少条私信（供主人排查）。"""
        try:
            with self._dm_daily_lock():
                data = self._dm_daily_load()
            today = _today_key()
            return {u: int((b or {}).get(today, 0) or 0)
                    for u, b in (data.get("users") or {}).items()
                    if int((b or {}).get(today, 0) or 0) > 0}
        except Exception:
            return {}

    def can_share_today(self, limit: int) -> bool:
        with self._file_lock():
            latest = self._load_latest_unlocked()
            self.data = _merge_state(latest, self.data)
            return int(self.data.setdefault("daily", {}).setdefault(_today_key(), {"comments": 0, "shares": 0}).get("shares", 0) or 0) < limit

    def inc_comment(self) -> None:
        # Atomic enough for station comments and legacy callers.  Active video
        # comments use reserve_video_comment() before sending.
        with self._file_lock():
            latest = self._load_latest_unlocked()
            latest = _merge_state(latest, self.data)
            bucket = latest.setdefault("daily", {}).setdefault(_today_key(), {"comments": 0, "shares": 0})
            bucket["comments"] = int(bucket.get("comments", 0) or 0) + 1
            latest["last_action_at"] = now_iso()
            save_json_atomic(self.path, latest)
            self.data = latest

    def inc_share(self) -> None:
        with self._file_lock():
            latest = self._load_latest_unlocked()
            latest = _merge_state(latest, self.data)
            bucket = latest.setdefault("daily", {}).setdefault(_today_key(), {"comments": 0, "shares": 0})
            bucket["shares"] = int(bucket.get("shares", 0) or 0) + 1
            latest["last_action_at"] = now_iso()
            save_json_atomic(self.path, latest)
            self.data = latest

    def record_history(self, item: dict[str, Any]) -> None:
        history = self.data.setdefault("history", [])
        history.append({"time": now_iso(), **_history_safe_item(item)})
        del history[:-1000]

    def claim_interactive(self, keys: list[str]) -> bool:
        """发送前原子占用互动回复位（私信/评论回复/@/动态），跨进程 + 崩溃安全。

        两个 chat 进程（或重启重叠窗口）同时处理同一条评论/私信时，
        只有先抢到锁的进程能占用；占位落盘为 'sending:<ts>'，另一个进程看到
        新鲜 sending 会跳过，从而避免同一条消息被回复两遍。
        """
        keys = [str(k) for k in keys if str(k)]
        if not keys:
            return False
        with self._file_lock():
            latest = self._load_latest_unlocked()
            latest = _merge_state(latest, self.data)
            replied = latest.setdefault("interactive_replied", {})
            for k in keys:
                status = replied.get(k)
                # A stale send claim still lacks a platform receipt.
                if (isinstance(status, str) and status.startswith("sending:")
                        and interactive_status_done(status)):
                    replied[k] = "unknown"
                    latest.setdefault("interactive_finalized_claims", {})[k] = {"claim": status, "status": "unknown"}
                    save_json_atomic(self.path, latest)
                    self.data = latest
                    return False
                if not interactive_status_claimable(status):
                    self.data = latest
                    return False
            stamp = "sending:" + datetime.now().isoformat(timespec="microseconds")
            for k in keys:
                replied[k] = stamp
            latest["last_action_at"] = now_iso()
            save_json_atomic(self.path, latest)
            self.data = latest
            return True

    def finalize_interactive(self, keys: list[str], status: str, *, expected_claim: str) -> bool:
        """Complete our exact claim atomically; never override another sender."""
        if status not in {'failed', 'unknown', 'sent', 'blocked'}:
            raise ValueError('invalid completion status')
        with self._file_lock():
            latest = _merge_state(self._load_latest_unlocked(), self.data)
            replied = latest.setdefault('interactive_replied', {})
            if any(replied.get(key) != expected_claim for key in keys):
                self.data = latest
                return False
            for key in keys:
                replied[key] = status
                latest.setdefault('interactive_finalized_claims', {})[key] = {'claim': expected_claim, 'status': status}
            save_json_atomic(self.path, latest)
            self.data = latest
            return True

    def is_video_commented(self, rule_name: str, bvid: str) -> bool:
        # 全局永久去重：只看 bvid，与规则名无关
        return (
            bvid in self.data.setdefault("commented_videos", {})
            or bvid in self.data.setdefault("comment_delivery_unknown", {})
        )

    def is_video_commenting(self, bvid: str) -> bool:
        # 发送前占位：避免同一视频在短时间内被下一轮/下一规则再次发送评论。
        ts = self.data.setdefault("commenting_videos", {}).get(bvid)
        return bool(ts and _is_fresh_ts(ts))

    def mark_video_comment_skipped(self, bvid: str, reason: str = "") -> None:
        """记录"整套分析做完、最终却没能发出评论"的视频，避免反复付费分析同一支。

        背景（2026-09-11 实测）：丢弃率从 08 月底的 2% 涨到 09-10 的 52%，
        而 BV1U9t66iEge 这类视频被完整分析后丢弃了 5 次 —— 每次都是全额付费
        （视觉增强 + 角色识别 + 生成 + 质量门重试），产出为零。跨轮/跨规则
        记住"这支不值得再花钱"，冷却期内直接不进入候选。
        """
        if not bvid:
            return
        with self._file_lock():
            latest = self._load_latest_unlocked()
            latest = _merge_state(latest, self.data)
            skipped = latest.setdefault("comment_skipped_videos", {})
            skipped[str(bvid)] = {"at": now_iso(), "reason": str(reason or "")[:80]}
            # 只保留最近 800 条，避免无限增长（按时间排序丢弃最旧的）
            if len(skipped) > 800:
                ordered = sorted(skipped, key=lambda k: str((skipped[k] or {}).get("at") or ""))
                for k in ordered[:-800]:
                    skipped.pop(k, None)
            latest["last_action_at"] = now_iso()
            save_json_atomic(self.path, latest)
            self.data = latest

    def video_comment_skipped_recently(self, bvid: str, cooldown_hours: int = 168) -> bool:
        """该视频是否在冷却期内已被判定"分析完也发不出去"。"""
        if cooldown_hours <= 0:
            return False
        rec = (self.data.setdefault("comment_skipped_videos", {}) or {}).get(str(bvid))
        ts = (rec or {}).get("at") if isinstance(rec, dict) else rec
        return _is_fresh_ts(ts, hours=int(cooldown_hours))

    def mark_video_commenting(self, bvid: str) -> bool:
        # Legacy API: reserve without a daily limit.  Prefer reserve_video_comment
        # for active outbound comments.
        with self._file_lock():
            latest = self._load_latest_unlocked()
            latest = _merge_state(latest, self.data)
            if bvid in latest.setdefault("commented_videos", {}):
                self.data = latest
                return False
            if bvid in latest.setdefault("comment_delivery_unknown", {}):
                self.data = latest
                return False
            commenting = latest.setdefault("commenting_videos", {})
            if bvid in commenting and _is_fresh_ts(commenting.get(bvid)):
                self.data = latest
                return False
            commenting[bvid] = now_iso()
            save_json_atomic(self.path, latest)
            self.data = latest
            return True

    def reserve_video_comment(self, rule_name: str, bvid: str, daily_limit: int) -> tuple[bool, str]:
        """Atomically reserve one active video-comment slot before sending.

        This prevents both duplicate BVID comments and cross-process daily-limit
        overruns.  The daily counter is intentionally incremented before the
        network send; if the send fails, cancel_video_comment_reservation()
        releases it.  If a process dies after B站 accepts the comment, the slot
        remains counted, which is safer than exceeding the promised cap.
        """
        with self._file_lock():
            latest = self._load_latest_unlocked()
            latest = _merge_state(latest, self.data)
            today = _today_key()
            bucket = latest.setdefault("daily", {}).setdefault(today, {"comments": 0, "shares": 0})
            used = int(bucket.get("comments", 0) or 0)
            if used >= max(0, int(daily_limit or 0)):
                self.data = latest
                return False, f"今日主动评论额度已用完：{used}/{daily_limit}"
            if bvid in latest.setdefault("commented_videos", {}):
                self.data = latest
                return False, "该视频已评论过"
            if bvid in latest.setdefault("comment_delivery_unknown", {}):
                self.data = latest
                return False, "该视频评论结果未知，禁止自动重发"
            commenting = latest.setdefault("commenting_videos", {})
            ts = commenting.get(bvid)
            if ts and _is_fresh_ts(ts):
                self.data = latest
                return False, "该视频已有发送中占位"
            commenting[bvid] = now_iso()
            bucket["comments"] = used + 1
            latest["last_action_at"] = now_iso()
            save_json_atomic(self.path, latest)
            self.data = latest
            return True, "reserved"

    def cancel_video_comment_reservation(self, bvid: str) -> None:
        with self._file_lock():
            latest = self._load_latest_unlocked()
            latest = _merge_state(latest, self.data)
            ts = latest.setdefault("commenting_videos", {}).pop(bvid, None)
            if ts:
                day = str(ts)[:10]
                bucket = latest.setdefault("daily", {}).setdefault(day, {"comments": 0, "shares": 0})
                bucket["comments"] = max(0, int(bucket.get("comments", 0) or 0) - 1)
            latest["last_action_at"] = now_iso()
            save_json_atomic(self.path, latest)
            self.data = latest

    def clear_video_commenting(self, bvid: str) -> None:
        self.data.setdefault("commenting_videos", {}).pop(bvid, None)

    def mark_video_commented(self, rule_name: str, bvid: str) -> None:
        self.data.setdefault("commented_videos", {})[bvid] = now_iso()
        self.clear_video_commenting(bvid)

    def finish_reserved_video_comment(self, rule_name: str, bvid: str, history_item: dict[str, Any]) -> None:
        """Finalize a successful pre-reserved active video comment."""
        with self._file_lock():
            latest = self._load_latest_unlocked()
            latest = _merge_state(latest, self.data)
            latest.setdefault("commented_videos", {})[bvid] = now_iso()
            latest.setdefault("commenting_videos", {}).pop(bvid, None)
            history = latest.setdefault("history", [])
            history.append({"time": now_iso(), **history_item})
            del history[:-1000]
            latest["last_action_at"] = now_iso()
            save_json_atomic(self.path, latest)
            self.data = latest

    def mark_video_comment_unknown(self, rule_name: str, bvid: str, history_item: dict[str, Any]) -> None:
        """Record an ambiguous comment result as terminal; keep the daily slot used."""
        with self._file_lock():
            latest = self._load_latest_unlocked()
            latest = _merge_state(latest, self.data)
            stamp = now_iso()
            latest.setdefault("comment_delivery_unknown", {})[bvid] = stamp
            latest.setdefault("commented_videos", {})[bvid] = stamp
            latest.setdefault("commenting_videos", {}).pop(bvid, None)
            history = latest.setdefault("history", [])
            history.append({"time": stamp, **history_item})
            del history[:-1000]
            latest["last_action_at"] = stamp
            save_json_atomic(self.path, latest)
            self.data = latest

    def is_video_shared(self, rule_name: str, recipient_uid: str, bvid: str) -> bool:
        key = f"{rule_name}:{recipient_uid}:{bvid}"
        return (
            key in self.data.setdefault("shared_videos", {})
            or key in self.data.setdefault("shared_pending", {})
        )

    def reserve_video_share(self, rule_name: str, recipient_uid: str, bvid: str, daily_limit: int) -> tuple[bool, str]:
        key = f"{rule_name}:{recipient_uid}:{bvid}"
        with self._file_lock():
            latest = self._load_latest_unlocked()
            latest = _merge_state(latest, self.data)
            bucket = latest.setdefault("daily", {}).setdefault(_today_key(), {"comments": 0, "shares": 0})
            used = int(bucket.get("shares", 0) or 0)
            if used >= max(0, int(daily_limit or 0)):
                self.data = latest
                return False, f"今日分享额度已用完：{used}/{daily_limit}"
            if key in latest.setdefault("shared_videos", {}):
                self.data = latest
                return False, "该视频已分享过"
            if key in latest.setdefault("shared_pending", {}):
                self.data = latest
                return False, "该分享已有发送中/结果未知占位"
            latest["shared_pending"][key] = now_iso()
            bucket["shares"] = used + 1
            latest["last_action_at"] = now_iso()
            save_json_atomic(self.path, latest)
            self.data = latest
            return True, "reserved"

    def finish_video_share(self, rule_name: str, recipient_uid: str, bvid: str) -> None:
        key = f"{rule_name}:{recipient_uid}:{bvid}"
        with self._file_lock():
            latest = self._load_latest_unlocked()
            latest = _merge_state(latest, self.data)
            latest.setdefault("shared_pending", {}).pop(key, None)
            latest.setdefault("shared_videos", {})[key] = now_iso()
            latest["last_action_at"] = now_iso()
            save_json_atomic(self.path, latest)
            self.data = latest

    def cancel_video_share_reservation(self, rule_name: str, recipient_uid: str, bvid: str) -> None:
        key = f"{rule_name}:{recipient_uid}:{bvid}"
        with self._file_lock():
            latest = self._load_latest_unlocked()
            latest = _merge_state(latest, self.data)
            stamp = latest.setdefault("shared_pending", {}).pop(key, None)
            if stamp:
                day = str(stamp)[:10]
                bucket = latest.setdefault("daily", {}).setdefault(day, {"comments": 0, "shares": 0})
                bucket["shares"] = max(0, int(bucket.get("shares", 0) or 0) - 1)
            latest["last_action_at"] = now_iso()
            save_json_atomic(self.path, latest)
            self.data = latest

    def mark_video_shared(self, rule_name: str, recipient_uid: str, bvid: str) -> None:
        key = f"{rule_name}:{recipient_uid}:{bvid}"
        self.data.setdefault("shared_pending", {}).pop(key, None)
        self.data.setdefault("shared_videos", {})[key] = now_iso()

    def claim_new_fan_thanks(self, uid: str, daily_limit: int | None = None) -> bool:
        """Atomically reserve one new-fan thank-you message.

        The old implementation used a standalone JSON list and marked it only
        after sending.  Two chat/main processes could therefore both send to a
        new follower, and an ambiguous network result would be retried later.
        A fresh pending reservation suppresses concurrent sends; a stale one is
        converted to an unknown terminal state (manual verification required).
        """
        uid = str(uid or "").strip()
        if not uid:
            return False
        with self._file_lock():
            latest = _merge_state(self._load_latest_unlocked(), self.data)
            sent = latest.setdefault("fan_thanks_sent", {})
            unknown = latest.setdefault("fan_thanks_unknown", {})
            pending = latest.setdefault("fan_thanks_pending", {})
            if uid in sent or uid in unknown:
                self.data = latest
                return False
            today_bucket = latest.setdefault("daily", {}).setdefault(_today_key(), {"comments": 0, "shares": 0})
            if daily_limit is not None:
                try:
                    limit = max(0, int(daily_limit))
                except Exception:
                    limit = 0
                if int(today_bucket.get("fan_thanks", 0) or 0) >= limit:
                    self.data = latest
                    return False
            stamp = pending.get(uid)
            if stamp:
                try:
                    fresh = datetime.now() - datetime.fromisoformat(str(stamp)) < timedelta(minutes=_FAN_THANKS_SENDING_STALE_MINUTES)
                except Exception:
                    fresh = False
                if fresh:
                    self.data = latest
                    return False
                # A crashed/unknown sender must never be automatically replayed.
                pending.pop(uid, None)
                unknown[uid] = now_iso()
                latest["last_action_at"] = now_iso()
                save_json_atomic(self.path, latest)
                self.data = latest
                return False
            pending[uid] = now_iso()
            if daily_limit is not None:
                today_bucket["fan_thanks"] = int(today_bucket.get("fan_thanks", 0) or 0) + 1
            latest["last_action_at"] = now_iso()
            save_json_atomic(self.path, latest)
            self.data = latest
            return True

    def finish_new_fan_thanks(self, uid: str) -> None:
        uid = str(uid or "").strip()
        if not uid:
            return
        with self._file_lock():
            latest = _merge_state(self._load_latest_unlocked(), self.data)
            latest.setdefault("fan_thanks_pending", {}).pop(uid, None)
            latest.setdefault("fan_thanks_sent", {})[uid] = now_iso()
            latest.setdefault("fan_thanks_unknown", {}).pop(uid, None)
            latest["last_action_at"] = now_iso()
            save_json_atomic(self.path, latest)
            self.data = latest

    def mark_new_fan_thanks_unknown(self, uid: str) -> None:
        uid = str(uid or "").strip()
        if not uid:
            return
        with self._file_lock():
            latest = _merge_state(self._load_latest_unlocked(), self.data)
            latest.setdefault("fan_thanks_pending", {}).pop(uid, None)
            latest.setdefault("fan_thanks_unknown", {})[uid] = now_iso()
            latest["last_action_at"] = now_iso()
            save_json_atomic(self.path, latest)
            self.data = latest

    def cancel_new_fan_thanks(self, uid: str) -> None:
        """Release a definitely-not-sent reservation so a later poll may retry."""
        uid = str(uid or "").strip()
        if not uid:
            return
        with self._file_lock():
            latest = _merge_state(self._load_latest_unlocked(), self.data)
            stamp = latest.setdefault("fan_thanks_pending", {}).pop(uid, None)
            if stamp:
                day = str(stamp)[:10]
                bucket = latest.setdefault("daily", {}).setdefault(day, {"comments": 0, "shares": 0})
                bucket["fan_thanks"] = max(0, int(bucket.get("fan_thanks", 0) or 0) - 1)
            latest["last_action_at"] = now_iso()
            save_json_atomic(self.path, latest)
            self.data = latest

    def _greeting_entry(self, kind: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        target = data if data is not None else self.data
        batches = target.setdefault("greeting_batch", {})
        entry = batches.setdefault(kind, {"date": "", "done": False})
        if not isinstance(entry, dict):
            entry = batches[kind] = {"date": "", "done": False}
        for key in ("sent_map", "blocked_map", "abandoned_map", "failed_map", "sending_map", "delivery_unknown_map", "fail_count"):
            if not isinstance(entry.get(key), dict):
                entry[key] = {}
        if not isinstance(entry.get("recipients_snapshot"), list):
            entry["recipients_snapshot"] = []
        if "recipients_snapshot_verified_mutual" not in entry:
            entry["recipients_snapshot_verified_mutual"] = False
        return entry

    def claim_greeting(self, kind: str, uid: str, today: str) -> bool:
        uid = str(uid).strip()
        with self._file_lock():
            latest = self._load_latest_unlocked()
            latest = _merge_state(latest, self.data)
            entry = self._greeting_entry(kind, latest)
            if str(entry.get("date") or "") != str(today):
                self.data = latest
                return False
            for map_name in _GREETING_TERMINAL_MAPS:
                if str(entry.get(map_name, {}).get(uid, "")) == str(today):
                    self.data = latest
                    return False
            sending = entry.setdefault("sending_map", {})
            stamp = sending.get(uid)
            if stamp and _is_fresh_greeting_ts(stamp):
                self.data = latest
                return False
            sending[uid] = now_iso()
            latest["last_action_at"] = now_iso()
            save_json_atomic(self.path, latest)
            self.data = latest
            return True

    def _mark_greeting_terminal(self, kind: str, uid: str, today: str, map_name: str) -> None:
        with self._file_lock():
            latest = self._load_latest_unlocked()
            latest = _merge_state(latest, self.data)
            entry = self._greeting_entry(kind, latest)
            entry.setdefault(map_name, {})[str(uid)] = str(today)
            entry.setdefault("sending_map", {}).pop(str(uid), None)
            latest["last_action_at"] = now_iso()
            save_json_atomic(self.path, latest)
            self.data = latest

    def mark_greeting_sent(self, kind: str, uid: str, today: str) -> None:
        self._mark_greeting_terminal(kind, uid, today, "sent_map")

    def mark_greeting_blocked(self, kind: str, uid: str, today: str) -> None:
        self._mark_greeting_terminal(kind, uid, today, "blocked_map")

    def mark_greeting_unknown(self, kind: str, uid: str, today: str) -> None:
        self._mark_greeting_terminal(kind, uid, today, "delivery_unknown_map")

    def record_greeting_failure(self, kind: str, uid: str, today: str) -> int:
        with self._file_lock():
            latest = self._load_latest_unlocked()
            latest = _merge_state(latest, self.data)
            entry = self._greeting_entry(kind, latest)
            uid = str(uid)
            count = int(entry.setdefault("fail_count", {}).get(uid, 0) or 0) + 1
            entry["fail_count"][uid] = count
            entry.setdefault("failed_map", {})[uid] = str(today)
            entry.setdefault("sending_map", {}).pop(uid, None)
            if count >= 3:
                entry.setdefault("abandoned_map", {})[uid] = str(today)
            latest["last_action_at"] = now_iso()
            save_json_atomic(self.path, latest)
            self.data = latest
            return count

    def add_greeting_quiet(self, uid: str) -> None:
        with self._file_lock():
            latest = self._load_latest_unlocked()
            latest = _merge_state(latest, self.data)
            quiet = latest.setdefault("greeting_quiet", [])
            uid = str(uid).strip()
            if uid and uid not in quiet:
                quiet.append(uid)
            latest["last_action_at"] = now_iso()
            save_json_atomic(self.path, latest)
            self.data = latest

    def get_rule_last_run_key(self, rule_name: str) -> str:
        return str(self.data.setdefault("rule_runs", {}).get(rule_name, ""))

    def set_rule_last_run_key(self, rule_name: str, key: str) -> None:
        self.data.setdefault("rule_runs", {})[rule_name] = key
