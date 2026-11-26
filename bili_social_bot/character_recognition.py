from __future__ import annotations

import json
import re
from typing import Any

from .constants import CHARACTER_REFERENCE_CHARS, CHARACTER_REFERENCE_FALLBACK, CHARACTER_REFERENCE_MIN
from .utils import resolve_path


def _load_fumo_reference(limit: int = 2600) -> str:
    """Load compact fumo/plush recognition reference for vision prompts."""
    try:
        p = resolve_path("data/fumo_character_lib.json")
        if not p.exists():
            return ""
        data = json.loads(p.read_text(encoding="utf-8"))
        chars = data.get("characters") or {}
        lines = ["【Fumo/毛绒形态识别库】".strip()]
        note = str(data.get("note") or "").strip()
        if note:
            lines.append(note[:220])
        rules = data.get("usage_rules") or []
        if rules:
            lines.append("规则：" + "；".join(str(x).strip() for x in rules if str(x).strip())[:520])
        for name, info in chars.items():
            if not isinstance(info, dict):
                continue
            feats = "、".join(str(x) for x in (info.get("fumo_features") or []) if str(x).strip())
            dont = "、".join(str(x) for x in (info.get("do_not_penalize") or []) if str(x).strip())
            conf = info.get("confusions") or {}
            conf_txt = "；".join(f"{k}: {v}" for k, v in conf.items()) if isinstance(conf, dict) else ""
            line = f"- {name}：fumo特征={feats[:180]}"
            if dont:
                line += f"；不要扣分={dont[:100]}"
            if conf_txt:
                line += f"；易混={conf_txt[:140]}"
            lines.append(line)
        return "\n".join(lines)[:limit]
    except Exception:
        return ""


PRIORITY_CHARACTER_REFERENCE = """【本地新增/易错角色识别重点规则（优先阅读）】
- 本地样本库若提示相似距离很近，尤其距离0-4，优先采用样本标注；人工确认样本不是猜测。
- fumo/毛绒/手办/Q版形态不要直接判非东方：优先看基础色板、发色、蝴蝶结/帽子、服装主色。琪露诺fumo重点：浅蓝/蓝色头发 + 蓝色大蝴蝶结 + 蓝裙/蓝白配色；手持吉他等二创道具不能否定琪露诺。
- 身上/头上的饰品不能当成本体特征：蝴蝶结、发饰、帽檐、衣服边饰、翅膀形挂件、包/袋子/道具的颜色，必须先归类为“饰品/道具”；不能把绿色蝴蝶结认成绿发、把羽翼状装饰认成蝙蝠翅、把衣服颜色认成发色。若颜色区域边界不清，必须写“疑似饰品/不确定”，不能据此硬认角色。
- 博丽灵梦重点：红白配色 + 巨大红色蝴蝶结/红白束发带 + 赛钱箱/お賽銭/博丽神社打钱梗，是强证据；棕发、红瞳、表情包/Q版化不能直接排除。
- 古明地觉 vs 秦心：粉发+红色圆形物时必须先区分“第三只眼”还是“面具”。古明地觉=粉色/粉紫齐脖短发 + 左胸第三只眼 + 红色血管/管线连接心形扣子/发卡 + 淡蓝上衣粉色褶边 + 粉白玫瑰裙；不要把第三只眼误当秦心面具。
- 灵乌路空/阿空 vs 东风谷早苗：看到黑发/深色发 + 绿色蝴蝶结或绿色发饰 + 地灵殿/阿空/灵乌路空标签时，优先检查阿空；绿色蝴蝶结不是绿发，不能仅凭绿色区域认早苗。早苗必须有长绿发、蓝白巫女服、青蛙/蛇发饰等强特征。
- 秦心重点：粉色及腰长发 + 头侧漂浮狐面/能面 + 蓝绿色上衣 + 橘红南瓜裙/几何纽扣/红绳。二创泳装或换装时，只要粉发+漂浮狐面清楚，也应优先秦心，不要被橙/八云蓝/椛等“狐/兽耳”方向带偏。
- 米斯蒂娅 vs 八云紫 vs 蕾米莉亚（高频认错组）：看到「粉/浅粉短发 + 暗红/酒红帽(带白绒球) + 白领暗红裙 + 羽翼状装饰」先检查米斯蒂娅·萝蕾拉（夜雀），不要往吸血鬼或紫身上凑。八云紫必须是金/及腰长发 + 白荷叶边洋帽/紫衣/大阳伞，粉短发红裙可直接排除紫；蕾米莉亚本体是灰蓝/蓝紫及肩发 + 白底红边裙，fumo才是红裙淡蓝发，且有蝙蝠翅膀（黑骨架/翼膜），看到羽翼状羽毛装饰或绒球帽时优先米斯蒂娅而不是蕾米；米斯蒂娅眼瞳是黑色，不是红瞳。
- 漫画/黑白/日常姿态截图要保守但不能漏掉：例如《东方醉蝶华》里博丽灵梦可能趴着或服装特征不完整，应结合作品/上下文和残留红白巫女特征判断。
- 多角色同框要逐个列出，不要只认最显眼角色；不确定时写疑似并说明缺少哪些强特征。
""".strip()


