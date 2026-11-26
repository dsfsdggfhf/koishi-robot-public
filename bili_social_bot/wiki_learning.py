"""THBWiki（东方Project维基）学习模块。

通过 MediaWiki API 抓取页面 wikitext，提炼成知识条目存入 touhou_learning.json。
避免保存大段官方原文，只保存摘要和理解（遵守东方二创规范）。
"""
from __future__ import annotations

import re
from typing import Any

import httpx

THWIKI_API = "https://thwiki.cc/api.php"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://thwiki.cc/",
}


def _strip_wikitext(wt: str) -> str:
    """粗略清理 wikitext：去掉模板、链接语法，保留可读文本。"""
    t = wt or ""
    # 去掉 infobox 模板块（{{...}} 嵌套）；有界循环，防止未闭合模板导致死循环
    for _ in range(50):
        if "{{" not in t:
            break
        t2 = re.sub(r"\{\{[^{}]*\}\}", "", t, flags=re.S)
        if t2 == t:
            break  # 无法再剥离（可能是未闭合模板），停止避免死循环
        t = t2
    # 文件/图片
    t = re.sub(r"\[\[文件:[^\]]*\]\]", "", t)
    t = re.sub(r"\[\[图片:[^\]]*\]\]", "", t)
    # 链接 [[目标|显示]] -> 显示；[[目标]] -> 目标
    t = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", t)
    t = re.sub(r"\[\[([^\]]+)\]\]", r"\1", t)
    # 外部链接 [url 文字] -> 文字
    t = re.sub(r"\[(?:https?://[^\s\]]+\s+)?([^\]]+)\]", r"\1", t)
    # 引用/注释
    t = re.sub(r"<ref[^>]*>.*?</ref>", "", t, flags=re.S)
    t = re.sub(r"<ref[^>]*/>", "", t)
    t = re.sub(r"<[^>]+>", "", t)
    # HTML 实体
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    # 压缩空白
    t = re.sub(r"\s+", " ", t)
    return t.strip()


async def fetch_page_wikitext(page: str, timeout: float = 30) -> str:
    """抓取 THBWiki 页面的 wikitext。失败抛异常。"""
    import urllib.parse
    q = urllib.parse.quote(page)
    url = f"{THWIKI_API}?action=parse&page={q}&prop=wikitext&format=json&formatversion=2"
    async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=True, headers=HEADERS) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            raise RuntimeError(f"THBWiki HTTP {resp.status_code}")
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"THBWiki 页面不存在或错误: {data['error']}")
        wt = (data.get("parse") or {}).get("wikitext") or ""
        if not wt:
            raise RuntimeError("THBWiki 返回空 wikitext")
        return wt


def extract_sections(wt: str) -> list[tuple[str, str]]:
    """把 wikitext 按 == 小节 == 切分，返回 [(小节名, 文本)]。

    注意必须先按原始 wikitext 切节，再分别清理正文；如果先整体 strip 会压平换行，
    MediaWiki 标题边界会丢失，学习结果容易退化成单个“概述”。
    """
    raw = wt or ""
    parts = re.split(r"^\s*={2,}\s*(.*?)\s*={2,}\s*$", raw, flags=re.M)
    sections: list[tuple[str, str]] = []
    if parts and _strip_wikitext(parts[0]):
        sections.append(("概述", _strip_wikitext(parts[0])))
    for i in range(1, len(parts) - 1, 2):
        title = _strip_wikitext(parts[i])
        body = _strip_wikitext(parts[i + 1] if i + 1 < len(parts) else "")
        if title and body:
            sections.append((title, body))
    return sections


async def learn_wiki_page(page: str, db, ai, *, category: str = "character") -> tuple[int, str]:
    """学习单个 THBWiki 页面：抓取→提炼→存入学习库。

    Returns: (新增条数, 摘要文本)
    """
    wt = await fetch_page_wikitext(page)
    sections = extract_sections(wt)
    if not sections:
        return 0, ""
    # 合并前几个有实质内容的小节
    meaningful = [(t, b) for t, b in sections if len(b) >= 30]
    added = 0
    digest: list[str] = []
    for title, body in meaningful[:6]:
        # 用 AI 提炼成简洁知识（避免照抄原文）；优先走 premium（rightapi）通道
        if ai is not None and ai.available():
            try:
                prompt = (
                    f"以下是东方Project维基（THBWiki）关于『{page}』的『{title}』小节内容：\n{body[:1800]}\n\n"
                    "请提炼成一条简洁的东方学习知识：核心设定/事实，100字内，用中文，不照抄原文，"
                    "如涉及二创内容请标注。只输出知识正文。"
                )
                if ai.premium_text_api_key and ai.premium_text_model:
                    learned = await ai.chat(
                        [{"role": "system", "content": "你是东方Project知识整理助手，只输出简洁、准确、不照抄原文的知识总结。"},
                         {"role": "user", "content": prompt}],
                        model=ai.premium_text_model,
                        max_tokens=180,
                        base_url=ai.premium_text_base_url,
                        api_key=ai.premium_text_api_key,
                    )
                else:
                    learned = await ai.chat([
                        {"role": "system", "content": "你是东方Project知识整理助手，只输出简洁、准确、不照抄原文的知识总结。"},
                        {"role": "user", "content": prompt},
                    ], max_tokens=180)
                learned = _clean_ai_output(str(learned or ""))
                if len(learned) < 15:
                    continue
            except Exception:
                learned = _fallback_digest(title, body)
        else:
            learned = _fallback_digest(title, body)
        try:
            db.add(
                title=f"Wiki学习：{page}·{title}",
                content=learned,
                category=category,
                source=f"thwiki:{page}",
                tags=["东方Wiki", "THBWiki", page],
                confidence="medium",
            )
            added += 1
            digest.append(f"{title}：{learned[:60]}")
        except Exception:
            continue
    return added, "；".join(digest)


def _fallback_digest(title: str, body: str) -> str:
    """无 AI 时的降级提炼：取前 100 字（已经是清理过的 wikitext 文本）。"""
    text = " ".join(str(body or "").split())
    return (f"{title}：{text[:100]}") if text else f"{title}"


def _clean_ai_output(text: str) -> str:
    """清洗 AI 提炼结果：去掉提示词残留、重复标题前缀、多余的引导语。"""
    t = str(text or "").strip()
    # 去掉常见的提示词残留
    for marker in ("用户想要", "请提炼", "要求：", "1. 核心设定", "输入：", "分析请求", "以下是东方Project维基"):
        idx = t.find(marker)
        if idx > 0:
            t = t[idx:]
            break
    # 去掉开头重复的"标题：标题："模式
    t = re.sub(r"^(?:[^：:]{1,12}[：:]){1,2}\s*", "", t)
    # 去掉 "角色介绍：" 这类重复前缀（如果内容本身以标题开头）
    t = re.sub(r"^(?:角色介绍|角色能力|生活状况|外貌特征|人际关系|登场剧情)[：:]\s*", "", t)
    # 压缩
    t = " ".join(t.split())
    return t[:180]


# 默认学习对象（角色/作品/音乐，可配置）
DEFAULT_WIKI_PAGES = [
    {"page": "古明地恋", "category": "character"},
    {"page": "古明地觉", "category": "character"},
    {"page": "地灵殿", "category": "story"},
    {"page": "东方地灵殿", "category": "story"},
    {"page": "旧地狱", "category": "story"},
    {"page": "火焰猫燐", "category": "character"},
    {"page": "灵乌路空", "category": "character"},
]
