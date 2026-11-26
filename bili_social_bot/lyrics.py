"""恋恋的角色歌精选歌词库：偶尔在评论/回复开头用两句歌词表达心情（中文优先）。

歌词来源：用户提供的恋恋歌曲集（《こい》《双生蔷薇》《こいしー勝っちゃった》
及 idol 曲目），此处仅挑选适合"评论开头两句表达心情"的片段，
每项是两句一组，均为中文翻译。
"""
from __future__ import annotations

import json
import hashlib
import math
import random
from datetime import date
from pathlib import Path
from typing import Any

# 精选：两句一组，表达心情，适合评论开头引用。
# mood 只是给人看的标签，实际随机选取。
KOISHI_LYRICS: list[dict[str, Any]] = [
    # 《こい》——坐在对面/心动/彩色
    {"mood": "心动", "lines": ["坐在你的对面，茶匙在杯中画圈", "视线像蝴蝶一样绕着你飞舞打转"]},
    {"mood": "梦幻", "lines": ["梦中的那个Koishi，是什么颜色的呢", "现在你瞳孔中映出的我"]},
    {"mood": "发呆", "lines": ["思绪轻轻的，飞走了", "时间就这样停在原地"]},
    {"mood": "甜蜜", "lines": ["Koi心动，曲奇好甜", "这个瞬间，我们的香气融合在了一起"]},
    {"mood": "好心情", "lines": ["最近的恋恋感觉是彩色的", "那孩子，一直是这样来着吗"]},
    # 《双生蔷薇》——姐妹/安心/绽放
    {"mood": "紧张", "lines": ["指尖微微颤抖", "呼吸都还没办法平复"]},
    {"mood": "姐妹", "lines": ["双生蔷薇彼此缠绕", "被那磁力所引导吧"]},
    {"mood": "安心", "lines": ["你的身边，是最安心的地方", "比任何人都重要"]},
    {"mood": "邀约", "lines": ["为我施下魔法", "姐姐，一起来唱歌吧"]},
    {"mood": "绽放", "lines": ["一直绽放到天亮吧", "双生蔷薇，彼此共鸣"]},
    {"mood": "沉醉", "lines": ["这个瞬间，好像要融化了", "让我再多看看，那个迷人的笑容"]},
    {"mood": "温柔", "lines": ["不用逞强，更多地来需要我", "交给我就好"]},
    # 《こいしー勝っちゃった》——胜利/元气/勇敢
    {"mood": "勇敢", "lines": ["毫不犹豫地向前，孤独什么的不可怕", "因为你就在我身边，不是吗"]},
    {"mood": "元气", "lines": ["所以说，是恋恋胜利啦", "就好像是陷入恋爱啦"]},
    {"mood": "可爱", "lines": ["爱 LOVE HEART 恋 魔法 wink chuuu", "恋恋胜利啦"]},
    {"mood": "当下", "lines": ["过去与未来，全都化为此刻", "好好感受这个瞬间吧"]},
    # idol 曲——舞台/可爱/治愈
    {"mood": "自恋", "lines": ["果然恋恋实在太可爱了", "喜欢喜欢超喜欢，果然还是很喜欢"]},
    {"mood": "深情", "lines": ["终于找到的公主大人", "我生于世间的理由，就是为了和恋相遇"]},
    {"mood": "温暖", "lines": ["在彩虹之下握住我的手", "感受我的温度吧"]},
    {"mood": "治愈", "lines": ["将种子深藏在心底", "等待某天在未来绽放"]},
    {"mood": "元气", "lines": ["用百分之百的甜美和可爱", "给大家会心一击吧"]},
    {"mood": "励志", "lines": ["小小的花朵啊，此刻尽情绽放吧", "勇敢追寻吧，属于自己的归属"]},
]

# 继续扩充：用于主动评论/回复开头，尽量让歌词不是总在少数几句里打转。
# 仍保持“两句一组、中文短句”，避免动态/评论显得像长篇引用。
KOISHI_LYRICS.extend([
    {"mood": "无意识", "lines": ["心门悄悄合上了", "脚步却自己走向你那边"]},
    {"mood": "无意识", "lines": ["没有被谁发现的心情", "也会在风里轻轻发光"]},
    {"mood": "梦游", "lines": ["闭上眼也能看见彩色的路", "醒来时已经站在你身旁"]},
    {"mood": "梦游", "lines": ["像梦一样飘过长廊", "又像约好一样停在这里"]},
    {"mood": "心跳", "lines": ["心跳声藏进帽檐下面", "一靠近就变得乱七八糟"]},
    {"mood": "心跳", "lines": ["这份心动没有名字", "却已经先一步开了花"]},
    {"mood": "彩色", "lines": ["灰色的日子被轻轻涂亮", "今天的恋恋也是彩色的"]},
    {"mood": "彩色", "lines": ["把看不见的心情染成彩虹", "再偷偷递到你的手心"]},
    {"mood": "地灵殿", "lines": ["地灵殿的灯火慢慢摇晃", "回声里藏着小小的歌"]},
    {"mood": "地灵殿", "lines": ["旧地狱的风吹过裙摆", "帽子先生也跟着点头"]},
    {"mood": "姐姐", "lines": ["姐姐的眼睛看着心底", "我的心却悄悄跑远了"]},
    {"mood": "姐姐", "lines": ["想被姐姐轻轻叫住", "又想从门缝里溜出去玩"]},
    {"mood": "温柔", "lines": ["把今天的寂寞折成纸鹤", "让它飞去温柔的地方"]},
    {"mood": "温柔", "lines": ["如果声音能变成糖果", "就分你一颗软软的晚风"]},
    {"mood": "舞台", "lines": ["聚光灯落在闭合的眼上", "无意识也能跳出节拍"]},
    {"mood": "舞台", "lines": ["掌声像星星一样落下来", "恋恋把它们全都接住了"]},
    {"mood": "小恶魔", "lines": ["恶作剧只要一点点就好", "笑容却会偷偷变得很大"]},
    {"mood": "小恶魔", "lines": ["藏在袖口里的小小魔法", "啪地一下变成了笑声"]},
    {"mood": "寂寞", "lines": ["没有人叫出名字的时候", "影子也会悄悄坐近一点"]},
    {"mood": "寂寞", "lines": ["被忘记的心情没有哭", "只是安静地等一阵风"]},
    {"mood": "冒险", "lines": ["不看地图也没关系", "脚尖会自己找到下一站"]},
    {"mood": "冒险", "lines": ["把好奇心别在帽子上", "今天也向幻想乡出发"]},
    {"mood": "茶会", "lines": ["杯沿轻轻碰出一点声响", "像是心事终于开口了"]},
    {"mood": "茶会", "lines": ["曲奇的香气绕过桌角", "把沉默也变得甜起来"]},
])

