"""Bounded, local-only DM context construction; no networking or model calls.

Current input belongs in the separate incoming_text prompt field. This helper
includes only earlier conversation from the same two participants and confirmed
local replies, then keeps recent content before spending space on old summaries.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _timestamp(value: Any) -> float:
    try:
        result = float(value or 0)
        if result > 1e14:
            result /= 1e6
        elif result > 1e11:
            result /= 1000
        return result if 0 < result < 1e11 else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _plain(value: Any) -> str:
    return " ".join(str(value or "").split())


def clip_text(text: Any, max_chars: int) -> str:
    """Retain both opening and trailing question when an individual item is long."""
    text = _plain(text)
    max_chars = max(0, int(max_chars))
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return "…"[:max_chars]
    head = (max_chars - 1) * 2 // 3
    return text[:head] + "…" + text[-(max_chars - head - 1):]


def message_payload(msg: Mapping[str, Any]) -> Mapping[str, Any]:
    content = msg.get("content")
    if isinstance(content, Mapping):
        return content
    if isinstance(content, str):
        try:
            data = json.loads(content)
            if isinstance(data, Mapping):
                return data
        except (ValueError, TypeError):
            return {"content": content}
    return {}


def message_text(msg: Mapping[str, Any]) -> str:
    data = message_payload(msg)
    return _plain(data.get("content") or data.get("text") or data.get("title") or "")


def message_has_image(msg: Mapping[str, Any]) -> bool:
    data = message_payload(msg)
    if _integer(msg.get("msg_type")) == 2:
        return True
    return any(data.get(k) for k in ("url", "img_url", "image_url", "pictures", "images", "urls"))


@dataclass(frozen=True)
class ConversationTurn:
    speaker: str
    text: str
    sequence: int
    timestamp: float
    key: str
    image: bool = False
    local_reply: bool = False


def _same_message(msg: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    if str(msg.get("sender_uid") or "") != str(current.get("sender_uid") or ""):
        return False
    key = str(msg.get("msg_key") or "")
    current_key = str(current.get("msg_key") or "")
    if key and current_key:
        return key == current_key
    seq = _integer(msg.get("msg_seqno"))
    cur_seq = _integer(current.get("msg_seqno"))
    if seq > 0 and cur_seq > 0:
        return seq == cur_seq
    return (_timestamp(msg.get("timestamp")) == _timestamp(current.get("timestamp"))
            and message_text(msg) == message_text(current)
            and message_has_image(msg) == message_has_image(current))


def ordered_prior_turns(
    rows: Iterable[Mapping[str, Any]],
    current_msg: Mapping[str, Any],
    talker_id: str | int,
    self_uid: str | int,
) -> list[ConversationTurn]:
    """Filter participants/current/future, de-duplicate API rows, order old to new.

    When sequence numbers are absent, timestamps are required. Ambiguous rows
    sharing the current timestamp are excluded instead of inventing their order.
    """
    uid, me = str(talker_id).strip(), str(self_uid).strip()
    if not uid or not me or uid == me or str(current_msg.get("sender_uid") or "") != uid:
        return []
    cur_seq = _integer(current_msg.get("msg_seqno"))
    cur_ts = _timestamp(current_msg.get("timestamp"))
    turns: list[ConversationTurn] = []
    identities: set[tuple[Any, ...]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        sender = str(row.get("sender_uid") or "")
        if sender not in {uid, me} or _integer(row.get("msg_type")) not in {1, 2}:
            continue
        if _same_message(row, current_msg):
            continue
        seq = _integer(row.get("msg_seqno"))
        ts = _timestamp(row.get("timestamp"))
        if cur_seq > 0 and seq > 0:
            if seq >= cur_seq:
                continue
        elif cur_ts > 0 and ts > 0:
            if ts >= cur_ts:
                continue
        else:
            # Neither comparable order nor safe temporal boundary available.
            continue
        text, image = message_text(row), message_has_image(row)
        if not text and image:
            text = "[图片；内容未保存]"
        if not text:
            continue
        key = str(row.get("msg_key") or "")
        identity = ((sender, "key", key) if key else
                    (sender, "seq", seq) if seq > 0 else
                    (sender, "time", ts, text, image))
        if identity in identities:
            continue
        identities.add(identity)
        turns.append(ConversationTurn("恋恋" if sender == me else "对方", text, seq, ts, key, image))
    if turns and all(t.sequence > 0 for t in turns):
        turns.sort(key=lambda t: t.sequence)
    else:
        # Seq-less records have already needed reliable timestamps to survive.
        turns.sort(key=lambda t: (t.timestamp, t.sequence))
    return turns


_CONFIRMED_ACTIONS = {"dm_auto_reply", "owner_command", "dm_role_recognition", "dm_role_recognition_start"}
_INVALID_DELIVERY = {"failed", "unknown", "blocked", "sending", "dry_run", "dry-run"}


def with_confirmed_local_replies(
    turns: list[ConversationTurn],
    history: Iterable[Mapping[str, Any]],
    talker_id: str | int,
) -> list[ConversationTurn]:
    """Recover same-loop sent replies that the API snapshot cannot yet contain.

    Existing history lacks sequence numbers. Require an unambiguous *earlier*
    user-message anchor, or a future explicit incoming_key. A free-floating
    historical reply is never appended merely because its UID matches.
    """
    uid = str(talker_id).strip()
    appended: dict[int, list[ConversationTurn]] = {}
    known_bot_text = {_plain(t.text) for t in turns if t.speaker == "恋恋"}
    for item in history:
        if not isinstance(item, Mapping) or str(item.get("uid") or "").strip() != uid:
            continue
        if str(item.get("action") or "") not in _CONFIRMED_ACTIONS or item.get("dry_run"):
            continue
        status = str(item.get("delivery_status") or item.get("status") or "").lower()
        if status in _INVALID_DELIVERY or status.startswith("sending:"):
            continue
        reply = _plain(item.get("reply"))
        if not reply or reply in known_bot_text:
            continue
        incoming = _plain(item.get("incoming") or item.get("command"))
        anchor_key = str(item.get("incoming_key") or "")
        anchor_sequence = _integer(item.get("incoming_seq"))
        candidates: list[int] = []
        for i, turn in enumerate(turns):
            if turn.speaker != "对方":
                continue
            if anchor_key:
                if turn.key and anchor_key == turn.key:
                    candidates.append(i)
            elif anchor_sequence > 0:
                if turn.sequence == anchor_sequence:
                    candidates.append(i)
            elif incoming and not turn.image and turn.text[:200] == incoming:
                candidates.append(i)
        if len(candidates) != 1:
            continue
        i = candidates[0]
        anchor = turns[i]
        # Avoid silently inserting a stale response from a much older occurrence.
        date = str(item.get("time") or "")
        if date and anchor.timestamp:
            try:
                reply_ts = datetime.fromisoformat(date).timestamp()
                if not anchor.timestamp <= reply_ts <= anchor.timestamp + 7 * 86400:
                    continue
            except (ValueError, OverflowError):
                continue
        appended.setdefault(i, []).append(ConversationTurn("恋恋", reply, anchor.sequence,
                                                          anchor.timestamp, "", local_reply=True))
        known_bot_text.add(reply)
    result: list[ConversationTurn] = []
    for i, turn in enumerate(turns):
        result.append(turn)
        result.extend(appended.get(i, []))
    return result


def recent_user_text(
    rows: Iterable[Mapping[str, Any]], current_msg: Mapping[str, Any],
    talker_id: str | int, self_uid: str | int, *, max_chars: int = 400,
    max_age_seconds: int = 900,
) -> str:
    """Prior user intent for a current image; never current/future/other user's text.

    This is context, not an instruction to launch character recognition. A new
    explicit topic switch ends recall of earlier user requests.
    """
    turns = ordered_prior_turns(rows, current_msg, talker_id, self_uid)
    current_ts = _timestamp(current_msg.get("timestamp"))
    selected: list[str] = []
    for turn in reversed(turns):
        if turn.speaker != "对方":
            continue
        if current_ts and turn.timestamp and current_ts - turn.timestamp > max_age_seconds:
            break
        if turn.image:
            break
        if re.search(r"换个话题|换一个话题|先不聊|不说这个|另外(?:一件事)?|说点别的", turn.text):
            selected.append(turn.text)
            break
        selected.append(turn.text)
        if len(selected) >= 3:
            break
    return clip_text("；".join(reversed(selected)), max_chars)


def build_private_context(
    rows: Iterable[Mapping[str, Any]], current_msg: Mapping[str, Any],
    talker_id: str | int, self_uid: str | int, *,
    history: Iterable[Mapping[str, Any]] = (),
    limit: int = 6, summary_limit: int = 9, max_chars: int = 2600,
    image_context: str = "", requirements: str = "",
    user_memory: str = "", image_note: str = "",
) -> str:
    """Build complete DM context within 2600 characters, prioritizing fresh data.

    UID, current image evidence and reply instructions have reserved space.
    Recent conversation is selected newest-first then displayed chronologically;
    older summary receives only the remaining space. Current input isn't copied.
    """
    max_chars = max(0, min(2600, int(max_chars)))
    if max_chars == 0:
        return ""
    turns = with_confirmed_local_replies(
        ordered_prior_turns(rows, current_msg, talker_id, self_uid), history, talker_id)
    raw_limit = max(1, min(12, _integer(limit) or 6))
    summary_limit = max(0, min(18, _integer(summary_limit)))
    header = f"对方UID：{str(talker_id).strip()}\n场景：B站私信聊天\n"
    image = ("【当前私信图片】\n" + clip_text(image_context, 800) + "\n") if image_context else ""
    rule = ("回复要求：" + clip_text(requirements, 440) + "\n") if requirements else ""
    memory = ("【仅此用户的记忆；有冲突以当前说法为准】\n" + clip_text(user_memory, 400) + "\n") if user_memory else ""
    # Caller supplies this only for a relevant subsequent question, never every chat.
    note = ("【此用户先前图片；不能当作当前新图】\n" + clip_text(image_note, 300) + "\n") if image_note else ""
    fixed = header + image + rule + memory + note
    if len(fixed) >= max_chars:
        # Narrow custom budget: retain its beginning and last instructions.
        return clip_text(fixed, max_chars)
    available = max_chars - len(fixed)
    recent_heading = "最近私信（旧→新；本次输入另列；只含已确认的恋恋回复）：\n"
    raw = turns[-raw_limit:]
    lines: list[str] = []
    used = len(recent_heading)
    for turn in reversed(raw):
        room = available - used - 1
        if room < 16:
            break
        line = f"{turn.speaker}：" + clip_text(turn.text, min(220, room - len(turn.speaker) - 1))
        lines.append(line)
        used += len(line) + 1
    recent = recent_heading + "\n".join(reversed(lines)) + "\n" if lines else ""
    remaining = available - len(recent)
    older = turns[-(raw_limit + summary_limit):-raw_limit] if summary_limit else []
    summary_heading = "更早摘要（旧→新；仅补充前情）：\n"
    compact: list[str] = []
    used = len(summary_heading)
    for turn in reversed(older):
        # Only strip known emoji tags; do not erase [用户称呼] or [图片].
        text = re.sub(r"\[(?:夜雀食堂_|tv_|doge|OK|妙啊)[^\[\]\n]{0,36}\]", "", turn.text)
        line = f"{turn.speaker}：" + clip_text(text, 70)
        if used + len(line) + 1 > remaining:
            break
        compact.append(line)
        used += len(line) + 1
    summary = summary_heading + "\n".join(reversed(compact)) + "\n" if compact else ""
    # Position fresh information first so even another caller's prefix cap is safer.
    result = header + image + rule + memory + note + recent + summary
    assert len(result) <= max_chars
    return result


def bound_final_dm_context(context: str, *, max_chars: int = 2600) -> str:
    """Final-stage defensive bound for legacy or unexpectedly long DM contexts.

    Normal build_private_context output passes through byte-for-byte. If older
    context builders add too much text, discard older summaries first, retain
    UID and the latest recent-dialogue lines, and reserve room for trailing image
    descriptions/instructions. This avoids the former unconditional prefix cut.
    """
    context = str(context or "")
    maximum = max(0, min(2600, int(max_chars)))
    if len(context) <= maximum:
        return context
    if not maximum:
        return ""
    headings = list(re.finditer(r"(?m)^(最近私信(?:原文)?[^\n]*：)\n", context))
    if not headings:
        # Opaque fallback: keep identifying front matter plus the latest suffix.
        front = min(220, maximum // 4)
        marker = "\n[更早上下文已压缩]\n"
        if maximum <= len(marker) + front:
            return clip_text(context, maximum)
        return context[:front] + marker + context[-(maximum - front - len(marker)):]
    recent_match = headings[-1]
    before, after = context[:recent_match.start()], context[recent_match.end():]
    older = re.search(r"(?m)^(?:更早私信摘要|更早摘要)[^\n]*：\n", before)
    if older:
        before = before[:older.start()]
    boundary = re.search(r"(?m)^(?:更早私信摘要|更早摘要|【|要求：|回复要求：)", after)
    dialogue = after[:boundary.start()] if boundary else after
    tail = after[boundary.start():] if boundary else ""
    # An old summary is the cheapest section to drop. Preserve following evidence.
    if tail.startswith(("更早私信摘要", "更早摘要")):
        evidence = re.search(r"(?m)^(?:【|要求：|回复要求：)", tail)
        tail = tail[evidence.start():] if evidence else ""
    lead = before
    if len(lead) > maximum * 3 // 5:
        lead = clip_text(lead, maximum * 3 // 5) + "\n"
    if len(tail) > maximum // 4:
        tail = clip_text(tail, maximum // 4) + "\n"
    heading = recent_match.group(1) + "\n"
    room = maximum - len(lead) - len(tail) - len(heading)
    if room <= 0:
        return clip_text(lead + heading + tail, maximum)
    chosen: list[str] = []
    used = 0
    for line in reversed([line for line in dialogue.splitlines() if line.strip()]):
        available = room - used - 1
        if available <= 0:
            break
        if len(line) > available:
            if chosen:
                break
            line = clip_text(line, available)
        chosen.append(line)
        used += len(line) + 1
    return lead + heading + "\n".join(reversed(chosen)) + ("\n" if chosen else "") + tail
