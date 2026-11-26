from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class CorrectionCase:
    wrong: str
    correct: str
    reason: str = ""


_CORRECTION_MARKERS = ["不是", "认错", "认成", "错了", "纠正", "更正", "应该是", "其实是", "看错", "不是魔理沙"]


def detect_character_correction(text: str) -> CorrectionCase | None:
    """Detect simple user corrections like '不是魔理沙，是安娜贝尔·卡娜'.

    Conservative by design: only returns a case when correction markers are present
    and both wrong/correct names can be extracted.
    """
    s = str(text or "").strip()
    if not s or not any(m in s for m in _CORRECTION_MARKERS):
        return None
    s = re.sub(r"[\s\u3000]+", "", s)
    # “绿发的不是四季映姬吗？”是反问确认，不能反向学成“绿发不是四季映姬吗”。
    # 没有明确的错误角色名时不生成二元纠错，交给画面和用户确认记录。
    if re.search(r"不是[^，,。！!？?、；;]{1,24}[吗么麼](?:[？?]|$)", s):
        return None
    # 不是X，是Y / 不是X而是Y / 不是X应该是Y
    patterns = [
        r"不是(?P<wrong>[^，,。！!？?、；;]{1,20}?)[，,。！!？?、；;]*(?:而是|是|应该是|其实是)(?P<correct>[^，,。！!？?、；;]{1,24})",
        r"(?P<wrong>[^，,。！!？?、；;]{1,20}?)(?:认错了|看错了|错了)[，,。！!？?、；;]*(?:这是|是|应该是|其实是)?(?P<correct>[^，,。！!？?、；;]{1,24})",
        r"(?:你)?把(?P<correct>[^，,。！!？?、；;]{1,24})认成(?P<wrong>[^，,。！!？?、；;]{1,20})",
        r"(?P<correct>[^，,。！!？?、；;]{1,24})不是(?P<wrong>[^，,。！!？?、；;]{1,20})",
    ]
    for pat in patterns:
        m = re.search(pat, s)
        if not m:
            continue
        wrong = _clean_name(m.group("wrong"))
        correct = _clean_name(m.group("correct"))
        if _valid_name(wrong) and _valid_name(correct) and wrong != correct:
            return CorrectionCase(wrong=wrong, correct=correct, reason="user_comment_correction")
    # Common explicit case: '安娜贝尔·卡娜，不是魔理沙'
    if "安娜贝尔" in s and "魔理沙" in s:
        return CorrectionCase(wrong="魔理沙", correct="安娜贝尔·卡娜", reason="known_kana_marisa_correction")
    return None


def learn_correction(db: Any, case: CorrectionCase, *, source: str = "comment-correction", video_id: str = "") -> dict[str, Any] | None:
    """Store a correction in TouhouLearningDB-like object."""
    if not case:
        return None
    title = f"角色识别纠错：{case.correct} 不是 {case.wrong}"
    content = (
        f"用户纠错案例：曾把『{case.correct}』误认成『{case.wrong}』。"
        f"以后遇到类似画面/标题/评论语境时，要优先检查是否是『{case.correct}』，不要直接按『{case.wrong}』判断。"
    )
    if case.correct == "安娜贝尔·卡娜" and case.wrong == "魔理沙":
        content += " 安娜贝尔·卡娜（Kana Anaberal）与雾雨魔理沙都可能有金发/帽子等容易混淆的视觉元素；识别时要结合服饰、出处、标题和评论区纠正信息。"
    if video_id:
        content += f" 案例来源视频：{video_id}。"
    return db.add(
        title=title,
        content=content,
        category="character",
        source=source + (f":{video_id}" if video_id else ""),
        tags=["角色识别纠错", case.correct, case.wrong, "自动学习"],
        official=False,
        confidence="high",
    )


def _clean_name(s: str) -> str:
    s = str(s or "").strip()
    # B站表情/后续吐槽可能被正则一起捕获，例如：幽香啊你这个baka[表情]
    # 先截断表情和明显的口语尾巴，避免把聊天语气存成角色名。
    s = s.split("[", 1)[0].split("【", 1)[0]
    s = re.sub(r"\[[^\]]*\]", "", s)
    s = re.sub(r"^(这是|这个是|那是|那个是|这|这个|那|那个|角色|人物|图里|画面里|视频里|你把)", "", s)
    s = re.sub(r"^(是)", "", s)
    s = re.split(r"(?:啊|呀|哦|啦|吧|了)?(?:你|妳|您)(?:这个|个)?", s, maxsplit=1)[0]
    s = re.split(r"(?:baka|笨蛋|傻瓜|傻|错啦|错了)", s, maxsplit=1, flags=re.IGNORECASE)[0]
    s = re.sub(r"(吧|啊|呀|哦|啦|了|的)+$", "", s).strip()
    aliases = {
        "安娜贝尔卡娜": "安娜贝尔·卡娜",
        "卡娜": "安娜贝尔·卡娜",
        "kana": "安娜贝尔·卡娜",
        "Kana": "安娜贝尔·卡娜",
        "雾雨魔理沙": "魔理沙",
        "东风谷早苗": "东风谷早苗",
        "早苗": "东风谷早苗",
        "风见幽香": "风见幽香",
        "幽香": "风见幽香",
    }
    return aliases.get(s, s)


def _valid_name(s: str) -> bool:
    if not s or len(s) > 24:
        return False
    bad_exact = {"不是", "应该", "其实", "认错", "错", "这个", "那个", "你", "我", "它"}
    # 外观描述不是角色名，不能给整组画面贴上“绿发”之类的标签。
    if re.fullmatch(r"(?:那个|这个|那位|这位)?(?:绿|绿色|金|金色|黑|黑色|蓝|蓝色|红|红色|粉|粉色|白|白色)(?:发|头发)(?:的|角色|少女|女孩)?", s):
        return False
    if s in bad_exact:
        return False
    bad_fragments = ["[", "]", "【", "】", "baka", "你这个", "这个baka", "表情"]
    if any(b.lower() in s.lower() for b in bad_fragments):
        return False
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z]", s))
