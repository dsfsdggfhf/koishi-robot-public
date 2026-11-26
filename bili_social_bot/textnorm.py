# -*- coding: utf-8 -*-
"""繁体→简体归一：用于评论/私信正文与识别结果，避免出现「霧雨魔理沙」这类繁体角色名。"""
from __future__ import annotations

TRAD_TO_SIMP = {
    '紅': '红', '霧': '雾', '麗': '丽', '靈': '灵', '夢': '梦', '華': '华', '鈴': '铃', '戀': '恋',
    '覺': '觉', '櫻': '樱', '蘭': '兰', '響': '响', '靜': '静', '貓': '猫', '龍': '龙', '豐': '丰',
    '聰': '聪', '劍': '剑', '優': '优', '曇': '昙', '園': '园', '統': '统', '條': '条', '將': '将',
    '織': '织', '絲': '丝', '綠': '绿', '藍': '蓝', '黃': '黄', '輕': '轻', '輪': '轮', '闇': '暗',
    '詰': '诘', '與': '与', '這': '这', '個': '个', '們': '们', '來': '来', '時': '时', '說': '说',
    '話': '话', '見': '见', '長': '长', '開': '开', '關': '关', '麼': '么', '樣': '样', '裡': '里',
    '對': '对', '還': '还', '沒': '没', '會': '会', '後': '后', '現': '现', '實': '实', '學': '学',
    '體': '体', '聲': '声', '樂': '乐', '風': '风', '雨': '雨', '雲': '云', '藍': '蓝', '純': '纯',
    # 日文新字体/异体字（角色名常见）
    '霊': '灵', '爲': '为', '繪': '绘', '絵': '绘', '澤': '泽', '圓': '圆', '緣': '缘', '樓': '楼',
    '藝': '艺', '義': '义', '藥': '药', '醫': '医', '雙': '双', '羅': '罗', '姫': '姬', '圍': '围',
    '團': '团', '產': '产', '畫': '画', '聽': '听', '讀': '读', '變': '变', '邊': '边', '進': '进',
    '點': '点', '燈': '灯', '鳳': '凤', '鳴': '鸣', '蟲': '虫', '馬': '马', '鳥': '鸟', '魚': '鱼',
    '麥': '麦', '齊': '齐', '戰': '战', '術': '术', '舊': '旧', '歸': '归', '從': '从', '寶': '宝',
    '輝': '辉', '燈': '灯', '顏': '颜', '惡': '恶', '強': '强', '彈': '弹', '符': '符',
}
_TABLE = str.maketrans(TRAD_TO_SIMP)


def normalize_traditional(text: str) -> str:
    """把常见繁体字归一成简体（只做字形替换，不改语序）。"""
    try:
        return str(text or "").translate(_TABLE)
    except Exception:
        return str(text or "")


