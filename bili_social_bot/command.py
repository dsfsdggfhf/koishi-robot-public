from __future__ import annotations

import re
from typing import Any

from .utils import extract_bvid


# 管理员指令前缀。普通用户不会因为这些前缀获得权限；权限判断在 chat_bot 中完成。
COMMAND_PREFIXES = ["指令", "命令", "执行", "@命令", "#", "！", "!"]


_ADMIN_ONLY_ACTIONS = {
    "admin_add", "admin_remove", "admin_list", "admin_whoami",
    "daily_status", "set_feature", "set_limit", "recent_history", "remember_note", "send_dm_text",
}


def is_command(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return False
    for p in COMMAND_PREFIXES:
        if t.startswith(p):
            return True
    # 也支持「机器人/恋恋/小石/石头，帮我xxx」这类明确的祈使句
    if re.match(r"^(机器人|恋恋|Koishi|koishi|小石|石头)[，,：:]\s*", t):
        return True
    # 管理员系统相关口令，即使没加前缀也按指令处理（权限仍由上层判断）。
    action, _ = parse_command(t)
    if action in _ADMIN_ONLY_ACTIONS:
        return True
    # 动词开头的指令（帮助/状态/搜索/评论/点赞/收藏/发/看/学习）
    return action not in {"unknown", ""}


def strip_command(text: str) -> str:
    t = str(text or "").strip()
    for p in COMMAND_PREFIXES:
        if t.startswith(p):
            return t[len(p):].strip()
    t = re.sub(r"^(机器人|恋恋|Koishi|koishi|小石|石头)[，,：:]\s*", "", t)
    # 去掉「帮我」「请帮我」「麻烦」等礼貌前缀
    t = re.sub(r"^(?:请|麻烦)?(?:帮我|替我|帮我一下)\s*", "", t)
    return t.strip()


def _first_uid(text: str) -> str:
    uid_candidates = [
        g for g in re.findall(r"(?<!\d)(\d{5,20})(?!\d)", text)
        if not re.fullmatch(r"(?:19|20)\d{6}", g)  # 排除 8 位日期格式
    ]
    return uid_candidates[0] if uid_candidates else ""


def _feature_name(raw: str) -> str:
    s = str(raw or "").strip().lower().replace(" ", "")
    aliases = {
        "私信": "私信", "私信回复": "私信", "dm": "私信", "private": "私信",
        "互动": "互动", "互动聊天": "互动",
        "评论回复": "评论回复", "回复评论": "评论回复", "commentreply": "评论回复",
        "@回复": "@回复", "at回复": "@回复", "at": "@回复",
        "动态回复": "动态回复", "动态评论": "动态回复", "dynamic": "动态回复",
        "早安": "早安", "早安私信": "早安",
        "晚安": "晚安", "晚安私信": "晚安",
        "新粉": "新粉", "新粉感谢": "新粉", "粉丝感谢": "新粉",
        "心跳": "心跳", "心跳报告": "心跳",
        "学习": "学习任务", "学习任务": "学习任务",
        "学习日报": "学习日报", "日报": "学习日报",
        "wiki": "wiki学习", "wiki学习": "wiki学习", "东方wiki": "wiki学习",
        "小站": "小站早晚安", "小站早晚安": "小站早晚安", "小站问候": "小站早晚安",
    }
    return aliases.get(s, raw.strip())


def parse_command(text: str) -> tuple[str, dict[str, Any]]:
    """Parse a natural-ish command into (action, params).

    Returns ("unknown", {}) when it can't be understood.
    """
    t = strip_command(text)
    if not t:
        return "help", {}

    low = t.lower().strip()

    # 帮助
    if low in {"帮助", "help", "菜单", "指令列表", "能干什么", "怎么用", "有什么指令", "你能做什么", "管理员帮助", "管理帮助"}:
        return "help", {}

    # 权限/管理员系统
    if low in {"我是谁", "我的权限", "权限", "身份", "whoami"}:
        return "admin_whoami", {}
    if low in {"管理员", "管理员列表", "管理列表", "admins", "admin list", "管理员名单"}:
        return "admin_list", {}
    m = re.match(r"^(?:添加|新增|设为|任命|授权)\s*(?:管理员|管理)\s*(\d{5,20})(?:\s+(.+))?$", t)
    if not m:
        m = re.match(r"^(?:把\s*)?(\d{5,20})\s*(?:设为|加为|加入|添加为)\s*(?:管理员|管理)(?:\s+(.+))?$", t)
    if m:
        return "admin_add", {"uid": m.group(1), "name": (m.group(2) or "").strip()}
    m = re.match(r"^(?:删除|移除|撤销|取消|解除)\s*(?:管理员|管理)?\s*(\d{5,20})", t)
    if not m:
        m = re.match(r"^(?:把\s*)?(\d{5,20})\s*(?:移出|踢出|解除|取消)\s*(?:管理员|管理)", t)
    if m:
        return "admin_remove", {"uid": m.group(1)}

    # 日常开关：开启/关闭 私信、早安、晚安、学习等
    m = re.match(r"^(开启|打开|启用|恢复|关闭|关掉|停用|暂停|停止)\s*(.+)$", t)
    if m:
        verb, feature = m.group(1), _feature_name(m.group(2))
        if feature:
            enabled = verb in {"开启", "打开", "启用", "恢复"}
            return "set_feature", {"feature": feature, "enabled": enabled}

    if low in {"日常状态", "功能状态", "开关状态", "设置状态", "daily status"}:
        return "daily_status", {}

    # 限额设置
    m = re.match(r"^(?:设置|调整|改)?\s*(评论上限|分享上限|私信每轮|评论回复每轮|@回复每轮|at回复每轮|动态回复每轮)\s*(?:为|到|=|：|:)?\s*(\d{1,4})$", t, re.I)
    if m:
        key = m.group(1).replace("at", "@").replace("AT", "@")
        return "set_limit", {"limit": key, "value": int(m.group(2))}

    # 历史/最近动作
    m = re.match(r"^(?:历史|最近|最近记录|做了什么|最近做了什么)\s*(\d{1,3})?", t)
    if m and (m.group(0).strip() == t or low.startswith(("历史", "最近", "做了什么"))):
        n = int(m.group(1) or 8)
        return "recent_history", {"limit": n}

    # 主动发一条私信：私信 <UID> <内容> / 发消息给 <UID> <内容>
    m = re.match(r"^(?:私信|发消息给|发送私信给)\s*(\d{5,20})\s+(.+)$", t, re.S)
    if m:
        return "send_dm_text", {"uid": m.group(1), "text": m.group(2).strip()}

    # 记录一条长期记忆/知识
    m = re.match(r"^(?:记住|记录一下|备忘)\s*(.+)$", t, re.S)
    if m:
        return "remember_note", {"text": m.group(1).strip()}

    # 状态
    if low in {"状态", "检查", "health", "运行状态", "检查状态", "一切正常吗"}:
        return "status", {}

    # 搜索并评论：搜索/搜一下/找 <关键词>
    m = re.match(r"^(?:搜索|搜一下|搜|找|评论搜索|看看关于)\s*(.+)$", t)
    if m:
        return "search_comment", {"keyword": m.group(1).strip()}

    # 评论指定视频：评论/评论一下 <BV号 或 链接>
    m = re.match(r"^(?:评论|评论一下|评论下|帮我评论)\s*(.+)$", t)
    if m:
        bvid = extract_bvid(m.group(1)) or m.group(1).strip()
        if re.match(r"^BV[0-9A-Za-z]+$", bvid):
            return "comment", {"bvid": bvid}
        return "unknown", {}

    # 点赞：点赞/点个赞 <BV号>
    m = re.match(r"^(?:点赞|点个赞|赞一下)\s*(.+)$", t)
    if m:
        bvid = extract_bvid(m.group(1)) or m.group(1).strip()
        if re.match(r"^BV[0-9A-Za-z]+$", bvid):
            return "like", {"bvid": bvid}
        return "unknown", {}

    # 收藏：收藏 <BV号>
    m = re.match(r"^收藏\s*(.+)$", t)
    if m:
        bvid = extract_bvid(m.group(1)) or m.group(1).strip()
        if re.match(r"^BV[0-9A-Za-z]+$", bvid):
            return "favorite", {"bvid": bvid}
        return "unknown", {}

    # 发视频给某人：发/分享/推荐 <BV号> [给 <UID>]
    m = re.match(r"^(?:发|发视频|分享|推荐)\s*(BV[0-9A-Za-z]+)\s*(?:给|到|送给)?\s*(\d+)?", t, re.I)
    if m:
        params = {"bvid": m.group(1).upper()}
        if m.group(2):
            params["uid"] = m.group(2)
        return "share", params

    # 深度分析（用 reasoner，较贵）：深度 <BV号>
    if re.match(r"^(?:深度|深度评论|深度分析)\s*(.+)$", t):
        return "deep_comment", {"bvid": extract_bvid(t) or t}

    # 开启视觉分析：开视觉 <BV号> / 看视频 <BV号>
    if re.match(r"^(?:开视觉|开图像|看视频|视觉)\s*(.+)$", t):
        return "vision_comment", {"bvid": extract_bvid(t) or t}

    # 消费统计
    if low in {"消费", "费用", "花费", "账单", "用了多少钱", "消耗", "cost", "消费统计"}:
        return "cost", {}

    # 学习库查看
    if re.match(r"^学习(?:库|记录|总结)?\s*$", t) or low in {"学习库", "看学习", "学习总结"}:
        return "learn_view", {}

    # 拉黑/解除拉黑某 UID（管理员管理私信黑名单）
    uid = _first_uid(t)
    if uid:
        if re.search(r"(?:拉黑|屏蔽|黑名单|别再?给|别回|别理|不再回复|停止回复|不回复|停回|停掉|block)", low):
            return "block", {"uid": uid}
        if re.search(r"(?:解除|恢复回复|取消拉黑|取消屏蔽|unblock|解除黑名单|继续回复)", low):
            return "unblock", {"uid": uid}

    return "unknown", {}


def unknown_tip() -> str:
    return (
        "这条话 Koishi 还没法稳稳执行呢。\n"
        "可以试试这些更明确的说法：\n"
        "· 指令 帮助\n"
        "· 指令 状态 / 指令 日常状态 / 指令 消费\n"
        "· 指令 评论 <BV号> / 指令 深度 <BV号> / 指令 开视觉 <BV号>\n"
        "· 指令 点赞 <BV号> / 指令 收藏 <BV号>\n"
        "· 指令 发 <BV号> 给 <UID>\n"
        "· 指令 拉黑 <UID> / 指令 解除 <UID>"
    )


def help_text(level: str = "admin") -> str:
    super_part = ""
    if level == "super":
        super_part = (
            "\n\n【最高管理员】\n"
            "· 指令 添加管理员 <UID> [名字] —— 授权别人指挥恋恋\n"
            "· 指令 删除管理员 <UID> —— 撤销管理员\n"
            "· 指令 管理员列表 —— 查看当前授权名单"
        )
    return (
        "Koishi 管理员指令：\n"
        "【日常查看】\n"
        "· 指令 状态 —— 检查登录、评论、私信、AI状态\n"
        "· 指令 日常状态 —— 查看私信/评论/@/动态/早晚安等开关\n"
        "· 指令 消费 —— 查看本进程模型消耗\n"
        "· 指令 学习库 —— 查看最近学到的东方知识\n"
        "· 指令 历史 8 —— 查看最近动作摘要\n"
        "· 指令 权限 —— 查看自己是不是管理员\n\n"
        "【视频动作】\n"
        "· 指令 搜索 <关键词> —— 找最新相关视频并尝试评论\n"
        "· 指令 评论 <BV号> —— 直接评论+点赞+收藏\n"
        "· 指令 深度 <BV号> —— 更认真也更贵的评论\n"
        "· 指令 开视觉 <BV号> —— 看画面后评论\n"
        "· 指令 点赞 <BV号> / 指令 收藏 <BV号>\n"
        "· 指令 发 <BV号> 给 <UID> —— 私信分享视频\n\n"
        "【日常开关/限额】\n"
        "· 指令 开启私信 / 关闭私信\n"
        "· 指令 开启评论回复 / 关闭评论回复\n"
        "· 指令 开启@回复 / 关闭@回复\n"
        "· 指令 开启动态回复 / 关闭动态回复\n"
        "· 指令 开启早安 / 关闭晚安 / 开启新粉 / 关闭心跳\n"
        "· 指令 设置评论上限 80 / 指令 设置私信每轮 20\n\n"
        "【管理杂项】\n"
        "· 指令 拉黑 <UID> / 指令 解除 <UID>\n"
        "· 指令 记住 <内容> —— 写入长期学习库\n"
        "· 指令 私信 <UID> <内容> —— 主动发一条安全校验后的私信\n\n"
        "可在B站私信里发，也可在评论/@/动态评论里发；只有管理员会被执行，普通用户只会按正常聊天处理。"
        f"{super_part}"
    )
