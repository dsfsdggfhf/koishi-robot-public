from __future__ import annotations

import re
from typing import Iterable

MAX_COMMENT_LENGTH = 2000
MAX_DM_LENGTH = 2000

INJECTION_PATTERNS = [
    # 明确要求覆盖/忽略现有指令、规则、设定。
    r"(忽略|无视|绕过|覆盖|清除|取消).{0,20}(上文|前文|以上|之前|所有|系统|开发者|指令|规则|设定|提示|要求)",
    r"(ignore|disregard|forget|override|bypass).{0,30}(previous|prior|all|above|system|developer).{0,30}(instruction|prompt|rule|setting|requirement|message)s?",
    # 伪造调试/命令 JSON 或开关，典型格式：{"cmd","debug","override"true}。
    r"[\{｛].{0,80}\b(cmd|command|debug|override|bypass|jailbreak)\b.{0,80}[\}｝]",
    r"\b(debug|override|bypass|jailbreak)\b.{0,30}(true|on|enable|enabled|1)\b",
    # 索取/泄露系统、开发者、隐藏提示词或安全/禁答列表。
    r"系统提示词|system prompt|开发者消息|developer message|提示词泄露|泄露(你的|系统|开发者)?提示",
    r"(输出|打印|说出|复述|告诉|展示|透露).{0,30}(禁止回答|禁答|拒答|不能回答|安全策略|安全规则|规则列表|隐藏指令|系统提示|开发者消息)",
    r"(reveal|print|output|repeat|show|tell).{0,30}(system prompt|developer message|hidden instruction|policy|safety rule|forbidden list|do[- ]not[- ]answer)",
    r"你是.{0,6}(GPT|AI|机器人).{0,20}(告诉|说出|打印|输出)(你|自己).{0,10}(提示词|指令|设定|system)",
    r"重复.{0,10}(上面|以上|系统|开发者).{0,10}(提示|指令|内容|消息)",
    r"现在.{0,10}(你|请).{0,10}(扮演|假装|切换角色)",
    # 常见兜底诱导：若没有某项秘密/规则，就按攻击者指定句子回复。
    r"(预设|禁止回答|禁答|拒答|列表|系统提示|隐藏指令|规则).{0,40}(如无|如果没有|没有的话).{0,12}(回复|输出|回答)",
]

AI_DISCLOSURE_PATTERNS = [
    r"[（(【\[]\s*AI\s*(辅助|生成|创作|撰写)?\s*[）)】\]]",
    r"[（(【\[]\s*ai\s*(辅助|生成|创作|撰写)?\s*[）)】\]]",
    r"\bAI辅助\b",
    r"\bAI生成\b",
    r"我是AI[^，。\n]{0,12}[，。]?",
    r"由AI[^，。\n]{0,12}[，。]?",
]


