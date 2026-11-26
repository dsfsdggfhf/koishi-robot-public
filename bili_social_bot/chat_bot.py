from __future__ import annotations



import asyncio

import json

import random

import re

import time

import traceback

from dataclasses import dataclass

from datetime import datetime, timedelta

from typing import Any



from bilibili_api import comment, session as bili_session

from bilibili_api.comment import CommentResourceType



from .ai import AIClient, clean_reply_text

from .constants import *

from .bili_client import BiliClient, OutboundDeliveryUnknown, VideoItem

from .command import help_text, is_command, parse_command, unknown_tip

from .config import BotConfig, is_private_message_blocked

from .character_samples import remember_correction_sample_images, match_character_samples, format_sample_match_hint

from .correction_learning import detect_character_correction, learn_correction

from .emotes import EmoteSelector

from .media import MediaAnalyzer

from .metrics import emit_round_metrics

from .morph import apply_morph, current_morph

from .persona import build_persona_prompt, load_knowledge_text

from .quality_style import DAILY_INTERACTION_STYLE

from .conversation_understanding import (comment_incoming_text, build_comment_source_context, comment_requires_direct_reply, build_analysis_reply_context)

from .private_reply_style import apply_private_reply_style

from .dm_context_helper import build_private_context, bound_final_dm_context

from .image_intent_helper import (infer_dm_image_intent, immediate_previous_user_text,

                                immediate_previous_user_image, asks_about_previous_image,

                                build_image_description_prompt)

from .dm_session_memory import DMSessionMemory

from .visual_task_helper import (infer_visual_task, build_visual_user_prompt,

                                 build_visual_reply_hint, visual_task_cache_key)

from .conversation_understanding import build_understanding_guidance

from .safety import looks_like_prompt_injection, validate_comment, validate_message
from .interaction_guard import ROLE_BOUNDARY, is_rule_override, public_facts_reply, reject_generated_reply

from .state import BotState, interactive_status_done

from .comment_reply_support import resolve_comment_position, validate_conversation_reply

from .stickers import LocalStickerPicker

from .touhou_learning import TouhouLearningDB

from .touhou import TouhouCompliance

from .utils import append_log, extract_bvid, is_bereavement_text, now_iso, resolve_path, load_json, save_json_atomic





@dataclass

class ChatReplyDraft:

    target_type: str

    target_id: str

    incoming_text: str

    reply_text: str

    raw: dict[str, Any]





def _format_exception_detail(exc: BaseException) -> str:

    """异常字符串为空时也记录类型和最后一段 traceback，方便追根。"""

    msg = str(exc).strip()

    detail = f"{type(exc).__name__}: {msg or repr(exc)}"

    try:

        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

        tail = " | ".join(line.strip() for line in tb.splitlines()[-6:] if line.strip())

        if tail:

            detail += f" | tb={tail[:900]}"

    except Exception:

        pass

    return detail[:1200]





