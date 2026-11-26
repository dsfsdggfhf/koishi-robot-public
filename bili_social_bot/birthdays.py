from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .bili_client import BiliClient, _get_wbi_mixin_key
from .config import BotConfig
from .scheduler import current_time, is_rule_due
from .utils import load_json, now_iso, resolve_path, save_json_atomic


def _pick_rows(page: Any) -> list[dict[str, Any]]:
    if not isinstance(page, dict):
        return []
    rows = page.get("list")
    if isinstance(rows, dict):
        rows = rows.get("list") or rows.get("items") or []
    if not isinstance(rows, list):
        rows = []
    return [r for r in rows if isinstance(r, dict)]


def _mid(row: dict[str, Any]) -> str:
    return str(row.get("mid") or row.get("uid") or "").strip()


def _uname(row: dict[str, Any]) -> str:
    return str(row.get("uname") or row.get("name") or "").strip()


async def _pace(state: dict[str, float], gap: float) -> None:
    gap = max(0.2, float(gap or 0.8))
    loop = asyncio.get_event_loop()
    now = loop.time()
    wait = gap - (now - float(state.get("last", 0.0) or 0.0))
    if wait > 0:
        await asyncio.sleep(wait)
    state["last"] = loop.time()


async def get_followings_map(client: BiliClient, *, max_users: int = 2000, gap: float = 0.8) -> tuple[dict[str, str], bool]:
    from bilibili_api import user as bili_user

    if not client.uid or not client.credential:
        return {}, False
    u = bili_user.User(uid=int(client.uid), credential=client.credential)
    out: dict[str, str] = {}
    complete = True
    pn = 1
    ps = 50
    pace_state = {"last": 0.0}
    while len(out) < max_users:
        await _pace(pace_state, gap)
        page = await u.get_followings(pn=pn, ps=ps)
        rows = _pick_rows(page)
        if not rows:
            break
        for row in rows:
            mid = _mid(row)
            if mid and mid != str(client.uid):
                out[mid] = _uname(row)
            if len(out) >= max_users:
                complete = False
                break
        if len(out) >= max_users:
            break
        if len(rows) < ps:
            break
        pn += 1
    return out, complete


async def get_followers_map(client: BiliClient, *, max_users: int = 3000, gap: float = 0.8) -> tuple[dict[str, str], bool]:
    from bilibili_api import user as bili_user

    if not client.uid or not client.credential:
        return {}, False
    u = bili_user.User(uid=int(client.uid), credential=client.credential)
    out: dict[str, str] = {}
    complete = True
    pn = 1
    ps = 50
    pace_state = {"last": 0.0}
    while len(out) < max_users:
        await _pace(pace_state, gap)
        page = await u.get_followers(pn=pn, ps=ps)
        rows = _pick_rows(page)
        if not rows:
            break
        for row in rows:
            mid = _mid(row)
            if mid and mid != str(client.uid):
                out[mid] = _uname(row)
            if len(out) >= max_users:
                complete = False
                break
        if len(out) >= max_users:
            break
        if len(rows) < ps:
            break
        pn += 1
    return out, complete


async def get_wbi_mixin_key(client: BiliClient, *, gap: float = 0.8, pace_state: dict[str, float] | None = None) -> str:
    if pace_state is not None:
        await _pace(pace_state, gap)
    http = await client._http_client()
    resp = await http.get("https://api.bilibili.com/x/web-interface/nav", cookies=client.cookies or None)
    resp.raise_for_status()
    data = resp.json()
    wbi_img = ((data.get("data") or {}).get("wbi_img") or {}) if isinstance(data, dict) else {}
    img_key = Path(urllib.parse.urlparse(str(wbi_img.get("img_url") or "")).path).stem
    sub_key = Path(urllib.parse.urlparse(str(wbi_img.get("sub_url") or "")).path).stem
    return _get_wbi_mixin_key(img_key + sub_key)