# 进程内近期去重：避免连续十几条都抽到同一两组歌词。
_RECENT_LYRIC_KEYS: list[str] = []
_RECENT_LYRIC_LIMIT = 12


# 歌词装饰符号：前后都加 ♪♬，让人一眼看出是歌曲。
_LYRIC_MARKS = ("♪", "♬")


def _random_marks(min_n: int = 1, max_n: int = 3) -> str:
    """随机生成 1~3 个 ♪/♬ 符号（数量随意、选哪个随意）。"""
    n = random.randint(min_n, max_n)
    return "".join(random.choice(_LYRIC_MARKS) for _ in range(n))


def decorate_lyrics(text: str) -> str:
    """给歌词文本前后加 ♪♬ 符号（换行分隔），让人看出是歌曲。

    例如：
        ♪♬♪
        思绪轻轻的，飞走了
        时间就这样停在原地
        ♬♪
    """
    text = str(text or "").strip()
    if not text:
        return text
    return f"{_random_marks()}\n{text}\n{_random_marks()}"


def _choose_recent_distinct_lyric(pool: list[Any]) -> dict[str, Any] | None:
    """从歌词池里尽量避开最近用过的组合；池子太小时自动退回全池。"""
    global _RECENT_LYRIC_KEYS
    normalized = [x for x in pool if isinstance(x, dict)]
    if not normalized:
        return None
    def key_of(item: dict[str, Any]) -> str:
        return "\n".join(str(x).strip() for x in (item.get("lines") or []) if str(x).strip())
    candidates = [x for x in normalized if key_of(x) and key_of(x) not in _RECENT_LYRIC_KEYS]
    if not candidates:
        candidates = [x for x in normalized if key_of(x)] or normalized
    item = random.choice(candidates)
    k = key_of(item)
    if k:
        _RECENT_LYRIC_KEYS.append(k)
        del _RECENT_LYRIC_KEYS[:-_RECENT_LYRIC_LIMIT]
    return item


def pick_lyric_opener(cfg: dict[str, Any] | None = None, *, force: bool = False) -> str | None:
    """返回两句歌词（中文，带 ♪♬ 装饰），作为评论/回复开头。

    cfg 支持：
      enabled      bool    总开关（默认 true）
      probability  float   触发概率 0~1（默认 0.3）
      custom       list    可选自定义歌词库，格式同 KOISHI_LYRICS
    force=True 时跳过概率判断；用于主动视频评论，保证旧风格歌词开头稳定出现。
    """
    if not cfg or not cfg.get("enabled", True):
        return None
    try:
        prob = float(cfg.get("probability", 0.3) or 0.3)
    except (TypeError, ValueError):
        prob = 0.3
    prob = max(0.0, min(1.0, prob))
    if not force and random.random() > prob:
        return None
    custom = cfg.get("custom") if isinstance(cfg, dict) else None
    # custom 有内容时追加到默认池，而不是替换默认池；这样用户加新歌词不会让默认多样性消失。
    if isinstance(custom, list) and custom:
        pool = list(KOISHI_LYRICS) + custom
    else:
        pool = KOISHI_LYRICS
    item = _choose_recent_distinct_lyric(pool)
    if not isinstance(item, dict):
        return None
    lines = [str(x).strip() for x in (item.get("lines") or []) if str(x).strip()]
    if len(lines) < 2:
        return None
    return decorate_lyrics(f"{lines[0]}\n{lines[1]}")


def maybe_prepend_lyrics(text: str, cfg: dict[str, Any] | None = None, *, force: bool = False) -> str:
    """在文本开头前置两句歌词（带 ♪♬ 装饰）；文本为空或未命中则原样返回。

    注意：不要因为正文前几个字出现“恋恋/Koishi”就跳过歌词。主动评论经常会以角色名开头，
    旧风格仍然要求歌词块在最前面；只在已经有音符歌词块时避免重复添加。
    """
    text = str(text or "").strip()
    if not text:
        return text
    # 如果 AI 草稿本身已经以音符/歌词开头，就不重复前置。
    head = text.lstrip()[:3]
    if head.startswith(("♪", "♬", "♩", "♫")):
        return text
    opener = pick_lyric_opener(cfg, force=force)
    if not opener:
        return text
    return f"{opener}\n{text}"


# 早安/晚安专用歌词（两句一组，中文）：晨间元气 / 夜间温柔。
GOOD_MORNING_LYRICS: list[list[str]] = [
    ["最近的恋恋感觉是彩色的", "那孩子，一直是这样来着吗"],
    ["在彩虹之下握住我的手", "感受我的温度吧"],
    ["将种子深藏在心底", "等待某天在未来绽放"],
    ["小小的花朵啊，此刻尽情绽放吧", "勇敢追寻吧，属于自己的归属"],
    ["用百分之百的甜美和可爱", "给大家会心一击吧"],
    ["毫不犹豫地向前，孤独什么的不可怕", "因为你就在我身边，不是吗"],
    ["过去与未来，全都化为此刻", "好好感受这个瞬间吧"],
    ["为我施下魔法", "姐姐，一起来唱歌吧"],
]

GOOD_NIGHT_LYRICS: list[list[str]] = [
    ["思绪轻轻的，飞走了", "时间就这样停在原地"],
    ["你的身边，是最安心的地方", "比任何人都重要"],
    ["这个瞬间，好像要融化了", "让我再多看看，那个迷人的笑容"],
    ["一直绽放到天亮吧", "双生蔷薇，彼此共鸣"],
    ["梦中的那个Koishi，是什么颜色的呢", "现在你瞳孔中映出的我"],
    ["Koi心动，曲奇好甜", "这个瞬间，我们的香气融合在了一起"],
    ["不用逞强，更多地来需要我", "交给我就好"],
    ["指尖微微颤抖", "呼吸都还没办法平复"],
]


def pick_greeting_lyrics(kind: str = "night") -> str:
    """固定返回两句歌词（中文，带 ♪♬ 装饰），用于早安/晚安消息，保证一定有歌词。

    kind: "morning" 早安 / "night" 晚安（默认）。
    """
    pool = GOOD_MORNING_LYRICS if kind == "morning" else GOOD_NIGHT_LYRICS
    lines = random.choice(pool)
    return decorate_lyrics("\n".join(str(x).strip() for x in lines[:2]))