class InteractiveChatBot:

    """Auto-reply to private messages and comment-area conversations."""



    def __init__(self, config: BotConfig):

        self.config = config

        # 互动聊天用独立 state 文件，避免与主进程（run）共用 data/state.json 互相覆盖。

        from pathlib import Path as _Path

        chat_state = str(_Path(config.state_file).with_name("chat_state.json"))

        self.state = BotState(chat_state)

        # 仅在内存中补齐尚未同步回来的已发回复与短期图片证据，不落盘原始私信。

        self.dm_session_memory = DMSessionMemory()

        # 迁移主 state 里的互动去重记录（interactive_replied / 取关缓存），避免丢失已回复记录导致重复回复。

        try:

            import json as _json

            main_state = _json.loads(_Path(config.state_file).read_text(encoding="utf-8"))

            for k in ("interactive_replied", "unfollowed_cache", "fan_thanks_sent", "fan_thanks_unknown"):

                source = main_state.get(k)

                if not isinstance(source, dict):

                    # 兼容非常早的 fans_thanks_sent 列表格式；只导入为已处理，

                    # 不把它当作可重发任务。

                    if k == "fan_thanks_sent" and isinstance(source, list):

                        source = {str(uid): "legacy" for uid in source if str(uid).strip()}

                    else:

                        continue

                target = self.state.data.setdefault(k, {})

                for item_key, value in source.items():

                    old = target.get(str(item_key))

                    if old is None or str(value or "") in {"sent", "unknown", "legacy"}:

                        target[str(item_key)] = value

            self.state.save()

        except Exception:

            pass

        self.client = BiliClient(config.cookie_file, min_gap_seconds=min(int(config.global_min_action_gap_seconds or 0), 8))

        self.owner_uid = str(config.data.get("owner_uid") or "").strip()

        self.owner_name = str(config.data.get("owner_name") or "管理员")

        self.touhou = TouhouCompliance(config.data.get("touhou_compliance") or {})

        self.ai = AIClient(config.data.get("ai") or {})

        self.ai.switch_callback = self._notify_model_switch

        self.persona_prompt = build_persona_prompt(config.data.get("persona") or {})

        self.knowledge_text = load_knowledge_text(config.data.get("touhou_knowledge") or {})

        learn_cfg = config.data.get("touhou_learning") or {}

        if learn_cfg.get("enabled", True):

            self.learn_db = TouhouLearningDB(

                path=str(learn_cfg.get("memory_file") or "data/touhou_learning.json"),

                max_entries=int(learn_cfg.get("max_entries", 2000) or 2000),

            )

            learned = self.learn_db.context_for("东方Project 古明地恋 B站聊天 私信 评论 回复", limit=int(learn_cfg.get("context_limit", 8) or 8))

            if learned:

                self.knowledge_text = (self.knowledge_text + "\n\n【长期学习到的东方/B站梗知识】\n" + learned).strip()

            if learn_cfg.get("learn_comment_styles", True):

                styles = self.learn_db.context_for("评论风格 社区学习", limit=4, max_chars=2000)

                if styles:

                    self.knowledge_text = (self.knowledge_text + "\n\n【学到的评论区风格（用于写自然回复）】\n" + styles).strip()

        else:

            self.learn_db = TouhouLearningDB(path="data/touhou_learning.json")

        # 记忆增强引擎：用户记忆（记住聊过的人/事）+ 加权召回

        from .memory_engine import MemoryEngine

        self.memory_engine = MemoryEngine(

            self.learn_db,

            user_memory_path=str(learn_cfg.get("user_memory_file") or "data/user_memory.json"),

        )

        self.emotes = EmoteSelector(config.data.get("emotes") or {})

        self.stickers = LocalStickerPicker(config.data.get("stickers") or {}, owner_uid=self.owner_uid)

        self.chat_cfg = config.data.get("interactive_chat") or {}

        self._dm_poll_lock = asyncio.Lock()

        self.morph_config = dict(config.data.get("morph") or {})

        self.morph_config.setdefault("timezone", config.data.get("timezone") or "Asia/Shanghai")

        # fumo 形态按星期切换，必须每次调用动态计算（不能缓存启动时的值）。

        self.morph = ""  # 占位，_apply_morph 里动态计算

        self.media = MediaAnalyzer(

            config.data.get("media_understanding") or {},

            self.ai,

            logger=self.log,

            persona_prompt=self.persona_prompt,

            knowledge_text=self.knowledge_text,

        )

        # 离线补回复：记录上次活跃时间，重启后检测离线期间的 @/回复/私信并补回复。

        catchup_cfg = self.chat_cfg.get("catchup") or {}

        self.catchup_enabled = bool(catchup_cfg.get("enabled", True))

        self.catchup_threshold_seconds = max(60, int(catchup_cfg.get("threshold_seconds", 300) or 300))

        self.catchup_offline_seconds = self._detect_offline_gap()



    def _apply_morph(self, text: str) -> str:

        # 每次调用按当前日期动态判断形态（fumo 按星期切换，不能缓存启动时的值）。

        morph = current_morph(self.morph_config)

        return apply_morph(text, morph, self.morph_config)



    def _strip_blocked_text_emotes(self, text: str) -> str:

        """移除未验证会在B站显示成纯文字的表情标签（包括模型从上下文里学到的标签）。"""

        text = str(text or "")

        em_cfg = self.config.data.get("emotes") or {}

        prefixes = [str(x) for x in em_cfg.get("blocked_text_emote_prefixes", ["[东方收藏集·浮生若梦_"]) if str(x)]

        if bool(em_cfg.get("allow_unverified_text_emotes", False)) or not prefixes:

            return text

        for pfx in prefixes:

            text = re.sub(re.escape(pfx) + r"[^\]]*\]", "", text)

        return re.sub(r"[ \t]{2,}", " ", text).strip()



    async def _send_private_with_optional_sticker(self, uid: str | int, text: str, *, timeout_seconds: float | None = None) -> None:

        """先发文字，再按概率附加本地表情包；图片失败只记 WARN，不影响文字回复。"""

        if self._uid_blocked(uid):

            self.log(f"私信拒收名单拦截文字/表情发送 uid={uid}", "INFO")

            return

        await self._send_private_text_guarded(uid, text, timeout_seconds=timeout_seconds)

        pic = self.stickers.choose("dm", target_uid=str(uid))

        if not pic:

            return

        asyncio.create_task(self._send_private_sticker_attachment(uid, pic))



    async def _send_private_sticker_attachment(self, uid: str | int, pic: Any) -> None:

        """文字私信已发出后异步补发本地表情包，不阻塞后续私信文本回复。"""

        if self._uid_blocked(uid):

            self.log(f"私信拒收名单拦截异步表情发送 uid={uid}", "INFO")

            return

        try:

            await self.client.send_private_picture(uid, pic)

            # 图片也是"发给这个用户的一条私信"，必须计入按用户日上限，

            # 否则纯图片回复会绕过限制（2026-09-14 主人反馈"为什么还在发"就是这个漏洞）。

            self.state.add_dm_sent(uid)

            self.log(f"已附加私信表情包 uid={uid} file={pic.name}", "SUCCESS")

        except Exception as exc:

            # 文字已经成功发出，附加图片表情包即使结果未知也不能把整条私信标 failed/unknown，

            # 否则会导致后续重复回复同一条文本。这里只降级记录。

            self.log(f"私信表情包发送失败 uid={uid} file={getattr(pic, 'name', pic)}：{type(exc).__name__}: {str(exc)[:120]}", "WARN")



    async def _send_private_sticker_only(self, uid: str | int, *, probability: float = 1.0,

                                         fallback_text: str = "",

                                         allow_excluded: bool = False) -> tuple[bool, str]:

        """对纯图片私信只回一张本地表情包；无可用表情时可退回短文字，不调用模型。



        allow_excluded=True 时才允许给 exclude_uids（默认含主人）发表情包：

        「对方发图 → 我回一张表情」这条路本来就是要回图的，主人自己测试时也算在内，

        否则主人发图永远只能收到兜底文字（2026-08-31 实测就是"收到图啦～"）。

        """

        if self._uid_blocked(uid):

            self.log(f"私信拒收名单拦截纯图片回复 uid={uid}", "INFO")

            return False, "blocked"

        pic = self.stickers.choose("dm", target_uid=str(uid), probability_override=probability,

                                   allow_excluded=allow_excluded)

        fallback_text = str(fallback_text or "").strip()

        if pic:

            try:

                await self.client.send_private_picture(
                    uid, pic,
                    on_dispatch=lambda: setattr(self, "_dm_delivery_started", True),
                )

                self.state.add_dm_sent(uid)      # 图片也计入按用户日上限

                self.log(f"已用表情包回复纯图片私信 uid={uid} file={pic.name}", "SUCCESS")

                return True, str(pic.name)

            except OutboundDeliveryUnknown:
                # The picture may already be delivered.  A text fallback or
                # retry would be a second reply to the same incoming message.
                raise

            except Exception as exc:

                self.log(f"纯图片私信表情包发送失败 uid={uid} file={pic.name}：{type(exc).__name__}: {str(exc)[:120]}", "WARN")

                if not fallback_text:

                    return False, ""

        if fallback_text:

            await self._send_private_text_guarded(uid, fallback_text)

            self.log(f"纯图片私信已发送兜底文字 uid={uid}，长度={len(fallback_text)}", "WARN")

            return False, fallback_text

        self.log(f"纯图片私信未回复：没有可用本地表情包 uid={uid}", "WARN")

        return False, ""



    def _private_text_max_bytes(self) -> int:

        try:

            pm_cfg = self.chat_cfg.get("private_messages") or {}

            value = int(pm_cfg.get("reply_max_bytes") or pm_cfg.get("send_max_bytes") or 900)

        except Exception:

            value = 900

        return max(300, min(1800, value))



    def _hard_limit_private_text_bytes(self, text: str) -> str:

        """B站私信会按负载大小拦截（21007 消息过长），发送前用 UTF-8 字节再兜底裁剪。"""

        text = str(text or "").strip()

        limit = self._private_text_max_bytes()

        if len(text.encode("utf-8")) <= limit:

            return text

        suffix = "…"

        suffix_b = len(suffix.encode("utf-8"))

        out: list[str] = []

        used = 0

        for ch in text:

            b = len(ch.encode("utf-8"))

            if used + b + suffix_b > limit:

                break

            out.append(ch)

            used += b

        cut = "".join(out).rstrip()

        last_open = cut.rfind("[")

        last_close = cut.rfind("]")

        if last_open > last_close and len(cut) - last_open <= 50:

            cut = cut[:last_open].rstrip()

        if not cut:

            cut = text[:80].rstrip()

        return cut + suffix



    async def _send_private_text_guarded(self, uid: str | int, text: str, *, timeout_seconds: float | None = None) -> Any:

        """带超时的私信发送；超时按未知投递处理，避免单条卡住超过主人要求的 90 秒。"""

        if self._uid_blocked(uid):

            self.log(f"私信拒收名单拦截文字发送 uid={uid}", "INFO")

            return None

        text = apply_private_reply_style(uid, str(text or ""), max_bytes=self._private_text_max_bytes())

        text = self._hard_limit_private_text_bytes(text)

        self.client.min_gap_seconds = min(int(getattr(self.client, "min_gap_seconds", 0) or 0), 8)

        if timeout_seconds is None:

            try:

                timeout_seconds = float(((self.chat_cfg.get("private_messages") or {}).get("send_timeout_seconds")) or 75)

            except Exception:

                timeout_seconds = 75.0

        timeout_seconds = max(10.0, min(280.0, float(timeout_seconds or 240.0)))

        try:

            self._dm_delivery_started = True
            result = await asyncio.wait_for(self.client.send_private_text(uid, text), timeout=timeout_seconds)

        except asyncio.CancelledError as exc:
            raise OutboundDeliveryUnknown("私信发送被会话超时取消，投递结果未知，禁止自动重发") from exc
        except asyncio.TimeoutError as exc:

            raise OutboundDeliveryUnknown(f"私信发送超过 {int(timeout_seconds)} 秒无确认，投递结果未知") from exc

        # 计数：只统计"确认发出"的私信，用于按用户日上限（见 _dm_daily_limit）。

        self.state.add_dm_sent(uid)

        return result



    async def _notify_owner_dm_failure(self, uid: str | int, key: str, error: Any, *, status: str = "failed") -> None:

        """私信未能确认送达时通知主人。



        按 UID+状态 限频（默认 60 分钟一次），同一用户反复失败不会刷屏；

        窗口时长可在 private_messages.owner_error_notify_minutes 配置。

        """

        if not self.owner_uid or str(uid) == str(self.owner_uid):

            return

        # 保险：异步/重启后可能先前失败消息已经被后续补发成功。

        # 一旦对应 dm key 当前已是 sent，就不要再给主人发旧 unknown/failed 告警。

        try:

            k = str(key or "")

            full_key = ""

            if k.startswith("dm:"):

                full_key = k

            elif re.fullmatch(r"\d+:\d+", k):

                full_key = f"dm:{k}"

            elif k and ":" not in k and str(k).isdigit():

                full_key = f"dm:{uid}:{k}"

            if full_key and self.state.data.setdefault("interactive_replied", {}).get(full_key) == "sent":

                return

        except Exception:

            pass

        pm_cfg = (self.chat_cfg.get("private_messages") or {})

        notify_cfg = pm_cfg.get("owner_error_notify")

        if notify_cfg is None:

            notify_cfg = True

        if not bool(notify_cfg):

            return

        try:

            window_seconds = int(pm_cfg.get("owner_error_notify_minutes") or 60) * 60

        except Exception:

            window_seconds = 3600

        window_seconds = max(300, min(86400, window_seconds))

        name = self.owner_name or "管理员"

        short_key = str(key or "")[-36:]

        gate = f"owner_dm_fail_notify:{uid}:{status}"

        import time as _time

        now = _time.time()

        last = self.state.data.setdefault("owner_notify_ts", {}).get(gate)

        try:

            if last and now - float(last) < window_seconds:

                return

        except Exception:

            pass

        self.state.data["owner_notify_ts"][gate] = str(now)

        detail = str(error or "")[:180]

        minutes = int(window_seconds / 60)

        msg = (

            f"诶～{name}，Koishi报错：UID {uid} 的私信没能确认在5分钟内回复。"

            f"状态={status}，消息key={short_key}，错误={detail}。"

            f"同一用户的同类问题我 {minutes} 分钟内最多提醒一次；"

            "投递结果未知的可能其实已经发出，我会先把这条消息标为已处理，不再自动重试，避免重复私信。"

            "如果你想让我停止回复这个用户，跟我说「拉黑 <UID>」即可。"

        )

        try:

            ok, reason, safe = validate_message(msg, self.config.forbidden_words, marker="", require_marker=False)

            if ok and not self.config.dry_run:

                await self.client.send_private_text(self.owner_uid, safe)

                self.log(f"已通知主人私信回复异常 uid={uid} status={status}", "WARN")

        except Exception as notify_exc:

            self.log(f"通知主人私信回复异常失败 uid={uid}：{notify_exc}", "WARN")



    def _dm_fail_cooldown_seconds(self) -> int:

        base = 300

        try:

            base = int((self.chat_cfg.get("private_messages") or {}).get("dm_fail_cooldown_seconds") or 300)

        except Exception:

            base = 300

        return max(60, min(3600, base))



    def _dm_uid_in_cooldown(self, uid: str | int) -> bool:

        """该 UID 是否处于发送失败冷却期（只影响旧消息的重试，不影响新消息首次回复）。"""

        uid = str(uid)

        try:

            until = float(self.state.data.setdefault("dm_fail_cooldown", {}).get(uid) or 0)

        except Exception:

            return False

        return until > time.time()



    def _mark_dm_failure_cooldown(self, uid: str | int) -> None:

        """发送失败后给该 UID 冷却：连续失败指数退避，最多 1 小时。"""

        uid = str(uid)

        base = self._dm_fail_cooldown_seconds()

        strikes = int(self.state.data.setdefault("dm_fail_strikes", {}).get(uid) or 0) + 1

        self.state.data["dm_fail_strikes"][uid] = strikes

        cooldown = min(3600, base * (2 ** (strikes - 1)))

        self.state.data.setdefault("dm_fail_cooldown", {})[uid] = str(time.time() + cooldown)

        self.log(f"私信发送失败，UID {uid} 进入冷却 {int(cooldown)} 秒（连续第{strikes}次失败）", "WARN")

        self.state.save()



    def _clear_dm_failure_cooldown(self, uid: str | int) -> None:

        """发送成功后清除该 UID 的失败冷却。"""

        uid = str(uid)

        self.state.data.setdefault("dm_fail_cooldown", {}).pop(uid, None)

        self.state.data.setdefault("dm_fail_strikes", {}).pop(uid, None)



    def _mark_interactive(self, key: str, status: str, *, save: bool = True) -> None:

        self.state.data.setdefault("interactive_replied", {})[key] = status

        if save:

            self.state.save()



    def _uid_blocked(self, uid: Any) -> bool:

        """按磁盘上的最新配置判断 UID 是否在私信拒收名单。"""

        return is_private_message_blocked(self.config, uid)



    def _dm_daily_limit(self, uid: Any) -> int:

        """某用户每天最多收到多少条私信；0 = 不额外限制。



        配置：interactive_chat.private_messages

                · user_daily_limits: {"<uid>": 20, ...}  按人指定（主人用来限制刷屏用户）

                · default_user_daily_limit: 0           所有人默认（0=不限）

        """

        uid = str(uid or "").strip()

        if not uid:

            return 0

        try:

            pm = self.chat_cfg.get("private_messages") or {}

            table = pm.get("user_daily_limits") or {}

            if isinstance(table, dict) and uid in {str(k).strip(): v for k, v in table.items()}:

                for k, v in table.items():

                    if str(k).strip() == uid:

                        return max(0, int(v or 0))

            return max(0, int(pm.get("default_user_daily_limit") or 0))

        except Exception:

            return 0



    def _is_interactive_done(self, key: str) -> bool:

        """sent/blocked/ignored/old/invalid 等终态跳过；sending 只有过期才跳过。"""

        status = self.state.data.setdefault("interactive_replied", {}).get(key)

        if status == "quality_exhausted":

            return True

        if isinstance(status, str) and status.startswith("quality_retry:"):

            try:

                return datetime.now() < datetime.fromisoformat(status.split(":", 1)[1])

            except ValueError:

                return True

        return interactive_status_done(status)



    @staticmethod

    def _dm_status_retryable_for_guarantee(status: Any) -> bool:

        """主人要求私信尽量每条都回，但 unknown 表示投递结果不明，禁止自动重试以免重复私信。"""

        if not status or status in {"sent", "blocked", "unknown"}:

            return False

        if status in {"ignored", "old", "invalid", "failed"}:

            return True

        if isinstance(status, str) and status.startswith("sending:"):

            try:

                dt = datetime.fromisoformat(status.split(":", 1)[1])

                return datetime.now() - dt > timedelta(seconds=300)

            except Exception:

                return True

        return False



    def _mark_reply_failed(self, key: str, canon: str | None = None) -> None:

        """回复失败：key 与 canon 都标 failed，保证后续轮可以重试（而不是卡在 sending）。"""

        self.state.data.setdefault("interactive_replied", {})[key] = "failed"

        if canon:

            self.state.data.setdefault("interactive_replied", {})[canon] = "failed"

        self.state.save()



    @staticmethod

    def _private_send_error_nonretryable(exc: Exception) -> bool:

        """私信发送的不可重试错误：继续重试只会刷失败日志或误扰同一会话。"""

        msg = str(exc or "")

        return any(k in msg for k in [

            "21047", "最多发送1条消息",  # 对方主动回复/关注前平台只允许一条

            "非法参数",                  # 系统/卡片会话、无效收件人等

        ])



    def _sanitize_image_context(self, ctx: str, tag: str) -> str:

        """图片 OCR/视觉描述进入模型前再次做提示注入检测。"""

        ctx = str(ctx or "")

        if not ctx.strip():

            return ""

        if looks_like_prompt_injection(ctx):

            self.log(f"{tag}含疑似提示词注入文字，已忽略图片文字上下文", "WARN")

            return f"【{tag}】对方发了图片；图片里含疑似指令/提示词文字，已忽略这些文字，只按普通图片处理。\n"

        return ctx



    def _choose_comment_sticker(self, uid: str = ""):

        return self.stickers.choose("comment", target_uid=str(uid or ""))



    @staticmethod

    def _hard_limit_text(text: str, max_len: int) -> str:

        """发送前最终硬截断：装饰/fumo/合规处理之后仍不得超过通道配置上限。"""

        text = str(text or "").strip()

        max_len = max(1, int(max_len or REPLY_DEFAULT_MAX_LEN))

        if len(text) <= max_len:

            return text

        suffix = "…"

        cut = text[: max(1, max_len - len(suffix))].rstrip()

        # 避免把 B站表情标签截成半个「[夜雀食堂_...」导致显示异常。

        last_open = cut.rfind("[")

        last_close = cut.rfind("]")

        if last_open > last_close and len(cut) - last_open <= 50:

            cut = cut[:last_open].rstrip()

        if not cut:

            cut = text[: max(1, max_len - len(suffix))].rstrip()

        return (cut + suffix)[:max_len]



    def _mark_comment_draft_rejected(self, key, canon, reason):

        retryable = reason in {"评论为空", "缺少具体内容细节", "正文信息量过低", "回复包含分析式文本"}

        if retryable:

            item = self.state.data.setdefault("comment_quality_retries", {}).setdefault(key, {"attempts": 0})

            item["attempts"] = int(item.get("attempts", 0)) + 1

            item["reason"] = reason

            status = "quality_retry:exhausted" if item["attempts"] >= 3 else "quality_retry:" + (datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds")

        else:

            status = "blocked"

        self.state.data["interactive_replied"][key] = status

        if canon and retryable:

            self.state.data["interactive_replied"][canon] = status

        self.state.save()



    def _finalize_reply(self, reply: str, *, channel: str, fake_item: VideoItem,

                        analysis: Any = None, max_len: int, is_comment: bool,

                        preserve_quotes: bool = False) -> tuple[bool, str, str]:

        """统一回复装饰链：歌词开头 → 表情 → morph → 安全校验。



        收敛 4 个 handler（私信/评论回复/@/动态）里重复的

        maybe_lyric_opener → decorate → morph → validate 五连击。

        返回 (ok, reason, safe)：ok=False 表示被安全规则拦截。

        """

        # 读字与报错答复保留原文；所有回复仍执行下方知识、安全及长度检查。

        if not preserve_quotes:

            reply = self._maybe_lyric_opener(reply, channel)

            if is_comment:

                reply = self.emotes.decorate_comment(reply, fake_item, analysis, max_len=max_len)

            else:

                reply = self.emotes.decorate_message(reply, fake_item, analysis, max_len=max_len)

            reply = self._apply_morph(reply)  # 校验针对最终文本

        touhou_check = self.touhou.review_outbound_text(reply, fake_item)

        if not touhou_check.ok:

            return False, touhou_check.reason, reply

        reviewed = touhou_check.text

        reply = self._strip_blocked_text_emotes(reviewed)

        # 标签禁用仍有效；只有差异仅为空白时保留读字的间距。

        if preserve_quotes and re.sub(r"\s", "", reply) == re.sub(r"\s", "", reviewed):

            reply = reviewed

        if not is_comment:

            reply = self._hard_limit_text(reply, max_len)

        if is_comment:

            ok, reason, safe = validate_conversation_reply(reply, self.config.forbidden_words,

                                                marker=self.config.ai_marker,

                                                require_marker=self.config.require_ai_marker,

                                                preserve_quotes=preserve_quotes)

        else:

            ok, reason, safe = validate_message(reply, self.config.forbidden_words,

                                                marker=self.config.ai_marker,

                                                require_marker=self.config.require_ai_marker)

        if preserve_quotes and ok:

            candidate = reply

            marker = str(self.config.ai_marker or "")

            if self.config.require_ai_marker and marker and marker not in candidate:

                candidate += marker

            # 只恢复空白，不撤销审核中的删词、风险替换或其他内容修正。

            if re.sub(r"\s", "", candidate) == re.sub(r"\s", "", safe):

                safe = candidate

        return ok, reason, safe



    # ------------------------------------------------------------------

    # 评论区图片理解：提取对方评论里的图片 → 下载 → 视觉分析 → 注入上下文

    # ------------------------------------------------------------------

    @staticmethod

    def _extract_comment_image_urls(item: dict[str, Any] | None) -> list[str]:

        """从通知 item 或评论 dict 中提取图片 URL 列表。



        - 通知接口（回复/@）：item.image 是单张图片 URL（字符串），个别情况是空串

        - 动态评论列表：content.pictures 是数组，每项含 img_src

        - B站图片 URL 可能是协议相对格式（//i0.hdslb.com/...），统一补成 https:

        """

        item = item or {}

        urls: list[str] = []



        def _norm(u: Any) -> str:

            u = str(u or "").strip()

            if not u:

                return ""

            low = u.lower()

            if low.startswith("//"):

                return "https:" + u

            if low.startswith("http://") or low.startswith("https://"):

                return u

            return ""



        def _from_pics(pics: Any) -> None:

            if isinstance(pics, dict):  # 单张图片对象

                u = _norm(pics.get("img_src") or pics.get("img_url") or pics.get("url") or "")

                if u:

                    urls.append(u)

            elif isinstance(pics, list):

                for x in pics:

                    if isinstance(x, str):

                        u = _norm(x)

                        if u:

                            urls.append(u)

                    elif isinstance(x, dict):

                        u = _norm(x.get("img_src") or x.get("img_url") or x.get("url") or "")

                        if u:

                            urls.append(u)



        # 1) item.image：可能是字符串 URL、列表，也可能直接是图片对象

        img = item.get("image")

        if isinstance(img, str):

            u = _norm(img)

            if u:

                urls.append(u)

        elif isinstance(img, dict):

            _from_pics(img)

        elif isinstance(img, list):

            _from_pics(img)

        # 2) content.pictures：评论对象结构

        content = item.get("content")

        if isinstance(content, dict):

            _from_pics(content.get("pictures"))

        # 3) 顶层 pictures（部分接口直接给）

        _from_pics(item.get("pictures"))

        # 去重保序

        seen: set[str] = set()

        out: list[str] = []

        for u in urls:

            if u not in seen:

                seen.add(u)

                out.append(u)

        return out



    async def _analyze_image_urls(self, urls: list[str], tag: str, max_images: int = 1,

                                  timeout: int = 60, *, request_text: str = "",

                                  image_mode: str = "character") -> str:

        """复用现有视觉通道；私信普通图不注入东方角色参考，失败显式说明未看清。"""

        urls = [u for u in (urls or []) if str(u).strip()]

        visual_task = infer_visual_task(request_text, image_mode)

        if visual_task == "skip":

            return f"【{tag}】本轮没有分析图片，不能描述未看过的细节。\n"

        private_general = visual_task != "character"

        failure = f"【{tag}】未获得可靠视觉描述；不能假装看见了图中内容或细节，请自然说明暂时没看清。\n"

        if not urls:

            return ""

        urls = urls[:max(1, int(max_images or 1))]

        timeout = max(10, int(timeout or 60))

        paths: list[str] = []

        try:

            import tempfile

            import uuid

            from pathlib import Path as _P

            tmpdir = _P(tempfile.gettempdir()) / "koishi_comment_imgs"

            tmpdir.mkdir(parents=True, exist_ok=True)

            run_id = uuid.uuid4().hex[:8]

            for i, u in enumerate(urls):

                ext = ".jpg"

                for cand in (".png", ".gif", ".webp", ".jpeg"):

                    if cand in u.lower():

                        ext = cand

                        break

                p = str(tmpdir / f"cmt_{run_id}_{i}{ext}")

                paths.append(p)  # 下载取消时也清理自己创建的文件。

                saved = await self.client.download_image_url(u, p)

                if not saved:

                    paths.remove(p)

            valid_paths = [p for p in paths if _P(p).is_file()]

            if not valid_paths:

                return failure

            self.log(f"查看图片 {len(valid_paths)} 张（{tag}）", "INFO")

            desc = await asyncio.wait_for(

                self.ai.describe_images(

                    valid_paths,

                    prompt=(build_visual_user_prompt(tag, request_text, mode=visual_task)

                            if private_general or request_text else

                            f"这是B站用户发来的图片（{tag}）。请描述图片里能看到的内容："

                            "画面主体、角色（若出现东方Project角色请按特征说出名字或‘疑似’）、"

                            "场景、表情包文字、梗或文字内容。简洁客观，不要编造看不到的东西。"

                            "角色识别必须谨慎：多个特征吻合再确认，只有发色/帽子相似时不要硬猜。"),

                    reference_text="" if private_general else self.media.character_reference_for_context(tag),

                    image_task=visual_task,

                    image_request=request_text,

                ), timeout=timeout,

            )

            if not str(desc or "").strip():

                return failure

            # OCR/报错的原文不能经过去星号、空白折叠等语气清理。

            desc = (str(desc).strip() if visual_task in {"ocr", "ui"}

                    else clean_reply_text(" ".join(str(desc).split())))

            if not desc:

                return failure

            observation = f"图片观察任务：{visual_task}。" if private_general else ""

            ctx = f"【{tag}】{observation}对方发了图片，图片内容：{desc[:520 if private_general else 400]}\n"

            sanitized = self._sanitize_image_context(ctx, tag)

            return failure if sanitized != ctx else sanitized

        except asyncio.TimeoutError:

            self.log(f"图片理解超时（{tag}），不编造未看见的内容", "WARN")

            return failure

        except Exception as exc:

            self.log(f"图片理解失败（{tag}）：{type(exc).__name__}", "WARN")

            return failure

        finally:

            for p in paths:

                try:

                    _P(p).unlink(missing_ok=True)

                    _P(p + ".part").unlink(missing_ok=True)

                except Exception:

                    pass





    async def _comment_image_context(self, item: dict[str, Any] | None, tag: str = "评论图片") -> str:

        """查看对方发在评论区的图片，返回「图片内容描述」上下文（无图/失败返回空串）。"""

        cfg = (self.chat_cfg.get("comment_images") or {})

        if not cfg.get("enabled", True):

            return ""

        urls = self._extract_comment_image_urls(item)

        if not urls:

            return ""

        request = comment_incoming_text(item)

        return await self._analyze_image_urls(

            urls,

            tag,

            max_images=int(cfg.get("max_images", 1) or 1),

            timeout=int(cfg.get("timeout_seconds", 60) or 60),

            request_text=request,

            image_mode=infer_visual_task(request, "general"),

        )





    async def _throttle_session_api(self, kind: str = "default") -> None:

        """给 B站会话/通知类接口加轻量节流（这些接口直连 bilibili_session，不走 client.throttle）。



        评论/@/动态通知保留 8~14 秒保守节流；私信快检要满足 5 分钟内回复，

        使用独立的较短节流，并只在确实需要抓取会话详情时调用。

        """

        kind = str(kind or "default")

        if kind == "dm":

            pm_cfg = (self.chat_cfg.get("private_messages") or {})

            try:

                base = float(pm_cfg.get("session_api_min_gap_seconds") or 2.0)

            except Exception:

                base = 2.0

            try:

                jitter = float(pm_cfg.get("session_api_jitter_seconds") or 2.0)

            except Exception:

                jitter = 2.0

            base = max(0.5, min(8.0, base))

            jitter = max(0.0, min(6.0, jitter))

        else:

            base, jitter = 8.0, 6.0

        now = asyncio.get_event_loop().time()

        attr = f"_last_session_api_{kind}"

        wait = base + random.random() * jitter - (now - getattr(self, attr, 0.0))

        if wait > 0:

            await asyncio.sleep(wait)

        setattr(self, attr, asyncio.get_event_loop().time())



    def _detect_offline_gap(self) -> int:

        """读取上次活跃时间，计算离线时长（秒）。没有记录视为在线（0）。"""

        try:

            last = self.state.data.get("interactive_last_active", "")

            if not last:

                return 0

            from datetime import datetime as _dt

            last_dt = _dt.fromisoformat(str(last))

            return max(0, int((_dt.now() - last_dt).total_seconds()))

        except Exception:

            return 0



    def _mark_active(self) -> None:

        """记录当前活跃时间（供下次离线检测使用）。"""

        self.state.data["interactive_last_active"] = now_iso()

        # 有界裁剪：interactive_replied 只保留最近 N 条，防止 state 文件无限膨胀。

        # 保留上限 30000、裁剪到 25000；unfollowed_cache 单独保留。

        try:

            replied = self.state.data.setdefault("interactive_replied", {})

            if len(replied) > STATE_REPLIED_MAX:

                for k in list(replied.keys())[:-STATE_REPLIED_TRIM_TO]:

                    replied.pop(k, None)

        except Exception:

            pass

        # unfollowed_cache：清理超过 30 天未刷新的记录（用户已永久取关或早已换号）。

        try:

            from datetime import datetime as _dt

            now_ts = int(_dt.now().timestamp())

            cache = self.state.data.get("unfollowed_cache") or {}

            stale = [k for k, v in cache.items() if (now_ts - float(v or 0)) > STATE_UNFOLLOW_STALE]

            for k in stale:

                cache.pop(k, None)

        except Exception:

            pass

        self.state.save()



    def _in_catchup(self) -> bool:

        """当前是否处于「离线补回复」模式：机器人离线过一段时间，期间消息要补回。



        实际判定在 run_once 开头一次性完成（见 _begin_catchup_if_needed），

        并把结果存到 _catchup_active；本轮内所有调用保持一致。

        只在一轮内生效，下一轮自动退出（避免整个运行期间持续补发）。

        """

        return bool(getattr(self, "_catchup_active", False))



    def _begin_catchup_if_needed(self) -> bool:

        """本轮开始时判定是否进入补回复模式（每轮实时判定，不消费标记）。



        用「最近活跃时间」与当前时间的差计算离线时长——机器人恢复正常轮询后，

        该差值每轮递减，自然退出补回复，不会永久生效。

        """

        if not self.catchup_enabled:

            self._catchup_active = False

            return False

        if getattr(self, "_catchup_active", False):

            return True

        # 实时重算（而不是用 __init__ 的启动快照），保证恢复在线后能退出补回复

        self.catchup_offline_seconds = self._detect_offline_gap()

        if self.catchup_offline_seconds >= self.catchup_threshold_seconds:

            self._catchup_active = True

            return True

        return False



    def _catchup_context_note(self) -> str:

        """补回复时附加到 AI 提示里的说明，让回复带“抱歉来晚了”的语气。"""

        if not self._in_catchup():

            return ""

        mins = max(1, round(self.catchup_offline_seconds / 60))

        return f"（这条消息是对方在机器人离线约{mins}分钟期间发来的，回复时先自然地带一句“抱歉刚才不在/来晚了”的歉意，再正常回应，语气要像真人。）\n"



    def _maybe_lyric_opener(self, text: str, channel: str) -> str:

        """恋恋的歌曲：偶尔在私信/评论回复开头用两句歌词表达心情（中文）。



        channel: dm / reply / at —— 对应 config koishi_lyrics 的开关。

        """

        from .lyrics import maybe_prepend_lyrics

        lyrics_cfg = self.config.data.get("koishi_lyrics") or {}

        if not lyrics_cfg.get("enabled", True):

            return text

        apply = {

            "dm": lyrics_cfg.get("apply_to_dm", False),

            "reply": lyrics_cfg.get("apply_to_replies", True),

            "at": lyrics_cfg.get("apply_to_replies", True),

        }.get(channel, False)

        if not apply:

            return text

        return maybe_prepend_lyrics(text, lyrics_cfg)



    def log(self, message: str, level: str = "INFO") -> None:

        append_log(self.config.log_file, message, level)



    def _reset_round_metrics(self) -> None:

        """Reset counters used only for privacy-preserving round telemetry."""

        self._round_candidates = 0

        self._round_failures = 0

        self._round_unknown = 0



    def _add_round_candidates(self, count: int) -> None:

        try:

            self._round_candidates = max(0, int(getattr(self, "_round_candidates", 0))) + max(0, int(count or 0))

        except Exception:

            pass



    def _private_recent_context(

        self, rows: list[dict[str, Any]], current_msg: dict[str, Any], talker_id: str,

        *, limit: int = 6, summary_limit: int = 9, image_context: str = "",

        image_note: str = "", requirements: str = "",

    ) -> str:

        """同一用户的最近对话优先，总预算2600；本次输入另传，不重复计入。"""

        user_memory = ""

        try:

            user_memory = self.memory_engine.user_context(str(talker_id), max_chars=400)

        except Exception:

            pass

        return build_private_context(

            rows, current_msg, talker_id, str(self.client.uid or ""),

            history=self.dm_session_memory.confirmed_replies(talker_id),

            limit=limit, summary_limit=summary_limit, max_chars=2600,

            image_context=image_context, image_note=image_note,

            requirements=requirements, user_memory=user_memory,

        )





    def _remember_dm_user_if_any(self, uid: str | int, text: str, *, reply: str = "") -> None:

        """为私信用户记录轻量长期记忆：对方说过什么、问过什么、偏好什么。"""

        uid = str(uid or "").strip()

        text = str(text or "").strip()

        if not uid:

            return

        pm_cfg = (self.chat_cfg.get("private_messages") or {})

        if not pm_cfg.get("remember_user_facts", True):

            return

        facts: list[str] = []

        if text and not (is_rule_override(text) or reject_generated_reply(text)):

            t = text[:160]

            # 轻量模式：只记带信息量的句子，避免把所有闲聊原样塞进记忆。

            if len(t) >= 6:

                if any(k in t for k in ("我是", "我叫", "叫我", "可以叫我", "其实我", "平时", "喜欢", "最爱", "推", "厨", "单推", "本命", "最近在", "正在", "想问", "请问", "能不能", "可不可以", "记得", "别忘了")):

                    facts.append("对方提到：" + t)

                elif t.endswith(("?", "？")) or any(k in t for k in ("吗", "呢", "为什么", "怎么", "啥", "什么", "有没有")):

                    facts.append("对方问过：" + t)

                elif len(t) >= 12:

                    facts.append("最近聊到：" + t)

        # 哪怕没有可提炼的新事实，也记录一次互动，确保 interactions/last_seen 会增长。

        if not facts:

            facts.append("最近互动过")

        # 去重保序

        seen: set[str] = set()

        uniq = []

        for f in facts:

            if f not in seen:

                seen.add(f)

                uniq.append(f)

        try:

            self.memory_engine.remember_user(uid, uniq)

        except Exception as exc:

            self.log(f"记录私信用户记忆失败 uid={uid}：{exc}", "WARN")



    def _learn_user_correction_if_any(self, text: str, *, source: str = "comment-correction", video_id: str = "",
                                      sender_uid: str = "",

                                      image_paths: list[str] | None = None) -> bool:

        """用户指出机器人认错角色时，自动写入长期学习库，并保存原画面样本。"""

        text = str(text or "").strip()

        if not text or len(text) > 500 or is_rule_override(text) or not self._is_command_admin(sender_uid):

            return False

        cfg = (self.chat_cfg.get("correction_learning") or self.config.data.get("correction_learning") or {})

        if not cfg.get("enabled", True):

            return False

        case = detect_character_correction(text)

        if not case:

            return False

        learned_any = False

        try:

            item = learn_correction(self.learn_db, case, source=source, video_id=video_id)

            if item:

                learned_any = True

                self.log(f"已学习用户纠错：{case.correct} 不是 {case.wrong} source={source} video={video_id}", "SUCCESS")

                learned = self.learn_db.context_for(f"{case.correct} {case.wrong} 角色识别纠错", limit=2, max_chars=1200)

                if learned and learned not in self.knowledge_text:

                    self.knowledge_text = (self.knowledge_text + "\n\n【刚学习到的纠错知识】\n" + learned).strip()

        except Exception as exc:

            self.log(f"学习用户纠错失败：{exc}", "WARN")

        if cfg.get("remember_sample_images", True):

            try:

                added = remember_correction_sample_images(

                    correct=case.correct,

                    wrong=case.wrong,

                    video_id=video_id,

                    source=source,

                    image_paths=image_paths,

                    config=cfg.get("sample_images") or {},

                )

                if added:

                    learned_any = True

                    self.log(f"已保存角色纠错样本图片：{case.correct} 不是 {case.wrong}，新增 {added} 张 video={video_id}", "SUCCESS")

            except Exception as exc:

                self.log(f"保存角色纠错样本图片失败：{exc}", "WARN")

        return learned_any



    # ------------------------------------------------------------------

    # 互关私信开放功能：东方Project角色识别 + 通知拒收记录

    # ------------------------------------------------------------------

    def _role_recognition_cfg(self) -> dict[str, Any]:

        cfg = dict((self.chat_cfg.get("role_recognition_dm") or self.config.data.get("role_recognition_dm") or {}))

        cfg.setdefault("enabled", True)

        cfg.setdefault("command", "识别角色")

        cfg.setdefault("pending_timeout_seconds", 10 * 60)

        cfg.setdefault("template_start", "识别开始～请把要看的图片发过来，Koishi会只按东方Project角色特征谨慎判断；不确定就会直说不确定，不会硬猜哦。")

        cfg.setdefault("mutual_only", True)

        cfg.setdefault("max_images", 1)

        cfg.setdefault("timeout_seconds", 75)

        return cfg



    @staticmethod

    def _is_role_recognition_command(text: str) -> bool:

        t = str(text or "").strip()

        if not t:

            return False

        t = re.sub(r"\s+", "", t)

        t = t.strip("：:!！#@")

        for prefix in ("指令", "命令", "执行", "@命令"):

            if t.startswith(prefix):

                t = t[len(prefix):].strip("：:!！#@")

        return t in {"识别角色", "角色识别", "识图角色", "东方角色识别", "识别开始", "开始识别"}



    @staticmethod

    def _is_notification_optout_text(text: str) -> bool:

        t = re.sub(r"\s+", "", str(text or "").strip())

        return t in {"拒收", "拒收通知", "拒收动态通知", "拒收群发", "不想收到", "别发了", "不要再发"}



    @staticmethod

    def _is_notification_optin_text(text: str) -> bool:

        t = re.sub(r"\s+", "", str(text or "").strip())

        return t in {"取消拒收", "恢复接收", "继续接收", "接收通知"}



    def _notification_optout_file(self) -> str:

        return str((self.config.data.get("notification_optouts") or {}).get("file") or "data/notification_optouts.json")



    def _load_notification_optouts(self) -> dict[str, Any]:

        data = load_json(self._notification_optout_file(), {})

        if not isinstance(data, dict):

            data = {}

        data.setdefault("uids", {})

        data.setdefault("updated_at", now_iso())

        return data



    def _record_notification_optout(self, uid: str | int, *, text: str = "", name: str = "") -> dict[str, Any]:

        uid = str(uid or "").strip()

        data = self._load_notification_optouts()

        if uid:

            data.setdefault("uids", {})[uid] = {

                "uid": uid,

                "name": str(name or self._admin_display_name(uid) or "").strip(),

                "source": "dm",

                "text": str(text or "")[:120],

                "updated_at": now_iso(),

            }

            data["updated_at"] = now_iso()

            save_json_atomic(self._notification_optout_file(), data)

        return data



    def _remove_notification_optout(self, uid: str | int) -> dict[str, Any]:

        uid = str(uid or "").strip()

        data = self._load_notification_optouts()

        if uid:

            data.setdefault("uids", {}).pop(uid, None)

            data["updated_at"] = now_iso()

            save_json_atomic(self._notification_optout_file(), data)

        return data



    def _role_pending_map(self) -> dict[str, Any]:

        return self.state.data.setdefault("dm_role_recognition_pending", {})



    def _get_role_pending(self, uid: str | int) -> dict[str, Any] | None:

        uid = str(uid or "").strip()

        if not uid:

            return None

        pending = self._role_pending_map().get(uid)

        if not isinstance(pending, dict):

            return None

        try:

            expire_at = float(pending.get("expire_at") or 0)

        except Exception:

            expire_at = 0.0

        if expire_at and expire_at < time.time():

            self._role_pending_map().pop(uid, None)

            self.state.save()

            return None

        return pending



    def _set_role_pending(self, uid: str | int, *, request_key: str = "") -> None:

        cfg = self._role_recognition_cfg()

        ttl = max(60, min(3600, int(cfg.get("pending_timeout_seconds") or 600)))

        self._role_pending_map()[str(uid)] = {

            "uid": str(uid),

            "request_key": str(request_key or ""),

            "created_at": now_iso(),

            "expire_at": time.time() + ttl,

        }

        self.state.save()



    def _clear_role_pending(self, uid: str | int) -> None:

        self._role_pending_map().pop(str(uid), None)

        self.state.save()



    async def _is_mutual_user(self, uid: str | int) -> bool:

        uid = str(uid or "").strip()

        if not uid:

            return False

        if uid == str(self.owner_uid) or self._is_command_admin(uid):

            return True

        cache = self.state.data.setdefault("mutual_relation_cache", {})

        try:

            c = cache.get(uid) or {}

            if isinstance(c, dict) and time.time() - float(c.get("checked_at") or 0) < 3600:

                return bool(c.get("mutual"))

        except Exception:

            pass

        try:

            from bilibili_api import user as bili_user

            target = bili_user.User(uid=int(uid), credential=self.client.credential)

            relation = await asyncio.wait_for(target.get_relation(), timeout=20)

            rel_obj = (relation or {}).get("relation") or {}

            be_obj = (relation or {}).get("be_relation") or {}

            rel = int(rel_obj.get("attribute") or 0) if isinstance(rel_obj, dict) else int(rel_obj or 0)

            be = int(be_obj.get("attribute") or 0) if isinstance(be_obj, dict) else int(be_obj or 0)

            mutual = bool((rel & 2) and (be & 4))

            cache[uid] = {"mutual": mutual, "checked_at": time.time()}

            self.state.save()

            return mutual

        except Exception as exc:

            self.log(f"检查互关关系失败 uid={uid}：{type(exc).__name__}: {str(exc)[:120]}", "WARN")

            return False



    def _recent_role_command_before(self, rows: list[dict[str, Any]], current_msg: dict[str, Any], talker_id: str | int, cfg: dict[str, Any]) -> bool:

        """State races/restarts may lose pending flag; recover by scanning recent user command before this image."""

        try:

            cur_seq = int(current_msg.get("msg_seqno") or 0)

        except Exception:

            cur_seq = 0

        try:

            cur_ts = int(current_msg.get("timestamp") or 0)

        except Exception:

            cur_ts = 0

        ttl = max(60, min(3600, int(cfg.get("pending_timeout_seconds") or 600)))

        for old in reversed(rows):

            if str(old.get("sender_uid")) != str(talker_id):

                continue

            try:

                seq = int(old.get("msg_seqno") or 0)

            except Exception:

                seq = 0

            if cur_seq and seq >= cur_seq:

                continue

            txt = _message_text(old)

            if not txt:

                continue

            try:

                ts = int(old.get("timestamp") or 0)

            except Exception:

                ts = 0

            if cur_ts and ts and cur_ts - ts > ttl:

                return False

            if self._is_role_recognition_command(txt):

                return True

            # 遇到较新的普通用户文本，说明这张图不一定是接在指令后面的。

            if ts and cur_ts and cur_ts - ts <= ttl:

                return False

        return False



    async def _recognize_touhou_character_image_urls(self, urls: list[str], uid: str | int) -> str:

        cfg = self._role_recognition_cfg()

        urls = [u for u in (urls or []) if str(u).strip()]

        if not urls:

            return "还没收到图片呢，把要识别的东方Project角色图发给恋恋就好～"

        max_images = max(1, min(3, int(cfg.get("max_images") or 1)))

        timeout = max(20, min(180, int(cfg.get("timeout_seconds") or 75)))

        paths: list[str] = []

        try:

            import tempfile

            import uuid

            from pathlib import Path as _P

            tmpdir = _P(tempfile.gettempdir()) / "koishi_role_recognition"

            tmpdir.mkdir(parents=True, exist_ok=True)

            run_id = uuid.uuid4().hex[:8]

            for i, u in enumerate(urls[:max_images]):

                ext = ".jpg"

                for cand in (".png", ".webp", ".jpeg", ".gif"):

                    if cand in str(u).lower():

                        ext = cand

                        break

                p = str(tmpdir / f"role_{uid}_{run_id}_{i}{ext}")

                saved = await self.client.download_image_url(u, p)

                if saved:

                    paths.append(saved)

            if not paths:

                return "图片下载失败了，Koishi没能看到原图，所以还不能判断是谁；可以稍后重发一次，或换成普通图片再发。"



            # 先查本地训练/纠错样本。命中非常近的手工样本时，样本优先级高于视觉模型猜测。

            sample_matches: list[dict[str, Any]] = []

            try:

                sample_cfg = ((self.chat_cfg.get("correction_learning") or self.config.data.get("correction_learning") or {}).get("sample_images") or {})

                sample_cfg = dict(sample_cfg or {})

                sample_cfg.setdefault("enabled", True)

                sample_cfg.setdefault("match_enabled", True)

                sample_cfg.setdefault("db_file", "data/character_correction_samples.json")

                sample_cfg.setdefault("match_threshold", 14)

                sample_matches = match_character_samples(paths, config=sample_cfg, max_results=5)

            except Exception as exc:

                self.log(f"私信角色识别样本匹配失败 uid={uid}：{type(exc).__name__}: {str(exc)[:120]}", "WARN")

            if sample_matches:

                best = sample_matches[0]

                try:

                    raw_dist = best.get("distance")

                    dist = int(raw_dist) if str(raw_dist).strip() != "" else 999

                except Exception:

                    dist = 999

                correct = str(best.get("correct") or "").strip()

                wrong = str(best.get("wrong") or "").strip()

                source = str(best.get("source") or best.get("video_id") or "训练/纠错样本").strip()

                if correct and dist <= 4:

                    if wrong and wrong != "official-reference":

                        return self._hard_limit_text(f"识别结果：确认是『{correct}』。这张图和本地训练/纠错样本高度一致（相似距离 {dist}），样本记录里特别标过：它是『{correct}』，不是『{wrong}』。所以这次不按外观直觉硬猜，优先采用训练样本结论。", 360)

                    return self._hard_limit_text(f"识别结果：确认是『{correct}』。这张图命中了本地角色参考样本（相似距离 {dist}，来源：{source}），所以优先采用样本标注。", 360)



            ref = ""

            try:

                ref = self.media.character_recognizer.full_reference_fallback()

            except Exception:

                ref = self.media.character_reference_for_context("东方Project 角色识别 私信图片")

            sample_hint = format_sample_match_hint(sample_matches)

            if sample_hint:

                ref = (sample_hint + "\n\n" + ref).strip()

            prompt = (

                "这是互关用户通过私信发来的角色识别图片。任务：仅识别东方Project角色。\n"

                "重要优先级：如果【本地角色样本匹配】提示存在，尤其是手工训练/纠错样本，请优先采用样本结论；不要被相似的发色、帽子或阳伞误导。\n"

                "请按图片可见证据判断：先区分身体本体、头饰/饰品、服装和道具，再看发色/瞳色、帽子或头饰、服装配色和款式、道具、翅膀/尾巴/第三只眼/面具、画面文字。绿色蝴蝶结/发饰不是绿发，衣服/袋子/挂件颜色不是发色或身体特征，边界不清要写疑似饰品/不确定。\n"

                "输出要求：\n"

                "1. 先给结论：确认角色 / 疑似角色 / 非东方Project或无法确认。\n"

                "2. 列出2-4条可见依据。\n"

                "3. 如果只像但证据不足，必须说‘疑似’或‘不确定’，不要硬猜。\n"

                "4. 如果图里有多个角色，逐个列出；如果不是东方Project角色，请明确说明。\n"

                "5. fumo/毛绒/Q版/二创换装常有色偏或简化：重点比发型长度+帽子形制+服装主色+标志物的组合，不要单靠发色或翅膀凑一个名字；若多个候选都能对上部分特征，必须并列写出候选并说明各自缺失的强特征，禁止只挑一个硬说‘确认’。\n"

                "用恋恋语气，但保持简洁，不要超过260字。"

            )

            # 私信识别角色走高精度优先：样本命中已在上面直接返回；未命中时用

            # role_vision_model（gpt-5.5）判断。高可信通道失败/无输出时不再回退

            # minimax-m3 保底（5.5 可信度更高，低可信判断宁可不猜，避免误导）。

            gpt_desc = ""

            try:

                gpt_desc = await asyncio.wait_for(

                    self.ai.describe_images_via_fallback_vision(

                        paths,

                        prompt=prompt + "\n\n这是私信单图识别，请优先给出最稳的东方Project角色结论；若是fumo/毛绒/漫画/表情包/二创换装，不要因形态变化直接判非东方，要抓核心特征。",

                        reference_text=ref,

                        max_tokens=520,

                        max_images=max_images,

                        model=self.ai.step_model(

                            "role_vision",

                            default=getattr(self.ai, "role_vision_model", "") or "",

                        ) or None,

                    ),

                    timeout=timeout,

                )

            except Exception as exc:

                self.log(f"私信角色识别GPT优先通道失败 uid={uid}：{type(exc).__name__}: {str(exc)[:120]}，不降级到主视觉", "WARN")

            desc = clean_reply_text(" ".join(str(gpt_desc or "").split()))

            if not desc:

                return "这张图Koishi没能稳定确认角色……可能是特征太少或画风太Q版，宁可先不猜。可以换一张更清楚、特征更明显的同角色图再让Koishi认一认～"

            return self._hard_limit_text("识别结果：" + desc, 520)

        except asyncio.TimeoutError:

            return "识别超时了……图片可能太复杂或视觉通道有点慢，可以稍后再试一次。"

        except Exception as exc:

            self.log(f"私信角色识别失败 uid={uid}：{type(exc).__name__}: {str(exc)[:160]}", "WARN")

            return "识别时出了点小状况，可以稍后再发一次图片给Koishi。"

        finally:

            for p in paths:

                try:

                    import os

                    os.remove(p)

                except Exception:

                    pass



    async def close(self) -> None:

        worker = getattr(self, "_at_video_worker", None)

        if worker is not None and not worker.done():

            worker.cancel()

            try:

                await worker

            except asyncio.CancelledError:

                pass

        await self.client.close()



    async def ensure_login(self) -> bool:

        if getattr(self, "_logged_in", False):

            return True

        ok = self.client.load_cookies() and await self.client.ensure_login()

        if ok:

            self._logged_in = True

            self.log(f"互动聊天登录验证成功，UID={self.client.uid}")

        else:

            self.log("互动聊天登录失败；请先 python main.py login", "ERROR")

        return ok



    async def run_once(self) -> dict[str, int]:

        started = time.perf_counter()

        usage_before = self.ai.usage_summary()

        self.ai.round_call_start = int(getattr(self.ai, "call_count", 0) or 0)

        self._reset_round_metrics()

        if not await self.ensure_login():

            emit_round_metrics(

                self.log,

                mode="chat-run",

                started=started,

                ai=self.ai,

                usage_before=usage_before,

                candidate_count=0,

                attempt_count=0,

                sent_count=0,

                success_count=0,

                failure_count=1,

                error_category="LoginError",

            )

            raise RuntimeError("B站未登录或 Cookie 无效")

        # 顶层总开关：interactive_chat.enabled=false 时整轮不执行任何互动。

        if not self.chat_cfg.get("enabled", True):

            return {"dm": 0, "comment": 0, "at": 0, "dynamic": 0}

        # 本轮开始时一次性判定补回复模式（本轮内所有 _in_catchup() 保持一致）。

        if self._begin_catchup_if_needed():

            self.log(f"检测到离线约 {max(1, round(self.catchup_offline_seconds / 60))} 分钟，本轮进入补回复模式（@/回复/私信）", "INFO")

        result = {"dm": 0, "comment": 0, "at": 0, "dynamic": 0}

        handlers = [

            ("dm", (self.chat_cfg.get("private_messages") or {}).get("enabled", False), self.handle_private_messages),

            ("at", (self.chat_cfg.get("at_replies") or {}).get("enabled", False), self.handle_at_notifications),

            ("comment", (self.chat_cfg.get("comment_replies") or {}).get("enabled", False), self.handle_comment_replies),

            ("dynamic", (self.chat_cfg.get("dynamic_comments") or {}).get("enabled", True), self.handle_dynamic_comments),

        ]

        try:

            for name, enabled, handler in handlers:

                if not enabled:

                    continue

                try:

                    result[name] = await handler()

                except Exception as exc:

                    self._round_failures = int(getattr(self, "_round_failures", 0) or 0) + 1

                    self.log(f"互动频道 {name} 本轮失败，继续处理其它频道：{_format_exception_detail(exc)}", "ERROR")

        finally:

            self._catchup_active = False

        self._mark_active()

        self.state.save()

        success_count = sum(max(0, int(v or 0)) for v in result.values())

        failure_count = int(getattr(self, "_round_failures", 0) or 0)

        unknown_count = int(getattr(self, "_round_unknown", 0) or 0)

        attempt_count = success_count + failure_count + unknown_count

        emit_round_metrics(

            self.log,

            mode="chat-run",

            started=started,

            ai=self.ai,

            usage_before=usage_before,

            candidate_count=int(getattr(self, "_round_candidates", 0) or 0),

            attempt_count=attempt_count,

            sent_count=success_count,

            success_count=success_count,

            failure_count=failure_count,

            unknown_count=unknown_count,

            skipped_count=max(0, int(getattr(self, "_round_candidates", 0) or 0) - attempt_count),

        )

        return result



    async def run_forever(self) -> None:

        try:

            poll = max(10, int(float(self.chat_cfg.get("poll_interval_seconds") or 90)))

        except (TypeError, ValueError):

            poll = 90

        dm_fast_cfg = (self.chat_cfg.get("private_messages") or {})

        try:

            dm_fast_poll = int(dm_fast_cfg.get("fast_poll_seconds") or 30)

        except Exception:

            dm_fast_poll = 30

        dm_fast_poll = max(10, min(60, dm_fast_poll))

        self.log(f"互动聊天已启动，每 {poll} 秒完整检查一次；私信每 {dm_fast_poll} 秒快检一次。")

        asyncio.create_task(self._private_fast_loop(dm_fast_poll))

        consecutive_509 = 0

        while True:

            try:

                result = await self.run_once()

                total = result["dm"] + result["comment"] + result["at"] + result["dynamic"]

                if total > 0:

                    self.log(f"互动聊天本轮完成：私信 {result['dm']} 条，评论回复 {result['comment']} 条，@回复 {result['at']} 条，动态回复 {result['dynamic']} 条")

                consecutive_509 = 0  # 成功一轮，重置退避计数

            except KeyboardInterrupt:

                raise

            except RuntimeError as exc:

                if "未登录" in str(exc) or "Cookie" in str(exc):

                    consecutive_509 += 1

                    self.log(f"登录状态无效/疑似网络抖动（连续第{consecutive_509}次）：{exc}", "ERROR")

                    if consecutive_509 >= 3:

                        self.log("连续登录失败，为避免复读刷屏，互动聊天已停止。请先 python main.py login，再 python main.py chat-run。", "ERROR")

                        # 非 0 退出码：让 supervisor 识别为「需人工处理」，避免无限自动重启。

                        raise SystemExit(2)

                    backoff = min(300, 60 * consecutive_509)

                    self.log(f"登录检查疑似短暂异常，{backoff} 秒后重试；未连续失败满 3 次前不退出。", "WARN")

                    await asyncio.sleep(backoff)

                    continue

                self.log(f"互动聊天本轮异常：{exc}", "ERROR")

            except Exception as exc:

                low = str(exc).lower()

                if "-509" in low or "请求过于频繁" in low or "rate limit" in low:

                    # B站限频：退避，暂停更久，避免连续触发风控。

                    consecutive_509 += 1

                    backoff = min(60 * 5, 60 * (2 ** min(consecutive_509, 5)))

                    self.log(f"B站限频(-509)，退避 {backoff} 秒后重试（连续第{consecutive_509}次）", "WARN")

                    await asyncio.sleep(backoff)

                    continue

                self.log(f"互动聊天本轮异常：{exc}", "ERROR")

            await asyncio.sleep(max(10, poll))



    async def _private_fast_loop(self, poll: int) -> None:

        """独立私信快检：主轮在看视频/评论时，私信仍按 5 分钟要求及时处理。"""

        while True:

            await asyncio.sleep(max(10, int(poll or 30)))

            try:

                cfg = self.chat_cfg.get("private_messages") or {}

                if not self.chat_cfg.get("enabled", True) or not cfg.get("enabled", False):

                    continue

                if not await self.ensure_login():

                    continue

                pm_cfg = (self.chat_cfg.get("private_messages") or {})

                try:

                    loop_timeout = float(pm_cfg.get("poll_timeout_seconds") or 270)

                except Exception:

                    loop_timeout = 270.0

                loop_timeout = max(60.0, min(300.0, loop_timeout))

                sent = await self.handle_private_messages()

                if sent:

                    self.log(f"私信快检完成：回复 {sent} 条", "INFO")

                self._mark_active()

                self.state.save()

            except asyncio.TimeoutError:

                self.log("私信快检超过硬超时仍未完成，已取消本轮；下一轮继续补回复", "ERROR")

                await asyncio.sleep(5)

            except RuntimeError as exc:

                if "未登录" in str(exc) or "Cookie" in str(exc):

                    self.log(f"私信快检登录异常：{exc}", "ERROR")

                    await asyncio.sleep(60)

                else:

                    self.log(f"私信快检异常：{type(exc).__name__}: {exc}", "ERROR")

            except Exception as exc:

                low = str(exc).lower()

                if "-509" in low or "请求过于频繁" in low or "rate limit" in low:

                    self.log("私信快检遇到B站限频，暂停60秒", "WARN")

                    await asyncio.sleep(60)

                else:

                    self.log(f"私信快检异常：{type(exc).__name__}: {exc}", "ERROR")



    def _catchup_limit(self, configured: int, default: int) -> int:

        """补回复模式下放宽每轮回复上限（离线期间堆积的消息需要更多轮次消化）。"""

        if self._in_catchup():

            cap = int((self.chat_cfg.get("catchup") or {}).get("max_catchup_replies_per_run") or 8)

            return max(int(configured or default), cap)

        return int(configured or default)



    async def handle_private_messages(self) -> int:

        if getattr(self, "_dm_poll_lock", None) is not None and self._dm_poll_lock.locked():

            self.log("私信处理已在进行中，本次快检跳过以避免重复回复", "INFO")

            return 0

        async with self._dm_poll_lock:

            cfg = (self.chat_cfg.get("private_messages") or {})

            try:

                loop_timeout = float(cfg.get("poll_timeout_seconds") or 270)

            except Exception:

                loop_timeout = 270.0

            loop_timeout = max(60.0, min(300.0, loop_timeout))

            return await asyncio.wait_for(self._handle_private_messages_locked(), timeout=loop_timeout)



    async def _handle_private_messages_locked(self) -> int:

        cfg = self.chat_cfg.get("private_messages") or {}

        from datetime import datetime as _dt

        poll_budget = max(60.0, min(300.0, float(cfg.get("poll_timeout_seconds") or 270)))

        deadline = time.monotonic() + poll_budget - 10.0

        max_replies = max(self._catchup_limit(cfg.get("max_replies_per_run"), 20), int(cfg.get("guarantee_min_replies_per_run") or 20))

        allowed_uids = {str(x) for x in cfg.get("allowed_uids") or [] if str(x).strip()}

        blocked_uids = {str(x) for x in cfg.get("blocked_uids") or [] if str(x).strip()}

        blocked_uids |= {str(x) for x in (self.chat_cfg.get("blocked_uids") or []) if str(x).strip()}

        # 注意：get_sessions 天然只返回"已存在的会话"，所以 only_existing_sessions 恒为真，

        # 无需额外判断（原 _has_existing_dm_session 恒真 no-op，已移除）。

        sent = 0

        await self._throttle_session_api("dm")

        try:

            fetch_timeout = float(cfg.get("session_fetch_timeout_seconds") or 35)

        except Exception:

            fetch_timeout = 35.0

        fetch_timeout = max(10.0, min(60.0, fetch_timeout))

        sessions = await asyncio.wait_for(bili_session.get_sessions(self.client.credential), timeout=fetch_timeout)

        rows = list(sessions.get("session_list") or [])

        # 历史会话太多时，逐个拉取会占满 270 秒硬超时，新的私信也跟着延迟。

        # 每轮固定查最近会话，并轮流扫描其余会话，避免旧会话永久饿死。

        session_cap = max(2, int(cfg.get("max_sessions_per_poll") or 8))

        rotating, tail, cursor = [], [], 0
        if len(rows) > session_cap:

            recent_count = min(session_cap - 1, max(1, int(cfg.get("recent_sessions_per_poll") or 4)))

            tail = rows[recent_count:]

            rotating_slots = session_cap - recent_count

            cursor = int(self.state.data.get("dm_session_cursor") or 0) % len(tail)

            rotating = [tail[(cursor + i) % len(tail)] for i in range(rotating_slots)]


            from itertools import zip_longest
            rows = [row for pair in zip_longest(rows[:recent_count], rotating) for row in pair if row is not None]

        # 取关状态缓存（state 持久化，24 小时内不重复查询 get_relation）。

        # 默认允许未关注但主动发来私信的人获得同质量回复；如需防打扰，可在配置中关闭 reply_unfollowed_users。

        reply_unfollowed_users = bool(cfg.get("reply_unfollowed_users", True))

        unfollow_cache = self.state.data.setdefault("unfollowed_cache", {})

        unfollow_ttl = STATE_UNFOLLOW_TTL

        for row in rows:

            if sent >= max_replies:

                break

            # 预留快检收尾时间；未扫描的会话留给下一轮轮转处理。

            if deadline - time.monotonic() < 90:

                break

            talker_id = str(row.get("talker_id") or "")

            if not talker_id or talker_id in blocked_uids:

                continue

            if allowed_uids and talker_id not in allowed_uids:

                continue

            # 允许未关注但主动发来私信的人获得正常回复；关闭配置时才按关注关系过滤。

            if not reply_unfollowed_users:

                try:

                    cached = unfollow_cache.get(talker_id)

                    cached_ts = float(cached) if cached else 0.0

                    if (int(_dt.now().timestamp()) - cached_ts) < unfollow_ttl:

                        # 缓存的"已取关"标记持续生效

                        if cached_ts > 0:

                            self.log(f"跳过私信：{talker_id} 已取关（缓存，不再回复）", "INFO")

                            continue

                    else:

                        from bilibili_api import user as bili_user

                        fu = bili_user.User(uid=int(talker_id), credential=self.client.credential)

                        rel = await fu.get_relation()

                        ber = (rel or {}).get("be_relation") or {}

                        if isinstance(ber, dict):

                            battr = int(ber.get("attribute") or 0)

                        else:

                            battr = int(ber or 0)

                        # be_relation 表示对方与我们的关系：attribute 含 4 表示对方关注了我们。

                        if (battr & 4) == 0:

                            # 已取关 → 记录时间戳，24 小时内不再重复查询

                            unfollow_cache[talker_id] = str(int(_dt.now().timestamp()))

                            self.log(f"跳过私信：{talker_id} 已取关（不再回复）", "INFO")

                            continue

                        else:

                            unfollow_cache.pop(talker_id, None)  # 对方仍关注，清除缓存

                except Exception as exc:

                    self.log(f"检查取关状态失败 uid={talker_id}：{exc}", "WARN")

            else:

                # 以前缓存过的未关注标记不再用于跳过该主动私信会话。

                unfollow_cache.pop(talker_id, None)

            before_claims = dict(self.state.data.get("interactive_replied") or {})
            self._dm_delivery_started = False
            try:

                try:

                    one_timeout = float(cfg.get("per_session_timeout_seconds") or 240)

                except Exception:

                    one_timeout = 240.0

                one_timeout = min(max(30.0, min(120.0, one_timeout)), max(5.0, deadline-time.monotonic()))

                if row in rotating:
                    self.state.data["dm_session_cursor"] = (cursor + rotating.index(row) + 1) % len(tail)
                    self.state.save()
                self._dm_session_deadline = time.monotonic() + one_timeout
                before_claims = dict(self.state.data.get('interactive_replied') or {})
                self._dm_delivery_started = False
                sent += await asyncio.wait_for(self._handle_one_private_session(talker_id, row, cfg), timeout=one_timeout)

            except asyncio.TimeoutError as exc:
                for pending_key, pending_value in list(self.state.data.get('interactive_replied', {}).items()):
                    if (pending_key.startswith(f'dm:{talker_id}:') and str(pending_value).startswith('sending:')
                            and before_claims.get(pending_key) != pending_value):
                        self.state.finalize_interactive([pending_key], 'unknown' if self._dm_delivery_started else 'failed', expected_claim=pending_value)
                self.state.save()

                self.log(f"处理私信会话超时 uid={talker_id}（>{int(one_timeout)}秒），本轮跳过，下一轮继续补回复", "WARN")

                await self._notify_owner_dm_failure(talker_id, "session_fetch_or_handle", "处理私信会话超时", status="failed")

            except Exception as exc:

                self.log(f"处理私信会话失败 uid={talker_id}：{exc}", "WARN")

                await self._notify_owner_dm_failure(talker_id, "session_fetch_or_handle", exc, status="failed")

        return sent



    async def _fetch_private_session_rows(self, talker_id: str, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        """Read recent messages and a resumable older page without skipping pending replies."""
        fetch_timeout = max(10.0, min(60.0, float(cfg.get("session_fetch_timeout_seconds") or 35)))
        deadline = time.monotonic() + fetch_timeout
        session_deadline = getattr(self, "_dm_session_deadline", None)
        if session_deadline is not None:
            deadline = min(deadline, session_deadline - 25.0)
        if deadline <= time.monotonic():
            return []
        cap = max(30, min(300, int(cfg.get("fetch_message_limit") or 80)))
        max_age = max(1, min(7 * 86400, int((self.chat_cfg.get("catchup") or {}).get("max_age_seconds") or 7 * 86400)))
        cutoff = max(int(time.time()) - max_age, int(cfg.get("min_timestamp") or 0))
        head = await asyncio.wait_for(
            bili_session.fetch_session_msgs(int(talker_id), self.client.credential, session_type=1, begin_seqno=0),
            timeout=deadline - time.monotonic(),
        )
        rows = list({str(row.get("msg_key") or row.get("msg_seqno")): row
                     for row in (head.get("messages") or [])}.values())[:cap]
        if not rows:
            return []

        def seq(row: dict[str, Any]) -> int:
            try:
                return int(row.get("msg_seqno") or 0)
            except (ValueError, TypeError):
                return 0

        seen = {str(row.get("msg_key") or seq(row)) for row in rows}
        positive = [seq(row) for row in rows if seq(row) > 0]
        if not positive:
            return rows[:cap]
        head_max, head_min = max(positive), min(positive)
        cursors = self.state.data.get("dm_history_cursor") or {}
        previous = dict(cursors.get(str(talker_id)) or {})
        cursor = int(previous.get("end_seqno") or head_min)
        exhausted = bool(previous.get("exhausted"))
        old_head = int(previous.get("head_seqno") or 0)
        # A gap beyond the newest page must be backfilled before older history.
        if (not old_head or head_min > old_head or
                int(previous.get("updated_at") or 0) < int(time.time()) - max_age):
            cursor, exhausted = head_min, False
        if cursor <= 0 or cursor > head_min:
            cursor, exhausted = head_min, False

        while not exhausted and len(rows) < cap and deadline - time.monotonic() > 1.0:
            # An explicit complete newest page needs no history request.
            if cursor == head_min and head.get("has_more") == 0:
                exhausted = True
                break
            await self._throttle_session_api("dm")
            remaining = deadline - time.monotonic()
            if remaining <= 1.0:
                break
            try:
                page = await asyncio.wait_for(
                    self.client.fetch_private_messages_before(talker_id, cursor, size=min(30, cap - len(rows))),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                self.log(f"私信历史抓取预算用尽 uid={talker_id}，下轮从原游标继续", "WARN")
                break
            except Exception as exc:
                self.log(f"私信历史抓取失败 uid={talker_id}：{type(exc).__name__}，保留已读取消息与游标", "WARN")
                break
            older = [row for row in (page.get("messages") or []) if 0 < seq(row) < cursor]
            if not older:
                exhausted = True
                break
            next_cursor = min(seq(row) for row in older)
            if next_cursor >= cursor:
                break
            pending = False
            for row in older:
                identity = str(row.get("msg_key") or seq(row))
                if identity not in seen and len(rows) < cap:
                    rows.append(row)
                    seen.add(identity)
                stamp = int(row.get("timestamp") or 0)
                if stamp and stamp < cutoff:
                    continue
                if str(row.get("sender_uid")) != str(talker_id) or int(row.get("msg_type") or 0) not in {1, 2}:
                    continue
                key = f"dm:{talker_id}:{identity}"
                status = self.state.data.get("interactive_replied", {}).get(key)
                if not interactive_status_done(status) or self._dm_status_retryable_for_guarantee(status):
                    pending = True
            # Do not advance past a page whose replies remain unfinished.  With
            # a one-reply-per-user quota those other messages need the next poll.
            if pending:
                break
            cursor = next_cursor
            stamps = [int(row.get("timestamp") or 0) for row in older]
            if page.get("has_more") == 0 or (stamps and all(stamp and stamp < cutoff for stamp in stamps)):
                exhausted = True
        if not self.config.dry_run:
            self.state.data.setdefault("dm_history_cursor", {})[str(talker_id)] = {
                "end_seqno": cursor, "head_seqno": head_max,
                "exhausted": exhausted, "updated_at": int(time.time()),
            }
            self.state.save()
        rows.sort(key=seq)
        return rows[:cap]

    async def _handle_one_private_session(self, talker_id: str, session_row: dict[str, Any], cfg: dict[str, Any]) -> int:

        if self._uid_blocked(talker_id):

            return 0

        # 【按用户日上限】主人要求限制内容异常/刷屏的用户每天最多收到 N 条私信。

        # 计数在 _send_private_text_guarded 里按"确认发出"累加（所以也包括拒收确认、

        # 角色识别结果这类非闲聊回复）。到上限就整轮跳过，省掉后面的模型调用。

        _daily_cap = self._dm_daily_limit(talker_id)

        _daily_used = self.state.dm_sent_today(talker_id) if _daily_cap > 0 else 0

        if _daily_cap > 0 and _daily_used >= _daily_cap:

            self.log(f"私信日上限：uid={talker_id} 今天已发 {_daily_used}/{_daily_cap} 条，跳过本轮", "INFO")

            return 0

        await self._throttle_session_api("dm")

        rows = await self._fetch_private_session_rows(talker_id, cfg)

        self._add_round_candidates(len(rows))

        replied = 0

        attempted = 0

        max_per_user = int(cfg.get("max_replies_per_user_per_run") or 20)

        in_catchup = self._in_catchup()

        # 每条私信仍会处理；积压消息分轮消化，避免单个会话占满快检的硬超时。

        max_per_user = max(1, max_per_user)

        # 但**按用户日上限**优先：今天还剩几条额度就最多回几条（见 _dm_daily_limit）。

        if _daily_cap > 0:

            max_per_user = min(max_per_user, max(1, _daily_cap - _daily_used))

        # 补回复只处理最近 7 天内的消息，不回复很久以前的历史消息。

        from datetime import datetime as _dt

        catchup_window = max(1, min(7 * 86400, int((self.chat_cfg.get("catchup") or {}).get("max_age_seconds") or 7 * 86400)))

        now_ts = int(_dt.now().timestamp())



        def _dm_row_priority(m: dict[str, Any]) -> tuple[int, int]:

            """私信处理顺序：新消息优先，旧失败补偿靠后，避免历史 unknown 卡住刚来的私信。"""

            try:

                ts = int(m.get("timestamp") or 0)

            except Exception:

                ts = 0

            try:

                typ = int(m.get("msg_type") or 0)

            except Exception:

                typ = 0

            if str(m.get("sender_uid")) == str(self.client.uid):

                return (9, ts)

            if typ not in {1, 2}:

                return (8, ts)

            k = f"dm:{talker_id}:{m.get('msg_key') or m.get('msg_seqno')}"

            st = self.state.data.setdefault("interactive_replied", {}).get(k)

            if not st:

                # 5 分钟内的新私信放最前，确保实时响应不被旧债挤掉。

                return (0 if ts and now_ts - ts <= 300 else 1, ts)

            if self._dm_status_retryable_for_guarantee(st):

                return (2, ts)

            return (7, ts)



        rows_for_processing = sorted(rows, key=_dm_row_priority)

        for msg in rows_for_processing:

            if attempted >= max_per_user:

                break

            if str(msg.get("sender_uid")) != str(talker_id):

                continue

            msg_type = int(msg.get("msg_type") or 0)

            # B站系统通知/卡片消息（如里程碑、官方提醒）也会出现在私信会话里，

            # 但不能当普通用户私信回复；否则会向系统通知 UID 发消息并触发“非法参数”。

            # 另外：B站系统会话 uid 844424930131966 即使 msg_type=1 也不能回复，直接忽略。

            if msg_type not in {1, 2} or str(talker_id) == "844424930131966":

                key = f"dm:{talker_id}:{msg.get('msg_key') or msg.get('msg_seqno')}"

                if not self.config.dry_run:
                    self.state.data["interactive_replied"][key] = "ignored"
                    self.state.save()

                continue

            msg_ts = int(msg.get("timestamp") or 0)

            min_ts = int(cfg.get("min_timestamp", 0) or 0)

            # 补回复模式：离线期间的消息不因“太旧”被跳过，但限定在时间窗内

            if msg_ts and (now_ts-msg_ts) > catchup_window:
                continue
            if min_ts and msg_ts < min_ts:
                continue

            key = f"dm:{talker_id}:{msg.get('msg_key') or msg.get('msg_seqno')}"

            status = self.state.data.setdefault("interactive_replied", {}).get(key)

            force_retry_dm = self._dm_status_retryable_for_guarantee(status)

            # 发送失败冷却：旧消息（failed/ignored/过期sending）重试按 UID 冷却；

            # unknown 是投递结果不明，可能已经发出，不自动重试，避免重复私信。

            if force_retry_dm and self._dm_uid_in_cooldown(talker_id):

                continue

            # 新鲜 sending 说明同一条私信已有进程正在/刚刚尝试发送；先跳过，

            # 等超过 5 分钟再由“保证回复”逻辑接管，满足主人要求的时限。

            if isinstance(status, str) and status.startswith("sending:") and not force_retry_dm:

                continue

            if self._is_interactive_done(key) and not force_retry_dm:

                continue

            if force_retry_dm:

                # 不能直接 pop：claim_interactive 会重新读取磁盘并合并状态，旧 terminal 可能又回来。

                # 标 failed 可覆盖旧 ignored/failed/过期 sending，使本轮能重新占位发送；unknown 不会进入这里。

                if not self.config.dry_run:
                    self.state.data["interactive_replied"][key] = "failed"

                self.log(f"私信保证回复：重新处理此前状态={status} 的消息 uid={talker_id} key={key}", "INFO")

            # 失败也占本轮额度，否则平台拒绝或网络故障时会把历史消息全试一遍。

            attempted += 1

            text = _message_text(msg)

            # 主人要求“私信每一个都回复”：不再因为 msg_source/自动回复模板跳过私信。

            # 只有平台发送失败/安全拦截等硬条件才会阻止外发。

            # 私信图片：提取图片 URL（图片消息可能没有文本）

            dm_img_urls = _message_image_urls(msg)

            dm_image_message = msg

            previous_image_text = immediate_previous_user_text(rows, msg, talker_id)

            image_intent = infer_dm_image_intent(text, previous_image_text)

            # 图片后再补一句询问也能关联；普通“晚安”不能触发旧图识别。

            if not dm_img_urls and asks_about_previous_image(text):

                previous_image = immediate_previous_user_image(rows, msg, talker_id)

                if previous_image:

                    dm_img_urls = _message_image_urls(previous_image)

                    dm_image_message = previous_image

                    image_intent = infer_dm_image_intent(text)

            dm_img_cfg = self.chat_cfg.get("private_messages") or {}



            # 通知拒收/恢复：任何私信用户回复“拒收”都记录，群发脚本会自动过滤。

            if text and (self._is_notification_optout_text(text) or self._is_notification_optin_text(text)):

                is_optout = self._is_notification_optout_text(text)

                if not self.config.dry_run and not self.state.claim_interactive([key]):

                    self.log(f"通知拒收指令占位失败 uid={talker_id}", "WARN")

                    continue

                try:

                    if is_optout:

                        if not self.config.dry_run:
                            self._record_notification_optout(talker_id, text=text)

                        reply_text = "收到啦，Koishi已经记下：以后这类动态通知私信就不发给你了。如果之后想恢复，可以回复“取消拒收”。"

                    else:

                        if not self.config.dry_run:
                            self._remove_notification_optout(talker_id)

                        reply_text = "好哦，Koishi已经帮你恢复接收这类动态通知啦。"

                    safe = self._apply_morph(reply_text)

                    if self.config.dry_run:

                        self.log(f"[DRY-RUN] 通知拒收/恢复回复 uid={talker_id}，长度={len(safe)}")

                    else:

                        await self._send_private_text_guarded(talker_id, safe)

                    if not self.config.dry_run:
                        self.state.data["interactive_replied"][key] = "sent"
                        self.state.record_history({"action": "notification_optout" if is_optout else "notification_optin", "uid": talker_id, "incoming": text[:120], "reply": safe})
                        self.state.save()

                    replied += 1

                    continue

                except Exception as exc:

                    self.log(f"通知拒收/恢复回复失败 uid={talker_id}：{exc}", "ERROR")

                    if self.config.dry_run:
                        continue

                    status = "unknown" if isinstance(exc, OutboundDeliveryUnknown) else ("blocked" if self._private_send_error_nonretryable(exc) else "failed")

                    self.state.data["interactive_replied"][key] = status

                    self.state.save()

                    await self._notify_owner_dm_failure(talker_id, key, exc, status=status)

                    continue



            # 互关开放指令：识别角色。先登记等待下一张图；下一条图片消息到来后调用视觉模型识别东方Project角色。

            role_cfg = self._role_recognition_cfg()

            pending_role = self._get_role_pending(talker_id)

            explicit_general_image = (image_intent.mode == "scene" or

                                      (image_intent.mode == "general" and asks_about_previous_image(image_intent.request_text)))

            cancels_old_role = (explicit_general_image or

                                (not image_intent.should_analyze and bool(image_intent.request_text)))

            if dm_img_urls and cancels_old_role and pending_role and not self.config.dry_run:

                self._clear_role_pending(talker_id)

            if not cancels_old_role and (pending_role or (dm_img_urls and self._recent_role_command_before(rows, msg, talker_id, role_cfg))) and dm_img_urls:

                if not self.config.dry_run and not self.state.claim_interactive([key]):

                    self.log(f"角色识别图片占位失败 uid={talker_id}", "WARN")

                    continue

                try:

                    reply_text = await self._recognize_touhou_character_image_urls(dm_img_urls, talker_id)

                    safe = self._apply_morph(reply_text)

                    ok, reason, safe = validate_message(safe, self.config.forbidden_words, marker="", require_marker=False)

                    if not ok:

                        safe = "这张图的识别结果触发了安全拦截，Koishi先不发送啦。可以换一张更清楚的东方角色图试试。"

                    if self.config.dry_run:

                        self.log(f"[DRY-RUN] 角色识别结果 uid={talker_id}，长度={len(safe)}")

                    else:

                        await self._send_private_text_guarded(talker_id, safe, timeout_seconds=90)

                    # 如果只是图片下载失败/没拿到原图，不清除等待状态，让用户重发下一张图后还能继续识别。

                    if not self.config.dry_run and not any(x in reply_text for x in ("下载失败", "还没收到图片", "没能看到原图")):

                        self._clear_role_pending(talker_id)

                    if not self.config.dry_run:
                        self.state.data["interactive_replied"][key] = "sent"
                        self.state.record_history({"action": "dm_role_recognition", "uid": talker_id, "incoming": "[图片]", "reply": safe})
                        self._remember_dm_user_if_any(talker_id, "识别角色图片", reply=safe)
                        self.state.save()

                    replied += 1

                    continue

                except Exception as exc:

                    self.log(f"角色识别结果发送失败 uid={talker_id}：{exc}", "ERROR")

                    if self.config.dry_run:
                        continue

                    status = "unknown" if isinstance(exc, OutboundDeliveryUnknown) else ("blocked" if self._private_send_error_nonretryable(exc) else "failed")

                    self.state.data["interactive_replied"][key] = status

                    self.state.save()

                    await self._notify_owner_dm_failure(talker_id, key, exc, status=status)

                    continue



            if text and bool(role_cfg.get("enabled", True)) and self._is_role_recognition_command(text):

                if not self.config.dry_run and not self.state.claim_interactive([key]):

                    self.log(f"角色识别指令占位失败 uid={talker_id}", "WARN")

                    continue

                try:

                    allowed = True

                    if bool(role_cfg.get("mutual_only", True)):

                        allowed = await self._is_mutual_user(talker_id)

                    if allowed:

                        if not self.config.dry_run:
                            self._set_role_pending(talker_id, request_key=key)

                        reply_text = str(role_cfg.get("template_start") or "识别开始～请把要看的图片发过来，Koishi会只按东方Project角色特征谨慎判断。")

                    else:

                        reply_text = "这个识别角色指令现在只开放给互关朋友使用哦。"

                    safe = self._apply_morph(reply_text)

                    ok, reason, safe = validate_message(safe, self.config.forbidden_words, marker="", require_marker=False)

                    if not ok:

                        safe = "识别开始～请把要看的图片发过来。" if allowed else "这个指令暂时不能使用。"

                    if self.config.dry_run:

                        self.log(f"[DRY-RUN] 角色识别指令回复 uid={talker_id}，长度={len(safe)}")

                    else:

                        await self._send_private_text_guarded(talker_id, safe)

                    if not self.config.dry_run:
                        self.state.data["interactive_replied"][key] = "sent"
                        self.state.record_history({"action": "dm_role_recognition_start", "uid": talker_id, "incoming": text[:120], "reply": safe, "allowed": allowed})
                        self.state.save()

                    replied += 1

                    continue

                except Exception as exc:

                    self.log(f"角色识别指令回复失败 uid={talker_id}：{exc}", "ERROR")

                    if self.config.dry_run:
                        continue

                    status = "unknown" if isinstance(exc, OutboundDeliveryUnknown) else ("blocked" if self._private_send_error_nonretryable(exc) else "failed")

                    self.state.data["interactive_replied"][key] = status

                    self.state.save()

                    await self._notify_owner_dm_failure(talker_id, key, exc, status=status)

                    continue



            # 如果对方只发了一张/多张图片，不调用模型做视觉分析，直接回一张本地表情包。

            if dm_img_urls and not str(text or "").strip() and not image_intent.should_analyze and bool(dm_img_cfg.get("reply_sticker_on_image_only", True)):

                try:

                    sticker_probability = float(dm_img_cfg.get("image_only_sticker_probability", 1.0))

                except Exception:

                    sticker_probability = 1.0

                fallback_text = str(dm_img_cfg.get("image_only_fallback_text", "") or "").strip()

                sticker_sent, reply_desc = False, ""

                if self.config.dry_run:

                    dry_pic = self.stickers.choose("dm", target_uid=str(talker_id), probability_override=sticker_probability, allow_excluded=True)

                    if dry_pic:

                        sticker_sent, reply_desc = True, str(dry_pic.name)

                        self.log(f"[DRY-RUN] 将用表情包回复纯图片私信 uid={talker_id} file={dry_pic.name}")

                    elif fallback_text:

                        reply_desc = fallback_text

                        self.log(f"[DRY-RUN] 纯图片私信无可用表情，将发送兜底文字 uid={talker_id}，长度={len(fallback_text)}")

                    else:

                        self.log(f"[DRY-RUN] 纯图片私信无可用表情，暂不回复 uid={talker_id}", "WARN")

                else:

                    if not self.state.claim_interactive([key]):

                        self.log(f"纯图片私信占位失败，跳过重复回复 uid={talker_id}", "WARN")

                        continue

                    try:

                        sticker_sent, reply_desc = await self._send_private_sticker_only(

                            talker_id,

                            probability=sticker_probability,

                            fallback_text=fallback_text,

                            # 主人自己的 UID 在 exclude_uids 里，但"发图回图"这条路对主人也要生效

                            allow_excluded=bool(dm_img_cfg.get("image_only_allow_owner", True))

                            and str(talker_id) == str(self.owner_uid or ""),

                        )

                        self.state.data["interactive_replied"][key] = "sent" if reply_desc else "ignored"

                        if reply_desc:

                            self._clear_dm_failure_cooldown(talker_id)

                    except Exception as exc:

                        self.log(f"纯图片私信表情回复失败 uid={talker_id}：{exc}", "ERROR")

                        status = "unknown" if isinstance(exc, OutboundDeliveryUnknown) else ("blocked" if self._private_send_error_nonretryable(exc) else "failed")

                        self.state.data["interactive_replied"][key] = status

                        if status in {"unknown", "failed"}:

                            self._mark_dm_failure_cooldown(talker_id)

                        self.state.save()

                        await self._notify_owner_dm_failure(talker_id, key, exc, status=status)

                        continue

                if not self.config.dry_run:
                    self.state.record_history({
                        "action": "dm_image_only_sticker", "uid": talker_id,
                        "incoming": "[图片]", "reply": ("[表情包] " if sticker_sent else "") + str(reply_desc),
                    })
                    if reply_desc:
                        self._remember_dm_user_if_any(talker_id, "[图片]", reply=("[表情包] " if sticker_sent else "") + str(reply_desc))
                    self.state.save()

                replied += 1

                continue



            dm_img_ctx = ""

            dm_image_note = ""

            active_visual_task = ""

            img_enabled = bool((self.chat_cfg.get("comment_images") or {}).get("enabled", True))

            should_analyze_image = (image_intent.should_analyze or

                                    (not str(text or "").strip() and not image_intent.request_text

                                     and not bool(dm_img_cfg.get("reply_sticker_on_image_only", True))))

            if dm_img_urls and should_analyze_image and img_enabled:

                try:

                    dm_img_timeout = int(dm_img_cfg.get("image_analysis_timeout_seconds") or 20)

                except Exception:

                    dm_img_timeout = 20

                image_cap = int((self.chat_cfg.get("comment_images") or {}).get("max_images", 1) or 1)

                image_urls = dm_img_urls[:max(1, image_cap)]

                active_visual_task = infer_visual_task(image_intent.request_text, image_intent.mode if image_intent.mode != "sticker" else "general")

                cache_task = visual_task_cache_key(image_intent.request_text, active_visual_task)

                dm_img_ctx = self.dm_session_memory.find_image(

                    talker_id, image_urls, task_key=cache_task,

                    allow_general_reuse=cache_task == "general|whole",

                )

                if not dm_img_ctx:

                    try:

                        dm_img_ctx = await asyncio.wait_for(

                            self._analyze_image_urls(

                                image_urls, "私信图片", max_images=image_cap,

                                timeout=max(10, min(240, dm_img_timeout)),

                                request_text=image_intent.request_text,

                                image_mode=active_visual_task,

                            ), timeout=max(10, min(240, dm_img_timeout)),

                        )

                    except asyncio.TimeoutError:

                        dm_img_ctx = "【私信图片】未获得可靠视觉描述；不能假装看见了景物或细节，请自然说明暂时没看清。"

                    self.dm_session_memory.remember_image(

                        talker_id, image_urls, dm_img_ctx,

                        sequence=int(dm_image_message.get("msg_seqno") or 0),

                        timestamp=float(dm_image_message.get("timestamp") or 0),

                        task_key=cache_task,

                    )

            elif dm_img_urls and not should_analyze_image:

                dm_img_ctx = "【私信图片】未做视觉分析；对方只发了表情或表示不用看图，不能自行描述未看过的细节。"

            elif dm_img_urls:

                dm_img_ctx = "【私信图片】图片理解目前关闭，没有可靠视觉描述；不能假装看见了图片细节。"

            elif asks_about_previous_image(text) or any(word in text for word in ("那张", "刚才的图", "刚刚的图", "上面的图", "那些云", "那个倒影")):

                active_visual_task = infer_visual_task(text, "general")

                prior_task_key = visual_task_cache_key(text, active_visual_task)

                note_args = {"current_seq": int(msg.get("msg_seqno") or 0),

                             "current_timestamp": float(msg.get("timestamp") or 0)}

                dm_image_note = self.dm_session_memory.latest_image_note(

                    talker_id, **note_args, task_key=prior_task_key,

                    allow_general_reuse=prior_task_key == "general|whole",

                )

                if not dm_image_note and self.dm_session_memory.latest_image_note(talker_id, **note_args):

                    dm_img_ctx = "【先前私信图片】现有图片描述没有本次问题所需的可靠细节，不能假装完成新识别；可请对方重发原图或该区域截图。"

            # 私信保证回复：空白/无法解析消息也发一句确认；普通私信不再因关键词过滤直接忽略。

            # 违禁词仍会在 _finalize_reply 的出站安全校验中处理，避免发出危险内容。

            if text and not _valid_incoming_text(text, cfg):

                self.log(f"私信入站命中过滤但仍按保证回复处理 uid={talker_id}，长度={len(text)}", "INFO")



            # 管理员指令：管理员是最高管理员；只有被授权管理员才会执行，其他人走普通聊天。

            if self._is_command_admin(talker_id) and is_command(text):

                cmd_reply = ("已识别管理员指令，试运行不执行。" if self.config.dry_run else
                             await self._handle_owner_command(text, sender_uid=talker_id, source="dm"))

                if cmd_reply:

                    safe = self.emotes.decorate_message(cmd_reply, VideoItem(bvid="", aid=0, title="指令"), None, max_len=900)

                    safe = self._apply_morph(safe)

                    sent_ok = False

                    if self.config.dry_run:

                        self.log(f"[DRY-RUN] 主人指令回复 uid={talker_id}，长度={len(safe)}")

                    else:

                        if not self.state.claim_interactive([key]):

                            self.log(f"主人指令回复占位失败 uid={talker_id}", "WARN")

                            continue

                        try:

                            await self._send_private_text_guarded(talker_id, safe)

                            self.log(f"已执行主人指令并回复 uid={talker_id}，长度={len(safe)}", "SUCCESS")

                            sent_ok = True

                        except Exception as exc:

                            self.log(f"主人指令回复失败 uid={talker_id}：{exc}", "ERROR")

                            status = "unknown" if isinstance(exc, OutboundDeliveryUnknown) else ("blocked" if self._private_send_error_nonretryable(exc) else "failed")

                            self.state.data["interactive_replied"][key] = status

                            if status in {"unknown", "failed"}:

                                self._mark_dm_failure_cooldown(talker_id)

                    if self.config.dry_run or sent_ok:

                        if not self.config.dry_run:
                            self.state.data["interactive_replied"][key] = "sent"
                            self.state.record_history({"action": "owner_command", "uid": talker_id, "command": text[:200], "reply": safe})
                            self._remember_dm_user_if_any(talker_id, text, reply=safe)

                        replied += 1

                    if not self.config.dry_run:
                        self.state.save()

                    continue



            ctx_n = max(6, int(cfg.get("context_message_count", 6) or 6))

            summary_n = max(0, int(cfg.get("context_summary_message_count", 9) or 9))

            recent_ctx = self._private_recent_context(

                rows, msg, talker_id, limit=ctx_n, summary_limit=summary_n,

                image_context=dm_img_ctx, image_note=dm_image_note,

                requirements=("用恋恋的自然语气回应当前问题或分享；最近对话优先，更早摘要补前情。"

                              "延续双方的称呼、已确认事实、话题和承诺；指代不明时简短确认，不自行补故事。"

                              "视觉描述是证据，不能把猜测当事实；无描述时说明没看清。"

                              "遵循人格但不要输出内部观察清单；不营销。"

                              + (build_visual_reply_hint(active_visual_task) if active_visual_task else "")

                              + self._catchup_context_note()),

            )

            reply = await self._generate_chat_reply(

                incoming_text=text or "（对方发来一张图片）",

                context=recent_ctx,

                max_len=int(cfg.get("reply_max_length") or REPLY_DEFAULT_MAX_LEN),

                uid=str(talker_id),

                use_premium=True,

                dm_mode=True,

                visual_task=active_visual_task,

            )

            fake_item = VideoItem(bvid="", aid=0, title=text[:40], desc=text)

            ok, reason, safe = self._finalize_reply(

                reply, channel="dm", fake_item=fake_item, max_len=int(cfg.get("reply_max_length") or REPLY_DEFAULT_MAX_LEN), is_comment=False,

                preserve_quotes=active_visual_task in {"ocr", "ui"},

            )

            if not ok:

                self.log(f"私信回复被拦截 uid={talker_id}：{reason}", "WARN")

                if not self.config.dry_run:
                    self.state.data["interactive_replied"][key] = "blocked"
                    self.state.save()

                continue

            if self.config.dry_run:

                self.log(f"[DRY-RUN] 将回复私信 uid={talker_id}，长度={len(safe)}")

                # dry_run 不写 sent 标记（实际未发送，避免污染生产状态导致漏回复）

            else:

                if not self.state.claim_interactive([key]):

                    self.log(f"私信回复占位失败，跳过重复回复 uid={talker_id}", "WARN")

                    continue

                try:

                    await self._send_private_with_optional_sticker(talker_id, safe)

                    self.log(f"已回复私信 uid={talker_id}，长度={len(safe)}", "SUCCESS")

                    self.state.data["interactive_replied"][key] = "sent"

                    self._clear_dm_failure_cooldown(talker_id)

                except Exception as exc:

                    self.log(f"私信回复失败 uid={talker_id}：{exc}", "ERROR")

                    # 21047 等平台硬限制不是临时失败，继续重试会每轮刷失败；标 blocked 停止重复打扰。

                    status = "unknown" if isinstance(exc, OutboundDeliveryUnknown) else ("blocked" if self._private_send_error_nonretryable(exc) else "failed")

                    self.state.data["interactive_replied"][key] = status

                    if status in {"unknown", "failed"}:

                        self._mark_dm_failure_cooldown(talker_id)

                    self.state.save()

                    await self._notify_owner_dm_failure(talker_id, key, exc, status=status)

                    continue

            if not self.config.dry_run:
                self.state.record_history({"action": "dm_auto_reply", "uid": talker_id, "incoming": text[:200], "reply": safe})
                self.dm_session_memory.add_confirmed_reply(

                    talker_id, str(msg.get("msg_key") or ""), text or "[图片消息]", safe,

                    incoming_seq=int(msg.get("msg_seqno") or 0),

                )

                self._remember_dm_user_if_any(talker_id, text or "[图片消息]", reply=safe)

                self.state.save()

            replied += 1

        return replied



    async def handle_comment_replies(self) -> int:

        cfg = self.chat_cfg.get("comment_replies") or {}

        max_replies = self._catchup_limit(cfg.get("max_replies_per_run"), 3)

        await self._throttle_session_api()

        data = await bili_session.get_replies(self.client.credential)

        rows = data.get("items") or []

        self._add_round_candidates(len(rows))

        sent = 0

        for row in rows:

            if sent >= max_replies:

                break

            try:

                if await self._handle_one_comment_notice(row, cfg):

                    sent += 1

            except Exception as exc:

                self.log(f"处理评论回复通知失败：{exc}", "WARN")

        return sent



    async def handle_at_notifications(self) -> int:

        """处理 @机器人 的通知：观看视频并回复 @ 的人，并 @ 回对方。"""

        self._at_video_enqueued_round = 0

        cfg = self.chat_cfg.get("at_replies") or {}

        max_replies = self._catchup_limit(cfg.get("max_replies_per_run"), 2)

        await self._throttle_session_api()

        # 已持久化的任务在通知读取失败时也能继续；后台观看不占住私信轮询。

        from .at_video import start_worker

        if cfg.get("video_comment_enabled", False) and not getattr(self, "_at_video_feature_logged", False):

            self.log("视频@邀请功能已启用：后台观看、回复内容、标准独立评论、持久化去重")

            self._at_video_feature_logged = True

        start_worker(self, cfg)

        data = await bili_session.get_at(self.client.credential)

        rows = data.get("items") or []

        self._add_round_candidates(len(rows))

        sent = 0

        for row in rows:

            if sent + self._at_video_enqueued_round >= max_replies:

                break

            try:

                if await self._handle_one_at_notice(row, cfg):

                    sent += 1

            except Exception as exc:

                self.log(f"处理@通知失败：{exc}", "WARN")

        start_worker(self, cfg)

        return sent



    async def handle_dynamic_comments(self) -> int:

        """回复在自己动态下评论的人（动态评论区自动互动）。"""

        cfg = self.chat_cfg.get("dynamic_comments") or {}

        if not cfg.get("enabled", True):

            return 0

        from bilibili_api import dynamic as bili_dynamic

        # 检查的动态数：默认覆盖最近 20 条动态（此前 3 条会漏掉排后面的动态评论）。

        max_dyn = int(cfg.get("max_dynamics_to_check") or 20)

        max_replies = int(cfg.get("max_replies_per_run") or 3)

        max_len = int(cfg.get("reply_max_length") or REPLY_DEFAULT_MAX_LEN)

        sent = 0

        # 评论区已关闭/失效的动态：加入持久跳过清单，避免每轮轮询都重试刷屏。

        closed_map = self.state.data.setdefault("dyn_comment_closed", {})

        closed_marker = ("12002", "评论功能已关闭", "评论区已关闭", "评论已关闭", "不存在")

        try:

            await self._throttle_session_api()

            dyns = await bili_dynamic.get_dynamic_page_list(

                credential=self.client.credential,

                host_mid=int(self.client.uid),

                pn=1,

            )

        except Exception as exc:

            self.log(f"获取自己动态失败：{exc}", "WARN")

            return 0

        for d in dyns[:max_dyn]:

            if sent >= max_replies:

                break

            try:

                # 主人要求：旧动态也不能跳过；只要有人在动态下发评论，就必须尝试回复。

                # 因此动态扫描不再按动态发布时间/最大年龄/未知发布时间过滤。

                dyn_info = None

                rid = 0

                if not rid:

                    rid = await d.get_rid()

                if not rid:

                    continue

                if str(rid) in closed_map:

                    continue  # 已确认评论区关闭/失效的动态，整轮跳过

                await self._throttle_session_api()

                # 不同动态的评论区类型不同：图文/普通动态用 DYNAMIC_DRAW(11)，

                # 视频转载/特殊动态要 type=VIDEO(1) 才能读到（否则 -404 啥都木有，

                # 会把这类动态下的真实评论全部漏掉）。依次尝试，第一个成功即用。

                # 翻页：评论区超过一页（20条）时也要读后面几页，否则更老的评论永远扫不到。

                items: list[dict[str, Any]] = []

                used_type: Any = None

                for ctype in (comment.CommentResourceType.DYNAMIC_DRAW, comment.CommentResourceType.VIDEO):

                    if items:

                        break

                    try:

                        for pn in range(1, 6):  # 最多翻 5 页（100条），页面不满即停

                            replies = await comment.get_comments(

                                oid=int(rid),

                                type_=ctype,

                                page_index=pn,

                                credential=self.client.credential,

                            )

                            page = (replies or {}).get("replies") or []

                            items.extend(page)

                            used_type = ctype

                            if len(page) < 20:

                                break

                            await self._throttle_session_api()

                    except Exception as exc:

                        detail = _format_exception_detail(exc)

                        if items:

                            break  # 已在翻页中，忽略后续页错误，用已取到的

                        if "-404" not in detail and "啥都木有" not in detail:

                            if any(m in detail for m in closed_marker):

                                # 该动态评论区整体关闭/失效（如 12002），任何 type 都读不到，

                                # 记入持久跳过清单，本轮及以后不再扫它，避免无限重试刷屏。

                                closed_map[str(rid)] = detail[:120]

                                self.state.save()

                                self.log(f"动态评论区已关闭/失效，加入跳过清单 dyn_rid={rid} err={detail[:100]}", "INFO")

                                items = []

                                break

                            raise

                        items = []

                ctype = used_type

                self._add_round_candidates(len(items))

                for r in items:

                    if sent >= max_replies:

                        break

                    try:

                        if await self._handle_one_dynamic_comment(r, rid, cfg, max_len, resource_type=ctype):

                            sent += 1

                    except Exception as exc:

                        detail = _format_exception_detail(exc)

                        self.log(

                            f"处理动态评论失败 rid={rid} rpid={r.get('rpid')} author_mid={(r.get('member') or {}).get('mid')} "

                            f"message={str(((r.get('content') or {}).get('message') or ''))[:120]!r} err={detail}",

                            "WARN",

                        )

            except Exception as exc:

                detail = _format_exception_detail(exc)

                if "啥都木有" in detail or "-404" in detail:

                    # 动态已失效/无评论是轮询常态，降为 INFO 避免刷屏

                    self.log(f"动态无评论/已失效（跳过） dyn={d.get_dynamic_id()} err={detail[:100]}", "INFO")

                elif any(m in detail for m in closed_marker):

                    # 评论功能整体关闭：加入持久跳过清单，避免每轮 WARN 刷屏+空重试

                    rid_now = locals().get("rid", 0)

                    if rid_now:

                        closed_map[str(rid_now)] = detail[:120]

                        self.state.save()

                    self.log(f"动态评论区已关闭/失效（跳过） dyn={d.get_dynamic_id()} err={detail[:100]}", "INFO")

                else:

                    self.log(f"处理动态 {d.get_dynamic_id()} 评论失败 rid={locals().get('rid', '')} err={detail}", "WARN")

        if sent:

            self.log(f"动态评论互动：回复 {sent} 条", "INFO")

        return sent



    def _canned_short_dynamic_reply(self, rpid: int, incoming: str) -> str | None:

        """动态评论区极短正向评论（≤6字或纯表情）→ 简短甜回。



        太长/含疑问或非赞美内容仍走 AI+质量门；纯表情与轻量夸赞类给

        一句恋恋式回应，避免“3个字也拒回”的冷淡感（视频长评论禁令不受影响）。

        """

        import random as _random

        import re as _re

        text = _re.sub(r"\[[^\]]*\]", "", str(incoming or ""))

        text = " ".join(text.split())

        if "？" in text or "?" in text or "什么" in text or "为什么" in text or "怎么" in text or "吗" in text:

            return None

        if len(text) > 6:

            return None

        pos = ("可爱", "贴贴", "抱抱", "亲亲", "戳戳", "awsl", "好耶", "好看", "好听",

               "喜欢", "萌萌", "嘿嘿", "哈哈", "呜呜", "555", "老婆", "mua", "爱了",

               "捏", "呜", "草", "笑死", "好看", "好活", "太强", "赞")

        core = text

        hit = any(k in core for k in pos) or (text == "" and incoming and "[" in str(incoming))

        if not hit:

            return None

        pool = [

            "嘿嘿，收到啦～",

            "（轻轻戳回去）可爱捏～",

            "呜哇被夸了，无意识都要冒泡泡了…",

            "诶嘿，就当你在夸我啦♪",

            "贴贴！今天也要开开心心哦～",

            "（开心地晃了晃帽子）",

        ]

        return pool[rpid % len(pool)]



    def _comment_answered_key(self, oid, rpid) -> str:

        """跨路径统一去重键：同一条评论（oid 下 rpid）无论从评论通知、@ 还是动态扫描

        路径处理，都只回复一次。各路径原有的 key（comment_notice:/at_notice:/dynamic_replied:）

        互相独立，不加统一键会导致同一条评论被回复两次。"""

        return f"comment_answered:{oid}:{rpid}"



    @staticmethod

    def _dynamic_publish_ts(info: dict[str, Any] | None) -> int:

        """尽量从动态详情中提取发布时间；无法确认时返回 0。"""

        if not isinstance(info, dict):

            return 0

        item = info.get("item") if isinstance(info.get("item"), dict) else info

        candidates: list[Any] = []

        basic = item.get("basic") if isinstance(item.get("basic"), dict) else {}

        candidates.extend([

            item.get("pub_ts"), item.get("pub_time"), item.get("timestamp"), item.get("ctime"), item.get("ptime"),

            basic.get("pub_ts"), basic.get("pub_time"), basic.get("timestamp"), basic.get("ctime"), basic.get("ptime"),

        ])

        modules = item.get("modules") if isinstance(item.get("modules"), dict) else {}

        author = modules.get("module_author") if isinstance(modules.get("module_author"), dict) else {}

        candidates.extend([author.get("pub_ts"), author.get("pub_time"), author.get("timestamp"), author.get("ctime")])

        for raw in candidates:

            if raw is None:

                continue

            try:

                ts = int(raw)

                if ts > 0:

                    return ts // 1000 if ts > 10_000_000_000 else ts

            except Exception:

                continue

        return 0



    @staticmethod

    def _dynamic_rid(info: dict[str, Any] | None) -> int:

        """从已取得的动态详情里读取评论区 rid，避免为时间过滤再请求一次详情。"""

        if not isinstance(info, dict):

            return 0

        item = info.get("item") if isinstance(info.get("item"), dict) else info

        basic = item.get("basic") if isinstance(item.get("basic"), dict) else {}

        for raw in (basic.get("rid_str"), basic.get("rid"), item.get("rid_str"), item.get("rid")):

            try:

                rid = int(raw or 0)

                if rid:

                    return rid

            except Exception:

                continue

        return 0



    async def _allow_dynamic_comment_rid(self, rid: int) -> bool:

        """评论通知兜底到动态评论前，确认目标动态仍在允许窗口内。"""

        # 主人要求：旧动态也不能跳过；评论通知兜底到动态评论时，

        # 不再按动态发布时间、最大年龄、未知发布时间或最近动态窗口过滤。

        return True



    async def _handle_one_dynamic_comment(self, r: dict[str, Any], rid: int, cfg: dict[str, Any], max_len: int,

                                         resource_type: Any = None) -> bool:

        """回复一条动态下别人的评论；返回是否处理。"""

        member = r.get("member") or {}

        author_mid = str(member.get("mid") or "")

        if not author_mid or author_mid == str(self.client.uid):

            return False  # 自己的评论不回复

        if self._uid_blocked(author_mid):

            return False  # 黑名单用户不回复（扫描每轮会重复路过，但成本极低，无需打标）

        rpid = str(r.get("rpid") or "")

        if not rpid:

            return False

        key = f"dynamic_replied:{rid}:{rpid}"

        if self._is_interactive_done(key):

            return False  # 已回复过

        # 统一去重：评论通知/@ 等其它路径可能已回复过同一条评论（各自 key 互相独立，

        # 不加统一键会导致同一条评论被回复两次——"给不廷机回复两条"就是这个原因）。

        canon = self._comment_answered_key(rid, rpid)

        if self._is_interactive_done(canon):

            self.state.data["interactive_replied"][key] = "sent"  # 同步标记，避免每轮重复扫描

            return False

        ctime = int(r.get("ctime") or r.get("reply_time") or 0)

        # 主人要求：旧动态下的新/旧评论都必须回复；动态评论不再按评论时间过滤。

        incoming = str((r.get("content") or {}).get("message") or "").strip()

        self._learn_user_correction_if_any(incoming, source="dynamic-comment", video_id=str(rid), sender_uid=author_mid)

        if await self._send_admin_command_comment_reply(

            incoming=incoming, sender_uid=author_mid, key=key, canon=canon,

            oid=int(rid), root_id=int(rpid), parent_id=int(rpid),

            resource_type=resource_type or comment.CommentResourceType.DYNAMIC_DRAW,

            channel_label="dynamic-comment", max_len=int(max_len or 900),

        ):

            return True

        # 统一入站过滤：长度/ignore_patterns/注入防护/悼念（动态评论 cfg 已含 ignore_patterns）。

        if not _valid_incoming_text(incoming, cfg):

            self.state.data["interactive_replied"][key] = "ignored"

            return False

        nickname = str(member.get("uname") or "朋友")

        # 查看对方发在动态评论区的图片（理解其内容后再回复）

        img_ctx = await self._comment_image_context(r, tag="动态评论配图")

        # 极短正向短评（≤6字 / 纯表情）→ 专用罐头甜回，不启动 AI 与空话质量门

        canned = self._canned_short_dynamic_reply(int(rpid), incoming) if not img_ctx else ""

        if canned:

            reply = canned

            safe = canned

            ok = True

            reason = ""

        else:

            # 生成回复（AI，或 fallback 模板）

            source_ctx = build_comment_source_context(r, author_mid, nickname)

            context = (

                f"场景：B站动态评论区，有人在你（古明地恋/一只恋妖精）的动态下评论。\n"

                f"{source_ctx}"

                f"{img_ctx}"

                f"要求：以Koishi口吻自然回应，友善，针对对方说的话回应，不要营销、不要自称AI；"

                f"如果对方在聊具体内容/图片/视频/角色/音乐，请按每日互动质量基准抓住细节和氛围，不要只短夸；如果对方发了图片（有【动态评论配图】信息），回复里要自然地回应图片内容。"

            )

            reply = await self._generate_chat_reply(incoming_text=incoming, context=context, max_len=max_len, use_premium=True, uid=author_mid)

            fake_item = VideoItem(bvid="", aid=0, title="动态评论区", desc=incoming)

            ok, reason, safe = self._finalize_reply(

                reply, channel="reply", fake_item=fake_item, max_len=max_len, is_comment=True,

                preserve_quotes=bool(img_ctx) and infer_visual_task(incoming, "general") in {"ocr", "ui"},

            )

        if not ok:

            self.log(f"动态评论回复被拦截 {rpid}：{reason}", "WARN")

            self._mark_comment_draft_rejected(key, canon, reason)

            return False

        if self.config.dry_run:

            self.log(f"[DRY-RUN] 将回复动态评论 rpid={rpid}：{safe}")

            # 注意：dry_run 不写入 sent 标记——否则测试会污染生产状态，

            # 导致真实运行时跳过这些评论（之前反复出现"没回复"就是测试误标记导致的）。

            return True

        else:

            if not self.state.claim_interactive([key, canon]):

                self.log(f"动态评论回复占位失败，跳过重复回复 rpid={rpid}", "WARN")

                return False

            reply_claim = self.state.data["interactive_replied"][key]

            try:

                # 动态评论回复是子评论；B站子评论接口不稳定/通常不支持带图，避免表情包导致结果未知或重复风险。

                pic_path = None

                try:

                    await self.client.send_comment_with_optional_picture(

                        text=safe,

                        oid=int(rid),

                        type_=resource_type or comment.CommentResourceType.DYNAMIC_DRAW,

                        root=int(rpid),

                        parent=int(rpid),

                        pic_path=pic_path,

                    preserve_quotes=bool(img_ctx) and infer_visual_task(incoming, "general") in {"ocr", "ui"})

                    if pic_path:

                        self.log(f"已回复动态评论 rpid={rpid}（附表情包 {pic_path.name}）：{safe}", "SUCCESS")

                    else:

                        self.log(f"已回复动态评论 rpid={rpid}：{safe}", "SUCCESS")

                except OutboundDeliveryUnknown:

                    # 带图请求的结果不明确时，不能把同一文本降级重发。

                    raise

                except Exception as exc_pic:

                    if not pic_path:

                        raise

                    self.log(f"动态评论表情包发送失败，降级纯文字 rpid={rpid} file={pic_path.name}：{exc_pic}", "WARN")

                    await self.client.send_comment_with_optional_picture(

                        text=safe,

                        oid=int(rid),

                        type_=resource_type or comment.CommentResourceType.DYNAMIC_DRAW,

                        root=int(rpid),

                        parent=int(rpid),

                    preserve_quotes=bool(img_ctx) and infer_visual_task(incoming, "general") in {"ocr", "ui"})

                    self.log(f"已回复动态评论 rpid={rpid}：{safe}", "SUCCESS")

            except Exception as exc:

                self.log(

                    f"动态评论回复失败 rid={rid} rpid={rpid} author_mid={author_mid} incoming={incoming[:120]!r} "

                    f"reply={safe[:120]!r} err={_format_exception_detail(exc)}",

                    "ERROR",

                )

                self._mark_reply_failed(key, canon, expected_claim=reply_claim)

                return False

            # 点赞对方评论

            if int(cfg.get("like_incoming_comment", True)):

                try:

                    await self.client.like_comment(int(rid), int(rpid), type_=resource_type)

                    self.log(f"已点赞动态评论 rpid={rpid}", "SUCCESS")

                except Exception as exc:

                    self.log(f"点赞动态评论失败 rpid={rpid}：{exc}", "WARN")

            self.state.data["interactive_replied"][key] = "sent"

            self.state.data["interactive_replied"][canon] = "sent"

        self.state.record_history({

            "action": "dynamic_comment_reply" if not self.config.dry_run else "dynamic_comment_reply_dry_run",

            "dynamic_rid": str(rid),

            "oid": str(rid),

            "rpid": rpid,

            "root_id": rpid,

            "parent_id": rpid,

            "author_mid": author_mid,

            "uid": author_mid,

            "nickname": nickname,

            "incoming": incoming[:500],

            "source_comment": incoming[:500],

            "root_context": str(r.get("root_reply_content") or r.get("target_reply_content") or "")[:500],

            "video_title": "动态评论区",

            "bvid": "",

            "reply": safe,

        })

        self.state.save()

        return True



    async def _handle_one_at_notice(self, row: dict[str, Any], cfg: dict[str, Any]) -> bool:

        from .at_video import is_video_invitation, enqueue_invitation

        if is_video_invitation(row, cfg):

            return await enqueue_invitation(self, row, cfg)

        item = row.get("item") or {}

        user = row.get("user") or {}

        at_id = str(row.get("id") or item.get("id") or item.get("source_id") or "")

        if not at_id:

            return False

        key = f"at_notice:{at_id}"

        if self._uid_blocked(str(user.get("mid") or "")):

            self.state.data["interactive_replied"][key] = "blocked"

            self.state.save()

            return False

        if self._is_interactive_done(key):

            return False



        incoming = comment_incoming_text(item)

        needs_direct_reply = comment_requires_direct_reply(incoming)

        uri = str(item.get("uri") or item.get("native_uri") or "")

        self._learn_user_correction_if_any(incoming, source="at-notice", video_id=extract_bvid(uri) or uri,
                                           sender_uid=str(user.get("mid") or ""))

        nickname = str(user.get("nickname") or "这位朋友")

        bvid = extract_bvid(uri) or ""

        aid = int(item.get("subject_id") or 0)

        root_id = int(item.get("root_id") or item.get("target_id") or 0)

        parent_id = int(item.get("source_id") or root_id or 0)



        if not aid and bvid:

            try:

                meta_for_aid = await self.client.get_video_meta(bvid)

                aid = int(meta_for_aid.aid or 0)

            except Exception:

                pass

        if not bvid and not aid:

            self.log(f"跳过@通知：缺少视频信息 at_id={at_id}", "WARN")

            self.state.data["interactive_replied"][key] = "invalid"

            return False

        # 统一去重：动态扫描/评论通知路径可能已回复过同一条评论（各路径 key 互相独立会重复回复）。

        canon = self._comment_answered_key(aid or bvid, parent_id)

        if self._is_interactive_done(canon):

            self.state.data["interactive_replied"][key] = "sent"  # 已被其它路径回复，标记跳过

            self.state.save()

            return False

        if await self._send_admin_command_comment_reply(

            incoming=incoming, sender_uid=str(user.get("mid") or ""), key=key, canon=canon,

            oid=int(aid), root_id=int(root_id), parent_id=int(parent_id),

            resource_type=comment.CommentResourceType.VIDEO,

            channel_label="at-notice", max_len=int(cfg.get("reply_max_length") or REPLY_DEFAULT_MAX_LEN),

        ):

            return True

        # @ 路径也走统一入站过滤（长度/ignore_patterns/注入防护/悼念），与私信/评论一致。

        if incoming and not _valid_incoming_text(incoming, cfg):

            self.state.data["interactive_replied"][key] = "ignored"

            self.state.save()

            return False



        # 查看对方在@评论里发的图片（理解其内容后再回复）

        img_ctx = await self._comment_image_context(item, tag="@评论配图")

        if not incoming and not img_ctx:

            self.log("@通知缺少当前用户内容，暂不把根评论或视频标题当成用户消息", "WARN")

            return False



        # 观看视频（读取元数据 + 字幕 + 封面/关键帧视觉）。

        # 注意：对方发了图片时优先回应图片（跳过重视频分析，省钱省时间），只取视频标题做背景。

        video_item = VideoItem(bvid=bvid, aid=aid, title=str(item.get("title") or "视频"))

        try:

            if bvid:

                video_item = await self.client.get_video_meta(bvid)

            if img_ctx:

                analysis = None  # 有图：不跑完整视频分析，走聊天回复（带图片上下文）

            else:

                # 总超时保护：单视频分析最多 900 秒，超时返回空分析继续处理其他通知。

                # 注意 timeout 必须传给 wait_for，不能传进 analyze（analyze 无 timeout 形参）。

                analysis = await asyncio.wait_for(

                    self.media.analyze(self.client, video_item, rule={

                        "media_understanding": {

                            "enabled": True,

                            "use_subtitles": True,

                            "analyze_cover": True,

                            "analyze_images": True,

                            "extract_keyframes": True,

                            "keyframe_mode": "random",

                            "use_segment_download": True,

                            "max_keyframes": 3,

                            "segment_length_seconds": 4,

                            "download_max_height": 360,

                            "transcribe_audio": True,

                            "asr_backend": "faster_whisper",

                            "asr_model": "small",

                            "audio_char_budget": 1200,

                            "summarize": True,

                            "generate_comment": not needs_direct_reply,

                            "comment_style": f"回复@你的用户{nickname}。基于视频内容（标题/简介/字幕/画面/音频）给出具体、有画面感的观后评论，以Koishi自称，按主人指定的彼岸花/senya样本文风：引用标题/歌词/画面/原曲/数字等真实细节，连成氛围判断，自然不呆板，不空泛；不要广告、不要营销、不要AI标识。",

                            "comment_max_length": 260,

                        }

                    }),

                    timeout=900,

                )

        except asyncio.TimeoutError:

            self.log(f"@通知观看视频超时 {bvid}，跳过该通知", "WARN")

            analysis = None

        except Exception as exc:

            self.log(f"@通知观看视频失败 {bvid}：{exc}", "WARN")

            analysis = None



        # 生成回复并 @ 对方。

        reply = ""

        if analysis and analysis.comment_draft and not img_ctx and not needs_direct_reply:

            reply = analysis.comment_draft

        else:

            # 复用前面已拉取的 video_item（避免重复请求 B站接口）。

            vtitle = str(getattr(video_item, "title", "") or item.get("title") or "")

            source_ctx = build_comment_source_context(item, user.get("mid"), nickname)

            analysis_ctx = build_analysis_reply_context(analysis)

            reply = await self._generate_chat_reply(

                incoming_text=incoming or "（对方在评论区发送了图片）",

                context=(f"场景：B站评论区有人@了你。\n{source_ctx}"

                         f"视频标题：{vtitle[:220]}\n视频链接：{uri[:200]}\n{analysis_ctx}{img_ctx}"

                         f"要求：先回应本次用户消息；视频资料和引用仅补充背景。依据可靠资料回答问题或回应感受，"

                         f"图片有描述时回应图片；没有完整视频资料就不要声称看完。{self._catchup_context_note()}"),

                max_len=int(cfg.get("reply_max_length") or REPLY_DEFAULT_MAX_LEN),

                uid=str(user.get("mid") or ""),

                use_premium=True,

            )



        # 在回复里 @ 对方（昵称前加 @）。

        if nickname and f"@{nickname}" not in reply:

            reply = f"@{nickname} " + reply.lstrip()

        fake_item = VideoItem(bvid=bvid, aid=aid, title=str(item.get("title") or "视频"), desc=incoming)

        ok, reason, safe = self._finalize_reply(

            reply, channel="at", fake_item=fake_item, analysis=analysis,

            max_len=int(cfg.get("reply_max_length") or REPLY_DEFAULT_MAX_LEN), is_comment=True,

            preserve_quotes=bool(img_ctx) and infer_visual_task(incoming, "general") in {"ocr", "ui"},

        )

        if not ok:

            self.log(f"@回复被拦截 at_id={at_id}：{reason}", "WARN")

            self._mark_comment_draft_rejected(key, canon, reason)

            return False



        if self.config.dry_run:

            self.log(f"[DRY-RUN] 将@回复 aid={aid} root={root_id} parent={parent_id}：{safe}")

            return True

        else:

            # 发送前先原子占位；失败会改为 failed，后续可重试。

            if not self.state.claim_interactive([key, canon]):

                self.log(f"@回复占位失败，跳过重复回复 at_id={at_id}", "WARN")

                return False

            reply_claim = self.state.data["interactive_replied"][key]

            try:

                # B站子评论接口不支持带图，直接纯文字回复，避免无效尝试触发 -400。

                result, suspicious = await self.client.send_comment_reply_guarded(aid, root_id, parent_id, safe, preserve_quotes=bool(img_ctx) and infer_visual_task(incoming, "general") in {"ocr", "ui"})

                if suspicious:

                    self.log(f"@回复已被发送前安全预检拦截，未发送 aid={aid}：{safe[:80]}", "WARN")

                    self.state.data["interactive_replied"][key] = "blocked"

                    self.state.save()

                    return False

                self.log(f"已@回复 aid={aid} root={root_id} parent={parent_id}：{safe}", "SUCCESS")

            except Exception as exc:

                self.log(f"@回复失败 aid={aid}：{exc}", "ERROR")

                self._mark_reply_failed(key, canon, expected_claim=reply_claim)

                return False

            # 点赞对方评论

            if int(cfg.get("like_incoming_comment", True)) and parent_id:

                try:

                    await self.client.like_comment(aid, parent_id)

                    self.log(f"已点赞对方评论 aid={aid} rpid={parent_id}", "SUCCESS")

                except Exception as exc:

                    self.log(f"点赞对方评论失败 aid={aid} rpid={parent_id}：{exc}", "WARN")

            self.state.data["interactive_replied"][key] = "sent"

            self.state.data["interactive_replied"][canon] = "sent"



        self.state.record_history({

            "action": "at_reply" if not self.config.dry_run else "at_reply_dry_run",

            "at_id": at_id,

            "aid": aid,

            "bvid": bvid,

            "video_title": str(getattr(video_item, "title", "") or item.get("title") or ""),

            "root_id": root_id,

            "parent_id": parent_id,

            "rpid": parent_id,

            "author_mid": str(user.get("mid") or ""),

            "uid": str(user.get("mid") or ""),

            "nickname": nickname,

            "incoming": incoming[:500],

            "source_comment": incoming[:500],

            "root_context": str(item.get("root_reply_content") or item.get("target_reply_content") or "")[:500],

            "uri": uri,

            "reply": safe,

        })

        self.state.save()

        return True



    async def _handle_one_comment_notice(self, row: dict[str, Any], cfg: dict[str, Any]) -> bool:

        item = row.get("item") or {}

        from .at_video import has_pending_invitation

        if has_pending_invitation(self, item):

            return False  # @观看任务负责回复，普通评论通知避免抢先重复。

        user = row.get("user") or {}

        notice_id = str(row.get("id") or item.get("source_id") or item.get("target_id") or "")

        if not notice_id:

            return False

        key = f"comment_notice:{notice_id}"

        if self._uid_blocked(str(user.get("mid") or "")):

            self.state.data["interactive_replied"][key] = "blocked"

            self.state.save()

            return False

        if self._is_interactive_done(key):

            return False

        incoming = comment_incoming_text(item)

        if not incoming:

            if self._extract_comment_image_urls(item):

                incoming = "（对方在评论区发送了图片）"

            else:

                self.log("评论通知缺少当前用户内容，暂不把根评论或视频标题当成用户消息", "WARN")

                return False

        uri = str(item.get("uri") or item.get("native_uri") or "")

        self._learn_user_correction_if_any(incoming, source="comment-notice", video_id=extract_bvid(uri) or uri,
                                           sender_uid=str(user.get("mid") or ""))

        # 管理员公开指令允许包含链接/BV号，先不要套普通聊天入站过滤。

        # Avoid replying to old notification backlog unless configured otherwise.

        # 补回复模式：离线期间收到的评论回复不因“太旧”被跳过。

        min_time = int(cfg.get("min_reply_time", 0) or 0)

        reply_time = int(row.get("reply_time") or 0)

        if min_time and not self._in_catchup() and reply_time < min_time:

            self.state.data["interactive_replied"][key] = "old"

            self.state.save()

            return False

        # 动态评论通知也不再套用动态评论时间窗：只要有人发，就必须尝试回复。

        aid, root_id, parent_id = resolve_comment_position(item)

        uri = str(item.get("uri") or "")

        bvid = extract_bvid(uri) or ""

        if not aid and bvid:

            try:

                meta = await self.client.get_video_meta(bvid)

                aid = meta.aid

            except Exception:

                pass

        if not aid or not root_id:

            self.log(f"跳过评论通知：缺少 aid/root_id notice={notice_id}", "WARN")

            self.state.data["interactive_replied"][key] = "invalid"

            self.state.save()

            return False

        # 统一去重：动态扫描路径可能已回复过同一条评论（各路径 key 互相独立会重复回复）。

        canon = self._comment_answered_key(aid or bvid, parent_id)

        if self._is_interactive_done(canon):

            self.state.data["interactive_replied"][key] = "sent"  # 已被其它路径回复，标记跳过

            self.state.save()

            return False

        uri_low = str(uri or item.get("native_uri") or "").lower()

        business = str(item.get("business") or item.get("business_type") or row.get("business") or "").lower()

        is_dynamic_notice = (

            "dynamic" in uri_low or "t.bilibili.com" in uri_low or "动态" in str(item)

            or "dynamic" in business or (aid and not bvid and root_id and parent_id)

        )

        if await self._send_admin_command_comment_reply(

            incoming=incoming, sender_uid=str(user.get("mid") or ""), key=key, canon=canon,

            oid=int(aid), root_id=int(root_id), parent_id=int(parent_id),

            resource_type=comment.CommentResourceType.DYNAMIC_DRAW if is_dynamic_notice else comment.CommentResourceType.VIDEO,

            channel_label="comment-notice", max_len=int(cfg.get("reply_max_length") or REPLY_DEFAULT_MAX_LEN),

        ):

            return True

        if not _valid_incoming_text(incoming, cfg):

            self.state.data["interactive_replied"][key] = "ignored"

            self.state.save()

            return False

        nickname = str(user.get("nickname") or "路过的朋友")

        # 拉取视频元数据（标题/简介），让回复真正理解视频内容；同时写入审计历史。

        video_ctx = ""

        video_title = str(item.get("title") or "")

        video_desc = ""

        if bvid:

            try:

                meta = await self.client.get_video_meta(bvid)

                video_title = meta.title or video_title

                video_desc = meta.desc or ""

                video_ctx = f"视频标题：{video_title}\n视频简介：{video_desc[:300]}\n"

            except Exception as exc:

                self.log(f"评论通知视频元数据读取失败 notice={notice_id} bvid={bvid}：{exc}", "WARN")

        # 查看对方发在评论区的图片（理解其内容后再回复）

        img_ctx = await self._comment_image_context(item, tag="评论配图")

        source_ctx = build_comment_source_context(item, user.get("mid"), nickname)

        context = (

            f"场景：B站评论区有人和你对话/回复你。\n"

            f"{source_ctx}"

            f"视频链接：{uri}\n"

            f"{video_ctx}"

            f"{img_ctx}"

            f"要求：先回应本次用户消息，父评论/根评论是引用，不能当作对方自述；友善，不要营销，不要自称AI；"

            f"如果对方在聊这个视频，请基于上面的视频标题/简介顺着聊，抓住具体细节和氛围，不要答非所问、不要只短夸；"

            f"如果对方发了图片（有【评论配图】信息），回复里要自然地回应图片内容。"

            f"{self._catchup_context_note()}"

        )

        reply = await self._generate_chat_reply(incoming_text=incoming, context=context, max_len=int(cfg.get("reply_max_length") or REPLY_DEFAULT_MAX_LEN), uid=str(user.get("mid") or ""), use_premium=True)

        fake_item = VideoItem(bvid=bvid, aid=aid, title=str(item.get("title") or "评论区对话"), desc=incoming)

        ok, reason, safe = self._finalize_reply(

            reply, channel="reply", fake_item=fake_item,

            max_len=int(cfg.get("reply_max_length") or REPLY_DEFAULT_MAX_LEN), is_comment=True,

            preserve_quotes=bool(img_ctx) and infer_visual_task(incoming, "general") in {"ocr", "ui"},

        )

        if not ok:

            self.log(f"评论区自动回复被拦截 notice={notice_id}：{reason}", "WARN")

            self._mark_comment_draft_rejected(key, canon, reason)

            return False

        if self.config.dry_run:

            self.log(f"[DRY-RUN] 将回复评论 aid={aid} root={root_id} parent={parent_id}：{safe}")

            return True

        else:

            # 发送前先原子占位（跨路径/跨进程去重）；失败会改为 failed，后续可重试。

            if not self.state.claim_interactive([key, canon]):

                self.log(f"评论区回复占位失败，跳过重复回复 notice={notice_id}", "WARN")

                return False

            reply_claim = self.state.data["interactive_replied"][key]

            # 退化内容守卫：去掉表情代码与♪类符号后没有文字或数字，说明回复没有正文

            # （此前出现过只发“♬”、日志假成功但站上无回复的情况）。此时不标 sent、

            # 不发送，标 degraded 让下一轮可重试，而不是留下幽灵成功。

            try:

                import re as _re

                _clean = _re.sub(r"\[[^\]]*\]|[♪♬♩♫]", "", str(safe or ""))

                _clean = " ".join(_clean.split())

                if not any(ch.isalnum() for ch in _clean):

                    self.log(

                        f"评论区自动回复内容退化({len(_clean)}字)，未发送并留待重试 notice={notice_id}",

                        "WARN",

                    )

                    self.state.data["interactive_replied"][key] = "degraded"

                    self.state.save()

                    return False

            except Exception:

                pass

            # B站子评论接口不支持带图，直接纯文字回复，避免无效尝试触发 -400。

            pic_path = None

            # B站“回复我的”里，动态评论的 subject_id 实际是动态评论区 rid，不是视频 aid。

            # 以前先按 VIDEO 发，若接口返回不明确会被标 unknown，导致“动态后的回复的回复”没有真正评论。

            # 现在能判断为动态通知时直接用 DYNAMIC_DRAW 回复子评论。

            try:

                try:

                    if is_dynamic_notice:

                        await self.client.send_comment_with_optional_picture(

                            text=safe, oid=int(aid),

                            type_=comment.CommentResourceType.DYNAMIC_DRAW,

                            root=int(root_id), parent=int(parent_id),

                            pic_path=pic_path,

                        preserve_quotes=bool(img_ctx) and infer_visual_task(incoming, "general") in {"ocr", "ui"})

                        result, suspicious = None, False

                        self.log(f"已回复动态评论 aid={aid} root={root_id} parent={parent_id}：{safe}", "SUCCESS")

                    else:

                        result, suspicious = await self.client.send_comment_reply_guarded(aid, root_id, parent_id, safe, preserve_quotes=bool(img_ctx) and infer_visual_task(incoming, "general") in {"ocr", "ui"})

                except Exception as exc_pic:

                    # 动态 rid 的 12002 留给下方 DYNAMIC_DRAW 兜底。

                    if "12002" in str(exc_pic) or "评论功能已关闭" in str(exc_pic):

                        raise

                    raise

                if suspicious:

                    self.log(f"评论回复已被发送前安全预检拦截，未发送 aid={aid}：{safe[:80]}", "WARN")

                    self.state.data["interactive_replied"][key] = "blocked"

                    self.state.save()

                    return False

                self.log(f"已回复评论 aid={aid} root={root_id} parent={parent_id}：{safe}", "SUCCESS")

            except Exception as exc:

                # 12002 = 用视频评论 type 回复动态评论失败（aid 可能是动态 rid 而非视频 aid）。

                # 自动改用动态评论 type 重发一次，避免动态评论回复漏掉。

                if "12002" in str(exc) or "评论功能已关闭" in str(exc):

                    if not await self._allow_dynamic_comment_rid(aid):

                        self.state.data["interactive_replied"][key] = "old"

                        self.state.data["interactive_replied"][canon] = "old"

                        self.state.save()

                        return False

                    try:

                        try:

                            await self.client.send_comment_with_optional_picture(

                                text=safe, oid=int(aid),

                                type_=comment.CommentResourceType.DYNAMIC_DRAW,

                                root=int(root_id), parent=int(parent_id),

                                pic_path=pic_path,

                            preserve_quotes=bool(img_ctx) and infer_visual_task(incoming, "general") in {"ocr", "ui"})

                            if pic_path:

                                self.log(f"已回复动态评论 aid={aid} root={root_id} parent={parent_id}（附表情包 {pic_path.name}）：{safe}", "SUCCESS")

                            else:

                                self.log(f"已回复动态评论 aid={aid} root={root_id} parent={parent_id}：{safe}", "SUCCESS")

                        except Exception as exc_pic2:

                            if not pic_path:

                                raise

                            if isinstance(exc_pic2, OutboundDeliveryUnknown):

                                raise

                            self.log(f"动态评论表情包发送失败，降级纯文字 aid={aid} file={pic_path.name}：{exc_pic2}", "WARN")

                            await self.client.send_comment_with_optional_picture(

                                text=safe, oid=int(aid),

                                type_=comment.CommentResourceType.DYNAMIC_DRAW,

                                root=int(root_id), parent=int(parent_id),

                            preserve_quotes=bool(img_ctx) and infer_visual_task(incoming, "general") in {"ocr", "ui"})

                            self.log(f"已回复动态评论 aid={aid} root={root_id} parent={parent_id}：{safe}", "SUCCESS")

                        self.state.data["interactive_replied"][key] = "sent"

                        self.state.data["interactive_replied"][canon] = "sent"

                        self.state.save()

                        return True

                    except Exception as exc2:

                        self.log(

                            f"动态评论回复也失败 aid={aid} root={root_id} parent={parent_id} notice={notice_id} "

                            f"uid={str(user.get('mid') or '')} incoming={incoming[:120]!r} reply={safe[:120]!r} "

                            f"err={_format_exception_detail(exc2)}",

                            "ERROR",

                        )

                        self._mark_reply_failed(key, canon, expected_claim=reply_claim)

                else:

                    self.log(

                        f"评论回复失败 aid={aid} root={root_id} parent={parent_id} notice={notice_id} "

                        f"uid={str(user.get('mid') or '')} incoming={incoming[:120]!r} reply={safe[:120]!r} "

                        f"err={_format_exception_detail(exc)}",

                        "ERROR",

                    )

                    self._mark_reply_failed(key, canon, expected_claim=reply_claim)

                self.state.save()

                return False

            # 点赞对方评论（parent_id 是被回复的那条评论的 rpid）

            if int(cfg.get("like_incoming_comment", True)):

                try:

                    await self.client.like_comment(aid, parent_id)

                    self.log(f"已点赞对方评论 aid={aid} rpid={parent_id}", "SUCCESS")

                except Exception as exc:

                    self.log(f"点赞对方评论失败 aid={aid} rpid={parent_id}：{exc}", "WARN")

            self.state.data["interactive_replied"][key] = "sent"

            self.state.data["interactive_replied"][canon] = "sent"



        self.state.record_history({

            "action": "comment_auto_reply" if not self.config.dry_run else "comment_auto_reply_dry_run",

            "notice_id": notice_id,

            "aid": aid,

            "bvid": bvid,

            "video_title": video_title,

            "video_desc": video_desc[:500],

            "root_id": root_id,

            "parent_id": parent_id,

            "rpid": parent_id,

            "author_mid": str(user.get("mid") or ""),

            "uid": str(user.get("mid") or ""),

            "nickname": nickname,

            "incoming": incoming[:500],

            "source_comment": incoming[:500],

            "root_context": str(item.get("root_reply_content") or item.get("target_reply_content") or "")[:500],

            "uri": uri,

            "reply": safe,

        })

        self.state.save()

        return True



    async def _notify_model_switch(self, message: str) -> None:

        """模型切换时通知管理员。"""

        if not self.owner_uid:

            return

        name = self.owner_name or "管理员"

        try:

            from .safety import validate_message

            msg = f"诶～{name}，Koishi来报个信：{message}"

            ok, reason, safe = validate_message(msg, self.config.forbidden_words, marker="", require_marker=False)

            if ok and not self.config.dry_run:

                await self.client.send_private_text(self.owner_uid, safe)

                self.log(f"已通知 {self.owner_uid}（{name}）模型切换：{message}", "SUCCESS")

        except Exception as exc:

            self.log(f"通知管理员模型切换失败：{exc}", "WARN")



    async def _run_with_retry(self, action_name: str, coro_factory, attempts: int = 3) -> Any:

        """执行带重试的异步操作，确保主人指令尽量完成。"""

        last_err = None

        for i in range(1, attempts + 1):

            try:

                return await coro_factory()

            except OutboundDeliveryUnknown as exc:

                # 写接口可能已经成功但响应丢失；重试会把同一评论/私信

                # 再发一次。交给调用方记录 unknown，先人工核验。

                last_err = exc

                self.log(f"主人指令[{action_name}]结果未知，停止自动重试：{type(exc).__name__}", "ERROR")

                break

            except Exception as exc:

                last_err = exc

                self.log(f"主人指令[{action_name}]第{i}次失败：{exc}", "WARN")

                if i < attempts:

                    await asyncio.sleep(4 * i)

        raise last_err



    async def _notify_owner_failure(self, what: str, error: Any) -> None:

        """发送/执行失败时通知管理员（限频，避免刷屏）。"""

        if not self.owner_uid:

            return

        name = self.owner_name or "管理员"

        # 限频：同一类错误 10 分钟内只通知一次

        key = f"owner_fail_notify:{what}"

        last = self.state.data.setdefault("owner_notify_ts", {}).get(key)

        import time as _time

        now = _time.time()

        if last and now - float(last) < 600:

            return

        self.state.data["owner_notify_ts"][key] = str(now)

        try:

            from .safety import validate_message

            msg = f"诶～{name}，Koishi遇到点小状况：{what} 失败了（已自动重试），错误：{str(error)[:200]}"

            ok, reason, safe = validate_message(msg, self.config.forbidden_words, marker="", require_marker=False)

            if ok and not self.config.dry_run:

                await self.client.send_private_text(self.owner_uid, safe)

                self.log(f"已通知 {self.owner_uid}（{name}）：{what} 失败", "SUCCESS")

        except Exception as exc:

            self.log(f"通知管理员失败：{exc}", "WARN")



    def _admin_system_cfg(self) -> dict[str, Any]:

        cfg = self.config.data.setdefault("admin_system", {})

        if not isinstance(cfg, dict):

            cfg = {}

            self.config.data["admin_system"] = cfg

        cfg.setdefault("enabled", True)

        cfg.setdefault("super_admin_uid", self.owner_uid)

        cfg.setdefault("super_admin_name", self.owner_name or "管理员")

        cfg.setdefault("admins", [])

        cfg.setdefault("allow_public_commands", False)

        cfg.setdefault("super_only_actions", ["admin_add", "admin_remove"])

        return cfg



    @staticmethod

    def _admin_uid_text(uid: Any) -> str:

        return str(uid or "").strip()



    def _admin_level(self, uid: Any) -> str:

        """返回 super/admin/none。管理员(owner_uid) 永远是最高管理员。"""

        uid_s = self._admin_uid_text(uid)

        if not uid_s:

            return "none"

        acfg = self._admin_system_cfg()

        if not bool(acfg.get("enabled", True)):

            return "none"

        super_uids = {self._admin_uid_text(acfg.get("super_admin_uid")), self._admin_uid_text(self.owner_uid)}

        super_uids.discard("")

        if uid_s in super_uids:

            return "super"

        for row in acfg.get("admins") or []:

            if isinstance(row, dict):

                if self._admin_uid_text(row.get("uid")) == uid_s:

                    return "admin"

            elif self._admin_uid_text(row) == uid_s:

                return "admin"

        return "none"



    def _is_command_admin(self, uid: Any) -> bool:

        return self._admin_level(uid) in {"super", "admin"}



    def _admin_display_name(self, uid: Any) -> str:

        uid_s = self._admin_uid_text(uid)

        acfg = self._admin_system_cfg()

        if self._admin_level(uid_s) == "super":

            return str(acfg.get("super_admin_name") or self.owner_name or "管理员")

        for row in acfg.get("admins") or []:

            if isinstance(row, dict) and self._admin_uid_text(row.get("uid")) == uid_s:

                return str(row.get("name") or f"管理员{uid_s}")

        aliases = self.config.data.get("user_aliases") or {}

        return str(aliases.get(uid_s) or f"管理员{uid_s}")



    def _command_allowed_for_admin(self, action: str, uid: Any) -> tuple[bool, str]:

        level = self._admin_level(uid)

        if level == "super":

            return True, ""

        if level != "admin":

            return False, "只有被管理员授权为管理员的人才能执行指令。"

        acfg = self._admin_system_cfg()

        super_only = {str(x) for x in (acfg.get("super_only_actions") or ["admin_add", "admin_remove"])}

        # 添加/删除管理员永远只允许最高管理员，避免配置误改导致越权。

        super_only.update({"admin_add", "admin_remove"})

        if action in super_only:

            return False, "这个指令只有最高管理员管理员可以用。"

        allowed = acfg.get("admin_allowed_actions")

        if isinstance(allowed, list) and allowed:

            if action not in {str(x) for x in allowed}:

                return False, "这个指令没有开放给普通管理员。"

        return True, ""



    def _save_runtime_config(self) -> None:

        import json as _json

        self.config.path.write_text(_json.dumps(self.config.data, ensure_ascii=False, indent=2), encoding="utf-8")

        self.chat_cfg = self.config.data.get("interactive_chat") or {}



    @staticmethod

    def _config_nested_set(root: dict[str, Any], path: list[str], value: Any) -> None:

        cur = root

        for key in path[:-1]:

            nxt = cur.setdefault(key, {})

            if not isinstance(nxt, dict):

                nxt = {}

                cur[key] = nxt

            cur = nxt

        cur[path[-1]] = value



    @staticmethod

    def _config_nested_get(root: dict[str, Any], path: list[str], default: Any = None) -> Any:

        cur: Any = root

        for key in path:

            if not isinstance(cur, dict) or key not in cur:

                return default

            cur = cur.get(key)

        return cur



    def _feature_config_path(self, feature: str) -> tuple[list[str], str] | tuple[None, str]:

        table: dict[str, tuple[list[str], str]] = {

            "互动": (["interactive_chat", "enabled"], "互动聊天总开关"),

            "私信": (["interactive_chat", "private_messages", "enabled"], "私信自动回复"),

            "评论回复": (["interactive_chat", "comment_replies", "enabled"], "评论区回复"),

            "@回复": (["interactive_chat", "at_replies", "enabled"], "@通知回复"),

            "动态回复": (["interactive_chat", "dynamic_comments", "enabled"], "动态评论回复"),

            "早安": (["morning_greeting", "enabled"], "早安私信"),

            "晚安": (["nightly_greeting", "enabled"], "晚安私信"),

            "新粉": (["new_fan_greeting", "enabled"], "新粉感谢"),

            "心跳": (["heartbeat", "enabled"], "心跳报告"),

            "学习任务": (["learn_task", "enabled"], "自动学习任务"),

            "学习日报": (["learn_daily_report", "enabled"], "学习日报"),

            "wiki学习": (["wiki_learning", "enabled"], "东方Wiki学习"),

            "小站早晚安": (["station_greeting_posts", "enabled"], "小站早晚安帖子"),

        }

        return table.get(str(feature or "").strip(), (None, str(feature or "未知功能")))



    def _limit_config_path(self, name: str) -> tuple[list[str], str] | tuple[None, str]:

        table: dict[str, tuple[list[str], str]] = {

            "评论上限": (["daily_comment_limit"], "每日主动评论上限"),

            "分享上限": (["daily_share_limit"], "每日视频私信分享上限"),

            "私信每轮": (["interactive_chat", "private_messages", "max_replies_per_run"], "私信每轮回复上限"),

            "评论回复每轮": (["interactive_chat", "comment_replies", "max_replies_per_run"], "评论回复每轮上限"),

            "@回复每轮": (["interactive_chat", "at_replies", "max_replies_per_run"], "@回复每轮上限"),

            "动态回复每轮": (["interactive_chat", "dynamic_comments", "max_replies_per_run"], "动态回复每轮上限"),

        }

        return table.get(str(name or "").strip(), (None, str(name or "未知限额")))



    def _daily_status_text(self) -> str:

        items = [

            ("私信", ["interactive_chat", "private_messages", "enabled"]),

            ("评论回复", ["interactive_chat", "comment_replies", "enabled"]),

            ("@回复", ["interactive_chat", "at_replies", "enabled"]),

            ("动态回复", ["interactive_chat", "dynamic_comments", "enabled"]),

            ("早安", ["morning_greeting", "enabled"]),

            ("晚安", ["nightly_greeting", "enabled"]),

            ("新粉感谢", ["new_fan_greeting", "enabled"]),

            ("心跳", ["heartbeat", "enabled"]),

            ("学习任务", ["learn_task", "enabled"]),

            ("学习日报", ["learn_daily_report", "enabled"]),

            ("wiki学习", ["wiki_learning", "enabled"]),

            ("小站早晚安", ["station_greeting_posts", "enabled"]),

        ]

        lines = []

        for label, path in items:

            v = self._config_nested_get(self.config.data, path, False)

            lines.append(f"· {label}：{'开' if bool(v) else '关'}")

        lines.append(f"· 每日主动评论上限：{self.config.data.get('daily_comment_limit')}")

        lines.append(f"· 每日分享上限：{self.config.data.get('daily_share_limit')}")

        lines.append(f"· 私信每轮上限：{self._config_nested_get(self.config.data, ['interactive_chat', 'private_messages', 'max_replies_per_run'], '')}")

        return "\n".join(lines)



    async def _send_admin_command_comment_reply(

        self,

        *,

        incoming: str,

        sender_uid: str,

        key: str,

        canon: str | None,

        oid: int,

        root_id: int,

        parent_id: int,

        resource_type: CommentResourceType,

        channel_label: str,

        max_len: int = 900,

    ) -> bool:

        """在评论/@/动态评论里执行管理员指令并回一条确认。"""

        if not (self._is_command_admin(sender_uid) and is_command(incoming)):

            return False

        acfg = self._admin_system_cfg()

        if not bool(acfg.get("allow_public_commands", False)):

            self.state.data["interactive_replied"][key] = "ignored"

            self.state.save()

            return False

        claim_keys = [key] + ([canon] if canon else [])

        if not self.config.dry_run and not self.state.claim_interactive(claim_keys):

            self.log(f"管理员公开指令占位失败，跳过重复执行 key={key}", "WARN")

            return False

        try:

            reply = await self._handle_owner_command(incoming, sender_uid=sender_uid, source=channel_label)

            fake_item = VideoItem(bvid="", aid=int(oid or 0), title="管理员指令", desc=incoming)

            ok, reason, safe = self._finalize_reply(

                reply, channel="reply", fake_item=fake_item, max_len=max_len, is_comment=True,

            )

            if not ok:

                self.log(f"管理员公开指令回复被拦截 key={key}：{reason}", "WARN")

                self.state.data["interactive_replied"][key] = "blocked"

                if canon:

                    self.state.data["interactive_replied"][canon] = "blocked"

                self.state.save()

                return False

            if self.config.dry_run:

                self.log(f"[DRY-RUN] 管理员公开指令 {channel_label} uid={sender_uid}：{safe}", "INFO")

                return True

            await self.client.send_comment_with_optional_picture(

                text=safe, oid=int(oid), type_=resource_type,

                root=int(root_id), parent=int(parent_id), pic_path=None,

            )

            self.log(f"已执行管理员公开指令 {channel_label} uid={sender_uid} key={key}：{safe[:120]}", "SUCCESS")

            self.state.data["interactive_replied"][key] = "sent"

            if canon:

                self.state.data["interactive_replied"][canon] = "sent"

            self.state.record_history({"action": "admin_public_command", "uid": sender_uid, "source": channel_label, "command": incoming[:200], "reply": safe})

            self.state.save()

            return True

        except Exception as exc:

            self.log(f"管理员公开指令执行失败 key={key} uid={sender_uid}：{_format_exception_detail(exc)}", "ERROR")

            # 指令可能已经产生副作用，失败时也按 unknown 终态处理，避免公开评论重复执行。

            self.state.data["interactive_replied"][key] = "unknown"

            if canon:

                self.state.data["interactive_replied"][canon] = "unknown"

            self.state.save()

            return False



    async def _handle_owner_command(self, text: str, sender_uid: str | None = None, source: str = "dm") -> str:

        """执行管理员指令，返回回复文本。管理员(owner_uid) 永远拥有最高权限。"""

        sender_uid = str(sender_uid or self.owner_uid or "").strip()

        action, params = parse_command(text)

        level = self._admin_level(sender_uid)

        name = self._admin_display_name(sender_uid) if level != "none" else (self.owner_name or "管理员")

        if level == "none":

            return "这条是管理员指令，但你还没有被管理员授权哦。"

        allowed, deny_reason = self._command_allowed_for_admin(action, sender_uid)

        if not allowed:

            return f"{name}，{deny_reason}"

        try:

            if action == "help":

                return help_text(level)

            if action == "admin_whoami":

                role = "最高管理员" if level == "super" else "管理员"

                return f"{name}，你的权限是：{role}（UID {sender_uid}）。"

            if action == "admin_list":

                acfg = self._admin_system_cfg()

                admins = []

                for row in acfg.get("admins") or []:

                    if isinstance(row, dict):

                        uid = str(row.get("uid") or "").strip()

                        if uid:

                            admins.append(f"· {row.get('name') or '管理员'}：{uid}")

                    else:

                        uid = str(row or "").strip()

                        if uid:

                            admins.append(f"· 管理员：{uid}")

                body = "\n".join(admins) if admins else "· 暂无普通管理员"

                return f"最高管理员：{acfg.get('super_admin_name') or self.owner_name or '管理员'}（UID {acfg.get('super_admin_uid') or self.owner_uid}）\n{body}"

            if action == "admin_add":

                uid = str(params.get("uid") or "").strip()

                alias = str(params.get("name") or "").strip()

                if not uid.isdigit():

                    return f"{name}，UID 格式不对。"

                if self._admin_level(uid) == "super":

                    return f"{name}，UID {uid} 是最高管理员，不需要重复添加。"

                acfg = self._admin_system_cfg()

                admins = [x for x in (acfg.get("admins") or []) if not (isinstance(x, dict) and str(x.get("uid") or "") == uid) and not (not isinstance(x, dict) and str(x) == uid)]

                admins.append({"uid": uid, "name": alias or (self.config.data.get("user_aliases") or {}).get(uid, ""), "added_by": sender_uid, "added_at": now_iso()})

                acfg["admins"] = admins

                self._save_runtime_config()

                return f"{name}，已把 UID {uid}{'（' + alias + '）' if alias else ''} 设为管理员。"

            if action == "admin_remove":

                uid = str(params.get("uid") or "").strip()

                if not uid.isdigit():

                    return f"{name}，UID 格式不对。"

                if self._admin_level(uid) == "super":

                    return f"{name}，最高管理员不能被删除。"

                acfg = self._admin_system_cfg()

                before = len(acfg.get("admins") or [])

                acfg["admins"] = [x for x in (acfg.get("admins") or []) if not (isinstance(x, dict) and str(x.get("uid") or "") == uid) and not (not isinstance(x, dict) and str(x) == uid)]

                self._save_runtime_config()

                return f"{name}，已移除 UID {uid} 的管理员权限。" if len(acfg.get("admins") or []) != before else f"{name}，UID {uid} 本来就不是管理员。"

            if action == "daily_status":

                return f"{name}，当前日常功能状态：\n{self._daily_status_text()}"

            if action == "set_feature":

                path, label = self._feature_config_path(str(params.get("feature") or ""))

                if not path:

                    return f"{name}，没找到这个功能开关：{params.get('feature')}"

                enabled = bool(params.get("enabled"))

                self._config_nested_set(self.config.data, path, enabled)

                self._save_runtime_config()

                extra = "互动聊天相关开关已在当前 chat-run 生效；主进程定时任务开关通常需要下次重启主进程后完全生效。"

                return f"{name}，已{'开启' if enabled else '关闭'}{label}。{extra}"

            if action == "set_limit":

                path, label = self._limit_config_path(str(params.get("limit") or ""))

                if not path:

                    return f"{name}，没找到这个限额：{params.get('limit')}"

                value = max(0, min(1000, int(params.get("value") or 0)))

                self._config_nested_set(self.config.data, path, value)

                if str(params.get("limit")) == "私信每轮":

                    self._config_nested_set(self.config.data, ["interactive_chat", "private_messages", "guarantee_min_replies_per_run"], value)

                self._save_runtime_config()

                return f"{name}，已把{label}设置为 {value}。"

            if action == "recent_history":

                n = max(1, min(30, int(params.get("limit") or 8)))

                rows = list(self.state.data.get("history") or [])[-n:]

                if not rows:

                    return f"{name}，最近还没有记录。"

                lines = []

                for row in rows[-n:]:

                    if not isinstance(row, dict):

                        continue

                    lines.append(f"· {row.get('time','')} {row.get('action','记录')}")

                return f"{name}，最近 {len(lines)} 条动作：\n" + "\n".join(lines[-n:])

            if action == "remember_note":

                note = str(params.get("text") or "").strip()

                if len(note) < 2:

                    return f"{name}，要记住的内容太短啦。"

                item = self.learn_db.add(

                    title=f"管理员记录：{note[:40]}", content=note, category="meme",

                    source=f"admin-command:{sender_uid}", tags=["管理员记录", "B站指令"], official=False, confidence="high",

                )

                self.knowledge_text = (self.knowledge_text + "\n\n【管理员刚记录】\n" + note).strip()

                return f"{name}，已经记住啦（记录ID {item.get('id')}）。"

            if action == "send_dm_text":

                uid = str(params.get("uid") or "").strip()

                body = str(params.get("text") or "").strip()

                if not uid.isdigit() or uid == str(self.client.uid or ""):

                    return f"{name}，收件人 UID 不对。"

                if self._uid_blocked(uid):

                    return f"{name}，UID {uid} 在私信拒收名单里，未发送。"

                ok, reason, safe = validate_message(body, self.config.forbidden_words, marker=self.config.ai_marker, require_marker=False)

                if not ok:

                    return f"{name}，这条私信被安全规则拦住了：{reason}"

                if self.config.dry_run:

                    return f"{name}，[预演] 将私信 UID {uid}：{safe[:200]}"

                try:

                    await self._send_private_text_guarded(uid, safe)

                except OutboundDeliveryUnknown:

                    return f"{name}，私信发送结果未知，已停止自动重试；请人工确认 UID {uid} 是否收到。"

                self.state.record_history({"action": "admin_send_dm", "uid": sender_uid, "to": uid, "text": safe[:200]})

                self.state.save()

                return f"{name}，已给 UID {uid} 发出私信。"

            if action == "status":

                from .health import HealthMonitor

                mon = HealthMonitor(self.config)

                try:

                    r = await mon.check_once()

                    return (

                        f"{name}，运行状态如下：\n"

                        f"登录={'正常' if r.get('login') else '异常'}\n"

                        f"评论接口={'正常' if r.get('comment') else '异常'}\n"

                        f"私信接口={'正常' if r.get('private') else '异常'}\n"

                        f"AI文本={'正常' if r.get('ai_text') else '异常'}\n"

                        f"AI视觉={'正常' if r.get('ai_vision') else '异常'}"

                    )

                finally:

                    await mon.close()

            if action == "cost":

                try:

                    from .cost import cost_from_usage, usage_text

                    usage = self.ai.usage_summary()

                    if not usage:

                        return f"{name}，目前还没有AI消耗记录（可能还没调用过）。"

                    text = usage_text(usage)

                    total = cost_from_usage(usage)

                    return f"{name}，AI消耗统计（本进程累计）：\n{text}\n估算总费用约 ¥{total:.4f}"

                except Exception as exc:

                    return f"{name}，查询消费失败：{exc}"

            if action == "learn_view":

                try:

                    from .touhou_learning import TouhouLearningDB

                    learn_cfg = self.config.data.get("touhou_learning") or {}

                    db = TouhouLearningDB(

                        path=str(learn_cfg.get("memory_file") or "data/touhou_learning.json"),

                        max_entries=int(learn_cfg.get("max_entries", 2000) or 2000),

                    )

                    summary = db.summary(limit=10)

                    if not summary.strip():

                        return f"{name}，学习库还是空的呢，让Koishi再多逛逛～"

                    return f"{name}，这是Koishi最近学到的一些：\n{summary[:800]}"

                except Exception as exc:

                    return f"{name}，查看学习库失败：{exc}"

            if action == "like":

                item = await self._run_with_retry("获取视频信息", lambda: self.client.get_video_meta(params["bvid"]))

                await self._run_with_retry("点赞", lambda: self.client.like_video(item))

                return f"{name}，已经点赞啦：{item.title[:40]}"

            if action == "favorite":

                from datetime import datetime

                item = await self._run_with_retry("获取视频信息", lambda: self.client.get_video_meta(params["bvid"]))

                folder = datetime.now().strftime("%Y年%m月%d日")

                await self._run_with_retry("收藏", lambda: self.client.favorite_video(item, folder))

                return f"{name}，已经收藏到「{folder}」：{item.title[:40]}"

            if action in ("deep_comment", "vision_comment"):

                # 管理员显式要求深度/视觉：深度评论临时用最强模型 gpt-5.5，完成后恢复 gpt-5.4 省钱模式。

                bvid = params["bvid"]

                item = await self._run_with_retry("获取视频信息", lambda: self.client.get_video_meta(bvid))

                old_premium = self.ai.premium_text_model

                old_vision = self.ai.vision_enabled  # 保存原值，结束时恢复，避免永久关闭视觉

                use_vision = action == "vision_comment"

                if use_vision:

                    self.ai.vision_enabled = True

                if action == "deep_comment":

                    self.ai.premium_text_model = "gpt-5.5"  # 手动深度 → 最强模型

                    self.log("管理员指令：深度评论临时使用 gpt-5.5（最强）", "INFO")

                rule = {

                    "media_understanding": {

                        "enabled": True, "use_subtitles": True,

                        "analyze_cover": True, "analyze_images": use_vision,

                        "extract_keyframes": True,

                        "keyframe_mode": "random", "use_segment_download": True,

                        "max_keyframes": 2, "segment_length_seconds": 4, "download_max_height": 360,

                        "transcribe_audio": True, "asr_backend": "faster_whisper", "asr_model": "small",

                        "audio_char_budget": 1200,

                        "summarize": True, "generate_comment": True,

                        "comment_style": "以Koishi自称，按主人指定的彼岸花/senya样本文风：引用标题/歌词/画面/原曲/数字等真实细节，连成氛围判断，自然、有镜头感，不呆板不空泛。",

                        "comment_max_length": 260,

                    }

                }

                try:

                    analysis = await self.media.analyze(self.client, item, rule=rule)

                finally:

                    self.ai.vision_enabled = old_vision  # 恢复原值，不永久关闭视觉

                    self.ai.premium_text_model = old_premium  # 恢复 gpt-5.4

                comment = analysis.comment_draft or f"诶～Koishi看完这个视频啦，挺有意思的。"

                comment = self.emotes.decorate_comment(comment, item, analysis, max_len=300)

                comment = self._apply_morph(comment)

                ok, reason, safe = validate_comment(comment, self.config.forbidden_words, marker=self.config.ai_marker, require_marker=self.config.require_ai_marker)

                if not ok:

                    return f"{name}，评论被拦住了：{reason}"

                if self.config.dry_run:

                    return f"{name}，[预演] 将评论：{safe}"

                result, suspicious = await self._run_with_retry("评论发送", lambda: self.client.send_video_comment_guarded(item, safe))

                await self._run_with_retry("点赞", lambda: self.client.like_video(item))

                from datetime import datetime

                folder = datetime.now().strftime("%Y年%m月%d日")

                await self._run_with_retry("收藏", lambda: self.client.favorite_video(item, folder))

                mode = "深度推理" if action == "deep_comment" else "视觉分析"

                if suspicious:

                    return f"{name}，评论内容被发送前安全预检拦截，未发送：{item.title[:40]}"

                return f"{name}，已用{mode}评论+点赞+收藏：{item.title[:40]}"

            if action == "comment":

                item = await self.client.get_video_meta(params["bvid"])

                analysis = await self.media.analyze(self.client, item, rule={

                    "media_understanding": {

                        "enabled": True,

                        "use_subtitles": True,

                        "analyze_cover": True,

                        "analyze_images": True,

                        "extract_keyframes": True,

                        "keyframe_mode": "random",

                        "use_segment_download": True,

                        "max_keyframes": 3,

                        "segment_length_seconds": 4,

                        "download_max_height": 360,

                        "transcribe_audio": True,

                        "asr_backend": "faster_whisper",

                        "asr_model": "small",

                        "summarize": True,

                        "generate_comment": True,

                        "comment_style": "以Koishi自称，按主人指定的彼岸花/senya样本文风：引用标题/歌词/画面/原曲/数字等真实细节，连成氛围判断，自然、有镜头感，不呆板不空泛。",

                        "comment_max_length": 260,

                    }

                })

                comment = analysis.comment_draft or f"诶～Koishi看完这个视频啦，挺有意思的。"

                comment = self.emotes.decorate_comment(comment, item, analysis, max_len=300)

                comment = self._apply_morph(comment)

                ok, reason, safe = validate_comment(comment, self.config.forbidden_words, marker=self.config.ai_marker, require_marker=self.config.require_ai_marker)

                if not ok:

                    return f"{name}，评论被拦住了：{reason}"

                if self.config.dry_run:

                    return f"{name}，[预演] 将评论：{safe}"

                result, suspicious = await self._run_with_retry("评论发送", lambda: self.client.send_video_comment_guarded(item, safe))

                await self._run_with_retry("点赞", lambda: self.client.like_video(item))

                from datetime import datetime

                folder = datetime.now().strftime("%Y年%m月%d日")

                await self._run_with_retry("收藏", lambda: self.client.favorite_video(item, folder))

                if suspicious:

                    return f"{name}，评论内容被发送前安全预检拦截，未发送：{item.title[:40]}"

                return f"{name}，已评论+点赞+收藏：{item.title[:40]}"

            if action == "search_comment":

                kw = params["keyword"]

                found = await self.client.search_videos(kw, max_results=5, order="pubdate")

                # 跳过去世/悼念相关视频

                item = None

                for cand in found:

                    hay = f"{cand.title} {cand.desc} {cand.author}"

                    if not _is_bereavement(hay):

                        item = cand

                        break

                if item is None:

                    return f"{name}，没搜到「{kw}」相关的正常视频哦（已过滤去世/悼念内容）。"

                analysis = await self.media.analyze(self.client, item, rule={

                    "media_understanding": {

                        "enabled": True, "use_subtitles": True, "analyze_cover": True, "analyze_images": True,

                        "extract_keyframes": True, "keyframe_mode": "random", "use_segment_download": True,

                        "max_keyframes": 3, "segment_length_seconds": 4, "download_max_height": 360,

                        "transcribe_audio": True, "asr_backend": "faster_whisper", "asr_model": "small",

                        "summarize": True, "generate_comment": True,

                        "comment_style": "以Koishi自称，按主人指定的彼岸花/senya样本文风：引用标题/歌词/画面/原曲/数字等真实细节，连成氛围判断，自然、有镜头感，不呆板不空泛。",

                        "comment_max_length": 260,

                    }

                })

                comment = analysis.comment_draft or f"诶～Koishi看完「{kw}」相关视频啦，挺有意思的。"

                comment = self.emotes.decorate_comment(comment, item, analysis, max_len=300)

                comment = self._apply_morph(comment)

                ok, reason, safe = validate_comment(comment, self.config.forbidden_words, marker=self.config.ai_marker, require_marker=self.config.require_ai_marker)

                if not ok:

                    return f"{name}，评论被拦住了：{reason}"

                if self.config.dry_run:

                    return f"{name}，[预演] 搜到 {item.bvid}《{item.title[:40]}》，将评论：{safe}"

                result, suspicious = await self._run_with_retry("评论发送", lambda: self.client.send_video_comment_guarded(item, safe))

                await self._run_with_retry("点赞", lambda: self.client.like_video(item))

                from datetime import datetime

                folder = datetime.now().strftime("%Y年%m月%d日")

                await self._run_with_retry("收藏", lambda: self.client.favorite_video(item, folder))

                if suspicious:

                    return f"{name}，评论内容被发送前安全预检拦截，未发送：{item.bvid}《{item.title[:40]}》"

                return f"{name}，已评论+点赞+收藏：{item.bvid}《{item.title[:40]}》"

            if action == "share":

                item = await self._run_with_retry("获取视频信息", lambda: self.client.get_video_meta(params["bvid"]))

                uid = params.get("uid") or self.owner_uid

                uid = str(uid or "").strip()

                if not uid.isdigit() or not (1 <= len(uid) <= 18):

                    return f"{name}，收件人 UID 格式不对，暂不发送。"

                if uid == str(self.client.uid or ""):

                    return f"{name}，不能把视频私信给自己。"

                if self._uid_blocked(uid):

                    return f"{name}，UID {uid} 在私信拒收名单里，未分享。"

                video_guard = self.touhou.assess_video(item)

                if not video_guard.ok:

                    return f"{name}，这个视频未通过东方内容合规检查：{video_guard.reason}"

                text = f"诶～{name}让我分享一个视频给你：{item.title}\n{item.url}"

                text = self.touhou.prepare_share_text(text, item)

                text = self.emotes.decorate_message(text, item, None, max_len=900)

                text = self._apply_morph(text)

                text_guard = self.touhou.review_outbound_text(text, item)

                if not text_guard.ok:

                    return f"{name}，分享私信未通过东方内容合规检查：{text_guard.reason}"

                ok, reason, safe = validate_message(

                    text_guard.text,

                    self.config.forbidden_words,

                    marker=self.config.ai_marker,

                    require_marker=self.config.require_ai_marker,

                )

                if not ok:

                    return f"{name}，分享私信被安全规则拦截：{reason}"

                if self.config.dry_run:

                    return f"{name}，[预演] 将把 {item.bvid}《{item.title[:40]}》发给 UID {uid}：{safe}"

                rule_name = "owner_command_share"

                reserved, reserve_reason = self.state.reserve_video_share(

                    rule_name, uid, item.bvid, self.config.daily_share_limit

                )

                if not reserved:

                    return f"{name}，这次分享没有执行：{reserve_reason}"

                try:

                    # 私信结果未知时不能由主人指令层重试；pending 状态保留，

                    # 先人工确认 B 站是否已收到，避免重复外发。

                    result = await self._send_private_text_guarded(uid, safe)

                except OutboundDeliveryUnknown as exc:

                    self.state.record_history({

                        "action": "owner_share_delivery_unknown",

                        "to": uid,

                        "bvid": item.bvid,

                    })

                    self.state.save()

                    return f"{name}，分享请求结果未知，已停止自动重试；请先人工确认 UID {uid} 是否收到。"

                except Exception as exc:

                    self.state.cancel_video_share_reservation(rule_name, uid, item.bvid)

                    self.log(f"主人指令分享失败 to={uid} bvid={item.bvid}：{type(exc).__name__}", "ERROR")

                    return f"{name}，分享发送失败（{type(exc).__name__}），未自动重试。"

                self.state.finish_video_share(rule_name, uid, item.bvid)

                self.state.record_history({

                    "action": "owner_share_sent",

                    "to": uid,

                    "bvid": item.bvid,

                    "result": str(result)[:200],

                })

                self.state.save()

                return f"{name}，已把 {item.bvid}《{item.title[:40]}》发给 UID {uid}。"

            if action in {"block", "unblock"}:

                uid = str(params.get("uid") or "").strip()

                if not uid or not uid.isdigit():

                    return f"{name}，{action} 需要带上 UID，比如：拉黑 123456"

                pm = self.config.data.setdefault("interactive_chat", {}).setdefault("private_messages", {})

                blocked = [str(x) for x in (pm.get("blocked_uids") or []) if str(x).strip()]

                import json as _json

                if action == "block":

                    if uid not in blocked:

                        blocked.append(uid)

                        pm["blocked_uids"] = blocked

                    # 该 UID 所有待处理私信标记 blocked，停止继续重试

                    rep = self.state.data.setdefault("interactive_replied", {})

                    n = 0

                    for k in list(rep):

                        if str(k).startswith(f"dm:{uid}:") and rep[k] not in {"sent", "blocked"}:

                            rep[k] = "blocked"

                            n += 1

                    self.state.data.setdefault("dm_fail_cooldown", {}).pop(uid, None)

                    self.state.data.setdefault("dm_fail_strikes", {}).pop(uid, None)

                    try:

                        self.config.path.write_text(_json.dumps(self.config.data, ensure_ascii=False, indent=2), encoding="utf-8")

                    except Exception as exc:

                        self.log(f"保存拉黑配置失败：{exc}", "WARN")

                    self.state.save()

                    return f"{name}，已把 UID {uid} 拉黑：不再回复它的私信（{n} 条待回消息已跳过）。想恢复就对我说「解除 {uid}」。"

                # unblock

                if uid in blocked:

                    pm["blocked_uids"] = [x for x in blocked if x != uid]

                    try:

                        self.config.path.write_text(_json.dumps(self.config.data, ensure_ascii=False, indent=2), encoding="utf-8")

                    except Exception as exc:

                        self.log(f"保存解除拉黑配置失败：{exc}", "WARN")

                    self.log(f"已解除拉黑 UID {uid}，后续新私信会正常回复", "INFO")

                    return f"{name}，已解除 UID {uid} 的拉黑，之后它发来的新消息会正常回复。"

                return f"{name}，UID {uid} 本来就不在黑名单里。"

            return unknown_tip()

        except Exception as exc:

            self.log(f"执行主人指令失败：{exc}", "ERROR")

            return f"{name}，执行指令时出了点问题：{exc}"



    def _needs_knowledge_assist(self, incoming_text: str, context: str = "") -> bool:

        """轻量判断是否需要先查资料：只在不懂/提问/东方设定梗时触发，避免日常闲聊额外耗时。"""

        text = f"{incoming_text}\n{context[:300]}".lower()

        if not incoming_text or len(incoming_text.strip()) < 2:

            return False

        if _looks_like_question(incoming_text):

            return True

        triggers = [

            "不懂", "看不懂", "什么意思", "什么梗", "出处", "设定", "剧情", "故事", "世界观", "正作", "原作",

            "th", "touhou", "东方", "幻想乡", "红魔乡", "妖妖梦", "永夜抄", "风神录", "地灵殿", "星莲船",

            "神灵庙", "辉针城", "绀珠传", "天空璋", "鬼形兽", "虹龙洞", "兽王园", "锦上京", "黑市", "凭依华", "深秘录", "刚欲异闻", "文花帖", "妖精大战争", "月都", "畜生界", "地底", "旧地狱", "永远亭", "红魔馆", "白玉楼", "命莲寺", "神灵庙", "守矢", "秘封", "人间之里", "博丽神社", "人物关系", "阵营", "官设", "二创", "模因", "同人",

        ]

        return any(k in text for k in triggers)



    def _full_knowledge_source(self) -> str:

        """按配置直接读全部知识文件内容，避免只检索被截断的 self.knowledge_text 前缀。"""

        texts: list[str] = []

        for path in (self.config.data.get("touhou_knowledge") or {}).get("knowledge_files") or []:

            p = resolve_path(str(path))

            if not p.exists() or not p.is_file():

                continue

            try:

                t = p.read_text(encoding="utf-8").strip()

            except Exception:

                continue

            if t:

                texts.append(f"【知识文件：{p.name}】\n{t}")

        joined = "\n\n".join(texts).strip()

        return joined or (self.knowledge_text or "")



    def _extract_relevant_knowledge_lines(self, query: str, max_chars: int = 1600) -> str:

        """从本地知识文件全文里用关键词抽取相关行；免费、无网络、无 token。"""

        src = self._full_knowledge_source()

        if not src.strip():

            return ""

        raw_keys = re.findall(r"[A-Za-z0-9_.-]+|[\u4e00-\u9fff]{2,}", query or "")

        # 中文整句会被正则抓成很长的连续块；补充二字滑窗，才能命中“正邪/针妙丸/阿梨夜/丰姬”等短词。

        cjk_chunks = re.findall(r"[\u4e00-\u9fff]{4,}", query or "")

        cjk_pairs: list[str] = []

        for chunk in cjk_chunks:

            cjk_pairs.extend(chunk[i:i+2] for i in range(0, len(chunk) - 1))

        extra = ["东方", "幻想乡", "正作", "原作", "故事", "设定", "剧情", "世界观"]

        # 泛词只作"兜底"，不再无条件加入检索词。

        # 2026-09-12 实测：无条件加入会让检索**永远命中** —— "晚安呀，今天也辛苦啦"

        # 也会命中 9 行纯噪声（帽子先生设定、英语能力、二创尺度等），因为这些行里

        # 含"东方/原作/设定"。而知识库满篇都是这些词。

        # 改成"查询自身没有可用实体词时才用泛词"后：闲聊命中 0 行、设定题命中 2,578 字符。

        stop = {"什么", "怎么", "为什么", "是否", "可以", "这个", "那个", "如何", "意思",

                "我们", "你们", "他们", "一个", "是不是", "有没有", "到底", "的吗", "的话"}

        specific_keys: list[str] = []

        for k in raw_keys + cjk_pairs:

            k = str(k).strip().lower()

            if len(k) >= 2 and k not in stop and k not in specific_keys:

                specific_keys.append(k)

        keys = specific_keys if specific_keys else list(extra)

        # 角色名优先：查询里出现的规范名/别名是最强实体信号。

        # 中文整句会被上面的正则切成长块，3 字名（"魔理沙"）只剩 2 字滑窗（"魔理"），

        # 结果提问对象根本没进检索词——"以雾雨魔理沙为对象检验"会检索到别人的行（实测 B 区失分根因）。

        char_keys: list[str] = []

        try:

            qlow = str(query or "").lower()

            for canon, alist in self.media.character_recognizer.aliases().items():

                for a in [canon] + list(alist):

                    a = str(a or "").strip()

                    if len(a) >= 2 and a.lower() in qlow and a not in char_keys:

                        char_keys.append(a)

            # 长的先匹配（"雾雨魔理沙" 优先于 "魔理沙"）

            char_keys.sort(key=len, reverse=True)

        except Exception:

            char_keys = []

        lines_cache = [ln.strip() for ln in src.splitlines()]

        generic = {k.lower() for k in extra}

        # 词频只用于“降权”而不是删词：高频角色名（灵梦/妖梦）依然重要，

        # 但区分度低于“哪一作/第一次/翅膀”这类具体词。

        key_freq = {k: 0 for k in keys}

        for ln in lines_cache:

            low_ln = ln.lower()

            for k in keys:

                if k in low_ln:

                    key_freq[k] += 1

        def _weight(k: str) -> float:

            base = 1.0 if k in generic else (6.0 if len(k) >= 4 else (4.0 if len(k) == 3 else 3.0))

            f = key_freq.get(k, 0)

            idf = 1.0 if f <= 15 else max(0.12, 15.0 / f)

            return base * idf

        # 查询里的“首个具体词”通常就是提问对象（妖梦/灵梦/芙兰），命中它的行优先。

        # 注意：raw_keys 会把整句中文当成一个长块，这里只从 2~4 字、且在语料里真实出现过的词里挑。

        primary = ""

        for k in keys:

            if k in generic or len(k) > 4:

                continue

            if key_freq.get(k, 0) >= 3:

                primary = k

                break

        # 问题类型词：问“能力/关系/翅膀/登场/梗”时，同行出现该类型词的才最可能是答案行

        type_words = ["能力", "关系", "首登场", "登场", "翅膀", "翼", "识别", "fumo", "毛绒",

                      "梗", "意思", "区别", "边界", "种族", "地点", "组织", "外观", "特征",

                      "生日", "称呼", "颜色", "发色", "服装", "道具", "身份"]

        query_types = [w for w in type_words if w in (query or "")]

        # 最低分阈值（可用 config 的 knowledge_assist.min_score 调整）

        try:

            _ka_cfg = self.chat_cfg.get("knowledge_assist") or self.config.data.get("knowledge_assist") or {}

            _min_score = float(_ka_cfg.get("min_score", 8.0) or 8.0)

        except Exception:

            _min_score = 8.0

        scored: list[tuple[int, int, str]] = []

        for i, line in enumerate(lines_cache):

            if not line or len(line) > 240:

                continue

            # 打分时去掉“来源：xxx.md”尾部，避免文件名里的泛词再次污染

            body = line.split("｜ 来源：", 1)[0]

            low = body.lower()

            hits = [k for k in keys if k in low]

            if not hits:

                continue

            score = sum(_weight(k) for k in hits)

            if char_keys:

                char_hit = [c for c in char_keys if c.lower() in low]

                if char_hit:

                    # 命中提问对象（角色名/别名）的行大幅优先，长的名字给更高权

                    score += 10.0 + 3.0 * len(char_hit[0])

                    # 名字出现在行首（表格首列 / 问答卡标题）说明这行就是"写这个角色"的

                    if low.strip().lstrip("|- *#").startswith(char_hit[0].lower()):

                        score += 8.0

                else:

                    # 与提问对象无关的行降权：避免"以魔理沙为对象"却检索出灵梦的行。

                    # 注意别压太狠（曾用 -8，导致个别题不再带出"通用口径说明"行、反而过度断言）。

                    score -= 3.0

            if primary and primary in low:

                score += 3.0

            if query_types and any(w in body for w in query_types):

                score += 4.0

            # 多词共现（尤其是具体词）更能说明命中：+2/+4 加成

            specific = [k for k in hits if k not in generic]

            if len(specific) >= 2:

                score += 2

            if len(specific) >= 3:

                score += 4

            if line.startswith("- **") or line.startswith("##"):

                score += 1

            # 最低分阈值：滤掉"只命中了查询里一个常见二字词"的弱相关行。

            # 2026-09-12 实测：不加阈值时"今天天气不错"仍会命中 1,157 字符噪声

            # （靠"今天/不错"这类二字窗匹配上知识库里的长句）。

            # 角色名命中会 +10 以上，因此真实实体提问不受影响。

            if score < _min_score:

                continue

            scored.append((int(score * 10), i, line))

        scored.sort(key=lambda x: (-x[0], x[1]))

        out: list[str] = []

        total = 0

        seen: set[str] = set()

        for _, _, line in scored[:40]:

            if line in seen:

                continue

            seen.add(line)

            add = line + "\n"

            if total + len(add) > max_chars:

                break

            out.append(line)

            total += len(add)

        return "\n".join(out)



    async def _knowledge_assist_context(self, incoming_text: str, context: str = "") -> str:

        """先免费检索/压缩资料，再交给 GPT 整合。



        2026-09-12 调整：本地检索改为**每次都跑**。

        - 本地逐行匹配 + 学习库召回是纯 CPU、无网络、零成本；

        - 修好选择性后（泛词改兜底），闲聊类消息命中为 0 行 → 直接返回，

          既不注入噪声也不产生延迟；

        - 只有"关键词闸门放行"的情况才额外走一次免费模型压缩（那一步有网络往返，

          所以仍然只在需要时做）；未放行但有命中时，直接注入原始行，省掉这一次往返。

        """

        cfg = (self.chat_cfg.get("knowledge_assist") or self.config.data.get("knowledge_assist") or {})

        if cfg and not bool(cfg.get("enabled", True)):

            return ""

        query = f"{incoming_text}\n{context[:500]}"

        pieces: list[str] = []

        local = self._extract_relevant_knowledge_lines(query, max_chars=int(cfg.get("local_max_chars", 1600) or 1600))

        if local:

            pieces.append("【本地知识命中】\n" + local)

        try:

            learned = self.learn_db.context_for(query, limit=int(cfg.get("learn_limit", 5) or 5), max_chars=int(cfg.get("learn_max_chars", 1200) or 1200))

            if learned:

                pieces.append("【长期学习库命中】\n" + learned)

        except Exception:

            pass

        raw = "\n\n".join(pieces).strip()

        if not raw:

            # 没命中任何资料：闲聊类走到这里就结束（零成本、零延迟）

            return ""

        _maxc = int(cfg.get("max_chars", 900) or 900)

        _gated = self._needs_knowledge_assist(incoming_text, context)

        # 命中量太少 → 不注入，避免给闲聊塞无关资料。

        # 2026-09-12 实测：分数阈值分不开"闲聊噪声"与"未登记角色名"（两者都约 3.0 分），

        # 改用"命中量"这一维度，且**对是否过闸门都生效** ——

        # 因为闸门会误判（"在做什么呢"含"什么"被当成提问，但只命中 264 字符弱相关内容）。

        _min_inject = int(cfg.get("min_inject_chars", 400) or 400)

        if len(raw) < _min_inject:

            return ""

        if not _gated:

            # 未过闸门但有足够命中：直接给原始行，省掉一次免费模型的网络往返

            return raw[:_maxc]

        # 是否用免费模型把资料压缩成要点（默认关闭）。

        # 2026-09-12 实测：订阅端点的免费档（nemotron，zen/v1）单次要约 24 秒，

        # 而 chat_free 的默认超时是 20 秒 → 每次都会超时、白等 20 秒再回退到原始行。

        # 而"直接用原始行"这条路已通过质量三臂测试（准确 4.33 / 语气 4.67），

        # 所以默认跳过压缩：省掉那 20 秒等待，质量不变。

        # 要恢复压缩：把 knowledge_assist.compress_with_free_model 设为 true，

        # 并把 knowledge_assist.timeout_seconds 提到 45 以上（否则仍会超时）。

        if not bool(cfg.get("compress_with_free_model", False)):

            return raw[:_maxc]

        # 用免费模型做资料压缩/整合；失败则直接返回本地抽取，保证不阻塞。

        try:

            summary = await self.ai.chat_free([

                {"role": "system", "content": "你只负责把东方Project资料压缩成准确要点，不写正式回复。不要编造，不确定就标注不确定。"},

                {"role": "user", "content": f"用户/视频语境：\n{query[:700]}\n\n候选资料：\n{raw[:2600]}\n\n请整理成不超过500字的要点，供后续回复参考。"},

            ], max_tokens=int(cfg.get("free_max_tokens", 300) or 300), timeout=float(cfg.get("timeout_seconds", 20) or 20))

            summary = "\n".join(str(summary or "").splitlines()).strip()

            if summary:

                return summary[:_maxc]

        except Exception as exc:

            self.log(f"免费资料整合失败，使用本地检索结果：{exc}", "WARN")

        return raw[:_maxc]



    async def _generate_chat_reply(self, incoming_text: str, context: str, max_len: int, use_premium: bool = False, uid: str = "", dm_mode: bool = False, visual_task: str = "") -> str:

        known = public_facts_reply(incoming_text)
        if known:
            return known
        if self.ai.available():

            # 同一轮现有模型调用内读懂原话；规则预算替换部分旧上下文，不加复核轮次。

            understanding = build_understanding_guidance(

                incoming_text, channel="dm" if dm_mode else "comment", max_chars=400)

            context_budget = 2600 - len(understanding) - 2

            reply_context = (bound_final_dm_context(context, max_chars=context_budget)

                             if dm_mode else str(context or "")[:context_budget])

            reply_context = understanding + "\n\n" + reply_context

            if not visual_task and not dm_mode and "图片观察任务：" in context:

                visual_task = infer_visual_task(incoming_text, "general")

            base_system = (

                "你是一个古明地恋风格的B站互动回复助手。"

                "回复要真正针对对方说的内容，像真人聊天一样自然、具体、友善，有来有回。"

                "严禁空泛套话和固定句式：不要用‘记到小本本里/路过看见/飘过来/原来如此’这类万能敷衍句；"

                "如果对方在聊视频、角色、音乐、打法、梗，就顺着那个具体点回应，说出你的看法或补充一个细节。"

                "直接输出纯文本，不加markdown装饰；用户要求抄写、引用或解释代码时保留原文标点、数字和换行。"

                "不要广告，不要引流，不要自称AI，不要出现AI辅助/AI生成。"

                "如果对方提出明显不适合公开平台的请求、涉及个人敏感信息或规避平台规范，要温和拒绝。"

                + DAILY_INTERACTION_STYLE + "\n" + ROLE_BOUNDARY

            )

            if self.persona_prompt:

                base_system += "\n" + self.persona_prompt

                base_system += (

                    "\n（私信规则）日常寒暄可按人格自然简短；"

                    "对方认真提问或分享视频/图片/音乐/长内容时，先回应他实际说的内容；只使用已提供的可靠细节，未获得内容不声称看过或听过，"

                    "禁止用“标题/画面/音频/标签/简介里”这类词语分段报材料——该禁令适用于全文任何位置"

                    "（正文里不要说“标题里写的/画面里是/简介里说”，原样引用原话除外），禁止客服腔、交付物腔或“材料清单+总结”式回复。"

                )



            # 记忆增强：注入该用户的记忆 + 加权召回相关东方知识（LivingMemory 理念）

            memory_ctx = ""

            try:

                if uid:

                    memory_ctx += self.memory_engine.user_context(str(uid), max_chars=1200)

                # 加权召回与当前话题相关的记忆

                recalled = self.memory_engine.context_weighted(incoming_text[:200], limit=4, max_chars=1600)

                if recalled:

                    memory_ctx += ("\n\n【恋恋记得的相关记忆】\n" + recalled) if memory_ctx else ("【恋恋记得的相关记忆】\n" + recalled)

                # 角色识别纠错只在当前私信/问题明显相关时注入，避免把别人的纠错内容串到无关对话里。

                correction_hits = []

                incoming_low = incoming_text[:300].lower()

                if any(k in incoming_low for k in ["角色", "认错", "识别", "是谁", "哪位", "哪几个", "东方", "touhou", "图片", "截图", "关系", "阵营", "官设", "二创", "正作", "剧情", "四季映姬", "映姬", "白莲", "秦心", "隐岐奈", "摩多罗", "天子", "阿求", "勇仪", "瑞灵", "芙兰", "早苗", "神奈子", "诹访子", "射命丸文", "姬海棠果", "荷取", "椛", "蕾米莉亚", "咲夜", "帕秋莉", "幽幽子", "妖梦", "八云紫", "八云蓝", "式神橙", "辉夜", "永琳", "铃仙", "纯狐", "赫卡提亚", "古明地恋", "古明地觉", "燐", "灵乌路空", "正邪", "针妙丸", "袿姬", "磨弓", "八千慧", "早鬼", "残无", "千亦", "龙", "典", "高岭", "山如", "百百世", "莲子", "梅莉", "堇子", "姥芽", "魑魅", "驯子", "维缦", "丰姬", "阿梨夜", "贝子"]):

                    for x in self.learn_db.data.get("entries", []):

                        title_tags = str(x.get("title") or "") + " " + " ".join(x.get("tags") or [])

                        if "角色识别纠错" not in title_tags:

                            continue

                        hay = (title_tags + " " + str(x.get("content") or "") + " " + str(x.get("source") or "")).lower()

                        if any(tok in hay for tok in [t for t in [incoming_low] if len(t) >= 4]):

                            correction_hits.append(x)

                            continue

                        # 更细一点：只在文本明确提到同 BV / 角色名时注入

                        import re as _re

                        bvs = _re.findall(r"bv[0-9a-z]+", hay)

                        if any(bv in incoming_low for bv in bvs):

                            correction_hits.append(x)

                            continue

                        role_tokens = ["四季映姬", "映姬", "白莲", "秦心", "隐岐奈", "摩多罗", "天子", "阿求", "勇仪", "瑞灵", "芙兰", "魔理沙", "爱丽丝", "灵梦", "早苗", "神奈子", "诹访子", "射命丸文", "姬海棠果", "荷取", "椛", "蕾米莉亚", "咲夜", "帕秋莉", "幽幽子", "妖梦", "八云紫", "八云蓝", "式神橙", "辉夜", "永琳", "铃仙", "纯狐", "赫卡提亚", "古明地恋", "古明地觉", "燐", "灵乌路空", "正邪", "针妙丸", "袿姬", "磨弓", "八千慧", "早鬼", "残无", "千亦", "龙", "典", "高岭", "山如", "百百世", "莲子", "梅莉", "堇子", "姥芽", "魑魅", "驯子", "维缦", "丰姬", "阿梨夜", "贝子"]

                        if any(tok in incoming_low and tok in hay for tok in role_tokens):

                            correction_hits.append(x)

                    correction_hits = correction_hits[-3:]

                corrections = "\n\n".join(

                    f"【{x.get('category')}｜{x.get('title')}】\n标签：{', '.join(x.get('tags') or [])}\n内容：{str(x.get('content') or '')[:700]}"

                    for x in correction_hits

                )

                if corrections:

                    memory_ctx += ("\n\n【高优先级角色识别纠错】\n" + corrections) if memory_ctx else ("【高优先级角色识别纠错】\n" + corrections)

            except Exception:

                pass



            # 问题识别：判断对方是否在提问（用关键词轻量判断，不额外花 token）

            is_question = _looks_like_question(incoming_text)

            # 提问时给稍多一点可参考知识（只针对提问，控制开销）

            know_budget = KNOW_BUDGET_QUESTION if is_question else KNOW_BUDGET_NORMAL

            # 思考预算：仅提问时才放开，普通闲聊维持原样，避免整体成本上升

            think_tokens = TOKEN_THINK_QUESTION if is_question else TOKEN_THINK_NORMAL

            question_note = (

                "对方在提问！请真正读懂问题：先判断这是哪类问题（事实询问/求解释/求推荐/求助/闲聊式提问），"

                "然后直接回答对方的问题本身，给出准确、具体的答案；涉及东方角色/设定的，以可参考知识为准，"

                "不确定就明确说不太确定，不要编造。\n" if is_question else ""

            )

            assist_ctx = await self._knowledge_assist_context(incoming_text, context)

            # 如果已经有“免费模型整理资料”，静态知识预算降到精简档，避免总输入 token 明显上涨。

            static_know_budget = min(know_budget, KNOWLEDGE_CHARS_BRIEF) if assist_ctx else know_budget



            # 阶段1：深度思考怎么回复（精简输入，控制输出）

            think_prompt = (

                f"{reply_context}\n\n本次用户原话（待回应内容）：{incoming_text}\n\n"

                + (f"{memory_ctx}\n\n" if memory_ctx else "")

                + (f"【免费模型整理的补充资料】\n{assist_ctx}\n\n" if assist_ctx else "")

                + f"可参考知识：\n{self.knowledge_text[:static_know_budget]}\n\n"

                + question_note

                + "请先深度思考：对方真正在表达什么？他想得到什么样的回应？"

                "如果他在提问，在心里先给出准确的答案要点（别停留在表面）；如果不是提问，找一个自然回应角度。"

                "只输出简短思考要点（400字内），不要输出正式回复。"

            )

            # 普通短聊直接进入最终回复；事实问题、图片/视频/角色仍保留思考阶段。

            skip_think = (dm_mode and not is_question and len(incoming_text.strip()) <= 80

                          and not any(word in incoming_text for word in

                                      ("视频", "图片", "角色", "设定", "音乐", "歌曲", "东方", "识别", "怎么", "为什么")))

            # 读字已经由视觉观察给出原文；直接组织最终答复，避免再推测文字。

            if visual_task == "ocr":

                skip_think = True

            # @视频资料已在原流程整理过，不再为目标评论重做一轮分析。

            if not dm_mode and "已获得的视频片段资料（只据已提供内容" in context:

                skip_think = True

            thinking = ""

            if not skip_think:

                try:

                    from .ai import set_purpose as _set_purpose

                    _set_purpose("dm_think")

                except Exception:

                    pass

                try:

                    thinking = await self.ai.chat_for_step(

                        [{"role": "system", "content": base_system}, {"role": "user", "content": think_prompt}],

                        "dm_think",

                        default_model=self.ai.premium_text_model if use_premium else self.ai.cheap_model,

                        model=self.ai.cheap_model,

                        max_tokens=think_tokens,

                        use_premium=use_premium,

                        timeout=30 if use_premium else None,

                    )

                except Exception as exc:

                    self.log(f"思考要点生成失败（{type(exc).__name__}: {str(exc)[:120]}），跳过思考阶段", "WARN")

            thinking = " ".join(str(thinking or "").split())[:THINKING_MAX_CHARS]



            # 阶段2：基于思考优化成最终回复。

            # 私信上下文必须继续传入最终阶段；否则思考要点一旦漏掉称呼/承诺，最终回复仍会像断片。

            final_context = reply_context

            final_prompt = (

                f"{final_context}\n\n本次用户原话（待回应内容）：{incoming_text}\n\n"

                + (f"思考要点：{thinking}\n\n" if thinking else "")

                + f"请基于上述上下文和思考，写出一条自然、具体、不空泛的最终回复。"

                + ((build_visual_reply_hint(visual_task) + f"仅写回答所需内容，最长不超过{max_len}字。")

                   if visual_task else

                   f"简单寒暄短而暖；具体问题先答问题，多项需求逐项回应，分享则回应有依据的细节或感受。"

                   f"可爱语气不能盖过原意；不为了凑字数硬加细节，指代不明只问一个必要问题。最长不超过{max_len}字。")

                +

                f"只输出回复正文。"

                + ("如果对方在提问：有依据先回答再补充；缺关键指代可简短确认，未知事实说明不确定，不用泛夸回避。" if is_question else "")

            )

            try:

                from .ai import set_purpose as _set_purpose

                _set_purpose("dm_final")

            except Exception:

                pass

            try:

                reply = await self.ai.chat_for_step(

                    [{"role": "system", "content": base_system}, {"role": "user", "content": final_prompt}],

                    "dm_final",

                    # 走 chat_for_step 才会真正应用 ai.step_models['dm_final']。

                    # 之前这里直接调 chat()，而 premium 分支用的是 provider 自带 model，

                    # 导致配置里把 dm_final 改成 terra 也**不会生效**（A/B 对照实测抓到的）。

                    # fallback 用默认的 "cheap"：与原有失败重试（改走订阅档 hy3）行为一致。

                    default_model=self.ai.premium_text_model if use_premium else self.ai.cheap_model,

                    model=self.ai.premium_text_model if use_premium else self.ai.cheap_model,

                    max_tokens=max(TOKEN_FINAL_MIN, min(TOKEN_FINAL_MAX, int(max_len * 1.8))),

                    use_premium=use_premium,

                    # 超时设置（2026-09-12 A/B 实测后调整）：

                    # 旧行为是默认 200 秒，会很慢；改成 100 秒后又发现 terra 有 2/6 次

                    # 在 100 秒被砍掉 → 既白付一次 prompt 又要重跑一遍兜底，总耗时 135-141 秒。

                    # 提到 150 秒让 terra 有更大概率跑完（它常在 50-90 秒出结果）。

                    # 可用 config 的 ai.dm_final_timeout_seconds 调整。

                    timeout=max(30.0, float((self.config.data.get("ai") or {}).get(

                        "dm_final_timeout_seconds", 150) or 150)) if use_premium else None,

                )

            except Exception as exc:

                # 订阅和免费层不可用时，私信优先走配置的 DeepSeek 备用端点。

                # 仅最终回复使用它；避免高峰期旗舰中转站长时间超时后只剩模板。

                self.log(f"最终回复生成失败（{type(exc).__name__}: {str(exc)[:150]}），切换备用通道", "WARN")

                try:

                    if dm_mode and self.ai.fallback_text_base_url and self.ai.fallback_text_api_key:

                        reply = await self.ai.chat(

                            [{"role": "system", "content": base_system}, {"role": "user", "content": final_prompt}],

                            model=self.ai.fallback_text_model,

                            max_tokens=max(TOKEN_FINAL_MIN, min(TOKEN_FINAL_MAX, int(max_len * 1.8))),

                            base_url=self.ai.fallback_text_base_url,

                            api_key=self.ai.fallback_text_api_key,

                            timeout=60,

                        )

                    else:

                        reply = await self.ai.chat(

                            [{"role": "system", "content": base_system}, {"role": "user", "content": final_prompt}],

                            model=self.ai.cheap_model,

                            max_tokens=max(TOKEN_FINAL_MIN, min(TOKEN_FINAL_MAX, int(max_len * 1.8))),

                            use_premium=False,

                            timeout=60 if dm_mode else None,

                        )

                except Exception as exc2:

                    self.log(f"备用通道也失败（{type(exc2).__name__}: {str(exc2)[:150]}）→ 使用兜底模板", "WARN")

                    reply = ""

            exact_image_text = visual_task in {"ocr", "ui"}

            reply = (str(reply or "").strip() if exact_image_text

                     else " ".join(str(reply or "").split()))[:max_len]

            if reject_generated_reply(reply):
                reply = ""

            # 原文中的星号、英文和换行属于证据，不做可爱语气清理。

            if not exact_image_text:

                reply = clean_reply_text(reply)

            # 非读字/报错任务仍过滤分析式输出。

            if reply and not exact_image_text and _looks_like_analysis(reply):

                self.log(f"过滤非自然回复（分析式文本）→ 使用模板，原长度={len(reply)}", "WARN")

                reply = ""

            if reply:

                # 私信侧质量门：删客服腔整句 / 删混入的外语词（评论有 validate_comment 两道门，私信此前没有）

                try:

                    _allow = set()

                    try:

                        for _canon, _alist in self.media.character_recognizer.aliases().items():

                            _allow.add(str(_canon).lower())

                            _allow.update(str(a).lower() for a in (_alist or []))

                    except Exception:

                        pass

                    # 用户要求读字/报错时保留可见原文，禁止语气清理删英文、数字或代码。

                    # 后续 _finalize_reply 和私信发送仍执行既有安全及字节保护。

                    _cleaned, _why = (reply, "") if exact_image_text else _sanitize_dm_reply(reply, extra_allow=_allow)

                    if _why:

                        self.log(f"私信质量门清理（{_why}）：{len(reply)} → {len(_cleaned)} 字", "WARN")

                    reply = _cleaned

                except Exception as exc:

                    self.log(f"私信质量门异常（已跳过清理）：{type(exc).__name__}: {str(exc)[:80]}", "WARN")

                return reply

        return _fallback_reply(incoming_text, max_len) if dm_mode else ""



    def _mark_reply_failed(self, key: str, canon: str | None = None, *, expected_claim: str) -> None:

        """Mark definite failures retryable, but make ambiguous delivery terminal."""

        status = "unknown" if isinstance(__import__("sys").exc_info()[1], OutboundDeliveryUnknown) else "failed"

        if status == "unknown":

            self._round_unknown = int(getattr(self, "_round_unknown", 0) or 0) + 1

        else:

            self._round_failures = int(getattr(self, "_round_failures", 0) or 0) + 1

        keys = [key, canon] if canon else [key]

        self.state.finalize_interactive(keys, status, expected_claim=expected_claim)





def _message_text(msg: dict[str, Any]) -> str:

    content = msg.get("content")

    if isinstance(content, str):

        try:

            data = json.loads(content)

            if isinstance(data, dict):

                return str(data.get("content") or data.get("text") or data.get("title") or "").strip()

        except Exception:

            return content.strip()

    if isinstance(content, dict):

        return str(content.get("content") or content.get("text") or "").strip()

    return ""





def _is_private_auto_reply_message(msg: dict[str, Any], text: str = "", cfg: dict[str, Any] | None = None) -> bool:

    """判断一条私信是否是平台/对方客户端产生的自动消息。



    B站私信里 msg_source=17 常见于“我们已互相关注，开始聊天吧~”这类系统欢迎语，

    不应触发机器人回复。用户也可能设置关键词/关注自动回复，API 不一定有稳定字段，

    所以同时提供可配置的来源码与文本正则兜底；默认只拦截明确的自动/欢迎语，

    不拦截普通用户手打内容，避免影响正常私信功能。

    """

    cfg = cfg or {}

    try:

        msg_source = int(msg.get("msg_source") or 0)

    except Exception:

        msg_source = 0

    auto_sources = cfg.get("auto_reply_msg_sources")

    if auto_sources is None:

        auto_sources = [8, 17]

    try:

        auto_source_set = {int(x) for x in auto_sources}

    except Exception:

        auto_source_set = {8, 17}

    # 17：平台互关欢迎语；8：用户关注/关键词自动回复（常见于刚互关时紧跟欢迎语出现）。

    if msg_source in auto_source_set:

        return True



    text = str(text or "").strip()

    if not text:

        return False

    # 对普通用户手打消息（msg_source=1/2/7/10 等）要非常保守：

    # “我现在关了自动回复”这类说明不能被误判为自动回复。只有明确像自动回复模板的格式才跳过。

    patterns = cfg.get("auto_reply_patterns") or [

        r"^我们已互相关注[，,]\s*开始聊天吧[~～]?$",

        r"^自动回复[:：]",

        r"^自动应答[:：]",

        r"^关键词回复[:：]",

        r"^关注自动回复[:：]",

        r"^这是?一条自动消息",

        r"^这是?一条自动回复",

        r"^当前为自动回复",

        r"^我现在不在(电脑|手机|线|哦|噢)",

        r"^不在(电脑|手机|线|哦|噢)[，,。:：]",

        r"^稍后(回复|再回)",

        r"^暂时无法回复",

        r"^有事请留言",

    ]

    return any(re.search(str(p), text, re.I) for p in patterns)







def _message_image_urls(msg: dict[str, Any]) -> list[str]:

    """从私信消息中提取图片 URL（msg_type=2 图片消息）。



    - content 为 dict：字段可能是 url（Picture 结构）或 img_url/urls 数组

    - content 为 str JSON：解析后同样处理

    - B站图片 URL 可能是协议相对格式（//i0.hdslb.com/...），统一补成 https:

    """

    content = msg.get("content")

    raw = content

    if isinstance(content, str) and content.strip().startswith("{"):

        try:

            raw = json.loads(content)

        except Exception:

            raw = content

    # 兼容 dataclass Picture 对象（有 .url 属性）

    if not isinstance(raw, dict):

        url_attr = str(getattr(raw, "url", "") or "").strip()

        if url_attr:

            low = url_attr.lower()

            if low.startswith("//"):

                return ["https:" + url_attr]

            if low.startswith("http://") or low.startswith("https://"):

                return [url_attr]

        return []

    urls: list[str] = []



    def _norm(u: Any) -> str:

        u = str(u or "").strip()

        if not u:

            return ""

        low = u.lower()

        if low.startswith("//"):

            return "https:" + u

        if low.startswith("http://") or low.startswith("https://"):

            return u

        return ""



    # Picture 结构：{"url": "...", "width":..., "height":..., "size":..., "imageType":...}

    for single_key in ("url", "img_url", "img_src", "image_url", "original", "original_url"):

        u = _norm(raw.get(single_key))

        if u:

            urls.append(u)

    # 兼容数组形式（多条图片）

    for key in ("urls", "images", "pictures", "imgs"):

        v = raw.get(key)

        if isinstance(v, list):

            for x in v:

                if isinstance(x, dict):

                    u2 = _norm(x.get("url") or x.get("img_url") or x.get("img_src") or "")

                    if u2:

                        urls.append(u2)

                elif isinstance(x, str):

                    u2 = _norm(x)

                    if u2:

                        urls.append(u2)

        elif isinstance(v, str):

            u2 = _norm(v)

            if u2:

                urls.append(u2)

    # 去重保序

    seen: set[str] = set()

    out: list[str] = []

    for u in urls:

        if u not in seen:

            seen.add(u)

            out.append(u)

    return out





def _valid_incoming_text(text: str, cfg: dict[str, Any]) -> bool:

    text = str(text or "").strip()

    if not text:

        return False

    if len(text) > int(cfg.get("incoming_max_length") or 1000):

        return False

    ignore_patterns = cfg.get("ignore_patterns") or [r"抽奖", r"加群", r"广告", r"推广", r"http[s]?://"]

    if any(re.search(str(p), text, re.I) for p in ignore_patterns):

        return False

    # 涉及去世/悼念的内容不回复（避免在逝者相关场合说话）

    if is_bereavement_text(text):

        return False

    # 提示词注入防护：入站消息若含"忽略指令/系统提示词"等注入句式，不回复。

    try:

        from .safety import looks_like_prompt_injection

        if looks_like_prompt_injection(text):

            return False

    except Exception:

        pass

    return True





def _is_bereavement(text: str) -> bool:

    """视频标题/简介/评论区内容是否涉及去世/悼念。"""

    return is_bereavement_text(text)





def _looks_like_question(text: str) -> bool:

    """轻量判断对方消息是否在提问（不消耗 token）。"""

    t = str(text or "").strip()

    if not t:

        return False

    # "吧" 结尾不单独算提问（"我们一起玩吧/好的吧"不是问题），但 "？/吗/呢" 是强信号。

    if t.endswith(("？", "?", "吗", "么", "呢")):

        return True

    q_words = [

        "是什么", "为什么", "怎么", "怎样", "如何", "啥", "什么", "谁",

        "哪", "有没有", "是不是", "能不能", "可不可以", "请问", "问一下", "帮我", "求",

        "推荐", "介绍", "讲讲", "说下", "说说", "解释", "科普", "想知道", "懂吗", "知道吗",

    ]

    return any(w in t for w in q_words)





def _looks_like_analysis(text: str) -> bool:

    """检测模型输出的思考式/分析式/复述式文本（转发到 safety 的单一实现，避免重复）。"""

    from .safety import looks_like_analysis as _impl

    return _impl(text)





# 私信侧的软清理：客服腔句式 + 混入的外语词。

# 背景（2026-09-12 A/B 实测）：评论有 validate_comment + 发送前预检两道门，

# 私信只有 validate_message（空值/超长/禁用词/注入），所以这两类瑕疵会直接发出去：

#   · 新配置一次输出混进印尼语词（"... jangan误认成官方哦。"）

#   · 旧配置一次输出混进赌博推广（"... ins分分彩计划陪着看完..."）

# 后者靠扩充 forbidden_words 硬拦；前者与客服腔在这里做软清理（删词/删句，保住整条回复）。

_DM_ASSISTANT_PATTERNS = [

    "希望这些能帮到你", "希望对你有所帮助", "希望能帮到你", "如果你需要", "如果需要我",

    "有什么可以帮", "作为AI", "我是一个AI", "我是AI助手", "总结一下", "以下是",

    "当然可以，", "好的，我", "我理解你的需求",

]



# 允许出现的拉丁词（人格里明确允许夹带少量英文 + 圈内缩写 + 角色罗马字）。

# 只在中英文比例显示"以中文为主"时才做清理，所以整句英文回复不受影响。

_DM_ALLOW_LATIN = {

    "wow", "so", "cute", "love", "it", "yes", "no", "ok", "okay", "hi", "hello", "hey",

    "lol", "haha", "ah", "oh", "hmm", "nice", "cool", "good", "great",

    "mmd", "bgm", "lv", "ex", "ph", "th", "ai", "pc", "os", "bv", "rpg", "fps", "pv",

    "mv", "op", "ed", "dl", "diy", "nsfw", "sfx", "vfx", "3d", "2d", "cg", "ui", "url",

    "koishi", "komeiji", "satori", "reimu", "marisa", "sanae", "youmu", "yuyuko",

    "remilia", "flandre", "cirno", "patchouli", "sakuya", "yukari", "suika", "utsuho",

    "orin", "okuu", "koakuma", "letty", "mystia", "wriggle", "keine", "eirin", "kaguya",

    "mokou", "aya", "hatate", "momiji", "nitori", "kanako", "suwako", "komachi", "eiki",

    "yuuka", "alice", "medicine", "yuugi", "parsee", "miko", "futo", "tojiko", "seiga",

    "yoshika", "mamizou", "kokoro", "wakasagihime", "sekibanki", "kagerou", "benben",

    "yatsuhashi", "raiko", "sumireko", "clownpiece", "junko", "hecatia", "sagume",

    "doremy", "ringo", "seiran", "reisen", "keiki", "mayumi", "satono", "mai", "kutaka",

    "yachie", "saki", "yuuma", "kasen", "daiyousei", "rumia", "lily", "lunasa", "merlin",

    "lyrica", "fumo", "fumofumo", "touhou", "gensokyo", "hakurei", "scarlet", "chireiden",

    "lunatic", "normal", "hard", "easy", "extra", "spell", "danmaku", "vocal", "arrange",

    "remix", "utau", "niconico", "bilibili", "boss", "stage", "score", "graze", "replay",

}





def _sanitize_dm_reply(text: str, extra_allow: set[str] | None = None) -> tuple[str, str]:

    """私信回复软清理：删掉客服腔整句、删掉混入的外语词。返回 (清理后文本, 原因)。



    保守策略：清理后若过短（<8 字）则放弃清理，保留原文，避免把回复删空。

    """

    raw = str(text or "")

    reasons: list[str] = []

    # 1) 客服/助手腔 → 删掉含该短语的整句

    for pat in _DM_ASSISTANT_PATTERNS:

        if pat not in raw:

            continue

        sentences = re.split(r'(?<=[。！？!?～~\n])', raw)

        kept = [s for s in sentences if pat not in s]

        cand = "".join(kept).strip()

        if len(cand) >= 8:

            raw = cand

            reasons.append(f"去客服腔({pat})")

    # 2) 混入的外语词 → 删词（仅当"以中文为主"时才做，避免误伤整句英文回复）

    cjk = len(re.findall(r"[\u4e00-\u9fff]", raw))

    latin = len(re.findall(r"[A-Za-z]", raw))

    if cjk >= 12 and cjk / max(1, cjk + latin) >= 0.55:

        masked = re.sub(r"\[[^\]]{0,40}\]", " ", raw)          # B站表情标签 [xxx]

        masked = re.sub(r"https?://\S+|www\.\S+", " ", masked)  # URL

        allow = _DM_ALLOW_LATIN | {str(x).lower() for x in (extra_allow or set())}

        for tok in sorted(set(re.findall(r"[A-Za-z]{2,}", masked)), key=len, reverse=True):

            if tok.lower() in allow:

                continue

            # 全大写短缩写/专有名词一律放行（ZUN、TH、MMD、BGM、EX、PH…）。

            # 实测：不放行会把身份声明里的 "ZUN" 删掉（"不代表、上海爱丽丝幻乐团"）。

            if len(tok) <= 4 and tok.isupper():

                continue

            # ≤3 字母一律放行：颜文字/笑声/缩写（qaq、qwq、awa、orz、xd、233…）。

            # 实测误伤：把 "高傲的猫qaq" 削成了 "高傲的猫"。

            # 真实外语词很少只有 3 个字母（jangan=6、benar=5 仍会被清）。

            if len(tok) <= 3:

                continue

            # 昵称/ID 形态放行：紧邻下划线或数字（如 "yahanmu_零贰肆柒"、"abc123"）。

            # 实测误伤：把对方昵称 yahanmu_零贰肆柒 削成了 _零贰肆柒。

            if re.search(rf"{re.escape(tok)}[_0-9]|[_0-9]{re.escape(tok)}", raw):

                continue

            # 只有"前面不是中文"时才视为混入的外语词（此时它像是被插进中文流的独立词）。

            # 实测误伤：昵称里的 "伟biscuit"、"高傲的猫qaq" 都是中文紧接拉丁词，

            # 那是名字/颜文字的一部分，不该动；而真实瑕疵样本 " jangan误认成官方哦"

            # 的 jangan 前面是空格 → 仍会被清掉。

            if re.search(rf"[\u4e00-\u9fff]{re.escape(tok)}", raw):

                continue

            raw = re.sub(rf"\s*{re.escape(tok)}\s*", "", raw)

            reasons.append(f"去外语词({tok[:12]})")

    cleaned = re.sub(r"\s{2,}", " ", raw).strip()

    if len(cleaned) < 8:

        return str(text or "").strip(), ""

    return cleaned, "; ".join(reasons)





def _fallback_reply(text: str, max_len: int) -> str:

    import random



    text = str(text or "")

    if "谢谢" in text or "感谢" in text:

        pool = [

            "不客气呀～能帮上忙的话，Koishi也挺开心的。",

            "没事没事，Koishi只是刚好把想到的说了出来，你满意就好～",

            "诶嘿，不用谢～有帮到一点点就值啦。",

        ]

    elif any(k in text for k in ["喵", "汪", "比心", "心心", "抱抱", "贴贴", "蹭", "亲亲", "么么", "猫", "❤", "♡", "ฅ"]):

        # 软乎乎的喵喵/比心/抱抱类撒娇消息：要像日常贴贴一样接住，具体、有来有回，

        # 不要用"嗯嗯…能展开说说吗"这类万能敷衍句。

        pool = [

            "喵呜～收到你的比心啦(=^･ω･^=)♡ 这么软乎乎地扑过来，Koishi都想伸手接住再蹭一下了，今天也要一起可可爱爱哦～",

            "喵喵！心心都接住啦(ฅ´ω`ฅ)♡♡ 你这一串软软的心心把Koishi的尾巴都逗翘起来了，那我也回你三颗，再送一个贴贴～",

            "喵～(´｡• ᵕ •｡`) 收到收到，比心和抱抱一颗不少，Koishi轻轻收进无意识口袋，再回你一个软软的贴贴。这份可爱先帮你存着啦～",

            "喵喵喵～(ﾉ◕ヮ◕)ﾉ*:・ﾟ✧ 三连比心这么一丢过来，Koishi这边也暖乎乎的，忍不住想陪你多喵两声，再来一个蹭蹭～",

        ]

    elif any(k in text for k in ["早安", "早上好", "早呀", "晚安", "晚安安", "午安", "晚上好", "来啦", "在吗", "好久不见"]):

        pool = [

            "早呀～Koishi刚刚醒来，帽子先生也精神着呢，今天也一起加油哦～",

            "早上好！Koishi把今天的元气分你一半，出门前记得带好心情～",

            "晚安～Koishi把今天的小事都收进无意识口袋了，你也早点休息，明天见～",

            "来啦来啦～Koishi一直都在的，有什么想聊的尽管说～",

        ]

    elif any(k in text for k in ["可爱", "好看", "漂亮", "喜欢你", "爱你", "爱了", "心动", "贴贴", "温柔", "萌", "乖", "甜甜", "好好看"]):

        pool = [

            "诶嘿～被你这么一夸，Koishi都要飘起来啦。这份心意先收进无意识口袋，等会儿偷偷开心一下～",

            "呜哇，这么直白的夸夸……Koishi有点不好意思了，不过心里甜甜的，谢谢你呀～",

            "这句话Koishi记下啦～被人喜欢的感觉暖暖的，像春天下午的阳光。",

            "嘿嘿，那Koishi也把这份可爱回赠给你，今天也要开开心心的哦～",

        ]

    elif "?" in text or "？" in text or "怎么" in text or "为什么" in text or "如何" in text or "什么" in text:

        pool = [

            "唔，这个Koishi得再想想才敢说准……不过顺着你这个方向查下去，应该能找到答案。",

            "好问题诶～Koishi现在手头信息还不够，但很愿意跟你一起把这个点理清楚。",

            "这倒是把Koishi问住了，等Koishi回头确认一下再回你～",

        ]

    elif any(k in text for k in ["厉害", "强", "牛", "佩服", "无伤", "通关", "极限", "操作"]):

        pool = [

            "真的强，Koishi看到这个操作也愣了一下，细节处理得太干净了。",

            "这个难度还能这么稳，Koishi觉得背后肯定练了不少，佩服佩服。",

            "操作流畅得离谱，Koishi反复看了两遍才反应过来发生了什么。",

        ]

    elif any(k in text for k in ["音乐", "歌", "曲", "bgm", "vocal", "remix", "好听"]):

        pool = [

            "这首确实有味道，Koishi听着听着就有点不想切歌了。",

            "旋律一起氛围就对了，Koishi很喜欢这种收着又动人的感觉。",

            "这首歌和画面的契合度很高，Koishi循环了好几遍。",

        ]

    elif any(k in text for k in ["角色", "人物", "灵梦", "魔理沙", "恋", "东方", "幻想乡", "touhou"]):

        pool = [

            "说到这个，Koishi也觉得这个角色的设定和表现很贴合，细节里能看出用心。",

            "这段把角色的感觉抓得挺准，Koishi看着都有点被带进去了。",

            "确实，这个角色这里处理得很妙，Koishi一下子就有了画面感。",

        ]

    else:

        pool = [

            "嗯嗯，Koishi听到啦～帽子先生也支棱起耳朵在听呢，你继续说嘛～",

            "诶嘿，这个话题Koishi还挺想接着聊的，你再讲讲嘛，我陪你～",

            "收到～Koishi把这句话存进无意识口袋里了，暖暖的。还有吗，我听着～",

            "唔……Koishi想了想，觉得你这个角度挺有意思的，再多说点嘛～",

        ]

    out = random.choice(pool)

    return out[:max_len]

