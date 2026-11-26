# -*- coding: utf-8 -*-
"""生日贺卡渲染：立绘铺满 + 头像贴蛋糕 + 日期 + 文案 + 落款。

只用本机能力（Pillow），零成本、可批量。

坐标来源（2026-09-13 实测定位）：
  立绘 `assets/birthday/koishi_birthday.jpg`（1054×1490）里那块**红白方块蛋糕**的
  中心在 (421, 787)、半径约 112。定位方法：先给图叠带标签的网格（5列×7行），
  让视觉模型报格子号（两次都答 R4C2,R4C3）；再在原图上画圈复验，
  模型回答 ON_CAKE yes / dx=0 dy=0。绝对坐标直接问模型是不可靠的（同一张图两次
  给出相差 400 像素的 y），网格法才稳。

主人要求（2026-09-13）：
  ① 文案用「今天是你的生日」　② 多款式（5 套配色，随机抽）
  ③ 贺卡在小故事之后最后发（顺序由 bot.py 控制）
  ④ 头像贴到蛋糕上　⑤ 右上角写日期
"""
from __future__ import annotations

import math
import random
from datetime import date
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1440
FONT_BOLD = next((p for p in (r"C:\Windows\Fonts\msyhbd.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc") if Path(p).exists()), r"C:\Windows\Fonts\msyhbd.ttc")
FONT_REG = next((p for p in (r"C:\Windows\Fonts\msyh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc") if Path(p).exists()), r"C:\Windows\Fonts\msyh.ttc")

ART_DEFAULT = "assets/birthday/koishi_birthday.jpg"
CAKE_CENTER = (421, 787)
CAKE_RADIUS = 112
# 第三只眼装饰（**目前不画**）：2026-09-14 主人看过成品后决定去掉——"眼睛那个还是
# 去掉吧，其他的可以"。函数 `_third_eye()` 与参数都保留着，想恢复就在 render_card 里
# 把 `img = _third_eye(img, pal["accent"], *EYE_ORNAMENT)` 加回来。
#
# 事后查清了它为什么一直显得"多出来一个东西"：**立绘原图自带了恋恋的第三只眼**——
# `assets/birthday/koishi_birthday.jpg` 里她右肩就有一个蓝色球体、闭着眼、接黑色管线，
# 左侧还有一条粉色管线弯成大爱心。我另画一只眼睛 = 同一张图上出现两个第三只眼，
# 位置还正好压在右侧中部那块。记住这条：**这张立绘不需要我们再补第三只眼**。
EYE_ORNAMENT = (W - 180, 360, 64)
# 空中飘的小爱心 (x, y, 半径, alpha)：2026-09-14 与第三只眼一起撤掉，理由同上——
# 立绘左侧本来就有粉色管线绕成的大爱心，再飘几颗就是重复元素。
# 想恢复就填回坐标，例如 ((762, 520, 14, 205), (704, 604, 11, 205))。
FLOAT_HEARTS: tuple[tuple[int, int, int, int], ...] = ()

PALETTES: dict[str, dict[str, Any]] = {
    "a": {"name": "恋恋夜色", "top": (8, 16, 14), "bottom": (18, 54, 42),
          "accent": (242, 210, 75), "text": (246, 249, 246),
          "vine": (54, 92, 64), "rose": (214, 96, 118)},
    "b": {"name": "生日暖金", "top": (28, 10, 18), "bottom": (78, 26, 40),
          "accent": (247, 205, 122), "text": (252, 246, 240),
          "vine": (66, 88, 58), "rose": (226, 116, 126)},
    "c": {"name": "樱粉糖霜", "top": (44, 18, 28), "bottom": (96, 46, 62),
          "accent": (255, 205, 214), "text": (255, 248, 250),
          "vine": (78, 96, 72), "rose": (255, 170, 194)},
    "d": {"name": "星空蓝紫", "top": (10, 12, 34), "bottom": (34, 30, 76),
          "accent": (186, 190, 255), "text": (240, 242, 255),
          "vine": (66, 84, 126), "rose": (180, 148, 246)},
    "e": {"name": "奶油薄荷", "top": (18, 32, 28), "bottom": (56, 92, 78),
          "accent": (206, 240, 206), "text": (248, 255, 250),
          "vine": (50, 96, 78), "rose": (236, 142, 164)},
}

# AI 现写失败时的兜底文案（口语、具体、不堆排比）
DEFAULT_WISHES = [
    "今天是你的生日，帽子先生说要把这天放进小本本里。新的一岁，"
    "愿你想做的事都能慢慢做成，累的时候也有人接住你。",
    "今天是你的生日。恋恋在无意识里绕了一圈，发现关于你的那部分一直是亮的。"
    "祝你今年多碰上几件小事刚好如愿。",
    "今天是你的生日。蜡烛吹完之前，先许一个只跟今天有关的愿望吧——"
    "不用很大，能让你笑一下就够了。",
]

