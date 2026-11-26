from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any

from .bili_client import VideoItem
from .media import MediaAnalysis


DEFAULT_KAOMOJI = {
    "happy": ["(≧▽≦)", "(*´▽`*)", "(｡•̀ᴗ-)✧", "(๑˃̵ᴗ˂̵)و", "(ﾉ◕ヮ◕)ﾉ*:・ﾟ✧", "(☆▽☆)"],
    "surprised": ["Σ(っ °Д °;)っ", "(ﾟдﾟ≡ﾟдﾟ)", "∑(°△°|||)", "Σ(ﾟДﾟ)", "(°ロ°)!"],
    "confused": ["(・_・?)", "(￣ω￣;)", "(｡-_-｡)", "(´･ω･`)", "(*´Д`)?"],
    "shy": ["(⁄ ⁄•⁄ω⁄•⁄ ⁄)", "(*/ω＼*)", "(〃▽〃)", "(｡･ω･｡)"],
    "proud": ["(๑•̀ㅂ•́)و✧", "(ง •̀_•́)ง", "٩(◕‿◕｡)۶", "(｀・ω・´)✧"],
    "love": ["(♡´▽`♡)", "(´▽`ʃ♡ƪ)", "(｡♥‿♥｡)"],
    "sad": ["(´;ω;`)", "(T_T)", "(｡•́︿•̀｡)", "(´-ω-`)", "(╥﹏╥)"],
    "wink": ["(^_-)-☆", "(´｡• ᵕ •｡`)", "(●'◡'●)"],
}

DEFAULT_KAOMOJI_POOL = [k for group in DEFAULT_KAOMOJI.values() for k in group]


DEFAULT_YASUZUME_EMOTES = [
    "[夜雀食堂_爷有钱]",
    "[夜雀食堂_想吃]",
    "[夜雀食堂_吐血]",
    "[夜雀食堂_囧]",
    "[夜雀食堂_疑惑]",
    "[夜雀食堂_熬夜]",
    "[夜雀食堂_Wink~]",
    "[夜雀食堂_我好嫉妒啊]",
    "[夜雀食堂_嗯哼]",
    "[夜雀食堂_今天加班]",
    "[夜雀食堂_开饭啦]",
    "[夜雀食堂_没文化底蕴]",
    "[夜雀食堂_害羞]",
    "[夜雀食堂_崩溃]",
    "[夜雀食堂_正邪立]",
    "[夜雀食堂_打call]",
    "[夜雀食堂_吹喇叭]",
    "[夜雀食堂_只要998]",
    "[夜雀食堂_上个buff]",
    "[夜雀食堂_敬礼]",
    "[夜雀食堂_大新闻]",
    "[夜雀食堂_城管来啦]",
    "[夜雀食堂_滑稽]",
    "[夜雀食堂_我在笑]",
    "[夜雀食堂_咱没钱]",
    "[东方收藏集·浮生若梦_好吃]",
    "[东方收藏集·浮生若梦_喷了]",
    "[东方收藏集·浮生若梦_阿部乃]",
    "[东方收藏集·浮生若梦_吃瓜]",
    "[东方收藏集·浮生若梦_已黑化]",
    "[东方收藏集·浮生若梦_燃起来了]",
    "[东方收藏集·浮生若梦_冻住了]",
    "[东方收藏集·浮生若梦_哭哭]",
    "[东方收藏集·浮生若梦_诶嘿嘿]",
    "[东方收藏集·浮生若梦_和善的笑容]",
    "[东方收藏集·浮生若梦_摸锦鲤]",
    "[东方收藏集·浮生若梦_吃桃]",
    "[东方收藏集·浮生若梦_饿饿]",
    "[东方收藏集·浮生若梦_逮捕！]",
    "[东方收藏集·浮生若梦_拿刀]",
    "[东方收藏集·浮生若梦_不会吧]",
    "[东方收藏集·浮生若梦_威严满满]",
    "[东方收藏集·浮生若梦_偷感十足]",
    "[东方收藏集·浮生若梦_钉稻草人]",
    "[东方收藏集·浮生若梦_要抱抱]",
]