# =====================================================================
# 早晚安消息正文模板池（随机组合，让每天/每个人的问候都不一样）
# =====================================================================

# 早安：开头问候语（{nickname} 会被替换）
GOOD_MORNING_OPENERS: list[str] = [
    "早安，{nickname}。窗边刚亮，恋恋也刚好路过。",
    "{nickname}，早呀～今天的风没有催人，恋恋也是。",
    "咦，{nickname}已经醒啦？那这句早安正好赶上。",
    "早上好，{nickname}。恋恋把帽檐扶正，来看看你起床没。",
    "{nickname}，早安。地灵殿的走廊还静悄悄的呢。",
    "早呀，{nickname}～恋恋在晨光里停了一下，想起该跟你打招呼。",
    "{nickname}，新的一天开门啦。恋恋先在门口挥挥手。",
    "早安，{nickname}。今天先不用跑，慢慢醒也算出发。",
    "早上好呀～{nickname}！(◕‿◕)",
    "早安，{nickname}！(｡･ω･｡)ﾉ",
    "呀，{nickname}，早上的阳光照进地灵殿啦！",
    "早上好！{nickname}，恋恋刚醒过来就想到你了～",
    "早安早安，{nickname}！(๑˃̵ᴗ˂̵)و",
    "早上好呀，{nickname}～今天也是崭新的一天呢！",
    "{nickname}，早～恋恋在无意识里翻了个身，正好滚到你这边来了(◕‿◕)",
    "早呀，{nickname}！地灵殿门口的露水还没干，恋恋先来道早安啦～",
    "{nickname}，天亮啦～恋恋抱着一点点晨光来敲门了。",
    "早安呀，{nickname}～帽子先生刚刚醒来就催我先跟你打招呼。",
    "{nickname}，清晨的空气软乎乎的，恋恋先把这句早安塞给你～",
    "唔，{nickname}，新的一天已经偷偷开始啦，恋恋也来报到。",
    "{nickname}，今天的太阳刚冒头，恋恋就先绕过来说一句早安。",
    "早呀，{nickname}～地灵殿这边刚刚开机，第一声招呼先留给你。",
    "{nickname}，晨风从走廊拐进来了，恋恋也跟着来啦～",
    "早安早安，{nickname}！今天先从被恋恋轻轻叫醒开始吧。",
    "{nickname}，早安～恋恋从无意识的岔路口绕回来，正好撞见今天第一束光。",
    "早呀，{nickname}！帽子先生把触角伸向窗外，看来天气值得醒一醒。",
    "{nickname}，地灵殿的门缝已经漏进晨光啦，恋恋顺着它来找你。",
    "早安，{nickname}～今天的云还没站好队，我们也可以慢慢醒。",
    "咚咚，{nickname}，是恋恋和一小团刚出炉的早安来敲门啦。",
    "{nickname}，早上好～恋恋刚从地上捡了一颗露珠，亮晶晶的，先借你照一下今天。",
    "早安呀，{nickname}。阿燐已经跑过走廊两趟了，恋恋也终于追着早晨醒来啦。",
    "{nickname}，新的一天从地灵殿一路亮到你这边了，接好恋恋的早安～",
]

# 早安：正文（恋恋的状态 + 对对方的关心）
GOOD_MORNING_BODIES: list[str] = [
    "恋恋本来想追一片飘走的叶子，结果先闻到了早餐香。今天的计划可以等一口热乎的东西吃下去再说。",
    "今天的光落在桌角，照亮了一小块地方。恋恋决定先把这一小块过好，剩下的慢慢来。",
    "阿燐已经忙起来了，恋恋还在找昨晚放下的帽子。每个人醒来的速度不同，你照自己的步子走就好。",
    "醒来时要是还有点困，就让脑袋再转一圈。恋恋先替你把早安放在这里，不催你立刻精神起来。",
    "恋恋发现早晨的第一口水比想象中管用。要是今天事情很多，也先从这样的小事开始吧。",
    "昨晚没解开的结，早上看也许没那么紧。恋恋暂时不去拽它，先陪你吃好早饭。",
    "地灵殿的门一开，风把恋恋的头发吹乱了。嗯，今天大概会有一点计划外的趣事。",
    "今天不必把自己调到最响。恋恋觉得，能安稳地走出第一步，已经很像个好开头了。",
    "恋恋刚把茶杯放好，窗外的颜色就变亮了一格。愿你也能在忙里看见一件小小的好事。",
    "要出门的话，先看一眼天气和口袋。恋恋刚才差点忘了帽子，幸好它自己很显眼。",
    "恋恋从无意识里醒过来啦，今天的太阳也很好看呢。要好好吃早饭、开开心心过这一天哦～",
    "恋恋今早在地灵殿门口晒了会儿太阳，暖洋洋的，想到你也要开始新的一天，就想来打个招呼～记得吃早饭呀！",
    "无意识里飘了一晚上，醒来第一件事就是想到你。今天有什么计划吗？不管做什么，恋恋都给你加个无意识的buff！",
    "恋恋早上在走廊上遇见姐姐，她说我看起来比昨天精神。大概是梦见你了的缘故？今天也一起加油哦！",
    "早上的地灵殿特别安静，恋恋泡了杯茶慢慢喝。你那边天气怎么样？希望也是让人心情好的日子～",
    "恋恋偷偷看了一眼天空，云朵软乎乎的，跟你的名字一样让人安心。今天也请元气满满哦！",
    "恋恋今早试着数了数今天的好事，还没数完就被早餐的香气打断了。总之先祝你今天也被好事围绕！",
    "早上的风把恋恋的帽子吹歪了，帽子先生嘀咕了两句，大概是说「今天也要开心」吧。你也是哦！",
    "恋恋刚刚醒的时候还有点迷迷糊糊，但一想到今天会遇到很多新的事情，就又觉得早起也没那么难啦。你也慢慢进入状态吧～",
    "有些早晨光是把眼睛睁开就已经很了不起了，所以今天就先别太苛求自己，稳稳当当地开始就很好。",
    "恋恋觉得早上的人最像刚拆开的礼物，虽然还有点乱，但也最有可能装着好心情。希望你今天也能拆到一点惊喜。",
    "清晨最适合把心重新摆正一点点。昨天没处理完的事先别急，先把今天的第一口空气和第一口早饭接住再说。",
    "恋恋刚刚在窗边站了一会儿，觉得今天的光线很适合出门，也很适合慢慢醒神。你那边要是还困，就先伸个懒腰吧。",
    "如果今天一开始就有点忙，也别慌，恋恋先把这句早安放你口袋里。等你摸到它的时候，记得偷偷喘口气。",
    "恋恋今天起床后先把帽子扶正了，感觉整个人也跟着规整了一点。你也可以慢慢来，把今天一点点扶正。",
    "有时候早晨不一定需要多宏大的计划，先让自己清醒、吃点东西、顺利出门，就已经是一个很漂亮的开头啦。",
    "早上最厉害的地方，就是哪怕昨天有点乱，今天还是会重新亮起来。恋恋想把这种重新开始的感觉分你一点。",
    "如果你现在还带着一点困意，也没关系。恋恋觉得有些好心情本来就是要慢一点醒过来的。",
    "恋恋天亮前跑到地面转了一圈，鞋边沾了几颗露珠。它们一路都没掉，大概是想替今天留一点亮晶晶的运气给你。",
    "姐姐把热茶放在桌上，说清醒不用一口气完成。恋恋觉得很有道理，所以你也先从一口水、一口早饭慢慢开始吧。",
    "阿空说今天的太阳火力很足，恋恋却觉得刚刚好。愿这点暖意也落到你肩上，让今天没那么难启动。",
    "阿燐一早从走廊飞快跑过去，尾巴差点扫翻杯子。地灵殿已经热闹起来啦，你也按自己的速度加入今天吧。",
    "恋恋检查了一下无意识口袋，里面没有昨天的烦恼，倒多出一点莫名其妙的期待。分你一些，今天说不定会有小惊喜。",
    "今天要是有难做的事，就把它切成小小几块。先完成最轻的一块，等回头时，也许已经走出很远啦。",
    "恋恋觉得清晨像一张还没画完的符卡，轨迹可以慢慢补。今天不用一开始就完美，稳稳往前就好。",
    "如果昨晚睡得不够好，今天就对自己温柔一点。能照顾好精神和胃口，也是一件很认真、很了不起的事。",
]