_NO_LINE_START = "，。！？、；：）」』】…～!?,.;:)"


def _vgrad(size, top, bottom):
    w, h = size
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)],
               fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return img


def _fit_cover(img, box_w, box_h):
    w, h = img.size
    scale = max(box_w / w, box_h / h)
    img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    w, h = img.size
    left, top = (w - box_w) // 2, (h - box_h) // 2
    return img.crop((left, top, left + box_w, top + box_h))


def _map_art_point(art_size, canvas_size, x, y):
    """原图坐标 → fit_cover 之后的画布坐标（含居中裁切偏移）。"""
    aw, ah = art_size
    cw, ch = canvas_size
    scale = max(cw / aw, ch / ah)
    sw, sh = aw * scale, ah * scale
    return x * scale - (sw - cw) / 2.0, y * scale - (sh - ch) / 2.0, scale


def _wrap(text, font, max_w):
    lines, cur = [], ""
    for ch in str(text):
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if font.getlength(cur + ch) <= max_w:
            cur += ch
        elif ch in _NO_LINE_START and cur:
            cur += ch
            lines.append(cur)
            cur = ""
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def _balanced_wrap(wish, font, hard_max_w, target_lines):
    """均衡折行：在不超过 hard_max_w 的前提下找**最小行宽**，使折行数正好等于 target_lines。

    为什么需要：贪心折行会让最后一行特别短（例如 840/840/840/517），
    视觉上就是"没对齐"。均衡之后每行长度接近，末行不再突兀。
    """
    if target_lines < 2:
        return hard_max_w, _wrap(wish, font, hard_max_w)
    lo, hi = 40.0, float(hard_max_w)
    best_w, best_lines = float(hard_max_w), _wrap(wish, font, hard_max_w)
    for _ in range(20):
        mid = (lo + hi) / 2
        lines = _wrap(wish, font, mid)
        if len(lines) <= target_lines:
            best_w, best_lines = mid, lines
            hi = mid
        else:
            lo = mid
    return int(best_w), best_lines


def _fit_body(wish, max_w, top, bottom, *, min_ratio=0.60):
    """自适应字号塞进 [top, bottom]，并做均衡折行。

    返回 (font, lines, line_height, wrap_width)。放不下时按句子边界收口，绝不半句切断。
    """
    for size in (38, 36, 34, 32, 30, 28, 26, 24):
        f = ImageFont.truetype(FONT_REG, size)
        lh = int(size * 1.55)
        lines = _wrap(wish, f, max_w)
        if top + len(lines) * lh > bottom:
            continue
        n = len(lines)
        if n >= 2:
            w_bal, lines_bal = _balanced_wrap(wish, f, max_w, n)
            # 只在行宽没被压得太窄时采用（否则字距会太松）
            if len(lines_bal) == n and w_bal >= max_w * min_ratio:
                return f, lines_bal, lh, w_bal
        return f, lines, lh, max_w
    f = ImageFont.truetype(FONT_REG, 24)
    lh = 37
    max_lines = max(1, (bottom - top) // lh)
    txt = "".join(_wrap(wish, f, max_w)[:max_lines])
    for sep in ("。", "！", "？", "～", "；", "，"):
        i = txt.rfind(sep)
        if i > 0:
            txt = txt[:i + 1]
            break
    return f, _wrap(txt, f, max_w)[:max_lines], lh, max_w


def _center(d, text, font, y, fill, shadow=None):
    w = d.textlength(text, font=font)
    x = (W - w) / 2
    if shadow:
        d.text((x + 2, y + 2), text, font=font, fill=shadow)
    d.text((x, y), text, font=font, fill=fill)
    return y


def _justify(d, text, font, y, left, width, fill, shadow=None):
    """把一行撑满 [left, left+width]，按**墨迹边界**对齐。

    关键：不能直接用 font.getlength 累加。标点（。，）的 advance 比它的实际字形
    宽得多，按 advance 对齐会让每行的右墨迹边缘参差 30px 左右——看起来仍然"没对齐"。
    这里最后一个字用 font.getbbox 的墨迹宽度来收尾，右边缘就真的齐了。
    """
    n = len(text)
    if n <= 1:
        d.text((left, y), text, font=font, fill=fill)
        return
    adv = [font.getlength(c) for c in text]
    # 先在临时透明图上按正常间距画一遍，**量出真实墨迹宽度**再决定字距。
    # 不再猜 Pillow 的 bbox 语义：font.getbbox 与 d.textbbox 对标点给的偏移不同，
    # 拿错就把右缘撑不到位（实测以「。」结尾的行差 21px）。量一次最稳。
    try:
        tmp = Image.new("L", (int(width) + 240, font.size * 3 + 60), 0)
        ImageDraw.Draw(tmp).text((120, 30), text, font=font, fill=255)
        bb = tmp.getbbox()
        ink_w = float(bb[2] - bb[0]) if bb else float(sum(adv))
    except Exception:
        ink_w = float(sum(adv))
    extra = float(width) - ink_w
    if extra <= 1.0:
        d.text((left, y), text, font=font, fill=fill)
        return
    gap = extra / (n - 1)
    x = float(left)
    for i, c in enumerate(text):
        if shadow:
            d.text((x + 2, y + 2), c, font=font, fill=shadow)
        d.text((x, y), c, font=font, fill=fill)
        if i < n - 1:
            x += adv[i] + gap


# ------------------------------------------------------- 角色特色装饰（一设）
# 依据（2026-09-14 查证 THBWiki「古明地恋」外貌特征 + ZUN 访谈）：
#   · 第三只眼：**紫色、眼睑闭合**，由管/触手连着身体，「在空中绕出爱心状的图案」。
#     旧版画成了"睁开的同心圆眼睛"，与一设不符 → 这里按一设重画：闭眼 + 连接管 + 爱心。
#   · 蔷薇/荆棘：裙子是蔷薇纹样，但 ZUN 说「不代表她和蔷薇本身有关，主要是象征着
#     内心中的刺」「让内心周围布满荆棘」；心绮楼 Last Word 即「Brambly Rose Garden
#     （带刺的蔷薇园）」→ 卡片边缘做带刺的蔷薇藤。
EYE_VIOLET = (178, 142, 238)
EYE_DEEP = (72, 44, 130)
EYE_PALE = (241, 237, 255)


def _mix(c1, c2, t):
    return tuple(int(round(c1[i] + (c2[i] - c1[i]) * t)) for i in range(3))


def _quad_pts(p0, p1, p2, n=48):
    """二次贝塞尔采样。"""
    out = []
    for i in range(n):
        t = i / (n - 1)
        u = 1 - t
        out.append((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                    u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]))
    return out