# 进程内近期去重：夜雀食堂表情和颜文字都避免连续反复抽到同几个。
_RECENT_YASUZUME: list[str] = []
_RECENT_KAOMOJI: list[str] = []
_RECENT_EMOTE_LIMIT = 14
_RECENT_KAOMOJI_LIMIT = 18


def _filter_unverified_text_emotes(values: list[str]) -> list[str]:
    return [str(v) for v in values if "东方收藏集·浮生若梦" not in str(v)]


@dataclass
class EmoteRule:
    emotes: list[str]
    keywords: list[str]


class EmoteSelector:
    def __init__(self, config: dict[str, Any] | None):
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", False))
        self.mode = str(self.config.get("mode") or "contextual").lower()
        self.comment_min = int(self.config.get("comment_min", 1) or 1)
        self.comment_max = int(self.config.get("comment_max", 2) or 2)
        self.message_min = int(self.config.get("message_min", 2) or 2)
        self.message_max = int(self.config.get("message_max", 5) or 5)
        self.position = str(self.config.get("position") or "append").lower()
        self.separator = str(self.config.get("separator") or "")
        self.all_emotes = [str(x).strip() for x in (self.config.get("emotes") or DEFAULT_YASUZUME_EMOTES) if str(x).strip()]
        self.blocked_prefixes = [str(x) for x in self.config.get("blocked_text_emote_prefixes", ["[东方收藏集·浮生若梦_"]) if str(x)]
        self.allow_unverified_text_emotes = bool(self.config.get("allow_unverified_text_emotes", False))
        if not self.allow_unverified_text_emotes and self.blocked_prefixes:
            self.all_emotes = [e for e in self.all_emotes if not any(e.startswith(pfx) for pfx in self.blocked_prefixes)]
        self.rules = _build_rules(self.config.get("rules") or {}, allow_unverified=self.allow_unverified_text_emotes)
        # 已确认可渲染的官方文字表情包优先权重：用户希望「东方收藏集·浮生若梦」更常出现。
        default_preferred = ["[东方收藏集·浮生若梦_"] if self.allow_unverified_text_emotes else []
        self.preferred_text_emote_prefixes = [
            str(x) for x in (self.config.get("preferred_text_emote_prefixes") or default_preferred) if str(x)
        ]
        try:
            self.preferred_text_emote_probability = float(self.config.get("preferred_text_emote_probability", 0.0) or 0.0)
        except Exception:
            self.preferred_text_emote_probability = 0.0
        self.preferred_text_emote_probability = max(0.0, min(1.0, self.preferred_text_emote_probability))
        try:
            self.preferred_text_emote_min_count = int(self.config.get("preferred_text_emote_min_count", 1) or 1)
        except Exception:
            self.preferred_text_emote_min_count = 1
        self.preferred_text_emote_min_count = max(1, min(5, self.preferred_text_emote_min_count))

        # “表情很密”是账号风格的必杀技：默认稍微克制，低概率爆发高密度。
        self.comment_burst_probability = float(self.config.get("comment_burst_probability", 0.12) or 0.0)
        self.message_burst_probability = float(self.config.get("message_burst_probability", 0.10) or 0.0)
        self.comment_burst_min = int(self.config.get("comment_burst_min", max(self.comment_max, 3)) or max(self.comment_max, 3))
        self.comment_burst_max = int(self.config.get("comment_burst_max", max(self.comment_burst_min, 4)) or max(self.comment_burst_min, 4))
        self.message_burst_min = int(self.config.get("message_burst_min", max(self.message_max, 5)) or max(self.message_max, 5))
        self.message_burst_max = int(self.config.get("message_burst_max", max(self.message_burst_min, 7)) or max(self.message_burst_min, 7))

        # 颜文字（kaomoji）
        self.kaomoji_enabled = bool(self.config.get("kaomoji_enabled", True))
        self.kaomoji_comment_min = int(self.config.get("kaomoji_comment_min", 1) or 1)
        self.kaomoji_comment_max = int(self.config.get("kaomoji_comment_max", 2) or 2)
        self.kaomoji_message_min = int(self.config.get("kaomoji_message_min", 2) or 2)
        self.kaomoji_message_max = int(self.config.get("kaomoji_message_max", 3) or 3)
        self.kaomoji_comment_burst_min = int(self.config.get("kaomoji_comment_burst_min", max(self.kaomoji_comment_max, 3)) or max(self.kaomoji_comment_max, 3))
        self.kaomoji_comment_burst_max = int(self.config.get("kaomoji_comment_burst_max", max(self.kaomoji_comment_burst_min, 5)) or max(self.kaomoji_comment_burst_min, 5))
        self.kaomoji_message_burst_min = int(self.config.get("kaomoji_message_burst_min", max(self.kaomoji_message_max, 4)) or max(self.kaomoji_message_max, 4))
        self.kaomoji_message_burst_max = int(self.config.get("kaomoji_message_burst_max", max(self.kaomoji_message_burst_min, 7)) or max(self.kaomoji_message_burst_min, 7))
        custom = self.config.get("kaomoji") or {}
        self.kaomoji_groups = {
            k: [str(x) for x in v if str(x).strip()]
            for k, v in {**DEFAULT_KAOMOJI, **(custom if isinstance(custom, dict) else {})}.items()
            if isinstance(v, list)
        }

    def decorate_comment(self, text: str, item: VideoItem, analysis: MediaAnalysis | None = None, max_len: int = 500) -> str:
        if not self.enabled:
            return text
        burst = random.random() < self.comment_burst_probability
        e_min, e_max = (self.comment_burst_min, self.comment_burst_max) if burst else (self.comment_min, self.comment_max)
        k_min, k_max = (self.kaomoji_comment_burst_min, self.kaomoji_comment_burst_max) if burst else (self.kaomoji_comment_min, self.kaomoji_comment_max)
        cap = _density_cap(text, burst)
        e_max = min(e_max, cap); e_min = min(e_min, e_max)
        k_max = min(k_max, cap); k_min = min(k_min, k_max)
        yasuzume = self.select(item, analysis, min_count=e_min, max_count=e_max)
        kaomoji = self._select_kaomoji(item, analysis, k_min, k_max)
        # 交替混合两类表情，避免颜文字一股脑堆在结尾。
        emotes = _interleave(yasuzume, kaomoji)
        return _attach_emotes(text, emotes, position=self.position, sep=self.separator, max_len=max_len)

    def decorate_message(self, text: str, item: VideoItem, analysis: MediaAnalysis | None = None, max_len: int = 900) -> str:
        if not self.enabled:
            return text
        burst = random.random() < self.message_burst_probability
        e_min, e_max = (self.message_burst_min, self.message_burst_max) if burst else (self.message_min, self.message_max)
        k_min, k_max = (self.kaomoji_message_burst_min, self.kaomoji_message_burst_max) if burst else (self.kaomoji_message_min, self.kaomoji_message_max)
        cap = _density_cap(text, burst)
        e_max = min(e_max, cap); e_min = min(e_min, e_max)
        k_max = min(k_max, cap); k_min = min(k_min, k_max)
        yasuzume = self.select(item, analysis, min_count=e_min, max_count=e_max)
        kaomoji = self._select_kaomoji(item, analysis, k_min, k_max)
        emotes = _interleave(yasuzume, kaomoji)
        return _attach_emotes(text, emotes, position=self.position, sep=self.separator, max_len=max_len)

    def _select_kaomoji(self, item: VideoItem, analysis: MediaAnalysis | None, min_count: int, max_count: int) -> list[str]:
        if not self.kaomoji_enabled or max_count <= 0:
            return []
        hay = _haystack(item, analysis)
        picked: list[str] = []
        for mood, keywords in _KAOMOJI_KEYWORDS.items():
            if any(k.lower() in hay for k in keywords):
                pool = self.kaomoji_groups.get(mood) or []
                if pool:
                    picked.append(_choice_fresh(pool, _RECENT_KAOMOJI, _RECENT_KAOMOJI_LIMIT))
        if not picked:
            pool = self.kaomoji_groups.get("happy") or DEFAULT_KAOMOJI_POOL
            picked.append(_choice_fresh(pool, _RECENT_KAOMOJI, _RECENT_KAOMOJI_LIMIT))
        # 再随机补足到目标数量，制造“很多颜文字”的效果。
        target = random.randint(min_count, max_count)
        pool = [k for group in self.kaomoji_groups.values() for k in group] or DEFAULT_KAOMOJI_POOL
        while len(picked) < target and len(picked) < len(pool):
            extra = _choice_fresh(pool, _RECENT_KAOMOJI, _RECENT_KAOMOJI_LIMIT)
            if extra not in picked:
                picked.append(extra)
        return picked[:max_count]

    def select(self, item: VideoItem, analysis: MediaAnalysis | None, min_count: int, max_count: int) -> list[str]:
        """挑选夜雀食堂表情（多样化策略）：

        1. 命中的每个规则随机抽 1 个表情（不固定全拿，让不同表情都有机会出场）；
        2. 多条规则命中可叠加；
        3. 没命中规则时走情绪兜底（分多组随机）；
        4. 最后从全池随机补足到目标数量，保证表情组合每次都不同。
        """
        if not self.all_emotes or max_count <= 0:
            return []
        hay = _haystack(item, analysis)
        picked: list[str] = []

        # 1) 规则匹配：每个命中规则随机取 1 个表情。
        matched = [r for r in self.rules if any(k.lower() in hay for k in r.keywords)]
        random.shuffle(matched)  # 不让 food/hard/news 等固定顺序长期压过后面的规则。
        for rule in matched:
            pool = [e for e in rule.emotes if e in self.all_emotes]
            if pool:
                picked.append(_choice_fresh(pool, _RECENT_YASUZUME, _RECENT_EMOTE_LIMIT))
            if len(picked) >= max_count:
                break

        # 2) 情绪兜底：没命中规则时按 haystack 情绪分组随机。
        if not picked:
            picked.extend(_fallback_emotes(hay, self.all_emotes))

        # 3) 终极兜底：随机从全池取一个。
        if not picked:
            picked.append(_choice_fresh(self.all_emotes, _RECENT_YASUZUME, _RECENT_EMOTE_LIMIT))

        # 去重且只保留配置内的表情。
        out: list[str] = []
        for e in picked:
            if e in self.all_emotes and e not in out:
                out.append(e)

        # 4) 随机补足到目标数量（min~max 之间随机，制造数量变化与多样性）。
        target = random.randint(min_count, max_count)
        preferred_pool = [
            e for e in self.all_emotes
            if any(e.startswith(pfx) for pfx in self.preferred_text_emote_prefixes)
        ]
        # 用户确认「东方收藏集·浮生若梦」会渲染成图：按概率保证至少出现若干个，避免被旧包稀释。
        prefer_this_reply = bool(preferred_pool and self.preferred_text_emote_probability > 0 and random.random() < self.preferred_text_emote_probability)
        if prefer_this_reply:
            preferred_need = min(self.preferred_text_emote_min_count, max_count, target)
            while len([e for e in out if e in preferred_pool]) < preferred_need and len(out) < target:
                extra = _choice_fresh(preferred_pool, _RECENT_YASUZUME, _RECENT_EMOTE_LIMIT)
                if extra not in out:
                    out.append(extra)
            # 如果短句 cap=1 且前面规则已抽到旧包导致没空间，直接替换 1 个旧包为东方收藏集。
            if len([e for e in out if e in preferred_pool]) < preferred_need and out:
                extra = _choice_fresh(preferred_pool, _RECENT_YASUZUME, _RECENT_EMOTE_LIMIT)
                for idx, old in enumerate(out):
                    if old not in preferred_pool and extra not in out:
                        out[idx] = extra
                        break
        while len(out) < target and len(out) < len(self.all_emotes):
            pool = preferred_pool if preferred_pool and random.random() < self.preferred_text_emote_probability else self.all_emotes
            extra = _choice_fresh(pool, _RECENT_YASUZUME, _RECENT_EMOTE_LIMIT)
            if extra not in out:
                out.append(extra)
        return out[:max_count]