class LocalCharacterRecognizer:
    """Low-cost Touhou character recognition helper.

    It only reads local knowledge/correction files and prepares compact reference
    text for existing vision calls. It never calls any model or network API.
    """

    def __init__(self, config: dict[str, Any] | None = None, *, knowledge_text: str = "", logger=None):
        self.config = config or {}
        self.knowledge_text = knowledge_text or ""
        self.logger = logger or (lambda msg, level="INFO": None)
        self._character_lib_cache: dict[str, str] | None = None

    def cfg(self) -> dict[str, Any]:
        cfg = dict(self.config.get("character_recognition") or {})
        cfg.setdefault("enabled", True)
        cfg.setdefault("reference_char_budget", 2800)
        cfg.setdefault("candidate_limit", 12)
        cfg.setdefault("common_limit", 18)
        cfg.setdefault("per_character_chars", 170)
        cfg.setdefault("include_correction_memory", True)
        cfg.setdefault("add_local_hints_to_evidence", True)
        cfg.setdefault("scene_alias_enabled", True)
        cfg.setdefault("scene_alias_bonus", 2)
        cfg.setdefault("context_tag_limit", 4)
        return cfg

    @staticmethod
    def clean_desc(text: str, limit: int = 170) -> str:
        text = re.sub(r"[\*'`#|]+", "", str(text or ""))
        text = re.sub(r"\s+", " ", text).strip(" ：:-")
        if not text:
            return "资料较少，需结合标题、画面文字、服饰与评论区线索，不能硬猜。"
        return text[:limit]

    def load_library(self) -> dict[str, str]:
        if self._character_lib_cache is not None:
            return self._character_lib_cache
        lib: dict[str, str] = {}
        for path in (resolve_path("data/character_lib_partial.json"), resolve_path("东方角色识别特征库.md")):
            try:
                if not path.exists():
                    continue
                if path.suffix.lower() == ".json":
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        for k, v in data.items():
                            name = str(k or "").strip()
                            if name:
                                lib[name] = str(v or "").strip()
                else:
                    for line in path.read_text(encoding="utf-8").splitlines():
                        m = re.match(r"\s*-\s*\*\*(.+?)\*\*[:：](.*)", line)
                        if not m:
                            continue
                        name = m.group(1).strip()
                        desc = m.group(2).strip()
                        if name and (name not in lib or "无外貌特征" in lib.get(name, "")):
                            lib[name] = desc
            except Exception as exc:
                self.logger(f"读取角色识别特征库失败 {path}：{exc}", "WARN")
        lib.setdefault(
            "安娜贝尔·卡娜",
            "又名卡娜·安娜贝拉尔/Kana Anaberal，PC-98《东方梦时空》角色，关联原曲《夢消失 ～ Lost Dream》。资料有限；遇到 Lost Dream、夢消失、FORCE、Kana 等标题/字幕线索时，优先检查是否是她，不要仅凭金发或帽子误判为魔理沙。",
        )
        self._character_lib_cache = lib
        return lib

    def names_in_text(self, text: str, limit: int = 8) -> list[str]:
        """从标题/标签/字幕等文本里召回角色名（用于识别时的候选先验，避免盲信）。"""
        t = str(text or "")
        if not t:
            return []
        found: list[str] = []
        for name, alist in self.aliases().items():
            for a in alist:
                a = str(a or "").strip()
                if len(a) >= 2 and a in t:
                    if name not in found:
                        found.append(name)
                    break
        return found[: max(1, int(limit or 8))]

    def appearance_candidates(self, features_text: str, limit: int = 4, per_char_chars: int = 200) -> str:
        """两步识别第一步：按“可见外观特征”从特征库检索 2-4 个候选卡 + 易混对照。

        用于主视觉第一遍只看画面写出特征后，第二遍只带着少量候选卡去判定角色，
        避免把整本特征库塞进提示词导致注意力稀释（知识包 README 的建议用法）。
        """
        text = str(features_text or "")
        if not text.strip():
            return ""
        lib = self.load_library()
        # 只要“具体”特征词：颜色/发色组合与有辨识度的物件，避免“发/裙/翼”这类泛词
        specific = [
            "浅绿", "绿发", "绿瞳", "金发", "金瞳", "粉发", "粉紫", "紫发", "紫瞳", "蓝发", "蓝瞳",
            "银发", "白发", "红发", "红瞳", "黑发", "黑瞳", "橙发", "棕发", "灰发", "绿短发", "长发",
            "短发", "双马尾", "侧马尾", "齐脖", "卷发",
            "第三只眼", "第三眼", "闭合", "心形", "管线", "面具", "能面", "狐面", "结晶", "冰翼", "冰晶",
            "吸血鬼", "蝙蝠", "膜翼", "彩色结晶", "翅膀", "羽翼", "兔耳", "猫耳", "狐耳", "兽耳", "角",
            "巫女服", "女仆", "魔女帽", "白帽", "黑帽", "宽檐", "尖顶", "贝雷帽", "睡帽", "荷叶边",
            "蝴蝶结", "缎带", "向日葵", "蛙", "青蛙", "蛇", "南瓜", "伞", "刀", "剑", "扇", "书",
            "护士", "护士帽", "扫帚", "炮", "枪", "机械", "齿轮", "机器人", "玩偶", "fumo", "毛绒",
            "亡灵", "幽灵", "僵尸", "妖怪", "妖精", "巫女", "帽子", "围裙", "披风", "斗篷", "中装", "汉服",
        ]
        keys = [k.lower() for k in specific if k.lower() in text.lower()]
        if not keys:
            return ""
        # idf：命中词条越少越是关键特征
        entries = [(n, (n + " " + (d or "")[:400]).lower(), len(d or "")) for n, d in lib.items()]
        df = {k: sum(1 for _, low, _ in entries if k in low) for k in keys}
        w = {k: 1.0 / (1.0 + df[k]) for k in keys if df[k] > 0}
        if not w:
            return ""
        scored: list[tuple[float, int, str]] = []
        for name, low, dlen in entries:
            hit_keys = [k for k in w if k in low]
            if len(hit_keys) < 2:
                continue
            # 长度归一：避免描述最长的条目靠“什么都提到”取胜
            score = sum(w[k] for k in hit_keys) / (1.0 + len(low) / 600.0)
            scored.append((score, -len(hit_keys), name))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        top = [n for _, _, n in scored[:max(1, int(limit or 4))]]
        if not top:
            return ""
        parts = ["【按外观特征检索出的候选角色】"]
        for n in top:
            parts.append(f"- {n}：{self.clean_desc(lib.get(n, ''), per_char_chars)}")
        # 易混对照：其他角色描述里提到候选名的，一并带入，帮助区分
        extra: list[str] = []
        for name, desc in lib.items():
            if name in top:
                continue
            if any(t in desc for t in top):
                extra.append(f"- {name}：{self.clean_desc(desc, per_char_chars)}")
        if extra:
            parts.append("【易混对照】")
            parts.extend(extra[:4])
        # fumo/毛绒形态：附加玩偶观察卡（若本地有对照文件）
        if any(k in text for k in ("玩偶", "fumo", "毛绒", "娃娃")):
            try:
                fp = resolve_path("东方FUMO角色识别对照.md")
                if fp.exists():
                    rows = []
                    for line in fp.read_text(encoding="utf-8").splitlines():
                        if not line.strip().startswith("|"):
                            continue
                        if any(t in line for t in top):
                            rows.append(line.strip()[:220])
                    if rows:
                        parts.append("【FUMO玩偶观察卡（候选角色）】")
                        parts.extend(rows[:4])
            except Exception:
                pass
        return "\n".join(parts)[:3600]

    def aliases(self) -> dict[str, list[str]]:
        lib = self.load_library()
        aliases: dict[str, list[str]] = {name: [name, name.replace("·", ""), name.replace("・", "")] for name in lib}
        aliases.update({
            "博丽灵梦": ["博丽灵梦", "灵梦", "Reimu", "Hakurei"],
            "雾雨魔理沙": ["雾雨魔理沙", "魔理沙", "Marisa", "Kirisame", "黑白"],
            "古明地恋": ["古明地恋", "恋恋", "恋", "Koishi", "Komeiji Koishi"],
            "古明地觉": ["古明地觉", "觉", "Satori"],
            "十六夜咲夜": ["十六夜咲夜", "咲夜", "Sakuya"],
            "蕾米莉亚·斯卡雷特": ["蕾米莉亚", "蕾咪", "Remilia", "Scarlet"],
            "芙兰朵露·斯卡雷特": ["芙兰朵露", "芙兰", "Flandre", "妹样"],
            "魂魄妖梦": ["魂魄妖梦", "妖梦", "Youmu"],
            "西行寺幽幽子": ["西行寺幽幽子", "幽幽子", "Yuyuko"],
            "八云紫": ["八云紫", "紫", "Yukari"],
            "八云蓝": ["八云蓝", "蓝", "Ran"],
            "东风谷早苗": ["东风谷早苗", "早苗", "Sanae"],
            "琪露诺": ["琪露诺", "Cirno", "⑨"],
            "射命丸文": ["射命丸文", "文文", "Aya"],
            "铃仙·优昙华院·因幡": ["铃仙", "优昙华", "Reisen", "Udonge"],
            "火焰猫燐": ["火焰猫燐", "阿燐", "燐", "Orin"],
            "灵乌路空": ["灵乌路空", "阿空", "Okuu", "Utsuho"],
            "帕秋莉·诺蕾姬": ["帕秋莉", "Patchouli"],
            "多多良小伞": ["小伞", "多多良小伞", "Kogasa"],
            "比那名居天子": ["比那名居天子", "天子", "Tenshi", "Hinanawi"],
            "爱丽丝·玛格特洛依德": ["爱丽丝·玛格特洛依德", "爱丽丝玛格特洛依德", "爱丽丝·玛格特罗依德", "爱丽丝玛格特罗依德", "爱丽丝", "Alice", "Margatroid"],
            "稗田阿求": ["稗田阿求", "阿求", "稗田", "Akyuu", "Hieda"],
            "宫出口瑞灵": ["宫出口瑞灵", "瑞灵", "Miyadeguchi", "Mizuchi"],
            "星熊勇仪": ["星熊勇仪", "勇仪", "星熊", "Yuugi", "Hoshiguma"],
            "梅蒂欣·梅兰可莉": ["梅蒂欣·梅兰可莉", "梅蒂欣", "梅蒂森", "Medicine", "Melancholy"],
            "霍青娥": ["霍青娥", "青娥", "霍青娥娘娘", "Seiga", "Kaku"],
            "宫古芳香": ["宫古芳香", "芳香", "Yoshika", "Miyako"],
            "藤原妹红": ["藤原妹红", "妹红", "妹紅", "Mokou", "Fujiwara"],
            "上白泽慧音": ["上白泽慧音", "慧音", "Keine", "Kamishirasawa"],
            "克劳恩皮丝": ["克劳恩皮丝", "克劳恩", "皮丝", "克劳恩皮斯", "Clownpiece"],
            "红美铃": ["红美铃", "红美鈴", "美铃", "美鈴", "Meiling", "Hong Meiling"],
            "米斯蒂娅·萝蕾拉": ["米斯蒂娅·萝蕾拉", "米斯蒂娅", "米斯蒂亚", "夜雀", "Mystia", "Lorelei"],
            "灵乌路空": ["灵乌路空", "阿空", "空", "Okuu", "Utsuho"],
            "大妖精": ["大妖精", "大酱", "Daiyousei", "Daiyousei"],
            "小恶魔": ["小恶魔", "小恶魔酱", "Koakuma"],
            "饕餮尤魔": ["饕餮尤魔", "尤魔", "Toutetsu", "Yuuma", "Toutetsu Yuuma"],
            "安娜贝尔·卡娜": ["安娜贝尔·卡娜", "安娜贝尔卡娜", "卡娜·安娜贝拉尔", "卡娜", "Kana", "Kana Anaberal", "Lost Dream", "夢消失", "梦消失"],
            # 以下补的是「别名表曾经完全没有」的角色：视觉或本地打标说对了名字，也会因为
            # 归一不到规范名而计不上票（多帧一致票永远凑不齐），属于纯白捡的识别率。
            "露米娅": ["露米娅", "露米亚", "Rumia", "宵暗"],
            "键山雏": ["键山雏", "雏酱", "Hina", "Kagiyama Hina", "厄神"],
            "鬼人正邪": ["鬼人正邪", "正邪", "Seija", "Kijin Seija", "Seija Kijin"],
            "黑谷山女": ["黑谷山女", "山女", "Yamame", "Kurodani Yamame"],
            "莉莉白": ["莉莉白", "莉莉霍瓦特", "Lily White", "Lily"],
            "露娜切露德": ["露娜切露德", "露娜·切露德", "露娜", "Luna Child", "Luna"],
            "高丽野阿吽": ["高丽野阿吽", "阿吽", "Aunn", "Komano Aunn", "高丽野阿哞"],
            "骊驹早鬼": ["骊驹早鬼", "早鬼", "Saki", "Kurokoma Saki"],
            "驹草山如": ["驹草山如", "山如", "Sannyo", "Komakusa Sannyo"],
            "饭纲丸龙": ["饭纲丸龙", "丸龙", "Megumu", "Iizunamaru Megumu"],
            "朱鹭子": ["朱鹭子", "朱鹭", "Ruukoto", "Tokiko"],
            "魂魄妖忌": ["魂魄妖忌", "妖忌", "Youki", "Konpaku Youki"],
            "蕾蒂·怀特洛克": ["蕾蒂·怀特洛克", "蕾蒂", "蕾迪", "Letty", "Letty Whiterock"],
            "玛艾露贝莉·赫恩": ["玛艾露贝莉·赫恩", "玛艾露贝莉赫恩", "梅莉", "Merry", "Maribel Hearn"],
            "庭渡久侘歌": ["庭渡久侘歌", "久侘歌", "鸡妹", "Kutaka", "Niwatari Kutaka"],
            # 知识库《主要角色首登场一览表》里有、但别名表曾经缺失的角色。
            # 缺了它们，检索时"提问对象"的名字带不上权重，会检索到别人的行
            # （2026-09-10 300 题测试 B 区失分根因：以"魅魔"提问却检索出稗田阿求）。
            "魅魔": ["魅魔", "魅魔小姐", "Mima"],
            "露娜萨·普莉兹姆利巴": ["露娜萨·普莉兹姆利巴", "露娜萨", "普莉兹姆利巴", "Lunasa"],
            "铃瑚": ["铃瑚", "Ringo"],
            "Reisen（新月兔）": ["Reisen（新月兔）", "新月兔", "二号铃仙", "新Reisen"],
            "咪咪号": ["咪咪号", "咪咪", "Mimi-chan"],
            "露易兹": ["露易兹", "路易兹", "Luize"],
            "雪": ["雪", "Yuki"],
            "舞（旧作）": ["舞（旧作）", "Mai"],
        })
        # 常见错写/简称补进已有角色的别名表（不新增键，避免覆盖）
        for _alias, _canon in (
            ("雷米莉亚", "蕾米莉亚·斯卡雷特"), ("雷米莉娅", "蕾米莉亚·斯卡雷特"),
            ("蕾米莉亚·斯卡蕾特", "蕾米莉亚·斯卡雷特"),
            ("芙兰朵露·斯卡蕾特", "芙兰朵露·斯卡雷特"),
            ("古明地こいし", "古明地恋"), ("小五", "古明地觉"),
        ):
            if _canon in aliases and _alias not in aliases[_canon]:
                aliases[_canon].append(_alias)
        return aliases

    def find_candidates(self, context_text: str, *, limit: int = 12) -> list[str]:
        text = str(context_text or "")
        if not text:
            return []
        low = text.lower()
        scored: list[tuple[int, str]] = []
        for name, alias_list in self.aliases().items():
            score = 0
            for alias in alias_list:
                alias = str(alias or "").strip()
                if not alias or len(alias) <= 1:
                    continue
                if alias.lower() in low:
                    score += 3 if alias == name else 1
            if score:
                scored.append((score, name))

        # 只根据标题/简介/字幕里明确出现的角色名或别名筛候选。
        # 不能因为「东方/MEME/送女儿上学/车内」这类场景词给特定角色加概率；
        # 无角色名时交给视觉模型按完整特征库的发色、服装、道具、头饰等证据识别。
        scored.sort(key=lambda x: (-x[0], x[1]))
        names = [n for _, n in scored[:limit]]
        if "雾雨魔理沙" in names and "安娜贝尔·卡娜" not in names:
            names.append("安娜贝尔·卡娜")
        return names[:limit]

    def correction_memory_reference(self, context_text: str = "") -> str:
        """Return only context-relevant correction memories.

        Corrections are evidence/memory, not title-prior probability.  A generic
        title like "东方/MEME/送女儿上学" must not inject a specific old answer;
        only inject a correction when the current context explicitly mentions the
        same BV/source id or one of the corrected/wrong character names/aliases.
        """
        if not self.cfg().get("include_correction_memory", True):
            return ""
        ctx = str(context_text or "").lower()
        try:
            data = {}
            p = resolve_path("data/touhou_learning.json")
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
            entries = []
            for e in (data.get("entries") or []):
                title = str(e.get("title") or "")
                tags_list = [str(x) for x in (e.get("tags") or [])]
                tags = " ".join(tags_list)
                if "角色识别纠错" not in title and "角色识别纠错" not in tags:
                    continue
                hay = " ".join([title, str(e.get("source") or ""), tags, str(e.get("content") or "")])
                # Exact BV/source id is relevant; plain scene words are not.
                bv_ids = re.findall(r"BV[0-9A-Za-z]+", hay)
                if any(bv.lower() in ctx for bv in bv_ids):
                    entries.append(str(e.get("content") or "").strip())
                    continue
                # Character names/aliases explicitly mentioned in current text are relevant.
                mentioned = False
                for name, alias_list in self.aliases().items():
                    if name not in hay:
                        continue
                    for alias in alias_list:
                        alias_s = str(alias or "").strip()
                        if len(alias_s) > 1 and alias_s.lower() in ctx:
                            mentioned = True
                            break
                    if mentioned:
                        break
                if mentioned:
                    entries.append(str(e.get("content") or "").strip())
        except Exception:
            return ""
        if not entries:
            return ""
        return "【相关角色纠错记忆】\n" + "\n".join("- " + self.clean_desc(x, 220) for x in entries[:3])

    def reference_for_context(self, context_text: str = "") -> str:
        cfg = self.cfg()
        if not cfg.get("enabled", True):
            return ""
        budget = max(600, int(cfg.get("reference_char_budget") or 2800))
        per_char = max(80, int(cfg.get("per_character_chars") or 170))
        lib = self.load_library()
        candidates = self.find_candidates(context_text, limit=int(cfg.get("candidate_limit") or 12))
        common = [
            "博丽灵梦", "雾雨魔理沙", "古明地恋", "古明地觉", "十六夜咲夜", "蕾米莉亚·斯卡雷特",
            "芙兰朵露·斯卡雷特", "魂魄妖梦", "西行寺幽幽子", "八云紫", "八云蓝", "东风谷早苗",
            "琪露诺", "射命丸文", "铃仙·优昙华院·因幡", "帕秋莉·诺蕾姬", "火焰猫燐", "灵乌路空",
        ]
        if candidates:
            names = candidates
        else:
            # 无明确角色名线索时，不做“热门角色概率排序”，而是优先把本地《东方角色识别特征库》前部整块提供给视觉模型对照。
            # 这里只保留少量常见角色速查作为补充；真正识别应依赖 full_reference_fallback() 中的完整特征库。
            names = [n for n in common if n in lib][: int(cfg.get("common_limit") or 18)]
        parts = ["【东方角色识别参考】识别顺序固定：先把可见区域分成身体本体、饰品/头饰、服装、道具，再用本地《东方角色识别特征库》对照当前图片里可见的发色、帽子/头饰、服装、道具、翅膀/尾巴/第三只眼、面具、兽耳、画面文字，最后决定角色名；如果按特征库仍不稳，再参考本地相似样本做二次校正。标题、简介、标签只作辅助线索，不能压过画面证据，也不能按题材给某些角色加概率。绿色蝴蝶结/发饰不是绿发，衣服/袋子/挂件颜色不是发色或身体特征；颜色区域边界不清必须写疑似饰品/不确定，禁止硬认。"]
        parts.append(
            "【易混角色强制对照】"
            "金发+帽子不等于魔理沙：必须看到黑白魔女帽、围裙/魔法扫帚等组合才可确认；"
            "若标题/文字有 Kana/Lost Dream/夢消失/梦消失，要优先检查安娜贝尔·卡娜；"
            "若B站标签/画面文字明确写了角色名，它是重要文本线索，但仍必须和画面可见特征互相核对；"
            "灵乌路空/阿空 vs 东风谷早苗：黑发/深色发+绿色蝴蝶结或绿色发饰+地灵殿/阿空/灵乌路空标签时优先检查阿空；绿色蝴蝶结不是绿发，不能仅凭绿色区域认早苗；早苗必须有长绿发、蓝白巫女服、青蛙/蛇发饰等强特征；"
            "蓝白女仆装+银发/蓝银发+怀表/小刀才偏咲夜；红白巫女装+大蝴蝶结/御币才偏灵梦；"
            "渐变金紫长发+黑白服装/哥特感+绿色念珠/经卷感优先检查圣白莲，不要因兽耳/耳机改装误判菅牧典或橙；"
            "粉发+漂浮面具+蓝绿色上衣/橘红裙优先检查秦心；金发+唐式幞巾/狩衣/星座纹样/背后门或椅子Boss气质优先检查摩多罗隐岐奈；"
            "黄褐短发+狐耳+白衣/绿色绑结且无圣白莲渐变长发时才偏菅牧典；小型深色天狗头襟+黑/棕黑发+记者/飒爽动作优先检查射命丸文；紫色小头襟+紫蝴蝶结双马尾才偏姬海棠果；"
            "棕短发+猫耳+绿色帽子+红裙优先检查橙；橙棕发+清晰鬼角/酒葫芦/锁链/大红蝴蝶结才偏伊吹萃香，只有深色小帽和V手势时不要硬判萃香；"
            "看到初音未来/VOCALOID/其他作品角色时必须标为非东方角色，不要硬套东方角色名。"
        )
        corr = self.correction_memory_reference(context_text)
        if corr:
            parts.append(corr)
        parts.append("【标题/上下文明确命中的候选角色】" if candidates else "【常见角色速查（真正识别仍以完整特征库和画面证据为准，不是概率排序）】")
        for name in names:
            desc = lib.get(name) or lib.get(name.replace("安娜贝尔·卡娜", "卡娜·安娜贝拉尔")) or ""
            parts.append(f"- {name}：{self.clean_desc(desc, per_char)}")
        return "\n".join(parts)[:budget]

    def local_hint(self, context_text: str = "") -> str:
        names = self.find_candidates(context_text, limit=8)
        if not names:
            return ""
        return "标题/简介/字幕明确提到：" + "、".join(names) + "。这只是文本线索，画面识别仍需对照可见特征；不能按题材或场景给角色加概率。"

    def full_reference_fallback(self) -> str:
        fumo_ref = _load_fumo_reference()
        prefix = (PRIORITY_CHARACTER_REFERENCE.strip() + ("\n\n" + fumo_ref if fumo_ref else "")).strip()
        lib = resolve_path("东方角色识别特征库.md")
        try:
            if lib.exists():
                text = lib.read_text(encoding="utf-8").strip()
                if text:
                    return (prefix + "\n\n" + text)[:CHARACTER_REFERENCE_CHARS]
        except Exception:
            pass
        kt = str(self.knowledge_text or "")
        if not kt:
            return prefix
        marker = "东方Project角色视觉识别特征库"
        idx = kt.find(marker)
        body = kt[idx: idx + CHARACTER_REFERENCE_FALLBACK] if idx >= 0 else kt[:CHARACTER_REFERENCE_MIN]
        return (prefix + "\n\n" + body).strip()