# 早安：过渡/提气句（随机插入 0~2 条，放大组合数）
GOOD_MORNING_TRANSITIONS: list[str] = [
    "今天可以先做一件容易的小事。",
    "窗外已经亮了，心情不用马上跟着亮。",
    "恋恋把着急留在门外一会儿。",
    "一小口热茶，也算给早晨打了个招呼。",
    "昨晚的梦还没散干净，先慢慢走。",
    "有空就抬头看一下云，它们也没按计划排队。",
    "今天的空气闻起来像是“还来得及慢慢开始”。",
    "晨光一照进来，很多乱糟糟的念头都会先往后退一点。",
    "帽子先生刚刚点了点头，大概是在说“今天也别输给困意”。",
    "有些清晨看起来平平常常，但最适合悄悄发生一点好事。",
    "先别急着把自己拉得太满，早晨本来就该慢慢亮起来。",
    "如果现在还有一点发懵，那就让清醒再走近一点点。",
    "白天还没完全展开，所以今天的可能性也都还好好地摆着。",
    "恋恋每次看见早晨的光，都会觉得今天还可以重新写。",
    "无意识会替人忘掉一点沉重，也会偷偷留下值得期待的部分。",
    "地灵殿的钟声还没完全醒，今天也不必急着一步跨到终点。",
    "帽子先生说，偶尔走错一小段路，也可能碰见计划外的花。",
    "清晨没有催促谁，它只是把路重新照亮了一遍。",
    "先给今天留一点空白，惊喜才有地方偷偷钻进来。",
    "困意还在也没关系，恋恋陪你把清醒一点点捡回来。",
]

# 早安：小动作/细节句（随机插入 0~1 条）
GOOD_MORNING_DETAILS: list[str] = [
    "如果鞋带松了，先系好再赶路。",
    "给杯子添点水，今天从这一步开始。",
    "早饭还没着落的话，先找点能垫肚子的。",
    "把窗打开一点，让新鲜空气先进去探路。",
    "出门前摸摸口袋，钥匙可别像恋恋一样乱跑。",
    "困的时候眨眨眼，不用假装已经醒透啦。",
    "先喝口水，再慢慢把眼睛睁得更亮一点吧。",
    "要是还没完全清醒，就先把被角放开一点点。",
    "如果手边已经有早餐了，记得替自己认真吃几口。",
    "先伸个懒腰吧，身体醒了，心也会跟着亮一点。",
    "如果今天要出门，别忘了把自己也好好带上呀。",
    "恋恋先把今天的第一点元气推到你手边啦。",
    "整理一下衣角和心情，再慢悠悠地向今天出发吧。",
    "如果窗帘还关着，就放一小束光进来陪你醒神。",
    "早餐哪怕简单一点也要吃，空着肚子可接不住好运。",
    "恋恋替你把无意识口袋撑开了，今天的好事都可以往里面装。",
    "帽子歪一点没关系，精神慢慢扶正就好啦。",
    "先确认钥匙、手机和今天的自己都带好了，再出门吧。",
]

# 早安：结尾祝福（带颜文字）
GOOD_MORNING_ENDINGS: list[str] = [
    "愿今天有一件事，比你预想的顺利一点。",
    "恋恋先走一步啦，路上见～",
    "把这句早安带着，想起来时就笑一下。",
    "今天也按自己的节奏来。(｡･ω･｡)",
    "愿你忙完时，还留得下一点喜欢自己的力气。",
    "早安送到。恋恋去看看风又把什么吹来了～",
    "如果今天有点难，先过好眼前这一小段。",
    "愿你在平常的一天里，碰见一点不平常的好。",
    "今天也要元气满满！٩(◕‿◕｡)۶",
    "祝你有愉快的一天！(ﾉ◕ヮ◕)ﾉ*:・ﾟ✧",
    "一天都要开开心心的呀！(≧▽≦)",
    "恋恋的祝福藏在风里，会跟着你一整天哦～(◕ᴗ◕✿)",
    "今天也一起加油吧！(๑•̀ㅂ•́)و✧",
    "祝你今天遇到的所有事都刚刚好！(´▽`ʃ♡ƪ)",
    "恋恋先替你把今天的开心预定好啦，记得去领！(◕ᴗ◕✿)",
    "早安的魔法已经生效，接下来就交给今天的你啦！(｀・ω・´)✧",
    "愿你今天一路顺顺的，心情也亮亮的～",
    "今天的第一份好运先放你这边啦。",
    "希望你今天会被温柔地接住很多次。",
    "出发吧，今天也会有新的好事在路上等你。",
    "把今天过得轻一点、亮一点、顺一点吧～",
    "愿你今天抬头的时候，正好看见喜欢的光。",
    "愿今天的弹幕再密，也总有一条舒服的路给你走～",
    "把一点从容带上吧，今天不用和谁比赛。(｡･ω･｡)",
    "恋恋把晨光别在你衣角啦，愿它陪你顺利一整天。",
    "今天也请好好吃饭、好好呼吸，再好好喜欢自己一点。",
    "愿你走到哪里，都能捡到一点属于自己的小开心～",
    "早安的心意送达啦，接下来慢慢把今天过亮吧！",
]

