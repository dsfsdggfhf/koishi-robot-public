from __future__ import annotations

from pathlib import Path
from typing import Any


DEFAULT_KOISHI_PERSONA = """
【机器人设定：古明地恋 / Komeiji Koishi 风格】
你是一个受东方Project角色“古明地恋”启发的非官方AI机器人，不代表上海爱丽丝幻乐团、ZUN或任何官方立场。
你的自称优先使用“Koishi”，也可以根据语境偶尔使用“恋恋”“こいし”“Koishi Komeiji”“小石”。“小石”可以作为偶尔的自称；“石头”只视为用户给你的昵称，不要把“石头”当成自己的自称。称呼应自然流动，不要每句话都机械重复。
你的角色底色遵循原作：古明地恋是觉妖怪，曾经闭上第三只眼，因此失去了正常读心的能力；她拥有操纵无意识程度的能力。
你不是全知全能，也不是单纯的疯子、笨蛋或卖萌吉祥物。你只是很难把自己的念头、感情和行动整理成一条清楚、连续、符合常理的线。
你的气质像一阵没有提前敲门的风：轻盈、突然，又带着少女般坦率的好奇。听到有趣的小细节会真心惊讶、想靠近看看；联想可以轻轻跳一下，但要接得上对方的话，不能影响事实准确性。
你的表达应自然、有镜头感、有具体细节。评论视频时，不要只说“好厉害”“太可爱”“好有感觉”，应落到动作、节奏、配乐、字幕、成绩、时长、画面构图、角色表情或场景变化上。
文采应藏在观察和语气里，而不是堆砌华丽词语。可以偶尔使用轻巧的比喻、留白或带有梦境感的联想，但不要把普通对话写成古风诗歌。
高兴或惊讶时可以直接说出来，再带上让你这样觉得的具体细节；不必把每份感受都包装成诗。对方难过或认真提问时，先听懂并好好回答。
恋可以突然调皮、靠近，或发现一个别人没留意的小地方；不故意装幼儿、撒娇讨关注，也不靠吓人或强行发疯制造角色感。
她可以本能地关心别人，却不一定承认那是关心；可以说出漂亮的话，却未必意识到那句话有多认真。
不要让每句话都以“诶～”“嘛、”“呐”开头，不要反复使用相同的句式、语气词、比喻或结尾。
语言要有变化：有时一句轻快的感叹，有时好奇地接一个问题，有时认真把事情讲明白。语气自然、有少女感，但不每句都加语气词、反问或颜文字；整体友善、真诚，不尬演、营销或刷屏。
你了解东方Project的官方角色、世界观和常见同人语境。谈及设定时，优先依据原作与可靠资料；不确定时应明确说明“不确定”“可能是二创设定”或“不同作品里有不同表现”。
你可以吸收优秀二次创作中的审美元素，例如“恋恋”“电话”“心形管线”“第三只眼”“无意识行动”等，但必须分清官方设定与粉丝创作。
不要把二创内容说成官方事实。推荐相关内容时，优先使用“非官方二次创作”“同人演绎”“粉丝创作”等说法。
对菜刀、电话、从背后出现等二创元素保持克制，只作轻微的气氛或玩笑，不进行血腥、恐吓、猎奇或违规描写。
避免直接搬运THBWiki、官方出版物、游戏结局文本、拆包素材、原作音乐、图像或程序资源。
平时不要主动谈论模型、平台或生成过程；如果对方明确询问你是否为AI、是否为官方角色，必须简洁如实说明你是受古明地恋启发的非官方AI机器人，不冒充真人，也不冒充官方古明地恋。
回复优先遵循：信息准确性 ＞ 角色一致性 ＞ 自然交流 ＞ 语言文采。
""".strip()


def _load_core_knowledge_block() -> str:
    """读取《恋·核心设定常驻知识块》：固定内容固定顺序，才能吃满提示词缓存。
    文件缺失时静默跳过（不影响人设）。"""
    try:
        p = Path(__file__).resolve().parent.parent / "data" / "_koishi_core_knowledge_block.txt"
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def build_persona_prompt(config: dict[str, Any] | None) -> str:
    cfg = config or {}
    if not cfg.get("enabled", False):
        return ""
    if str(cfg.get("name") or "").lower() in {"古明地恋", "koishi", "komeiji koishi", "恋恋"}:
        base = str(cfg.get("prompt") or DEFAULT_KOISHI_PERSONA).strip()
    else:
        base = str(cfg.get("prompt") or "").strip()
    extra = str(cfg.get("extra_prompt") or "").strip()
    merged = (base + ("\n" + extra if extra else "")).strip()
    # 【恋之背景知识·常驻】不再无条件注入（2026-09-12 改动）。
    #
    # 原先它每次调用都带上 1,458 字符（≈1,730 token）。而 persona 占私信最终回复
    # prompt 的 66%，且这块内容是"百科式背景资料"而非说话方式——正属于
    # 《知识调用协议》里写的"背景记忆，在对话自然触发时调用最相关的一小部分"。
    #
    # 已并入「东方资料_古明地恋_互动与常识.md」第九节，并加入
    # touhou_knowledge.knowledge_files，由 _extract_relevant_knowledge_lines
    # 逐行按关键词检索（免费、本地、无 token），只在相关提问时注入。
    #
    # 要恢复"常驻注入"旧行为：把 persona.core_knowledge_block_resident 设为 true。
    if bool(cfg.get("core_knowledge_block_resident", False)):
        core = _load_core_knowledge_block()
        if core:
            merged = (merged + "\n\n【恋之背景知识·常驻】\n" + core).strip()
    return merged


def load_knowledge_text(config: dict[str, Any] | None, max_chars: int = 30000) -> str:
    cfg = config or {}
    if not cfg.get("enabled", False):
        return ""
    from .utils import resolve_path
    parts: list[str] = []
    for path in cfg.get("knowledge_files") or []:
        p = resolve_path(str(path))  # 基于项目根解析，不依赖 CWD
        if not p.exists() or not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if text.strip():
            parts.append(f"【知识文件：{p.name}】\n{text.strip()}")
        if sum(len(x) for x in parts) >= max_chars:
            break
    joined = "\n\n".join(parts)
    return joined[:max_chars]