def _stem_pts(p0, ctrl, p1, wag=0.0, n=72):
    """带波浪扰动的茎（贝塞尔 + 法向正弦偏移）。"""
    pts = []
    for i in range(n):
        t = i / (n - 1)
        u = 1 - t
        x = u * u * p0[0] + 2 * u * t * ctrl[0] + t * t * p1[0]
        y = u * u * p0[1] + 2 * u * t * ctrl[1] + t * t * p1[1]
        if wag:
            dx = 2 * u * (ctrl[0] - p0[0]) + 2 * t * (p1[0] - ctrl[0])
            dy = 2 * u * (ctrl[1] - p0[1]) + 2 * t * (p1[1] - ctrl[1])
            ln = math.hypot(dx, dy) or 1.0
            off = wag * math.sin(t * math.pi * 2.1)
            x += -dy / ln * off
            y += dx / ln * off
        pts.append((x, y))
    return pts


def _heart_pts(cx, cy, r, n=64):
    """心形参数曲线（16sin³t 那一组），r 约等于半宽。"""
    out = []
    for i in range(n):
        t = 2 * math.pi * i / n
        hx = 16 * math.sin(t) ** 3
        hy = -(13 * math.cos(t) - 5 * math.cos(2 * t)
               - 2 * math.cos(3 * t) - math.cos(4 * t))
        out.append((cx + hx * r / 17.0, cy + hy * r / 17.0))
    return out