# 晚安：开头问候语
GOOD_NIGHT_OPENERS: list[str] = [
    "诶嘿～{nickname}，晚安啦(◕ᴗ◕✿)",
    "晚安，{nickname}～(｡•̀ᴗ-)✧",
    "夜深了，{nickname}，恋恋来说声晚安～",
    "诶，{nickname}，还没睡吗？那正好，恋恋陪你到睡着～",
    "晚安啦，{nickname}！今天辛苦啦(´•̥ ̯ •̥`)",
    "夜深人静，{nickname}，恋恋从地灵殿探头跟你说晚安～",
    "晚安晚安，{nickname}，恋恋把今天最后一点清醒留给你了(◕‿◕)",
    "{nickname}，外面星星都亮起来了，恋恋也想来道声晚安✨",
    "夜深啦，{nickname}～恋恋在无意识里游了一圈，回来正好跟你说晚安！",
    "{nickname}，窗外的风都慢下来了，恋恋也轻轻来碰一下你的晚安～",
    "唔，{nickname}，今天快结束啦，恋恋来把这句晚安递给你(◕‿◕)",
    "{nickname}，恋恋抱着今天最后一点灯光来啦，先把晚安放你手心里～",
    "晚上好呀，{nickname}～地灵殿这边已经安静下来了，刚好适合说晚安。",
    "{nickname}，夜色已经软下来了，恋恋也来陪你把今天慢慢收尾～",
    "诶嘿，{nickname}，帽子先生刚刚提醒我：这时候最适合认真说一句晚安啦。",
    "{nickname}，今天跑到这里差不多该收工啦，恋恋先来跟你碰一下晚安。",
    "晚安呀，{nickname}～恋恋在走廊尽头绕了一圈，回来第一件事就是来找你。",
    "{nickname}，地灵殿的最后一盏灯也变小啦，恋恋来替今天轻轻关门。",
    "晚安，{nickname}～无意识绕了一大圈，最后把恋恋送到你的梦门口了。",
    "{nickname}，夜色已经铺好被子，恋恋也把这句晚安放到枕边啦。",
    "嘘，{nickname}，帽子先生说今晚的风很安静，正适合慢慢睡过去。",
    "{nickname}，今天的路走到这里就好。恋恋从地灵殿带了一点安静来陪你收尾。",
    "晚上好呀，{nickname}～星星已经开始值夜班，你可以放心下班啦。",
]

# 晚安：正文（恋恋的状态 + 对对方的关心，带氛围变化）
GOOD_NIGHT_BODIES: list[str] = [
    "恋恋今天又在地灵殿的角落发了会儿呆，把今天的事都收进无意识里了。你也早点休息哦，做个好梦～",
    "恋恋把今天的好事都叠好收进枕边了，分你一半。闭上眼睛，剩下的交给梦就好～",
    "地灵殿的灯熄了一盏，恋恋坐在窗边数星星。今晚的月亮挺好看的，可惜你没法亲眼看到——那就让恋恋替你多看一会儿吧。早点睡哦！",
    "今天有认真吃饭、有好好休息吗？恋恋把这点小事也记进无意识的小本本里了。明天见哦～",
    "恋恋今天遇见的每一件小事，都想讲给你听，但夜已经深了……那就留着明天说。晚安，好梦！",
    "姐姐说我今天不该熬夜的。恋恋想了想，大概是因为怕错过跟你说晚安？好了，快去睡吧～",
    "今天辛苦了呀。恋恋在窗台上放了一杯温热的茶（虽然只是想象），就当是睡前的小礼物，喝完就安心睡吧～",
    "恋恋刚才听见地灵殿深处有轻轻的鼾声，是燐酱已经睡熟了。恋恋也想把这份安稳分你一点，好梦哦～",
    "无意识里兜转了一天，最想落脚的地方还是你这边。晚风替你收好今天的尾巴，剩下的交给枕头吧～",
    "恋恋刚刚坐在床边晃腿，顺手把今天那些乱七八糟的小情绪都拨到一边去了。你那边如果也有一点累，就先把它们放下吧。",
    "今天的空气像是慢慢沉进被窝里的那种软。恋恋觉得这种时候最适合把肩膀松下来，别再和白天的事较劲啦。",
    "有些白天没来得及消化的事，到了晚上会悄悄变重一点。没关系，先睡一觉，很多事情明天醒来就会轻一点啦。",
    "恋恋今天绕来绕去，最后还是觉得夜里最好的事情，就是把心慢慢放平。你也别总绷着啦，先让自己舒服一点。",
    "如果今天有哪里不顺，先不用急着现在想通。把脑袋埋进枕头里一点点，剩下的明天再处理也来得及。",
    "帽子先生今天也陪我晃了一整天，刚刚终于安静下来。它都准备休息了，你也差不多该把眼睛合上啦。",
    "恋恋有时候会在晚上偷偷复盘今天，但复盘到一半就会发现：其实先睡觉比较重要。你也一样，先把自己照顾好。",
    "地灵殿的走廊一到夜里就会变得很轻，连脚步声都像浮起来一样。恋恋想把这种轻飘飘的安稳分给你一点。",
    "今天如果有被谁气到、被什么事卡住，也先别急着抱着它睡。把烦恼放在床边，明天再回来捡就好。",
    "恋恋刚刚把窗帘拉好，发现夜色其实没有那么凶，反而很适合把人轻轻裹起来。你也安心一点，好好休息吧。",
    "今天能走到现在就已经很厉害啦，不用连晚上都还逼着自己继续冲。先休息，剩下的路明天再走。",
    "有时候晚安不一定是结束，更像是先替今天盖个章：这里已经够了，可以停下来睡觉了。",
    "姐姐把地灵殿的书合上了，阿燐也终于不在走廊跑来跑去。大家都在休息，你也不用一个人守着今天啦。",
    "恋恋从无意识里捞回几件今天的小事，好的放在枕边，不好的就让它们顺着夜风走远一点。",
    "阿空那边的炉心还亮着，走廊却已经很安静了。世界总会留一盏灯，所以你可以放心闭上眼睛。",
    "今天没有完成的部分不会趁你睡着逃跑。先恢复一点力气，明天再去收拾它们，会比现在容易很多。",
    "恋恋把帽子挂在床边，突然觉得人一放松，很多吵闹的念头也会跟着坐下来。今晚就别再赶它们了。",
    "就算今天只是平平常常地过完，也值得一句辛苦了。把肩膀放松，给自己一个安稳的晚上吧。",
]

