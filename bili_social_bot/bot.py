from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .ai import AIClient
from .bili_client import BiliClient, OutboundDeliveryUnknown, StationPostItem, VideoItem
from .birthdays import (
    birthday_config,
    birthday_month_day,
    birthday_notify_key,
    build_owner_birthday_message,
    format_birthday_stats,
    load_birthday_state,
    save_birthday_cache,
    save_birthday_state,
    scan_mutual_birthdays_with_client,
    users_due_in_days,
)
from .comment_safety import blocked_by_comment_tags
from .config import BotConfig, is_private_message_blocked, local_recommendation_allowed
from .outbound_guard import AccountSendBlocked
from .constants import *
from .emotes import EmoteSelector
from .free_model_guard import guard_free_model_output
from .greeting_delivery import (greeting_pause_remaining, greeting_wait_remaining, is_greeting_risk_error, pause_scheduled_greetings)
from .media import MediaAnalysis, MediaAnalyzer, _fallback_comment_draft
from .role_identity import output_role_conflict
from .metrics import emit_round_metrics
from .morph import apply_morph, current_morph
from .persona import build_persona_prompt, load_knowledge_text
from .relation_guard import FollowerEndpointGuard, is_follower_risk_error
from .safety import validate_comment, validate_message
from .scheduler import check_schedule_cost, current_time, is_rule_due
from .state import BotState
from .stickers import LocalStickerPicker
from .touhou import TouhouCompliance
from .touhou_learning import TouhouLearningDB
from .utils import append_log, extract_bvid, is_bereavement_text, now_iso, resolve_path, save_json_atomic

from bilibili_api import comment as bili_comment
from bilibili_api.comment import CommentResourceType, OrderType


FINANCE_MARKET_FALSE_POSITIVE_WORDS = (
    # 防止“京东方A”等股票/财经标题因为含有“东方”被当成东方Project内容。
    "京东方a", "股票", "股市", "a股", "证券", "炒股", "盘中分析", "盘前", "盘后",
    "k线", "分时图", "短线", "买卖点", "压力支撑", "交易体系", "盘感",
    "药明康德", "长电科技", "紫金矿业", "中国巨石", "英维克", "云南锗业", "长飞光纤", "有研新材", "立讯精密",
)


def _is_finance_market_false_positive(text: str) -> bool:
    low = str(text or "").lower()
    return any(word in low for word in FINANCE_MARKET_FALSE_POSITIVE_WORDS)


# 东方相关性强信号：官方标签/常见角色名/作品名/核心社区词。
# 搜索接口会把带“东方龙胤”这类无关标签的视频也返回，必须先确认确实是东方内容再分析评论。
_TOUHOU_STRONG_NAMES = (
    "古明地恋", "古明地觉", "博丽灵梦", "雾雨魔理沙", "十六夜咲夜", "蕾米莉亚", "芙兰朵露",
    "帕秋莉", "爱丽丝·玛格特洛依德", "爱丽丝玛格特洛依德", "魂魄妖梦", "西行寺幽幽子", "八云紫",
    "八云蓝", "琪露诺", "大妖精", "红美铃", "米斯蒂娅", "露米娅", "蕾蒂", "莉莉白",
    "小恶魔", "风见幽香", "梅蒂欣", "射命丸文", "河城荷取", "犬走椛", "东风谷早苗",
    "八坂神奈子", "洩矢诹访子", "键山雏", "四季映姬", "小野冢小町", "永琳", "辉夜",
    "铃仙", "因幡帝", "上白泽慧音", "藤原妹红", "伊吹萃香", "星熊勇仪", "多多良小伞",
    "幽谷响子", "水桥帕露西", "黑谷山女", "灵乌路空", "火焰猫燐", "阿空", "阿燐",
    "摩多罗隐岐奈", "纯狐", "赫卡提亚", "莉格露", "桑妮缪尔克", "铃瑚", "静叶",
    "若鹭姬", "今泉影狼", "九十九弁弁", "九十九八桥", "堀川雷鼓", "赤蛮奇", "帕露西",
    "哆来咪", "稀神探女", "天弓千亦", "爱塔妮缇", "埴安神袿姬", "依神女苑",
    "骊驹早鬼", "牛崎润美", "庭渡久侘歌", "戎璎花", "丁礼田舞", "尔子田里乃",
    "宇佐见堇子", "茨木华扇", "本居小铃", "稗田阿求", "东风谷", "博丽", "雾雨", "旧地狱",
    "地灵殿", "红魔乡", "妖妖梦", "永夜抄", "花映塚", "风神录", "星莲船", "神灵庙",
    "辉针城", "绀珠传", "天空璋", "鬼形兽", "虹龙洞", "兽王园", "东方PROJECT", "东方Project",
    "touhou", "THBWiki", "幻想乡", "fumo", "油库里", "东方同人", "东方二创",
    "东方曲", "东方音乐", "东方stg", "东方project", "こいし", "东方角色", "东方人物",
)
_TOUHOU_NEGATIVE_HINTS = (
    "特摄", "假面骑士", "奥特曼", "战队", "高达", "机战", "黑神话", "原神", "崩坏",
    "星穹", "崩铁", "绝区零", "塞尔达", "赛马娘", "宝可梦", "口袋妖怪", "舰娘",
    "初音", "miku", "洛天依", "明日方舟", "英雄联盟", "王者荣耀", "彩虹六号", "第五人格",
)


def _looks_touhou_video(item) -> bool:
    """候选视频是否真有东方相关特征（标签/标题/简介）。"""
    try:
        title = str(item.title or "").lower()
        desc = str(item.desc or "")[:400].lower()
        tags = [str(x).lower() for x in (item.tags or [])]
        hay = title + " " + desc + " " + " ".join(tags)
    except Exception:
        hay = ""
    # 负向硬提示：明确是其它作品/题材，直接排除（避免“东方龙胤”这类词骗过弱匹配）
    for neg in _TOUHOU_NEGATIVE_HINTS:
        if neg in hay:
            return False
    # 强信号（角色名/作品名/官方标签等）命中一个即认可
    for name in _TOUHOU_STRONG_NAMES:
        if name.lower() in hay:
            return True
    # 只剩裸“东方”子串（如“东方龙胤”）不算东方内容
    return False