def _third_eye(img, accent, cx, cy, r=84):
    """恋恋的第三只眼：紫色、**眼睑闭合**、连着管、管端绕出爱心。"""
    ss = 3
    sw, sh = int(r * 4.6), int(r * 5.6)
    S = Image.new("RGBA", (sw * ss, sh * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(S)
    ox, oy = sw * ss / 2.0, r * ss * 1.78      # 眼心（精灵内坐标）
    gw, rh = r * ss * 1.20, r * ss * 0.78      # 半宽 / 上睑隆起

    # 1) 暗紫光晕：淡紫的眼在花哨立绘上也要读得出来
    glow = Image.new("RGBA", S.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        [ox - gw * 1.32, oy - rh * 1.95, ox + gw * 1.32, oy + rh * 1.95],
        fill=EYE_DEEP + (140,))
    S.alpha_composite(glow.filter(ImageFilter.GaussianBlur(r * ss * 0.40)))

    # 2) 眼型：上弧 + 下弧围成的杏仁形，淡紫底
    up = _quad_pts((ox - gw, oy), (ox, oy - rh * 1.55), (ox + gw, oy))
    lo = _quad_pts((ox + gw, oy), (ox, oy + rh * 1.02), (ox - gw, oy))
    lens = up + lo
    d.polygon(lens, fill=EYE_PALE + (250,))
    d.line(lens + [lens[0]], fill=EYE_DEEP + (255,),
           width=max(2, int(r * ss * 0.055)), joint="curve")

    # 3) 闭着的眼睑：一条向下微弯的粗线（一设「眼睑闭合」）+ 深色睑线
    lid = _quad_pts((ox - gw * 0.99, oy - rh * 0.14), (ox, oy + rh * 0.64),
                    (ox + gw * 0.99, oy - rh * 0.14), 56)
    d.line(lid, fill=EYE_VIOLET + (255,),
           width=max(4, int(r * ss * 0.30)), joint="curve")
    d.line(lid, fill=EYE_DEEP + (255,),
           width=max(2, int(r * ss * 0.048)), joint="curve")

    # 4) 睫毛：两头各 3 根，往外上翘
    for side in (-1, 1):
        bx, by = ox + side * gw * 0.90, oy - rh * 0.20
        for k in range(3):
            ang = (math.radians(-56 + k * 25) if side > 0
                   else math.radians(180 + 56 - k * 25))
            ln = r * ss * (0.36 - k * 0.055)
            d.line([(bx, by), (bx + math.cos(ang) * ln, by + math.sin(ang) * ln)],
                   fill=EYE_DEEP + (250,), width=max(2, int(r * ss * 0.05)))

    # 5) 高光：让闭着的眼睑有体积
    hx, hy = ox - gw * 0.40, oy - rh * 0.52
    d.ellipse([hx - r * ss * 0.14, hy - r * ss * 0.09,
               hx + r * ss * 0.14, hy + r * ss * 0.09],
              fill=(255, 255, 255, 215))

    # 6) 连接管：从眼下伸出，末端系一颗爱心（一设：绕出爱心状的图案）
    cord = _quad_pts((ox - gw * 0.52, oy + rh * 0.62), (ox - gw * 1.52, oy + rh * 1.55),
                     (ox - gw * 1.16, oy + rh * 2.62), 48)
    d.line(cord, fill=EYE_VIOLET + (220,),
           width=max(2, int(r * ss * 0.055)), joint="curve")
    hcx, hcy, hr = ox - gw * 1.16, oy + rh * 3.42, r * ss * 0.40
    hp = _heart_pts(hcx, hcy, hr)
    d.polygon(hp, fill=EYE_VIOLET + (238,))
    d.line(hp + [hp[0]], fill=EYE_DEEP + (235,),
           width=max(2, int(r * ss * 0.045)))
    # 旁边两颗小爱心，呼应"爱心状的图案"
    for dx, dy, sc in ((gw * 1.02, -rh * 1.55, 0.19), (gw * 0.62, rh * 2.75, 0.14)):
        sp = _heart_pts(ox + dx, oy + dy, r * ss * sc)
        d.polygon(sp, fill=EYE_VIOLET + (195,))
        d.line(sp + [sp[0]], fill=EYE_DEEP + (170,), width=max(1, int(r * ss * 0.03)))

    img.alpha_composite(S.resize((sw, sh), Image.LANCZOS),
                        (int(cx - sw / 2), int(cy - oy / ss)))
    return img


def _float_hearts(img, hearts=None):
    """空中飘的小爱心（第三只眼的"轨迹" + 左侧配平），3 倍超采样。"""
    hearts = FLOAT_HEARTS if hearts is None else hearts
    if not hearts:
        return img
    xs = [h[0] for h in hearts]
    ys = [h[1] for h in hearts]
    pad = 60
    x0, y0 = int(min(xs)) - pad, int(min(ys)) - pad
    x1, y1 = int(max(xs)) + pad, int(max(ys)) + pad
    ss = 3
    S = Image.new("RGBA", ((x1 - x0) * ss, (y1 - y0) * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(S)
    glow = Image.new("RGBA", S.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for hx, hy, hr, _al in hearts:
        gd.ellipse([(hx - x0 - hr * 1.6) * ss, (hy - y0 - hr * 1.6) * ss,
                    (hx - x0 + hr * 1.6) * ss, (hy - y0 + hr * 1.6) * ss],
                   fill=EYE_DEEP + (110,))
    S.alpha_composite(glow.filter(ImageFilter.GaussianBlur(6 * ss)))
    for hx, hy, hr, al in hearts:
        pts = _heart_pts((hx - x0) * ss, (hy - y0) * ss, hr * ss)
        d.polygon(pts, fill=EYE_VIOLET + (int(al),))
        d.line(pts + [pts[0]], fill=EYE_DEEP + (min(255, int(al) + 35),),
               width=max(2, int(hr * ss * 0.10)))
    img.alpha_composite(S.resize((x1 - x0, y1 - y0), Image.LANCZOS), (x0, y0))
    return img


def _rose(d, cx, cy, rr, rose, deep, light):
    """一朵蔷薇（俯视）：深色底盘 + 留缝的外圈花瓣 + 内圈螺旋花心。

    前四版都被视觉复审判成"不像蔷薇"：
      ① 5 片椭圆瓣 + 弧线 → "简笔五瓣小花（像雏菊）"
      ② 三层 14 片椭圆瓣   → 同上
      ③ 同心粗圆弧带       → "圆盘/漩涡"
      ④ 同层交叠的月牙瓣   → "同心圆环/靶心"
    共同毛病是**同层花瓣周向连成整圈**，于是只剩下"环"的印象。这一版让同层花瓣
    之间**留 20% 的缝**（底盘深色从缝里透出来 = 花瓣分界），每片半径再抖动一点，
    内圈用一条 4 圈的收拢螺旋当花心 —— 这是小尺寸下最容易被读成"玫瑰"的画法。
    """
    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=deep + (250,))
    cnt = 5
    for k in range(cnt):
        a0 = -90 + k * 360.0 / cnt
        rf = rr * (0.86 + 0.07 * ((k * 7) % 3))
        wd = max(4, int(rr * 0.34))
        span = 360.0 / cnt * 0.80
        col = _mix(rose, (255, 255, 255), 0.20 if k % 2 else 0.02)
        d.arc([cx - rf, cy - rf, cx + rf, cy + rf], start=a0, end=a0 + span,
              fill=col + (252,), width=wd)
    pts = []
    for i in range(160):                      # 内圈螺旋：由外向内收拢
        t = i / 159.0
        th = t * 4.2 * 2 * math.pi
        r = rr * 0.56 * (1 - 0.74 * t)
        pts.append((cx + math.cos(th) * r, cy + math.sin(th) * r))
    d.line(pts, fill=deep + (150,), width=max(2, int(rr * 0.20)), joint="curve")
    d.line(pts, fill=_mix(rose, (255, 255, 255), 0.32) + (250,),
           width=max(2, int(rr * 0.13)), joint="curve")
    rc = rr * 0.10
    d.ellipse([cx - rc, cy - rc, cx + rc, cy + rc], fill=light + (252,))


def _thorn_vine(img, pal, box, *, flip=False, ss=3):
    """一丛带刺蔷薇（一设：蔷薇象征她心里/心周围的荆棘）。

    box=(x0,y0,x1,y1) 是最终像素范围；flip=True 左右镜像（右半边那丛）。
    全部在 ss 倍超采样精灵里画，再 LANCZOS 缩小 → 曲线与睫毛都是干净的。
    """
    x0, y0, x1, y1 = box
    bw, bh = int(x1 - x0), int(y1 - y0)
    vine = tuple(pal.get("vine", (56, 92, 66)))
    rose = tuple(pal.get("rose", (206, 92, 116)))
    deep = _mix(vine, (0, 0, 0), 0.45)
    leaf = _mix(vine, (255, 255, 255), 0.24)
    r_deep = _mix(rose, (0, 0, 0), 0.40)
    r_light = _mix(rose, (255, 255, 255), 0.62)

    S = Image.new("RGBA", (bw * ss, bh * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(S)
    w, h = float(bw * ss), float(bh * ss)

    def at(u, v):
        return (u * w, v * h)

    def stem(p0, ctrl, p1, wag, thorns, leaves, width):
        pts = _stem_pts(at(*p0), at(*ctrl), at(*p1), wag * w, 72)
        d.line(pts, fill=vine + (250,), width=width, joint="curve")
        d.line([(x, y - ss) for x, y in pts], fill=leaf + (140,),
               width=max(1, width // 3), joint="curve")
        for i, t in enumerate(thorns):          # 刺：沿法向的三角
            k = max(1, min(len(pts) - 2, int(t * (len(pts) - 1))))
            ax, ay = pts[k - 1]
            bx2, by2 = pts[k + 1]
            dx, dy = bx2 - ax, by2 - ay
            ln = math.hypot(dx, dy) or 1.0
            nx, ny = -dy / ln, dx / ln
            sgn = 1 if i % 2 == 0 else -1
            bx3, by3 = pts[k]
            bl = 2.8 * ss
            tl = (0.030 + 0.012 * ((i * 7) % 3)) * w
            d.polygon([(bx3 - nx * bl, by3 - ny * bl),
                       (bx3 + nx * bl, by3 + ny * bl),
                       (bx3 + sgn * nx * tl + dx / ln * tl * 0.32,
                        by3 + sgn * ny * tl + dy / ln * tl * 0.32)],
                      fill=deep + (235,))
        for t, ang, sz in leaves:               # 叶：两段二次曲线拼的尖椭圆
            k = max(0, min(len(pts) - 1, int(t * (len(pts) - 1))))
            cxp, cyp = pts[k]
            ln, wd = sz * w, sz * w * 0.44
            ca, sa = math.cos(ang), math.sin(ang)
            tipx, tipy = cxp + ca * ln, cyp + sa * ln
            mx, my = (cxp + tipx) / 2, (cyp + tipy) / 2
            d.polygon(_quad_pts((cxp, cyp), (mx - sa * wd, my + ca * wd),
                                (tipx, tipy), 18)
                      + _quad_pts((tipx, tipy), (mx + sa * wd, my - ca * wd),
                                  (cxp, cyp), 18),
                      fill=leaf + (215,), outline=deep + (165,))

    # 主蔓：从角落斜着爬上右上方，末端开一朵大的
    stem((0.02, 0.95), (0.36, 0.34), (0.90, 0.24), 0.028,
         [0.10, 0.21, 0.32, 0.44, 0.56, 0.68, 0.80, 0.90],
         [(0.30, math.radians(-40), 0.098), (0.63, math.radians(-22), 0.082),
          (0.82, math.radians(-2), 0.068)],
         width=3 * ss)
    # 侧枝：垂到角落，末端一朵小的
    stem((0.36, 0.66), (0.62, 0.74), (0.54, 0.99), 0.016,
         [0.20, 0.45, 0.70, 0.88],
         [(0.55, math.radians(62), 0.062)],
         width=2 * ss)
    _rose(d, *at(0.90, 0.24), 0.100 * w, rose, r_deep, r_light)
    _rose(d, *at(0.54, 0.99), 0.058 * w, rose, r_deep, r_light)
    _rose(d, *at(0.31, 0.28), 0.042 * w, _mix(rose, (255, 255, 255), 0.20),
          r_deep, r_light)

    if flip:
        S = S.transpose(Image.FLIP_LEFT_RIGHT)
    img.alpha_composite(S.resize((bw, bh), Image.LANCZOS), (x0, y0))
    return img


def _paste_avatar(img, avatar_path, art_size, accent):
    if not (avatar_path and Path(avatar_path).exists()):
        return img
    ax, ay, scale = _map_art_point(art_size, img.size, *CAKE_CENTER)
    d = int(CAKE_RADIUS * scale * 1.9)
    av = _fit_cover(Image.open(avatar_path).convert("RGB"), d, d).convert("RGBA")
    mask = Image.new("L", (d, d), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, d - 1, d - 1], fill=255)
    av.putalpha(mask)

    sh = Image.new("RGBA", (d + 60, d + 60), (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse([30, 38, 30 + d, 38 + d], fill=(0, 0, 0, 125))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(11)),
                        (int(ax - (d + 60) / 2), int(ay - (d + 60) / 2)))

    ring = Image.new("RGBA", (d + 26, d + 26), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse([0, 0, d + 25, d + 25], fill=(255, 255, 255, 242))
    img.alpha_composite(ring, (int(ax - (d + 26) / 2), int(ay - (d + 26) / 2)))
    img.alpha_composite(av, (int(ax - d / 2), int(ay - d / 2)))

    halo = Image.new("RGBA", (d + 40, d + 40), (0, 0, 0, 0))
    ImageDraw.Draw(halo).ellipse([0, 0, d + 39, d + 39], outline=accent + (235,), width=4)
    img.alpha_composite(halo, (int(ax - (d + 40) / 2), int(ay - (d + 40) / 2)))
    return img


def _artistic_date(img, text, pal):
    """右上角日期：艺术字（渐变填充 + 深色描边 + 投影 + 菱形装饰线）。

    主人 2026-09-14 要求：**字号调小、不要底衬**（直接压在立绘上）。
    所以这里不铺任何底色/色块，靠深色描边与投影把字从背景里"剥"出来。
    """
    accent = pal["accent"]
    size, track, pad = 40, 5, 20
    font = ImageFont.truetype(FONT_BOLD, size)
    widths = [font.getlength(c) for c in text]
    total = int(sum(widths) + track * max(0, len(text) - 1))
    bw, bh = total + pad * 2, size + pad * 2 + 16
    x0, y0 = W - bw - 30, 30

    # 1) 投影（没有底色，投影就是可读性的主要保障）
    shadow = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    x = pad
    for c, w in zip(text, widths):
        sd.text((x, pad + 2), c, font=font, fill=(0, 0, 0, 205))
        x += int(w) + track
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(6)), (x0 - 2, y0 + 2))

    # 2) 描边层（深色描边，替代底衬保证压在任何底色上都可读）
    stroke = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    dd = ImageDraw.Draw(stroke)
    x = pad
    for c, w in zip(text, widths):
        dd.text((x, pad), c, font=font, fill=(0, 0, 0, 0), stroke_width=4,
                stroke_fill=(10, 9, 12, 245))
        x += int(w) + track

    # 3) 渐变填充（顶部主色 → 底部近白），用文字 alpha 当蒙版
    mask = Image.new("L", (bw, bh), 0)
    md = ImageDraw.Draw(mask)
    x = pad
    for c, w in zip(text, widths):
        md.text((x, pad), c, font=font, fill=255)
        x += int(w) + track
    grad = Image.new("RGB", (bw, bh))
    gd = ImageDraw.Draw(grad)
    for y in range(bh):
        t = y / max(1, bh - 1)
        gd.line([(0, y), (bw, y)],
                fill=tuple(int(accent[i] + (255 - accent[i]) * t) for i in range(3)))
    layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    layer.alpha_composite(stroke)
    fill = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    fill.paste(grad, (0, 0), mask)
    layer.alpha_composite(fill)

    # 4) 装饰线 + 两端小菱形（随字号缩小，离字留余量）
    dl = ImageDraw.Draw(layer)
    ly = pad + size + 15
    inset = pad + int((bw - 2 * pad) * 0.12)
    dl.line([(inset, ly), (bw - inset, ly)], fill=accent + (205,), width=2)
    for cx in (inset, bw - inset):
        dl.polygon([(cx, ly - 5), (cx + 5, ly), (cx, ly + 5), (cx - 5, ly)], fill=accent + (230,))

    img.alpha_composite(layer, (x0, y0))
    return img


def _paste_sticker(img, sticker_path, size=168):
    """左上角小图：白边 + 轻微旋转，做成"贴上去"的感觉。"""
    if not (sticker_path and Path(str(sticker_path)).exists()):
        return img
    st = Image.open(str(sticker_path)).convert("RGBA")
    st.thumbnail((size, size), Image.LANCZOS)
    m = 10
    fr = Image.new("RGBA", (st.size[0] + m * 2, st.size[1] + m * 2), (0, 0, 0, 0))
    ImageDraw.Draw(fr).rounded_rectangle([0, 0, fr.size[0] - 1, fr.size[1] - 1],
                                         radius=18, fill=(255, 255, 255, 236))
    fr.alpha_composite(st, (m, m))
    # 投影
    sh = Image.new("RGBA", (fr.size[0] + 30, fr.size[1] + 30), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([15, 20, 15 + fr.size[0], 20 + fr.size[1]],
                                         radius=18, fill=(0, 0, 0, 120))
    fr2 = fr.rotate(-7, expand=True, resample=Image.BICUBIC)
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(8)).rotate(-7, expand=True,
                                                                      resample=Image.BICUBIC),
                        (40 - 15, 34 - 12))
    img.alpha_composite(fr2, (40, 34))
    return img


def render_card(*, nickname: str, wish: str = "", variant: str | None = None,
                avatar_path: str | Path | None = None, datestr: str = "",
                art: str | Path | None = None, sticker: str | Path | None = None,
                out: str | Path = "data/birthday_card.jpg") -> Path:
    """生成一张贺卡，返回输出路径。variant 为空则从 PALETTES 随机抽。"""
    variant = str(variant or "").strip() or random.choice(list(PALETTES))
    pal = PALETTES.get(variant, PALETTES["a"])
    wish = str(wish or "").strip() or random.choice(DEFAULT_WISHES)
    art_path = str(art or ART_DEFAULT)
    if Path(art_path).exists():
        src = Image.open(art_path).convert("RGB")
        art_size = src.size
        img = _fit_cover(src, W, H).convert("RGBA")
    else:
        art_size = (W, H)
        img = _vgrad((W, H), pal["top"], pal["bottom"]).convert("RGBA")

    img = _paste_avatar(img, avatar_path, art_size, pal["accent"])

    # 磨砂底座：轻度渐进模糊 + 轻压暗。
    # 主人要求文字底色保留"半透明、能透出立绘"的感觉，所以这里比之前轻很多
    # （模糊 13、压暗上限 150）。起点仍放在蛋糕下方，避免糊掉蛋糕与头像。
    y_start, y_full = int(H * 0.615), int(H * 0.775)
    mask = Image.new("L", (1, H), 0)
    for y in range(H):
        v = 0 if y <= y_start else (255 if y >= y_full
                                    else int(255 * (y - y_start) / max(1, y_full - y_start)))
        mask.putpixel((0, y), v)
    # 磨砂底座：更轻的一层（模糊 8 / 压暗上限 96），保留"能透出立绘"的半透明感。
    y_start, y_full = int(H * 0.615), int(H * 0.775)
    mask = Image.new("L", (1, H), 0)
    for y in range(H):
        v = 0 if y <= y_start else (255 if y >= y_full
                                    else int(255 * (y - y_start) / max(1, y_full - y_start)))
        mask.putpixel((0, y), v)
    img = Image.composite(img.filter(ImageFilter.GaussianBlur(8)), img, mask.resize((W, H)))

    scrim = Image.new("L", (1, H), 0)
    for y in range(H):
        t = max(0.0, (y - y_start) / (H - y_start))
        scrim.putpixel((0, y), int(96 * min(1.0, t ** 0.9)))
    dark = Image.new("RGBA", (W, H), pal["top"] + (255,))
    dark.putalpha(scrim.resize((W, H)))
    img = Image.alpha_composite(img, dark)

    # 只加一道极淡的圆角描边做区域划分，**不铺底色**：
    # 主人要的是"文案底色半透明、能透出立绘"，铺底色怎么都会显得发实。
    panel_top, panel_bottom = int(H * 0.612), H - 62
    pd = ImageDraw.Draw(img, "RGBA")
    pd.rounded_rectangle([62, panel_top, W - 62, panel_bottom], radius=44,
                         outline=pal["accent"] + (58,), width=2)

    # 装饰 ①：第三只眼 —— 主人 2026-09-14 看完成品后要求去掉（"眼睛那个还是去掉吧"）。
    # 原因事后查清：**立绘原图自带第三只眼**（右肩蓝球 + 闭眼 + 黑色管线），见文件头注释。
    # 实现与参数都还在（_third_eye / EYE_ORNAMENT），要恢复就把下面这行放开：
    #     img = _third_eye(img, pal["accent"], *EYE_ORNAMENT)
    # 装饰 ①b：空中飘心（FLOAT_HEARTS，默认空 = 不画；立绘左侧已有粉色爱心管线）
    img = _float_hearts(img)
    # 装饰 ②：带刺蔷薇藤。一设：蔷薇是她"内心中的刺"，心绮楼 Last Word 是「带刺的蔷薇园」。
    # 两角的丛**花尖朝外**（离落款远、不挤中间），标题两侧的花尖**朝内**（框住标题）。
    # 与文字墨迹的间距由 _check_ornaments.py 逐字形复核。
    img = _thorn_vine(img, pal, (28, 1296, 372, 1424), flip=True)
    img = _thorn_vine(img, pal, (708, 1296, 1052, 1424))
    img = _thorn_vine(img, pal, (78, 898, 238, 1082))
    img = _thorn_vine(img, pal, (842, 898, 1002, 1082), flip=True)
    d = ImageDraw.Draw(img, "RGBA")

    ds = str(datestr or "").strip()
    if not ds:
        t = date.today()
        ds = f"{t.year}.{t.month:02d}.{t.day:02d}"
    elif len(ds) == 10 and ds[4] == "-":
        y_, m_, d_ = (int(x) for x in ds.split("-"))
        ds = f"{y_}.{m_:02d}.{d_:02d}"
    img = _artistic_date(img, ds, pal)
    img = _paste_sticker(img, sticker or ART_DEFAULT)
    d = ImageDraw.Draw(img, "RGBA")

    y = int(H * 0.640)
    _center(d, "生日快乐！", ImageFont.truetype(FONT_BOLD, 92), y, pal["accent"], (0, 0, 0, 175))
    y += 112
    _center(d, f"致 {nickname}", ImageFont.truetype(FONT_BOLD, 44), y, pal["text"], (0, 0, 0, 165))
    body_top, body_bottom = y + 82, H - 186
    # 排版策略（2026-09-14 定稿）：
    #   · 均衡折行 → 每行字数接近，末行不会短得突兀；
    #   · **撑满宽度 = 均衡宽度**（不是固定 844），这样拉伸量只有几到几十像素，
    #     字距自然，不会出现"末行被拉得很开"；
    #   · 整块正文**居中**（标题/正文/落款同一条视觉轴线），块内末行左对齐不撑满。
    f_body, lines, lh, wrap_w = _fit_body(wish, W - 236, body_top, body_bottom)
    block_left = int((W - wrap_w) / 2)
    yy = body_top
    last_i = len(lines) - 1
    for i, line in enumerate(lines):
        if i == last_i and last_i > 0:
            d.text((block_left, yy), line, font=f_body, fill=pal["text"])
        else:
            _justify(d, line, f_body, yy, block_left, wrap_w, pal["text"], (0, 0, 0, 165))
        yy += lh

    # 落款：不要 "—— 古明地恋 · Koishi"。全角破折号 + 空格在视觉上会被读成
    # "字距被拉大"（两次视觉复核都这么反馈）。改成紧凑的「古明地恋·Koishi」。
    _center(d, "古明地恋·Koishi", ImageFont.truetype(FONT_BOLD, 26), H - 88,
            pal["text"], (0, 0, 0, 170))

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path, quality=92)
    return out_path