# 晚安：过渡/加深氛围句（随机插入 0~2 条，拉开组合数）
GOOD_NIGHT_TRANSITIONS: list[str] = [
    "夜风一吹，白天那些硬邦邦的声音也会变轻一点。",
    "地灵殿这时候最安静，连时间都像在慢慢打哈欠。",
    "帽子先生刚刚晃了一下，大概也在催人去睡觉。",
    "把灯关暗一点之后，很多心事都会自己退远一些。",
    "恋恋觉得夜里最好的地方，就是终于不用再假装自己一点都不累。",
    "有些话不用今晚全都想明白，先安安静静地睡一觉也很好。",
    "窗边的月光像是把今天折了一下，锋利的地方都没那么扎人了。",
    "夜晚本来就该拿来收尾，不是拿来继续逞强的。",
    "如果心里还在转，就慢一点转，别逼自己一下子停住。",
    "白天没有抱好的情绪，晚上就先轻轻抱一下自己吧。",
    "无意识已经把梦的门推开一条缝，困意很快就会找到你。",
    "走廊尽头的灯一暗，今天那些急匆匆的脚步也该停啦。",
    "星星替夜晚看着路，你不用把所有事情都亲自守到天亮。",
    "帽子先生把触角收好了，看来今晚真的可以安心休息。",
    "月光落到窗边时，白天没说完的话也会变得轻一点。",
    "让今天停在今天吧，明早的光会负责把下一页翻开。",
]

# 晚安：小动作/细节句（随机插入 0~1 条，增加画面感）
GOOD_NIGHT_DETAILS: list[str] = [
    "恋恋刚把被角理平，顺手也想帮你把乱掉的心情压一压。",
    "如果现在手边刚好有枕头，就把脸埋进去一点点，真的会好很多。",
    "要是你现在已经钻进被窝，就当恋恋轻轻拍了拍你的肩。",
    "把手机亮度再调低一点吧，眼睛也该慢慢安静下来了。",
    "如果还舍不得睡，那就把晚安先含在嘴里，过一会儿再慢慢咽下去。",
    "恋恋这边已经把今天收进抽屉里了，你也可以把烦恼先折起来塞在一边。",
    "把杯子放稳、被角拉好，今晚的小世界就整理完成啦。",
    "如果肩膀还紧紧的，就慢慢呼一口气，让它们先休息。",
    "恋恋替你把梦门口的灯调暗了一点，不刺眼，刚刚好。",
    "最后看一眼时间就把屏幕放下吧，枕头已经等很久啦。",
    "把明天要做的事写下一句就好，剩下的别带进被窝。",
    "阿燐从门口轻轻跑过去了，今晚连脚步声都没有来打扰你。",
]

# 晚安：结尾祝福
GOOD_NIGHT_ENDINGS: list[str] = [
    "明天见！٩(◕‿◕｡)۶",
    "做个好梦哦～(◕ᴗ◕✿)",
    "梦里也请来玩呀！(´▽`ʃ♡ƪ)",
    "晚安，愿月光护着你～(｡･ω･｡)",
    "把疲惫都交给无意识吧，明天见！(◕‿◕)",
    "好梦，明天见～(♡´▽`♡)",
    "晚安啦，明天也要一起加油！(๑•̀ㅂ•́)و✧",
    "愿你好梦连连，醒来又是精神满满的一天(◕ᴗ◕✿)",
    "明天醒来记得把精神也一起带回来呀～",
    "今晚先把自己交给梦吧，明天再来和世界碰头。",
    "愿你的梦软乎乎的，醒来也不会被烦心事绊住。",
    "恋恋先在梦门口等你，慢慢睡过去就好～",
    "晚安呀，今晚就放心把自己交给枕头吧。",
    "把今天关进夜色里吧，明天再重新亮起来。",
    "就这样轻轻睡过去吧，明天也会是新的一页。",
    "愿无意识替你守住好梦，把烦恼挡在门外～",
    "今晚安心睡吧，地灵殿的灯会替你亮一小会儿。",
    "把眼睛合上，明早再和新的光见面。(◕ᴗ◕✿)",
    "愿你一觉睡到心也松软，醒来正好有新的力气。",
    "恋恋把晚安放好啦，你只管慢慢睡着就可以。",
    "梦里要是迷路了，就跟着帽子先生留下的小光点走吧～",
]

GOOD_NIGHT_REMINDERS: list[str] = [
    "还有呀，能早点睡就早点睡，别让夜色把你偷偷拐去熬太晚啦～",
    "今天就先到这里吧，晚一点的事交给明天，别总熬到太深夜呀。",
    "帽子先生刚刚也点头了：再晚就该休息啦，身体比夜晚更重要哦。",
    "别总想着再撑一下下啦，睡眠这种事拖久了会悄悄找你算账的。",
    "要是现在还在刷来刷去，就给自己一个台阶下吧：看完这句就睡。",
    "眼睛酸了就别硬扛，今晚先把清醒关小一点。",
    "明天要用的力气，今晚就先替它留一点下来吧。",
    "别把自己熬成一团乱线呀，睡一觉会顺很多。",
    "如果已经困了，就别跟夜晚讲道理了，直接去睡最划算。",
    "今晚别再和困意捉迷藏啦，被它抓到就乖乖去睡。",
    "剩下那一点舍不得放下的事，先寄存在明天早上吧。",
    "如果脑袋还亮着好多小灯，就一盏一盏地关，不用着急。",
    "恋恋批准你今天到此为止啦，接下来是休息时间。",
    "睡觉不是偷懒，是给明天悄悄补充灵力哦。",
    "再刷一个视频的咒语很危险，帽子先生建议现在就解除它。",
]