def wbi_sign(params: dict[str, Any], mixin_key: str) -> dict[str, str]:
    if not mixin_key:
        return {str(k): str(v) for k, v in params.items()}
    signed = dict(params)
    signed["wts"] = int(time.time())
    clean: dict[str, str] = {}
    for k, v in signed.items():
        clean[str(k)] = re.sub(r"[!'()*]", "", str(v))
    query = urllib.parse.urlencode(sorted(clean.items()))
    clean["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return clean


def normalize_birthday(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    low = text.lower()
    if low in {"0", "null", "none", "false", "保密", "未知", "未公开", "--"}:
        return ""
    if text in {"0000-00-00", "0000-0-0", "00-00", "0-0", "1970-01-01"}:
        return ""
    if re.fullmatch(r"0{1,4}[-/.年]0{1,2}([-/.月]0{1,2}日?)?", text):
        return ""
    return text


def find_birthday(data: dict[str, Any]) -> str:
    candidates: list[Any] = []
    for container in (data, data.get("card") if isinstance(data.get("card"), dict) else None):
        if not isinstance(container, dict):
            continue
        for key in ("birthday", "birth", "birth_day", "birthday_md", "birth_date"):
            if key in container:
                candidates.append(container.get(key))
    for item in candidates:
        b = normalize_birthday(item)
        if b:
            return b
    return ""


def birthday_month_day(value: Any) -> str:
    text = normalize_birthday(value)
    if not text:
        return ""
    # Accept YYYY-MM-DD / MM-DD / M月D日 / YYYY年M月D日.
    m = re.search(r"(?:(?:\d{4})\s*[-/.年]\s*)?(\d{1,2})\s*(?:[-/.月])\s*(\d{1,2})", text)
    if not m:
        return ""
    month = int(m.group(1))
    day = int(m.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{month:02d}-{day:02d}"


def _safe_date(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        # For 02-29 in non-leap years, remind on Mar 1 rather than crashing.
        if month == 2 and day == 29:
            return date(year, 3, 1)
        raise


def is_possible_default_birthday(md: str) -> bool:
    """B站资料里 01-01 很常见，提醒时标低可信而不是直接当真。"""
    return str(md or "").strip() == "01-01"


def next_birthday_date(md: str, today: date | None = None) -> date | None:
    if not re.fullmatch(r"\d{2}-\d{2}", str(md or "")):
        return None
    today = today or current_time().date()
    month, day = [int(x) for x in md.split("-")]
    try:
        target = _safe_date(today.year, month, day)
        if target < today:
            target = _safe_date(today.year + 1, month, day)
        return target
    except Exception:
        return None


async def get_profile_birthday(
    client: BiliClient,
    uid: str,
    mixin_key: str,
    *,
    gap: float = 0.8,
    pace_state: dict[str, float] | None = None,
) -> tuple[bool, str, str]:
    """Return (queried, birthday, error_type). Birthday is from normal public/logged-in APIs only."""
    http = await client._http_client()
    pace_state = pace_state if pace_state is not None else {"last": 0.0}

    params = wbi_sign({"mid": uid, "token": "", "platform": "web", "web_location": "1550101"}, mixin_key)
    try:
        await _pace(pace_state, gap)
        resp = await http.get("https://api.bilibili.com/x/space/wbi/acc/info", params=params, cookies=client.cookies or None)
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("code") == 0 and isinstance(payload.get("data"), dict):
            return True, find_birthday(payload.get("data") or {}), ""
        if isinstance(payload, dict) and payload.get("code") in {-352, -412, -509}:
            return False, "", str(payload.get("code"))
        if isinstance(payload, dict) and payload.get("code") in {-400, -403, -404, 22115}:
            return True, "", str(payload.get("code"))
    except Exception:
        pass

    try:
        await _pace(pace_state, gap)
        resp = await http.get("https://api.bilibili.com/x/web-interface/card", params={"mid": uid}, cookies=client.cookies or None)
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("code") == 0 and isinstance(payload.get("data"), dict):
            return True, find_birthday(payload.get("data") or {}), ""
        return False, "", str(payload.get("code") if isinstance(payload, dict) else "bad_json")
    except Exception as exc:
        return False, "", type(exc).__name__


async def scan_mutual_birthdays_with_client(
    client: BiliClient,
    *,
    max_followings: int = 2000,
    max_followers: int = 3000,
    max_birthday_check: int = 0,
    gap: float = 0.8,
    mutual_users_override: dict[str, str] | None = None,
    previous_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if mutual_users_override is not None:
        clean_mutual = {
            str(uid).strip(): str(uname or "")
            for uid, uname in mutual_users_override.items()
            if str(uid).strip()
        }
        followings = dict(clean_mutual)
        followers = dict(clean_mutual)
        following_complete = True
        follower_complete = True
        relation_source = "verified_mutual_cache"
    else:
        followings, following_complete = await get_followings_map(client, max_users=max(1, int(max_followings)), gap=gap)
        followers, follower_complete = await get_followers_map(client, max_users=max(1, int(max_followers)), gap=gap)
        relation_source = "live_lists"
    mutual_ids = sorted(set(followings) & set(followers), key=lambda x: int(x) if x.isdigit() else 0)
    scan_ids = mutual_ids[:max_birthday_check] if max_birthday_check and max_birthday_check > 0 else mutual_ids
    today = current_time().date()
    mixin_key = await get_wbi_mixin_key(client, gap=gap, pace_state={"last": 0.0})
    pace_state = {"last": 0.0}

    visible: list[dict[str, Any]] = []
    hidden: list[dict[str, str]] = []
    failed_users: list[dict[str, str]] = []
    deferred_users: list[dict[str, str]] = []
    stop_reason = ""
    consecutive_failures = 0
    query_success = 0
    query_failed = 0
    error_counts: dict[str, int] = {}
    for index, uid in enumerate(scan_ids):
        queried, birthday, err = await get_profile_birthday(client, uid, mixin_key, gap=gap, pace_state=pace_state)
        uname = followings.get(uid) or followers.get(uid) or ""
        if queried:
            query_success += 1
            consecutive_failures = 0
        else:
            query_failed += 1
            consecutive_failures += 1
            failed_users.append({"mid": uid, "uname": uname, "error": err or "unknown"})
            error_counts[err or "unknown"] = error_counts.get(err or "unknown", 0) + 1
        md = birthday_month_day(birthday)
        if birthday and md:
            next_dt = next_birthday_date(md, today=today)
            possible_default = is_possible_default_birthday(md)
            visible.append({
                "mid": uid,
                "uname": uname,
                "birthday": birthday,
                "birthday_md": md,
                "birthday_confidence": "low_possible_default" if possible_default else "normal",
                "birthday_note": "01-01 在B站资料中常见，可能是默认/随手填日期，祝福前建议确认。" if possible_default else "",
                "next_birthday": next_dt.isoformat() if next_dt else "",
                "days_until": (next_dt - today).days if next_dt else None,
                "last_seen": now_iso(),
            })
        elif queried:
            hidden.append({"mid": uid, "uname": uname, "last_seen": now_iso()})
        if not queried and (err in {"-352", "-412", "-509"} or consecutive_failures >= 3):
            stop_reason = err if err in {"-352", "-412", "-509"} else "network_backoff"
            deferred_users = [{"mid": other, "uname": followings.get(other) or followers.get(other) or ""}
                              for other in scan_ids[index + 1:]]
            break
    # Preserve old publicly known dates only for failed/unqueried profiles that
    # remain mutual. A successful privacy/no-date response removes the old date.
    fresh_ids = {str(u['mid']) for u in visible}
    unresolved = {str(u['mid']) for u in failed_users + deferred_users}
    for old in (previous_result or {}).get('users') or []:
        uid = str(old.get('mid') or '')
        if uid in unresolved and uid in mutual_ids and uid not in fresh_ids:
            row = dict(old)
            row['birthday_source'] = 'cached_after_query_failure'
            next_dt = next_birthday_date(str(row.get('birthday_md') or ''), today=today)
            row['next_birthday'] = next_dt.isoformat() if next_dt else ''
            row['days_until'] = (next_dt-today).days if next_dt else None
            visible.append(row)
            fresh_ids.add(uid)

    visible.sort(key=lambda x: (int(x["days_until"]) if x.get("days_until") is not None else 9999, str(x.get("uname") or ""), str(x.get("mid") or "")))
    result: dict[str, Any] = {
        "ok": True,
        "updated_at": now_iso(),
        "self_uid": str(client.uid or ""),
        "followings_count": len(followings),
        "followers_count": len(followers),
        "mutual_count": len(mutual_ids),
        "following_list_complete": following_complete,
        "follower_list_complete": follower_complete,
        "birthday_checked_count": query_success + query_failed,
        "birthday_query_success_count": query_success,
        "birthday_query_failed_count": query_failed,
        "birthday_visible_count": len(visible),
        "birthday_hidden_or_missing_count": len(hidden),
        "birthday_scan_complete": len(scan_ids) == len(mutual_ids) and not query_failed and not deferred_users and following_complete and follower_complete,
        "birthday_traversal_complete": not deferred_users and len(scan_ids) == len(mutual_ids),
        "failed_users": failed_users,
        "deferred_users": deferred_users,
        "stop_reason": stop_reason,
        "retry_after_ts": time.time() + (21600 if stop_reason in {"-352", "-412", "-509"} else 3600) if query_failed or deferred_users else 0,
        "cached_dates_retained": sum(u.get('birthday_source') == 'cached_after_query_failure' for u in visible),
        "relation_source": relation_source,
        "users": visible,
        "hidden_or_missing_users": hidden,
        "note": "只保存/使用正常公开或登录后资料接口返回的生日；不绕过隐私设置。",
    }
    if error_counts:
        result["query_error_counts"] = error_counts
    return result


async def scan_mutual_birthdays(
    config: BotConfig,
    *,
    max_followings: int = 2000,
    max_followers: int = 3000,
    max_birthday_check: int = 0,
    gap: float = 0.8,
) -> dict[str, Any]:
    client = BiliClient(config.cookie_file, min_gap_seconds=max(0.2, float(gap or 0.8)))
    try:
        if not client.load_cookies():
            return {"ok": False, "error": "cookie_missing", "updated_at": now_iso()}
        if not await client.ensure_login():
            return {"ok": False, "error": "login_invalid", "detail": getattr(client, "last_login_error", ""), "updated_at": now_iso()}
        return await scan_mutual_birthdays_with_client(
            client,
            max_followings=max_followings,
            max_followers=max_followers,
            max_birthday_check=max_birthday_check,
            gap=gap,
        )
    finally:
        await client.close()


def save_birthday_cache(path: str | Path, result: dict[str, Any]) -> None:
    save_json_atomic(path, result)


def load_birthday_state(path: str | Path) -> dict[str, Any]:
    data = load_json(path, {"notified": {}})
    if not isinstance(data, dict):
        data = {"notified": {}}
    data.setdefault("notified", {})
    return data


def save_birthday_state(path: str | Path, state: dict[str, Any]) -> None:
    save_json_atomic(path, state)


def users_due_in_days(result: dict[str, Any], days: int = 1) -> list[dict[str, Any]]:
    return [u for u in (result.get("users") or []) if isinstance(u, dict) and u.get("days_until") is not None and int(u["days_until"]) == int(days)]


def birthday_notify_key(user: dict[str, Any]) -> str:
    return f"{user.get('next_birthday') or ''}:{user.get('mid') or ''}"


def build_owner_birthday_message(users: list[dict[str, Any]], *, owner_name: str = "管理员") -> str:
    if not users:
        return ""
    lines = []
    for u in users[:30]:
        name = str(u.get("uname") or "这位互关朋友")
        uid = str(u.get("mid") or "")
        birthday = str(u.get("birthday") or u.get("birthday_md") or "")
        next_day = str(u.get("next_birthday") or "")
        note = "（可能是默认生日，建议确认）" if is_possible_default_birthday(str(u.get("birthday_md") or "")) else ""
        lines.append(f"- {name}（UID {uid}）：{birthday}，明天 {next_day}{note}")
    default_count = sum(1 for u in users if is_possible_default_birthday(str(u.get("birthday_md") or "")))
    default_note = ""
    if default_count:
        default_note = f"\n其中 {default_count} 位是 01-01，B站资料里这个日期可能是默认/随手填，祝福前最好确认一下。"
    more = "" if len(users) <= 30 else f"\n还有 {len(users) - 30} 位没有展开。"
    return (
        f"诶～{owner_name}，Koishi 提醒一下：明天有 {len(users)} 位互关朋友过生日。\n"
        "如果要送祝福，可以提前准备一下哦。"
        + default_note
        + "\n\n"
        + "\n".join(lines)
        + more
    )


def format_birthday_stats(result: dict[str, Any], *, include_users: bool = True) -> str:
    if not result.get("ok"):
        return f"生日检测失败：{result.get('error') or 'unknown'} {result.get('detail') or ''}".strip()
    lines = [
        f"更新时间：{result.get('updated_at') or ''}",
        f"关注数：{result.get('followings_count')}",
        f"粉丝数：{result.get('followers_count')}",
        f"确认互关数：{result.get('mutual_count')}",
        f"已检测互关：{result.get('birthday_checked_count')}",
        f"能检测到生日：{result.get('birthday_visible_count')}",
        f"未公开/接口未返回：{result.get('birthday_hidden_or_missing_count')}",
        f"查询失败：{result.get('birthday_query_failed_count')}",
    ]
    if include_users:
        users = result.get("users") or []
        default_count = sum(1 for u in users if is_possible_default_birthday(str(u.get("birthday_md") or "")))
        lines.append("")
        if default_count:
            lines.append(f"提示：名单中有 {default_count} 个 01-01，可能是B站默认/随手填生日，已标注低可信。");
        lines.append("可见生日名单：")
        if not users:
            lines.append("（无）")
        else:
            for i, u in enumerate(users, start=1):
                note = "；可能默认生日，低可信" if is_possible_default_birthday(str(u.get("birthday_md") or "")) else ""
                lines.append(
                    f"{i:02d}. {u.get('uname') or '未命名'}（UID {u.get('mid') or ''}）："
                    f"{u.get('birthday') or u.get('birthday_md') or ''}"
                    f"；下次生日 {u.get('next_birthday') or ''}，还有 {u.get('days_until')} 天"
                    f"{note}"
                )
    return "\n".join(lines)


def birthday_config(data: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(data.get("birthday_reminder") or {})
    cfg.setdefault("enabled", True)
    cfg.setdefault("schedule", {"type": "daily", "times": ["09:10"], "grace_seconds": 3600})
    cfg.setdefault("notify_days_before", 1)
    cfg.setdefault("max_followings", 2000)
    cfg.setdefault("max_followers", 3000)
    cfg.setdefault("max_birthday_check", 0)
    cfg.setdefault("gap_seconds", 0.8)
    cfg.setdefault("cache_file", "data/mutual_birthdays.json")
    cfg.setdefault("state_file", "data/mutual_birthdays_state.json")
    return cfg