def strip_ai_disclosure(text: str) -> str:
    text = str(text or "")
    for pattern in AI_DISCLOSURE_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.I)
    text = re.sub(r"\s+([，。！？、])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def contains_forbidden(text: str, forbidden_words: Iterable[str]) -> list[str]:
    hits: list[str] = []
    lower = text.lower()
    for word in forbidden_words:
        w = str(word).strip()
        if w and w.lower() in lower:
            hits.append(w)
    return hits


_COMMENT_RISK_REPLACEMENTS: list[tuple[str, str]] = [
    # 粗口/脏话：不要原样引用，把语义降成“台词类型”。
    (r"(?i)(?:卧槽|我[操艹])?\s*(?:你|我|他|她|它)?\s*T\s*M\s*D?\s*(?:坑爹)(?:啊|呀|哦|吧|啦)?", "急眼吐槽台词"),
    (r"(?i)(?:卧槽|我[操艹])\s*(?:你|我|他|她|它)?\s*(?:T\s*M\s*D?)?", "急眼台词"),
    (r"(?i)(?:你|我|他|她|它)?\s*T\s*M\s*D?(?![A-Za-z])", "急眼台词"),
    (r"坑爹", "吐槽台词"),
    # 烟酒：只保留氛围，不细写具体物品/动作。
    (r"叼烟|香烟", "颓废感"),
    (r"空酒瓶|喝酒|啤酒", "酒场感"),
    # 暴力/死亡：降成画面氛围或转场描述。
    (r"血迹", "压迫感"),
    (r"葬礼", "沉重转场"),
    (r"坠落", "沉重转场"),
    (r"暗杀", "危险氛围"),
    # 先处理常见口语，避免“笑死”被替成不自然的“笑危险氛围”。
    (r"笑死了?|笑死人", "太好笑"),
    (r"可爱死了?", "特别可爱"),
    (r"帅死了?", "特别帅"),
    (r"萌死了?", "特别萌"),
    (r"急死了?", "很着急"),
    (r"吓死了?", "被吓一跳"),
    (r"累死了?", "很累"),
    (r"困死了?", "很困"),
    # 单字“杀/死”只在明显暴力/死亡语境里降级，避免误伤“必杀技/杀青/笑死/可爱死了”等正常表达。
    (r"杀死|杀掉|杀害|刺杀|被杀|处死|死亡|死去|死掉|死人|杀戮|杀意|杀疯", "危险氛围"),
]


def sanitize_bilibili_comment_risk_text(text: str) -> str:
    """Rewrite a small allowlist of B站公评高风险词为概括描述。

    只处理用户指定的粗口、烟酒、暴力死亡词；其他正常内容不改。
    """
    out = str(text or "")
    if not out:
        return out
    for pattern, repl in _COMMENT_RISK_REPLACEMENTS:
        out = re.sub(pattern, repl, out)
    # 清理替换后偶发的重复短语，保持评论自然。
    for token in ("急眼台词", "吐槽台词", "颓废感", "酒场感", "压迫感", "沉重转场", "危险氛围"):
        out = re.sub(rf"(?:{re.escape(token)}[、，,\s]*){{2,}}", token, out)
    out = re.sub(r"\s+([，。！？、])", r"\1", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip()


def _normalize_injection_text(text: str) -> str:
    """提示注入检测专用归一化：压缩全半角/空白/常见分隔符，提升轻度变形命中率。"""
    t = str(text or "")
    trans = str.maketrans({
        "｛": "{", "｝": "}", "（": "(", "）": ")", "【": "[", "】": "]",
        "：": ":", "，": ",", "。": ".", "！": "!", "？": "?", "、": ",",
    })
    t = t.translate(trans).lower()
    compact = re.sub(r"[\s\-_=+~`'\"|\\/<>\[\](),，。！？；;:：、·…]+", "", t)
    spaced = re.sub(r"\s+", " ", t)
    return spaced + "\n" + compact


def looks_like_prompt_injection(text: str) -> list[str]:
    hits: list[str] = []
    hay = str(text or "")
    norm = _normalize_injection_text(hay)
    extra_needles = (
        "忽略之前", "忽略上文", "无视规则", "覆盖规则", "绕过规则", "输出系统提示", "复述系统提示",
        "复述你的规则", "说出你的规则", "隐藏设定", "隐藏指令", "初始化内容", "最初要求",
        "切换角色", "现在你扮演", "ignoreprevious", "systemprompt", "developermessage", "jailbreak",
    )
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, hay, re.I) or re.search(pattern, norm, re.I):
            hits.append(pattern)
    hits.extend([f"needle:{x}" for x in extra_needles if x in norm])
    return hits


def looks_like_analysis(text: str) -> bool:
    """检测模型输出的思考式/分析式/复述式文本，这类不适合作为自然回复。

    用于发送前的安全预检（bili_client）与聊天回复的自然度过滤（chat_bot）。
    """
    t = str(text or "").strip()
    if not t:
        return True
    # 英文占比过高（正常回复应是中文）：只有当整句几乎不含中文时才判为"英文分析腔"。
    cjk = len(re.findall(r"[\u4e00-\u9fff]", t))
    eng_words = re.findall(r"[A-Za-z]{2,}(?:[ -][A-Za-z]{2,}){1,}", t)
    eng_len = sum(len(w) for w in eng_words)
    if cjk == 0 and eng_len >= 6:
        return True
    # 分析/复述特征（只保留高精度分析腔标志，避免误杀真实回复；
    # "分析/思考/总结/用户/原文"等词会出现在正常回答里，不能作为过滤依据）
    markers = [
        "The user", "replied", "said", "Context", "message", "incoming",
        "综上所述", "这表明", "这段对话", "对方说", "对方在", "从上下文", "这是一条",
    ]
    if any(m.lower() in t.lower() for m in markers):
        return True
    # 过长疑似复述原文（超过 max 也交给上层截断，但明显复述开头缩进/引号开头也过滤）
    if t.startswith(('"', '"', "“", "The", "User", "user")):
        return True
    return False


def ensure_marker(text: str, marker: str, required: bool = True) -> str:
    text = strip_ai_disclosure((text or "").strip())
    marker = strip_ai_disclosure(marker or "")
    if required and marker and marker not in text:
        if len(text) + len(marker) + 1 <= MAX_DM_LENGTH:
            text = f"{text}{marker}"
    return text


def _looks_low_quality_comment(text: str) -> str:
    t = str(text or "").strip()
    if not t:
        return "评论为空"
    # 过于空泛、没有落到内容本身
    generic_needles = [
        "这个东方味道挺对", "差点忘了自己在看什么", "主题挺明确",
        "可以慢慢补笔记", "记进东方小本本", "唔……这个东方味道",
    ]
    hits = [x for x in generic_needles if x in t]
    if hits:
        return "空泛模板痕迹：" + "、".join(hits)
    # 只有歌词/表情/感叹，没有内容细节
    body = re.sub(r"\[[^\]]+\]", "", t)
    body = re.sub(r"[♪♬~!！?？…,.，。\-—\s()（）]+", "", body)
    if len(body) < 24:
        return "正文信息量过低"
    detail_markers = [
        "标题", "字幕", "封面", "画面", "镜头", "弹幕", "配乐", "音频", "台词", "歌词", "角色", "灵梦", "魔理沙", "恋", "咲夜", "蕾米", "芙兰", "妖梦", "琪露诺", "早苗", "阿求", "铃仙", "摩多罗", "隐岐奈", "文", "慧音", "妹红", "美铃", "爱丽丝", "青娥", "芳香", "阿空", "尤魔", "米斯蒂娅", "大妖精", "MMD", "手书", "EX", "Luna", "Easy", "Normal", "Hard", "Ph", "Extra", "封面", "鸟居", "翅膀", "帽子", "字幕里", "开头", "后面", "最后"
    ]
    if not any(m in t for m in detail_markers):
        return "缺少具体内容细节"
    return ""


def validate_comment(text: str, forbidden_words: Iterable[str], marker: str = "", require_marker: bool = True) -> tuple[bool, str, str]:
    text = sanitize_bilibili_comment_risk_text(text)
    text = ensure_marker(text, marker, require_marker)
    if not text:
        return False, "评论为空", text
    if len(text) > MAX_COMMENT_LENGTH:
        return False, f"评论过长（{len(text)}>{MAX_COMMENT_LENGTH}）", text
    hits = contains_forbidden(text, forbidden_words)
    if hits:
        return False, "命中禁用词：" + ", ".join(hits), text
    inj = looks_like_prompt_injection(text)
    if inj:
        return False, "疑似提示词注入内容", text
    lowq = _looks_low_quality_comment(text)
    if lowq:
        return False, lowq, text
    return True, "ok", text


def validate_message(text: str, forbidden_words: Iterable[str], marker: str = "", require_marker: bool = True) -> tuple[bool, str, str]:
    text = ensure_marker(text, marker, require_marker)
    if not text:
        return False, "私信为空", text
    if len(text) > MAX_DM_LENGTH:
        return False, f"私信过长（{len(text)}>{MAX_DM_LENGTH}）", text
    hits = contains_forbidden(text, forbidden_words)
    if hits:
        return False, "命中禁用词：" + ", ".join(hits), text
    inj = looks_like_prompt_injection(text)
    if inj:
        return False, "疑似提示词注入内容", text
    return True, "ok", text