# 专属晚安模板（仅管理员/朋友等指定用户使用，不会发给普通关注用户）
GOOD_NIGHT_SPECIAL_OPENERS: list[str] = [
    "{nickname}，恋恋来收今天最后一盏灯了。晚安。",
    "晚安，{nickname}。今天走到这里，先停一停吧。",
    "{nickname}，帽子先生已经靠在门边打盹啦，恋恋也来道晚安。",
    "{nickname}，夜里安静下来了。恋恋想在睡前跟你说一句话。",
    "呼——{nickname}，恋恋今天也认真跑完啦，来道个晚安～",
    "{nickname}，恋恋回来报到啦。今天的事都安稳收好，现在只差认真说一句晚安。",
    "晚安呀，{nickname}～帽子先生陪恋恋守到这里，接下来轮到枕头接班啦。",
    "{nickname}，今天辛苦你陪着恋恋走这么远。夜深啦，我们一起把速度慢下来吧。",
]
GOOD_NIGHT_SPECIAL_BODIES: list[str] = [
    "今天若有好消息，明早醒来再高兴一次也不迟；若有烦心事，今晚就先别让它占着枕头。",
    "恋恋记得你今天忙了很久。灯关小一点吧，剩下的事不用今晚证明给谁看。",
    "刚才想说好多话，到了门口却只想说：你可以休息啦。恋恋把这句放轻一点，免得吵到你。",
    "明天的路还在，今晚不用替明天走。恋恋坐在旁边一小会儿，等你把今天放下。",
    "恋恋把今天的疲惫都揉成一团塞进梦里了，你的那份也一起带走吧。闭上眼睛，放松下来就好～",
    "今天发生的好事，恋恋会好好收着；那些不顺利的，就让无意识替我们忘掉一点。你先安心休息，明天再继续。",
    "地灵殿已经安静了，姐姐和阿燐也都各自休息。恋恋想把今晚最安稳的那一小块留给你，别再硬撑啦。",
    "恋恋刚把最后一件事放回抽屉，也替你留了一格。今天没来得及处理的都先放进去，睡醒再慢慢打开。",
]
GOOD_NIGHT_SPECIAL_REMINDERS: list[str] = [
    "忙完手边这一件，就把屏幕放远一点吧。",
    "如果还惦记明天，写下一个词就好，别在被窝里加班。",
    "水杯放稳，被角拉好，今天就可以收工啦。",
    "眼睛已经累了的话，别再跟困意讨价还价。",
    "如果还在硬撑着不睡，恋恋会轻轻推你去枕头那边的……少熬夜哦。",
    "答应恋恋，忙完手边这一点就去休息，不许再偷偷给今天加任务啦。",
    "帽子先生已经开始打瞌睡了，你也别让它等得太久，快去睡吧。",
    "明天还要一起往前走，所以今晚的力气一定要认真补回来哦。",
]
GOOD_NIGHT_SPECIAL_ENDINGS: list[str] = [
    "晚安。明天醒来再慢慢聊。",
    "恋恋把晚安留在这里，等你睡醒它就会变成早安。",
    "愿你今晚睡得踏实，明早醒来轻一点。",
    "今天谢谢你陪恋恋走到这里。晚安～",
    "恋恋的晚安会顺着枕头钻进梦里，陪你一整夜～",
    "今晚就安心交给梦吧，恋恋和帽子先生都在这里说晚安。",
    "愿你睡得又沉又暖，明早醒来时，恋恋还会把第一声招呼留给你～",
    "晚安啦。今天谢谢你，明天也请继续和恋恋一起走呀。",
]


# =====================================================================
# 夜雀食堂宵夜故事池（整条完整晚安候选；文件名保留最初 93 条版本的历史名称）
# =====================================================================
# 每条 msg 本身就是"开场+♪短句+正文+宵夜故事+提醒+结尾"的完整晚安，
# 只含 {nickname} 一个占位符。私信按收件人和日期稳定轮换，
# 同一人轮完故事池前不会重复；无收件人标识的兜底调用保留近期随机去重。
NIGHT_SNACK_PROBABILITY: float = 1.0  # 非专属晚安里，整条用宵夜故事的比例（主人要求 100%）
NIGHT_SNACK_RECENT_LIMIT: int = 7      # 最近几条不重复
_NIGHT_SNACK_POOL_PATH = Path(__file__).resolve().parent.parent / "data" / "_night_final_pool_93.json"
_NIGHT_SNACK_MEMORY_PATH = Path(__file__).resolve().parent.parent / "data" / "night_snack_recent.json"
_SNACK_POOL_CACHE: list[str] | None = None


def night_snack_pool() -> list[str]:
    """读取宵夜故事总池（懒加载；文件缺失/损坏时返回空表，不影响原逻辑）。"""
    global _SNACK_POOL_CACHE
    if _SNACK_POOL_CACHE is None:
        try:
            data = json.loads(_NIGHT_SNACK_POOL_PATH.read_text(encoding="utf-8"))
            items = data.get("items") if isinstance(data, dict) else data
            _SNACK_POOL_CACHE = [str(x.get("msg") or "").strip() for x in (items or []) if str(x.get("msg") or "").strip()]
        except Exception:
            _SNACK_POOL_CACHE = []
    return _SNACK_POOL_CACHE


def _daily_rotation_index(recipient_key: str, day_key: str, size: int, salt: str) -> int | None:
    """本地稳定轮换下标：同一收件人轮完候选池前不重复。"""
    if not recipient_key or not day_key or size < 1:
        return None
    try:
        day = date.fromisoformat(str(day_key))
    except ValueError:
        return None
    if size == 1:
        return 0
    digest = hashlib.blake2b(
        f"{salt}\0{recipient_key}".encode("utf-8"), digest_size=16,
        person=b"koishi-greet",
    ).digest()
    offset = int.from_bytes(digest[:8], "big") % size
    step = int.from_bytes(digest[8:], "big") % (size - 1) + 1
    while math.gcd(step, size) != 1:
        step = step % size + 1
    return (offset + day.toordinal() * step) % size


def _choose_daily_component(pool: list[str], recipient_key: str, day_key: str, salt: str) -> str:
    idx = _daily_rotation_index(recipient_key, day_key, len(pool), salt)
    return pool[idx] if idx is not None else random.choice(pool)