# 罗马字/英文角色名 → 中文名（只收“足够独特”的写法，避免误伤普通英文词）
ROMAJI_TO_CN = {
    'reimu hakurei': '博丽灵梦', 'hakurei reimu': '博丽灵梦', 'reimu': '博丽灵梦',
    'marisa kirisame': '雾雨魔理沙', 'kirisame marisa': '雾雨魔理沙', 'marisa': '雾雨魔理沙',
    'sakuya izayoi': '十六夜咲夜', 'izayoi sakuya': '十六夜咲夜', 'sakuya': '十六夜咲夜',
    'remilia scarlet': '蕾米莉亚·斯卡蕾特', 'remilia': '蕾米莉亚·斯卡蕾特',
    'flandre scarlet': '芙兰朵露·斯卡蕾特', 'flandre': '芙兰朵露·斯卡蕾特',
    'patchouli knowledge': '帕秋莉·诺蕾姬', 'patchouli': '帕秋莉·诺蕾姬',
    'hong meiling': '红美铃', 'meiling': '红美铃',
    'cirno': '琪露诺', 'rumia': '露米娅',
    'youmu konpaku': '魂魄妖梦', 'konpaku youmu': '魂魄妖梦', 'youmu': '魂魄妖梦',
    'yuyuko saigyouji': '西行寺幽幽子', 'saigyouji yuyuko': '西行寺幽幽子', 'yuyuko': '西行寺幽幽子',
    'yukari yakumo': '八云紫', 'yakumo yukari': '八云紫', 'yukari': '八云紫',
    'ran yakumo': '八云蓝', 'yakumo ran': '八云蓝',
    'sanae kochiya': '东风谷早苗', 'kochiya sanae': '东风谷早苗', 'sanae': '东风谷早苗',
    'alice margatroid': '爱丽丝·玛格特洛依德',
    'kogasa tatara': '多多良小伞', 'tatara kogasa': '多多良小伞', 'kogasa': '多多良小伞',
    'rin kaenbyou': '火焰猫燐', 'kaenbyou rin': '火焰猫燐', 'orin': '火焰猫燐',
    'utsuho reiuji': '灵乌路空', 'reiuji utsuho': '灵乌路空', 'utsuho': '灵乌路空', 'okuu': '灵乌路空',
    'mokou fujiwara': '藤原妹红', 'fujiwara mokou': '藤原妹红', 'mokou': '藤原妹红',
    'kaguya houraisan': '蓬莱山辉夜', 'houraisan kaguya': '蓬莱山辉夜', 'kaguya': '蓬莱山辉夜',
    'tenshi hinanawi': '比那名居天子', 'hinanawi tenshi': '比那名居天子', 'tenshi': '比那名居天子',
    'suika ibuki': '伊吹萃香', 'ibuki suika': '伊吹萃香', 'suika': '伊吹萃香',
    'suwako moriya': '洩矢诹访子', 'moriya suwako': '洩矢诹访子', 'suwako': '洩矢诹访子',
    'kanako yasaka': '八坂神奈子', 'yasaka kanako': '八坂神奈子', 'kanako': '八坂神奈子',
    'nitori kawashiro': '河城荷取', 'kawashiro nitori': '河城荷取', 'nitori': '河城荷取',
    'aya shameimaru': '射命丸文', 'shameimaru aya': '射命丸文',
    'mystia lorelei': '米斯蒂娅·萝蕾拉', 'mystia': '米斯蒂娅·萝蕾拉',
    'keine kamishirasawa': '上白泽慧音', 'kamishirasawa keine': '上白泽慧音', 'keine': '上白泽慧音',
    'reisen udongein inaba': '铃仙·优昙华院·因幡', 'reisen': '铃仙·优昙华院·因幡',
    'tewi inaba': '因幡帝', 'inaba tewi': '因幡帝',
    'koishi komeiji': '古明地恋', 'komeiji koishi': '古明地恋', 'koishi': '古明地恋',
    'satori komeiji': '古明地觉', 'komeiji satori': '古明地觉', 'satori': '古明地觉',
    'nazrin': '娜兹玲', 'shou toramaru': '寅丸星', 'byakuren hijiri': '圣白莲', 'byakuren': '圣白莲',
    'nue houjuu': '封兽鵺', 'houjuu nue': '封兽鵺', 'mamizou futatsuiwa': '二岩猯藏', 'mamizou': '二岩猯藏',
    'seiga kaku': '霍青娥', 'yoshika miyako': '宫古芳香', 'tojiko soga': '苏我屠自古',
    'futo mononobe': '物部布都', 'miko toyosatomimi': '丰聪耳神子', 'kokoro hata': '秦心', 'kokoro': '秦心',
    'shinmyoumaru sukuna': '少名针妙丸', 'seija kijin': '鬼人正邪', 'seija': '鬼人正邪',
    'kasen ibaraki': '茨木华扇', 'kasen': '茨木华扇',
    'sumireko usami': '宇佐见堇子', 'renko usami': '宇佐见莲子',
    'maribel hearn': '玛艾露贝莉·赫恩', 'doremy sweet': '哆来咪·苏伊特', 'doremy': '哆来咪·苏伊特',
    'sagume kishin': '稀神探女', 'clownpiece': '克劳恩皮丝', 'junko': '纯狐',
    'hecatia lapislazuli': '赫卡提亚·拉碧斯拉祖利', 'eirin yagokoro': '八意永琳', 'eirin': '八意永琳',
    'yuuka kazami': '风见幽香', 'kazami yuuka': '风见幽香', 'yuuka': '风见幽香',
    'komachi onozuka': '小野塚小町', 'eiki shiki': '四季映姬·夜摩仙那度',
    'letty whiterock': '蕾蒂·霍瓦特洛克', 'daiyousei': '大妖精', 'koakuma': '小恶魔',
    'wriggle nightbug': '莉格露·奈特巴格', 'wriggle': '莉格露·奈特巴格',
    'hina kagiyama': '键山雏', 'momiji inubashiri': '犬走椛', 'hatate himekaidou': '姬海棠果',
    'medicine melancholy': '梅蒂欣·梅兰可莉', 'shizuha aki': '秋静叶', 'minoriko aki': '秋穰子',
    'sakuya': '十六夜咲夜', 'luna child': '露娜切露德', 'star sapphire': '斯塔萨菲雅',
    'sunny milk': '桑尼米尔克', 'lily white': '莉莉霍瓦特',
    # ===== WD14/danbooru 风格标签补全（surname_givenname）=====
    'teireida mai': '丁礼田舞', 'mai teireida': '丁礼田舞', 'satono nishida': '尔子田里乃',
    'nishida satono': '尔子田里乃', 'toyosatomimi no miko': '丰聪耳神子', 'mononobe no futo': '物部布都',
    'soga no tojiko': '苏我屠自古', 'tsukumo yatsuhashi': '九十九八桥', 'tsukumo benben': '九十九弁弁',
    'futatsuiwa mamizou': '二岩猯藏', 'kumoi ichirin': '云居一轮', 'imaizumi kagerou': '今泉影狼',
    'yorigami shion': '依神紫苑', 'yorigami jo\'on': '依神女苑', 'yagokoro eirin': '八意永琳',
    'okazaki yumemi': '冈崎梦美', 'kitashirakawa chiyuri': '北白河千百合', 'yumemi okazaki': '冈崎梦美',
    'chiyuri kitashirakawa': '北白河千百合', 'saigyouji yuyuko': '西行寺幽幽子',
    'konpaku youmu': '魂魄妖梦', 'hakurei reimu': '博丽灵梦', 'kirisame marisa': '雾雨魔理沙',
    'izayoi sakuya': '十六夜咲夜', 'scarlet remilia': '蕾米莉亚·斯卡蕾特', 'scarlet flandre': '芙兰朵露·斯卡蕾特',
    'knowledge patchouli': '帕秋莉·诺蕾姬', 'meiling hong': '红美铃', 'komeiji satori': '古明地觉',
    'komeiji koishi': '古明地恋', 'kaenbyou rin': '火焰猫燐', 'reiuji utsuho': '灵乌路空',
    'yakumo yukari': '八云紫', 'yakumo ran': '八云蓝', 'kochiya sanae': '东风谷早苗',
    'moriya suwako': '洩矢诹访子', 'yasaka kanako': '八坂神奈子', 'kawashiro nitori': '河城荷取',
    'shameimaru aya': '射命丸文', 'inubashiri momiji': '犬走椛', 'himekaidou hatate': '姬海棠果',
    'houraisan kaguya': '蓬莱山辉夜', 'fujiwara no mokou': '藤原妹红', 'fujiwara mokou': '藤原妹红',
    'inaba tewi': '因幡帝', 'reisen udongein inaba': '铃仙·优昙华院·因幡',
    'hinanawi tenshi': '比那名居天子', 'nagae iku': '永江衣玖', 'ibuki suika': '伊吹萃香',
    'hijiri byakuren': '圣白莲', 'toramaru shou': '寅丸星', 'nazrin': '娜兹玲',
    'houjuu nue': '封兽鵺', 'murasa minamitsu': '村纱水蜜', 'kaku seiga': '霍青娥',
    'miyako yoshika': '宫古芳香', 'hata no kokoro': '秦心', 'sukuna shinmyoumaru': '少名针妙丸',
    'kijin seija': '鬼人正邪', 'ibaraki kasen': '茨木华扇', 'usami sumireko': '宇佐见堇子',
    'usami renko': '宇佐见莲子', 'hearn maribel': '玛艾露贝莉·赫恩', 'maribel hearn': '玛艾露贝莉·赫恩',
    'sweet doremy': '哆来咪·苏伊特', 'kishin sagume': '稀神探女', 'lapislazuli hecatia': '赫卡提亚·拉碧斯拉祖利',
    'kazami yuuka': '风见幽香', 'onozuka komachi': '小野塚小町', 'shiki eiki': '四季映姬·夜摩仙那度',
    'whiterock letty': '蕾蒂·霍瓦特洛克', 'nightbug wriggle': '莉格露·奈特巴格',
    'lorelei mystia': '米斯蒂娅·萝蕾拉', 'kamishirasawa keine': '上白泽慧音',
    'melancholy medicine': '梅蒂欣·梅兰可莉', 'kagiyama hina': '键山雏', 'aki shizuha': '秋静叶',
    'aki minoriko': '秋穰子', 'houraisan kaguya': '蓬莱山辉夜', 'margatroid alice': '爱丽丝·玛格特洛依德',
    'luna child': '露娜切露德', 'star sapphire': '斯塔萨菲雅', 'clownpiece': '克劳恩皮丝',
    'lapislazuli hecatia': '赫卡提亚·拉碧斯拉祖利', 'komeiji satori': '古明地觉',
    'tatara kogasa': '多多良小伞', 'kotiya sanae': '东风谷早苗', 'morichika rinnosuke': '森近霖之助',
    'motoori kosuzu': '本居小铃', 'hieda no akyuu': '稗田阿求', 'medicine melancholy': '梅蒂欣·梅兰可莉',
}
_ROMAJI_RE = None


def normalize_romaji(text: str) -> str:
    """把英文/罗马字角色名替换成中文名（长名优先，避免部分覆盖）。"""
    global _ROMAJI_RE
    s = str(text or "")
    if not s:
        return s
    if _ROMAJI_RE is None:
        import re as _re
        keys = sorted(ROMAJI_TO_CN.keys(), key=len, reverse=True)
        _ROMAJI_RE = _re.compile(r'(?<![A-Za-z])(' + '|'.join(_re.escape(k) for k in keys) + r')(?![A-Za-z])',
                                 _re.IGNORECASE)
    def _sub(m):
        return ROMAJI_TO_CN.get(m.group(0).lower(), m.group(0))
    try:
        return _ROMAJI_RE.sub(_sub, s)
    except Exception:
        return s