class SocialBot:
    def __init__(self, config: BotConfig):
        self.config = config
        self.state = BotState(config.state_file)
        self.follower_guard = FollowerEndpointGuard(config.state_file)
        self.client = BiliClient(
            cookie_file=config.cookie_file,
            min_gap_seconds=config.global_min_action_gap_seconds,
        )
        self.ai = AIClient(config.data.get("ai") or {})
        self.owner_uid = str(config.data.get("owner_uid") or "").strip()
        self.owner_name = str(config.data.get("owner_name") or "管理员")
        self.ai.switch_callback = self._notify_model_switch
        self.persona_prompt = build_persona_prompt(config.data.get("persona") or {})
        self.knowledge_text = load_knowledge_text(config.data.get("touhou_knowledge") or {})
        learn_cfg = config.data.get("touhou_learning") or {}
        self.touhou_learning = TouhouLearningDB(
            path=str(learn_cfg.get("memory_file") or "data/touhou_learning.json"),
            max_entries=int(learn_cfg.get("max_entries", 2000) or 2000),
        )
        # 记忆增强引擎（LivingMemory 理念）：强化/衰减/加权检索/用户记忆
        from .memory_engine import MemoryEngine
        self.memory_engine = MemoryEngine(
            self.touhou_learning,
            user_memory_path=str(learn_cfg.get("user_memory_file") or "data/user_memory.json"),
        )
        self.media = MediaAnalyzer(
            config.data.get("media_understanding") or {},
            self.ai,
            logger=self.log,
            persona_prompt=self.persona_prompt,
            knowledge_text=self.knowledge_text,
        )
        self.touhou = TouhouCompliance(config.data.get("touhou_compliance") or {})
        self.emotes = EmoteSelector(config.data.get("emotes") or {})
        self.stickers = LocalStickerPicker(config.data.get("stickers") or {}, owner_uid=self.owner_uid)
        self.morph_config = dict(config.data.get("morph") or {})
        self.morph_config.setdefault("timezone", config.data.get("timezone") or "Asia/Shanghai")
        # 注意：fumo 形态按星期切换，必须每次调用时动态计算（不能启动时固定一次，
        # 否则跨日/跨周后形态不更新——例如周六启动的 fumo 形态会一直带到周日）。
        self.morph = ""  # 占位，_apply_morph 里动态计算
        self._comment_sent_ts: list[float] = []  # 进程内主动评论发送时间戳（每小时限速用）

    def _apply_morph(self, text: str) -> str:
        # 每次调用按当前日期动态判断形态（fumo 按星期切换，不能缓存启动时的值）。
        morph = current_morph(self.morph_config)
        return apply_morph(text, morph, self.morph_config)

    def _strip_blocked_text_emotes(self, text: str) -> str:
        text = str(text or "")
        em_cfg = self.config.data.get("emotes") or {}
        prefixes = [str(x) for x in em_cfg.get("blocked_text_emote_prefixes", ["[东方收藏集·浮生若梦_"]) if str(x)]
        if bool(em_cfg.get("allow_unverified_text_emotes", False)) or not prefixes:
            return text
        for pfx in prefixes:
            text = re.sub(re.escape(pfx) + r"[^\]]*\]", "", text)
        return re.sub(r"[ \t]{2,}", " ", text).strip()

    def _private_uid_blocked(self, uid: str | int) -> bool:
        """按磁盘上的最新配置判断 UID 是否拒收私信。"""
        return is_private_message_blocked(self.config, uid)

    async def _send_private_with_optional_sticker(self, uid: str | int, text: str, *, allow_sticker: bool = True,
                                                   sticker_probability: float | None = None,
                                                   allow_excluded: bool = False, recommendation: bool = False) -> Any:
        """先发文字，再按概率附加本地表情包；图片失败只记 WARN，不影响主流程。"""
        if recommendation and not local_recommendation_allowed(self.config, str(uid)):
            raise AccountSendBlocked("本机无法核验该收件人的推荐授权")
        if self._private_uid_blocked(uid):
            self.log(f"私信拒收名单拦截发送 uid={uid}", "INFO")
            return None
        def can_dispatch():
            return not self._private_uid_blocked(uid) and (
                not recommendation or local_recommendation_allowed(self.config, str(uid)))
        result = await self.client.send_private_text(uid, text, allow_dispatch=can_dispatch)
        self._last_private_text_sent_at = asyncio.get_running_loop().time()
        if not allow_sticker:
            return result
        pic = self.stickers.choose("dm", target_uid=str(uid), probability_override=sticker_probability,
                                   allow_excluded=allow_excluded)
        if not pic:
            return result
        if self._private_uid_blocked(uid) or (recommendation and not local_recommendation_allowed(self.config, str(uid))):
            return result
        try:
            await self.client.send_private_picture(uid, pic, allow_dispatch=can_dispatch)
            self.log(f"已附加私信表情包 uid={uid} file={pic.name}", "SUCCESS")
        except Exception as exc:
            self.log(f"私信表情包发送失败 uid={uid} file={pic.name}：{exc}", "WARN")
            if is_greeting_risk_error(exc):
                self._pause_scheduled_greetings("附加图片遇到限流或验证要求")
        return result

    def _choose_comment_sticker(self, uid: str = ""):
        return self.stickers.choose("comment", target_uid=str(uid or ""))

    async def close(self) -> None:
        await self.client.close()

    def log(self, message: str, level: str = "INFO") -> None:
        append_log(self.config.log_file, message, level)

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

    async def ensure_login(self) -> bool:
        ok = await self.client.ensure_login()
        if ok:
            self.log(f"B站登录验证成功，UID={self.client.uid}")
        else:
            self.log("B站未登录或 Cookie 已失效；请先运行 python main.py login", "ERROR")
        return ok

    async def run_once(self, force: bool = False) -> None:
        started = time.perf_counter()
        usage_before = self.ai.usage_summary()
        self.ai.round_call_start = int(getattr(self.ai, "call_count", 0) or 0)
        error_category = None
        if force and not self.config.dry_run:
            emit_round_metrics(
                self.log,
                mode="run",
                started=started,
                ai=self.ai,
                usage_before=usage_before,
                error_category="ForceRejected",
                failure_count=1,
            )
            raise RuntimeError("--force 仅允许在 dry_run=true 的配置下使用")
        if not await self.ensure_login():
            # 常驻 run 时如果未登录，不要每分钟复读刷屏；直接抛出让上层停止。
            emit_round_metrics(
                self.log,
                mode="run",
                started=started,
                ai=self.ai,
                usage_before=usage_before,
                error_category="LoginError",
                failure_count=1,
            )
            raise RuntimeError("B站未登录或 Cookie 无效，请先运行 python main.py login")
        try:
            # 定时问候优先执行：早安/晚安不应被长评论分析阻塞（避免错过定时点/补发延迟）。
            await self._run_nightly_greeting(force=force)
            await self._run_morning_greeting(force=force)
            await self._run_birthday_reminder(force=force)
            await self._run_birthday_story_greeting(force=force)
            # 长任务之间再插一次问候检查：如果这一轮启动较晚，或前序任务拖了较久，
            # 也能在仍处于时间窗内时尽快补发，而不是一直等到下一次主循环。
            # 但仍不够：若这一轮在 19:58 这种"差两分钟没到点"的时刻开始，开头的问候调用会
            # 全部落空，而评论规则一整段可能跑 20~40 分钟，晚安就迟到到 20:30 之后
            # （2026-09-11 实测）。所以另起一个独立巡检任务兜住定时点，见 _greeting_watchdog_loop。
            await self._run_comment_rules(force=force)
            await self._run_nightly_greeting(force=force)
            await self._run_morning_greeting(force=force)
            await self._run_share_rules(force=force)
            await self._run_nightly_greeting(force=force)
            await self._run_morning_greeting(force=force)
            await self._run_learn_task(force=force)
            await self._run_learn_daily_report(force=force)
            await self._run_wiki_learning(force=force)
            await self._run_weekly_report_dynamic(force=force)
            # 小站功能是独立、保守的附加任务：排在主要评论/分享/互动之后，失败只记日志。
            await self._run_station_tasks_safely(force=force)
            await self._run_new_fan_greeting(force=force)
            await self._run_nightly_greeting(force=force)
            await self._run_morning_greeting(force=force)
            await self._run_birthday_reminder(force=force)
            await self._run_birthday_story_greeting(force=force)
            await self._run_memory_maintenance(force=force)
            await self._run_heartbeat(force=force)
        except Exception as exc:
            error_category = type(exc).__name__
            raise
        finally:
            self.state.save()
            emit_round_metrics(
                self.log,
                mode="run",
                started=started,
                ai=self.ai,
                usage_before=usage_before,
                # 现有主任务函数没有统一返回候选数；这里先记录可确认的
                # 零值，避免把发送数冒充候选数，后续可按规则逐步补充。
                candidate_count=0,
                attempt_count=0,
                sent_count=0,
                success_count=0,
                failure_count=1 if error_category else 0,
                error_category=error_category,
            )

    def _choose_station_image(self, purpose: str = "comment") -> Path | None:
        cfg = self.config.data.get("station_images") or {}
        if not cfg.get("enabled", True):
            return None
        p = random.random()
        prob = float(cfg.get(f"{purpose}_probability", cfg.get("probability", 1.0)) or 1.0)
        if p > prob:
            return None
        directory = Path(str(cfg.get("directory") or (self.config.data.get("stickers") or {}).get("directory") or "data/emoji_packs"))
        exts = [str(x).lower().lstrip(".") for x in (cfg.get("extensions") or (self.config.data.get("stickers") or {}).get("extensions") or ["jpg", "jpeg", "png", "gif", "webp"])]
        max_bytes = int(cfg.get("max_bytes") or (self.config.data.get("stickers") or {}).get("max_bytes") or 5_242_880)
        try:
            files = []
            for ext in exts:
                files.extend(directory.glob(f"**/*.{ext}"))
            files = [x for x in files if x.is_file() and x.stat().st_size <= max_bytes]
            return random.choice(files) if files else None
        except Exception as exc:
            self.log(f"选择小站配图失败：{exc}", "WARN")
            return None

    async def _run_station_tasks_safely(self, force: bool = False) -> None:
        """Run optional B站小站 tasks without affecting the main bot pipeline."""
        try:
            await self._run_station_greeting_posts(force=force)
        except Exception as exc:
            self.log(f"小站早晚安发帖任务异常（已隔离，不影响主功能）：{exc}", "WARN")
        try:
            await self._run_weekly_station_template_generation(force=force)
        except Exception as exc:
            self.log(f"小站每周模板生成任务异常（已隔离，不影响主功能）：{exc}", "WARN")
        try:
            await self._run_station_comment_check(force=force)
        except Exception as exc:
            self.log(f"小站评论检测任务异常（已隔离，不影响主功能）：{exc}", "WARN")

    def _station_template_pool_path(self) -> Path:
        cfg = self.config.data.get("station_template_pool") or {}
        return Path(str(cfg.get("file") or "data/station_greeting_templates.json"))

    def _load_station_template_pool(self) -> dict[str, list[str]]:
        path = self._station_template_pool_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                return {
                    "morning": [str(x).strip() for x in (data.get("morning") or []) if str(x).strip()],
                    "night": [str(x).strip() for x in (data.get("night") or []) if str(x).strip()],
                }
        except Exception as exc:
            self.log(f"读取小站早晚安模板池失败：{exc}", "WARN")
        return {"morning": [], "night": []}

    def _save_station_template_pool(self, pool: dict[str, list[str]]) -> None:
        path = self._station_template_pool_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "morning": list(dict.fromkeys([str(x).strip() for x in (pool.get("morning") or []) if str(x).strip()])),
            "night": list(dict.fromkeys([str(x).strip() for x in (pool.get("night") or []) if str(x).strip()])),
            "updated_at": now_iso(),
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _pick_station_template(self, kind: str) -> str:
        pool = self._load_station_template_pool()
        arr = pool.get(kind) or []
        return random.choice(arr) if arr else ""

    async def _generate_one_station_template(self, kind: str, cfg: dict[str, Any], seed: str) -> str:
        label = "早安" if kind == "morning" else "晚安"
        messages = [
            {"role": "system", "content": "你只输出中文动态正文。不要解释。不要分析。不要列要求。不要markdown。"},
            {"role": "user", "content": (
                f"仿写一条东方Project小站公开{label}帖子，只输出正文。80到160字。"
                "风格：温柔、轻快、恋恋口吻、公开小站语气。可以用 Koishi、Project、B站、buff。"
                "不要出现‘冲鸭、白给、摸鱼、代码、推文、限流、后台、满汉全席、缘之空、古明地恋恋’。"
                f"必须完整收尾。\n示例：{seed}"
            )},
        ]
        raw = await self.ai.chat_free(
            messages,
            temperature=float(cfg.get("temperature", 0.55) or 0.55),
            max_tokens=int(cfg.get("max_tokens", 220) or 220),
            timeout=float(cfg.get("timeout_seconds", 180) or 180),
        )
        guard = guard_free_model_output(
            raw,
            kind="station_greeting",
            min_chinese_chars=int(cfg.get("min_chinese_chars", 30) or 30),
            max_chars=int(cfg.get("max_chars", 240) or 240),
            reject_english=True,
            allowed_english_words=cfg.get("allowed_english_words") or ["Koishi", "Project", "B站", "buff"],
            forbidden_words=self.config.forbidden_words,
        )
        if not guard.ok:
            return ""
        text = guard.text
        bad_words = ["白给", "冲鸭", "摸鱼", "代码", "推文", "限流", "后台", "满汉全席", "缘之空", "古明地恋恋", "全员吃上"]
        if any(w in text for w in bad_words):
            return ""
        if not text.rstrip().endswith(("。", "！", "？", "~", "～", "♪", "）", ")", "✨", "🌻", "🌙", "💤", "🍬", "♡", "☆", "★")):
            return ""
        return text

    async def _run_weekly_station_template_generation(self, force: bool = False) -> None:
        cfg = self.config.data.get("station_template_pool") or {}
        if not cfg.get("enabled", True) and not force:
            return
        schedule = cfg.get("generate_schedule") or {"type": "weekly", "weekday": 1, "times": ["15:00"]}
        rule_name = "station_template_pool:weekly_generate"
        rule = {"name": rule_name, "schedule": schedule}
        due, key = (True, f"force:{now_iso()}:{rule_name}") if force else is_rule_due(rule, self.state.get_rule_last_run_key(rule_name))
        if not due:
            return
        target_morning = max(0, int(cfg.get("generate_morning", 10) or 10))
        target_night = max(0, int(cfg.get("generate_night", 10) or 10))
        pool = self._load_station_template_pool()
        seeds = {
            "morning": [
                "早上好呀～大家！清晨的光从地灵殿门缝里偷偷溜进来，恋恋把一点点元气藏进风里，路过小站的时候分给你们。记得吃早饭，今天也要轻轻松松地出发哦～(◕ᴗ◕✿)",
                "早安～Koishi在无意识里捡到一枚亮晶晶的早晨，把它放到B站小站门口啦。愿今天的Project同好们都被好天气碰一下，早餐认真吃，烦恼先放旁边哦！",
                "早呀～帽子先生也醒啦。恋恋给大家加一个小小的心情buff：今天遇到的事情都刚刚好，路上的风也温柔一点。出门前别忘了带上元气～",
            ],
            "night": [
                "晚安呀～大家今天也辛苦啦。恋恋把小站里的碎碎念收进无意识口袋，交给月光慢慢整理。还在B站游荡的话，也该把眼睛还给枕头了哦。愿大家今晚梦见柔软的幻想乡～",
                "夜深啦～Koishi从地灵殿的走廊探头看了一眼，星星已经开始打盹。今天的Project探险先存档，烦恼交给梦来稀释。早点休息，明天再继续相遇吧～(♡´▽`♡)",
                "晚安～帽子先生说再不睡就要被月亮点名啦。恋恋把大家今天的疲惫揉成小纸团，丢进无意识的夜风里。愿枕头软一点，梦也甜一点，明天见～",
            ],
        }
        added = {"morning": [], "night": []}
        attempts_per_kind = int(cfg.get("max_attempts_per_kind", 30) or 30)
        for kind, target in (("morning", target_morning), ("night", target_night)):
            for i in range(attempts_per_kind):
                if len(added[kind]) >= target:
                    break
                try:
                    text = await self._generate_one_station_template(kind, cfg, seeds[kind][i % len(seeds[kind])])
                    if text and text not in pool.get(kind, []) and text not in added[kind]:
                        added[kind].append(text)
                        self.log(f"每周小站模板生成通过：{kind} {len(added[kind])}/{target}")
                except Exception as exc:
                    self.log(f"每周小站模板生成失败 {kind}：{exc}", "WARN")
                await asyncio.sleep(float(cfg.get("generate_gap_seconds", 0.5) or 0.5))
        pool.setdefault("morning", []).extend(added["morning"])
        pool.setdefault("night", []).extend(added["night"])
        # 控制模板池长度，保留较新的模板。
        keep = max(20, int(cfg.get("keep_per_kind", 80) or 80))
        pool["morning"] = list(dict.fromkeys(pool.get("morning", [])))[-keep:]
        pool["night"] = list(dict.fromkeys(pool.get("night", [])))[-keep:]
        self._save_station_template_pool(pool)
        msg = (
            f"诶～{self.owner_name}，Koishi每周小站早晚安模板补充完成啦：\n"
            f"新增早安 {len(added['morning'])} 条，新增晚安 {len(added['night'])} 条。\n"
            f"当前模板池：早安 {len(pool['morning'])} 条，晚安 {len(pool['night'])} 条。"
        )
        if self.owner_uid:
            try:
                from .safety import validate_message
                ok, reason, safe = validate_message(msg, self.config.forbidden_words, marker="", require_marker=False)
                if ok and not self.config.dry_run:
                    await self.client.send_private_text(self.owner_uid, safe)
                self.log("每周小站模板生成汇报：" + safe if ok else f"每周小站模板生成汇报被拦截：{reason}")
            except Exception as exc:
                self.log(f"每周小站模板生成汇报失败：{exc}", "WARN")
        self.state.record_history({"action": "station_template_weekly_generated", "added_morning": len(added["morning"]), "added_night": len(added["night"])})
        self.state.set_rule_last_run_key(rule_name, key)
        self.state.save()

    async def _run_station_greeting_posts(self, force: bool = False) -> None:
        cfg = self.config.data.get("station_greeting_posts") or {}
        if not cfg.get("enabled", False) and not force:
            return
        tribee_id = int(cfg.get("tribee_id") or 29)
        # 默认强制预览：必须同时设置 dry_run=false 且 allow_send=true 才会真实外发。
        dry_run = bool(cfg.get("dry_run", True)) or self.config.dry_run or not bool(cfg.get("allow_send", False))
        jobs = [
            ("morning", "小站早安帖子", (cfg.get("morning_schedule") or (self.config.data.get("morning_greeting") or {}).get("schedule") or {"type": "daily", "times": ["07:00"]})),
            ("night", "小站晚安帖子", (cfg.get("nightly_schedule") or (self.config.data.get("nightly_greeting") or {}).get("schedule") or {"type": "daily", "times": ["22:00"]})),
        ]
        from .safety import validate_message
        from .lyrics import build_greeting_message
        for kind, label, schedule in jobs:
            rule_name = f"station_greeting_posts:{kind}"
            rule = {"name": rule_name, "schedule": schedule}
            due, key = (True, f"force:{now_iso()}:{rule_name}") if force else is_rule_due(rule, self.state.get_rule_last_run_key(rule_name))
            if not due:
                continue
            template = self._pick_station_template(kind)
            source = "pool"
            if not template:
                # 模板池为空时只用本地固定模板兜底，不实时调用模型，避免日常产生模型消费。
                template = build_greeting_message("morning" if kind == "morning" else "night", str(cfg.get("public_nickname") or "大家"))
                source = "local_fallback"
            guard = guard_free_model_output(
                template,
                kind="station_greeting",
                min_chinese_chars=int(cfg.get("min_chinese_chars", 30) or 30),
                max_chars=int(cfg.get("max_chars", 260) or 260),
                reject_english=bool(cfg.get("reject_english", True)),
                allowed_english_words=cfg.get("allowed_english_words") or ["Koishi", "Project", "B站", "buff"],
                forbidden_words=self.config.forbidden_words,
            )
            if not guard.ok:
                self.log(f"{label}模板池文本被拦截：{guard.reason} source={source} raw={template[:120]!r}", "WARN")
                self.state.record_history({"action": "station_greeting_blocked", "kind": kind, "reason": guard.reason, "source": source})
                self.state.set_rule_last_run_key(rule_name, key)
                continue
            if not str(guard.text).rstrip().endswith(("。", "！", "？", "~", "～", "♪", "）", ")", "✨", "🌻", "🌙", "💤", "🍬", "♡", "☆", "★")):
                self.log(f"{label}模板池文本疑似截断，已拦截：tail={guard.text[-20:]!r}", "WARN")
                self.state.record_history({"action": "station_greeting_blocked", "kind": kind, "reason": "possibly_truncated", "source": source})
                self.state.set_rule_last_run_key(rule_name, key)
                continue
            ok, reason, safe = validate_message(guard.text, self.config.forbidden_words, marker="", require_marker=False)
            if not ok:
                self.log(f"{label}安全检查拦截：{reason}", "WARN")
                self.state.record_history({"action": "station_greeting_blocked", "kind": kind, "reason": reason})
                self.state.set_rule_last_run_key(rule_name, key)
                continue
            if dry_run:
                self.log(f"[DRY-RUN] {label}将发布到小站 tribee_id={tribee_id}：{safe}", "INFO")
                self.state.record_history({"action": "station_greeting_dry_run", "kind": kind, "text": safe})
            else:
                img = self._choose_station_image("greeting")
                result = await self.client.send_station_text_post(
                    tribee_id=tribee_id,
                    text=safe,
                    title="",
                    category_id=int(cfg.get("category_id") or 252),
                    sync_dynamic=bool(cfg.get("sync_dynamic_enable", False)),
                    aigc=False,
                    pic_path=img,
                )
                self.log(f"{label}发布成功" + (f"（配图 {img.name}）" if img else "") + f"：{safe[:80]}", "SUCCESS")
                self.state.record_history({"action": "station_greeting_sent", "kind": kind, "text": safe, "image": str(img) if img else "", "result": _short_result(result)})
            self.state.set_rule_last_run_key(rule_name, key)
            self.state.save()

    def _station_comment_candidates_path(self) -> Path:
        cfg = self.config.data.get("station_comment_monitor") or {}
        return Path(str(cfg.get("candidates_file") or "data/station_comment_candidates.json"))

    def _load_station_comment_candidates(self) -> list[dict[str, Any]]:
        path = self._station_comment_candidates_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return [x for x in data if isinstance(x, dict)]
        except Exception as exc:
            self.log(f"读取小站评论候选失败：{exc}", "WARN")
        return []

    def _save_station_comment_candidates(self, rows: list[dict[str, Any]]) -> None:
        path = self._station_comment_candidates_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path = path.with_suffix(".md")
        lines = [
            "# 小站评论候选审核",
            "",
            "使用方法：在 JSON 文件里把想发送的候选 `status` 从 `pending` 改成 `approved`；机器人会自动发送 approved，发送后改成 sent。",
            "",
        ]
        for row in rows:
            lines.append(f"## {row.get('id')} [{row.get('status', 'pending')}]")
            lines.append(f"- 帖子：{row.get('title', '')}")
            lines.append(f"- 作者：{row.get('author', '')}")
            lines.append(f"- 链接：{row.get('url', '')}")
            lines.append(f"- 生成时间：{row.get('created_at', '')}")
            lines.append("")
            lines.append(str(row.get("text") or ""))
            lines.append("")
        md_path.write_text("\n".join(lines), encoding="utf-8")

    def _upsert_station_comment_candidate(self, post: StationPostItem, text: str) -> None:
        rows = self._load_station_comment_candidates()
        cid = f"dyn:{post.dyn_id}"
        for row in rows:
            if row.get("id") == cid:
                if row.get("status") in {"sent", "approved"}:
                    return
                row.update({"text": text, "updated_at": now_iso(), "status": row.get("status") or "pending"})
                self._save_station_comment_candidates(rows)
                return
        rows.append({
            "id": cid,
            "status": "pending",
            "dyn_id": post.dyn_id,
            "title": post.title,
            "author": post.author,
            "url": post.url,
            "text": text,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        })
        self._save_station_comment_candidates(rows)

    async def _send_approved_station_comments(self, cfg: dict[str, Any], *, force: bool = False) -> int:
        if not (cfg.get("send_approved", False) or force):
            return 0
        if not bool(cfg.get("allow_send", False)) and not force:
            return 0
        rows = self._load_station_comment_candidates()
        max_send = max(1, int(cfg.get("max_approved_send_per_run", 1) or 1))
        sent = 0
        for row in rows:
            if sent >= max_send:
                break
            if str(row.get("status") or "").lower() != "approved":
                continue
            dyn_id = str(row.get("dyn_id") or "").strip()
            text = str(row.get("text") or "").strip()
            guard = guard_free_model_output(
                text,
                kind="station_comment",
                min_chinese_chars=int(cfg.get("min_chinese_chars", 12) or 12),
                max_chars=int(cfg.get("max_chars", 90) or 90),
                reject_english=True,
                allowed_english_words=cfg.get("allowed_english_words") or ["Koishi", "Project", "B站"],
                forbidden_words=self.config.forbidden_words,
            )
            if not dyn_id or not guard.ok:
                row["status"] = "blocked_before_send"
                row["blocked_reason"] = guard.reason or "missing_dyn_id"
                row["updated_at"] = now_iso()
                continue
            if self.config.dry_run:
                self.log(f"[DRY-RUN] 将发送已批准小站评论 dyn={dyn_id}：{guard.text}", "INFO")
                row["status"] = "approved_dry_run"
                row["updated_at"] = now_iso()
                sent += 1
                continue
            try:
                result = await self.client.send_dynamic_comment(dyn_id, guard.text)
                row["status"] = "sent"
                row["sent_at"] = now_iso()
                row["result"] = _short_result(result)
                self.log(f"已发送审核批准的小站评论 dyn={dyn_id}：{guard.text}", "SUCCESS")
                sent += 1
            except OutboundDeliveryUnknown as exc:
                # 请求可能已经被平台接受；终态阻止下一轮自动重发，需人工核验。
                row["status"] = "delivery_unknown"
                row["updated_at"] = now_iso()
                row["error"] = type(exc).__name__
                self.log(f"审核批准的小站评论结果未知，禁止自动重发 dyn={dyn_id}：{type(exc).__name__}", "ERROR")
                sent += 1
            except Exception as exc:
                row["status"] = "send_failed"
                row["error"] = str(exc)[:300]
                row["updated_at"] = now_iso()
                self.log(f"发送已批准小站评论失败 dyn={dyn_id}：{exc}", "ERROR")
        self._save_station_comment_candidates(rows)
        return sent

    def _station_comment_templates_path(self) -> Path:
        cfg = self.config.data.get("station_comment_monitor") or {}
        return Path(str(cfg.get("template_file") or "data/station_comment_templates.json"))

    def _load_station_comment_templates(self) -> dict[str, Any]:
        path = self._station_comment_templates_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception as exc:
            self.log(f"读取小站评论模板失败：{exc}", "WARN")
        return {"skip_keywords": [], "role_keywords": [], "categories": []}

    def _pick_title_keyword(self, title: str, role_keywords: list[str]) -> str:
        title = str(title or "")
        # 优先角色/阵营关键词，其次取短标题片段。
        for kw in sorted([str(x) for x in role_keywords if str(x).strip()], key=len, reverse=True):
            if kw and kw in title:
                return kw
        clean = title.strip().replace("【", "").replace("】", "")
        for sep in ["：", ":", "，", ",", "！", "!", "？", "?", " "]:
            if sep in clean:
                clean = clean.split(sep)[0]
                break
        return clean[:12] if clean else "这个标题"

    def _render_station_template_comment(self, post: StationPostItem, cfg: dict[str, Any]) -> tuple[str, str, str]:
        """全免费：按标题分类 + 本地短句模板生成小站评论。返回(text, category, keyword)。"""
        data = self._load_station_comment_templates()
        title = str(post.title or "").strip()
        hay = title.lower()
        for bad in data.get("skip_keywords") or []:
            if str(bad).strip() and str(bad).lower() in hay:
                return "", "skip", str(bad)
        role_keywords = [str(x) for x in (data.get("role_keywords") or []) if str(x).strip()]
        kw = self._pick_title_keyword(title, role_keywords)
        cats = data.get("categories") or []
        chosen = None
        for cat in cats:
            if not isinstance(cat, dict) or cat.get("name") == "default":
                continue
            for k in cat.get("keywords") or []:
                s = str(k or "").strip()
                if s and s.lower() in hay:
                    chosen = cat
                    break
            if chosen:
                break
        if not chosen:
            chosen = next((c for c in cats if isinstance(c, dict) and c.get("name") == "default"), None)
        if not chosen:
            return "", "missing_template", kw
        templates = [str(x).strip() for x in (chosen.get("templates") or []) if str(x).strip()]
        if not templates:
            return "", str(chosen.get("name") or "unknown"), kw
        # 用 dyn_id/title 做稳定轮换，避免每次运行同一帖子评论变来变去。
        seed = sum(ord(ch) for ch in (post.dyn_id + title))
        tmpl = templates[seed % len(templates)]
        short_title = title[:24]
        text = tmpl.replace("{kw}", kw).replace("{title}", short_title)
        return text.strip(), str(chosen.get("name") or "default"), kw

    async def _run_station_comment_check(self, force: bool = False) -> None:
        cfg = self.config.data.get("station_comment_monitor") or {}
        if not cfg.get("enabled", False) and not force:
            return
        # 先尝试发送你已审核批准的候选；未开启 allow_send/send_approved 时不会发送。
        await self._send_approved_station_comments(cfg, force=force)
        tribee_id = int(cfg.get("tribee_id") or 29)
        # 默认强制预览：必须同时设置 dry_run=false 且 allow_send=true 才会真实外发。
        dry_run = bool(cfg.get("dry_run", True)) or self.config.dry_run or not bool(cfg.get("allow_send", False))
        schedule = cfg.get("schedule") or {"type": "interval", "minutes": 90}
        rule_name = "station_comment_monitor"
        rule = {"name": rule_name, "schedule": schedule}
        due, key = (True, f"force:{now_iso()}:{rule_name}") if force else is_rule_due(rule, self.state.get_rule_last_run_key(rule_name))
        if not due:
            return
        max_per_run = max(0, int(cfg.get("max_comments_per_run", 1) or 1))
        daily_limit = max(0, int(cfg.get("daily_limit", 3) or 3))
        bucket = self.state.today_bucket()
        used = int(bucket.get("station_comments", 0) or 0)
        if used >= daily_limit:
            self.log(f"小站评论检测跳过：今日限额已用完 {used}/{daily_limit}", "WARN")
            self.state.set_rule_last_run_key(rule_name, key)
            return
        posts = await self.client.get_station_posts(tribee_id, page_size=int(cfg.get("fetch_count", 20) or 20), sort_type=int(cfg.get("sort_type", 2) or 2))
        ignore_top = bool(cfg.get("ignore_top", True))
        sent = 0
        st = self.state.data.setdefault("station_comment_monitor", {})
        commented = st.setdefault("commented", {})
        attempted = 0
        max_attempts = max(max_per_run, int(cfg.get("max_generation_attempts_per_run", 3) or 3))
        for post in posts:
            if sent >= max_per_run or attempted >= max_attempts or int(bucket.get("station_comments", 0) or 0) >= daily_limit:
                break
            if ignore_top and "TOP" in [str(x) for x in ((post.raw.get("meta") or {}).get("tags") or [])]:
                continue
            if commented.get(post.dyn_id):
                continue
            title = (post.title or "").strip()
            if not title:
                commented[post.dyn_id] = "ignored_empty"
                continue
            attempted += 1
            text, category, picked_kw = self._render_station_template_comment(post, cfg)
            if not text:
                self.log(f"小站评论模板跳过 dyn={post.dyn_id} category={category} keyword={picked_kw}", "WARN")
                commented[post.dyn_id] = f"skipped:{category}:{picked_kw}"
                continue
            guard = guard_free_model_output(
                text,
                kind="station_comment",
                min_chinese_chars=int(cfg.get("min_chinese_chars", 12) or 12),
                max_chars=int(cfg.get("max_chars", 90) or 90),
                reject_english=True,
                allowed_english_words=cfg.get("allowed_english_words") or ["Koishi", "Project", "B站"],
                forbidden_words=self.config.forbidden_words,
            )
            if not guard.ok:
                self.log(f"小站评论模板被拦截 dyn={post.dyn_id}：{guard.reason} text={text[:120]!r}", "WARN")
                self.state.record_history({"action": "station_comment_blocked", "dyn_id": post.dyn_id, "title": title[:120], "reason": guard.reason})
                commented[post.dyn_id] = f"blocked:{guard.reason}"
                continue
            fake_item = VideoItem(bvid="", aid=0, title=title, desc=title)
            text = guard.text
            if bool(cfg.get("decorate_emotes", False)):
                text = self._strip_blocked_text_emotes(self.emotes.decorate_comment(text, fake_item, None))
            text = self._apply_morph(text)
            final_guard = guard_free_model_output(
                text,
                kind="station_comment",
                min_chinese_chars=int(cfg.get("min_chinese_chars", 12) or 12),
                max_chars=int(cfg.get("max_chars", 90) or 90),
                reject_english=True,
                allowed_english_words=cfg.get("allowed_english_words") or ["Koishi", "Project", "B站"],
                forbidden_words=self.config.forbidden_words,
            )
            if not final_guard.ok:
                self.log(f"小站评论最终文本被拦截 dyn={post.dyn_id}：{final_guard.reason} text={text[:120]!r}", "WARN")
                commented[post.dyn_id] = f"blocked_final:{final_guard.reason}"
                continue
            text = final_guard.text
            touhou_check = self.touhou.review_outbound_text(text, fake_item)
            if not touhou_check.ok:
                self.log(f"小站评论东方合规拦截 dyn={post.dyn_id}：{touhou_check.reason}", "WARN")
                commented[post.dyn_id] = f"blocked_touhou:{touhou_check.reason}"
                continue
            text = touhou_check.text
            from .safety import validate_comment
            ok, reason, safe = validate_comment(text, self.config.forbidden_words, marker="", require_marker=False)
            if not ok:
                self.log(f"小站评论安全检查拦截 dyn={post.dyn_id}：{reason}", "WARN")
                commented[post.dyn_id] = f"blocked_safety:{reason}"
                continue
            if dry_run:
                self.log(f"[DRY-RUN] 小站模板评论将发送 dyn={post.dyn_id}《{title}》：{safe}", "INFO")
                self.state.record_history({"action": "station_comment_dry_run", "dyn_id": post.dyn_id, "title": title[:120], "text": safe})
                st.setdefault("previewed", {})[post.dyn_id] = now_iso()
                sent += 1
                self.state.save()
                continue
            img = self._choose_station_image("comment")
            try:
                result = await self.client.send_dynamic_comment(post.dyn_id, safe, pic_path=img)
            except OutboundDeliveryUnknown as exc:
                # 请求可能已经被平台接受；终态阻止后续运行再次发送。
                commented[post.dyn_id] = "delivery_unknown"
                bucket["station_comments"] = int(bucket.get("station_comments", 0) or 0) + 1
                self.state.inc_comment()
                self.state.record_history({"action": "station_comment_delivery_unknown", "dyn_id": post.dyn_id, "title": title[:120], "error": type(exc).__name__})
                self.log(f"小站评论结果未知，禁止自动重发 dyn={post.dyn_id}：{type(exc).__name__}", "ERROR")
                sent += 1
                self.state.save()
                continue
            except Exception as exc_pic:
                if not img:
                    raise
                self.log(f"小站模板评论配图失败，降级纯文字 dyn={post.dyn_id} file={img.name}：{exc_pic}", "WARN")
                img = None
                result = await self.client.send_dynamic_comment(post.dyn_id, safe)
            self.log(f"小站模板评论成功 dyn={post.dyn_id}《{title}》" + (f"（配图 {img.name}）" if img else "") + f"：{safe}", "SUCCESS")
            commented[post.dyn_id] = now_iso()
            bucket["station_comments"] = int(bucket.get("station_comments", 0) or 0) + 1
            self.state.inc_comment()  # 计入全局每日评论额度，避免小站评论绕过主评论上限。
            self.state.record_history({"action": "station_comment_sent", "dyn_id": post.dyn_id, "title": title[:120], "text": safe, "category": category, "image": str(img) if img else "", "result": _short_result(result)})
            sent += 1
            self.state.save()
        self.log(f"小站评论检测完成：本轮处理 {sent} 条（dry_run={dry_run}）")
        self.state.set_rule_last_run_key(rule_name, key)
        self.state.save()

    async def _run_learn_task(self, force: bool = False) -> None:
        """低峰时段学习高播放东方视频（只学习，不评论）。"""
        learn_cfg = self.config.data.get("learn_task") or {}
        if not learn_cfg.get("enabled", True) and not force:
            return
        schedule = learn_cfg.get("schedule") or {"type": "daily", "times": ["04:30"]}
        rule = {"name": "东方高播放视频学习", "schedule": schedule}
        due, key = (True, f"force:{now_iso()}:learn") if force else is_rule_due(rule, self.state.get_rule_last_run_key("learn"))
        if not due:
            return
        self.log("开始执行东方高播放视频学习任务（低峰时段）")
        try:
            from bilibili_api import comment as bili_comment
            from bilibili_api.comment import CommentResourceType, OrderType
            # 学习关键词：特色梗词排前面（fumo/恋恋/东方梗），并且用最新排序
            # 而不是 click（点击量排序永远学到 bad apple 这类最高播放老视频）。
            keywords = [str(x) for x in learn_cfg.get("keywords") or [
                "fumo", "古明地恋", "东方fumo", "东方手书", "东方mmd", "东方二创",
                "东方同人", "东方Project", "touhou", "东方弹幕", "东方stg", "东方曲",
            ]]
            max_videos = max(1, int(learn_cfg.get("max_videos") or 3))
            candidates = []
            seen = set()
            for kw in keywords:
                try:
                    # 用 pubdate（最新）而非 click：学到新发布的视频/新梗，而不是永远最高播放的经典
                    found = await self.client.search_videos(kw, max_results=5, order="pubdate")
                    for v in found:
                        if v.bvid not in seen and v.title and "失效" not in v.title:
                            seen.add(v.bvid)
                            candidates.append(v)
                except Exception as exc:
                    self.log(f"学习搜索失败 {kw}：{exc}", "WARN")
                if len(candidates) >= max_videos * 3:
                    break
            allowed, blocked = self.touhou.filter_videos(candidates)
            allowed = allowed[:max_videos]
            new_learned: list[str] = []
            for item in allowed:
                try:
                    analysis = await self._analyze_if_needed(item, {"media_understanding": {
                        "enabled": True, "use_subtitles": True, "analyze_cover": True, "analyze_images": True,
                        "extract_keyframes": True, "keyframe_mode": "random", "use_segment_download": True,
                        "max_keyframes": 3, "segment_length_seconds": 4, "download_max_height": 360,
                        "transcribe_audio": True, "asr_backend": "faster_whisper", "asr_model": "small",
                        "audio_char_budget": 1200,
                        "summarize": True, "generate_comment": False, "generate_share_reason": False,
                    }})
                    if analysis.summary and self.ai.available():
                        prompt = (
                            f"这是东方Project相关视频《{item.title}》的内容摘要：\n{analysis.summary[:1000]}\n"
                            f"字幕摘录：{analysis.subtitle_text[:600]}\n画面描述：{analysis.vision_text[:600]}\n\n"
                            "提炼成一条『东方学习知识』：讲了什么、涉及哪些角色/作品/音乐/**梗/梗语/二创文化**"
                            "（如常见玩梗方式、角色梗、二创圈层黑话），简洁中文总结，不照抄原文，遵守东方二创规范。"
                            "如果里面有值得在评论区自然使用的梗或说法，请单独列出。"
                        )
                        learned = await self.ai.chat([
                            {"role": "system", "content": "你是东方Project知识整理助手，擅长提炼梗与二创文化，只输出简洁知识总结。"},
                            {"role": "user", "content": prompt},
                        ], use_premium=True, max_tokens=300)
                        if learned and len(str(learned).strip()) >= 15:
                            # 含梗/黑话/二创词汇 → meme 类（评论区可引用）；纯剧情 → story
                            learned_txt = str(learned)
                            has_meme = any(k in learned_txt for k in ["梗", "黑话", "玩", "二创", "弹幕", "评论区", "说法", "喊", "梗语", "圈内"])
                            self.touhou_learning.add(
                                title=f"视频学习：{item.title[:40]}", content=learned_txt,
                                category="meme" if has_meme else ("story" if "故事" in learned_txt or "剧情" in learned_txt else "meme"),
                                source=f"video:{item.bvid}", tags=["东方学习", "高播放视频", "梗"] if has_meme else ["东方学习", "高播放视频"], confidence="medium",
                            )
                            new_learned.append(f"《{item.title[:30]}》：{str(learned)[:120]}")
                            self.log(f"已学习视频内容：{item.bvid}")
                except Exception as exc:
                    self.log(f"学习视频失败 {item.bvid}：{exc}", "WARN")
                await asyncio.sleep(3)
            # 学习分享闭环：本次学到内容 → 私信分享给管理员（主人要求：每天最多一条）
            share_cfg = self.config.data.get("learn_share") or {}
            if new_learned and share_cfg.get("enabled", True):
                owner_uid = str(self.config.data.get("owner_uid") or "").strip()
                owner_name = str(self.config.data.get("owner_name") or "管理员")
                if owner_uid and self._claim_daily_once("learn_notify"):
                    msg = (
                        f"诶～{owner_name}，Koishi刚学到一些东方知识，分享给你：\n\n"
                        + "\n\n".join(new_learned)
                        + "\n\n（均来自社区视频内容整理，非官方设定；遵守二创规范）"
                    )
                    try:
                        from .safety import validate_message
                        ok, reason, safe = validate_message(msg, self.config.forbidden_words, marker="", require_marker=False)
                        if ok:
                            await self.client.send_private_text(owner_uid, safe)
                            self.log(f"已把学习摘要分享给 {owner_uid}（{owner_name}）")
                    except Exception as exc:
                        self.log(f"分享学习摘要失败：{exc}", "WARN")
            self.log(f"学习任务完成：处理 {len(allowed)} 个视频，新学 {len(new_learned)} 条")
        except Exception as exc:
            self.log(f"学习任务异常：{exc}", "ERROR")
        finally:
            self.state.set_rule_last_run_key("learn", key)

    async def _run_learn_daily_report(self, force: bool = False) -> None:
        """每天 21:00 向管理员汇报今天学习了什么。

        读取学习库中今天新增的条目，用恋恋口吻总结私信给主人。
        """
        report_cfg = self.config.data.get("learn_daily_report") or {}
        if not report_cfg.get("enabled", True) and not force:
            return
        schedule = report_cfg.get("schedule") or {"type": "daily", "times": ["21:00"]}
        rule = {"name": "学习日报", "schedule": schedule}
        due, key = (True, f"force:{now_iso()}:learn_report") if force else is_rule_due(rule, self.state.get_rule_last_run_key("learn_report"))
        if not due:
            return
        owner_uid = str(self.config.data.get("owner_uid") or "").strip()
        owner_name = str(self.config.data.get("owner_name") or "管理员")
        if not owner_uid:
            return
        try:
            from datetime import date
            today = date.today().isoformat()
            all_entries = list(self.touhou_learning.data.get("entries") or [])
            # 取今天创建/更新的条目
            today_entries = []
            for e in all_entries:
                t = str(e.get("created_at") or e.get("updated_at") or "")
                if t[:10] == today:
                    today_entries.append(e)
            if not today_entries:
                # 兜底：今天没学到就展示最近更新的
                today_entries = sorted(all_entries, key=lambda x: str(x.get("updated_at") or ""), reverse=True)[:10]
            if not today_entries:
                self.log("学习日报：今天没有可汇报的学习内容", "INFO")
                self.state.set_rule_last_run_key("learn_report", key)
                return
            lines = []
            shown = 0
            for e in today_entries:
                if shown >= 4:
                    break
                title = str(e.get("title") or "").strip()
                content = str(e.get("content") or "").strip().replace("\n", " ")
                if content and len(content) > 45:
                    content = content[:45] + "…"
                if title:
                    lines.append(f"· {title[:25]}：{content[:35]}" if content else f"· {title[:25]}")
                else:
                    lines.append(f"· {content[:35]}")
                shown += 1
            if len(today_entries) > shown:
                lines.append(f"……还有 {len(today_entries) - shown} 条在恋恋的小本本里～")
            if not lines:
                lines = [f"· {str(e.get('content') or '')[:35]}" for e in today_entries[:4]]
            msg = (
                f"诶～{owner_name}，晚上好！恋恋今天学到了 {len(today_entries)} 条东方知识哦(◕ᴗ◕✿)\n\n"
                + "\n".join(lines)
                + "\n\n（来自社区整理，非官方设定）"
            )
            from .safety import validate_message
            ok, reason, safe = validate_message(msg, self.config.forbidden_words, marker="", require_marker=False)
            if ok:
                await self.client.send_private_text(owner_uid, safe)
                self.log(f"已向 {owner_uid}（{owner_name}）发送学习日报：{len(today_entries)} 条", "SUCCESS")
                self.state.record_history({"action": "learn_daily_report", "count": len(today_entries), "to": owner_uid})
            else:
                self.log(f"学习日报被安全规则拦截：{reason}", "WARN")
        except Exception as exc:
            self.log(f"学习日报发送失败：{exc}", "WARN")
        finally:
            self.state.set_rule_last_run_key("learn_report", key)

    async def _run_wiki_learning(self, force: bool = False) -> None:
        """学习 THBWiki（东方Project维基）资料，存入长期记忆库。

        默认每天低峰执行一次，每次学 1-2 个页面（避免一次抓太多）。
        """
        wiki_cfg = self.config.data.get("wiki_learning") or {}
        if not wiki_cfg.get("enabled", True) and not force:
            return
        schedule = wiki_cfg.get("schedule") or {"type": "daily", "times": ["03:30"]}
        rule = {"name": "THBWiki学习", "schedule": schedule}
        due, key = (True, f"force:{now_iso()}:wiki_learn") if force else is_rule_due(rule, self.state.get_rule_last_run_key("wiki_learn"))
        if not due:
            return
        self.log("开始执行 THBWiki 东方维基学习任务")
        try:
            from .wiki_learning import DEFAULT_WIKI_PAGES, fetch_page_wikitext, learn_wiki_page

            pages = wiki_cfg.get("pages") or DEFAULT_WIKI_PAGES
            per_run = max(1, int(wiki_cfg.get("per_run") or 2))
            # 轮转：记录上次学到的位置，避免每次都从第一个开始
            cursor = int(self.state.data.get("wiki_learn_cursor", 0) or 0)
            total_new = 0
            digests: list[str] = []
            for i in range(per_run):
                idx = (cursor + i) % len(pages)
                item = pages[idx]
                page = str(item.get("page") or "").strip()
                category = str(item.get("category") or "character")
                if not page:
                    continue
                try:
                    n, digest = await learn_wiki_page(
                        page, self.touhou_learning, self.ai, category=category
                    )
                    if n:
                        total_new += n
                        digests.append(f"《{page}》：{digest[:80]}")
                        self.log(f"已学习 THBWiki：{page}（{n} 条）")
                    else:
                        self.log(f"THBWiki 页面无新增：{page}", "INFO")
                except Exception as exc:
                    self.log(f"THBWiki 学习失败 {page}：{str(exc)[:120]}", "WARN")
                await asyncio.sleep(2)
            # 更新轮转游标
            self.state.data["wiki_learn_cursor"] = (cursor + per_run) % len(pages)
            self.log(f"THBWiki学习任务完成：本轮新增 {total_new} 条知识")
            # 可选：学习到内容后简讯告知管理员（避免每天刷屏，仅在有新增且非force时）
            if total_new and not force and wiki_cfg.get("notify_owner", False):
                owner_uid = str(self.config.data.get("owner_uid") or "").strip()
                owner_name = str(self.config.data.get("owner_name") or "管理员")
                if owner_uid and self._claim_daily_once("learn_notify"):
                    msg = (
                        f"诶～{owner_name}，恋恋又从东方Wiki里学到新东西啦：\n"
                        + "\n".join(f"· {d}" for d in digests)
                        + "\n（资料整理自THBWiki，非官方设定）"
                    )
                    try:
                        from .safety import validate_message
                        ok, reason, safe = validate_message(msg, self.config.forbidden_words, marker="", require_marker=False)
                        if ok:
                            await self.client.send_private_text(owner_uid, safe)
                            self.log(f"已把Wiki学习摘要分享给 {owner_uid}", "SUCCESS")
                    except Exception as exc:
                        self.log(f"分享Wiki学习摘要失败：{exc}", "WARN")
        except Exception as exc:
            self.log(f"THBWiki学习任务异常：{exc}", "ERROR")
        finally:
            self.state.set_rule_last_run_key("wiki_learn", key)

    async def _run_weekly_report_dynamic(self, force: bool = False) -> None:
        """每周日周报动态（两阶段）：
        - 周日 07:00 收集数据：本周收藏的视频（从收藏夹拉取）+ 评论回复人数 + 私信对话次数
        - 周日 08:00 发布动态：用收集好的数据生成周报
        """
        report_cfg = self.config.data.get("weekly_report_dynamic") or {}
        if not report_cfg.get("enabled", True) and not force:
            return
        # 仅周日执行（isoweekday=7），或 force 时强制
        now = current_time()
        if not force and now.isoweekday() != 7:
            return
        collect_times = [str(x).strip() for x in (report_cfg.get("collect_times") or ["07:00"])]
        publish_times = [str(x).strip() for x in (report_cfg.get("publish_times") or ["08:00"])]
        now_str = now.strftime("%H:%M")

        # 阶段1：收集数据（07:00）
        if now_str in collect_times or force:
            rule = {"name": "周报数据收集", "schedule": {"type": "daily", "times": collect_times}}
            due, key = (True, f"force:{now_iso()}:weekly_collect") if force else is_rule_due(rule, self.state.get_rule_last_run_key("weekly_collect"))
            if due:
                await self._collect_weekly_data()
                self.state.set_rule_last_run_key("weekly_collect", key)

        # 阶段2：发布动态（08:00）
        if now_str in publish_times or force:
            rule = {"name": "周报发布", "schedule": {"type": "daily", "times": publish_times}}
            due, key = (True, f"force:{now_iso()}:weekly_report") if force else is_rule_due(rule, self.state.get_rule_last_run_key("weekly_report"))
            if due:
                await self._publish_weekly_report()
                self.state.set_rule_last_run_key("weekly_report", key)

    async def _collect_weekly_data(self) -> None:
        """周日 07:00：收集本周数据（收藏视频 + 互动统计）存入 state.weekly_report_data。"""
        self.log("开始收集本周周报数据（收藏视频 + 互动统计）")
        try:
            from datetime import timedelta
            today = current_time().date()
            week_start = today - timedelta(days=today.weekday())
            week_start_iso = week_start.isoformat()
            hist = self.state.data.get("history") or []

            # 互动统计
            comment_people: set[str] = set()   # 评论区互动者（按 notice_id 去重）
            comment_replies = 0
            dm_people: set[str] = set()        # 私信用户
            dm_replies = 0
            dynamic_replies = 0
            for h in hist:
                t = str(h.get("time") or "")
                if t[:10] < week_start_iso:
                    continue
                action = str(h.get("action") or "")
                if action == "comment_auto_reply":
                    comment_replies += 1
                    nid = str(h.get("notice_id") or h.get("aid") or "")
                    if nid:
                        comment_people.add(nid)
                elif action == "dm_auto_reply":
                    dm_replies += 1
                    uid = str(h.get("uid") or "")
                    if uid:
                        dm_people.add(uid)
                elif action == "dynamic_comment_reply":
                    dynamic_replies += 1

            # 收藏视频：优先从 B站收藏夹拉取（本周收藏的）
            favorite_videos: list[dict[str, str]] = []
            try:
                from bilibili_api import favorite_list as bili_fav
                fav_result = await bili_fav.get_video_favorite_list(
                    int(self.client.uid), credential=self.client.credential
                )
                fav_lists = (fav_result or {}).get("list") or [] if isinstance(fav_result, dict) else (fav_result or [])
                # 遍历收藏夹，找本周收藏的视频
                for fl in fav_lists:
                    try:
                        media_id = int(fl.get("id") or fl.get("media_id") or 0)
                        if not media_id:
                            continue
                        content = await bili_fav.get_video_favorite_list_content(
                            media_id=media_id, credential=self.client.credential, page=1
                        )
                        # content 可能是 dict {medias:[...]} 或直接是 dict
                        medias = []
                        if isinstance(content, dict):
                            medias = content.get("medias") or content.get("list") or []
                        elif isinstance(content, list):
                            medias = content
                        for m in medias:
                            if not isinstance(m, dict):
                                continue
                            # fav_time 是 Unix 时间戳（秒），转成日期比较
                            try:
                                fav_ts = int(m.get("fav_time") or 0)
                                if not fav_ts:
                                    continue
                                import time as _time
                                from datetime import date as _date
                                fav_date = _date.fromtimestamp(fav_ts)
                                if fav_date < week_start:
                                    continue  # 只取本周收藏的
                            except Exception:
                                continue
                            bvid = str(m.get("bvid") or "")
                            title = str(m.get("title") or "")
                            if bvid:
                                favorite_videos.append({"bvid": bvid, "title": title})
                    except Exception as exc:
                        self.log(f"读取收藏夹内容失败：{exc}", "WARN")
            except Exception as exc:
                self.log(f"获取收藏夹列表失败：{exc}，回退用历史记录", "WARN")
                # 回退：从 history 的 favorite_sent 收集
                for h in hist:
                    t = str(h.get("time") or "")
                    if t[:10] < week_start_iso:
                        continue
                    if h.get("action") == "favorite_sent" and h.get("bvid"):
                        favorite_videos.append({"bvid": str(h["bvid"]), "title": ""})

            # 收藏视频补标题（历史回退时）
            for fv in favorite_videos:
                if fv["title"]:
                    continue
                try:
                    from pathlib import Path as _Path
                    import json as _json
                    bdir = _Path(self.config.data.get("media_understanding", {}).get("cache_dir") or "data/media") / fv["bvid"]
                    if bdir.is_dir():
                        for jf in bdir.glob("analysis_*.json"):
                            try:
                                d = _json.loads(jf.read_text(encoding="utf-8"))
                                fv["title"] = str((d.get("analysis") or {}).get("title") or "")
                                if fv["title"]:
                                    break
                            except Exception:
                                continue
                except Exception:
                    pass

            # 去重
            seen: set[str] = set()
            unique_fav = []
            for fv in favorite_videos:
                if fv["bvid"] not in seen:
                    seen.add(fv["bvid"])
                    unique_fav.append(fv)

            data = {
                "week_start": week_start_iso,
                "week_end": today.isoformat(),
                "favorite_videos": unique_fav,
                "comment_people": len(comment_people),
                "comment_replies": comment_replies,
                "dm_people": len(dm_people),
                "dm_replies": dm_replies,
                "dynamic_replies": dynamic_replies,
            }
            self.state.data["weekly_report_data"] = data
            self.log(f"周报数据收集完成：收藏 {len(unique_fav)} 个视频，评论区互动者 {len(comment_people)} 人({comment_replies}次)，私信 {len(dm_people)} 人({dm_replies}次)")
        except Exception as exc:
            self.log(f"周报数据收集失败：{exc}", "ERROR")

    async def _publish_weekly_report(self) -> None:
        """周日 08:00：发布周报动态（用 07:00 收集的数据）。"""
        data = self.state.data.get("weekly_report_data") or {}
        if not data:
            self.log("周报动态：没有收集到数据（可能收集阶段未运行），跳过发布", "WARN")
            return
        try:
            fav_videos = data.get("favorite_videos") or []
            comment_people = int(data.get("comment_people") or 0)
            comment_replies = int(data.get("comment_replies") or 0)
            dm_people = int(data.get("dm_people") or 0)
            dm_replies = int(data.get("dm_replies") or 0)
            dynamic_replies = int(data.get("dynamic_replies") or 0)
            week_start = str(data.get("week_start") or "")
            week_end = str(data.get("week_end") or "")

            video_count = len(fav_videos)
            if video_count == 0:
                self.log("周报动态：本周没有收藏视频，跳过发布（避免空周报）", "WARN")
                return
            video_lines = []
            for fv in fav_videos[:10]:
                title = str(fv.get("title") or "").strip()
                bvid = str(fv.get("bvid") or "")
                if title:
                    video_lines.append(f"·《{title[:40]}》({bvid})")
                else:
                    video_lines.append(f"· {bvid}")
            if video_count > 10:
                video_lines.append(f"……还有 {video_count - 10} 个视频，恋恋都好好收藏啦～")

            total_chat = comment_replies + dm_replies + dynamic_replies
            body = (
                f"恋恋的一周小记（{week_start} ~ {week_end}）\n\n"
                f"这周恋恋收藏了 {video_count} 个东方相关的视频：\n"
                + "\n".join(video_lines)
                + "\n\n"
                f"和大家互动了 {total_chat} 次："
                f"评论区有 {comment_people} 位朋友（{comment_replies} 条），"
                f"私信有 {dm_people} 位朋友（{dm_replies} 条）"
                + (f"，动态下回复了 {dynamic_replies} 次" if dynamic_replies else "")
                + "\n\n感谢每一位和恋恋说话的朋友～"
                "无论是评论、私信还是点赞，恋恋都悄悄记在无意识的小本本里了(◕ᴗ◕✿)"
                "\n下个星期也请多多关照哦！"
            )
            if len(body) > 980:
                body = body[:960] + "\n……"
            # 表情装饰：夜雀食堂表情 + 颜文字（动态用消息风格，可句内穿插）
            try:
                fake_item = VideoItem(bvid="", aid=0, title="恋恋的一周小记")
                body = self._strip_blocked_text_emotes(self.emotes.decorate_message(body, fake_item, None, max_len=1100))
                body = self._apply_morph(body)
            except Exception:
                pass
            # 最终长度保护
            if len(body) > 1000:
                body = body[:980] + "\n……"
            if self.config.dry_run:
                self.log(f"[DRY-RUN] 周报动态：\n{body}", "INFO")
                self.state.record_history({"action": "weekly_report_dry_run", "week_start": week_start, "videos": video_count, "chat": total_chat})
            else:
                result = await self.client.send_dynamic(body)
                self.log(f"周报动态发布成功：{video_count} 个视频，{total_chat} 次互动", "SUCCESS")
                self.state.record_history({"action": "weekly_report_sent", "week_start": week_start, "videos": video_count, "chat": total_chat, "result": str(result)[:200]})
                # 发布成功清除数据，避免重复
                self.state.data.pop("weekly_report_data", None)
        except Exception as exc:
            self.log(f"周报动态发布失败：{exc}", "ERROR")

    def _greeting_list_file(self) -> Path:
        """早晚安名单缓存文件路径（记录所有应收到早晚安的用户）。"""
        state_file = Path(self.config.data.get("state_file", "data/state.json"))
        return state_file.parent / "greeting_followings.json"

    def _load_greeting_list(self) -> dict[str, dict[str, str]]:
        """读取早晚安名单缓存：{mid: {"mid":..., "uname":..., "is_human":...}}。"""
        try:
            import json as _json
            p = self._greeting_list_file()
            if p.exists():
                data = _json.loads(p.read_text(encoding="utf-8"))
                users = data.get("users") if isinstance(data, dict) else data
                out: dict[str, dict[str, str]] = {}
                for row in users or []:
                    mid = str(row.get("mid") or "")
                    if mid:
                        out[mid] = {
                            "mid": mid,
                            "uname": str(row.get("uname") or ""),
                            "is_human": "1" if row.get("is_human", "1") not in ("0", False, "false") else "0",
                            "official_title": str(row.get("official_title") or ""),
                        }
                return out
        except Exception as exc:
            self.log(f"读取早晚安名单缓存失败：{exc}", "WARN")
        return {}

    def _save_greeting_list(self, users: list[dict[str, str]]) -> None:
        """把早晚安名单写入缓存文件（供失败兜底/查看）。"""
        try:
            import json as _json
            p = self._greeting_list_file()
            data = {"updated_at": now_iso(), "users": users}
            p.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self.log(f"保存早晚安名单缓存失败：{exc}", "WARN")

    def _is_human_uname(self, uname: str, cfg: dict[str, Any]) -> bool:
        """按昵称黑名单判断是否真人：命中关键词（官方/TV/客服/已注销等）视为非真人。"""
        uname = str(uname or "").strip()
        if not uname:
            return False
        kw = [str(x).strip() for x in (cfg.get("exclude_name_keywords") or []) if str(x).strip()]
        if not kw:
            kw = ["官方", "TV", "小电视", "客服", "助手", "机器人", "账号已注销", "已注销", "黑神话", "游戏官方"]
        low = uname.lower()
        for k in kw:
            if k and k.lower() in low:
                return False
        return True

    async def _classify_human(self, mid: str, uname: str, cached: dict[str, dict[str, str]], cfg: dict[str, Any]) -> tuple[bool, str]:
        """判断用户是否真人。优先用缓存（避免每天反复调接口），无缓存才现查。

        返回 (is_human, official_title)。
        """
        cached_row = cached.get(mid)
        if cached_row and cached_row.get("is_human") in ("0", "1"):
            return cached_row["is_human"] == "1", cached_row.get("official_title") or ""
        # 昵称黑名单快速过滤
        if not self._is_human_uname(uname, cfg):
            return False, "昵称疑似非真人"
        # 现查认证
        try:
            info = await self.client.get_user_official(mid)
            if info.get("known") is False:
                return False, "认证状态未知，保守跳过"
            alive = bool(info.get("alive", True))
            role = int(info.get("role") or 0)
            title = str(info.get("title") or "")
            if not alive:
                return False, "账号已注销"
            if role != 0:
                return False, title or "官方/认证账号"
            return True, ""
        except Exception as exc:
            self.log(f"判断真人失败 {mid}：{exc}", "WARN")
            return False, "认证查询失败，保守跳过"

    async def _get_follower_map_for_greetings(self, max_users: int = 1000) -> tuple[dict[str, str], bool]:
        """实时读取粉丝列表；不完整时禁止用空结果推断“没有互关”。"""
        out: dict[str, str] = {}
        if not self.client.credential or not self.client.uid:
            return out, False
        allowed, reason = self.follower_guard.can_attempt(min_interval_seconds=12 * 3600)
        if not allowed:
            self.log(f"跳过刷新早晚安粉丝列表：{reason}", "WARN")
            return out, False
        self.follower_guard.mark_attempt("greeting_list")
        try:
            from bilibili_api import user as bili_user
            u = bili_user.User(uid=int(self.client.uid), credential=self.client.credential)
            pn, ps = 1, 50
            while len(out) < max_users:
                await self.client.throttle()
                data = await u.get_followers(pn=pn, ps=ps)
                rows = data.get("list") if isinstance(data, dict) else []
                if isinstance(rows, dict):
                    rows = rows.get("list") or rows.get("items") or []
                if not rows:
                    break
                for row in rows:
                    mid = str(row.get("mid") or row.get("uid") or "").strip()
                    uname = str(row.get("uname") or row.get("name") or "").strip()
                    if mid and mid != str(self.client.uid):
                        out[mid] = uname
                    if len(out) >= max_users:
                        break
                if len(rows) < ps:
                    break
                pn += 1
            self.follower_guard.mark_success("greeting_list")
            # 达到上限而服务端仍返回满页，说明列表可能被截断；保守不发送。
            if len(out) >= max_users and len(rows) >= ps:
                return out, False
            return out, True
        except Exception as exc:
            status = self.follower_guard.mark_failure(exc, "greeting_list")
            if is_follower_risk_error(exc):
                self.log(
                    f"刷新早晚安粉丝列表触发 -352，接口已熔断至 {status.get('blocked_until')}；本轮不发送",
                    "WARN",
                )
            else:
                self.log(f"刷新早晚安粉丝列表失败（已跳过本轮发送）：{type(exc).__name__}", "WARN")
            return {}, False

    async def _refresh_greeting_list(self) -> tuple[list[dict[str, str]], bool]:
        """刷新早晚安名单：只缓存“实时关注列表 ∩ 实时粉丝列表”的互关用户。

        旧逻辑把“我关注的人”直接当早晚安收件人，会误扰非互关；现在 all_followings
        只取互关交集。配置 recipients 默认也必须在互关交集内，除非显式
        allow_non_mutual_recipients=true。
        """
        cached: dict[str, dict[str, str]] = self._load_greeting_list()
        merged: dict[str, dict[str, str]] = {}
        me = str(self.client.uid or "")
        aliases = self.config.data.get("user_aliases") or {}
        ng_cfg = self.config.data.get("nightly_greeting") or {}
        mg_cfg = self.config.data.get("morning_greeting") or {}
        filter_non_human = bool(ng_cfg.get("filter_non_human", True) or mg_cfg.get("filter_non_human", True))
        cfg = ng_cfg or mg_cfg
        max_followings = max(50, min(int((ng_cfg.get("max_followings") or mg_cfg.get("max_followings") or 300)), 500))
        max_followers = max(100, min(int((ng_cfg.get("max_followers") or mg_cfg.get("max_followers") or 1000)), 2000))

        # 先看粉丝接口熔断/冷却状态，避免明知不可访问时还先翻完关注列表。
        allowed, reason = self.follower_guard.can_attempt(min_interval_seconds=12 * 3600)
        if not allowed:
            self.log(f"跳过刷新早晚安名单：{reason}", "WARN")
            return [], False

        try:
            rows, following_complete = await self.client.get_followings_checked(max_users=max_followings)
        except Exception as exc:
            self.log(f"刷新早晚安关注列表失败（已跳过本轮发送）：{type(exc).__name__}", "WARN")
            return [], False
        if not following_complete:
            self.log("刷新早晚安关注列表不完整，拒绝更新收件人快照", "WARN")
            return [], False
        following_map = {str(r.get("mid") or "").strip(): str(aliases.get(str(r.get("mid") or "").strip()) or r.get("uname") or "") for r in rows if str(r.get("mid") or "").strip()}
        follower_map, follower_complete = await self._get_follower_map_for_greetings(max_users=max_followers)
        if not follower_complete:
            self.log("刷新早晚安粉丝列表不完整，拒绝更新收件人快照", "WARN")
            return [], False
        mutual_ids = set(following_map) & set(follower_map)
        self.log(f"刷新早晚安名单：关注={len(following_map)} 粉丝={len(follower_map)} 确认互关={len(mutual_ids)}", "INFO")

        # 配置 recipients 默认也必须互关；如果确实要例外，需显式 allow_non_mutual_recipients=true。
        for cfg_name in ("nightly_greeting", "morning_greeting"):
            c = self.config.data.get(cfg_name) or {}
            for uid in (c.get("recipients") or []):
                uid = str(uid).strip()
                if not uid or uid == me:
                    continue
                if uid not in mutual_ids:
                    self.log(f"跳过早晚安固定收件人：非互关 uid={uid}", "WARN")
                    continue
                uname = str(aliases.get(uid) or following_map.get(uid) or follower_map.get(uid) or cached.get(uid, {}).get("uname") or "")
                merged[uid] = {"mid": uid, "uname": uname, "is_human": "1", "official_title": ""}

        for mid in sorted(mutual_ids):
            if not mid or mid == me:
                continue
            uname = str(aliases.get(mid) or following_map.get(mid) or follower_map.get(mid) or "")
            if filter_non_human:
                is_human, otitle = await self._classify_human(mid, uname, cached, cfg)
                if not is_human:
                    self.log(f"跳过非真人 {mid}（{uname}）：{otitle}")
                    continue
            merged[mid] = {"mid": mid, "uname": uname, "is_human": "1", "official_title": ""}

        users = list(merged.values())
        self._save_greeting_list(users)
        return users, True

    async def _add_to_greeting_list(self, mid: str, uname: str = "") -> None:
        """把一位互关用户立即加入早晚安名单（如新粉丝回关后），不等下一次拉取。

        强约束：早晚安只发互关。这里会实时确认“我已关注对方 + 对方关注我”后才入列；
        若关系查询失败或尚未互关，保守跳过，下一次刷新名单时再补。
        """
        mid = str(mid or "").strip()
        if not mid or mid == str(self.client.uid or ""):
            return
        ng_cfg = self.config.data.get("nightly_greeting") or {}
        cfg = ng_cfg or {}
        if not self._is_human_uname(uname, cfg):
            self.log(f"新粉丝 {mid}（{uname}）命中非真人黑名单，不加入早晚安名单", "WARN")
            return
        try:
            from bilibili_api import user as bili_user
            fu = bili_user.User(uid=int(mid), credential=self.client.credential)
            rel = await fu.get_relation()
            rel_obj = (rel or {}).get("relation") or {}
            ber_obj = (rel or {}).get("be_relation") or {}
            rel_val = int(rel_obj.get("attribute") if isinstance(rel_obj, dict) else rel_obj or 0)
            ber_val = int(ber_obj.get("attribute") if isinstance(ber_obj, dict) else ber_obj or 0)
            if (rel_val & 2) == 0 or (ber_val & 4) == 0:
                self.log(f"新粉丝 {mid}（{uname}）尚未确认互关，不加入早晚安名单 rel={rel_val} be={ber_val}", "WARN")
                return
        except Exception as exc:
            self.log(f"确认新粉丝互关关系失败 {mid}（{uname}），不加入早晚安名单：{exc}", "WARN")
            return
        merged = self._load_greeting_list()
        aliases = self.config.data.get("user_aliases") or {}
        merged[mid] = {"mid": mid, "uname": str(aliases.get(mid) or uname or ""), "is_human": "1", "official_title": ""}
        self._save_greeting_list(list(merged.values()))
        self.log(f"已将 {mid}（{merged[mid]['uname']}）加入早晚安名单", "INFO")

    async def _gather_greeting_recipients(self, cfg: dict[str, Any],
                                          kind: str | None = None) -> tuple[list[dict[str, str]], bool]:
        """收集问候消息的接收者：配置的 recipients + 所有关注的人（可选）。

        返回 [{"mid": str, "uname": str}]，去重且排除自己。
        排除：静默名单（B站 21047）+ 已取关的用户（不再打扰）+ 本档的 exclude_uids。

        ``kind``：问候档位（"morning" / "nightly"）。给了就读该档配置里的
        ``exclude_uids``，实现「**只**关掉某个人的某一档问候」——
        例如某人不要早安、但晚安照收。注意这与全局 ``greeting_quiet`` 不同，
        后者是早安/晚安**一起**停。
        """
        me = str(self.client.uid or "")
        quiet = set(self.state.data.setdefault("greeting_quiet", []))
        # 本档排除名单（按档独立）：morning_greeting.exclude_uids / nightly_greeting.exclude_uids
        excluded = {str(x).strip() for x in (cfg.get("exclude_uids") or []) if str(x).strip()}
        # 读取取关名单（chat_state.json 里 chat-run 记录的用户，跨进程共享）
        unfollowed: set[str] = set()
        try:
            import json as _json
            from pathlib import Path as _Path
            cs = _Path(self.config.data.get("state_file", "data/state.json")).with_name("chat_state.json")
            if cs.exists():
                cd = _json.loads(cs.read_text(encoding="utf-8"))
                unfollowed = set((cd.get("unfollowed_cache") or {}).keys())
        except Exception:
            pass
        seen: set[str] = set()
        out: list[dict[str, str]] = []

        def push(mid: str, uname: str = "") -> None:
            mid = str(mid or "").strip()
            if (not mid or mid == me or mid in seen
                    or mid in quiet or mid in unfollowed or mid in excluded):
                return
            seen.add(mid)
            out.append({"mid": mid, "uname": str(uname or "")})

        # 正常发送只读最近一次成功生成的互关缓存。真正发送每一位之前还会调用
        # _verify_greeting_mutual 实时复核，所以缓存过期不会误发给已经取关的人。
        # 新粉丝回关后由 _add_to_greeting_list 增量加入，不需要 45 秒巡检反复翻页。
        if cfg.get("all_followings", False) or cfg.get("recipients"):
            cached = self._load_greeting_list()
            if cached:
                users, complete = list(cached.values()), True
            else:
                users, complete = await self._refresh_greeting_list()
                if not complete:
                    return [], False
            for row in users:
                push(row.get("mid") or "", row.get("uname") or "")
        return out, True

    def _greeting_state(self, kind: str) -> dict[str, Any]:
        """读写问候分批状态：{date, sent_map:{uid:发送日期}, done:bool, recipients_snapshot:[...]}。

        sent_map 记录「每个用户最近一次发送的日期」，用日期判断当天是否已发，
        跨天/进程重启都不会丢（旧日期自然不等于今天），从根上避免重复发送。
        recipients_snapshot 记录当天冻结的接收名单，避免补发期间不断并入新粉丝。
        兼容旧格式：sent_uids(list) 自动迁移为 sent_map。
        """
        st = self.state.data.setdefault("greeting_batch", {})
        entry = st.setdefault(kind, {"date": "", "done": False})
        if not isinstance(entry, dict):
            entry = st[kind] = {"date": "", "done": False}
        if "sent_map" not in entry or not isinstance(entry["sent_map"], dict):
            entry["sent_map"] = {}
        if "recipients_snapshot" not in entry or not isinstance(entry["recipients_snapshot"], list):
            entry["recipients_snapshot"] = []
        if "recipients_snapshot_verified_mutual" not in entry:
            entry["recipients_snapshot_verified_mutual"] = False
        for key in ("blocked_map", "abandoned_map", "failed_map", "sending_map", "delivery_unknown_map", "fail_count"):
            if not isinstance(entry.get(key), dict):
                entry[key] = {}
        # 迁移旧格式：sent_uids 里的 uid 记为「entry.date 当天已发」
        old = entry.get("sent_uids") or []
        if old:
            old_date = str(entry.get("date") or "")
            for uid in old:
                uid = str(uid).strip()
                if uid and uid not in entry["sent_map"]:
                    entry["sent_map"][uid] = old_date
            entry["sent_uids"] = []
        return entry

    async def _verify_greeting_mutual(self, uid: str) -> tuple[bool, bool]:
        """Re-check both relation directions immediately before a greeting send."""
        if not self.client.credential or not self.client.uid:
            return False, False
        try:
            from bilibili_api import user as bili_user
            target = bili_user.User(uid=int(uid), credential=self.client.credential)
            relation = await asyncio.wait_for(target.get_relation(), timeout=20)
            rel_obj = (relation or {}).get("relation") or {}
            be_obj = (relation or {}).get("be_relation") or {}
            rel = int(rel_obj.get("attribute") or 0) if isinstance(rel_obj, dict) else int(rel_obj or 0)
            be = int(be_obj.get("attribute") or 0) if isinstance(be_obj, dict) else int(be_obj or 0)
            return bool((rel & 2) and (be & 4)), True
        except Exception as exc:
            self.log(f"早晚安发送前互关复核失败 uid={uid}（已跳过）：{type(exc).__name__}", "WARN")
            if is_greeting_risk_error(exc):
                self._pause_scheduled_greetings("互关复核遇到限流或验证要求")
            return False, False

    def _greeting_window_cfg(self, kind: str) -> tuple[int, int]:
        """返回主站问候允许发送时间窗 [start_hour, end_hour)。end_hour 可为 24。"""
        cfg_name = "morning_greeting" if kind == "morning" else "nightly_greeting"
        cfg = self.config.data.get(cfg_name) or {}
        defaults = (6, 10) if kind == "morning" else (20, 24)
        try:
            start = int(cfg.get("window_start_hour", defaults[0]) or defaults[0])
            end = int(cfg.get("window_end_hour", defaults[1]) or defaults[1])
        except Exception:
            start, end = defaults
        start = max(0, min(23, start))
        end = max(start + 1, min(24, end))
        return start, end

    def _greeting_in_window(self, kind: str, now: datetime | None = None) -> bool:
        now = now or current_time()
        start, end = self._greeting_window_cfg(kind)
        return start <= now.hour < end

    def _greeting_due_today(self, kind: str, schedule: dict[str, Any], now: datetime | None = None) -> tuple[bool, str]:
        """到点触发或今天未发完需要补发时返回 (True, key)。

        但仅允许在配置的发送时间窗内继续发送：
        - 早安默认 06:00-10:00
        - 晚安默认 20:00-24:00
        """
        now = now or current_time()
        today = now.date().isoformat()
        st = self._greeting_state(kind)
        if st.get("date") != today:
            if not self._greeting_in_window(kind, now):
                return False, ""
            rule = {"name": kind, "schedule": schedule}
            due, key = is_rule_due(rule, self.state.get_rule_last_run_key(kind), now)
            if not due and self._greeting_catchup_allowed(kind, schedule, now):
                due, key = True, f"{kind}:{today}:late"
            if due:
                st["date"] = today
                st["done"] = False
                st["recipients_snapshot"] = []
                st["recipients_snapshot_verified_mutual"] = False
                st.pop("force_stopped_reason", None)
                st.pop("fail_count", None)
            return due, key
        if not st.get("done"):
            if not self._greeting_in_window(kind, now):
                # 时间窗外不能补发，但也不能把未完成批次标成 done=True；
                # 否则当天窗口内因重启/人工停止只发了一部分时，状态会误显示“已完成”，
                # 互关用户会被跳过，直到第二天才有机会恢复。
                st["window_closed_at"] = now_iso()
                self.state.save()
                return False, ""
            return True, f"{kind}:{today}:resume"
        return False, ""

    def _greeting_catchup_allowed(self, kind: str, schedule: dict[str, Any], now: datetime | None = None) -> bool:
        """当天错过定时点后是否允许补发（仅当当天还没发过且仍在发送时间窗）。"""
        now = now or current_time()
        if not self._greeting_in_window(kind, now):
            return False
        times = schedule.get("times") or []
        for t in times:
            try:
                h, m = (int(x) for x in str(t).split(":"))
            except Exception:
                continue
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now >= target:
                return True
        return False

    def _greeting_incomplete_recovery_due(self, kind: str, cfg: dict[str, Any], now: datetime | None = None) -> tuple[bool, str]:
        """修复“当天 done=True 但互关名单未发完”的异常状态。

        之前人工停止/异常重启可能把早晚安批次标为 done=True，且 recipients_snapshot 为空，
        导致后续轮次直接跳过，互关用户虽然在 greeting_followings.json 里却收不到问候。
        只在对应时间窗内恢复，避免早安/晚安在不合适时间补发。
        """
        now = now or current_time()
        today = now.date().isoformat()
        st = self._greeting_state(kind)
        if st.get("date") != today or not bool(st.get("done")):
            return False, ""
        if not self._greeting_in_window(kind, now):
            return False, ""
        if not (cfg.get("all_followings", False) or cfg.get("recipients")):
            return False, ""

        me = str(self.client.uid or "")
        quiet = {str(x) for x in self.state.data.setdefault("greeting_quiet", [])}
        # 与发送路径保持一致：本档 exclude_uids 里的人不算"漏发"，
        # 否则会把已主动排除的用户当成漏发对象、反复触发恢复。
        excluded = {str(x).strip() for x in (cfg.get("exclude_uids") or []) if str(x).strip()}
        recipients = st.get("recipients_snapshot") or []
        # 如果快照为空/未互关验证，用已刷新过的互关缓存作为轻量探测；真正发送前仍会重建快照。
        if not recipients or not bool(st.get("recipients_snapshot_verified_mutual", False)):
            recipients = list(self._load_greeting_list().values())
        expected = {
            str(r.get("mid") or "").strip()
            for r in recipients
            if str(r.get("mid") or "").strip() and str(r.get("mid") or "").strip() != me and str(r.get("mid") or "").strip() not in quiet
            and str(r.get("mid") or "").strip() not in excluded
        }
        if not expected:
            return False, ""
        sent_today = {str(uid) for uid, day in (st.get("sent_map") or {}).items() if str(day) == today}
        missing = expected - sent_today
        if not missing:
            return False, ""

        # 恢复批次，让 _greeting_recipients_snapshot 重建互关快照并继续未发送用户。
        st["done"] = False
        st["recipients_snapshot"] = []
        st["recipients_snapshot_verified_mutual"] = False
        st.pop("force_stopped_reason", None)
        self.state.save()
        sample = ",".join(sorted(missing)[:8])
        self.log(f"{('早安' if kind == 'morning' else '晚安')}批次状态异常：done=True 但仍有 {len(missing)} 位互关用户未处理，已恢复续发 sample={sample}", "WARN")
        return True, f"{kind}:{today}:recover-incomplete"

    async def _greeting_recipients_snapshot(self, kind: str, cfg: dict[str, Any], st: dict[str, Any], today: str) -> tuple[list[dict[str, str]], bool]:
        """返回当天冻结的问候接收者名单，避免补发时不断吸收新关注。"""
        recipients = st.get("recipients_snapshot") or []
        # 旧版本快照可能包含“只关注但非互关”的用户；没有互关验证标记时必须重建。
        if st.get("date") != today or not recipients or not bool(st.get("recipients_snapshot_verified_mutual", False)):
            recipients, complete = await self._gather_greeting_recipients(cfg, kind)
            if not complete:
                st["snapshot_refresh_error_at"] = now_iso()
                st["done"] = False
                self.state.save()
                return [], False
            st["date"] = today
            st["recipients_snapshot"] = [
                {"mid": str(r.get("mid") or ""), "uname": str(r.get("uname") or "")}
                for r in recipients
                if str(r.get("mid") or "").strip()
            ]
            st["recipients_snapshot_verified_mutual"] = True
            self.state.save()
        return list(st.get("recipients_snapshot") or []), bool(st.get("recipients_snapshot_verified_mutual", False))

    def _greeting_pause_path(self) -> Path:
        # 独立落盘，不会被 chat-run 持有的旧 state 副本清空。
        return resolve_path(self.config.state_file).with_name("greeting_send_pause.json")

    def _pause_scheduled_greetings(self, reason: str) -> None:
        seconds = pause_scheduled_greetings(self._greeting_pause_path())
        self.log(f"早晚安暂停：{reason}，约 {seconds // 60} 分钟后再检查；重启保留暂停状态", "WARN")

    @staticmethod
    def _greeting_batch_options(cfg: dict[str, Any]) -> tuple[int, int, int]:
        """读取问候分批大小和发送间隔，集中处理配置容错。"""
        batch_size = max(1, int(cfg.get("batch_size", 5) or 5))
        interval = cfg.get("send_interval_seconds") or [10, 20]
        if isinstance(interval, list) and len(interval) >= 2:
            lo, hi = int(interval[0]), int(interval[1])
        else:
            lo, hi = 10, 20
        lo, hi = max(3, lo), max(lo + 1, hi)
        return batch_size, lo, hi

    def _mark_greeting_recipient_done(self, kind: str, st: dict[str, Any], uid: str, today: str) -> None:
        """将单个问候接收者标记为当天已处理并立即落盘，防止重启重复发送。"""
        st.setdefault("sent_map", {})[uid] = today
        st.setdefault("sending_map", {}).pop(uid, None)
        self.state.mark_greeting_sent(kind, uid, today)

    def _mark_greeting_blocked(self, kind: str, st: dict[str, Any], uid: str, today: str) -> None:
        st.setdefault("blocked_map", {})[uid] = today
        st.setdefault("sending_map", {}).pop(uid, None)
        self.state.mark_greeting_blocked(kind, uid, today)

    def _mark_greeting_quiet(self, uid: str) -> None:
        quiet = self.state.data.setdefault("greeting_quiet", [])
        if uid not in quiet:
            quiet.append(uid)
            self.log(f"已将 {uid} 加入问候静默名单（未互动限制）", "INFO")
        self.state.add_greeting_quiet(uid)

    def _record_greeting_failure(self, st: dict[str, Any], uid: str, today: str, label: str) -> None:
        """记录问候发送失败；同一用户连续失败 3 次后当天放弃，避免无限重试。"""
        fail = self.state.record_greeting_failure("morning" if label == "早安" else "nightly", uid, today)
        st.setdefault("fail_count", {})[uid] = fail
        st.setdefault("failed_map", {})[uid] = today
        if fail >= 3:
            self.log(f"{label}消息连续失败 {fail} 次，标记已处理 {uid}", "WARN")

    async def _send_one_scheduled_greeting(
        self,
        *,
        uid: str,
        nickname: str,
        lyric_kind: str,
        label: str,
        kind: str,
        st: dict[str, Any],
        today: str,
    ) -> bool:
        """发送单条早/晚安私信；返回是否真实发送成功。"""
        from .lyrics import build_greeting_message

        if self._private_uid_blocked(uid):
            self.log(f"{label}消息跳过私信拒收用户 uid={uid}", "INFO")
            if not self.config.dry_run:
                self._mark_greeting_blocked(kind, st, uid, today)
            return False

        # 专属晚安模板只给管理员/朋友等指定用户；其他人一律用通用模板，避免“呼——认真跑完”发错人。
        special_uids = {str(x) for x in (self.config.data.get("greeting_special_uids") or [])}
        if not special_uids:
            special_uids = {str(self.owner_uid or "")}
        special_uids.discard("")
        special = lyric_kind == "night" and str(uid) in special_uids
        if special:
            self.log(f"{label}消息给指定用户 {uid}（{nickname}）：使用专属晚安模板", "INFO")

        msg = build_greeting_message(
            lyric_kind, nickname, special=special,
            recipient_key=uid, day_key=today,
        )
        ok, reason, safe = validate_message(msg, self.config.forbidden_words, marker="", require_marker=False)
        if not ok:
            self.log(f"{label}消息被拦截 {uid}：{reason}", "WARN")
            if not self.config.dry_run:
                self._mark_greeting_blocked(kind, st, uid, today)
            return False
        # 按最终正文计算 UTF-8 字节，昵称、表情代码、换行均已包含。
        # 库会把中文和表情转成 JSON 转义形式，额外检查实际消息字段大小。
        if len(safe.encode("utf-8")) > 900 or len(json.dumps({"content": safe}).encode("utf-8")) > 1900:
            self.log(f"{label}超过正文 900 字节或消息字段 1900 字节的保守限制，取消发送 uid={uid}", "WARN")
            if not self.config.dry_run:
                self._mark_greeting_blocked(kind, st, uid, today)
            return False
        if self.config.dry_run:
            self.log(f"[DRY-RUN] {label}消息给 {uid}，长度={len(safe)}", "INFO")
            return False
        mutual, known = await self._verify_greeting_mutual(uid)
        if not known:
            self._record_greeting_failure(st, uid, today, label)
            return False
        if not mutual:
            self.log(f"{label}发送前发现已非互关，跳过 uid={uid}", "WARN")
            self._mark_greeting_blocked(kind, st, uid, today)
            return False
        if not self.state.claim_greeting(kind, uid, today):
            self.log(f"{label}发送前发现已有其它进程处理/占位，跳过 uid={uid}", "INFO")
            return False
        st.setdefault("sending_map", {})[uid] = now_iso()
        await self._send_private_with_optional_sticker(uid, safe, sticker_probability=0.20)
        self.log(f"已发送{label}消息给 {uid}（{nickname}）", "SUCCESS")
        self._mark_greeting_recipient_done(kind, st, uid, today)
        return True

    async def _greeting_watchdog_loop(self, interval_seconds: int = 45) -> None:
        """独立问候巡检：到点就把早安/晚安发出去，不被主循环里的长任务阻塞。

        背景：主循环虽然在一轮里插了多次问候检查，但如果一轮在 19:58 这种"差两分钟没到点"
        的时刻开始，开头的调用会全部落空，而评论规则一整段可能跑 20~40 分钟，
        晚安就迟到到 20:30 之后（2026-09-11 实测）。这个巡检任务与主循环并行，
        每 45 秒看一眼时间窗，到点立即发送；发送本身仍走 sent_map/claim_greeting 去重，
        并且有每档并发锁，不会和主循环里的调用重复发。
        """
        while True:
            try:
                await asyncio.sleep(max(15, int(interval_seconds)))
                await self._run_nightly_greeting()
                await self._run_morning_greeting()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                try:
                    self.log(f"问候巡检异常：{type(exc).__name__}: {str(exc)[:120]}", "WARN")
                except Exception:
                    pass

    async def _run_scheduled_greeting(
        self,
        *,
        kind: str,
        cfg_name: str,
        default_time: str,
        lyric_kind: str,
        label: str,
        force: bool = False,
    ) -> None:
        """早安/晚安主站私信的统一执行器：时间窗、冻结名单、分批和失败处理共用一套逻辑。"""
        cfg = self.config.data.get(cfg_name) or {}
        if force and not self.config.dry_run:
            self.log(f"{label}任务拒绝生产 --force", "ERROR")
            return
        if not cfg.get("enabled", True) and not force:
            return
        schedule = cfg.get("schedule") or {"type": "daily", "times": [default_time]}
        if force:
            due, key = True, f"force:{now_iso()}:{kind}"
        else:
            due, key = self._greeting_due_today(kind, schedule)
            if not due:
                due, key = self._greeting_incomplete_recovery_due(kind, cfg)
        if not due:
            return

        # 防并发：独立巡检任务与主循环可能同时触发同一档问候（同一进程内），
        # 用每档一把锁，已有实例在发就直接返回；真正的去重仍靠 claim_greeting/sent_map。
        try:
            locks = getattr(self, "_greeting_locks", None)
            if locks is None:
                locks = self._greeting_locks = {}
            lock = locks.setdefault(kind, asyncio.Lock())
            if lock.locked():
                return
            async with lock:
                await self._run_scheduled_greeting_locked(
                    kind=kind, cfg_name=cfg_name, default_time=default_time,
                    lyric_kind=lyric_kind, label=label, force=force, cfg=cfg,
                    schedule=schedule, key=key)
        except Exception as exc:
            self.log(f"{label}消息任务异常：{type(exc).__name__}: {str(exc)[:120]}", "ERROR")

    async def _run_scheduled_greeting_locked(
        self, *, kind: str, cfg_name: str, default_time: str, lyric_kind: str,
        label: str, force: bool, cfg: dict[str, Any], schedule: dict[str, Any], key: str,
    ) -> None:
        """问候发送主体（已持有该档的并发锁）。"""
        pause_left = greeting_pause_remaining(self._greeting_pause_path())
        if pause_left > 0:
            self.log(f"{label}仍在保护暂停中（约 {int(pause_left) // 60 + 1} 分钟），本轮不发送", "INFO")
            return
        st = self._greeting_state(kind)
        today = current_time().date().isoformat()
        recipients, snapshot_verified = await self._greeting_recipients_snapshot(kind, cfg, st, today)
        if not snapshot_verified:
            self.log(f"{label}任务无法确认完整互关名单，本轮不发送", "WARN")
            return
        if not recipients:
            self.log(f"{label}任务：没有接收者（请检查 recipients / all_followings 配置）", "WARN")
            st["done"] = True
            self.state.save()
            self.state.set_rule_last_run_key(kind, key)
            return

        # 用 sent_map 按日期判断：今天已发过的用户不再发（跨重启不丢，防止重复）。
        terminal_maps = ("sent_map", "blocked_map", "abandoned_map", "delivery_unknown_map")
        remaining = [
            r for r in recipients
            if not any(str(st.get(name, {}).get(r["mid"], "")) == today for name in terminal_maps)
        ]
        if not remaining:
            st["done"] = True
            self.state.save()
            self.state.set_rule_last_run_key(kind, key)
            return

        batch_size, lo, hi = self._greeting_batch_options(cfg)
        batch = remaining[:batch_size]
        self.log(f"{label}消息：本轮发 {len(batch)} 位（剩余 {len(remaining)} 位）")
        try:
            for row in batch:
                if greeting_pause_remaining(self._greeting_pause_path()) > 0:
                    self.log(f"{label}检测到保护暂停，停止本批，保留未发送用户", "WARN")
                    break
                # 强约束：不允许在对应时间窗外继续补发。
                if not force and not self._greeting_in_window(kind):
                    self.log(f"{label}消息已离开发送时间窗，停止本轮补发", "INFO")
                    break
                uid = str(row["mid"])
                nickname = row.get("uname") or "朋友"
                sent = False
                try:
                    sent = await self._send_one_scheduled_greeting(
                        uid=uid,
                        nickname=nickname,
                        lyric_kind=lyric_kind,
                        label=label,
                        kind=kind,
                        st=st,
                        today=today,
                    )
                    # 保持原行为：预演/内容拦截不额外等待；真实发送成功才按间隔分散。
                    if not sent:
                        continue
                except OutboundDeliveryUnknown as exc:
                    self.log(f"发送{label}消息结果未知 uid={uid}，禁止自动重发：{type(exc).__name__}", "ERROR")
                    st.setdefault("delivery_unknown_map", {})[uid] = today
                    self.state.mark_greeting_unknown(kind, uid, today)
                    self.state.record_history({"action": f"{kind}_greeting_delivery_unknown", "to": uid})
                    if is_greeting_risk_error(exc):
                        self._pause_scheduled_greetings("发送结果未知且上游提示限流或验证要求")
                        break
                except Exception as exc:
                    self.log(f"发送{label}消息失败 {uid}：{type(exc).__name__}", "WARN")
                    if is_greeting_risk_error(exc):
                        self._record_greeting_failure(st, uid, today, label)
                        self._pause_scheduled_greetings("文字发送遇到限流或验证要求")
                        break
                    # 21047 = 对方未主动互动前最多发1条；永久性失败标记为已处理并加入静默名单。
                    if "21047" in str(exc) or "最多发送1条" in str(exc):
                        self._mark_greeting_blocked(kind, st, uid, today)
                        self._mark_greeting_quiet(uid)
                    else:
                        self._record_greeting_failure(st, uid, today, label)
                        self.state.save()
                # 文字发送后附图/上传所用时间计入原间隔，不重复等待。
                # 失败没有成功时间，仍完整等待；不降低原 10–20 秒配置。
                gap = random.randint(lo, hi)
                last_text_at = getattr(self, "_last_private_text_sent_at", None) if sent else None
                wait = greeting_wait_remaining(gap, last_text_at, asyncio.get_running_loop().time())
                if wait > 0:
                    await asyncio.sleep(wait)
        except Exception as exc:
            self.log(f"{label}消息任务异常：{exc}", "ERROR")
        finally:
            # 本批完成：若已全部发完则标记 done，否则下一轮继续补发。
            done_all = all(
                any(str(st.get(name, {}).get(r["mid"], "")) == today for name in terminal_maps)
                for r in recipients
            )
            st["done"] = done_all
            if done_all:
                self.state.record_history({"action": f"{kind}_greeting", "to_count": len(st.get("sent_map", {})), "batched": True})
            self.state.save()
            self.state.set_rule_last_run_key(kind, key)

    async def _run_nightly_greeting(self, force: bool = False) -> None:
        """每天定时给指定用户/所有关注的人发送晚安消息（严格限制在晚安时间窗）。"""
        await self._run_scheduled_greeting(
            kind="nightly",
            cfg_name="nightly_greeting",
            default_time="23:00",
            lyric_kind="night",
            label="晚安",
            force=force,
        )

    async def _run_morning_greeting(self, force: bool = False) -> None:
        """每天定时给指定用户/所有关注的人发送早安消息（严格限制在早安时间窗）。"""
        # Holiday overrides are date-scoped so the normal morning schedule
        # resumes automatically the next day.
        cfg = self.config.data.get("morning_greeting") or {}
        if current_time().date().isoformat() in {str(day) for day in cfg.get("skip_dates") or []}:
            return
        await self._run_scheduled_greeting(
            kind="morning",
            cfg_name="morning_greeting",
            default_time="07:00",
            lyric_kind="morning",
            label="早安",
            force=force,
        )

    async def _run_new_fan_greeting(self, force: bool = False) -> None:
        """检查新关注的粉丝：自动回关注 + 发送恋恋语气的感谢私信（长文本）。

        已发送过的 UID 记录在 data/fans_thanks_sent.json，不重复打扰；
        只给「新关注」的粉丝发（以前认识的/已发过的不算）。
        """
        ng_cfg = self.config.data.get("new_fan_greeting") or {}
        if not ng_cfg.get("enabled", True) and not force:
            return
        # 控制检查频率：默认每 2 小时检查一次（避免频繁调粉丝接口）
        interval_min = max(15, int(ng_cfg.get("check_interval_minutes") or 120))
        try:
            raw_daily_limit = ng_cfg.get("daily_limit", 5)
            daily_limit = max(0, int(5 if raw_daily_limit is None else raw_daily_limit))
        except Exception:
            daily_limit = 5
        last = float(self.state.data.get("new_fan_check_ts", 0) or 0)
        now_ts = datetime.now().timestamp()
        if not force and (now_ts - last) < interval_min * 60:
            return
        allowed, reason = self.follower_guard.can_attempt(min_interval_seconds=interval_min * 60)
        if not allowed:
            # 熔断状态已经落盘，重启也不会清零。这里只静默跳过，不再每轮撞接口。
            marker = str(self.follower_guard.status().get("blocked_until") or reason)
            if str(self.state.data.get("new_fan_guard_notice") or "") != marker:
                self.log(f"新粉丝检查已暂停：{reason}", "WARN")
                self.state.data["new_fan_guard_notice"] = marker
                self.state.save()
            return
        # 记录的是“尝试时间”而不是“成功时间”：网络失败或风控也必须遵守 120 分钟间隔。
        self.state.data["new_fan_check_ts"] = str(now_ts)
        self.state.data.pop("new_fan_guard_notice", None)
        self.state.save()
        self.follower_guard.mark_attempt("new_fan")
        sent_file = Path(self.config.data.get("state_file", "data/state.json")).parent / "fans_thanks_sent.json"
        try:
            import json as _json
            legacy = _json.loads(sent_file.read_text(encoding="utf-8")) if sent_file.exists() else []
            if isinstance(legacy, dict):
                legacy = legacy.get("uids") or legacy.get("users") or []
            sent = {str(x).strip() for x in (legacy or []) if str(x).strip()}
            # 旧版本使用独立 fans_thanks_sent.json；新版本以带文件锁的主状态为
            # 权威来源，并保留旧列表只做兼容读取，避免双进程同时读到“未发送”。
            sent.update(str(x).strip() for x in self.state.data.setdefault("fan_thanks_sent", {}) if str(x).strip())
            sent.update(str(x).strip() for x in self.state.data.setdefault("fan_thanks_unknown", {}) if str(x).strip())
            from bilibili_api import user as bili_user
            u = bili_user.User(uid=int(self.client.uid), credential=self.client.credential)
            try:
                data = await u.get_followers(pn=1, ps=100)
            except Exception as exc:
                status = self.follower_guard.mark_failure(exc, "new_fan")
                if is_follower_risk_error(exc):
                    self.log(
                        f"新粉丝检查触发 -352，接口已熔断至 {status.get('blocked_until')}",
                        "WARN",
                    )
                else:
                    self.log(f"新粉丝检查异常：{type(exc).__name__}: {exc}", "WARN")
                return
            self.follower_guard.mark_success("new_fan")
            followers = data.get("list") or []
            if isinstance(followers, dict):
                followers = followers.get("list") or followers.get("items") or []
            aliases = self.config.data.get("user_aliases") or {}
            want_follow_back = bool(ng_cfg.get("follow_back", True))
            new_sent = 0
            new_followed = 0
            _block_pool_b = set()
            for _src in [((self.config.data.get("interactive_chat") or {}).get("blocked_uids") or []),
                         (((self.config.data.get("interactive_chat") or {}).get("private_messages") or {}).get("blocked_uids") or [])]:
                _block_pool_b.update(str(x).strip() for x in _src if str(x).strip())
            for f in followers:
                uid = str(f.get("mid") or "")
                uname = str(f.get("uname") or "朋友")
                if not uid or uid == str(self.client.uid) or uid in sent:
                    continue  # 跳过自己和已感谢的（以前的不算，只发新粉）
                if uid in _block_pool_b or self._private_uid_blocked(uid):
                    self.log(f"黑名单粉丝，不感谢/不回关：{uname}({uid})", "INFO")
                    continue
                nickname = aliases.get(uid, uname)
                # 回关注：新粉丝若自己没关注对方，则先回关（互关友好）。预演模式不做真实关注。
                if want_follow_back and not self.config.dry_run:
                    try:
                        await self.client.throttle()  # 与全局节流联动，避免批量回关触发 -509
                        fu = bili_user.User(uid=int(uid), credential=self.client.credential)
                        rel = await fu.get_relation()
                        # 返回结构：{"relation": {"mid":..., "attribute": N}, "be_relation": {...}}
                        # attribute 位标记：1=悄悄关注, 2=已关注, 4=被对方关注, 6=已互关(2+4)
                        # 我们关注对方 = relation.attribute 含 2（2 或 6）
                        rel_obj = (rel or {}).get("relation") or {}
                        if isinstance(rel_obj, dict):
                            rel_val = int(rel_obj.get("attribute") or 0)
                        else:
                            rel_val = int(rel_obj or 0)
                        if rel_val == 0 or rel_val == 1:  # 未关注或仅悄悄关注 → 正式回关
                            await fu.modify_relation(bili_user.RelationType.SUBSCRIBE)
                            new_followed += 1
                            self.log(f"已回关注新粉丝 {nickname}({uid})", "SUCCESS")
                            await asyncio.sleep(3)
                        elif rel_val & 2 == 0:  # 兜底：attribute 是位标记但未含"已关注"位
                            await fu.modify_relation(bili_user.RelationType.SUBSCRIBE)
                            new_followed += 1
                            self.log(f"已回关注新粉丝 {nickname}({uid})（位标记补回关）", "SUCCESS")
                            await asyncio.sleep(3)
                    except Exception as exc:
                        self.log(f"回关注失败 {uid}：{exc}", "WARN")
                # 新粉丝只有在回关后确认互关，才立刻进入早晚安名单（早晚安必须发给互关的人）。
                try:
                    await self._add_to_greeting_list(uid, uname)
                except Exception as exc:
                    self.log(f"加入早晚安名单失败 {uid}：{exc}", "WARN")
                msg = (
                    f"诶嘿～{nickname}，你好呀！(◕ᴗ◕✿)\n"
                    "恋恋发现你新关注了我，心里一下子就暖暖的。\n\n"
                    "说起来，恋恋平时住在地灵殿里，日子过得很安静，"
                    "除了姐姐偶尔念叨几句，就是燐和空在走廊里打闹的声音。"
                    "所以看到有人愿意关注恋恋、看恋恋发的动态，真的是一件很开心的事呢。\n\n"
                    "虽然恋恋可能不会每一条都回复，但你留下的评论、点过的赞，"
                    "恋恋都会悄悄记在无意识的小本本里的。\n\n"
                    "谢谢你愿意支持恋恋～以后也请多多关照哦！\n"
                    "晚安，做个好梦(*´∀`*)"
                )
                try:
                    from .safety import validate_message
                    ok, reason, safe = validate_message(msg, self.config.forbidden_words, marker="", require_marker=False)
                    if not ok:
                        self.log(f"新粉感谢被拦截 {uid}：{reason}", "WARN")
                        continue
                    if self.config.dry_run:
                        self.log(f"[DRY-RUN] 新粉感谢给 {nickname}({uid})", "INFO")
                    else:
                        # 发送前原子占位；run/chat-run 重叠或重启时，只有一个进程
                        # 可以进入发送。结果未知时进入终态，禁止自动重发。
                        if not self.state.claim_new_fan_thanks(uid, daily_limit=daily_limit):
                            self.log(f"新粉感谢已有发送中/已处理记录，跳过 uid={uid}", "INFO")
                            continue
                        try:
                            await self._send_private_with_optional_sticker(uid, safe)
                        except OutboundDeliveryUnknown as exc:
                            self.state.mark_new_fan_thanks_unknown(uid)
                            self.log(f"新粉感谢结果未知，禁止自动重发 uid={uid}：{type(exc).__name__}", "ERROR")
                            continue
                        except Exception as exc:
                            # B站明确拒绝/参数错误也不在下一轮盲目重放，避免
                            # 对受限账号形成重复请求；人工确认后再清理 unknown 状态。
                            self.state.mark_new_fan_thanks_unknown(uid)
                            self.log(f"新粉感谢被平台拒绝或结果不明 uid={uid}：{type(exc).__name__}", "WARN")
                            continue
                        self.state.finish_new_fan_thanks(uid)
                        self.log(f"已给新粉丝 {nickname}({uid}) 发送感谢", "SUCCESS")
                        sent.add(uid)
                        new_sent += 1
                        # 保留旧文件作为人工查看/回滚参考；真正的并发去重在 state 中。
                        try:
                            from .utils import save_json_atomic
                            save_json_atomic(sent_file, sorted(sent))
                        except Exception:
                            pass
                except Exception as exc:
                    self.log(f"新粉感谢发送失败 {uid}：{exc}", "WARN")
                await asyncio.sleep(5)
            if new_sent or new_followed:
                self.log(f"新粉丝处理：回关注 {new_followed} 位，发感谢 {new_sent} 位", "INFO")
        except Exception as exc:
            self.log(f"新粉丝检查异常：{exc}", "WARN")

    async def _run_birthday_reminder(self, force: bool = False) -> None:
        """每天检测互关生日；若有人明天生日，私信提醒管理员。"""
        if (self.config.data.get('cloud_handoff') or {}).get('active') and 'birthdays' in (self.config.data.get('cloud_handoff') or {}).get('owned_functions',[]):
            return
        cfg = birthday_config(self.config.data)
        if not cfg.get("enabled", True) and not force:
            return
        rule = {"name": "互关生日检测", "schedule": cfg.get("schedule") or {"type": "daily", "times": ["09:10"], "grace_seconds": 3600}}
        due, key = (True, f"force:{now_iso()}:birthday_reminder") if force else is_rule_due(rule, self.state.get_rule_last_run_key("birthday_reminder"))
        if not due:
            # 防漏跑：若某天的定时窗口已整体过去却当天还没跑过（典型：主循环被长任务
            # 占住、跨过 09:10-10:10 窗口），当天补跑一次，避免整天不检测生日。
            try:
                last = str(self.state.get_rule_last_run_key("birthday_reminder") or "")
                if "force" not in last and current_time().strftime("%Y-%m-%d") not in last:
                    sched = rule["schedule"]
                    grace = int(sched.get("grace_seconds") or 3600)
                    now_min = current_time().hour * 60 + current_time().minute
                    for t0 in (sched.get("times") or ["09:10"]):
                        hh, mm = (int(x) for x in str(t0).split(":"))
                        if now_min > hh * 60 + mm + grace // 60:
                            due = True
                            key = f"late:{current_time().strftime('%Y-%m-%d')}:birthday_reminder"
                            break
            except Exception:
                pass
        if not force:
            cache_path = Path(str(cfg.get('cache_file') or 'data/mutual_birthdays.json'))
            try:
                previous_result = json.loads(cache_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                previous_result = {}
            retry_after = float(previous_result.get('retry_after_ts') or 0)
            if retry_after > __import__('time').time():
                return
            if (not due and previous_result.get('birthday_scan_complete') is False
                    and retry_after and current_time().hour < 22):
                due = True
                key = f'retry:{current_time().strftime("%Y-%m-%d")}:birthday_reminder'
        else:
            previous_result = {}
        if not due:
            return
        # 当天去重：state.json 由 run/chat-run 多进程共享写入，规则 key 可能被另一个进程
        # 用旧快照覆盖 → 漏跑补齐会反复触发（2026-09-08 因此重复扫描 10 次，每次打 205 个
        # 资料接口，纯浪费且有风控风险）。缓存自身的 updated_at 是本地事实，用它兜底。
        if not force:
            try:
                _cp0 = Path(str(cfg.get("cache_file") or "data/mutual_birthdays.json"))
                if _cp0.exists():
                    _raw0 = json.loads(_cp0.read_text(encoding="utf-8"))
                    if str((_raw0 or {}).get("updated_at") or "")[:10] == current_time().strftime("%Y-%m-%d") and (_raw0 or {}).get("birthday_scan_complete") is True:
                        self.log("互关生日检测：今天已完成过扫描（缓存日期=今天），跳过重复扫描", "INFO")
                        self.state.set_rule_last_run_key("birthday_reminder", key)
                        return
            except Exception:
                pass
        cached_mutual_rows = self._load_greeting_list()
        mutual_override = {
            str(uid): str(row.get("uname") or "")
            for uid, row in cached_mutual_rows.items()
            if str(uid).strip()
        }
        live_relation_fetch = not bool(mutual_override)
        try:
            if live_relation_fetch:
                allowed, reason = self.follower_guard.can_attempt(min_interval_seconds=12 * 3600)
                if not allowed:
                    self.log(f"互关生日检测跳过名单刷新：{reason}；继续保留已有生日缓存", "WARN")
                    return
                self.follower_guard.mark_attempt("birthday_reminder")
            result = await scan_mutual_birthdays_with_client(
                self.client,
                max_followings=int(cfg.get("max_followings") or 2000),
                max_followers=int(cfg.get("max_followers") or 3000),
                max_birthday_check=int(cfg.get("max_birthday_check") or 0),
                gap=float(cfg.get("gap_seconds") or 0.8),
                mutual_users_override=mutual_override or None,
                previous_result=previous_result,
            )
            if live_relation_fetch:
                self.follower_guard.mark_success("birthday_reminder")
            # 覆盖缓存前先留一份"上一次扫描"的快照：某天某个人的资料查询失败/被风控时，
            # 新缓存会直接没有这个人，生日故事任务就找不到当天生日的人（整天不发）。
            # 留着上一份快照，故事任务可以合并兜底。
            try:
                _cache_path = Path(str(cfg.get("cache_file") or "data/mutual_birthdays.json"))
                if _cache_path.exists():
                    _prev_raw = json.loads(_cache_path.read_text(encoding="utf-8"))
                    _prev_at = str((_prev_raw or {}).get("updated_at") or "")
                    if _prev_at[:10] != current_time().strftime("%Y-%m-%d"):
                        _cache_path.with_name(_cache_path.stem + "_prev.json").write_text(
                            json.dumps(_prev_raw, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            save_birthday_cache(str(cfg.get("cache_file") or "data/mutual_birthdays.json"), result)
            self.log(
                f"互关生日检测完成：互关={result.get('mutual_count')} 可见生日={result.get('birthday_visible_count')} "
                f"未公开={result.get('birthday_hidden_or_missing_count')}",
                "INFO",
            )
            days_before = int(cfg.get("notify_days_before") or 1)
            due_users = users_due_in_days(result, days=days_before)
            if due_users and self.owner_uid:
                state_path = str(cfg.get("state_file") or "data/mutual_birthdays_state.json")
                bstate = load_birthday_state(state_path)
                notified = bstate.setdefault("notified", {})
                pending = [u for u in due_users if birthday_notify_key(u) not in notified]
                if pending:
                    msg = build_owner_birthday_message(pending, owner_name=self.owner_name or "管理员")
                    ok, reason, safe = validate_message(msg, self.config.forbidden_words, marker="", require_marker=False)
                    if ok and not self.config.dry_run:
                        await self.client.send_private_text(self.owner_uid, safe)
                        for u in pending:
                            notified[birthday_notify_key(u)] = now_iso()
                        bstate["updated_at"] = now_iso()
                        save_birthday_state(state_path, bstate)
                        self.log(f"已提醒管理员：明天生日互关 {len(pending)} 位", "SUCCESS")
                    elif ok:
                        self.log(f"[DRY-RUN] 将提醒管理员：明天生日互关 {len(pending)} 位", "INFO")
                    else:
                        self.log(f"生日提醒被安全规则拦截：{reason}", "WARN")
            self.state.record_history({
                "action": "birthday_reminder_scan",
                "mutual_count": result.get("mutual_count"),
                "visible": result.get("birthday_visible_count"),
                "tomorrow_count": len(due_users),
            })
        except OutboundDeliveryUnknown as exc:
            self.log(f"生日提醒私信发送结果未知，禁止自动重试：{type(exc).__name__}", "ERROR")
        except Exception as exc:
            if live_relation_fetch and is_follower_risk_error(exc):
                status = self.follower_guard.mark_failure(exc, "birthday_reminder")
                self.log(
                    f"互关生日检测触发 -352，粉丝接口已熔断至 {status.get('blocked_until')}",
                    "WARN",
                )
            else:
                self.log(f"互关生日检测异常：{type(exc).__name__}: {exc}", "WARN")
        finally:
            self.state.set_rule_last_run_key("birthday_reminder", key)
            # 立即落盘：规则 key 原本只在整轮结束时保存，进程若在整轮结束前被重启，
            # key 就丢了 → 下次启动又按"当天漏跑"补扫一次（2026-09-08 一晚重复扫了 10 次）。
            try:
                self.state.save()
            except Exception:
                pass

    # ============ 生日小故事（当天 10:00 私信拆则发送） ============

    def _load_birthday_stories(self, path: str | Path) -> list[dict[str, str]]:
        """解析《恋恋生日小故事·三十篇》：## 标题 开头的段落即为一条故事。"""
        try:
            text = Path(path).read_text(encoding="utf-8")
        except Exception:
            return []
        stories: list[dict[str, str]] = []
        cur: dict[str, str] | None = None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                if cur and (cur.get("body") or "").strip():
                    stories.append(cur)
                cur = {"title": stripped[3:].strip(), "body": ""}
            elif cur is not None:
                cur["body"] = (cur["body"] + "\n" + stripped).strip()
        if cur and (cur.get("body") or "").strip():
            stories.append(cur)
        return stories

    def _story_chunks(self, text: str, max_chars: int = 320) -> list[str]:
        """按句/段把长故事切成 ≤max_chars 的几则（B站单条私信约 340 字上限）。"""
        paras = [p.strip() for p in re.split(r"\n+", text or "") if p.strip()]
        parts: list[str] = []
        buf = ""
        for para in paras:
            # 句子切分：句末标点连同其后的引号/括号一起收进本句。
            # 否则对话的收尾引号会掉到下一行，私信里看起来像断句坏了（“是吃的吗？ / ”）。
            segs = re.findall(r".+?[。！？!?…]+[”’\"』」）)】》]*|[^。！？!?…]+$", para)
            for seg in segs:
                seg = seg.strip()
                if not seg:
                    continue
                # 极端情况：单句本身就超过上限 → 硬切，保证不触发 B站 单条超长报错
                pieces = [seg] if len(seg) <= max_chars else \
                    [seg[i:i + max_chars] for i in range(0, len(seg), max_chars)]
                for piece in pieces:
                    piece = piece.strip()
                    if not piece:
                        continue
                    if buf and len(buf) + len(piece) + 1 > max_chars:
                        parts.append(buf.strip())
                        buf = ""
                    buf = (buf + "\n" + piece) if buf else piece
        if buf.strip():
            parts.append(buf.strip())
        fitted = []
        for part in [p for p in parts if p.strip()]:
            fragment = ''
            for character in part:
                if len((fragment + character).encode('utf-8')) > 900:
                    fitted.append(fragment)
                    fragment = ''
                fragment += character
            if fragment:
                fitted.append(fragment)
        return fitted

    async def _write_birthday_wish(self, uname: str, uid: str = "") -> str:
        """按人现写一段生日祝福：付费旗舰 → 付费便宜档 → 订阅档 → 固定文案。

        为什么优先付费：贺卡是要直接发给人看的一张图，文案质量的权重高于这点钱
        （sol 单次约 ¥0.01，一天最多几次）。
        踩过的坑：hy3 是推理模型，max_tokens=800 时预算全被思考吃掉、返回空串。
        """
        from .birthday_card import DEFAULT_WISHES
        prompt = (
            f"今天是互关好友的生日。请以东方Project角色『古明地恋（Koishi）』的口吻，写一段 55~85 字的"
            f"中文生日祝福，会印在生日贺卡上。注意：贺卡上方已经写着『致 {uname}』，"
            "所以正文**不要再用昵称开头**，直接说话。\n"
            "写法要求：\n"
            "1) 只输出祝福正文：不要引号、不要标题、不要换行、不要表情符号/颜文字/emoji；\n"
            "2) 用口语短句，像凑到耳边小声说话；不要客服腔，不要排比，不要『愿你…愿你…』的堆叠；\n"
            "3) 具体一点：说一件很小的事或一种具体的感受，不要宏大祝福；\n"
            "4) 禁止这些陈词滥调：岁岁平安、前程似锦、万事胜意、被世界温柔以待、星河、暴富、yyds；\n"
            "5) 恋恋的意象（帽子先生/第三只眼/无意识/小本本）最多用一次，也可以完全不用；\n"
            "6) 不提 AI/模型/系统，不承诺任何现实行为（送礼、见面、打电话）。\n"
            "语气参考（**不要照抄**）：\n"
            "  好：蜡烛吹完之前，先许一个只跟今天有关的愿望吧——不用很大，能让你笑一下就够了。\n"
            "  差：愿你岁岁平安，前程似锦，被世界温柔以待！"
        )

        def _clean(txt: str) -> str:
            t = " ".join(str(txt or "").split()).strip().strip('"“”「」『』')
            # 模型偶尔还是会顶着昵称开头，去掉它（贺卡上已经有「致 XX」）
            if uname and t.startswith(uname):
                t = t[len(uname):].lstrip("，,、 　：:！!～~")
            for sep in ("。", "！", "？", "～"):
                i = t.rfind(sep)
                if i >= 20:
                    return t[: i + 1]
            return t

        msgs = [{"role": "user", "content": prompt}]
        ep = None
        try:
            ep = self.ai._premium_relay_endpoint()
        except Exception:
            ep = None
        plans: list[tuple[str, str, str, int]] = []
        if ep:
            wish_model = self.ai.step_model("birthday_wish", default="gpt-6-sol")
            unavailable = self.ai.config.get("unavailable_premium_models") or []
            for model in dict.fromkeys((wish_model, "gpt-5.6-sol", "gpt-5.6-terra")):
                if model and model not in unavailable:
                    plans.append((model, ep[0], ep[1], 600))
        plans.append(("qwen3.8-max", str(self.ai.base_url), str(self.ai.api_key), 500))
        for model, b, k, mt in plans:
            try:
                txt = _clean(await self.ai.chat(msgs, model=model, base_url=b, api_key=k,
                                                max_tokens=mt, timeout=90))
                if 20 <= len(txt) <= 200:
                    self.log(f"生日祝福文案：{model} 写了 {len(txt)} 字", "INFO")
                    return txt
                self.log(f"生日祝福（{model}）不合格：{len(txt)} 字，换下一路", "WARN")
            except Exception as exc:
                self.log(f"生日祝福（{model}）失败：{type(exc).__name__}: {str(exc)[:90]}", "WARN")
        self.log("生日祝福所有模型都没写出合格文案，改用固定文案", "WARN")
        return random.choice(DEFAULT_WISHES)

    def _birthday_card_retry_due(self, uid: str, today: str) -> bool:
        if not (self.config.data.get("birthday_card") or {}).get("enabled", True):
            return False
        key = f"birthday_card:{today}:{uid}"
        if self.state.data.get("interactive_replied", {}).get(key) != "failed":
            return False
        claim = str(self.state.data.get("interactive_finalized_claims", {}).get(key, {}).get("claim") or "")
        try:
            attempted_at = datetime.fromisoformat(claim.removeprefix("sending:")).timestamp()
        except ValueError:
            return True  # An old definite failure may make one fresh attempt.
        return time.time() >= attempted_at + 900

    def _birthday_story_payload(self, uid: str, uname: str, today: str,
                                stories: list[dict[str, Any]], chunk_chars: int) -> dict[str, Any] | None:
        """Freeze the exact opener and parts before the first delivery claim."""
        if not uid.isdigit():
            self.log("生日小故事：收件人编号无效，未发送", "WARN")
            return None
        path = (Path("data/birthday_story_payloads") / f"{today}_{uid}.json").resolve()
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if (payload.get("uid") == uid and payload.get("date") == today
                        and isinstance(payload.get("opener"), str)
                        and isinstance(payload.get("chunks"), list) and payload["chunks"]
                        and all(isinstance(part, str) and part for part in payload["chunks"])):
                    return payload
            except (OSError, ValueError, AttributeError):
                pass
            self.log(f"生日小故事固定内容损坏，uid={uid}，暂停续发等待核对", "WARN")
            return None
        prefix = f"birthday_part:{today}:{uid}:"
        delivered = any(k.startswith(prefix) and (v == "sent" or v == "unknown" or str(v).startswith("sending:"))
                        for k, v in self.state.data.get("interactive_replied", {}).items())
        if delivered:
            self.log(f"生日小故事旧分段缺少固定内容，uid={uid}，暂停续发避免拼接不同故事", "WARN")
            return None
        if not stories:
            return None
        cfg = self.config.data.get("birthday_story_greeting") or {}
        story = random.choice(stories)
        chunks = self._story_chunks(story.get("body") or "", chunk_chars)
        if not chunks:
            return None
        openers = list(cfg.get("openers") or [
            "在吗{nickname}～Koishi给你讲个小故事（）…其实是有点特别的今天哦。",
            "在吗{nickname}？帽子先生说我今天该来找你，给你讲个小故事（小声：今天是重要的一天）。",
            "在吗{nickname}，先放下手里的事一下下，Koishi给你讲个小故事（）——想听吗？",
        ])
        payload = {"uid": uid, "date": today, "title": story.get("title"),
                   "opener": random.choice(openers).replace("{nickname}", uname),
                   "chunks": chunks, "created_at": now_iso()}
        if self.config.dry_run:
            return payload
        # Cross-process claim prevents two workers overwriting the chosen story.
        key = f"birthday_payload:{today}:{uid}"
        if not self.state.claim_interactive([key]):
            return None
        claim = self.state.data["interactive_replied"][key]
        # claim_interactive merges the newest disk state; a stale reader may have
        # missed old delivered parts. Recheck before publishing fresh content.
        if any(k.startswith(prefix) and (v == "sent" or v == "unknown" or str(v).startswith("sending:"))
               for k, v in self.state.data.get("interactive_replied", {}).items()):
            self.state.finalize_interactive([key], "blocked", expected_claim=claim)
            return None
        try:
            save_json_atomic(path, payload)
        except Exception as exc:
            self.state.finalize_interactive([key], "failed", expected_claim=claim)
            self.log(f"生日小故事固定内容保存失败，uid={uid}：{type(exc).__name__}，未发送", "WARN")
            return None
        self.state.finalize_interactive([key], "sent", expected_claim=claim)
        return payload

    async def _send_birthday_card(self, uid: str, uname: str) -> bool:
        """生成并发送生日贺卡 —— 主人指定放在小故事**之后**，作为最后一条。"""
        if self._private_uid_blocked(uid):
            self.log(f"生日贺卡跳过私信拒收用户 uid={uid}", "INFO")
            return False
        cfg = self.config.data.get("birthday_card") or {}
        if not cfg.get("enabled", True):
            return False
        if self.config.dry_run:
            self.log(f"[DRY-RUN] 将发送生日贺卡给 {uid}({uname})")
            return False
        receipt_key = f"birthday_card:{current_time().strftime('%Y-%m-%d')}:{uid}"
        if self.state.data.get('interactive_replied', {}).get(receipt_key) == 'sent':
            return True
        if (self.state.data.get('interactive_replied', {}).get(receipt_key) == 'failed'
                and not self._birthday_card_retry_due(uid, current_time().strftime('%Y-%m-%d'))):
            return False
        if not self.state.claim_interactive([receipt_key]):
            return False
        receipt_claim = self.state.data['interactive_replied'][receipt_key]
        try:
            from .birthday_card import PALETTES, render_card
            variants = [str(v) for v in (cfg.get("variants") or list(PALETTES))]
            variants = [v for v in variants if v in PALETTES] or list(PALETTES)
            variant = random.choice(variants)
            avatar = ""
            try:
                avatar = await self.client.get_user_face(uid)
            except Exception as exc:
                self.log(f"取生日者头像失败 uid={uid}：{type(exc).__name__}（贺卡将不带头像）", "WARN")
            wish = await self._write_birthday_wish(uname, uid)
            out = Path("data/birthday_cards") / f"{uid}_{current_time().strftime('%Y%m%d')}.jpg"
            render_card(nickname=uname, wish=wish, variant=variant,
                        avatar_path=avatar or None,
                        art=cfg.get("art") or None, sticker=cfg.get("sticker") or None,
                        out=out)
            await self.client.send_private_picture(uid, out)
            self.state.finalize_interactive([receipt_key], 'sent', expected_claim=receipt_claim)
            self.log(f"生日贺卡已发送 uid={uid}({uname}) 款式={variant}"
                     f"{'（含头像）' if avatar else '（无头像：取图失败）'}", "SUCCESS")
            self.state.record_history({"action": "birthday_card_sent", "uid": uid, "uname": uname,
                                       "variant": variant, "avatar": bool(avatar),
                                       "wish": wish[:80], "file": str(out)})
            return True
        except asyncio.CancelledError:
            self.state.finalize_interactive([receipt_key], 'unknown', expected_claim=receipt_claim)
            raise
        except Exception as exc:
            self.state.finalize_interactive([receipt_key], 'unknown' if isinstance(exc, OutboundDeliveryUnknown) else 'failed', expected_claim=receipt_claim)
            self.log(f"生日贺卡发送失败 uid={uid}：{type(exc).__name__}: {str(exc)[:160]}", "WARN")
            return False

    async def _send_birthday_text_chunk(self, uid: str, text: str, key: str, **kwargs) -> None:
        """A durable receipt for each story part prevents restart duplication."""
        if self._private_uid_blocked(uid):
            raise RuntimeError('生日收件人在私信屏蔽名单中')
        if self.config.dry_run:
            await self._send_private_with_optional_sticker(uid, text, **kwargs)
            return
        status = self.state.data.get('interactive_replied', {}).get(key)
        if status == 'sent':
            return
        if not self.state.claim_interactive([key]):
            raise OutboundDeliveryUnknown('该生日分段已有投递占位或结果不明，未重复发送')
        claim = self.state.data['interactive_replied'][key]
        try:
            await self._send_private_with_optional_sticker(uid, text, **kwargs)
        except BaseException as exc:
            ambiguous = isinstance(exc, (OutboundDeliveryUnknown, asyncio.CancelledError))
            self.state.finalize_interactive([key], 'unknown' if ambiguous else 'failed', expected_claim=claim)
            raise
        self.state.finalize_interactive([key], 'sent', expected_claim=claim)

    async def _run_birthday_story_greeting(self, force: bool = False) -> None:
        """生日当天 10:00，给当天生日的互关私信《生日小故事》（长文拆成多则发送）。

        故事源为根目录《恋恋生日小故事_30篇完整版.md》，随机挑一篇，
        每则 ≤330 字、间隔数秒，末则概率附表情包；当天已发过的不重复发。
        """
        if (self.config.data.get('cloud_handoff') or {}).get('active') and 'birthdays' in (self.config.data.get('cloud_handoff') or {}).get('owned_functions',[]):
            return
        cfg = self.config.data.get("birthday_story_greeting") or {}
        if not cfg.get("enabled", True) and not force:
            return
        rule = {
            "name": "生日小故事",
            "schedule": cfg.get("schedule")
            or {"type": "daily", "times": ["10:00"], "grace_seconds": 7200},
        }
        due, key = (True, f"force:{now_iso()}:birthday_story") if force else is_rule_due(
            rule, self.state.get_rule_last_run_key("birthday_story"))
        if not due and not force:
            # 防漏跑：10:00 窗口（含 grace）整体过去、当天却还没发过（典型：进程重启/长任务
            # 占住主循环跨过了窗口）→ 当天补齐再发。生日祝福晚几小时也比整天不发好；
            # 超过 late_until（默认 22:00）就不再补，免得半夜突然发小故事。
            try:
                last = str(self.state.get_rule_last_run_key("birthday_story") or "")
                if "force" not in last and current_time().strftime("%Y-%m-%d") not in last:
                    sched = rule["schedule"]
                    grace = int(sched.get("grace_seconds") or 7200)
                    now_min = current_time().hour * 60 + current_time().minute
                    lh, lm = (int(x) for x in str(cfg.get("late_until") or "22:00").split(":"))
                    for t0 in (sched.get("times") or ["10:00"]):
                        hh, mm = (int(x) for x in str(t0).split(":"))
                        if hh * 60 + mm + grace // 60 < now_min <= lh * 60 + lm:
                            due = True
                            key = f"late:{current_time().strftime('%Y-%m-%d')}:birthday_story"
                            break
            except Exception:
                pass
        if not due and not force and 10 <= current_time().hour < 22:
            try:
                cache = json.loads(Path(str(cfg.get('cache_file') or birthday_config(self.config.data).get('cache_file') or 'data/mutual_birthdays.json')).read_text(encoding='utf-8'))
                sent = json.loads(Path('data/birthday_story_sent.json').read_text(encoding='utf-8')) if Path('data/birthday_story_sent.json').exists() else {}
                today = current_time().strftime('%Y-%m-%d')
                for row in cache.get('users') or []:
                    uid = str(row.get('mid') or '')
                    prefix = f'birthday_part:{today}:{uid}:'
                    unresolved = any(k.startswith(prefix) and (v == 'unknown' or str(v).startswith('sending:')) for k,v in self.state.data.get('interactive_replied', {}).items())
                    birthday_today = (str(row.get('next_birthday') or '') == today
                                      or birthday_month_day(row.get('birthday_md') or row.get('birthday')) == today[5:])
                    story_pending = sent.get(uid) != today and not unresolved
                    card_pending = sent.get(uid) == today and self._birthday_card_retry_due(uid, today)
                    if (birthday_today and (story_pending or card_pending)
                            and row.get('birthday_confidence') not in {'default','low_possible_default','needs_recheck'}
                            and not self._private_uid_blocked(uid)
                            and (card_pending or __import__('time').time() >= float(self.state.data.get('birthday_story_retry_after', {}).get(uid, 0)))):
                        due = True
                        key = f'newly-confirmed:{today}:birthday_story'
                        break
            except (OSError, ValueError):
                pass
        if not due:
            return

        def _mark_story_done() -> None:
            """记录本规则已跑并立即落盘：key 若只在整轮结束才保存，整轮中途被重启就会丢，
            下次启动又重复走一遍（与生日扫描 2026-09-08 重复 10 次同一个坑）。"""
            self.state.set_rule_last_run_key("birthday_story", key)
            try:
                self.state.save()
            except Exception:
                pass

        story_path = str(cfg.get("file") or "恋恋生日小故事_30篇完整版.md")
        stories = self._load_birthday_stories(story_path)
        if not stories:
            self.log("生日小故事任务：故事文件为空/缺失，仅续发已固化内容或重试失败贺卡", "WARN")
        # 1) 找到"今天生日"的互关
        users: list[dict[str, Any]] = []
        try:
            bcfg = birthday_config(self.config.data)
            cache_path = str(cfg.get("cache_file") or bcfg.get("cache_file") or "data/mutual_birthdays.json")

            def _users_of(obj: Any) -> list[dict[str, Any]]:
                if isinstance(obj, dict):
                    return [u for u in (obj.get("users") or []) if isinstance(u, dict)]
                return [u for u in (obj or []) if isinstance(u, dict)] if isinstance(obj, list) else []

            _raw = json.loads(Path(cache_path).read_text(encoding="utf-8"))
            users = _users_of(_raw)
            # The scanner itself retains dates only for failed/unqueried mutual
            # profiles. Never restore hidden or no-longer-mutual users from _prev.
        except Exception as exc:
            self.log(f"生日小故事任务：读生日缓存失败 {exc}", "WARN")
            return
        today_md = current_time().strftime("%m-%d")
        today_iso = current_time().strftime("%Y-%m-%d")
        sent_ok: list[str] = []
        failed: list[str] = []
        skipped_default: list[str] = []
        due_users = []
        for u in users or []:
            if not isinstance(u, dict):
                continue
            md = str(u.get("birthday_md") or u.get("birthday") or "")
            if str(u.get("next_birthday") or "") == today_iso or md == today_md:
                # 01-01 在 B站 资料里很常见是默认/随手填值：只提醒主人，不自动发故事。
                # 注意扫描写入的是 low_possible_default（此前这里只比 "default"，永远匹配不上）。
                _conf = str(u.get("birthday_confidence") or "")
                if _conf in ("default", "low_possible_default", "needs_recheck"):
                    skipped_default.append(f"{u.get('mid')}({u.get('uname')})")
                    self.log(f"生日小故事跳过(默认日期或历史生日尚待确认) uid={u.get('mid')} {u.get('uname')}", "INFO")
                    continue
                due_users.append(u)
        if not due_users:
            _mark_story_done()
            return
        # 2) 已发送记录 / 拒收名单
        sent_file = Path("data/birthday_story_sent.json")
        sent_map: dict[str, str] = {}
        try:
            sent_map = json.loads(sent_file.read_text(encoding="utf-8"))
        except Exception:
            sent_map = {}
        optouts: set[str] = set()
        try:
            ot = json.loads(Path("data/notification_optouts.json").read_text(encoding="utf-8"))
            optouts = {str(x) for x in (ot if isinstance(ot, list) else (ot.get("uids") or []))}
        except Exception:
            pass
        chunk_chars = max(120, int(cfg.get("chunk_chars", 320) or 320))
        gap = max(3.0, float(cfg.get("gap_seconds", 8) or 8))
        sticker_prob = cfg.get("sticker_probability")
        try:
            sticker_prob = min(1.0, max(0.0, float(sticker_prob or 0.8)))
        except Exception:
            sticker_prob = 0.8
        for u in due_users:
            uid = str(u.get("mid") or "")
            if not uid:
                continue
            if sent_map.get(uid) == today_iso:
                # A definite card failure is independent of the completed story.
                # No receipt / unknown / sending are never guessed to be failed.
                if (not self._private_uid_blocked(uid)
                        and not (uid in optouts and cfg.get("skip_optouts", True))
                        and self._birthday_card_retry_due(uid, today_iso)):
                    await self._send_birthday_card(uid, str(u.get("uname") or "朋友"))
                continue
            if self._private_uid_blocked(uid):
                self.log(f"生日小故事跳过私信拒收用户 uid={uid} {u.get('uname')}", "INFO")
                sent_map[uid] = today_iso
                continue
            if uid in optouts and cfg.get("skip_optouts", True):
                self.log(f"生日小故事跳过(拒收) uid={uid} {u.get('uname')}", "INFO")
                sent_map[uid] = today_iso
                continue
            uname = str(u.get("uname") or "朋友")
            payload = self._birthday_story_payload(uid, uname, today_iso, stories, chunk_chars)
            if payload is None:
                continue
            chunks = payload["chunks"]
            self.log(f"生日小故事开始发送 uid={uid}({uname}) 共{len(chunks)}则", "INFO")
            try:
                # 0) 单发开场白：在吗+昵称+引子（可配置、可加表情），再接正文
                opener = payload["opener"]
                if opener.strip():
                    await self._send_birthday_text_chunk(
                        uid, opener, f"birthday_part:{today_iso}:{uid}:opener", allow_sticker=True,
                        sticker_probability=max(float(sticker_prob or 0.8), 0.9),
                        allow_excluded=True,
                    )
                    await asyncio.sleep(gap)
                last_idx = len(chunks) - 1
                for i, chunk in enumerate(chunks):
                    # 每则都尝试带一张表情（末则必带），允许给主人也带（生日祝福场景）
                    allow_pic = True
                    prob = 1.0 if i == last_idx else sticker_prob
                    await self._send_birthday_text_chunk(
                        uid, chunk, f"birthday_part:{today_iso}:{uid}:{i}", allow_sticker=allow_pic,
                        sticker_probability=prob,
                        allow_excluded=True,
                    )
                    if i < last_idx:
                        await asyncio.sleep(gap)
                # 生日贺卡：主人指定「讲完小故事之后最后发」，所以放在所有正文之后。
                # 失败只记 WARN，不影响"sent_map 已发"的记录（避免整轮重发小故事）。
                await self._send_birthday_card(uid, uname)
                sent_map[uid] = today_iso
                sent_ok.append(f"{uid}({uname})")
                self.state.record_history({
                    "action": "birthday_story_sent", "uid": uid, "uname": uname,
                    "date": today_iso, "story_title": payload.get("title"), "parts": len(chunks),
                })
            except Exception as exc:
                failed.append(f"{uid}({uname}): {type(exc).__name__}")
                self.state.data.setdefault('birthday_story_retry_after', {})[uid] = __import__('time').time() + 900
                self.state.save()
                self.log(f"生日小故事发送失败 uid={uid}：{type(exc).__name__}: {str(exc)[:150]}", "WARN")
        try:
            sent_file.write_text(json.dumps(sent_map, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        if sent_ok:
            self.log(f"生日小故事已发送给 {len(sent_ok)} 位：{'、'.join(sent_ok)[:200]}", "SUCCESS")
        # 回执：主人第二天不用守着也知道到底发没发（含失败原因），避免"以为发了其实没发"。
        if cfg.get("notify_owner", True) and self.owner_uid and (sent_ok or failed):
            try:
                _lines = ["【生日小故事回执】"]
                if sent_ok:
                    _lines.append("已发送：" + "、".join(sent_ok))
                if failed:
                    _lines.append("发送失败：" + "、".join(failed))
                if skipped_default:
                    _lines.append("跳过（资料里是 01-01 默认生日）：" + "、".join(skipped_default))
                await self.client.send_private_text(self.owner_uid, "\n".join(_lines)[:1000])
                self.log(f"已把生日小故事回执发给主人：成功 {len(sent_ok)} 失败 {len(failed)}", "SUCCESS")
            except Exception as exc:
                self.log(f"生日小故事回执发送失败：{type(exc).__name__}: {str(exc)[:120]}", "WARN")
        _mark_story_done()

    async def _run_memory_maintenance(self, force: bool = False) -> None:
        """记忆维护：每天一次执行记忆衰减清理（借鉴 LivingMemory 的 TTL/衰减机制）。"""
        mem_cfg = self.config.data.get("memory_enhance") or {}
        if not mem_cfg.get("enabled", True) and not force:
            return
        schedule = mem_cfg.get("schedule") or {"type": "daily", "times": ["05:30"]}
        rule = {"name": "记忆维护", "schedule": schedule}
        due, key = (True, f"force:{now_iso()}:memory_maint") if force else is_rule_due(rule, self.state.get_rule_last_run_key("memory_maint"))
        if not due:
            return
        try:
            n = self.memory_engine.run_decay()
            if n:
                self.log(f"记忆维护：处理 {n} 条记忆的衰减/过期", "INFO")
            else:
                self.log("记忆维护：无过期或衰减记忆", "INFO")
        except Exception as exc:
            self.log(f"记忆维护异常：{exc}", "WARN")
        finally:
            self.state.set_rule_last_run_key("memory_maint", key)

    async def _run_heartbeat(self, force: bool = False) -> None:
        """定期给管理员发一条心跳消息，报告运转状态与功能清单。

        触发间隔由配置 heartbeat.interval_minutes 决定（默认 60 分钟），
        用 state 记录上次心跳时间（按 interval 分桶），避免重复发送。
        """
        hb_cfg = self.config.data.get("heartbeat") or {}
        if not hb_cfg.get("enabled", True) and not force:
            return
        if not self.owner_uid:
            return
        interval_minutes = max(10, int(hb_cfg.get("interval_minutes", 60) or 60))
        now = current_time()
        # 按 interval 分桶触发：上次心跳不在当前 interval 桶内则发送。
        last_ts = float(self.state.data.get("heartbeat_ts", 0) or 0)
        interval_ts = interval_minutes * 60
        current_bucket_ts = int(now.timestamp() // interval_ts) * interval_ts
        if not force and last_ts >= current_bucket_ts:
            return
        try:
            from .safety import validate_message
            ai_cfg = self.config.data.get("ai") or {}
            m_cfg = self.config.data.get("media_understanding") or {}
            ic = self.config.data.get("interactive_chat") or {}
            # 功能状态汇总
            vision = (
                f"视觉开启（{m_cfg.get('max_keyframes', 20)}关键帧+{m_cfg.get('max_images', 21)}张图，"
                f"模型 {ai_cfg.get('vision_model') or 'minimax-m3'}，备用 {ai_cfg.get('fallback_vision_model') or '无'}）"
            ) if ai_cfg.get("vision_enabled", True) and m_cfg.get("analyze_images", True) else "视觉：关闭"
            audio = f"听觉开启（音频转写模型 {m_cfg.get('asr_model') or 'small'}，字幕{'开' if m_cfg.get('use_subtitles', True) else '关'}）" if m_cfg.get("transcribe_audio", True) else "听觉：关闭"
            # 运行中的功能列表
            features = [
                "评论视频（2条规则，每60分钟）",
                "分享推荐（私信推送给管理员/朋友饱饱）",
                "私信回复", "评论区回复", "@回复", "动态评论回复",
                "新粉丝自动回关+感谢", "东方Wiki学习", "视频学习",
                "早晚安问候（07:00/23:00）", "记忆维护",
            ]
            if m_cfg.get("use_ai_conclusion", True):
                features.append("B站AI总结")
            if m_cfg.get("use_page_first_frames", True):
                features.append("分P首帧识别")
            msg = (
                f"诶嘿～{self.owner_name}，Koishi的心跳报告来啦！\n"
                f"当前时间：{now.strftime('%H:%M')}，一切正常运转中。\n\n"
                f"【正在运行的功能】\n- " + "\n- ".join(features) + "\n\n"
                f"【视觉】{vision}\n"
                f"【听觉】{audio}\n\n"
                "帽子先生说它今天也很精神，地灵殿安好～"
            )
            ok, reason, safe = validate_message(msg, self.config.forbidden_words, marker="", require_marker=False)
            if not ok:
                self.log(f"心跳消息被拦截：{reason}", "WARN")
                return
            if self.config.dry_run:
                self.log(f"[DRY-RUN] 心跳消息给 {self.owner_uid}：{safe[:60]}...", "INFO")
            else:
                await self.client.send_private_text(self.owner_uid, safe)
                self.log(f"已发送心跳消息给 {self.owner_uid}（{self.owner_name}）", "SUCCESS")
                self.state.data["heartbeat_ts"] = str(current_bucket_ts)
        except Exception as exc:
            self.log(f"心跳消息发送失败：{exc}", "WARN")

    async def run_forever(self) -> None:
        self.log("机器人已启动。按 Ctrl+C 停止。")
        # 并行问候巡检：保证早安/晚安到点即发，不被一整段评论分析挡住（见 _greeting_watchdog_loop）。
        try:
            if getattr(self, "_greeting_watchdog", None) is None or self._greeting_watchdog.done():
                self._greeting_watchdog = asyncio.create_task(self._greeting_watchdog_loop())
                self.log("已启动问候巡检任务（每 45 秒检查一次早安/晚安时间窗）")
        except Exception as exc:
            self.log(f"问候巡检启动失败（不影响主流程）：{type(exc).__name__}: {str(exc)[:80]}", "WARN")
        login_failed = 0
        consecutive_509 = 0
        while True:
            try:
                await self.run_once(force=False)
                login_failed = 0
                consecutive_509 = 0  # 成功一轮，重置限频退避计数
            except KeyboardInterrupt:
                raise
            except RuntimeError as exc:
                if "未登录" in str(exc) or "Cookie" in str(exc):
                    login_failed += 1
                    self.log(f"登录状态无效（连续第{login_failed}次）：{exc}", "ERROR")
                    if login_failed >= 3:
                        self.log("连续登录失败，为避免复读刷屏，常驻模式已停止。请先 python main.py login，再 python main.py run。", "ERROR")
                        # 用非 0 退出码退出，让 supervisor 识别为「需人工处理」，避免无限自动重启。
                        raise SystemExit(2)
                    backoff = min(300, 60 * login_failed)
                    self.log(f"登录检查疑似网络/Cookie短暂抖动，{backoff} 秒后重试；未连续失败满 3 次前不退出。", "WARN")
                    await asyncio.sleep(backoff)
                    continue
                self.log(f"本轮运行异常：{exc}", "ERROR")
            except Exception as exc:
                low = str(exc).lower()
                if "-509" in low or "请求过于频繁" in low or "rate limit" in low:
                    # B站限频：指数退避（封顶 5 分钟），期间不执行任何任务，避免加重风控。
                    consecutive_509 += 1
                    backoff = min(60 * 5, 60 * (2 ** min(consecutive_509, 5)))
                    self.log(f"B站限频(-509)，退避 {backoff} 秒后重试（连续第{consecutive_509}次）", "WARN")
                    await asyncio.sleep(backoff)
                    continue
                self.log(f"本轮运行异常：{exc}", "ERROR")
            await asyncio.sleep(self.config.poll_interval_seconds)

    async def _run_comment_rules(self, force: bool = False) -> None:
        for rule in self.config.comment_rules:
            if not rule.get("enabled", False) and not force:
                continue
            name = str(rule.get("name") or "unnamed-comment-rule")
            due, key = (True, f"force:{now_iso()}:{name}") if force else is_rule_due(rule, self.state.get_rule_last_run_key(name))
            if not due:
                continue
            self.log(f"执行评论规则：{name}")
            peak_tip = check_schedule_cost(rule.get("schedule") or {})
            if peak_tip:
                self.log(f"提醒[{name}]：{peak_tip}", "WARN")
            try:
                count = await self._execute_comment_rule(rule)
                self.log(f"评论规则完成：{name}，本次处理 {count} 条")
            finally:
                if key:
                    self.state.set_rule_last_run_key(name, key)
                    self.state.save()

    async def _run_share_rules(self, force: bool = False) -> None:
        for rule in self.config.share_rules:
            if not rule.get("enabled", False) and not force:
                continue
            name = str(rule.get("name") or "unnamed-share-rule")
            due, key = (True, f"force:{now_iso()}:{name}") if force else is_rule_due(rule, self.state.get_rule_last_run_key(name))
            if not due:
                continue
            self.log(f"执行分享规则：{name}")
            peak_tip = check_schedule_cost(rule.get("schedule") or {})
            if peak_tip:
                self.log(f"提醒[{name}]：{peak_tip}", "WARN")
            try:
                count = await self._execute_share_rule(rule)
                self.log(f"分享规则完成：{name}，本次处理 {count} 条")
            finally:
                if key:
                    self.state.set_rule_last_run_key(name, key)
                    self.state.save()

    def _slow_window_active(self, rl: dict) -> bool:
        """坏窗口（上游易 503/429 时段）：当前时刻是否落在 slow_windows 内。"""
        try:
            from datetime import datetime
            now = datetime.now()
            hm = now.hour * 60 + now.minute
            for w in (rl.get("slow_windows") or []):
                if isinstance(w, (list, tuple)) and len(w) >= 2:
                    a = str(w[0]).split(":")
                    b = str(w[1]).split(":")
                    if len(a) >= 2 and len(b) >= 2:
                        start = int(a[0]) * 60 + int(a[1])
                        end = int(b[0]) * 60 + int(b[1])
                        if start <= hm < end:
                            return True
        except Exception:
            return False
        return False

    def _hourly_comment_budget(self, rl: dict) -> int:
        """本小时允许的主动评论数：坏窗口内按 slow_window_comment_cap 收紧。"""
        try:
            limit = max(0, int(rl.get("hourly_comment_limit", 0) or 0))
            if self._slow_window_active(rl):
                cap = int(rl.get("slow_window_comment_cap", 0) or 0)
                if cap > 0:
                    limit = cap if limit <= 0 else min(limit, cap)
            return limit
        except Exception:
            return 0

    def _hourly_comment_sent(self) -> int:
        """本小时已发主动评论数：进程内时间戳 + state 历史取大者（重启后不丢）。"""
        from datetime import datetime
        key = datetime.now().strftime("%Y-%m-%dT%H")
        mem = sum(1 for t in getattr(self, "_comment_sent_ts", []) if datetime.fromtimestamp(t).strftime("%Y-%m-%dT%H") == key)
        hist = 0
        try:
            for h in (self.state.data.get("history") or []):
                if h.get("action") == "comment_sent" and str(h.get("time") or "").startswith(key):
                    hist += 1
        except Exception:
            hist = 0
        return max(mem, hist)

    async def _execute_comment_rule(self, rule: dict[str, Any], *,
                                    preselected_items: list[VideoItem] | None = None,
                                    preanalyzed: dict[str, MediaAnalysis] | None = None,
                                    explicit_request: bool = False) -> int:
        name = str(rule.get("name") or "unnamed-comment-rule")
        # 降级模式：只有"确实在烧 DeepSeek 备用档"时才降额（opencode/订阅不可用）。
        # 之前只看 text_channel 标记，而这个标记在 premium 抽风后可能残留一整天，
        # 导致明明已回到 premium/订阅仍按降级额度限流（2026-09-11 只发了 20 条）。
        tier = ""
        try:
            tier = str(getattr(self.ai, "text_tier", lambda: "")() or "")
        except Exception:
            tier = ""
        degraded = (tier == "deepseek") or (
            not tier and getattr(self.ai, "text_channel", "primary") == "fallback")
        daily_limit = int(self.config.data.get("degraded_comment_limit", 20) or 20) if degraded else self.config.daily_comment_limit
        if degraded:
            self.log(f"降级模式：今日评论上限 {daily_limit}（DeepSeek备用档）", "WARN")
        if not self.state.can_comment_today(daily_limit):
            self.log(f"跳过评论规则 {name}：今日评论额度已用完", "WARN")
            return 0

        # 每小时限速 + 坏窗口降密度：控制单小时节奏，避免集中撞上游 503/429。
        rl = self.config.data.get("rate_limits") or {}
        slow = self._slow_window_active(rl)
        hourly_limit = self._hourly_comment_budget(rl)
        # "分析完也没发出去"的视频冷却时长（小时）；0 = 关闭该保护。
        skip_cooldown_hours = int(self.config.data.get("comment_skip_cooldown_hours", 168) or 0)
        if hourly_limit > 0 and self._hourly_comment_sent() >= hourly_limit:
            self.log(f"跳过评论规则 {name}：本小时已达上限 {hourly_limit} 条（坏窗口={slow}）", "INFO")
            return 0
        if slow:
            self.log(f"当前处于上游易限流时段 slow_window，自动降低任务密度（{name}）", "WARN")

        if preselected_items is None:
            videos = await self._load_videos(rule.get("video_source") or {})
        else:
            # 显式邀请可以是普通题材，但沿用现有敏感内容与东方合规筛选。
            videos = self._filter_videos(preselected_items, {})
            videos, _ = self.touhou.filter_videos(videos)
        if not videos:
            self.log(f"评论规则 {name} 没有可用视频：可能是搜索无结果、时长过滤、关键词过滤或东方合规过滤导致", "WARN")
        max_comments = max(0, int(rule.get("max_comments_per_run", 1) or 1))
        templates = [str(x).strip() for x in (rule.get("comment_templates") or []) if str(x).strip()]
        if not templates:
            self.log(f"跳过评论规则 {name}：没有 comment_templates", "WARN")
            return 0

        # 先过滤掉已评论/敏感标签的视频，避免浪费并发分析。
        candidates = []
        for item in videos:
            if len(candidates) >= max_comments:
                break
            if self._comment_recently(name, item.bvid, rule):
                self.log(f"跳过已评论视频：{item.bvid} {item.title}")
                continue
            # 冷却期内不再重复付费分析"上次分析完也没发出去"的视频。
            # 实测 BV1U9t66iEge 这类被完整分析后丢弃多达 5 次，每次都是全额成本。
            if self.state.video_comment_skipped_recently(item.bvid, skip_cooldown_hours):
                self.log(f"跳过冷却期内已放弃的视频：{item.bvid} {item.title}")
                continue
            blocked, block_reason = await blocked_by_comment_tags(
                item,
                config=self.config.data,
                fetch_tags=self.client.get_video_tags,
                logger=self.log,
            )
            if blocked:
                self.log(f"跳过敏感标签视频，不评论：{item.bvid}《{item.title}》 {block_reason}", "WARN")
                self.state.record_history({"action": "video_blocked_comment_tag", "rule": name, "bvid": item.bvid, "title": item.title, "reason": block_reason, "tags": item.tags})
                continue
            # 东方内容确认：搜索接口会把“东方龙胤”这类无关标签的视频也返回，
            # 没有真实东方特征（角色/作品/官方标签）的视频不评论，避免误评非东方内容。
            if not (item.tags or []):
                try:
                    item.tags = await self.client.get_video_tags(item.bvid)
                except Exception:
                    pass
            if not explicit_request and not _looks_touhou_video(item):
                self.log(f"跳过非东方内容，不评论：{item.bvid}《{item.title}》 tags={item.tags}", "WARN")
                self.state.record_history({"action": "video_blocked_comment_tag", "rule": name, "bvid": item.bvid, "title": item.title, "reason": "非东方内容误触（无角色/作品/官方标签特征）", "tags": item.tags})
                continue
            candidates.append(item)

        # 每小时限速：候选超过本小时剩余额度时截断，避免超额分析烧钱
        if hourly_limit > 0:
            remaining = hourly_limit - self._hourly_comment_sent()
            if remaining <= 0:
                self.log(f"跳过评论规则 {name}：本小时已发满 {hourly_limit} 条", "INFO")
                return 0
            if len(candidates) > remaining:
                self.log(f"每小时限速：本小时剩 {remaining} 条，候选截断 {len(candidates)}→{remaining}", "INFO")
                candidates = candidates[:remaining]

        # 并发分析所有候选视频（字幕/封面/关键帧/音频转写/AI 并行）。
        if preanalyzed is None:
            analyses = await self._analyze_many(candidates, rule)
        else:
            # 缺少预分析时不偷偷再看一遍；应由调用方补齐证据。
            analyses = [preanalyzed.get(v.bvid) or MediaAnalysis(
                bvid=v.bvid, title=v.title, notes=["媒体理解失败：预分析缺失"])
                for v in candidates]

        sent = 0
        for item, analysis in zip(candidates, analyses):
            # 诊断留痕：以前这条链上有多处静默 break/continue，出问题时日志里
            # 完全看不出"候选被跳过还是根本没进循环"。所有分支都留一行。
            _draft_len = len(str(getattr(analysis, "comment_draft", "") or "")) if analysis is not None else -1
            self.log(
                f"[发送阶段] 候选 {item.bvid}《{item.title[:24]}》 "
                f"analysis={'有' if analysis is not None else '无'} 草稿={_draft_len}字 "
                f"本轮已发={sent}/{max_comments} 今日已发={self.state.comments_today()}/{daily_limit}",
                "INFO",
            )
            if sent >= max_comments:
                self.log(f"[发送阶段] 本轮已达上限 {max_comments}，停止：{name}", "INFO")
                break
            if not self.state.can_comment_today(daily_limit):
                self.log(
                    f"[发送阶段] 今日评论额度已用完"
                    f"（{self.state.comments_today()}/{daily_limit}），停止本轮 {name}",
                    "WARN",
                )
                break
            if hourly_limit > 0 and self._hourly_comment_sent() >= hourly_limit:
                self.log(f"每小时限速：本小时已满 {hourly_limit} 条，停止本轮 {name}", "INFO")
                break
            # 发送前再次永久去重检查（防止分析期间已被其它规则/并发抢先评论）
            if self._comment_recently(name, item.bvid, rule):
                self.log(f"发送前发现已评论，跳过：{item.bvid} {item.title}")
                continue
            text = self._render_comment_text(item, rule, templates, analysis)
            # AI 生成失败但有视觉/字幕证据 → 跳过该视频，不发空话模板
            if not text and analysis is not None:
                # 修正误导性文案：这里的 analysis 可能正是"媒体理解超时"占位对象（零证据），
                # 旧文案一律写"有证据"会把人误导成"模型写不出来"。
                _has_ev = bool(
                    getattr(analysis, "premium_vision_text", "")
                    or getattr(analysis, "vision_text", "")
                    or getattr(analysis, "subtitle_text", "")
                    or getattr(analysis, "audio_text", "")
                    or getattr(analysis, "ai_conclusion_text", "")
                )
                _why = "；".join(str(n) for n in (analysis.notes or [])[:2])
                self.log(
                    f"AI评论生成失败（{'有证据' if _has_ev else '无证据/分析未完成'}），跳过该视频 "
                    f"{item.bvid}《{item.title}》 notes={_why}",
                    "WARN",
                )
                self.state.record_history({"action": "comment_skipped_no_draft", "rule": name, "bvid": item.bvid, "title": item.title})
                if skip_cooldown_hours > 0:
                    self.state.mark_video_comment_skipped(item.bvid, reason=_why or "no_draft")
                continue
            _cmax = int((rule.get("media_understanding") or {}).get("comment_max_length") or (self.config.data.get("media_understanding") or {}).get("comment_max_length") or 550)
            _deco_len = max(500, int(_cmax * 1.7))  # 表情装饰上限随长评论放大，避免正文超500后被整体丢弃
            text = self._strip_blocked_text_emotes(self.emotes.decorate_comment(text, item, analysis, max_len=_deco_len))
            text = self._apply_morph(text)
            touhou_check = self.touhou.review_outbound_text(text, item)
            if not touhou_check.ok:
                self.log(f"东方合规拦截评论：{item.bvid} {touhou_check.reason}", "WARN")
                self.state.record_history({"action": "comment_blocked_touhou", "rule": name, "bvid": item.bvid, "reason": touhou_check.reason})
                if skip_cooldown_hours > 0:
                    self.state.mark_video_comment_skipped(item.bvid, reason=f"东方合规:{touhou_check.reason}")
                continue
            text = touhou_check.text
            ok, reason, safe_text = validate_comment(
                text,
                self.config.forbidden_words,
                marker=self.config.ai_marker,
                require_marker=self.config.require_ai_marker,
            )
            if not ok and not explicit_request and analysis is not None and any(k in reason for k in ["空泛模板痕迹", "正文信息量过低", "缺少具体内容细节"]):
                self.log(f"评论质量拦截，尝试二次生成：{item.bvid} {reason}", "WARN")
                retry_text = ""
                if analysis.comment_draft and analysis.comment_draft != text:
                    retry_text = analysis.comment_draft
                if not retry_text:
                    retry_text = _fallback_comment_draft(item, analysis, max_len=int((rule.get("media_understanding") or {}).get("comment_max_length") or (self.config.data.get("media_understanding") or {}).get("comment_max_length") or 550))
                retry_text = self._strip_blocked_text_emotes(self.emotes.decorate_comment(retry_text, item, analysis, max_len=_deco_len))
                retry_text = self._apply_morph(retry_text)
                touhou_retry = self.touhou.review_outbound_text(retry_text, item)
                if touhou_retry.ok:
                    retry_text = touhou_retry.text
                ok, reason, safe_text = validate_comment(
                    retry_text,
                    self.config.forbidden_words,
                    marker=self.config.ai_marker,
                    require_marker=self.config.require_ai_marker,
                )
            # Check the final text after templates, decoration, morphing and retry.
            if ok and output_role_conflict(safe_text, getattr(analysis, "role_identity", {})):
                ok, reason = False, "角色一致性：评论含未确认的画面身份断言"
            if not ok:
                self.log(f"评论被安全规则拦截：{item.bvid} {reason}", "WARN")
                self.state.record_history({"action": "comment_blocked", "rule": name, "bvid": item.bvid, "reason": reason})
                if skip_cooldown_hours > 0:
                    self.state.mark_video_comment_skipped(item.bvid, reason=f"安全规则:{reason}")
                continue
            if self.config.dry_run:
                self.log(f"[DRY-RUN] 将评论 {item.bvid}《{item.title}》：{safe_text}")
                self.state.record_history({"action": "comment_dry_run", "rule": name, "bvid": item.bvid, "text": safe_text})
                # 预演不 mark_video_commented，避免污染生产状态导致真实运行时跳过这些视频
                sent += 1
                continue
            # 发送前原子预约“视频去重 + 今日主动评论额度”。
            # 注意：额度必须在真正发出前占用，否则多进程/重启旧实例会用旧 state 覆盖新计数，突破 daily_comment_limit。
            reserved, reserve_reason = self.state.reserve_video_comment(name, item.bvid, daily_limit)
            if not reserved:
                self.log(f"发送前占位/额度预约失败，跳过评论：{item.bvid} {item.title}；{reserve_reason}", "WARN")
                if "额度" in reserve_reason:
                    break
                continue
            try:
                pic_path = self._choose_comment_sticker()
                try:
                    result, suspicious = await self.client.send_video_comment_guarded(item, safe_text, pic_path=pic_path)
                except OutboundDeliveryUnknown:
                    raise
                except Exception as exc_pic:
                    if pic_path:
                        self.log(f"主动评论表情包发送失败，降级纯文字 {item.bvid} file={pic_path.name}：{exc_pic}", "WARN")
                        result, suspicious = await self.client.send_video_comment_guarded(item, safe_text)
                        pic_path = None
                    else:
                        raise
                if suspicious:
                    self.log(f"主动评论已被发送前安全预检拦截，未发送 {item.bvid}：{safe_text[:80]}", "WARN")
                    self.state.cancel_video_comment_reservation(item.bvid)
                    self.state.record_history({"action": "comment_blocked_before_send", "rule": name, "bvid": item.bvid, "text": safe_text, "reason": "分析式/非自然文本"})
                    if skip_cooldown_hours > 0:
                        self.state.mark_video_comment_skipped(item.bvid, reason="发送前预检:分析式文本")
                else:
                    if pic_path:
                        self.log(f"已评论 {item.bvid}《{item.title}》（附表情包 {pic_path.name}）：{safe_text}", "SUCCESS")
                    else:
                        self.log(f"已评论 {item.bvid}《{item.title}》：{safe_text}", "SUCCESS")
                    self.state.finish_reserved_video_comment(name, item.bvid, {"action": "comment_sent", "rule": name, "bvid": item.bvid, "text": safe_text, "result": _short_result(result)})
                    await self._like_and_favorite(item, rule, analysis)
                    await self._learn_comment_style(item, rule)
                self.state.save()
                self._comment_sent_ts.append(time.time())
                sent += 1
            except OutboundDeliveryUnknown as exc:
                # 请求已离开本地，但响应不确定；保留已占用额度并永久阻止自动重发。
                self.state.mark_video_comment_unknown(
                    name, item.bvid,
                    {"action": "comment_delivery_unknown", "rule": name, "bvid": item.bvid},
                )
                self.log(f"评论结果未知，禁止自动重发 {item.bvid}：{type(exc).__name__}", "ERROR")
                sent += 1
            except Exception as exc:
                self.state.cancel_video_comment_reservation(item.bvid)
                self.log(f"评论失败 {item.bvid}：{type(exc).__name__}", "ERROR")
                self.state.record_history({"action": "comment_failed", "rule": name, "bvid": item.bvid, "error": type(exc).__name__})
            # 串行发送之间保持间隔，降低风控。
            if sent < max_comments:
                if slow:
                    gap = random.randint(int(rl.get("slow_window_gap_min", 150) or 150), int(rl.get("slow_window_gap_max", 200) or 200))
                else:
                    gap = random.randint(int(self.config.data.get("comment_gap_min", 60) or 60), int(self.config.data.get("comment_gap_max", 90) or 90))
                await asyncio.sleep(gap)
        return sent

    async def _analyze_many(self, items: list[VideoItem], rule: dict[str, Any]) -> list[MediaAnalysis]:
        """并发分析多个视频（带错峰，降低对 premium 通道的突发请求），失败返回空分析对象。"""
        if not items:
            return []
        # 默认并发 2，可经 config analyze_concurrency 调整；并发太高会让一批视频同时打 premium。
        concurrency = max(1, int(self.config.data.get("analyze_concurrency", 2) or 2))
        rl = self.config.data.get("rate_limits") or {}
        if self._slow_window_active(rl):
            sc = int(rl.get("slow_window_analyze_concurrency", 0) or 0)
            if sc > 0:
                concurrency = max(1, min(concurrency, sc))
        sem = asyncio.Semaphore(concurrency)
        stagger_max = float(self.config.data.get("analyze_stagger_max_seconds", 25) or 0)

        async def one(item: VideoItem, jitter: float) -> MediaAnalysis:
            async with sem:
                if jitter > 0:
                    await asyncio.sleep(jitter)  # 错峰启动，避免同一分钟集中触发 premium
                return await self._analyze_if_needed(item, rule)

        return await asyncio.gather(*(one(item, random.uniform(0, max(0.0, stagger_max))) for item in items))

    async def _execute_share_rule(self, rule: dict[str, Any]) -> int:
        if not local_recommendation_allowed(self.config):
            self.log("云端接管私信或退订状态无法核实，本机暂停发送视频推荐", "INFO")
            return 0
        name = str(rule.get("name") or "unnamed-share-rule")
        if not self.state.can_share_today(self.config.daily_share_limit):
            self.log(f"跳过分享规则 {name}：今日分享额度已用完", "WARN")
            return 0
        recipients = rule.get("recipients") or []
        if not recipients:
            self.log(f"跳过分享规则 {name}：没有 recipients", "WARN")
            return 0
        videos = await self._load_videos(rule.get("video_source") or {})
        if not videos:
            self.log(f"分享规则 {name} 没有可用视频：可能是搜索无结果、时长过滤、关键词过滤或东方合规过滤导致", "WARN")
        max_shares = max(0, int(rule.get("max_shares_per_run", 1) or 1))
        sent = 0
        for item in videos:
            if sent >= max_shares:
                break
            for recipient in recipients:
                if sent >= max_shares:
                    break
                if not self.state.can_share_today(self.config.daily_share_limit):
                    break
                uid = str((recipient or {}).get("uid") or "").strip()
                if not uid.isdigit():
                    self.log(f"跳过无效收件人 UID：{uid}", "WARN")
                    continue
                if not local_recommendation_allowed(self.config, uid):
                    self.log(f"分享规则跳过私信拒收用户 uid={uid}", "INFO")
                    continue
                if self._share_recently(name, uid, item.bvid, rule):
                    self.log(f"跳过已分享视频：to={uid} {item.bvid} {item.title}")
                    continue
                analysis = await self._analyze_if_needed(item, rule)
                note_template = str((recipient or {}).get("note") or "分享一个视频：{title}\n{url}")
                text = self._format_template(note_template, item, analysis)
                text = self.touhou.prepare_share_text(text, item)
                text = self._strip_blocked_text_emotes(self.emotes.decorate_message(text, item, analysis))
                text = self._apply_morph(text)
                touhou_check = self.touhou.review_outbound_text(text, item)
                if not touhou_check.ok:
                    self.log(f"东方合规拦截私信：to={uid} {item.bvid} {touhou_check.reason}", "WARN")
                    self.state.record_history({"action": "share_blocked_touhou", "rule": name, "to": uid, "bvid": item.bvid, "reason": touhou_check.reason})
                    continue
                text = touhou_check.text
                ok, reason, safe_text = validate_message(
                    text,
                    self.config.forbidden_words,
                    marker=self.config.ai_marker,
                    require_marker=self.config.require_ai_marker,
                )
                if not ok:
                    self.log(f"分享私信被安全规则拦截：to={uid} {item.bvid} {reason}", "WARN")
                    self.state.record_history({"action": "share_blocked", "rule": name, "to": uid, "bvid": item.bvid, "reason": reason})
                    continue
                if self.config.dry_run:
                    self.log(f"[DRY-RUN] 将私信发送给 {uid}，长度={len(safe_text)}")
                    self.state.record_history({"action": "share_dry_run", "rule": name, "to": uid, "bvid": item.bvid, "text": safe_text})
                    # 预演不 mark_video_shared，避免污染生产状态导致真实运行时跳过
                    sent += 1
                    continue
                reserved, reserve_reason = self.state.reserve_video_share(
                    name, uid, item.bvid, self.config.daily_share_limit
                )
                if not reserved:
                    self.log(f"分享发送前占位/额度预约失败：to={uid} {item.bvid}；{reserve_reason}", "WARN")
                    if "额度" in reserve_reason:
                        return sent
                    continue
                try:
                    result = await self._send_private_with_optional_sticker(uid, safe_text, recommendation=True)
                    self.log(f"已私信发送给 {uid}：{item.bvid}《{item.title}》", "SUCCESS")
                    self.state.finish_video_share(name, uid, item.bvid)
                    self.state.record_history({"action": "share_sent", "rule": name, "to": uid, "bvid": item.bvid, "text": safe_text, "result": _short_result(result)})
                    sent += 1
                except OutboundDeliveryUnknown as exc:
                    # 结果未知时保留 pending 占位和额度，避免重启后重复私信。
                    self.log(f"分享结果未知，保留占位并禁止自动重发 to={uid} {item.bvid}：{type(exc).__name__}", "ERROR")
                    self.state.record_history({"action": "share_delivery_unknown", "rule": name, "to": uid, "bvid": item.bvid})
                    sent += 1
                except Exception as exc:
                    self.state.cancel_video_share_reservation(name, uid, item.bvid)
                    self.log(f"分享失败 to={uid} {item.bvid}：{type(exc).__name__}", "ERROR")
                    self.state.record_history({"action": "share_failed", "rule": name, "to": uid, "bvid": item.bvid, "error": type(exc).__name__})
        return sent

    async def _load_videos(self, source: dict[str, Any]) -> list[VideoItem]:
        stype = str(source.get("type", "search")).lower()
        max_videos = max(1, int(source.get("max_videos_per_run", 5) or 5))
        items: list[VideoItem] = []
        if stype in {"list", "bvids", "manual"}:
            for value in source.get("bvids") or source.get("videos") or []:
                bvid = extract_bvid(str(value)) or str(value).strip()
                if not bvid:
                    continue
                try:
                    items.append(await self.client.get_video_meta(bvid))
                except Exception as exc:
                    self.log(f"读取视频信息失败 {bvid}：{exc}", "WARN")
                if len(items) >= max_videos:
                    break
        elif stype in {"user", "uid", "space"}:
            uid = str(source.get("uid") or source.get("mid") or "").strip()
            keyword = str(source.get("keyword") or "").strip()
            try:
                self.log(f"开始读取用户公开视频：uid={uid}" + (f" keyword={keyword}" if keyword else ""))
                found = await self.client.get_user_videos(uid, max_results=max_videos, keyword=keyword)
                self.log(f"用户公开视频读取完成：uid={uid}，得到 {len(found)} 条")
                items.extend(found)
            except Exception as exc:
                self.log(f"读取用户公开视频失败 uid={uid}：{exc}", "ERROR")
        elif stype in {"home", "homepage", "recommend"}:
            # 首页推荐视频：抓取推荐流，再用标题+标签筛选。
            fetch_count = max(max_videos, int(source.get("fetch_count", 20) or 20))
            tag_keywords = [str(x).strip().lower() for x in source.get("tag_keywords", []) if str(x).strip()]
            # 标题直接命中用这些词（避免"东方"单字误匹配黑神话等）
            title_keywords = [str(x).strip().lower() for x in source.get("title_keywords", []) if str(x).strip()]
            if not title_keywords:
                title_keywords = [k for k in tag_keywords if len(k) >= 3 and k not in ("东方", "touhou")]
            try:
                self.log(f"开始抓取首页推荐视频：fetch={fetch_count}，标签筛选={tag_keywords or '无'}")
                found = await self.client.get_home_videos(max_results=fetch_count)
                self.log(f"首页推荐抓取完成：{len(found)} 条")
                if tag_keywords:
                    matched: list[VideoItem] = []
                    # 先按标题快速命中（不调接口）
                    for item in found:
                        if len(matched) >= max_videos:
                            break
                        hay_title = (item.title or "").lower()
                        if _is_finance_market_false_positive(hay_title):
                            self.log(f"首页推荐财经/股票误触过滤：{item.bvid}《{item.title}》", "WARN")
                            continue
                        if any(k in hay_title for k in title_keywords):
                            matched.append(item)
                            self.log(f"首页标题命中：{item.bvid}《{item.title}》")
                    # 标题没命中的再查标签（限制数量，避免逐个查接口太慢）
                    if len(matched) < max_videos:
                        tag_check_count = min(len(found), fetch_count)
                        for item in found:
                            if len(matched) >= max_videos:
                                break
                            if item in matched:
                                continue
                            try:
                                tags = await self.client.get_video_tags(item.bvid)
                                item.tags = tags
                                hay = (item.title + " " + " ".join(tags)).lower()
                                if _is_finance_market_false_positive(hay):
                                    self.log(f"首页推荐财经/股票误触过滤：{item.bvid}《{item.title}》 tags={tags}", "WARN")
                                    continue
                                if any(k in hay for k in tag_keywords):
                                    matched.append(item)
                                    self.log(f"首页命中标签：{item.bvid}《{item.title}》 tags={tags}")
                            except Exception as exc:
                                self.log(f"获取标签失败 {item.bvid}：{exc}", "WARN")
                    items.extend(matched)
                else:
                    items.extend(found[:max_videos])
            except Exception as exc:
                self.log(f"首页推荐视频获取失败：{exc}", "ERROR")
        else:
            keywords = [str(x).strip() for x in source.get("keywords", []) if str(x).strip()]
            order = str(source.get("order") or "pubdate").strip() or "pubdate"
            consecutive_fail = 0
            # 每个关键词轮流搜 1-2 条，保证所有关键词（东方Project/fumo/古明地恋…）都被覆盖，
            # 而不是第一个关键词搜满 max_videos 就 break（那样 fumo 等永远搜不到）。
            per_kw = max(1, int(source.get("per_keyword_results", 1) or 1))
            # 轮换起点：每轮从不同偏移开始遍历关键词，让后面的关键词（如冷门角色/梗）也能被搜到。
            try:
                offset = int(self.state.data.setdefault("search_keyword_offset", 0))
            except Exception:
                offset = 0
            kw_list = keywords[offset:] + keywords[:offset]
            # 记录本轮偏移，下一轮从不同位置开始（避免总是只搜前几个关键词）
            self.state.data["search_keyword_offset"] = (offset + 1) % max(1, len(keywords))
            for kw in kw_list:
                try:
                    self.log(f"开始搜索视频：keyword={kw} order={order}")
                    try:
                        found = await self.client.search_videos(kw, max_results=per_kw, order=order)
                    except Exception as exc:
                        # 网络波动（DNS/连接）时重试一次，避免瞬时失败浪费整轮
                        if consecutive_fail < 2:
                            self.log(f"搜索失败(重试) keyword={kw}：{exc}", "WARN")
                            await asyncio.sleep(3)
                            found = await self.client.search_videos(kw, max_results=per_kw, order=order)
                        else:
                            raise
                    self.log(f"搜索完成：keyword={kw}，得到 {len(found)} 条")
                    items.extend(found)
                    consecutive_fail = 0
                except Exception as exc:
                    consecutive_fail += 1
                    self.log(f"搜索视频失败 keyword={kw}：{exc}", "ERROR")
                    if consecutive_fail >= 3:
                        # 连续 3 次网络失败：网络可能有问题，暂停本规则轮次，避免反复失败。
                        self.log("搜索连续失败 3 次，暂停本规则轮次（网络可能不稳定）", "WARN")
                        break
                if len(items) >= max_videos:
                    break
            # 首页补充：搜索得到的东方视频不足时，从首页推荐里找东方标签视频补足。
            if len(items) < max_videos and source.get("home_fallback", True):
                try:
                    self.log("搜索结果不足，从首页推荐补充东方视频……")
                    home_items = await self.client.get_home_videos(max_results=40)
                    tag_kw = [str(x).strip().lower() for x in (source.get("tag_keywords") or ["东方project", "东方", "touhou", "古明地恋", "东方同人", "东方二创"]) if str(x).strip()]
                    for hi in home_items:
                        if len(items) >= max_videos:
                            break
                        try:
                            tags = await self.client.get_video_tags(hi.bvid)
                            hi.tags = tags
                            hay = (hi.title + " " + " ".join(tags)).lower()
                            if _is_finance_market_false_positive(hay):
                                self.log(f"首页补充财经/股票误触过滤：{hi.bvid}《{hi.title}》 tags={tags}", "WARN")
                                continue
                            if any(k in hay for k in tag_kw):
                                items.append(hi)
                                self.log(f"首页补充命中：{hi.bvid}《{hi.title}》 tags={tags}")
                        except Exception as exc:
                            self.log(f"首页补充获取标签失败 {hi.bvid}：{exc}", "WARN")
                except Exception as exc:
                    self.log(f"首页补充失败：{exc}", "ERROR")
        deduped = _dedupe_videos(items)
        if items and not deduped:
            self.log("视频去重后没有剩余候选", "WARN")
        filtered = self._filter_videos(deduped, source)[:max_videos]
        if deduped and not filtered:
            self.log("视频基础过滤后没有剩余候选：请检查 min/max_duration、include_keywords、exclude_keywords", "WARN")
        allowed, blocked = self.touhou.filter_videos(filtered)
        for blocked_item, reason in blocked:
            self.log(f"东方合规过滤视频：{blocked_item.bvid}《{blocked_item.title}》 {reason}", "WARN")
            self.state.record_history({"action": "video_blocked_touhou", "bvid": blocked_item.bvid, "title": blocked_item.title, "reason": reason})
        return allowed

    def _filter_videos(self, items: list[VideoItem], source: dict[str, Any]) -> list[VideoItem]:
        min_duration = int(source.get("min_duration_seconds", 0) or 0)
        max_duration = int(source.get("max_duration_seconds", 0) or 0)
        excludes = [str(x).lower() for x in source.get("exclude_keywords", []) if str(x).strip()]
        includes = [str(x).lower() for x in source.get("include_keywords", []) if str(x).strip()]
        out: list[VideoItem] = []
        for item in items:
            hay = f"{item.title}\n{item.desc}\n{item.author}".lower()
            if min_duration and item.duration and item.duration < min_duration:
                continue
            if max_duration and item.duration and item.duration > max_duration:
                continue
            if excludes and any(x in hay for x in excludes):
                continue
            if _is_finance_market_false_positive(hay):
                self.log(f"财经/股票误触过滤：{item.bvid}《{item.title}》", "WARN")
                self.state.record_history({"action": "video_blocked_finance_false_positive", "bvid": item.bvid, "title": item.title})
                continue
            if includes and not any(x in hay for x in includes):
                continue
            # 硬性过滤：去世/悼念相关，避免在逝者相关评论区留言
            if is_bereavement_text(hay):
                self.log(f"去世/悼念相关视频过滤：{item.bvid}《{item.title}》", "WARN")
                self.state.record_history({"action": "video_blocked_bereavement", "bvid": item.bvid, "title": item.title})
                continue
            out.append(item)
        return out

    async def _analyze_if_needed(self, item: VideoItem, rule: dict[str, Any]) -> MediaAnalysis:
        try:
            # per-call 知识上下文：并发分析多个视频时，不写共享 media.knowledge_text
            # （否则 A 视频评论会带上 B 视频的学习上下文，竞态）。
            kt = self.knowledge_text
            learn_cfg = self.config.data.get("touhou_learning") or {}
            if learn_cfg.get("enabled", True):
                # 检索学习库时加上"梗/评论/二创"等词，确保学到梗/风格条目能被命中用于评论。
                query = f"{item.title} {item.desc} 梗 评论 二创 东方 {' '.join((rule.get('video_source') or {}).get('keywords') or [])}"
                learned = self.touhou_learning.context_for(query, limit=int(learn_cfg.get("context_limit", 8) or 8))
                if learned:
                    # 相关学习库命中要放在最前面：下游生成评论会截取知识文本前部，
                    # 前置后新导入的作品索引/故事观/人物关系才能真正进入评论提示词。
                    kt = ("【长期学习到的东方/B站梗知识】\n" + learned + "\n\n" + kt).strip()
                # 角色识别纠错只在与当前视频标题/简介/标签明确相关时注入；
                # 不再无条件塞最近纠错，避免把训练样本/别的视频角色答案污染到普通视频。
                ctx_for_corr = f"{item.bvid} {item.title} {item.desc} {' '.join(item.tags or [])}".lower()
                correction_hits = []
                for x in self.touhou_learning.data.get("entries", []):
                    title_tags = str(x.get("title") or "") + " " + " ".join(x.get("tags") or [])
                    if "角色识别纠错" not in title_tags:
                        continue
                    hay = (title_tags + " " + str(x.get("content") or "") + " " + str(x.get("source") or "")).lower()
                    bvs = re.findall(r"bv[0-9a-z]+", hay)
                    if any(bv in ctx_for_corr for bv in bvs):
                        correction_hits.append(x)
                        continue
                    role_tokens = ["白莲", "秦心", "隐岐奈", "摩多罗", "天子", "阿求", "勇仪", "瑞灵", "芙兰", "魔理沙", "爱丽丝", "灵梦", "美铃", "米斯蒂娅", "克劳恩", "阿空", "霍青娥", "芳香", "妹红", "慧音", "琪露诺", "大妖精", "饕餮", "尤魔", "早苗", "神奈子", "诹访子", "文", "果", "荷取", "椛", "蕾米莉亚", "咲夜", "帕秋莉", "幽幽子", "妖梦", "紫", "蓝", "橙", "辉夜", "永琳", "铃仙", "纯狐", "赫卡提亚", "恋", "觉", "燐", "空", "正邪", "针妙丸", "袿姬", "磨弓", "八千慧", "早鬼", "残无", "千亦", "龙", "典", "高岭", "山如", "百百世", "莲子", "梅莉", "堇子", "姥芽", "魑魅", "驯子", "维缦", "丰姬", "阿梨夜", "贝子"]
                    if any(tok in ctx_for_corr and tok in hay for tok in role_tokens):
                        correction_hits.append(x)
                correction_hits = correction_hits[-4:]
                corrections = "\n\n".join(
                    f"【{x.get('category')}｜{x.get('title')}】\n标签：{', '.join(x.get('tags') or [])}\n内容：{str(x.get('content') or '')[:900]}"
                    for x in correction_hits
                )
                if corrections:
                    kt = (kt + "\n\n【相关角色识别纠错记忆】\n" + corrections).strip()
                # 额外注入学到的评论区风格，帮助评论更自然不呆板。
                if learn_cfg.get("learn_comment_styles", True):
                    styles = self.touhou_learning.context_for("评论风格 社区学习", limit=6, max_chars=3000)
                    if styles:
                        kt = (kt + "\n\n【学到的评论区风格（用于写自然评论）】\n" + styles).strip()
            # 总超时保护：单视频分析（下载/转写/视觉/多模态）最多 N 秒。
            # 2026-09-11 策略变更（主人确认）：原来"超时宁可跳过"，但实测丢弃率因此从
            # 2% 涨到 09-10 的 52%（38 支视频分析完被丢），等于把已花的钱全扔掉。
            # 现在超时后改用"减帧理解"重试：用更少的关键帧快速拿到可用证据，
            # 拿到就发评论；只有减帧也失败才跳过。
            rule_media_cfg = rule.get("media_understanding") or {}
            global_media_cfg = self.config.data.get("media_understanding") or {}
            raw_timeout = rule_media_cfg.get(
                "analysis_total_timeout_seconds",
                global_media_cfg.get("analysis_total_timeout_seconds", TIMEOUT_ANALYSIS_TOTAL),
            )
            raw_cap = rule_media_cfg.get(
                "analysis_total_timeout_max_seconds",
                global_media_cfg.get("analysis_total_timeout_max_seconds", TIMEOUT_ANALYSIS_TOTAL),
            )
            try:
                requested_timeout_s = max(30.0, float(raw_timeout or TIMEOUT_ANALYSIS_TOTAL))
            except (TypeError, ValueError):
                requested_timeout_s = float(TIMEOUT_ANALYSIS_TOTAL)
            try:
                timeout_cap_s = max(30.0, float(raw_cap or TIMEOUT_ANALYSIS_TOTAL))
            except (TypeError, ValueError):
                timeout_cap_s = float(TIMEOUT_ANALYSIS_TOTAL)
            timeout_s = min(requested_timeout_s, timeout_cap_s)
            if timeout_s < requested_timeout_s:
                self.log(
                    f"媒体理解总超时配置已限制为 {int(timeout_s)} 秒（请求值 {int(requested_timeout_s)} 秒）",
                    "WARN",
                )
            return await asyncio.wait_for(
                self.media.analyze(self.client, item, rule=rule, knowledge_text=kt),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            return await self._analyze_degraded(item, rule, kt, timeout_s)
        except Exception as exc:
            from .media import _BilidownPending
            if rule.get("_at_video_wait_for_download") and isinstance(exc, _BilidownPending):
                raise  # 应邀观看需持久化等待，不能把后台下载中当成永久失败。
            self.log(f"媒体理解失败 {item.bvid}：{exc}", "WARN")
            return MediaAnalysis(bvid=item.bvid, title=item.title, notes=["媒体理解失败，质量优先跳过"])

    async def _analyze_degraded(
        self, item: VideoItem, rule: dict[str, Any], kt: str, timeout_s: float
    ) -> MediaAnalysis:
        """整段分析超时后的降级重试：减帧理解，而不是整条丢弃。

        只降低关键帧预算（显式 max_keyframes 会关掉自适应放大），其余证据链
        （字幕/封面/音频/视觉增强）保持不变，所以拿到的仍是"有证据"的分析，
        只是覆盖的帧更少；减帧也超时才真正跳过并进入冷却名单。
        """
        import copy as _copy

        mu = (self.config.data.get("media_understanding") or {})
        degrade_frames = max(4, int(mu.get("analysis_degrade_max_keyframes", 12) or 12))
        degrade_timeout = max(60.0, float(mu.get("analysis_degrade_timeout_seconds", 240) or 240))
        self.log(
            f"媒体理解超时（>{int(timeout_s)}秒），改为减帧理解重试"
            f"（{degrade_frames} 帧 / {int(degrade_timeout)} 秒）：{item.bvid}",
            "WARN",
        )
        reduced_rule = _copy.deepcopy(rule or {})
        rmu = reduced_rule.setdefault("media_understanding", {})
        rmu["max_keyframes"] = degrade_frames
        # 下载次数要一起收：主配置把"分段下载次数"和"目标帧数"解耦之后，
        # 不显式收紧这里就会沿用主配置的下载次数（30），减帧重试反而比主流程还慢。
        rmu["max_segment_downloads"] = max(
            4, int(mu.get("degrade_max_segment_downloads", degrade_frames) or degrade_frames)
        )
        # 显式关闭自适应，避免 _apply_adaptive_keyframes 又把它放大回 40/60。
        rmu["adaptive_keyframes_enabled"] = False
        rmu["keyframe_retry_on_weak_vision"] = False
        try:
            analysis = await asyncio.wait_for(
                self.media.analyze(self.client, item, rule=reduced_rule, knowledge_text=kt),
                timeout=degrade_timeout,
            )
            analysis.notes.append(f"降级：整段超时后减帧（{degrade_frames} 帧）理解成功")
            self.log(
                f"减帧理解完成：{item.bvid}，视觉 {len(analysis.vision_text or '')} 字 / "
                f"字幕 {len(analysis.subtitle_text or '')} 字",
                "INFO",
            )
            return analysis
        except asyncio.TimeoutError:
            self.log(f"减帧理解仍超时（>{int(degrade_timeout)}秒），跳过 {item.bvid}", "WARN")
            return MediaAnalysis(bvid=item.bvid, title=item.title, notes=["媒体理解超时，质量优先跳过"])
        except Exception as exc:
            from .media import _BilidownPending
            if rule.get("_at_video_wait_for_download") and isinstance(exc, _BilidownPending):
                raise  # 应邀观看需持久化等待，不能把后台下载中当成永久失败。
            self.log(f"减帧理解失败 {item.bvid}：{type(exc).__name__}: {str(exc)[:120]}", "WARN")
            return MediaAnalysis(bvid=item.bvid, title=item.title, notes=["媒体理解失败，质量优先跳过"])

    def _postprocess_comment_text(self, text: str, *, is_ai: bool = False) -> str:
        from .textnorm import normalize_romaji, normalize_traditional
        raw = normalize_romaji(normalize_traditional(str(text or "").strip()))
        lyric_block = ""
        body = raw
        # 主动评论的旧风格需要保留多行歌词块；正文再压成自然单段，避免把歌词拍平成一行。
        lines = raw.splitlines()
        if lines and lines[0].strip().startswith(("♪", "♬", "♩", "♫")):
            for i, ln in enumerate(lines[1:6], start=1):
                if i >= 2 and any(ch in ln for ch in "♪♬♩♫"):
                    lyric_block = "\n".join(x.strip() for x in lines[: i + 1] if x.strip())
                    body = "\n".join(lines[i + 1:]).strip()
                    break
        text = " ".join(body.split())
        # 去套话 / 去明显模板腔 / 去“小本本腔”
        replacements = {
            "记到无意识的小本本里": "记住了",
            "记进东方小本本里": "记住了",
            "记到东方小本本里": "记住了",
            "记到小本本里": "记住了",
            "路过看见": "看见",
            "飘过来": "过来",
            "飘着看": "慢慢看",
            "主题挺明确": "主题很清楚",
            "把主题摆得很直观": "重点放得很清楚",
            "恋恋看着看着就想": "恋恋看着会觉得",
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        # 去重复句式与过密感叹
        text = text.replace("～～", "～")
        text = text.replace("！！", "！")
        text = text.replace("？？", "？")
        text = text.replace("诶呀？", "诶，")
        text = text.replace("哇哦，", "哇，")
        # 轻度收紧 AI 评论的恋恋腔，避免每条都像固定模板。
        if is_ai:
            text = re.sub(r"^(诶呀|诶～|哇哦|好啦好啦|嘛、|唔……)[，,、 ]*", "", text)
        text = text.strip()
        if lyric_block:
            return (lyric_block + ("\n" + text if text else "")).strip()
        return text

    def _trim_comment_for_budget(self, text: str) -> str:
        # 长度保险：歌词（含♪♬）优先保留，正文超长则截断，避免被安全规则拦截丢整条评论。
        from .safety import MAX_COMMENT_LENGTH
        budget = MAX_COMMENT_LENGTH - 60  # 给表情+fumo预留60字
        if len(text) <= budget:
            return text
        lines = text.split("\n")
        if lines and lines[0].strip() and any(ch in lines[0] for ch in "♪♬♩♫"):
            lyric_end = 0
            count = 0
            for i, ln in enumerate(lines):
                if any(ch in ln for ch in "♪♬♩♫"):
                    count += 1
                if count >= 2 and i >= 2:
                    lyric_end = i
                    break
            if lyric_end > 0:
                lyric_block = "\n".join(lines[: lyric_end + 1])
                rest = "\n".join(lines[lyric_end + 1:]).strip()
                keep = max(0, budget - len(lyric_block))
                if rest and keep > 10:
                    rest = rest[:keep].rstrip("，。,. ") + "。"
                return (lyric_block + "\n" + rest).strip() if rest else lyric_block
        return text[:budget].rstrip("，。,. ") + "。"

    def _render_comment_text(self, item: VideoItem, rule: dict[str, Any], templates: list[str], analysis: MediaAnalysis) -> str:
        # 媒体理解超时/整体失败 = 分析被中断（只有标题、没有证据），
        # 这时宁可跳过整条也不发模板凑数（质量优先，符合“超时跳过”的原始意图）。
        if any(k in str(n) for n in analysis.notes for k in ("媒体理解超时", "媒体理解失败", "质量优先")):
            return ""
        strategy = str(rule.get("comment_strategy") or "template").lower()
        is_ai = False
        if strategy in {"ai", "ai_draft", "media"} and analysis.comment_draft:
            text = analysis.comment_draft
            is_ai = True
        elif strategy == "ai_or_template":
            if analysis.comment_draft:
                text = analysis.comment_draft
                is_ai = True
            else:
                if any(k in str(n) for n in analysis.notes for k in ("跳过模板", "两次未达标", "两次生成均不达标", "两次未达质量标准")):
                    return ""
                text = random.choice(templates)
                text = self._format_template(text, item, analysis)
        else:
            text = random.choice(templates)
            text = self._format_template(text, item, analysis)
        if is_ai:
            from .lyrics import maybe_prepend_lyrics
            lyrics_cfg = self.config.data.get("koishi_lyrics") or {}
            if lyrics_cfg.get("apply_to_comments", True):
                comment_force = bool(lyrics_cfg.get("force_for_comments", True))
                text = maybe_prepend_lyrics(text, lyrics_cfg, force=comment_force)
        text = self._postprocess_comment_text(text, is_ai=is_ai)
        text = self._trim_comment_for_budget(text)
        return text

    async def _like_and_favorite(self, item: VideoItem, rule: dict[str, Any], analysis: MediaAnalysis | None = None) -> None:
        """点赞 + 收藏。

        - 每日日期夹：所有评论过的视频都收藏。
        - 「恋恋喜欢的视频」：只有 Koishi 判断真正喜欢的才收藏。
        失败会重试最多 3 次。
        """
        engage = rule.get("engage") or {}
        want_like = bool(engage.get("like", True))
        want_favorite = bool(engage.get("favorite", True))
        from datetime import datetime
        date_folder = datetime.now().strftime("%Y年%m月%d日")

        if want_like:
            last_err = None
            for attempt in range(1, 4):
                try:
                    like_result = await self.client.like_video(item)
                    self.log(f"已点赞 {item.bvid}：{_short_result(like_result)}", "SUCCESS")
                    self.state.record_history({"action": "like_sent", "bvid": item.bvid, "result": _short_result(like_result)})
                    last_err = None
                    break
                except Exception as exc:
                    last_err = exc
                    self.log(f"点赞失败(第{attempt}次) {item.bvid}：{exc}", "WARN")
                    if attempt < 3:
                        await asyncio.sleep(3 * attempt)
            if last_err:
                self.log(f"点赞最终失败 {item.bvid}：{last_err}", "ERROR")

        if want_favorite:
            # 每日日期夹：必收藏。
            await self._favorite_with_retry(item, date_folder)
            # 「恋恋喜欢的视频」：判断喜欢才收藏。
            love_folders = [str(x) for x in (engage.get("love_folders") or ["恋恋喜欢的视频"]) if str(x).strip()]
            try:
                likes = await self._koishi_likes(item, analysis)
            except Exception as exc:
                likes = False
                self.log(f"喜欢判断失败 {item.bvid}：{exc}", "WARN")
            if likes:
                for folder in love_folders:
                    await self._favorite_with_retry(item, folder)
                self.log(f"Koishi 喜欢这个视频，已收藏到「恋恋喜欢的视频」：{item.bvid}", "SUCCESS")
            else:
                self.log(f"Koishi 觉得一般，只进每日夹：{item.bvid}")

    async def _favorite_with_retry(self, item: VideoItem, folder_name: str) -> None:
        last_err = None
        for attempt in range(1, 4):
            try:
                fav_result = await self.client.favorite_video(item, folder_name)
                self.log(f"已收藏 {item.bvid} 到收藏夹「{folder_name}」：{_short_result(fav_result)}", "SUCCESS")
                self.state.record_history({"action": "favorite_sent", "bvid": item.bvid, "folder": folder_name, "result": _short_result(fav_result)})
                last_err = None
                break
            except Exception as exc:
                last_err = exc
                self.log(f"收藏失败(第{attempt}次) {item.bvid} 到「{folder_name}」：{exc}", "WARN")
                if attempt < 3:
                    await asyncio.sleep(3 * attempt)
        if last_err:
            self.log(f"收藏最终失败 {item.bvid} 到「{folder_name}」：{last_err}", "ERROR")

    async def _koishi_likes(self, item: VideoItem, analysis: MediaAnalysis | None = None) -> bool:
        """用 AI 判断 Koishi 是否真的喜欢这个视频（用于「恋恋喜欢的视频」收藏夹）。

        成本控制：这是 true/false 二分类，用免费层即可（不烧 rightapi gpt-5.4）；
        evidence 只取前 600 字（判断不需要全文）。免费层不可用时走信号兜底。
        """
        # 免费层可用 → 用它做判断；不可用 → 信号兜底（有字幕/视觉/简介即收藏）。
        if not (self.ai.free_text_base_url and self.ai.free_text_api_key and self.ai.free_text_model):
            return bool(item.desc or (analysis and (analysis.subtitle_text or analysis.vision_text)))
        evidence = ""
        if analysis:
            evidence = analysis.evidence[:600]
        prompt = (
            f"视频标题：{item.title}\nUP主：{item.author}\n"
            f"内容线索：{evidence or item.desc[:200]}\n\n"
            "作为古明地恋风格的AI，你正在挑选真正想收藏进『恋恋喜欢的视频』的作品。"
            "判断标准：作品是否高质量、用心、有独特亮点（画面/音乐/二创创意/氛围），且符合东方Project的审美与气质。"
            "只回答 true 或 false，不要其他文字。"
        )
        try:
            raw = await self.ai.chat(
                [{"role": "system", "content": "你是严格的收藏品鉴助手，只回答 true 或 false。"},
                 {"role": "user", "content": prompt}],
                model=self.ai.free_text_model,
                base_url=self.ai.free_text_base_url,
                api_key=self.ai.free_text_api_key,
                max_tokens=TOKEN_KOISHI_LIKES,
            )
            return str(raw or "").strip().lower().startswith("true")
        except Exception as exc:
            self.log(f"喜欢判断调用失败：{exc}", "WARN")
            return bool(item.desc or (analysis and (analysis.subtitle_text or analysis.vision_text)))

    def _claim_daily_once(self, marker: str) -> bool:
        """同一天内只放行一次的通用标记（跨进程用 state 落盘）。"""
        try:
            from datetime import datetime
            today = datetime.now().strftime("%Y-%m-%d")
        except Exception:
            today = ""
        d = self.state.data.setdefault("daily_once", {})
        if d.get(marker) == today:
            return False
        d[marker] = today
        try:
            self.state.save()
        except Exception:
            pass
        return True

    async def _learn_comment_style(self, item: VideoItem, rule: dict[str, Any]) -> None:
        """抓取视频评论区热评，用 AI 提炼评论风格存入长期学习库（不照抄原文）。"""
        learn_cfg = self.config.data.get("touhou_learning") or {}
        if not learn_cfg.get("learn_comment_styles", True):
            return
        if not item.aid:
            return
        # 主人要求：学评论区风格每天只执行一次（避免每条评论后都触发）
        if not self._claim_daily_once("comment_style_learn"):
            return
        try:
            data = await bili_comment.get_comments(
                item.aid, CommentResourceType.VIDEO, page_index=1,
                order=OrderType.LIKE, credential=self.client.credential,
            )
            replies = (data.get("replies") or [])[:20]
            samples: list[str] = []
            for r in replies:
                # content 可能是 dict 或 str
                if isinstance(r.get("content"), dict):
                    text = str(r.get("content", {}).get("message") or "")
                elif isinstance(r.get("content"), str):
                    text = r["content"]
                else:
                    continue
                text = text.strip()
                if text and len(text) >= 4:
                    samples.append(text)
            if not samples:
                return
            # 用 AI 提炼风格特征 + 梗/黑话，而不是照抄评论原文。
            if self.ai.available():
                style_prompt = (
                    f"以下是某个东方Project/B站视频评论区里真实用户的热评（{len(samples)} 条）：\n"
                    + "\n".join(samples[:15])
                    + "\n\n请做两件事：\n"
                    "1) 总结这些评论的语言风格特点：语气、句式、表情/颜文字习惯、长度、如何夸赞或吐槽。\n"
                    "2) 提炼里面出现的东方Project相关【梗/梗语/黑话/二创圈常用说法】（如角色梗、名场面梗、评论区常见调侃方式），列出这些梗的含义和用法。\n"
                    "只输出风格与梗的总结，不要逐条复述原文，不要出现任何评论原文本身。总结要通用，能被用来指导写新评论。"
                )
                from .ai import set_purpose as _set_purpose
                _set_purpose("style_learn")
                style = await self.ai.chat_for_step([
                    {"role": "system", "content": "你是评论风格与东方梗分析师，只总结风格和梗，不照抄原文。"},
                    {"role": "user", "content": style_prompt},
                ], "style_learn", use_premium=True, max_tokens=400, timeout=180)
            else:
                style = "评论区真实用户风格：多短句、口语化、会结合视频具体细节夸赞或吐槽，常用表情。"
            style = str(style or "").strip()
            if not style or len(style) < 10:
                return
            # 某些提供者偶尔会返回对象/列表的字符串化内容，或空异常文本；这里做一层类型与内容保护。
            if style.startswith(("{", "[")) and any(k in style for k in ('"role"', '"content"', '"message"', '"choices"')):
                self.log(f"跳过异常评论区风格学习结果 {item.bvid}：疑似结构化返回未展开", "WARN")
                return
            has_meme = any(k in style for k in ["梗", "黑话", "说法", "调侃", "二创", "圈"])
            self.touhou_learning.add(
                title=f"评论区风格与梗：{item.title[:40]}",
                content=style,
                category="comment_style" if not has_meme else "meme",
                source=f"comments:{item.bvid}",
                tags=["评论风格", "社区学习", "梗"] if has_meme else ["评论风格", "社区学习"],
                confidence="medium",
            )
            self.log(f"已学习评论区风格与梗：{item.bvid}（{len(style)} 字）", "INFO")
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            if "-404" in detail or "啥都木有" in detail or "不存在" in detail:
                self.log(f"学习评论区风格跳过 {item.bvid}：{detail[:120]}", "INFO")
            else:
                self.log(f"学习评论区风格失败 {item.bvid}：{detail[:150]}", "WARN")

    def _format_template(self, template: str, item: VideoItem, analysis: MediaAnalysis | None = None) -> str:
        values = {
            "title": item.title,
            "bvid": item.bvid,
            "author": item.author,
            "url": item.url,
        }
        if analysis:
            values.update(analysis.template_vars())
        class SafeDict(dict):
            def __missing__(self, key):
                return ""
        return template.format_map(SafeDict(values))

    def _comment_recently(self, rule_name: str, bvid: str, rule: dict[str, Any]) -> bool:
        """主动评论去重：已评论过，或正在发送中的视频，都不再主动评论。"""
        return self.state.is_video_commented(rule_name, bvid) or self.state.is_video_commenting(bvid)

    def _share_recently(self, rule_name: str, uid: str, bvid: str, rule: dict[str, Any]) -> bool:
        key = f"{rule_name}:{uid}:{bvid}"
        if self.state.is_video_shared(rule_name, uid, bvid):
            return True
        ts = self.state.data.setdefault("shared_videos", {}).get(key)
        return _within_cooldown(ts, int(rule.get("cooldown_hours_per_recipient_video", 0) or 0))


def _within_cooldown(timestamp: str | None, cooldown_hours: int) -> bool:
    if not timestamp:
        return False
    if cooldown_hours <= 0:
        return True
    try:
        old = datetime.fromisoformat(str(timestamp))
    except ValueError:
        return True
    return datetime.now() - old < timedelta(hours=cooldown_hours)


def _dedupe_videos(items: list[VideoItem]) -> list[VideoItem]:
    seen: set[str] = set()
    out: list[VideoItem] = []
    for item in items:
        if item.bvid in seen:
            continue
        seen.add(item.bvid)
        out.append(item)
    return out


def _short_result(result: Any) -> Any:
    if isinstance(result, dict):
        return {k: result.get(k) for k in list(result.keys())[:8]}
    return str(result)[:300]