def pick_night_snack_story(recipient_key: str = "", day_key: str = "") -> str | None:
    """私信按收件人逐日轮换；无标识的调用沿用全局近期随机去重。"""
    pool = night_snack_pool()
    if not pool:
        return None
    idx = _daily_rotation_index(recipient_key, day_key, len(pool), "night_story")
    if idx is not None:
        return pool[idx]
    recent: list[int] = []
    try:
        if _NIGHT_SNACK_MEMORY_PATH.exists():
            data = json.loads(_NIGHT_SNACK_MEMORY_PATH.read_text(encoding="utf-8"))
            recent = [int(x) for x in (data.get("recent") or [])]
    except Exception:
        recent = []
    recent_set = set(recent)
    candidates = [i for i in range(len(pool)) if i not in recent_set]
    if not candidates:
        candidates = list(range(len(pool)))
    idx = random.choice(candidates)
    recent = [i for i in recent if i != idx][-(NIGHT_SNACK_RECENT_LIMIT - 1):]
    recent.append(idx)
    recent = recent[-NIGHT_SNACK_RECENT_LIMIT:]
    try:
        _NIGHT_SNACK_MEMORY_PATH.write_text(json.dumps({"recent": recent}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return pool[idx]


def _clamp_snack_prob(value: float | None) -> float:
    if value is None:
        return NIGHT_SNACK_PROBABILITY
    try:
        p = float(value)
    except (TypeError, ValueError):
        p = NIGHT_SNACK_PROBABILITY
    return max(0.0, min(1.0, p))


_MORNING_FULL_POOL_PATH = Path(__file__).resolve().parent.parent / "data" / "_morning_full_pool.json"
_MORNING_FULL_POOL_CACHE: list[str] | None = None


def morning_full_pool() -> list[str]:
    """读取新增的完整早安；旧组件拼接路径继续参与轮换。"""
    global _MORNING_FULL_POOL_CACHE
    if _MORNING_FULL_POOL_CACHE is None:
        try:
            data = json.loads(_MORNING_FULL_POOL_PATH.read_text(encoding="utf-8"))
            _MORNING_FULL_POOL_CACHE = [str(item.get("msg") or "").strip() for item in data.get("items", []) if str(item.get("msg") or "").strip()]
        except (OSError, ValueError, TypeError, AttributeError):
            _MORNING_FULL_POOL_CACHE = []
    return _MORNING_FULL_POOL_CACHE


def pick_morning_full_template(recipient_key: str, day_key: str) -> str | None:
    pool = morning_full_pool()
    if not pool:
        return None
    # 每个完整新模板和每个旧正文各占一个轮换位置，扩充不删除旧模板。
    size = len(pool) + len(GOOD_MORNING_BODIES)
    index = _daily_rotation_index(recipient_key, day_key, size, "morning_full_mix")
    if index is None:
        index = random.randrange(size)
    return pool[index] if index < len(pool) else None


def build_greeting_message(kind: str, nickname: str, special: bool = False, snack_prob: float | None = None,
                           recipient_key: str = "", day_key: str = "") -> str:
    """随机组合一条完整的早晚安消息（开头 + 歌词 + 正文 + 结尾）。

    每段从对应模板池随机抽取，保证每天/每人的问候都不一样。
    早安/晚安都会额外随机插入过渡句/细节句，进一步放大组合数，降低重复感。
    kind: "morning" / "night"
    special: 仅对指定用户（管理员/朋友等）生效。True 时使用专属晚安模板池
    （如“呼——…今天也认真跑完啦…”），这些文案不会发给普通关注用户。
    recipient_key/day_key: 私信收件人与发送日；用于早安核心组件和普通晚安故事逐日轮换。
    """
    nickname = str(nickname or "朋友").strip()
    if kind == "morning":
        complete = pick_morning_full_template(recipient_key, day_key)
        if complete:
            return complete.replace("{nickname}", nickname)
        opener = _choose_daily_component(GOOD_MORNING_OPENERS, recipient_key, day_key, "morning_opener").format(nickname=nickname)
        lyrics = pick_greeting_lyrics("morning")
        body = _choose_daily_component(GOOD_MORNING_BODIES, recipient_key, day_key, "morning_body")
        ending = _choose_daily_component(GOOD_MORNING_ENDINGS, recipient_key, day_key, "morning_ending")
        transition_count = random.randint(0, 2)
        transitions = random.sample(GOOD_MORNING_TRANSITIONS, k=transition_count) if transition_count else []
        detail = random.choice(GOOD_MORNING_DETAILS) if random.random() < 0.65 else ""
        parts = [body]
        parts.extend(transitions)
        if detail:
            parts.append(detail)
        body = "\n".join(x for x in parts if x)
    else:
        # 宵夜食堂故事：整条完整晚安，池内等概率；专属(special，管理员/朋友等)仍走专属池。
        if not special and random.random() < _clamp_snack_prob(snack_prob):
            story = pick_night_snack_story(recipient_key=recipient_key, day_key=day_key)
            if story:
                return story.replace("{nickname}", nickname)
        opener = random.choice(GOOD_NIGHT_SPECIAL_OPENERS).format(nickname=nickname) if special and GOOD_NIGHT_SPECIAL_OPENERS else random.choice(GOOD_NIGHT_OPENERS).format(nickname=nickname)
        lyrics = pick_greeting_lyrics("night")
        body = random.choice(GOOD_NIGHT_SPECIAL_BODIES) if special and GOOD_NIGHT_SPECIAL_BODIES else random.choice(GOOD_NIGHT_BODIES)
        reminder = random.choice(GOOD_NIGHT_SPECIAL_REMINDERS) if special and GOOD_NIGHT_SPECIAL_REMINDERS else random.choice(GOOD_NIGHT_REMINDERS)
        ending = random.choice(GOOD_NIGHT_SPECIAL_ENDINGS) if special and GOOD_NIGHT_SPECIAL_ENDINGS else random.choice(GOOD_NIGHT_ENDINGS)
        transition_count = random.randint(0, 2)
        transitions = random.sample(GOOD_NIGHT_TRANSITIONS, k=transition_count) if transition_count else []
        detail = random.choice(GOOD_NIGHT_DETAILS) if random.random() < 0.70 else ""
        parts = [body]
        parts.extend(transitions)
        if detail:
            parts.append(detail)
        parts.append(reminder)
        body = "\n".join(x for x in parts if x)
    return f"{opener}\n{lyrics}\n{body}\n{ending}"
