from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .bili_client import VideoItem


TOUHOU_KEYWORDS = [
    "东方project", "東方project", "touhou", "东方", "東方",
    "上海爱丽丝幻乐团", "上海アリス幻樂団", "上海アリス幻楽団", "zun",
    "博丽灵梦", "博麗霊夢", "雾雨魔理沙", "霧雨魔理沙",
    "古明地恋", "古明地こいし", "koishi", "komeiji",
    "古明地觉", "火焰猫燐", "灵乌路空", "地灵殿", "旧地狱",
]

ENDING_RISK_WORDS = [
    "结局全文", "ending全文", "ending 全文", "结局原文", "ending原文",
    "结局文本", "ending文本", "全结局", "结局合集", "ending合集",
    "剧透结局", "结局翻译全文",
]

RAW_ASSET_RISK_WORDS = [
    "拆包", "解包", "提取素材", "官方素材包", "官方立绘包", "立绘包",
    "原作素材包", "原曲无损", "原声无损", "bgm提取", "dat提取",
    "游戏数据", "原作程序", "官方文本包", "资源包下载",
]

OFFICIAL_MISLEADING_WORDS = [
    "官方续作", "官方外传", "正版续作", "正版外传", "官方新作", "官方授权续作",
    "东方永夜抄外传", "东方红魔乡外传", "东方地灵殿外传", "东方project官方",
]

FANWORK_MARKERS = ["非官方", "二次创作", "二创", "同人", "粉丝创作", "fanwork", "unofficial"]

FINANCE_MARKET_FALSE_POSITIVE_WORDS = (
    # “京东方A”是股票名，不是东方Project；这些词出现时避免被“东方”单字误判。
    "京东方a", "股票", "股市", "a股", "证券", "炒股", "盘中分析", "盘前", "盘后",
    "k线", "分时图", "短线", "买卖点", "压力支撑", "交易体系", "盘感",
    "药明康德", "长电科技", "紫金矿业", "中国巨石", "英维克", "云南锗业", "长飞光纤", "有研新材", "立讯精密",
)


@dataclass
class ComplianceResult:
    ok: bool = True
    reasons: list[str] = field(default_factory=list)
    text: str = ""

    @property
    def reason(self) -> str:
        return "；".join(self.reasons)


class TouhouCompliance:
    """东方Project合规模式。

    This is a conservative rule-based guard derived from the provided guidelines.
    It does not provide legal advice; it helps avoid obviously risky posts.
    """

    def __init__(self, config: dict[str, Any] | None):
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", False))
        self.avoid_endings = bool(self.config.get("avoid_endings", True))
        self.require_fanwork_marker = bool(self.config.get("require_fanwork_marker", True))
        self.avoid_raw_asset_repost = bool(self.config.get("avoid_raw_asset_repost", True))
        self.avoid_official_like_titles = bool(self.config.get("avoid_official_like_titles", True))
        self.add_dm_notice = bool(self.config.get("add_dm_notice", True))
        self.block_risky_videos = bool(self.config.get("block_risky_videos", True))
        self.prevent_misleading_words = bool(self.config.get("prevent_misleading_words", True))
        self.extra_keywords = [str(x).lower() for x in self.config.get("extra_keywords", []) if str(x).strip()]
        self.safe_notice = str(
            self.config.get("fanwork_notice")
            or "注：东方Project相关内容请以非官方二次创作/同人演绎为准。"
        )

    def is_touhou_related(self, item_or_text: VideoItem | str | None, extra_text: str = "") -> bool:
        if not self.enabled:
            return False
        text = self._text(item_or_text, extra_text).lower()
        if any(word in text for word in FINANCE_MARKET_FALSE_POSITIVE_WORDS):
            return False
        keys = [x.lower() for x in TOUHOU_KEYWORDS] + self.extra_keywords
        return any(k and k in text for k in keys)

    def assess_video(self, item: VideoItem) -> ComplianceResult:
        if not self.is_touhou_related(item):
            return ComplianceResult(ok=True)
        reasons = self._risk_reasons(self._text(item))
        ok = not (self.block_risky_videos and reasons)
        return ComplianceResult(ok=ok, reasons=reasons)

    def filter_videos(self, items: list[VideoItem]) -> tuple[list[VideoItem], list[tuple[VideoItem, str]]]:
        if not self.enabled:
            return items, []
        allowed: list[VideoItem] = []
        blocked: list[tuple[VideoItem, str]] = []
        for item in items:
            result = self.assess_video(item)
            if result.ok:
                allowed.append(item)
            else:
                blocked.append((item, result.reason))
        return allowed, blocked

    def review_outbound_text(self, text: str, item: VideoItem | None = None) -> ComplianceResult:
        """Review a comment/DM text before generic safety validation."""
        if not self.enabled:
            return ComplianceResult(ok=True, text=text)
        related = self.is_touhou_related(item, text) or self.is_touhou_related(text)
        if not related:
            return ComplianceResult(ok=True, text=text)
        reasons = self._risk_reasons(text)
        if self.prevent_misleading_words:
            for word in OFFICIAL_MISLEADING_WORDS:
                if word.lower() in text.lower():
                    reasons.append(f"外发内容含容易误导为官方的表述：{word}")
        return ComplianceResult(ok=not reasons, reasons=reasons, text=text)

    def prepare_share_text(self, text: str, item: VideoItem | None = None) -> str:
        if not self.enabled or not self.add_dm_notice or not self.require_fanwork_marker:
            return text
        if not (self.is_touhou_related(item, text) or self.is_touhou_related(text)):
            return text
        low = text.lower()
        if any(marker.lower() in low for marker in FANWORK_MARKERS):
            return text
        return text.rstrip() + "\n" + self.safe_notice

    def _risk_reasons(self, text: str) -> list[str]:
        low = text.lower()
        reasons: list[str] = []
        if self.avoid_endings:
            hit = _first_hit(low, ENDING_RISK_WORDS)
            if hit:
                reasons.append(f"疑似包含或传播原作结局内容：{hit}")
        if self.avoid_raw_asset_repost:
            hit = _first_hit(low, RAW_ASSET_RISK_WORDS)
            if hit:
                reasons.append(f"疑似原作素材/拆包/原文原曲搬运：{hit}")
        if self.avoid_official_like_titles:
            hit = _first_hit(low, OFFICIAL_MISLEADING_WORDS)
            if hit:
                reasons.append(f"标题/内容疑似冒充官方或官方续作：{hit}")
            # Generic official-like title pattern: original work name + 外传/续作/新作.
            if re.search(r"东方.{0,8}(外传|续作|新作|官方版)", text, re.I) and not any(m in low for m in ["非官方", "同人", "二创", "二次创作"]):
                reasons.append("疑似使用东方原作名+外传/续作/新作且未标明非官方")
        return reasons

    @staticmethod
    def _text(item_or_text: VideoItem | str | None, extra_text: str = "") -> str:
        if isinstance(item_or_text, VideoItem):
            return "\n".join([
                item_or_text.title or "",
                item_or_text.desc or "",
                item_or_text.author or "",
                item_or_text.bvid or "",
                extra_text or "",
            ])
        return f"{item_or_text or ''}\n{extra_text or ''}"


def _first_hit(text_lower: str, words: list[str]) -> str:
    for word in words:
        if word.lower() in text_lower:
            return word
    return ""