def _choice_fresh(pool: list[str], recent: list[str], limit: int) -> str:
    """从 pool 里优先选最近没用过的项；记录在进程内 recent 中。"""
    clean = [str(x) for x in pool if str(x)]
    if not clean:
        return ""
    candidates = [x for x in clean if x not in recent]
    if not candidates:
        candidates = clean
    picked = random.choice(candidates)
    recent.append(picked)
    del recent[:-max(1, int(limit or 1))]
    return picked


def _density_cap(text: str, burst: bool) -> int:
    """表情数量随文本长度缩放：短消息克制，长消息才允许堆表情。

    默认约每 15 字 1 个表情；爆发模式放宽到约 10 字 1 个，
    让"表情很密"的必杀技保留在足够长的内容上，而不是出现在一两句短消息上。
    """
    n = len(str(text or "").strip())
    if n <= 0:
        return 1
    per = 10 if burst else 15
    return max(1, n // per)


def _build_rules(raw: dict[str, Any], *, allow_unverified: bool = False) -> list[EmoteRule]:
    default_rules = {
        "food": {"keywords": ["食堂", "料理", "美食", "吃", "饭", "开饭", "夜雀", "米斯蒂娅", "饿", "桃"], "emotes": ["[夜雀食堂_开饭啦]", "[夜雀食堂_想吃]", "[夜雀食堂_嗯哼]", "[东方收藏集·浮生若梦_好吃]", "[东方收藏集·浮生若梦_吃桃]", "[东方收藏集·浮生若梦_饿饿]"]},
        "money": {"keywords": ["钱", "便宜", "贵", "998", "价格", "打折", "氪", "抽卡", "运气", "欧", "锦鲤"], "emotes": ["[夜雀食堂_爷有钱]", "[夜雀食堂_只要998]", "[夜雀食堂_咱没钱]", "[东方收藏集·浮生若梦_摸锦鲤]"]},
        "hard": {"keywords": ["高难", "挑战", "极限", "折磨", "困难", "打了", "失败", "崩溃", "九禁", "1/10", "通关", "燃", "黑化", "冻"], "emotes": ["[夜雀食堂_吐血]", "[夜雀食堂_崩溃]", "[夜雀食堂_上个buff]", "[东方收藏集·浮生若梦_已黑化]", "[东方收藏集·浮生若梦_燃起来了]", "[东方收藏集·浮生若梦_冻住了]", "[东方收藏集·浮生若梦_哭哭]"]},
        "late": {"keywords": ["熬夜", "深夜", "加班", "肝", "爆肝", "通宵"], "emotes": ["[夜雀食堂_熬夜]", "[夜雀食堂_今天加班]", "[东方收藏集·浮生若梦_哭哭]"]},
        "news": {"keywords": ["新闻", "更新", "公告", "补档", "修复", "版本", "大新闻", "情报", "围观"], "emotes": ["[夜雀食堂_大新闻]", "[夜雀食堂_敬礼]", "[东方收藏集·浮生若梦_吃瓜]"]},
        "funny": {"keywords": ["搞笑", "笑", "乐", "滑稽", "吐槽", "整活", "蚌埠", "绷不住", "喷了", "不会吧"], "emotes": ["[夜雀食堂_滑稽]", "[夜雀食堂_我在笑]", "[东方收藏集·浮生若梦_喷了]", "[东方收藏集·浮生若梦_诶嘿嘿]", "[东方收藏集·浮生若梦_不会吧]"]},
        "cute": {"keywords": ["可爱", "恋恋", "古明地恋", "害羞", "萌", "甜", "温柔", "抱抱"], "emotes": ["[夜雀食堂_害羞]", "[夜雀食堂_Wink~]", "[东方收藏集·浮生若梦_诶嘿嘿]", "[东方收藏集·浮生若梦_和善的笑容]", "[东方收藏集·浮生若梦_要抱抱]"]},
        "music": {"keywords": ["音乐", "曲", "bgm", "vocal", "remix", "arrange", "编曲", "吹喇叭"], "emotes": ["[夜雀食堂_吹喇叭]", "[夜雀食堂_打call]", "[东方收藏集·浮生若梦_燃起来了]"]},
        "confused": {"keywords": ["疑惑", "为什么", "怎么", "教程", "修复器", "工具", "blender", "sdk", "mod"], "emotes": ["[夜雀食堂_疑惑]", "[夜雀食堂_囧]", "[东方收藏集·浮生若梦_不会吧]"]},
        "touhou": {"keywords": ["东方", "touhou", "幻想乡", "地灵殿", "红魔乡", "妖妖梦", "弹幕", "威严", "草人"], "emotes": ["[夜雀食堂_打call]", "[夜雀食堂_嗯哼]", "[东方收藏集·浮生若梦_威严满满]", "[东方收藏集·浮生若梦_偷感十足]", "[东方收藏集·浮生若梦_钉稻草人]", "[东方收藏集·浮生若梦_阿部乃]", "[东方收藏集·浮生若梦_逮捕！]", "[东方收藏集·浮生若梦_拿刀]"]},
    }
    if isinstance(raw, dict) and raw:
        for key, value in raw.items():
            if isinstance(value, dict):
                default_rules[str(key)] = {
                    "keywords": [str(x) for x in value.get("keywords", [])],
                    "emotes": [str(x) for x in value.get("emotes", [])],
                }
    return [EmoteRule(emotes=(v["emotes"] if allow_unverified else _filter_unverified_text_emotes(v["emotes"])), keywords=[k.lower() for k in v["keywords"]]) for v in default_rules.values()]


def _fallback_emotes(hay: str, pool: list[str] | None = None) -> list[str]:
    """按情绪分组随机挑选兜底表情（只从 pool 里可选的表情中取）。

    分组覆盖更多情绪，且每组随机选，让不同表情都有出场机会。
    """
    pool = pool or DEFAULT_YASUZUME_EMOTES
    groups = [
        (["好", "赞", "厉害", "不错", "喜欢", "认真", "棒", "妙", "精彩", "优秀", "强", "第一", "成功"], ["[夜雀食堂_打call]", "[夜雀食堂_嗯哼]", "[夜雀食堂_Wink~]", "[夜雀食堂_爷有钱]", "[东方收藏集·浮生若梦_燃起来了]", "[东方收藏集·浮生若梦_和善的笑容]"]),
        (["难", "累", "久", "卡", "bug", "折磨", "崩溃", "失败", "吐血", "高难", "挑战", "黑化", "冻"], ["[夜雀食堂_上个buff]", "[夜雀食堂_吐血]", "[夜雀食堂_崩溃]", "[夜雀食堂_熬夜]", "[东方收藏集·浮生若梦_已黑化]", "[东方收藏集·浮生若梦_冻住了]", "[东方收藏集·浮生若梦_哭哭]"]),
        (["钱", "贵", "便宜", "打折", "价格", "氪", "抽卡", "998", "运气", "欧", "锦鲤"], ["[夜雀食堂_爷有钱]", "[夜雀食堂_只要998]", "[夜雀食堂_咱没钱]", "[东方收藏集·浮生若梦_摸锦鲤]"]),
        (["笑", "搞笑", "乐", "滑稽", "整活", "绷不住", "吐槽", "喷了", "不会吧"], ["[夜雀食堂_滑稽]", "[夜雀食堂_我在笑]", "[夜雀食堂_城管来啦]", "[东方收藏集·浮生若梦_喷了]", "[东方收藏集·浮生若梦_诶嘿嘿]", "[东方收藏集·浮生若梦_不会吧]"]),
        (["疑惑", "不懂", "为什么", "怎么", "奇怪", "谜"], ["[夜雀食堂_疑惑]", "[夜雀食堂_囧]", "[夜雀食堂_没文化底蕴]", "[东方收藏集·浮生若梦_不会吧]"]),
        (["可爱", "甜", "萌", "恋恋", "古明地恋", "害羞", "温柔", "抱抱"], ["[夜雀食堂_害羞]", "[夜雀食堂_Wink~]", "[夜雀食堂_正邪立]", "[东方收藏集·浮生若梦_诶嘿嘿]", "[东方收藏集·浮生若梦_和善的笑容]", "[东方收藏集·浮生若梦_要抱抱]"]),
        (["熬夜", "深夜", "加班", "肝", "通宵"], ["[夜雀食堂_熬夜]", "[夜雀食堂_今天加班]", "[夜雀食堂_上个buff]", "[东方收藏集·浮生若梦_哭哭]"]),
        (["音乐", "曲", "bgm", "vocal", "remix", "arrange", "编曲", "歌", "旋律"], ["[夜雀食堂_吹喇叭]", "[夜雀食堂_打call]", "[夜雀食堂_开饭啦]", "[东方收藏集·浮生若梦_燃起来了]"]),
        (["新闻", "更新", "公告", "情报", "版本", "修复", "围观"], ["[夜雀食堂_大新闻]", "[夜雀食堂_敬礼]", "[夜雀食堂_嗯哼]", "[东方收藏集·浮生若梦_吃瓜]"]),
        (["吃", "饭", "食堂", "美食", "料理", "夜雀", "开饭", "饿", "桃"], ["[夜雀食堂_开饭啦]", "[夜雀食堂_想吃]", "[夜雀食堂_嗯哼]", "[东方收藏集·浮生若梦_好吃]", "[东方收藏集·浮生若梦_吃桃]", "[东方收藏集·浮生若梦_饿饿]"]),
    ]
    for keywords, emotes in groups:
        if any(k in hay for k in keywords):
            allowed = [e for e in emotes if e in pool]
            if allowed:
                return [_choice_fresh(allowed, _RECENT_YASUZUME, _RECENT_EMOTE_LIMIT)]
    return []


_KAOMOJI_KEYWORDS = {
    "happy": ["开心", "高兴", "好", "赞", "厉害", "不错", "喜欢", "棒", "成功", "漂亮", "舒服", "顺"],
    "surprised": ["惊讶", "厉害", "居然", "没想到", "太快", "震惊", "惊", "哇", "无伤", "极限"],
    "confused": ["疑惑", "为什么", "怎么", "不懂", "奇怪", "迷", "忘记", "教程", "工具"],
    "shy": ["害羞", "不好意思", "脸红", "可爱", "恋恋", "古明地恋", "萌", "甜"],
    "proud": ["得意", "强", "赢", "第一", "过关", "拿下", "成功", "挑战"],
    "love": ["喜欢", "爱", "可爱", "心动", "萌", "推荐", "分享"],
    "sad": ["难", "累", "失败", "遗憾", "遗憾", "崩", "卡", "吐血", "加班", "熬夜"],
    "wink": ["wink", "眨眼", "悄悄", "秘密", "记", "本本", "偷偷"],
}


def _haystack(item: VideoItem, analysis: MediaAnalysis | None) -> str:
    parts = [item.title, item.desc, item.author]
    if analysis:
        parts.extend([analysis.summary, analysis.comment_draft, analysis.share_reason, analysis.vision_text, analysis.subtitle_text[:1200]])
    return "\n".join(str(x or "") for x in parts).lower()


def _interleave(a: list[str], b: list[str]) -> list[str]:
    """交替混合两个列表，避免某一类表情集中出现。"""
    out: list[str] = []
    max_len = max(len(a), len(b))
    for i in range(max_len):
        if i < len(a):
            out.append(a[i])
        if i < len(b):
            out.append(b[i])
    return out


def _attach_emotes(text: str, emotes: list[str], position: str = "append", sep: str = "", max_len: int = 180) -> str:
    text = str(text or "").strip()
    emotes = [e for e in emotes if e]
    if not emotes:
        return text
    # Do not duplicate an emote that is already present.
    emotes = [e for e in emotes if e not in text]
    if not emotes:
        return text
    # Fit under max_len by trimming emote count, not main text.
    chosen: list[str] = []
    for e in emotes:
        candidate_emotes = chosen + [e]
        candidate = _combine(text, candidate_emotes, position, sep)
        if len(candidate) <= max_len:
            chosen.append(e)
    if not chosen:
        return text
    return _combine(text, chosen, position, sep)


def _combine(text: str, emotes: list[str], position: str, sep: str) -> str:
    e = sep.join(emotes)
    if position == "prepend":
        return (e + sep + text).strip()
    if position == "both" and len(emotes) >= 2:
        return (emotes[0] + sep + text + sep + sep.join(emotes[1:])).strip()
    if position in {"inline", "middle", "sentence"}:
        return _insert_inline(text, emotes, sep)
    return (text + sep + e).strip()


def _insert_inline(text: str, emotes: list[str], sep: str = "") -> str:
    text = str(text or "").strip()
    if not text or not emotes:
        return text
    # 保护 URL，避免表情插入到链接内部破坏可点击性。
    url_guard = re.findall(r"https?://\S+", text)
    protected_text = re.sub(r"https?://\S+", "\u0001URL\u0001", text)
    # Split while preserving sentence punctuation. Prefer inserting after natural clauses.
    parts = re.split(r"([，。！？；,.!?;\n])", protected_text)
    units: list[str] = []
    for i in range(0, len(parts), 2):
        chunk = parts[i]
        punct = parts[i + 1] if i + 1 < len(parts) else ""
        if chunk or punct:
            units.append(chunk + punct)
    if len(units) <= 1:
        # No punctuation: insert near the middle.
        mid = max(1, len(protected_text) // 2)
        result = (protected_text[:mid] + sep + emotes[0] + sep + protected_text[mid:] + sep + sep.join(emotes[1:])).strip()
        return _restore_urls(result, url_guard)
    # Spread emotes across clauses as evenly as possible.
    count = len(emotes)
    n = len(units)
    slots = list(range(0, n))
    positions: list[int] = []
    for idx in range(count):
        pos = round(idx * (len(slots) - 1) / max(1, count - 1)) if count > 1 else len(slots) // 2
        pos = min(max(pos, 0), len(slots) - 1)
        positions.append(slots[pos])
    used: set[int] = set()
    final_pos: list[int] = []
    for p in positions:
        while p in used and p < n:
            p += 1
        if p < n:
            used.add(p)
            final_pos.append(p)
    # 表情多于分句时，剩余的表情轮流塞到已有位置（跟在已插表情后面），避免堆在结尾。
    out: list[str] = []
    emote_iter = iter(emotes)
    pos_set = set(final_pos)
    # 先按位置插入第一轮表情。
    slot_emotes: dict[int, list[str]] = {p: [] for p in final_pos}
    for p in final_pos:
        try:
            slot_emotes[p].append(next(emote_iter))
        except StopIteration:
            break
    # 剩余表情轮流追加到这些位置。
    leftovers = list(emote_iter)
    if leftovers and slot_emotes:
        keys = list(slot_emotes.keys())
        idx = 0
        for e in leftovers:
            slot_emotes[keys[idx % len(keys)]].append(e)
            idx += 1
    for i, unit in enumerate(units):
        out.append(unit)
        if i in slot_emotes:
            out.append(sep + sep.join(slot_emotes[i]) + sep)
    return _restore_urls("".join(out).strip(), url_guard)


def _restore_urls(text: str, urls: list[str]) -> str:
    for url in urls:
        text = text.replace("\u0001URL\u0001", url, 1)
    return text
