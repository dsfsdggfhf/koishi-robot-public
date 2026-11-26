from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import re
import shutil
import signal
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ai import AIClient
from .bili_client import BiliClient, VideoItem
from .character_recognition import LocalCharacterRecognizer
from .character_samples import format_sample_match_hint, match_character_samples
from .constants import *
from .role_identity import (parse_frame_roles, additional_roles, make_identity,
    identity_context, apply_identity, output_role_conflict, role_candidate_metadata, verification_images,
    role_policy, local_frame_candidates, cached_identity)
from .role_names import canonical_role, role_alias_map


# 上游限流/过载类错误：命中时放慢重试、兜底不重复打 premium（下轮自然重试该视频）。
_THROTTLE_KEYWORDS = (
    "429", "503", "rate limit", "ratelimit", "quota", "too many requests",
    "overload", "busy", "retry later", "限流", "过载", "服务繁忙",
)


def _no_window_kwargs() -> dict[str, Any]:
    """Windows 下阻止子进程新建"可见的"控制台窗口。

    为什么需要：机器人三个进程都由 pythonw.exe 启动，**pythonw 没有控制台**。
    Windows 的规则是——没有控制台的父进程去启动控制台程序（yt-dlp / ffmpeg /
    ffprobe / powershell），系统会为它新建一个控制台窗口并显示出来。
    视频分析里"分段下载 + 逐段抽帧"会在几秒内连续起几十个 yt-dlp/ffmpeg，
    于是屏幕上就是一连串黑窗口闪现（用户反馈："命令窗口一直在启动"）。

    CREATE_NO_WINDOW 只改变窗口可见性，不影响命令行为、退出码与输出。
    """
    if os.name != "nt":
        return {}
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": si}
    except Exception:
        return {}


@dataclass
class MediaAnalysis:
    bvid: str
    title: str
    metadata_text: str = ""
    subtitle_text: str = ""
    audio_text: str = ""
    vision_text: str = ""
    premium_vision_text: str = ""
    role_verify_text: str = ""
    role_identity: dict[str, Any] = field(default_factory=dict)
    ai_conclusion_text: str = ""
    summary: str = ""
    comment_draft: str = ""
    share_reason: str = ""
    # ACRCloud 音乐识别结果（无人声 BGM 时识别曲名/专辑；失败为空）
    music_identify_text: str = ""
    image_paths: list[str] = field(default_factory=list)
    # Storyboard/contact-sheet images generated from keyframes. They are used for
    # model input compression only; raw image_paths remain the source of truth for
    # character sample matching and high-precision role verification.
    storyboard_paths: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def evidence(self) -> str:
        parts = []
        role_context = identity_context(self.role_identity)
        if role_context:
            parts.append(role_context)
        if self.metadata_text:
            parts.append("【标题/简介/元数据】\n" + self.metadata_text)
        if self.subtitle_text:
            parts.append("【字幕】\n" + self.subtitle_text[:EVIDENCE_PER_SOURCE_MAX_CHARS])
        if self.audio_text:
            parts.append("【音频转写】\n" + self.audio_text[:EVIDENCE_PER_SOURCE_MAX_CHARS])
        if self.ai_conclusion_text:
            parts.append("【B站AI总结】\n" + self.ai_conclusion_text[:EVIDENCE_AI_CONCLUSION_CHARS])
        if self.premium_vision_text:
            parts.append("【画面整体理解(GPT)】\n" + self.premium_vision_text[:EVIDENCE_PREMIUM_VISION_CHARS])
        if self.vision_text:
            parts.append("【画面细节分析】\n" + self.vision_text[:EVIDENCE_VISION_CHARS])
        return "\n\n".join(parts)

    def template_vars(self) -> dict[str, str]:
        return {
            "summary": self.summary,
            "comment_draft": self.comment_draft,
            "reason": self.share_reason,
            "evidence": self.evidence[:2000],
            "subtitle": self.subtitle_text[:2000],
            "audio_text": self.audio_text[:2000],
            "vision": self.vision_text[:2000],
        }


_WD_TAGGER = None


def _wd_tagger_singleton():
    """本地 WD14 打标器单例（首次调用才加载 ONNX；缺失/失败静默返回 None）。"""
    global _WD_TAGGER
    if _WD_TAGGER is None:
        try:
            from .wd_tagger import WDTagger
            t = WDTagger()
            _WD_TAGGER = t if t.available() else False
        except Exception:
            _WD_TAGGER = False
    return _WD_TAGGER or None


class MediaAnalyzer:
    """Optional video/image understanding pipeline.

    It is intentionally best-effort: if optional tools are missing, it records a
    note and continues with metadata/subtitles instead of crashing the bot.
    """

    def __init__(self, config: dict[str, Any], ai: AIClient, logger=None, persona_prompt: str = "", knowledge_text: str = ""):
        self.config = config or {}
        self.ai = ai
        self.logger = logger or (lambda msg, level="INFO": None)
        self.persona_prompt = persona_prompt
        self.knowledge_text = knowledge_text
        self.enabled = bool(self.config.get("enabled", False))
        self.cache_dir = Path(str(self.config.get("cache_dir") or "data/media"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.analysis_cache_enabled = bool(self.config.get("analysis_cache_enabled", True))
        self.cache_ttl_hours = int(self.config.get("analysis_cache_ttl_hours", 168) or 168)
        self.character_recognizer = LocalCharacterRecognizer(self.config, knowledge_text=self.knowledge_text, logger=self.logger)
        self._character_lib_cache: dict[str, str] | None = None  # legacy compatibility

    def _character_recognition_cfg(self) -> dict[str, Any]:
        return self.character_recognizer.cfg()

    def character_reference_for_context(self, context_text: str = "") -> str:
        """Compatibility wrapper for chat image analysis."""
        return self.character_recognizer.reference_for_context(context_text)

    def _local_character_hint(self, context_text: str = "") -> str:
        return self.character_recognizer.local_hint(context_text)

    def _character_reference(self) -> str:
        """Compatibility wrapper for old callers; implementation lives in character_recognition.py."""
        return self.character_recognizer.full_reference_fallback()

    def _apply_adaptive_keyframes(self, item: VideoItem, cfg: dict[str, Any], analysis: MediaAnalysis) -> dict[str, Any]:
        """Decide effective keyframe budget from cheap local signals only.

        The rule is conservative by design: default stays at the configured base
        (40 in production), and uncertain videos are not down-sampled.  Tags/title
        can raise the cap for long/game/story videos; optional low-motion reduction
        is off by default to avoid missing unlabelled hand-drawn/MMD music videos.
        """
        out = dict(cfg)
        if out.get("_explicit_max_keyframes_override"):
            out.setdefault("adaptive_keyframes_effective", int(out.get("max_keyframes", 20) or 20))
            return out
        if not out.get("adaptive_keyframes_enabled", False):
            out.setdefault("adaptive_keyframes_effective", int(out.get("max_keyframes", 20) or 20))
            return out
        try:
            base = int(out.get("adaptive_keyframes_default") or out.get("max_keyframes") or 40)
        except Exception:
            base = 40
        try:
            adaptive_max = int(out.get("adaptive_keyframes_max") or 60)
        except Exception:
            adaptive_max = 60
        try:
            hard_cap = int(out.get("adaptive_keyframes_hard_cap") or 72)
        except Exception:
            hard_cap = 72
        base = max(1, min(base, hard_cap))
        adaptive_max = max(base, min(adaptive_max, hard_cap))

        text = " ".join([
            str(item.title or ""),
            str(item.author or ""),
            str(item.desc or ""),
            " ".join(item.tags or []),
        ]).lower()
        duration = int(getattr(item, "duration", 0) or 0)

        game_words = ["通关", "全关卡", "录像", "replay", "clear", "no miss", "nomiss", "lunatic", "extra", "stg", "弹幕", "符卡", "boss", "score", "hit", "怒首领蜂", "东方弹幕", "机体", "全机体"]
        story_words = ["mmd", "手书", "手描", "动画", "短片", "剧情", "pv", "motion", "camera", "model", "stage", "笑传", "剧场", "mad", "amv"]
        draw_words = ["绘画", "水粉", "板绘", "手绘", "过程", "speedpaint", "上色", "教程", "制作", "live2d", "建模"]
        music_low_words = ["xfd", "crossfade", "试听", "专辑", "曲单", "album", "vocal", "bgm", "ost", "arrange", "原曲", "双语字幕", "歌词", "音频可视化", "音乐可视化", "visualizer"]

        score_game = sum(1 for w in game_words if w in text)
        score_story = sum(1 for w in story_words if w in text)
        score_draw = sum(1 for w in draw_words if w in text)
        score_music_low = sum(1 for w in music_low_words if w in text)

        target = base
        reasons: list[str] = [f"base={base}"]
        if duration >= 20 * 60:
            target = max(target, min(adaptive_max, 60))
            reasons.append("long>=20m")
        elif duration >= 8 * 60:
            target = max(target, min(adaptive_max, 48))
            reasons.append("medium-long>=8m")
        if score_game >= 2 or (score_game >= 1 and duration >= 5 * 60):
            target = max(target, min(adaptive_max, 60))
            reasons.append(f"game/stg={score_game}")
        elif score_story >= 1 or score_draw >= 1:
            target = max(target, min(adaptive_max, 48))
            reasons.append(f"visual-story={score_story}/draw={score_draw}")

        # Optional cost-saving path.  Disabled by default because B站 titles/tags
        # often call a hand-drawn/MMD PV just by the song name.
        if bool(out.get("adaptive_keyframes_reduce_low_motion", False)):
            if score_music_low >= 2 and score_game == 0 and score_story == 0 and score_draw == 0 and duration <= 8 * 60:
                target = min(target, max(24, min(base, 32)))
                reasons.append(f"low-motion-music={score_music_low}")

        target = max(1, min(int(target), hard_cap))
        out["max_keyframes"] = target
        out["adaptive_keyframes_effective"] = target
        out["adaptive_keyframes_reason"] = ",".join(reasons)

        if bool(out.get("adaptive_comment_length_enabled", False)) and not out.get("_explicit_comment_max_length_override"):
            length_map = out.get("adaptive_comment_length_by_keyframes") or {}
            if not isinstance(length_map, dict):
                length_map = {}
            defaults = {40: 550, 48: 650, 60: 700}
            # 选不超过当前关键帧档位的最高评论长度档；例如 72 帧仍按 60 档 700 字。
            selected_len = None
            for tier in sorted(defaults):
                raw = length_map.get(str(tier), length_map.get(tier, defaults[tier]))
                try:
                    val = int(raw)
                except Exception:
                    val = defaults[tier]
                if target >= tier:
                    selected_len = val
            if selected_len is None:
                selected_len = int(out.get("comment_max_length", defaults[40]) or defaults[40])
            try:
                floor_len = int(out.get("adaptive_comment_length_min", 300) or 300)
            except Exception:
                floor_len = 300
            try:
                cap_len = int(out.get("adaptive_comment_length_max", 800) or 800)
            except Exception:
                cap_len = 800
            selected_len = max(floor_len, min(int(selected_len), cap_len))
            out["comment_max_length"] = selected_len
            out["adaptive_comment_length_effective"] = selected_len
            out["adaptive_comment_length_reason"] = f"keyframes={target}"
            reasons.append(f"comment_len={selected_len}")

        try:
            analysis.notes.append(f"自适应关键帧：{target}（{out['adaptive_keyframes_reason']}）")
            if out.get("adaptive_comment_length_effective"):
                analysis.notes.append(f"自适应评论长度：{out['adaptive_comment_length_effective']}（{out.get('adaptive_comment_length_reason', '')}）")
            self.logger(f"自适应关键帧：{item.bvid} → {target}（{out['adaptive_keyframes_reason']}）")
            if out.get("adaptive_comment_length_effective"):
                self.logger(f"自适应评论长度：{item.bvid} → {out['adaptive_comment_length_effective']} 字")
        except Exception:
            pass
        return out

    async def analyze(self, client: BiliClient, item: VideoItem, rule: dict[str, Any] | None = None,
                      knowledge_text: str | None = None, persona_prompt: str | None = None) -> MediaAnalysis:
        rule = rule or {}
        cfg = dict(self.config)
        rule_media = rule.get("media_understanding") or {}
        cfg.update(rule_media)
        # Per-call rules (e.g. owner manual deep-comment) may intentionally set a
        # tiny keyframe budget. Do not silently expand those to the global
        # adaptive 40/60 policy unless the rule explicitly opts into adaptive.
        if "max_keyframes" in rule_media and "adaptive_keyframes_enabled" not in rule_media:
            cfg["_explicit_max_keyframes_override"] = True
        if "comment_max_length" in rule_media and "adaptive_comment_length_enabled" not in rule_media:
            cfg["_explicit_comment_max_length_override"] = True
        # per-call 覆盖（用于并发分析时避免共享属性被互相改写）；None 则用实例默认
        kt = self.knowledge_text if knowledge_text is None else knowledge_text
        pp = self.persona_prompt if persona_prompt is None else persona_prompt
        analysis = MediaAnalysis(bvid=item.bvid, title=item.title)
        # 角色识别弱/不确定时，B站视频标签里经常直接写角色名。
        # 标签只作为“明确文本线索”进入角色参考，不按题材/场景给角色加概率；失败不影响主流程。
        if (not item.tags and item.bvid and cfg.get("use_video_tags_for_character_recognition", True)
                and cfg.get("analyze_images", True) and (cfg.get("character_recognition") or {}).get("enabled", True)):
            try:
                item.tags = await client.get_video_tags(item.bvid)
                if item.tags:
                    self.logger(f"已获取视频标签用于角色识别：{item.bvid} tags={item.tags[:8]}")
            except Exception as exc:
                analysis.notes.append(f"视频标签读取失败：{str(exc)[:80]}")
                self.logger(f"视频标签读取失败 {item.bvid}：{str(exc)[:120]}", "WARN")
        analysis.metadata_text = _metadata_text(item)
        cfg = self._apply_adaptive_keyframes(item, cfg, analysis)
        cache_key = _analysis_cache_key(item, cfg, self.ai.model, self.ai.vision_model,
                                        persona_prompt=pp, knowledge_text=kt)
        if cfg.get("analysis_cache_enabled", self.analysis_cache_enabled):
            cached = self._load_analysis_cache(item.bvid, cache_key, int(cfg.get("analysis_cache_ttl_hours", self.cache_ttl_hours) or self.cache_ttl_hours), role_cfg=cfg)
            if cached:
                self.logger(f"命中媒体分析缓存：{item.bvid}")
                # 唧唧可能在缓存生成后又下载了同一视频。缓存已验证可读时，可以安全清掉
                # 这个重复源文件；仍然只允许删除配置目录内、且文件名明确匹配 BV/CID 的文件。
                jiji_cached_source = self._find_jijidown_video(item, cfg, cached)
                if jiji_cached_source:
                    self._delete_jijidown_source(jiji_cached_source, cfg, cached)
                self._cleanup_temp_files(item.bvid)  # 缓存命中也可能残留 wav/mp4，顺手清理
                return cached
        if not cfg.get("enabled", self.enabled):
            return analysis

        if cfg.get("use_subtitles", True):
            try:
                analysis.subtitle_text = await client.get_subtitle_text(
                    item,
                    preferred_languages=list(cfg.get("subtitle_languages") or ["zh-CN", "zh-Hans", "zh", "ai-zh"]),
                )
                if analysis.subtitle_text:
                    self.logger(f"已读取字幕：{item.bvid}，{len(analysis.subtitle_text)} 字")
                else:
                    analysis.notes.append("没有读取到字幕")
            except Exception as exc:
                analysis.notes.append(f"字幕读取失败：{exc}")
                self.logger(f"字幕读取失败 {item.bvid}：{exc}", "WARN")

        # 唧唧没有稳定的命令行下载入口，因此把它作为本地下载仓：优先复用已完成文件。
        # 找不到时再走内置下载兜底，避免全天运行时因为桌面程序没有打开而停摆。
        jiji_video_path = self._find_jijidown_video(item, cfg, analysis)
        managed_video_path = jiji_video_path
        video_path = jiji_video_path
        managed_audio_ready = not bool(cfg.get("transcribe_audio", False))

        # Cover image is cheap and often useful.
        if cfg.get("analyze_cover", True):
            try:
                cover = await client.download_cover(item, str(self.cache_dir))
                if cover:
                    analysis.image_paths.append(cover)
            except Exception as exc:
                analysis.notes.append(f"封面下载失败：{exc}")

        # 视觉增强：多分P首帧（参考 astrbot_plugin_parser 的分P首帧方案）。
        # 合集/系列视频每P有独立首帧画面，比单封面信息量大；失败不影响主流程。
        if cfg.get("use_page_first_frames", True):
            try:
                page_frames = await client.get_page_first_frames(item.bvid, max_pages=int(cfg.get("max_page_first_frames", 6) or 6))
                if page_frames:
                    pf_dir = Path(self.cache_dir) / item.bvid / "page_first_frames"
                    pf_dir.mkdir(parents=True, exist_ok=True)
                    got = 0
                    for i, url in enumerate(page_frames[: int(cfg.get("max_page_first_frames", 6) or 6)]):
                        out = pf_dir / f"page_{i + 1:03d}.jpg"
                        if out.exists():
                            analysis.image_paths.append(str(out))
                            got += 1
                            continue
                        saved = await client.download_image_url(url, str(out))
                        if saved:
                            analysis.image_paths.append(saved)
                            got += 1
                    if got:
                        self.logger(f"已获取 {got} 个分P首帧：{item.bvid}")
            except Exception as exc:
                analysis.notes.append(f"分P首帧获取失败：{exc}")

        # 关键帧：若唧唧已下载完整视频，全部在本地提取；否则使用原下载流程。
        if cfg.get("extract_keyframes", False):
            if jiji_video_path:
                try:
                    frames = await self._extract_keyframes(jiji_video_path, item, cfg, analysis)
                    if frames:
                        analysis.image_paths.extend(frames)
                except Exception as exc:
                    analysis.notes.append(f"唧唧视频抽帧失败：{exc}")
                    self.logger(f"唧唧视频抽帧失败 {item.bvid}：{exc}", "WARN")
            elif not cfg.get("use_segment_download", True):
                # 唧唧暂时没有对应文件时，兜底也只完整下载一次。完整文件后续同时
                # 供抽帧、Whisper 和歌曲识别使用，不再额外下载一遍音频。
                jiji_cfg = cfg.get("jijidown") or {}
                if not (jiji_cfg.get("enabled", False)
                        and not jiji_cfg.get("fallback_to_builtin_downloader", True)):
                    # 第二下载方式：Bilidown 本地接口。失败后才使用机器人内置下载器。
                    video_path = await self._download_with_bilidown(item, cfg, analysis)
                    if video_path:
                        managed_video_path = video_path
                    else:
                        video_path = await self._download_video(item, cfg, analysis)
                    if video_path:
                        frames = await self._extract_keyframes(video_path, item, cfg, analysis)
                        analysis.image_paths.extend(frames)
            elif cfg.get("keyframe_mode", "random") == "random" and cfg.get("use_segment_download", True):
                try:
                    frames = await self._download_sections_and_extract(item, cfg, analysis)
                    if frames:
                        analysis.image_paths.extend(frames)
                except _RiskControlBlocked:
                    # 风控拦截：不再回退完整下载，直接放弃本视频（由上层跳过本轮，下轮再试）。
                    raise
                except Exception as exc:
                    analysis.notes.append(f"分段抽帧失败，回退完整下载：{exc}")
                    self.logger(f"分段抽帧失败 {item.bvid}：{exc}", "WARN")

        # 音频转写：独立下载音频流，不依赖完整视频下载。
        if cfg.get("transcribe_audio", False):
            try:
                if video_path:
                    min_sub = int(cfg.get("skip_asr_min_subtitle_chars", 0) or 0)
                    if min_sub and len(str(analysis.subtitle_text or "").strip()) >= min_sub:
                        analysis.notes.append(
                            f"已有字幕 {len(analysis.subtitle_text)} 字（≥{min_sub}），跳过音频转写"
                        )
                        self.logger(f"已有字幕，跳过本地视频音频转写：{item.bvid}（{len(analysis.subtitle_text)} 字）")
                        if managed_video_path:
                            managed_audio_ready = True
                    else:
                        analysis.audio_text = await self._transcribe_video_audio(
                            video_path, item, cfg, analysis
                        )
                        if managed_video_path:
                            wav = self.cache_dir / item.bvid / "audio.wav"
                            managed_audio_ready = wav.exists() and wav.stat().st_size > 0
                else:
                    analysis.audio_text = await self._download_audio_and_transcribe(client, item, cfg, analysis)
                stripped = (analysis.audio_text or "").strip()
                if stripped and len(stripped) < int(cfg.get("asr_min_meaningful_chars", 6) or 6):
                    # 纯音乐/BGM/游戏演奏常被 whisper 听成几个字甚至乱码（如 2 字杂音）；
                    # 当作无人声处理：既避免垃圾转写混进证据，也让后面的 ACRCloud 指纹补曲名。
                    analysis.notes.append(f"音频转写过短({len(stripped)}字)，疑似无有效人声，已忽略")
                    self.logger(f"音频转写过短，忽略 {item.bvid}：{stripped[:30]!r}", "WARN")
                    analysis.audio_text = ""
                if analysis.audio_text:
                    self.logger(f"音频转写完成：{item.bvid}，{len(analysis.audio_text)} 字")
            except Exception as exc:
                analysis.notes.append(f"音频转写失败：{exc}")
                self.logger(f"音频转写失败 {item.bvid}：{exc}", "WARN")

        # 音乐识别（ACRCloud 免费档，可选）：无人声 BGM 时识别曲名写进证据。
        # 失败不再完全静默：额度满/凭证缺失/ACR 错误码/异常各报一次 WARN（见 _acr_warn）。
        await self._try_music_fingerprint(item, cfg, analysis)

        # 听力增强：B站官方 AI 总结（零成本，参考 astrbot_plugin_parser 的 AI 总结方案）。
        # 与 whisper 转写互补：转写给原文，AI总结给结论；失败静默跳过。
        if cfg.get("use_ai_conclusion", True):
            try:
                conclusion = await client.get_ai_conclusion(item.bvid, item.cid)
                if conclusion:
                    analysis.ai_conclusion_text = conclusion
                    self.logger(f"B站AI总结完成：{item.bvid}，{len(conclusion)} 字")
            except Exception as exc:
                analysis.notes.append(f"B站AI总结失败：{exc}")
                self.logger(f"B站AI总结失败 {item.bvid}：{exc}", "WARN")

        # 若分段抽帧没拿到帧且仍需关键帧，回退完整下载抽帧。
        # 注意：只能用「路径段 == frames」精确判断（"/frames/xxx"），
        # 否则 page_first_frames 目录也会命中 "frames" 子串，导致兜底永不触发。
        has_frames = any(
            ("/frames/" in str(p).replace("\\", "/")) or str(p).replace("\\", "/").endswith("/frames")
            for p in analysis.image_paths
        )
        if cfg.get("extract_keyframes", False) and not has_frames:
            if not video_path:
                jiji_cfg = cfg.get("jijidown") or {}
                if jiji_cfg.get("enabled", False) and not jiji_cfg.get("fallback_to_builtin_downloader", True):
                    analysis.notes.append("没有找到唧唧下载文件，且已关闭内置下载兜底")
                else:
                    video_path = await self._download_video(item, cfg, analysis)
            if video_path:
                frames = await self._extract_keyframes(video_path, item, cfg, analysis)
                analysis.image_paths.extend(frames)

        if cfg.get("storyboard_enabled", False):
            try:
                analysis.storyboard_paths = self._build_storyboards(item.bvid, analysis.image_paths, cfg)
                if analysis.storyboard_paths:
                    analysis.notes.append(f"故事板拼图：{len(analysis.storyboard_paths)} 张")
            except Exception as exc:
                analysis.notes.append(f"故事板生成失败：{str(exc)[:100]}")
                self.logger(f"故事板生成失败 {item.bvid}：{str(exc)[:120]}", "WARN")

        character_context = "\n".join([
            role_candidate_metadata(analysis.metadata_text),
            analysis.subtitle_text[:2000],
            analysis.ai_conclusion_text[:1200],
            item.title,
            item.desc or "",
        ])
        self.character_recognizer.knowledge_text = kt
        # 角色识别以本地《东方角色识别特征库》为主，标题/标签命中的候选只是补充；
        # 无明确角色名时也要把完整特征库喂给视觉模型，避免只在少量常见角色里打转。
        character_ref = self.character_recognizer.full_reference_fallback()
        focused_ref = self.character_recognizer.reference_for_context(character_context)
        if focused_ref:
            character_ref = (focused_ref + "\n\n【完整角色特征库补充】\n" + character_ref).strip()
        sample_cfg = (self.config.get("correction_learning") or {}).get("sample_images") or cfg.get("character_sample_matching") or {}
        sample_hint = ""
        strong_sample_hint = ""
        try:
            sample_matches = match_character_samples(analysis.image_paths, config=sample_cfg, max_results=3)
            # 感知哈希距离 14 只表示“相似”，跨角色二创图也会撞；只有极近且
            # 无其它角色竞争的人工样本才作为提示，复核仍照常运行。
            if sample_matches:
                top = sample_matches[0]
                top_name = str(top.get("correct") or "")
                top_distance = int(top.get("distance") if top.get("distance") is not None else 999)
                competing = any(
                    str(m.get("correct") or "") != top_name
                    and int(m.get("distance") if m.get("distance") is not None else 999) <= top_distance + 4
                    for m in sample_matches[1:]
                )
                if top_name and top_distance <= 4 and not competing:
                    strong_sample_hint = format_sample_match_hint([top])
                    sample_hint = strong_sample_hint
                    analysis.notes.append(f"命中本地角色纠错样本（强匹配：{top_name}，距离{top_distance}）")
                else:
                    analysis.notes.append(f"本地角色样本仅弱相似（最小距离{top_distance}），继续视觉复核")
        except Exception as exc:
            self.logger(f"角色纠错样本匹配失败 {item.bvid}：{exc}", "WARN")
        # 角色识别顺序：先用完整《东方角色识别特征库》按画面特征识别；
        # 如果仅靠特征仍不稳，再参考本地相似样本做纠错，不让样本库压过特征库本身。
        if sample_hint:
            # 模型只读取参考文字前 4000 字；旧代码把样本放在完整特征库末尾，
            # 结果“命中样本并跳过复核”，模型却根本没看到样本。
            character_ref = (strong_sample_hint + "\n\n" + character_ref).strip()
        if cfg.get("character_recognition", {}).get("add_local_hints_to_evidence", True):
            hint = self.character_recognizer.local_hint(character_context)
            local_bits = []
            if hint:
                local_bits.append(hint)
            if sample_hint:
                local_bits.append(sample_hint)
            if local_bits:
                addition = "【本地角色候选/样本】" + "\n".join(local_bits)
                if addition not in analysis.metadata_text:
                    analysis.metadata_text = (analysis.metadata_text + "\n" + addition).strip()

        vision_input_paths = _vision_images_for_model(analysis.image_paths, analysis.storyboard_paths, cfg)
        if vision_input_paths and cfg.get("analyze_images", True):
            extra_refetch_done = False
            if getattr(self.ai, "text_channel", "primary") == "fallback":
                # 降级模式：主提供者额度用尽，视觉自动走阿里百炼备用（由 describe_images 处理）。
                self.logger(f"降级模式，视觉走阿里备用：{item.bvid}", "WARN")
            if not self.ai.vision_available():
                analysis.notes.append("视觉AI未配置或不可用，跳过封面/关键帧理解")
                self.logger(f"视觉AI未配置，跳过图片/画面分析：{item.bvid}", "WARN")
            else:
                try:
                    # 分段视觉启用且帧数足够时：主视觉只看少量全景输入做整体印象（省token），
                    # 全部画面的细致描述交给分段视觉（前/后两批）完成。
                    use_seg = bool(cfg.get("segmented_vision", True)) and len(vision_input_paths) >= 12
                    if use_seg:
                        # 有故事板时前几张已覆盖全片；无故事板时也要均匀抽样，避免整体印象只看开头。
                        selected_images = (vision_input_paths[:4] if analysis.storyboard_paths else _select_full_span_images(
                            vision_input_paths,
                            4,
                            keep_leading=1,
                        ))
                    else:
                        selected_images = _select_full_span_images(
                            vision_input_paths,
                            int(cfg.get("max_images", 6) or 6),
                            keep_leading=1,
                        )
                    self.logger(f"开始视觉分析：{item.bvid}，图片 {len(selected_images)} 张，模型 {self.ai.vision_model}" + ("（全景模式）" if use_seg else ""))
                    _vision_prompt = str(cfg.get("vision_prompt") or "请分析这些 B站视频封面/关键帧，提取画面文字、角色/人物、场景、动作、PPT要点和可见主题。不要编造看不到的内容。")
                    vision = ""
                    if bool(cfg.get("two_pass_recognition", True)):
                        # 两步识别：第一遍只看画面写“可见外观特征”（不给角色库，避免先入为主），
                        # 再按特征检索 2-4 个候选卡+易混对照，第二遍带着少量候选判定角色。
                        try:
                            feats = await self.ai.describe_images(
                                selected_images,
                                prompt=("只描述画面里可见的外观特征，不要猜角色名：①发色与发型 ②瞳色 "
                                        "③帽子/头饰/蝴蝶结 ④服装主色与款式 ⑤非人特征（第三只眼/翼/尾/兽耳/角）"
                                        "⑥手持道具 ⑦画面文字。短句分行，看不清就写看不清。"),
                                reference_text="",
                            )
                            if feats:
                                cand = ""
                                try:
                                    cand = self.character_recognizer.appearance_candidates(feats, limit=4)
                                except Exception:
                                    cand = ""
                                ref2 = ((character_ref + "\n\n" + cand) if cand else character_ref)
                                vision = await self.ai.describe_images(
                                    selected_images, prompt=_vision_prompt, reference_text=ref2)
                                analysis.notes.append("识别两步走：先外观特征→候选卡" + (f"（{len(cand)}字候选）" if cand else "（无候选）"))
                        except Exception as exc:
                            analysis.notes.append(f"两步识别失败，回退单步：{str(exc)[:80]}")
                    if not vision:
                        vision = await self.ai.describe_images(
                            selected_images,
                            prompt=_vision_prompt,
                            reference_text=character_ref,
                        )
                    analysis.vision_text = vision
                    if vision:
                        self.logger(f"视觉分析完成：{item.bvid}，{len(vision)} 字")
                except Exception as exc:
                    analysis.notes.append(f"图片/画面分析失败：{exc}")
                    self.logger(f"图片/画面分析失败 {item.bvid}：{exc}", "WARN")

            if not extra_refetch_done:
                extra_refetch_done = await self._retry_keyframes_if_weak(
                    item, cfg, analysis, character_ref=character_ref, video_path=video_path
                )
                if extra_refetch_done:
                    # Retry may replace raw keyframes; rebuild storyboards and model-input list
                    # so the following segmented pass never references stale/deleted frames.
                    if cfg.get("storyboard_enabled", False):
                        analysis.storyboard_paths = self._build_storyboards(item.bvid, analysis.image_paths, cfg)
                    vision_input_paths = _vision_images_for_model(analysis.image_paths, analysis.storyboard_paths, cfg)

        # ===== 视觉加强：分段深度分析（前/后两段，控制token消耗）=====
        # 关键帧较多（>=12）时，把图片分时间前/后两批分别分析（2次调用），
        # 每批聚焦描述，合并成完整画面叙事；相比单次21图更细，相比3批更省。
        if (vision_input_paths and cfg.get("analyze_images", True)
                and cfg.get("segmented_vision", True)
                and len(vision_input_paths) >= IMAGE_SEGMENTED_MIN):
            try:
                # 分段视觉的输入 = 故事板 + 精选细节帧。故事板负责时间线，
                # 细节帧负责角色/OCR，避免把 40~60 张原图全部送进模型导致费用和延迟飙升。
                imgs = vision_input_paths
                if len(imgs) >= IMAGE_SEGMENTED_MIN:
                    half = max(1, len(imgs) // 2)
                    batches = [imgs[:half], imgs[half:]]
                    labels = ["视频前半部分", "视频后半部分"]
                    seg_parts: list[str] = []
                    for batch, label in zip(batches, labels):
                        if not batch:
                            continue
                        seg_prompt = (
                            f"这是B站视频的{label}画面（封面/关键帧，按时间顺序）。请聚焦分析这部分：\n"
                            "1) 出现的角色（对照特征说出名字）和场景 2) 发生的动作/事件 3) 画面文字（英文要翻译）\n"
                            "用中文，150字内，只描述看得到的。"
                        )
                        seg = await self.ai.describe_images(
                            batch,
                            prompt=seg_prompt,
                            reference_text=character_ref,
                        )
                        if seg:
                            seg_parts.append(f"【{label}】\n{seg}")
                    if len(seg_parts) >= 2:
                        combined = "\n\n".join(seg_parts)
                        # 视觉文本以分段详细版为主（覆盖更全）
                        if len(combined) > len(analysis.vision_text or ""):
                            analysis.vision_text = combined
                            analysis.notes.append(f"视觉分段深度分析：{len(combined)} 字（{len(seg_parts)}段）")
                            self.logger(f"视觉分段分析完成：{item.bvid}，{len(combined)} 字")
            except Exception as exc:
                analysis.notes.append(f"视觉分段分析失败：{exc}")
                self.logger(f"视觉分段分析失败 {item.bvid}：{exc}", "WARN")

        await self._run_character_premium_verify(item, cfg, analysis, character_ref=character_ref)

        want_summary = bool(cfg.get("summarize", True))
        want_comment = bool(cfg.get("generate_comment", False))
        want_share_reason = bool(cfg.get("generate_share_reason", True))

        # ===== 中转站 GPT 视觉增强（默认关闭以省 token，与分段视觉功能重叠）=====
        # 用 rightapi gpt-5.4 看「封面 + 3 张关键帧」做整体剧情/主题理解，
        # 与 minimax-m3 的逐帧细节描述互补；失败自动跳过不影响主流程。
        # 2026-09-07：对“要发评论”的视频默认开启整片理解（premium_vision_for_comment，
        # 默认 True），可随时关；纯学习/其它流程仍按 premium_vision_enhance 开关。
        _premium_on = cfg.get("premium_vision_enhance", False) or (
            want_comment and cfg.get("premium_vision_for_comment", True)
        )
        if analysis.image_paths and _premium_on:
            # 冷却期不撞墙：所有 premium 提供者都在冷却/不可用时，本轮整片理解直接跳过，
            # 免得每个视频都白烧一次无效请求、把限流期拖得更长（恢复后下个视频自动重试）。
            _premium_ready = bool(getattr(self.ai, "premium_ready", lambda: True)())
            if not _premium_ready:
                analysis.notes.append("整片理解跳过（premium通道冷却中）")
                self.logger(f"中转站GPT视觉增强跳过（premium冷却中）{item.bvid}", "WARN")
            else:
                try:
                    sel = _select_full_span_images(analysis.image_paths, 4, keep_leading=1)
                    enhance = await self.ai.describe_images_premium(
                        sel,
                        prompt=(
                            "这是B站视频的封面和关键帧（按时间顺序）。请像真的看视频一样理解："
                            "1) 画面里有什么角色/场景/动作 2) 整体在讲什么故事或主题 3) 最突出的亮点。"
                            "300字内，简洁有力。"
                        ),
                        max_tokens=400,
                        cheap=bool(cfg.get("premium_vision_cheap", True)),  # 默认走便宜档 terra，失败自动回退旗舰
                    )
                    if enhance:
                        # 前面已经做过高风险角色复核；整片理解不能覆盖那份结果。
                        analysis.premium_vision_text = (
                            analysis.premium_vision_text + "\n\n【整片理解】\n" + enhance
                            if analysis.premium_vision_text else enhance
                        )
                        analysis.notes.append(f"中转站GPT视觉增强：{len(enhance)} 字")
                        self.logger(f"中转站GPT视觉增强完成：{item.bvid}，{len(enhance)} 字")
                    else:
                        # 整片理解是评论最重要的素材源之一；为空基本是 premium 通道限流/不可用
                        #（describe_images_premium 遇错返回空串不留痕迹）。留痕便于后续判断：
                        # 是"没跑成"而不是"跑过了但没啥可写"。
                        analysis.notes.append("整片理解无结果（premium通道限流/不可用），评论仅靠普通视觉/文本证据")
                        self.logger(f"中转站GPT视觉增强无结果 {item.bvid}", "WARN")
                except Exception as exc:
                    analysis.notes.append(f"中转站GPT视觉增强失败：{str(exc)[:100]}")
                    self.logger(f"中转站GPT视觉增强失败 {item.bvid}：{str(exc)[:120]}", "WARN")

        # ===== 角色识别：多帧投票 + 元数据先验 + 低置信不写名（免费提升识别率）=====
        if cfg.get("character_vote_enabled", True) and analysis.image_paths:
            try:
                await self._identify_characters(item, cfg, analysis)
            except Exception as exc:
                analysis.notes.append(f"角色投票识别失败：{str(exc)[:80]}")

        ai_throttle = False  # 本视频 AI 生成是否遇到上游限流/过载（抑制重复付费调用）
        budgets = _quality_budgets(cfg)
        # 评论生成本体需要按目标长度给足 token：550/650/700 字目标如果还用 620 的
        # 合并预算，会被 JSON(summary+comment+share_reason) 瓜分截断，导致模型写不到
        # 目标长度而反复被质量门拦截。这里按 comment_max_length*1.8 放大（封顶1600）。
        _comment_cfg_len = int(cfg.get("comment_max_length", 120) or 120)
        gen_budget = int(budgets.get("max_tokens") or 620)
        if want_comment and int(_comment_cfg_len or 0) >= 420:
            gen_budget = max(gen_budget, min(1600, int(_comment_cfg_len * 1.8)))
        # 评论目标长度：保持规则配置值（曾对“无字幕且无音频”自动降级到约 72%，
        # 主人反馈评论因此明显变短，已取消降级；防编造由【证据说明】脚注+看点纪律负责）。
        _eff_comment_len = int(_comment_cfg_len or 0)
        compact_evidence = _compact_evidence(
            analysis,
            metadata_chars=budgets["metadata"],
            subtitle_chars=budgets["subtitle"],
            vision_chars=budgets["vision"],
            audio_chars=budgets["audio"],
        )
        if ((want_summary or want_comment or want_share_reason)
                and getattr(self.ai, 'video_evidence_providers', None) and self.ai.available()):
            compact_evidence = await self.ai.select_video_evidence(compact_evidence)
        # 分享理由是纯文本短任务：可走便宜档 terra 单生成，避免在旗舰档合并里重复烧额度。
        _share_cheap = bool(want_share_reason) and bool(cfg.get("share_reason_cheap", True))
        _inline_share = bool(want_share_reason) and not _share_cheap
        # 便宜档不可用时，摘要与评论合并生成，避免旗舰模型再读一遍相同证据。
        _summary_cheap = bool(want_summary) and bool(cfg.get("summary_cheap", True))
        _inline_summary = bool(want_summary) and not _summary_cheap
        if _inline_summary and want_comment:
            # 合并多一个短字段，留少量输出空间；仍远低于再次发送整份证据。
            gen_budget += min(240, max(160, int(gen_budget * 0.15)))

        if (want_summary or want_comment or want_share_reason) and compact_evidence and self.ai.available():
            try:
                # 多模态优先（评论）：有封面/关键帧图片且订阅窗口健康时，
                # 用 GPT 最高档直接看图+字幕+音频生成评论，再由便宜模型润色转交。
                # 注意：仅当 premium 走 opencode 订阅端点时才支持多模态；
                # rightapi 中转站不支持图片输入（generate_comment_multimodal 会直接返回空），
                # 提前跳过可省掉无谓的 go_usage_ok 查询与日志。
                mm_supported = "opencode.ai" in str(getattr(self.ai, "premium_text_base_url", "") or "")
                if want_comment and analysis.image_paths and cfg.get("analyze_images", True) and mm_supported:
                    usage_ok = True
                    try:
                        usage_ok = await self.ai.go_usage_ok(min_free_percent=25)
                    except Exception:
                        usage_ok = True
                    if usage_ok:
                        try:
                            self.logger(f"GPT多模态评论开始：{item.bvid}，图片 {len(analysis.image_paths)} 张")
                            evidence_txt = _compact_evidence(
                                analysis,
                                metadata_chars=budgets["metadata"],
                                subtitle_chars=budgets["subtitle"],
                                vision_chars=0,  # 图片直接给GPT看，不再重复视觉文本
                                audio_chars=budgets["audio"],
                            )
                            # 硬超时：订阅额度不足/图片上传慢时防止卡死主循环（超时降级走文本链）
                            # 多模态评论精选覆盖全片的 N 张图（默认8张），避免只看封面/开头几帧。
                            mm_max = int(cfg.get("multimodal_max_images", 8) or 8)
                            mm_images = _select_full_span_images(analysis.image_paths, mm_max, keep_leading=2)
                            if len(mm_images) != len(analysis.image_paths):
                                analysis.notes.append(f"多模态评论全片选图：{len(mm_images)}/{len(analysis.image_paths)}张")
                            draft = await asyncio.wait_for(
                                self.ai.generate_comment_multimodal(
                                    item.title,
                                    evidence_txt,
                                    mm_images,
                                    comment_style=str(cfg.get("comment_style") or "自然、具体、真诚，像认真看过后的普通评论"),
                                    comment_max_len=_eff_comment_len,
                                    persona_prompt=pp,
                                    knowledge_text=kt,
                                ),
                                timeout=float(cfg.get("multimodal_comment_timeout_seconds", 90) or 90),
                            )
                            if draft:
                                # 便宜模型转交润色：保留细节，改成更自然口吻
                                final = await self.ai.polish_comment_cheap(draft, item.title, max_len=int(cfg.get("comment_max_length", 120) or 120))
                                analysis.comment_draft = final or draft
                                analysis.notes.append(f"GPT多模态评论：{len(draft)}字→润色")
                                self.logger(f"GPT多模态评论完成：{item.bvid}，{len(analysis.comment_draft)} 字")
                        except asyncio.TimeoutError:
                            analysis.notes.append("GPT多模态评论超时，降级走文本链")
                            self.logger(f"GPT多模态评论超时 {item.bvid}，降级文本链", "WARN")
                        except Exception as exc:
                            analysis.notes.append(f"GPT多模态评论失败，回退文本流程：{exc}")
                            self.logger(f"GPT多模态评论失败 {item.bvid}：{exc}，回退文本流程", "WARN")

                if cfg.get("combine_ai_tasks", True):
                    out = await self.ai.analyze_video_text(
                        item.title,
                        compact_evidence,
                        want_summary=_inline_summary,
                        # 多模态已产出评论时不再重复生成（避免浪费额度）
                        want_comment=want_comment and not analysis.comment_draft,
                        want_share_reason=_inline_share,
                        comment_style=str(cfg.get("comment_style") or "自然、简短、真诚，像认真看过后的普通评论"),
                        share_reason_style=str(cfg.get("share_reason_style") or "简洁、客观地说明为什么值得看"),
                        comment_max_len=_eff_comment_len,
                        persona_prompt=pp,
                        knowledge_text=kt,
                        max_tokens=gen_budget,
                    )
                    analysis.summary = out.get("summary", analysis.summary)
                    if not analysis.comment_draft:
                        analysis.comment_draft = out.get("comment", analysis.comment_draft)
                    analysis.share_reason = out.get("share_reason", analysis.share_reason)
                else:
                    if want_summary:
                        analysis.summary = await self.ai.summarize_text(item.title, compact_evidence, str(cfg.get("summary_prompt") or ""), persona_prompt=pp, knowledge_text=kt, cheap=_summary_cheap)
                    if want_comment and not analysis.comment_draft:
                        analysis.comment_draft = await self.ai.generate_comment(
                            item.title,
                            (identity_context(analysis.role_identity) + "\n" + analysis.summary) if analysis.summary else compact_evidence,
                            style=str(cfg.get("comment_style") or "自然、简短、真诚，像认真看过后的普通评论"),
                            max_len=_eff_comment_len,
                            persona_prompt=pp,
                            knowledge_text=kt,
                        )
                    if want_share_reason and not _share_cheap:
                        analysis.share_reason = await self.ai.generate_share_reason(
                            item.title,
                            (identity_context(analysis.role_identity) + "\n" + analysis.summary) if analysis.summary else compact_evidence,
                            style=str(cfg.get("share_reason_style") or "简洁、客观地说明为什么值得看"),
                            persona_prompt=pp,
                            knowledge_text=kt,
                        )
                    elif _share_cheap:
                        analysis.share_reason = await self.ai.generate_share_reason(
                            item.title,
                            (identity_context(analysis.role_identity) + "\n" + analysis.summary) if analysis.summary else compact_evidence,
                            style=str(cfg.get("share_reason_style") or "简洁、客观地说明为什么值得看"),
                            persona_prompt=pp,
                            knowledge_text=kt,
                            cheap=True,
                        )
            except Exception as exc:
                # 瞬时错误（503/限流/超时等）→ 退避后重试一次；限流时放宽等待并抑制重复 premium
                err = str(exc).lower()
                ai_throttle = ai_throttle or any(k in err for k in _THROTTLE_KEYWORDS)
                wait = 30 if ai_throttle else 10
                analysis.notes.append(f"AI生成第一次失败：{str(exc)[:120]}")
                self.logger(f"AI生成失败，{wait}秒后重试 {item.bvid}：{str(exc)[:150]}", "WARN")
                await asyncio.sleep(wait)
                try:
                    if cfg.get("combine_ai_tasks", True):
                        out = await self.ai.analyze_video_text(
                            item.title,
                            compact_evidence,
                            want_summary=_inline_summary,
                            want_comment=want_comment and not analysis.comment_draft,
                            want_share_reason=_inline_share,
                            comment_style=str(cfg.get("comment_style") or "自然、简短、真诚，像认真看过后的普通评论"),
                            share_reason_style=str(cfg.get("share_reason_style") or "简洁、客观地说明为什么值得看"),
                            comment_max_len=_eff_comment_len,
                            persona_prompt=pp,
                            knowledge_text=kt,
                            max_tokens=gen_budget,
                        )
                        analysis.summary = out.get("summary", analysis.summary)
                        if not analysis.comment_draft:
                            analysis.comment_draft = out.get("comment", analysis.comment_draft)
                        analysis.share_reason = out.get("share_reason", analysis.share_reason)
                    elif want_comment and not analysis.comment_draft:
                        analysis.comment_draft = await self.ai.generate_comment(
                            item.title,
                            (identity_context(analysis.role_identity) + "\n" + analysis.summary) if analysis.summary else compact_evidence,
                            style=str(cfg.get("comment_style") or "自然、简短、真诚，像认真看过后的普通评论"),
                            max_len=_eff_comment_len,
                            persona_prompt=pp,
                            knowledge_text=kt,
                        )
                    analysis.notes.append("AI生成重试成功")
                except Exception as exc2:
                    err2 = str(exc2).lower()
                    ai_throttle = ai_throttle or any(k in err2 for k in _THROTTLE_KEYWORDS)
                    analysis.notes.append(f"AI生成重试仍失败：{str(exc2)[:120]}")
                    self.logger(f"AI生成重试仍失败 {item.bvid}：{str(exc2)[:150]}", "WARN")

        # AI 完全失败时的兜底：只有当没有视觉/字幕证据时才用模板；
        # 有丰富证据却生成失败时宁可留空（上层跳过），不发空话模板。
        # ===== 评论质量门：确保每次评论都是高质量（具体细节+证据相关）=====
        # 兜底档（DeepSeek）生成的评论实测更容易写到 480~500 字，给它更严的长度下限，
        # 避免"降级那天评论整体变短"（只影响兜底档，premium/订阅档不受影响）。
        _gen_tier = ""
        try:
            _gen_tier = str(getattr(self.ai, "text_tier", lambda: "")() or "")
        except Exception:
            _gen_tier = ""
        _len_floor = int(cfg.get("fallback_min_comment_length", 520) or 520) if _gen_tier == "deepseek" else 0
        if _len_floor:
            self.logger(f"兜底档生成，评论长度下限提高到 {_len_floor} 字 {item.bvid}")
        if want_comment and analysis.comment_draft and self.ai.available():
            quality = _comment_quality(analysis.comment_draft, analysis,
                                       int(cfg.get("comment_max_length", 120) or 120),
                                       min_len_floor=_len_floor)
            if not quality:
                # 质量不达标 → 重新生成一次（强调具体细节）
                self.logger(f"评论质量门未过（{quality.reason}），重新生成 {item.bvid}", "WARN")
                try:
                    # 只写评论的独立调用：不再跟 summary/share_reason 抢预算，max_tokens 也按
                    # 长目标放大（见 ai.generate_comment），避免思维链吃完后被截断再次失败。
                    retry_draft = await self.ai.generate_comment(
                        item.title,
                        (identity_context(analysis.role_identity) + "\n" + analysis.summary) if analysis.summary else compact_evidence,
                        style=(str(cfg.get("comment_style") or "自然、真诚")
                               + "。必须引用证据里的具体细节：画面中可见的角色/场景/动作/文字，或音频/字幕里的具体台词、歌名、数字，至少两处，禁止空泛。"),
                        max_len=_eff_comment_len,
                        persona_prompt=pp,
                        knowledge_text=kt,
                    )
                    if retry_draft and _comment_quality(retry_draft, analysis, int(cfg.get("comment_max_length", 120) or 120), min_len_floor=_len_floor):
                        analysis.comment_draft = retry_draft
                        analysis.notes.append("质量门：重新生成后通过")
                    else:
                        # 两次都不达标时的处理（2026-09-11 策略变更）：
                        # 旧逻辑"有证据就不发"，导致丢弃率从 2% 涨到 52% —— 上游
                        # 分析（视觉增强/角色识别/两次生成）的钱已经花掉，整条丢弃
                        # 等于全部白花。现在改为**降级采用**：两个稿子里选较长的那个，
                        # 只要不低于下限就发出去；只有明显太短才真正放弃。
                        has_ev = bool(analysis.premium_vision_text or analysis.vision_text or analysis.subtitle_text or analysis.audio_text or analysis.ai_conclusion_text)
                        _floor = int(cfg.get("quality_gate_degraded_min_chars", 260) or 0)
                        _best = _pick_degraded_draft(retry_draft, analysis.comment_draft, _floor)
                        if _best:
                            analysis.comment_draft = _best
                            analysis.notes.append(
                                f"质量门：两次未达标，降级采用较长稿（{len(_best)} 字 ≥ 下限 {_floor}）"
                            )
                            self.logger(
                                f"评论质量门两次未达标，降级采用 {len(_best)} 字稿 {item.bvid}",
                                "WARN",
                            )
                        elif has_ev:
                            analysis.notes.append("质量门：两次生成均不达标且有证据，跳过发布")
                            self.logger(f"评论两次未达质量标准，跳过 {item.bvid}", "WARN")
                            analysis.comment_draft = ""
                        else:
                            analysis.notes.append("质量门：两次未达标但无证据，保留第二次结果")
                            if retry_draft:
                                analysis.comment_draft = retry_draft
                except Exception as exc:
                    analysis.notes.append(f"质量门重试失败：{str(exc)[:100]}")
                    # 重试失败保留原稿（可能只是偏泛，聊胜于无？不——有证据时仍应坚持质量）
                    if analysis.premium_vision_text or analysis.vision_text or analysis.subtitle_text or analysis.audio_text or analysis.ai_conclusion_text:
                        pass  # 保留原稿，最后一道门在 bot.py 再拦
            else:
                analysis.notes.append("质量门：通过")

        has_evidence = bool(analysis.premium_vision_text or analysis.vision_text or analysis.subtitle_text or analysis.audio_text or analysis.ai_conclusion_text)
        if want_summary and not analysis.summary and compact_evidence:
            # 合并输出偶尔缺字段时才单独补摘要；正常情况不重复发送整份证据。
            try:
                s = await self.ai.summarize_text(
                    item.title,
                    compact_evidence,
                    str(cfg.get("summary_prompt") or ""),
                    persona_prompt=pp,
                    knowledge_text=kt,
                    cheap=_summary_cheap,
                )
                if s:
                    analysis.summary = s
                    analysis.notes.append("摘要(terra档)")
            except Exception as exc:
                analysis.notes.append(f"摘要便宜档失败：{str(exc)[:80]}")
        if want_summary and not analysis.summary and analysis.evidence:
            analysis.summary = _fallback_summary(_compact_evidence(analysis))
        if want_comment and not analysis.comment_draft:
            if has_evidence:
                # 有证据但普通通道生成失败 → 用 GPT 兜底重试一次（保住评论质量，不浪费已做的分析）。
                # 若质量门已经连续生成两次仍未达标，说明模型在此证据/预算下写不到要求长度，
                # 第三次同链路重试基本只会重复付费，直接跳过不重打。
                _quality_twice_failed = any(
                    ("两次生成均不达标" in str(x)) or ("质量门：两次未达标" in str(x))
                    for x in (analysis.notes or [])
                )
                if _quality_twice_failed:
                    analysis.notes.append("质量门两次未达标：跳过模板，本视频不发评论（避免第三次付费重试）")
                    self.logger(f"评论质量门两次未达标，跳过GPT兜底重试 {item.bvid}", "WARN")
                    analysis.comment_draft = ""
                else:
                    try:
                        self.logger(f"评论生成失败，用GPT兜底重试 {item.bvid}", "WARN")
                        gpt_draft = await self.ai.generate_comment(
                            item.title,
                            (identity_context(analysis.role_identity) + "\n" + analysis.summary) if analysis.summary else compact_evidence,
                            style=(str(cfg.get("comment_style") or "自然、真诚")
                                   + "。必须引用证据里的具体细节：画面中可见的角色/场景/动作/文字，或音频/字幕里的具体台词、歌名、数字，至少两处，禁止空泛。"),
                            max_len=_eff_comment_len,
                            persona_prompt=pp,
                            knowledge_text=kt,
                            # 上游限流/过载时兜底改走订阅通道（premium 正在被限流，重打只会失败/烧钱）
                            use_premium=not ai_throttle,
                        )
                        if gpt_draft and _comment_quality(gpt_draft, analysis, int(cfg.get("comment_max_length", 120) or 120), min_len_floor=_len_floor):
                            analysis.comment_draft = gpt_draft
                            analysis.notes.append("GPT兜底重试成功")
                            self.logger(f"GPT兜底评论成功：{item.bvid}，{len(gpt_draft)} 字")
                    except Exception as exc:
                        self.logger(f"GPT兜底重试失败 {item.bvid}：{exc}", "WARN")
                    if not analysis.comment_draft:
                        analysis.notes.append("AI生成失败且有视觉/字幕证据：跳过模板，本视频不发评论")
                        self.logger(f"AI生成失败且有证据，跳过模板评论 {item.bvid}", "WARN")
                        analysis.comment_draft = ""
            else:
                analysis.comment_draft = _fallback_comment_draft(item, analysis, max_len=int(cfg.get("comment_max_length", 120) or 120))
        if want_share_reason and not analysis.share_reason:
            if _share_cheap and (analysis.summary or compact_evidence):
                # 合并生成时已跳过分享理由 → 用便宜档(terra)单独补一份，失败才模板兜底
                try:
                    sr = await self.ai.generate_share_reason(
                        item.title,
                        (identity_context(analysis.role_identity) + "\n" + analysis.summary) if analysis.summary else compact_evidence,
                        style=str(cfg.get("share_reason_style") or "简洁、客观地说明为什么值得看"),
                        persona_prompt=pp,
                        knowledge_text=kt,
                        cheap=True,
                    )
                    if sr:
                        analysis.share_reason = sr
                        analysis.notes.append("分享理由(terra档)")
                except Exception as exc:
                    analysis.notes.append(f"分享理由便宜档失败：{str(exc)[:80]}")
            if not analysis.share_reason:
                analysis.share_reason = _fallback_share_reason(item, analysis)

        # This is a local final gate, with no extra model retry or name replacement.
        for field_name in ("summary", "comment_draft", "share_reason"):
            if output_role_conflict(getattr(analysis, field_name), analysis.role_identity):
                setattr(analysis, field_name, "")
                analysis.notes.append(f"角色一致性门：{field_name}含未确认或冲突身份，已拦截")
        cache_saved = False
        if cfg.get("analysis_cache_enabled", self.analysis_cache_enabled):
            cache_saved = self._save_analysis_cache(item.bvid, cache_key, analysis)

        # 自动删除只发生在：缓存 JSON 已成功落盘、所需关键帧仍存在、分析至少产生一项
        # 可复用结果、音频处理也已完成。任何中断或失败都会保留源视频供下次重试。
        if managed_video_path and self._jijidown_analysis_is_durable(
            analysis, cfg, cache_saved=cache_saved, audio_ready=managed_audio_ready
        ):
            self._delete_jijidown_source(managed_video_path, cfg, analysis)
        # 磁盘清理：分析完成并缓存后，删除大体积临时文件（全片/音频/片段），
        # 只保留关键帧图片（缓存 JSON 引用它们）。防止 data/media 无限膨胀。
        try:
            bdir = self.cache_dir / item.bvid
            if bdir.is_dir():
                for p in bdir.glob("video.*"):
                    p.unlink(missing_ok=True)
                seg_dir = bdir / "segs"
                if seg_dir.is_dir():
                    for p in seg_dir.rglob("*"):
                        if p.is_file():
                            p.unlink(missing_ok=True)
                    try:
                        seg_dir.rmdir()
                    except Exception:
                        pass
                audio_wav = bdir / "audio.wav"
                if audio_wav.exists():
                    audio_wav.unlink(missing_ok=True)
        except Exception:
            pass
        return analysis

    def _build_storyboards(self, bvid: str, image_paths: list[str], cfg: dict[str, Any]) -> list[str]:
        """Build compact contact sheets from extracted keyframes.

        A storyboard gives the vision model the time-line with far fewer uploaded
        images.  It is intentionally optional and best-effort; missing Pillow or
        unreadable images simply disables the optimization for that video.
        """
        frame_paths = [
            Path(p) for p in image_paths
            if "/frames/" in str(p).replace("\\", "/") and Path(p).exists()
        ]
        try:
            min_frames = int(cfg.get("storyboard_min_frames") or 16)
            segments = int(cfg.get("storyboard_segments") or 3)
            per_segment = int(cfg.get("storyboard_frames_per_segment") or 8)
            thumb_w = int(cfg.get("storyboard_thumb_width") or 320)
            quality = int(cfg.get("storyboard_jpeg_quality") or 82)
        except Exception:
            min_frames, segments, per_segment, thumb_w, quality = 16, 3, 8, 320, 82
        min_frames = max(4, min_frames)
        segments = max(1, min(8, segments))
        per_segment = max(2, min(12, per_segment))
        thumb_w = max(160, min(640, thumb_w))
        quality = max(60, min(92, quality))
        if len(frame_paths) < min_frames:
            return []
        try:
            from PIL import Image, ImageDraw, ImageFont
        except Exception:
            return []

        board_dir = self.cache_dir / str(bvid) / "storyboards"
        board_dir.mkdir(parents=True, exist_ok=True)
        for old in board_dir.glob("storyboard_*.jpg"):
            try:
                old.unlink()
            except Exception:
                pass

        # Split the time-line, then sample evenly inside every segment.
        boards: list[str] = []
        total_used = 0
        for si in range(segments):
            start = int(si * len(frame_paths) / segments)
            end = int((si + 1) * len(frame_paths) / segments)
            chunk = frame_paths[start:end]
            if not chunk:
                continue
            picked = [Path(p) for p in _pick_evenly_spaced([str(x) for x in chunk], min(per_segment, len(chunk)))]
            thumbs = []
            for idx, path in enumerate(picked, 1):
                try:
                    img = Image.open(path).convert("RGB")
                    w, h = img.size
                    if w <= 0 or h <= 0:
                        continue
                    thumb_h = max(1, int(h * (thumb_w / w)))
                    img = img.resize((thumb_w, thumb_h), Image.LANCZOS)
                    thumbs.append((path.name, img))
                except Exception:
                    continue
            if not thumbs:
                continue
            cols = 4 if len(thumbs) >= 4 else len(thumbs)
            rows = (len(thumbs) + cols - 1) // cols
            label_h = 22
            cell_h = max(img.height for _, img in thumbs) + label_h
            canvas = Image.new("RGB", (thumb_w * cols, cell_h * rows), "white")
            draw = ImageDraw.Draw(canvas)
            try:
                font = ImageFont.load_default()
            except Exception:
                font = None
            for i, (name, img) in enumerate(thumbs):
                x = (i % cols) * thumb_w
                y = (i // cols) * cell_h
                canvas.paste(img, (x, y + label_h))
                draw.text((x + 4, y + 4), f"S{si + 1}-{i} {name}", fill=(0, 0, 0), font=font)
            out = board_dir / f"storyboard_{si + 1:03d}.jpg"
            canvas.save(out, format="JPEG", quality=quality, optimize=True)
            boards.append(str(out))
            total_used += len(thumbs)
        if boards:
            self.logger(f"故事板生成完成：{bvid}，{len(boards)} 张（覆盖 {total_used} 帧）")
        return boards

    def _cache_file(self, bvid: str, cache_key: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", bvid or "unknown")
        return self.cache_dir / safe / f"analysis_{cache_key[:16]}.json"

    def _load_analysis_cache(self, bvid: str, cache_key: str, ttl_hours: int,
                             role_cfg: dict | None = None) -> MediaAnalysis | None:
        path = self._cache_file(bvid, cache_key)
        if not path.exists():
            return None
        try:
            import time
            if ttl_hours > 0 and time.time() - path.stat().st_mtime > ttl_hours * 3600:
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("cache_key") != cache_key:
                return None
            payload = data.get("analysis") or {}
            if not isinstance(payload, dict):
                return None
            image_paths = [str(x) for x in payload.get("image_paths", [])]
            existing_images = [x for x in image_paths if Path(x).exists()]
            storyboard_paths = [str(x) for x in payload.get("storyboard_paths", [])]
            existing_storyboards = [x for x in storyboard_paths if Path(x).exists()]
            notes = [str(x) for x in payload.get("notes", [])] + ["来自媒体分析缓存"]
            if image_paths and not existing_images:
                # 图片证据全丢失时让缓存失效，避免后续多模态/评论质量静默退化。
                return None
            if len(existing_images) != len(image_paths):
                notes.append(f"缓存图片丢失：{len(image_paths) - len(existing_images)} 张")
            cached = MediaAnalysis(
                bvid=str(payload.get("bvid") or bvid),
                title=str(payload.get("title") or ""),
                metadata_text=str(payload.get("metadata_text") or ""),
                subtitle_text=str(payload.get("subtitle_text") or ""),
                audio_text=str(payload.get("audio_text") or ""),
                vision_text=str(payload.get("vision_text") or ""),
                premium_vision_text=str(payload.get("premium_vision_text") or ""),
                role_verify_text=str(payload.get("role_verify_text") or ""),
                role_identity=dict(payload.get("role_identity") or {}),
                ai_conclusion_text=str(payload.get("ai_conclusion_text") or ""),
                summary=str(payload.get("summary") or ""),
                comment_draft=str(payload.get("comment_draft") or ""),
                share_reason=str(payload.get("share_reason") or ""),
                music_identify_text=str(payload.get("music_identify_text") or ""),
                image_paths=existing_images,
                storyboard_paths=existing_storyboards,
                notes=notes,
            )
            identity = cached.role_identity
            if not identity:
                # Read an existing explicit verdict only; no new vision call or global cache invalidation.
                verdicts = re.findall(r"【角色识别】([^\n（(]+)", cached.vision_text)
                if "【角色识别·未确认" in cached.vision_text:
                    identity = make_identity([], self.character_recognizer.aliases(),
                        cached.vision_text + "\n" + cached.premium_vision_text,
                        source="cached-explicit-uncertain")
                elif verdicts:
                    label = verdicts[-1].strip()
                    soft = any(t in label for t in ("疑似", "未确认", "不确定"))
                    names = self.character_recognizer.names_in_text(label, limit=6)
                    if names or soft:
                        identity = make_identity([] if soft else names,
                            self.character_recognizer.aliases(),
                            cached.vision_text + "\n" + cached.premium_vision_text,
                            uncertain=names if soft else [], source="cached-explicit-verdict")
            if identity or (role_cfg and role_cfg.get("character_vote_enabled", True) and existing_images):
                identity = cached_identity(identity, self.character_recognizer.aliases(),
                    cached.vision_text + "\n" + cached.premium_vision_text, role_cfg)
                apply_identity(cached, identity)
                if identity.get("source") == "cached-policy-needs-verification":
                    cached.notes.append("角色缓存策略已变化或旧证据不完整：身份暂列待核验；复用已有分析，不增加识图调用")
            return cached
        except Exception:
            return None

    def _cleanup_temp_files(self, bvid: str) -> None:
        """删除该视频的大体积临时文件（全片/音频/片段），保留关键帧图片与缓存 JSON。

        独立方法：在 analyze 的缓存命中、正常完成、finally（含超时取消）都会调用，
        防止中断路径残留 wav/mp4 导致磁盘无限膨胀。
        """
        try:
            bdir = self.cache_dir / str(bvid)
            if not bdir.is_dir():
                return
            # 全片视频 / 音频 / 分段片段 / 其它临时文件
            for pattern in ("video.*", "audio.*", "*.part", "*.ytdl", "*.tmp"):
                for p in bdir.glob(pattern):
                    p.unlink(missing_ok=True)
            seg_dir = bdir / "segs"
            if seg_dir.is_dir():
                for p in seg_dir.rglob("*"):
                    if p.is_file():
                        p.unlink(missing_ok=True)
                try:
                    seg_dir.rmdir()
                except Exception:
                    pass
            # 清理过期的 analysis 缓存（超过 TTL 的）
            try:
                import time as _t
                ttl_hours = int(getattr(self, "cache_ttl_hours", 24) or 24)
                for p in bdir.glob("analysis_*.json"):
                    if ttl_hours > 0 and _t.time() - p.stat().st_mtime > ttl_hours * 3600:
                        p.unlink(missing_ok=True)
            except Exception:
                pass
        except Exception:
            pass

    def _save_analysis_cache(self, bvid: str, cache_key: str, analysis: MediaAnalysis) -> bool:
        try:
            path = self._cache_file(bvid, cache_key)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "cache_key": cache_key,
                "analysis": {
                    "bvid": analysis.bvid,
                    "title": analysis.title,
                    "metadata_text": analysis.metadata_text,
                    "subtitle_text": analysis.subtitle_text,
                    "audio_text": analysis.audio_text,
                    "vision_text": analysis.vision_text,
                    "premium_vision_text": analysis.premium_vision_text,
                    "role_verify_text": analysis.role_verify_text,
                    "role_identity": analysis.role_identity,
                    "ai_conclusion_text": analysis.ai_conclusion_text,
                    "summary": analysis.summary,
                    "comment_draft": analysis.comment_draft,
                    "share_reason": analysis.share_reason,
                    "music_identify_text": analysis.music_identify_text,
                    "image_paths": analysis.image_paths,
                    "storyboard_paths": analysis.storyboard_paths,
                    "notes": analysis.notes,
                },
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception:
            return False

    def _jijidown_roots(self, cfg: dict[str, Any]) -> list[Path]:
        jcfg = cfg.get("jijidown") or {}
        project_root = Path(__file__).resolve().parent.parent
        roots: list[Path] = []
        raw_roots: list[str] = []
        if jcfg.get("enabled", False):
            raw_roots.extend(str(x) for x in (jcfg.get("download_dirs") or []))
        bcfg = cfg.get("bilidown") or {}
        if bcfg.get("enabled", False) and bcfg.get("download_dir"):
            raw_roots.append(str(bcfg.get("download_dir")))
        for raw in raw_roots:
            value = os.path.expandvars(os.path.expanduser(str(raw or "").strip()))
            if not value:
                continue
            path = Path(value)
            if not path.is_absolute():
                path = project_root / path
            try:
                resolved = path.resolve()
            except Exception:
                continue
            if resolved not in roots:
                roots.append(resolved)
        return roots

    def _find_jijidown_video(self, item: VideoItem, cfg: dict[str, Any], analysis: MediaAnalysis) -> str:
        """Find a completed JiJiDown file whose name contains this video's BV or CID."""
        jcfg = cfg.get("jijidown") or {}
        roots = self._jijidown_roots(cfg)
        if not roots:
            return ""
        extensions = {
            str(x).lower() if str(x).startswith(".") else "." + str(x).lower()
            for x in (jcfg.get("extensions") or [".mp4", ".mkv", ".flv", ".webm", ".mov"])
        }
        settle = max(0, int(jcfg.get("settle_seconds", 90) or 90))
        max_age_hours = max(0, int(jcfg.get("max_age_hours", 168) or 168))
        max_scan = max(100, int(jcfg.get("max_scan_files", 5000) or 5000))
        recursive = bool(jcfg.get("recursive", True))
        now = __import__("time").time()
        bvid = str(getattr(item, "bvid", "") or "").strip()
        cid = str(getattr(item, "cid", "") or "").strip()
        candidates: list[tuple[int, float, int, Path]] = []
        for root in roots:
            if not root.is_dir():
                continue
            scanned = 0
            try:
                paths = root.rglob("*") if recursive else root.glob("*")
                for path in paths:
                    scanned += 1
                    if scanned > max_scan:
                        break
                    if not path.is_file() or path.suffix.lower() not in extensions:
                        continue
                    name = path.stem
                    bvid_match = bool(bvid and re.search(
                        rf"(?<![A-Za-z0-9]){re.escape(bvid)}(?![A-Za-z0-9])", name, re.IGNORECASE
                    ))
                    cid_match = bool(cid and cid != "0" and re.search(
                        rf"(?<!\d){re.escape(cid)}(?!\d)", name
                    ))
                    if not (bvid_match or cid_match):
                        continue
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    age = now - st.st_mtime
                    if st.st_size <= 0 or age < settle:
                        continue
                    if max_age_hours and age > max_age_hours * 3600:
                        continue
                    candidates.append((2 if bvid_match else 1, st.st_mtime, st.st_size, path.resolve()))
            except OSError:
                continue
        if not candidates:
            return ""
        candidates.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        selected = candidates[0][3]
        label = "唧唧"
        bcfg = cfg.get("bilidown") or {}
        if bcfg.get("download_dir"):
            try:
                selected.relative_to(Path(os.path.expandvars(str(bcfg.get("download_dir")))).resolve())
                label = "Bilidown"
            except (ValueError, OSError, RuntimeError):
                pass
        analysis.notes.append(f"使用{label}本地视频：{selected.name}")
        self.logger(f"使用{label}本地视频：{item.bvid} → {selected}")
        return str(selected)

    def _jijidown_analysis_is_durable(self, analysis: MediaAnalysis, cfg: dict[str, Any], *,
                                      cache_saved: bool, audio_ready: bool) -> bool:
        if not cache_saved:
            return False
        if cfg.get("extract_keyframes", False):
            kept_images = list(analysis.image_paths or []) + list(analysis.storyboard_paths or [])
            if not any(Path(str(p)).is_file() for p in kept_images):
                return False
        if cfg.get("transcribe_audio", False) and not audio_ready:
            return False
        return bool(
            analysis.vision_text or analysis.premium_vision_text or analysis.summary
            or analysis.comment_draft or analysis.audio_text or analysis.music_identify_text
            or analysis.subtitle_text
        )

    def _delete_jijidown_source(self, video_path: str, cfg: dict[str, Any], analysis: MediaAnalysis) -> bool:
        jcfg = cfg.get("jijidown") or {}
        try:
            path = Path(video_path).resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        # 防止配置错误导致删除任意文件：目标必须严格位于某个唧唧下载目录之内。
        allowed = False
        source_label = "唧唧"
        for root in self._jijidown_roots(cfg):
            try:
                path.relative_to(root)
                allowed = True
                bcfg = cfg.get("bilidown") or {}
                if bcfg.get("download_dir"):
                    try:
                        path.relative_to(Path(os.path.expandvars(str(bcfg.get("download_dir")))).resolve())
                        source_label = "Bilidown"
                    except (ValueError, OSError, RuntimeError):
                        pass
                break
            except ValueError:
                continue
        if not allowed or not path.is_file():
            self.logger(f"拒绝删除不在受管下载目录内的文件：{path}", "WARN")
            return False
        bcfg = cfg.get("bilidown") or {}
        auto_delete = (
            bool(bcfg.get("auto_delete_after_analysis", False))
            if source_label == "Bilidown"
            else bool(jcfg.get("auto_delete_after_analysis", False))
        )
        if not auto_delete:
            return False
        if source_label == "Bilidown":
            from .bilidown_cleanup import OwnedDownloads
            try:
                deleted = OwnedDownloads(self.cache_dir, cfg).delete_owned(path)
                if deleted:
                    analysis.notes.append(f"Bilidown机器人源视频已自动删除：{path.name}")
                    self.logger(f"Bilidown机器人源视频已自动删除：{path}")
                else:
                    self.logger(f"Bilidown文件无机器人所有权记录或已被修改，保留：{path.name}", "INFO")
                return deleted
            except Exception as exc:
                self.logger(f"Bilidown所有权核验失败，保留源视频：{type(exc).__name__}", "WARN")
                return False
        # Imported JiJiDown files have no bot submission receipt. Finding a BV/CID
        # in a file name is sufficient for analysis, but does not prove ownership.
        analysis.notes.append(f"{source_label}源视频无机器人任务所有权回执，保留：{path.name}")
        return False

    async def _retry_keyframes_if_weak(self, item: VideoItem, cfg: dict[str, Any], analysis: MediaAnalysis, *, character_ref: str, video_path: str = "") -> bool:
        """首轮视觉信息不足时，清理旧帧并重新抽取一批关键帧。返回是否已尝试。"""
        if not (cfg.get("keyframe_retry_on_weak_vision", True)
                and cfg.get("extract_keyframes", False)
                and _vision_capture_weak(analysis.vision_text, analysis.metadata_text)):
            return False
        try:
            self.logger(f"视觉信息不足，重新捕捉关键帧：{item.bvid}", "WARN")
            self._clear_keyframe_retry_dirs(item.bvid)
            retry_cfg = self._retry_keyframe_cfg(cfg)
            retry_frames = await self._download_sections_and_extract(item, retry_cfg, analysis)
            if not retry_frames and video_path:
                retry_frames = await self._extract_keyframes(video_path, item, retry_cfg, analysis)
            if retry_frames and self.ai.vision_available():
                await self._analyze_retry_keyframes(item, retry_cfg, analysis, retry_frames, character_ref=character_ref)
        except Exception as exc:
            analysis.notes.append(f"关键帧二次捕捉失败：{exc}")
            self.logger(f"关键帧二次捕捉失败 {item.bvid}：{exc}", "WARN")
        return True

    def _clear_keyframe_retry_dirs(self, bvid: str) -> None:
        bdir = self.cache_dir / str(bvid)
        for sub in ("frames", "segs", "storyboards"):
            d = bdir / sub
            if not d.is_dir():
                continue
            for path in sorted(d.rglob("*"), reverse=True):
                try:
                    if path.is_file():
                        path.unlink(missing_ok=True)
                    elif path.is_dir():
                        path.rmdir()
                except Exception:
                    pass

    @staticmethod
    def _retry_keyframe_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
        retry_cfg = dict(cfg)
        retry_cfg["max_keyframes"] = max(int(cfg.get("max_keyframes", 8) or 8), int(cfg.get("retry_max_keyframes", 12) or 12))
        retry_cfg["segment_length_seconds"] = max(int(cfg.get("segment_length_seconds", 4) or 4), int(cfg.get("retry_segment_length_seconds", 6) or 6))
        # 重抽只补一小批，且**限制下载次数**：重抽是一次额外的整轮分段下载，
        # 不放上限的话（目标帧数 ×2 时）会把风控暴露翻倍。帧数靠每段多抽补足。
        retry_cfg["max_segment_downloads"] = min(
            int(retry_cfg.get("max_segment_downloads", retry_cfg["max_keyframes"]) or retry_cfg["max_keyframes"]),
            max(6, int(cfg.get("retry_max_segment_downloads", 12) or 12)),
        )
        retry_cfg["segment_frames_per_segment"] = max(1, int(cfg.get("segment_frames_per_segment", 1) or 1))
        return retry_cfg

    async def _analyze_retry_keyframes(self, item: VideoItem, cfg: dict[str, Any], analysis: MediaAnalysis, retry_frames: list[str], *, character_ref: str) -> None:
        retry_prompt = str(cfg.get("vision_prompt") or "请分析这些 B站视频关键帧，提取画面文字、角色/人物、场景、动作和可见主题。不要编造看不到的内容。")
        retry_prompt += "\n\n注意：上一轮关键帧信息不足，这一轮请重点寻找角色、字幕/画面文字、关键动作和剧情转折；仍然看不到就明确说看不到。"
        retry_vision = await self.ai.describe_images(
            _select_full_span_images(retry_frames, int(cfg.get("max_images", 12) or 12), keep_leading=0),
            prompt=retry_prompt,
            reference_text=character_ref,
        )
        if retry_vision and (len(retry_vision) > len(analysis.vision_text or "") or not _vision_capture_weak(retry_vision, analysis.metadata_text)):
            analysis.vision_text = retry_vision
            analysis.image_paths = [p for p in analysis.image_paths if "/frames/" not in str(p).replace("\\", "/")]
            analysis.image_paths.extend(retry_frames)
            analysis.notes.append(f"关键帧二次捕捉成功：{len(retry_frames)} 帧")
            self.logger(f"关键帧二次捕捉完成：{item.bvid}，{len(retry_frames)} 帧，视觉 {len(retry_vision)} 字")

    async def _identify_characters(self, item: VideoItem, cfg: dict[str, Any], analysis: MediaAnalysis) -> None:
        """多帧角色投票识别：每帧单独判定 → 取多数；低置信/分歧时明确“不要断言角色名”。

        先用「元数据点名 + 外观候选卡」构造紧凑先验（避免塞整本特征库导致稀释/超时），
        再逐帧问“第一行给角色名”，最后按票数定论。
        """
        frames = _select_full_span_images(analysis.image_paths, int(cfg.get("character_vote_frames", 3) or 3),
                                          keep_leading=1)
        if not frames:
            return
        title_text = str(item.title or "")
        tags_text = " ".join(str(t) for t in (getattr(item, "tags", None) or []))
        meta = " ".join([
            title_text,
            str(item.desc or ""),
            tags_text,
            (analysis.subtitle_text or "")[:800],
            (analysis.ai_conclusion_text or "")[:400],
        ])
        priors: list[str] = []
        try:
            priors = self.character_recognizer.names_in_text(meta, limit=6)
        except Exception:
            priors = []
        # 标题/标签点名的角色：比简介/字幕里的顺带提及可信得多（UP 自己写的作品对象），
        # 用来做“画面证据薄时的收口”，而不是替代画面判断。
        declared: list[str] = []
        try:
            declared = self.character_recognizer.names_in_text(f"{title_text} {tags_text}".strip(), limit=6)
        except Exception:
            declared = []
        cand = ""
        try:
            cand = self.character_recognizer.appearance_candidates(analysis.vision_text or "", limit=8)
        except Exception:
            cand = ""
        ref = (cand + ("\n\n【文本线索里出现过的角色（仅供参考，不得盲信）】" + "、".join(priors)) if priors else cand)
        # Both engines and metadata use exactly the same character identifiers.
        aliases = self.character_recognizer.aliases()
        alias_map = role_alias_map(aliases)
        declared = list(dict.fromkeys(n for label in declared if (n := canonical_role(label, alias_map))))
        priors = list(dict.fromkeys(n for label in priors if (n := canonical_role(label, alias_map))))
        evidence = {'visual_frames': [], 'local_frames': []}
        votes: dict[str, int] = {}
        high_votes: dict[str, int] = {}
        detail: list[tuple[str, str]] = []
        coobserved: set[tuple[str, str]] = set()
        visual_available = getattr(self.ai, "vision_available", lambda: False)()
        for img in (frames if visual_available else []):
            try:
                out = await self.ai.describe_images(
                    [img],
                    prompt=("识别图中每一个东方角色。同框人物分别判断；第一行格式：角色名｜高/中/低；第二角色名｜高/中/低。"
                            "看不清写不确定｜低。不能只认最显眼的一个。之后按左/右/上/下描述至少两项可见区分特征；"
                            "fumo重点对比帽子、发结/女仆头饰、衣装、身前眼球和管线。道具没出现不排除，不凭发色猜。"),
                    reference_text=ref,
                )
            except Exception:
                out = ""
            first = ""
            for ln in str(out or "").splitlines():
                if ln.strip():
                    first = ln.strip()
                    break
            frame_roles = parse_frame_roles(out, lambda name: _canonical_single_role(name, alias_map))
            evidence['visual_frames'].append({'image': str(img),
                'roles': [{'name': name, 'confidence': conf} for name, conf in frame_roles],
                'reason': 'scored_roles' if frame_roles else 'no_scored_role'})
            if len(frame_roles) > 1:
                coobserved.update(tuple(sorted((a, b))) for a, _ in frame_roles for b, _ in frame_roles if a != b)
            if not frame_roles:
                detail.append((first[:60] or "空", "低"))
            for canon, conf in frame_roles:
                detail.append((canon, conf))
                votes[canon] = votes.get(canon, 0) + 1
                if conf == "高":
                    high_votes[canon] = high_votes.get(canon, 0) + 1
        # Local WD has no paid API call; retain every returned candidate for audit.
        local_votes: dict[str, int] = {}
        local_candidates: list[str] = []
        local_frames = frames
        try:
            tagger = _wd_tagger_singleton()
            if tagger and tagger.available():
                min_score = float(cfg.get("wd_min_score", 0.6) or 0.6)
                # Keep the configured frame budget; a secondary candidate is not another vote.
                try:
                    local_frames = _select_full_span_images(
                        analysis.image_paths, int(cfg.get("character_wd_frames", 8) or 8), keep_leading=1)
                except Exception:
                    local_frames = frames
                for img in (local_frames or frames):
                    try:
                        hits = tagger.predict(img, topk=3)
                    except Exception as exc:
                        evidence['local_frames'].append({'image': str(img), 'candidates': [],
                            'reason': 'prediction_error', 'error': str(exc)[:100]})
                        continue
                    candidates = local_frame_candidates(hits, alias_map, min_score)
                    evidence['local_frames'].append({'image': str(img), 'candidates': candidates,
                        'reason': 'returned_candidates' if candidates else 'no_candidates'})
                    for candidate in candidates:
                        name, reason = candidate['name'], candidate['reason']
                        if reason in ('counted_primary', 'secondary_candidate') and name not in local_candidates:
                            local_candidates.append(name)
                        if reason == 'counted_primary':
                            local_votes[name] = local_votes.get(name, 0) + 1
                            detail.append((name, f"本地{candidate['score']:.4f}"))
        except Exception as exc:
            analysis.notes.append(f"本地打标投票跳过：{str(exc)[:60]}")

        def attach_evidence(identity):
            identity['policy'] = role_policy(cfg)
            identity['evidence'] = {**evidence, 'visual_votes': dict(votes), 'local_votes': dict(local_votes),
                'coobserved_pairs': [list(pair) for pair in sorted(coobserved)]}
            return identity

        combined: dict[str, int] = dict(votes)
        for k, v in local_votes.items():
            combined[k] = combined.get(k, 0) + v
        # 标题/标签里只点名了一个角色时，允许它作为“收口票”：
        # 画面/本地打标至少有一票指向同一角色 → 直接确认；完全没有票 → 给软结论（可提名、别描述外观）。
        declared_single = ""
        try:
            _uniq = []
            for _d in declared:
                _d = str(_d or "").strip()
                if _d and _d not in _uniq:
                    _uniq.append(_d)
            if len(_uniq) == 1:
                declared_single = _uniq[0]
        except Exception:
            declared_single = ""
        if not combined:
            identity = make_identity([], self.character_recognizer.aliases(),
                analysis.vision_text + "\n" + analysis.premium_vision_text,
                uncertain=list(dict.fromkeys(declared + local_candidates)), source="no-confirmed-visual-votes")
            attach_evidence(identity)
            apply_identity(analysis, identity)
            premium_name = _premium_confirmed_role(analysis.role_verify_text, alias_map)
            if premium_name and declared_single and premium_name != declared_single:
                analysis.premium_vision_text = (
                    "【角色识别冲突】标题/标签与复核结论不一致；评论不要断言角色身份。\n"
                    + (analysis.premium_vision_text or "")
                )
                analysis.vision_text = (
                    "【角色识别·未确认｜优先读这段】标题/标签与高精度复核对角色身份有分歧，"
                    "评论不要断言具体角色名。\n\n" + (analysis.vision_text or "")
                )
                analysis.notes.append(f"角色投票：标题/标签 {declared_single} 与复核 {premium_name} 冲突 → 未确认")
                return
            if declared_single:
                analysis.vision_text = (analysis.vision_text or "") + \
                    f"\n\n【角色识别】疑似{declared_single}（标题/标签点名，画面与本地打标均未确认；可以提到角色名，但不要描述其外观细节，也不要用‘确认/就是’这类断言）"
                analysis.notes.append(f"角色投票：画面无票，标题/标签点名 {declared_single} → 软结论（不断言外观）")
                self.logger(f"角色识别软结论（仅标题/标签点名）：{item.bvid} {declared_single}")
            else:
                analysis.notes.append("角色投票：全部未确认 → 评论避免断言角色名")
                analysis.vision_text = (analysis.vision_text or "") + "\n\n【角色识别】未确认（不要断言具体角色名）"
            return
        # ===== 定论层级（重要）=====
        # Preserve conservative evidence thresholds. Correlated WD frames must not
        # outvote a conflicting engine merely because more local frames were read.
        visual_top_name, visual_top_n = max(votes.items(), key=lambda kv: kv[1]) if votes else ("", 0)
        local_top_name, local_top_n = max(local_votes.items(), key=lambda kv: kv[1]) if local_votes else ("", 0)
        local_frames_n = max(1, len(local_frames or frames))
        best, best_src, best_n, conflict = "", "", 0, ""
        visual_ties = [name for name, count in votes.items() if count == visual_top_n]
        unresolved_tie = len(visual_ties) > 1 and any(
            tuple(sorted((a, b))) not in coobserved for a in visual_ties for b in visual_ties if a != b)
        if unresolved_tie:
            conflict = "视觉最高票并列且缺少共同出现证据，保留候选而不按插入顺序定论"
        elif visual_top_n >= 2:
            local_same = int(local_votes.get(visual_top_name, 0))
            # 本地是否构成"强对立"（≥3 票且过半）——多角色同框时两边会各认一个，不能简单压过去
            strong_local = bool(local_top_name) and local_top_name != visual_top_name \
                and local_top_n >= 3 and local_top_n * 2 > local_frames_n
            if not strong_local:
                best = visual_top_name
                best_n = visual_top_n + local_same
                best_src = f"视觉{visual_top_n}" + (f"+本地{local_same}" if local_same else "")
                if local_top_n and local_top_name != visual_top_name:
                    conflict = f"本地打标另有 {local_top_name}×{local_top_n}（未成强对立），以视觉为准"
            else:
                v_dec = visual_top_name in declared
                l_dec = local_top_name in declared
                if v_dec and l_dec and tuple(sorted((visual_top_name, local_top_name))) in coobserved:
                    # Metadata alone does not prove that two conflicting identities coexist.
                    best = f"{visual_top_name}、{local_top_name}"
                    best_n = visual_top_n + local_top_n
                    best_src = f"视觉{visual_top_n}({visual_top_name})/本地{local_top_n}({local_top_name})，标题标签两名都点名"
                elif l_dec and not v_dec:
                    best = local_top_name
                    best_n = local_top_n
                    best_src = f"本地{local_top_n}（标题/标签点名支持；视觉另有 {visual_top_name}×{visual_top_n}）"
                elif v_dec and not l_dec:
                    best = visual_top_name
                    best_n = visual_top_n
                    best_src = f"视觉{visual_top_n}（标题/标签点名支持；本地另有 {local_top_name}×{local_top_n}）"
                else:
                    # 两边都很强、互不相让，标题/标签又没点名 → 宁可不下结论
                    conflict = f"视觉{visual_top_name}×{visual_top_n} 与 本地{local_top_name}×{local_top_n} 强对立，标题/标签未点名 → 不下结论"
                    best = ""
        elif visual_top_name and visual_top_name == local_top_name and (visual_top_n + local_top_n) >= 2:
            # 视觉与本地两路引擎各自至少一票、且指向同一角色：互相独立的一致证据，足以确认
            best = visual_top_name
            best_n = visual_top_n + local_top_n
            best_src = f"视觉{visual_top_n}+本地{local_top_n}（两路独立一致）"
        elif not votes and local_top_n >= 3 and local_top_n * 2 > local_frames_n:
            best = local_top_name
            best_n = local_top_n
            best_src = f"本地{local_top_n}"
        elif declared_single and int(high_votes.get(declared_single, 0)) >= 1 \
                and visual_top_name in ("", declared_single) and local_top_name in ("", declared_single):
            _c = int(votes.get(declared_single, 0)) + int(local_votes.get(declared_single, 0))
            best = declared_single
            best_n = _c
            best_src = (f"视觉{int(votes.get(declared_single, 0))}" if votes.get(declared_single) else "") + \
                       (f"+本地{int(local_votes.get(declared_single, 0))}" if local_votes.get(declared_single) else "")
            best_src = f"标题/标签点名+{best_src.strip('+') or '0票'}"
        if best:
            best = "、".join(additional_roles(votes, high_votes, local_votes, declared, coobserved, best.split("、")))
        premium_name = _premium_confirmed_role(analysis.role_verify_text, alias_map)
        if best and premium_name and premium_name not in best.split("、"):
            # 付费复核也可能错，故不让它直接改判；两路强证据冲突时先撤销
            # 确认，避免把一个错误角色写进公开评论。
            conflict = f"逐帧/本地投票指向 {best}，高精度复核确认 {premium_name}，身份冲突"
            analysis.premium_vision_text = (
                "【角色识别冲突】逐帧投票与复核结论不一致；评论不要断言角色身份。\n"
                + (analysis.premium_vision_text or "")
            )
            best = ""
        confirmed = []
        if best:
            confirmed = additional_roles(votes, high_votes, local_votes, declared, coobserved, best.split("、"))
            best = "、".join(confirmed)
            if len(confirmed) > 1:
                best_src = "；".join(f"{name}:视觉{votes.get(name, 0)}+本地{local_votes.get(name, 0)}" for name in confirmed)
                best_n = sum(votes.get(name, 0) + local_votes.get(name, 0) for name in confirmed)
        identity = make_identity(confirmed, self.character_recognizer.aliases(),
            analysis.vision_text + "\n" + analysis.premium_vision_text,
            uncertain=list(dict.fromkeys(declared + list(votes) + local_candidates + ([premium_name] if premium_name else []))),
            source="multi-frame-and-local-votes")
        attach_evidence(identity)
        apply_identity(analysis, identity)
        if best:
            visual_hit = int(votes.get(best, 0))
            local_hit = int(local_votes.get(best, 0))
            analysis.vision_text = (analysis.vision_text or "") + \
                f"\n\n【角色识别】{best}（{best_src}，共 {best_n} 票）"
            analysis.notes.append(f"角色投票：{best}（{best_src}，共 {best_n} 票{('；' + conflict) if conflict else ''}；明细 {detail}）")
            self.logger(f"角色识别确认：{item.bvid} {best}（{best_src}，共 {best_n} 票）{('｜' + conflict) if conflict else ''}")
        else:
            visual_hit = int(votes.get(visual_top_name, 0))
            local_hit = int(local_votes.get(local_top_name, 0))
            extra = conflict or (f"标题/标签点名 {declared_single}，画面 {visual_top_name or '无'}，冲突不确认" if declared_single else "")
            analysis.notes.append(f"角色投票分歧/证据不足 视觉{votes} 本地{local_votes} → 评论避免断言角色名{'（' + extra + '）' if extra else ''}")
            # 案例 BV1iYYZ6rEpY（2026-09-13）：原来这里只加一句笼统的"多帧结果不一致"，
            # 而且贴在 700+ 字画面描述的**末尾**；前面"KFC联名主视觉角色：博丽灵梦"这种
            # 自信表述被生成器直接抄进了评论（实际是幽幽子）。
            # 改为：点名冲突候选 + 放在画面证据的**最前面**，让生成器先看到"未确认"。
            _cand = "、".join(dict.fromkeys([n for n in (visual_top_name, local_top_name, premium_name) if n])) or "无"
            _declared_txt = "、".join(dict.fromkeys([str(d) for d in declared if str(d).strip()])) or "无"
            _warn = (
                "【角色识别·未确认｜优先读这段】多路角色证据分歧，无法确认图中的东方角色。"
                f"冲突候选：{_cand}；标题/标签点名：{_declared_txt}。"
                "下面这段画面描述里出现的角色名**都可能是错的**（常见错误：把一只角色写成另一只）。"
                "评论中不要断言任何具体角色名；若确实要提到角色，只能用标题/标签点名的名字，"
                "并且必须带『像是/疑似/大概是』这类不确定说法。宁可不提角色，也不要写错名字。\n\n"
            )
            analysis.vision_text = _warn + (analysis.vision_text or "")
            self.logger(f"角色识别未确认：{item.bvid} 视觉{votes} 本地{local_votes} {extra}", "WARN")
        best = best or visual_top_name or local_top_name
        # 纠错留档：与标题/标签点名的角色不一致时记录，便于回看与补样本
        try:
            if priors and best not in priors:
                import json as _json
                import time as _time
                with open("data/vision_corrections.jsonl", "a", encoding="utf-8") as f:
                    f.write(_json.dumps({
                        "bvid": getattr(item, "bvid", ""), "title": str(item.title or "")[:80],
                        "prior": priors, "declared": declared, "visual": best,
                        "visual_votes": visual_hit, "local_votes": local_hit,
                        "ts": _time.strftime("%Y-%m-%d %H:%M:%S"),
                    }, ensure_ascii=False) + "\n")
        except Exception:
            pass

    async def _run_character_premium_verify(self, item: VideoItem, cfg: dict[str, Any], analysis: MediaAnalysis, *, character_ref: str) -> None:
        """用强视觉模型做少量精选帧角色复核；失败不影响主流程。"""
        recog_cfg = cfg.get("character_recognition") or {}
        if not (analysis.image_paths and recog_cfg.get("premium_verify_enabled", False)):
            return
        if recog_cfg.get("premium_verify_high_risk_only", True):
            high_risk, risk_reason = _character_verify_high_risk(item, analysis)
            if not high_risk:
                analysis.notes.append("高精度角色复核跳过：普通低风险视频")
                self.logger(f"高精度角色复核跳过 {item.bvid}：普通低风险视频")
                return
            analysis.notes.append(f"高精度角色复核触发：{risk_reason}")
            self.logger(f"高精度角色复核触发 {item.bvid}：{risk_reason}")
        try:
            max_verify = max(1, int(recog_cfg.get("premium_verify_max_images", 4) or 4))
            role_names = self.character_recognizer.names_in_text(
                str(item.title or "") + " " + " ".join(item.tags or []), limit=6)
            sel = verification_images(analysis.image_paths, max_verify,
                self.cache_dir / str(item.bvid) / "role_crops", multi_person=len(role_names) > 1)
            verify = await self.ai.describe_images_via_fallback_vision(
                sel,
                prompt=(
                    "请专门做东方Project角色识别复核。输入含原图和局部裁剪；裁剪不是新增人物，必须与原图对应。"
                    "同框按位置分别核对人物，不因另一人物遮挡而乱猜。第一行严格写一种："
                    "复核结论：确认｜单个角色规范中文名；复核结论：疑似｜角色名；"
                    "复核结论：不确定｜无。多角色同框或角色身份冲突时写不确定。"
                    "只有至少两项互相独立、实际可见的强特征吻合才能写确认；"
                    "标题、标签、画面文字和相似样本不能单独支撑确认。\n"
                    "之后逐张说明可见的发色、帽子/头饰、服装、道具、翅膀/尾巴/第三只眼等特征，"
                    "并指出与易混角色的区别。非东方角色或只是MMD模型相似时明确说不确定。"
                    "不要只凭发色、帽子或裙色下结论。输出中文，300字内。"
                ),
                reference_text=character_ref,
                max_tokens=int(recog_cfg.get("premium_verify_max_tokens", 500) or 500),
                max_images=max_verify,
                model=self.ai.step_model(
                    "role_vision",
                    default=str(recog_cfg.get("premium_verify_model")
                                or getattr(self.ai, "role_vision_model", "") or ""),
                ) or None,
            )
            if verify:
                analysis.role_verify_text = verify
                analysis.premium_vision_text = (analysis.premium_vision_text + "\n\n" if analysis.premium_vision_text else "") + "【高精度角色复核】\n" + verify
                analysis.notes.append(f"高精度角色复核：{len(verify)} 字")
                self.logger(f"高精度角色复核完成：{item.bvid}，{len(verify)} 字")
        except Exception as exc:
            analysis.notes.append(f"高精度角色复核失败：{str(exc)[:100]}")
            self.logger(f"高精度角色复核失败 {item.bvid}：{str(exc)[:120]}", "WARN")

    async def _try_music_fingerprint(self, item: VideoItem, cfg: dict[str, Any], analysis: MediaAnalysis) -> None:
        """无有效人声（纯BGM/游戏演奏/转写乱码）且有音频文件时，用 ACRCloud 识别曲名；任何失败都静默不影响主流程。"""
        try:
            if (analysis.music_identify_text or "").strip():
                return
            fp = cfg.get("audio_fingerprint") or {}
            if not fp.get("enabled", True):
                return
            # 有明确语音内容时无需指纹（省 ACR 免费档额度）；只有无人声或转写极短
            # （纯音乐/游戏BGM/演奏，whisper 常出乱码或空）才识别——此时曲名本身就是
            # 最有价值的证据，能补上评论里真正可写的素材。
            speech_len = len((analysis.audio_text or "").strip())
            if speech_len >= int(fp.get("max_speech_chars", 30) or 30):
                return
            wav = self.cache_dir / item.bvid / "audio.wav"
            if not wav.exists() or wav.stat().st_size <= 0:
                return
            res = await asyncio.to_thread(_acr_identify_blocking, str(wav), cfg, _ACR_TMP_DIR,
                                          self.logger)
            if res:
                analysis.music_identify_text = res
                analysis.notes.append("音乐识别：" + res[:80])
                self.logger(f"音乐识别完成：{item.bvid}：{res[:100]}")
        except Exception as exc:
            analysis.notes.append(f"音乐识别跳过：{str(exc)[:100]}")

    async def _bilidown_json(self, cfg: dict[str, Any], method: str, endpoint: str,
                             payload: Any = None, timeout: float = 15.0) -> dict[str, Any]:
        """Call Bilidown's loopback API. This never opens its web page or a browser."""
        import httpx

        bcfg = cfg.get("bilidown") or {}
        base = str(bcfg.get("api_base") or "http://127.0.0.1:8098").rstrip("/")
        if not re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):\d{2,5}", base, re.IGNORECASE):
            raise RuntimeError("Bilidown API 必须是本机 127.0.0.1/localhost 地址")
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            if method.upper() == "POST":
                response = await client.post(base + endpoint, json=payload)
            else:
                response = await client.get(base + endpoint)
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict) or not data.get("success", False):
            message = data.get("message") if isinstance(data, dict) else "返回格式错误"
            raise RuntimeError(f"Bilidown 接口失败：{message}")
        return data

    async def _ensure_bilidown_api(self, cfg: dict[str, Any]) -> bool:
        bcfg = cfg.get("bilidown") or {}
        if not bcfg.get("enabled", False):
            return False
        try:
            await self._bilidown_json(cfg, "GET", "/api/checkLogin", timeout=4)
            return True
        except Exception:
            pass
        if not bcfg.get("auto_start", True):
            return False
        exe = Path(os.path.expandvars(str(bcfg.get("exe_path") or "")))
        if not exe.is_file():
            self.logger(f"Bilidown 不存在：{exe}", "WARN")
            return False

        # 多个机器人进程可能同时需要媒体分析。用原子锁确保只启动一个 Bilidown；
        # 其余进程只等待同一个 8098 本地接口上线。
        lock = self.cache_dir.parent / "_bilidown_start.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        have_lock = False
        try:
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                have_lock = True
            except FileExistsError:
                try:
                    if __import__("time").time() - lock.stat().st_mtime > 90:
                        lock.unlink(missing_ok=True)
                except OSError:
                    pass
            if have_lock:
                kwargs = _no_window_kwargs()
                proc = await asyncio.create_subprocess_exec(
                    str(exe), cwd=str(exe.parent),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs
                )
                # Keep a handle so Python does not warn about an uncollected child. Bilidown is
                # a shared tray/background service and intentionally outlives one analysis call.
                self._bilidown_process = proc
            deadline = __import__("time").monotonic() + max(
                5, int(bcfg.get("startup_timeout_seconds", 25) or 25)
            )
            while __import__("time").monotonic() < deadline:
                try:
                    await self._bilidown_json(cfg, "GET", "/api/checkLogin", timeout=3)
                    return True
                except Exception:
                    await asyncio.sleep(0.5)
            return False
        finally:
            if have_lock:
                try:
                    lock.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _choose_bilidown_streams(play: dict[str, Any], max_quality: int = 16,
                                 preferred_codec: int = 7) -> dict[str, Any]:
        dash = play.get("dash") or {}
        videos = list(dash.get("video") or [])
        if not videos:
            raise RuntimeError("Bilidown 没有返回视频流")
        qualities = sorted({int(v.get("id") or 0) for v in videos if int(v.get("id") or 0) > 0}, reverse=True)
        quality = next((q for q in qualities if q <= max_quality), qualities[-1])
        codec_order = []
        for codec in (preferred_codec, 7, 12, 13):
            if codec not in codec_order:
                codec_order.append(codec)
        video = None
        for codec in codec_order:
            video = next((v for v in videos if int(v.get("id") or 0) == quality
                          and int(v.get("codecid") or 0) == codec), None)
            if video:
                break
        if not video:
            video = next(v for v in videos if int(v.get("id") or 0) == quality)
        audios = list(dash.get("audio") or [])
        audio = max(audios, key=lambda a: int(a.get("id") or 0)) if audios else None
        video_url = str(video.get("baseUrl") or video.get("base_url") or "")
        audio_url = str((audio or {}).get("baseUrl") or (audio or {}).get("base_url") or "")
        if not video_url or not audio_url:
            raise RuntimeError("Bilidown 音视频地址不完整")
        return {
            "format": quality,
            "video": video_url,
            "audio": audio_url,
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
            "duration": int(dash.get("duration") or 0),
        }

    @staticmethod
    def _bilidown_task_path(task: dict[str, Any]) -> Path:
        task_id = str(task.get("id") or "")
        token = base64.b64encode(task_id.encode("utf-8")).decode("ascii").rstrip("=")
        suffix = ".m4a" if task.get("downloadType") == "audio" else ".mp4"
        return Path(str(task.get("folder") or "")) / f"{task.get('title') or ''} {token}{suffix}"

    async def _bilidown_tasks_for(self, item: VideoItem, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        """Bilidown 页码从 0 开始；跨页寻找原任务，防止漏掉新任务或重复下载。"""
        seen = set()
        for page in range(100):
            result = await self._bilidown_json(
                cfg, "GET", f"/api/getTaskList?page={page}&pageSize=200", timeout=10
            )
            batch = list(result.get("data") or [])
            matches = [t for t in batch if str(t.get("bvid") or "") == str(item.bvid)
                       and int(t.get("cid") or 0) == int(item.cid or 0)
                       and str(t.get("downloadType") or "merge") == "merge"]
            if matches:
                return matches
            ids = tuple(str(t.get("id") or "") for t in batch)
            if batch and ids in seen:
                raise RuntimeError("Bilidown任务列表分页重复，停止新建下载以防重复")
            seen.add(ids)
            if len(batch) < 200:
                return []
        raise RuntimeError("Bilidown任务列表超过查询上限，停止新建下载以防重复")

    async def _download_with_bilidown(self, item: VideoItem, cfg: dict[str, Any],
                                      analysis: MediaAnalysis) -> str:
        """Second source: submit one merged low-resolution job through Bilidown's local API."""
        bcfg = cfg.get("bilidown") or {}
        if not bcfg.get("enabled", False):
            return ""
        task_committed = False
        try:
            if not await self._ensure_bilidown_api(cfg):
                raise RuntimeError("后台接口没有启动")

            # Reuse a queued/running/completed task for the same BV/CID before creating one.
            tasks = await self._bilidown_tasks_for(item, cfg)
            matches = [t for t in tasks if str(t.get("bvid") or "") == str(item.bvid)
                       and int(t.get("cid") or 0) == int(item.cid or 0)
                       and str(t.get("downloadType") or "merge") == "merge"
                       and str(t.get("status") or "") in {"waiting", "running", "done"}]
            task = max(matches, key=lambda t: int(t.get("id") or 0)) if matches else None
            if task and str(task.get("status")) == "done":
                path = self._bilidown_task_path(task)
                if path.is_file() and path.stat().st_size > 0:
                    from .bilidown_cleanup import OwnedDownloads
                    OwnedDownloads(self.cache_dir, cfg).completed(task, path)
                    analysis.notes.append("复用Bilidown已完成任务")
                    return str(path.resolve())
                # Bilidown 会保留完成记录；源文件被清理后要允许重新建立任务。
                task = None
            elif task is not None:
                task_committed = True

            if task is None:
                from urllib.parse import quote
                play_res = await self._bilidown_json(
                    cfg, "GET", f"/api/getPlayInfo?bvid={quote(str(item.bvid))}&cid={int(item.cid or 0)}",
                    timeout=20,
                )
                streams = self._choose_bilidown_streams(
                    dict(play_res.get("data") or {}),
                    max_quality=int(bcfg.get("max_quality", 16) or 16),
                    preferred_codec=int(bcfg.get("preferred_codec", 7) or 7),
                )
                title = f"Robot_{item.bvid}_{int(item.cid or 0)}"
                payload = [{
                    "bvid": item.bvid,
                    "cid": int(item.cid or 0),
                    "cover": str(item.pic or ""),
                    "title": title,
                    "format": streams["format"],
                    "owner": str(item.author or ""),
                    "audio": streams["audio"],
                    "duration": streams["duration"] or int(item.duration or 0),
                    "downloadType": "merge",
                    "video": streams["video"],
                    "width": streams["width"],
                    "height": streams["height"],
                }]
                await self._bilidown_json(cfg, "POST", "/api/createTask", payload=payload, timeout=20)
                task_committed = True
                from .bilidown_cleanup import OwnedDownloads
                OwnedDownloads(self.cache_dir, cfg).submitted(item.bvid, item.cid, title)
                analysis.notes.append("已提交Bilidown后台下载（不打开网页）")

            deadline = __import__("time").monotonic() + max(
                60, int(bcfg.get("download_timeout_seconds", 480) or 480)
            )
            poll = max(1.0, float(bcfg.get("poll_interval_seconds", 2) or 2))
            while __import__("time").monotonic() < deadline:
                tasks = await self._bilidown_tasks_for(item, cfg)
                matches = [t for t in tasks
                           if str(t.get("bvid") or "") == str(item.bvid)
                           and int(t.get("cid") or 0) == int(item.cid or 0)
                           and str(t.get("downloadType") or "merge") == "merge"]
                if matches:
                    task = max(matches, key=lambda t: int(t.get("id") or 0))
                    status = str(task.get("status") or "")
                    if status == "error":
                        task_committed = False
                        raise RuntimeError("下载任务失败")
                    if status == "done":
                        path = self._bilidown_task_path(task)
                        if path.is_file() and path.stat().st_size > 0:
                            from .bilidown_cleanup import OwnedDownloads
                            OwnedDownloads(self.cache_dir, cfg).completed(task, path)
                            analysis.notes.append("Bilidown后台下载完成")
                            self.logger(f"Bilidown下载完成：{item.bvid} → {path}")
                            return str(path.resolve())
                await asyncio.sleep(poll)
            raise _BilidownPending("Bilidown任务仍在排队或下载，下一轮复用，避免重复下载")
        except asyncio.CancelledError:
            # Bilidown 会在后台继续任务；下一轮会直接复用完成文件。
            raise
        except _BilidownPending as exc:
            analysis.notes.append(str(exc))
            self.logger(f"Bilidown任务待完成 {item.bvid}：{exc}", "INFO")
            raise
        except Exception as exc:
            if task_committed:
                pending = _BilidownPending(
                    f"Bilidown任务已提交但暂时无法读取状态，下一轮复用：{str(exc)[:100]}"
                )
                analysis.notes.append(str(pending))
                self.logger(f"Bilidown任务待完成 {item.bvid}：{pending}", "INFO")
                raise pending from exc
            analysis.notes.append(f"Bilidown下载失败，回退内置下载：{str(exc)[:120]}")
            self.logger(f"Bilidown下载失败 {item.bvid}：{str(exc)[:160]}", "WARN")
            return ""

    async def _download_video(self, item: VideoItem, cfg: dict[str, Any], analysis: MediaAnalysis) -> str:
        yt = _find_tool("yt-dlp", str(cfg.get("yt_dlp_path") or ""))
        if not yt:
            analysis.notes.append("未安装 yt-dlp，跳过视频下载、关键帧和音频分析")
            return ""
        out_tpl = self.cache_dir / item.bvid / "video.%(ext)s"
        out_tpl.parent.mkdir(parents=True, exist_ok=True)
        existing = list(out_tpl.parent.glob("video.*"))
        if existing:
            return str(existing[0])
        max_height = int(cfg.get("download_max_height", 480) or 480)
        video_only = bool(cfg.get("video_only_download", True))
        cmd = [
            yt,
            "--no-playlist",
            "--proxy", "",
            "--add-header", "Referer:https://www.bilibili.com/",
            "--add-header", "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        ]
        cmd += _yt_cookie_args()
        cmd += ['--max-filesize', str(_media_byte_limit(cfg, 'download_max_bytes', 1024 ** 3))]
        cmd += ["--retries", "3", "--fragment-retries", "3"]
        ffmpeg_bin = _find_tool("ffmpeg", str(cfg.get("ffmpeg_path") or ""))
        if ffmpeg_bin:
            cmd += ["--ffmpeg-location", str(Path(ffmpeg_bin).parent)]
        if video_only:
            # 只下载低清视频流、不合并音频，下载量大幅减少；b 作最后回退（任意最佳流）
            cmd += ["-f", f"bv[height<={max_height}]/b[height<={max_height}]/bv/b"]
        else:
            cmd += [
                "-f", f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/bv[height<={max_height}]/b[height<={max_height}]/best",
                "--merge-output-format", "mp4",
            ]
        cmd += ["-o", str(out_tpl), item.url]
        try:
            await _run_command(cmd, timeout=int(cfg.get("download_timeout_seconds", 600) or 600),
                               output_dir=out_tpl.parent, resource_config=cfg)
        except Exception as exc:
            analysis.notes.append(f"视频下载失败：{exc}")
            self.logger(f"视频下载失败 {item.bvid}：{exc}", "WARN")
            return ""
        existing = list(out_tpl.parent.glob("video.*"))
        return str(existing[0]) if existing else ""

    async def _extract_keyframes(self, video_path: str, item: VideoItem, cfg: dict[str, Any], analysis: MediaAnalysis) -> list[str]:
        ffmpeg_bin = _find_tool("ffmpeg", str(cfg.get("ffmpeg_path") or ""))
        if not ffmpeg_bin:
            analysis.notes.append("未安装 ffmpeg，跳过关键帧提取")
            return []
        frame_dir = self.cache_dir / item.bvid / "frames"
        frame_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(frame_dir.glob("frame_*.jpg"))
        if existing:
            return [str(p) for p in existing]
        max_frames = max(1, int(cfg.get("max_keyframes", 4) or 4))
        mode = str(cfg.get("keyframe_mode") or "random").lower()
        if mode == "random":
            # 随机平均选时间点，每个点只抽一帧，避免全片扫描。
            duration = await asyncio.to_thread(_probe_duration_seconds, ffmpeg_bin, video_path)
            if duration and duration > 2:
                count = max(1, min(max_frames, int(duration // 5) or 1))
                # 均匀分散 + 随机扰动
                raw = [i * duration / (count + 1) for i in range(1, count + 1)]
                times = sorted(max(1, min(int(duration) - 1, int(t + random.uniform(-duration / (count + 1) / 2, duration / (count + 1) / 2)))) for t in raw)
                frames: list[str] = []
                for i, t in enumerate(times, 1):
                    out = frame_dir / f"frame_{i:03d}.jpg"
                    cmd = [ffmpeg_bin, "-y", "-ss", str(t), "-i", video_path, "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", str(out)]
                    try:
                        await _run_command(cmd, timeout=int(cfg.get("ffmpeg_timeout_seconds", 120) or 120),
                                           output_dir=self.cache_dir / item.bvid, resource_config=cfg)
                        if out.exists():
                            frames.append(str(out))
                    except Exception as exc:
                        analysis.notes.append(f"关键帧抽取失败 t={t}s：{exc}")
                return frames
            analysis.notes.append("无法探测视频时长，回退间隔抽帧")
        interval = max(5, int(cfg.get("keyframe_interval_seconds", 45) or 45))
        vf = f"fps=1/{interval},scale='min(1280,iw)':-2"
        cmd = [ffmpeg_bin, "-y", "-i", video_path, "-vf", vf, "-frames:v", str(max_frames), str(frame_dir / "frame_%03d.jpg")]
        try:
            await _run_command(cmd, timeout=int(cfg.get("ffmpeg_timeout_seconds", 180) or 180),
                               output_dir=self.cache_dir / item.bvid, resource_config=cfg)
        except Exception as exc:
            analysis.notes.append(f"关键帧提取失败：{exc}")
            return []
        return [str(p) for p in sorted(frame_dir.glob("frame_*.jpg"))]

    async def _download_sections_and_extract(self, item: VideoItem, cfg: dict[str, Any], analysis: MediaAnalysis) -> list[str]:
        """Fast path: download only tiny segments around random timestamps, then grab one frame each.

        Avoids downloading the full video; each segment is a few seconds (~100-300KB).
        """
        import time as _t
        # 风控退避中：刚被 B站 412/人机验证拦截，短时间不再发任何分段下载请求，
        # 直接放弃本视频（上层跳过本轮，退避结束后下轮自然重试）。
        if _t.time() < float(getattr(self, "_risk_blocked_until", 0.0) or 0.0):
            analysis.notes.append("风控退避中，跳过本视频分段抽帧")
            raise _RiskControlBlocked("risk control backoff")
        yt = _find_tool("yt-dlp", str(cfg.get("yt_dlp_path") or ""))
        if not yt:
            return []
        ffmpeg_bin = _find_tool("ffmpeg", str(cfg.get("ffmpeg_path") or ""))
        if not ffmpeg_bin:
            return []

        # 1) Probe remote duration without downloading.
        duration = await asyncio.to_thread(_probe_remote_duration_seconds, yt, item.url)
        if not duration or duration <= 2:
            analysis.notes.append("无法探测远程视频时长，改用完整下载")
            return []

        max_frames = max(1, int(cfg.get("max_keyframes", 3) or 3))
        seg_len = max(3, int(cfg.get("segment_length_seconds", 4) or 4))
        # 短视频不能按长片段切：seg_len 接近或超过视频长度时，所有时间点都会被
        # 夹到同一处，最后只剩一个片段。按视频长度收一下上限。
        if duration and duration > 0:
            seg_len = max(3, min(seg_len, max(3, int(duration // 3) or 3)))
        # 网络请求数（分段下载次数）与目标帧数**解耦**。
        # 每一次分段下载都是一次可能被 B站 412/人机验证拦截的请求；而"同一个 4 秒片段
        # 本地用 ffmpeg 多抽几帧"是零网络成本的。所以想加帧要走 segment_frames_per_segment，
        # 而不是加大分段下载次数——后者只会线性抬高风控与下载失败的概率。
        # 默认 max_segment_downloads = max_keyframes，保持旧行为不变。
        max_dl = max(1, int(cfg.get("max_segment_downloads", max_frames) or max_frames))
        count = max(1, min(max_dl, int(duration // 5) or 1))
        # 每段抽几帧：刚好凑够目标帧数即可，最多 4 帧（再多只是同片段里的重复画面）。
        per_seg = max(1, min(int(cfg.get("segment_frames_per_segment", 1) or 1),
                             (max_frames + count - 1) // count, 4))
        # 均匀分散 + 随机扰动，覆盖整片。
        raw = [i * duration / (count + 1) for i in range(1, count + 1)]
        times = []
        for t in raw:
            jitter = duration / (count + 1) / 2
            t2 = t + random.uniform(-jitter, jitter)
            t2 = max(1.0, min(duration - seg_len - 1, t2))
            times.append(int(t2))
        times = sorted(set(times))

        seg_dir = self.cache_dir / item.bvid / "segs"
        seg_dir.mkdir(parents=True, exist_ok=True)
        max_height = int(cfg.get("download_max_height", 360) or 360)
        frames: list[str] = []
        frame_dir = self.cache_dir / item.bvid / "frames"
        frame_dir.mkdir(parents=True, exist_ok=True)

        for i, t in enumerate(times, 1):
            out_tpl = seg_dir / f"seg_{i}.%(ext)s"
            # 清理旧的同前缀文件，避免 glob 到旧片段。
            for old in seg_dir.glob(f"seg_{i}.*"):
                try:
                    old.unlink()
                except Exception:
                    pass
            cmd = [
                yt, "--no-playlist", "--proxy", "",
                "--ffmpeg-location", str(Path(ffmpeg_bin).parent),
                "--download-sections", f"*{t}-{t + seg_len}",
                "--force-keyframes-at-cuts",
                "-f", f"bv[height<={max_height}]/b[height<={max_height}]/bv/b",
                "-o", str(out_tpl),
                item.url,
            ]
            cmd += _yt_cookie_args()
            cmd += ['--max-filesize', str(_media_byte_limit(cfg, 'download_max_bytes', 1024 ** 3))]
            cmd += ["--retries", "3", "--fragment-retries", "3"]
            # 412/风控分段下载失败常见于短时间密集请求：最多 3 次，带随机退避；
            # 连续多段被风控拦则快速失败（不再把 60 段 × 3 次重试全部耗完）。
            seg_ok = False
            risk_streak = 0
            for _attempt in range(3):
                try:
                    await _run_command(cmd, timeout=int(cfg.get("segment_download_timeout_seconds", 180) or 180),
                                       output_dir=self.cache_dir / item.bvid, resource_config=cfg)
                    seg_ok = True
                    break
                except Exception as exc:
                    if _attempt >= 2:
                        analysis.notes.append(f"分段下载失败 t={t}s：{exc}")
                        if _is_risk_block_error(str(exc)):
                            risk_streak += 1
                            if risk_streak >= max(2, int(cfg.get("risk_fail_fast_segments", 2) or 2)):
                                backoff = max(120, int(cfg.get("risk_backoff_seconds", 300) or 300))
                                self._risk_blocked_until = _t.time() + backoff
                                analysis.notes.append("连续多段被B站风控拦截，放弃分段抽帧并进入退避")
                                self.logger(
                                    f"连续 {risk_streak} 段被B站风控拦截，退避 {backoff // 60} 分钟，跳过 {item.bvid}",
                                    "WARN",
                                )
                                raise _RiskControlBlocked("risk control: consecutive blocked segments")
                        else:
                            risk_streak = 0
                    else:
                        self.logger(f"分段下载重试 t={t}s（{_attempt + 1}/3）：{str(exc)[:120]}", "WARN")
                    await asyncio.sleep(random.uniform(6, 14))
            if not seg_ok:
                continue
            seg_files = list(seg_dir.glob(f"seg_{i}.*"))
            if not seg_files:
                continue
            seg_path = seg_files[0]
            # 清掉本段上一轮留下的帧，避免新旧混在一起（一个片段现在可能抽多帧）。
            for old in frame_dir.glob(f"frame_{i:03d}*.jpg"):
                try:
                    old.unlink()
                except Exception:
                    pass
            if per_seg <= 1:
                ff_cmd = [ffmpeg_bin, "-y", "-i", str(seg_path), "-frames:v", "1",
                          "-vf", "scale='min(1280,iw)':-2",
                          str(frame_dir / f"frame_{i:03d}.jpg")]
            else:
                # 一个片段内按 fps 均匀取 per_seg 帧，文件名加 _01/_02 后缀保持时间顺序。
                ff_cmd = [ffmpeg_bin, "-y", "-i", str(seg_path),
                          "-vf", f"fps={per_seg / seg_len:.4f},scale='min(1280,iw)':-2",
                          "-frames:v", str(per_seg),
                          str(frame_dir / f"frame_{i:03d}_%02d.jpg")]
            try:
                await _run_command(ff_cmd, timeout=int(cfg.get("ffmpeg_timeout_seconds", 60) or 60),
                                   output_dir=self.cache_dir / item.bvid, resource_config=cfg)
                made = sorted(frame_dir.glob(f"frame_{i:03d}*.jpg"))
                frames.extend(str(p) for p in made)
            except Exception as exc:
                analysis.notes.append(f"片段抽帧失败 t={t}s：{exc}")
            # 段间留 3~7 秒随机间隔，降低 412/风控触发概率。
            await asyncio.sleep(random.uniform(3, 7))
        # 多抽的帧按目标帧数均匀抽稀（保持时间顺序），避免帧池无意义地膨胀。
        if len(frames) > max_frames:
            _before = len(frames)
            frames = _pick_evenly_spaced(frames, max_frames)
            analysis.notes.append(f"关键帧 {_before} 张按目标 {max_frames} 张抽稀")
        if frames:
            self.logger(f"分段抽帧完成：{item.bvid}，{len(frames)} 帧"
                        f"（{len(times)} 次分段下载 × 每段最多 {per_seg} 帧，每段约 {seg_len}s）")
        return frames

    async def _download_audio_and_transcribe(self, client: BiliClient, item: VideoItem, cfg: dict[str, Any], analysis: MediaAnalysis) -> str:
        """Download audio-only stream and transcribe, without full video download.

        优先 DASH 直链（参考 astrbot_plugin_parser）：用 bilibili-api 直接拿最高质量
        音频流 URL，yt-dlp 只负责下载该直链（跳过整站解析，更快更稳）；
        直链失败再回退 yt-dlp 整站解析。
        """
        yt = _find_tool("yt-dlp", str(cfg.get("yt_dlp_path") or ""))
        if not yt:
            analysis.notes.append("未安装 yt-dlp，跳过音频下载")
            return ""
        ffmpeg_bin = _find_tool("ffmpeg", str(cfg.get("ffmpeg_path") or ""))
        if not ffmpeg_bin:
            analysis.notes.append("未安装 ffmpeg，跳过音频提取")
            return ""

        # 已经有足够长的字幕时不再跑语音转写。三条理由：
        #  1) 字幕本身就是逐句文本，重复跑 Whisper 只是把同一内容再算一遍；
        #  2) 这是整条分析链最贵的一步——要下整段音频 + 在 CPU 上跑 small 模型，
        #     20 分钟的视频要几分钟，实测它会把 analysis_total_timeout_seconds
        #     吃光，导致整个视频进入降级再被跳过（一个评论都发不出）；
        #  3) 少一次整段音频下载，对 B站 的请求也更轻。
        # 阈值可调；没有字幕或字幕过短的视频照旧走语音转写。
        min_sub = int(cfg.get("skip_asr_min_subtitle_chars", 0) or 0)
        if min_sub and len(str(analysis.subtitle_text or "").strip()) >= min_sub:
            analysis.notes.append(
                f"已有字幕 {len(analysis.subtitle_text)} 字（≥{min_sub}），跳过音频转写"
            )
            self.logger(f"已有字幕，跳过音频转写：{item.bvid}（{len(analysis.subtitle_text)} 字）")
            return ""

        audio_dir = self.cache_dir / item.bvid
        audio_dir.mkdir(parents=True, exist_ok=True)
        wav = audio_dir / "audio.wav"
        if wav.exists():
            return await asyncio.to_thread(_transcribe_faster_whisper, str(wav), str(cfg.get("asr_model") or "small"))
        # 清理上次中断留下的半成品（.part/.ytdl），避免污染本次下载。
        try:
            for stale in audio_dir.glob("audio.*"):
                if stale.suffix.lower() in (".part", ".ytdl", ".tmp"):
                    stale.unlink(missing_ok=True)
        except Exception:
            pass

        target_url = item.url
        use_dash = False
        if cfg.get("use_dash_audio", True):
            try:
                dash_url = await client.get_dash_audio_url(item)
                if dash_url:
                    target_url = dash_url
                    use_dash = True
            except Exception as exc:
                self.logger(f"DASH音频直链获取失败 {item.bvid}：{exc}", "WARN")

        out_tpl = audio_dir / "audio.%(ext)s"
        ffmpeg_location = str(Path(ffmpeg_bin).parent)
        cmd = [
            yt, "--no-playlist", "--proxy", "",
            '--max-filesize', str(_media_byte_limit(cfg, 'download_max_bytes', 1024 ** 3)),
            "--ffmpeg-location", ffmpeg_location,
            "--add-header", "Referer:https://www.bilibili.com/",
            "--add-header", "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "-f", "bestaudio/best",
            "-x", "--audio-format", "wav",
            "--audio-quality", "0",
            "-o", str(out_tpl),
            target_url,
        ]
        if use_dash:
            analysis.notes.append("使用DASH音频直链")
        try:
            await _run_command(cmd, timeout=int(cfg.get("audio_download_timeout_seconds", 600) or 600),
                               output_dir=audio_dir, resource_config=cfg)
        except Exception as exc:
            # DASH 直链失败 → 回退整站解析（老路径）
            if use_dash:
                analysis.notes.append(f"DASH音频直链下载失败，回退整站解析：{exc}")
                self.logger(f"DASH音频下载失败 {item.bvid}，回退 yt-dlp：{exc}", "WARN")
                cmd = [
                    yt, "--no-playlist", "--proxy", "",
                    '--max-filesize', str(_media_byte_limit(cfg, 'download_max_bytes', 1024 ** 3)),
                    "--ffmpeg-location", ffmpeg_location,
                    "--add-header", "Referer:https://www.bilibili.com/",
                    "--add-header", "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                    "-f", "bestaudio/best",
                    "-x", "--audio-format", "wav",
                    "--audio-quality", "0",
                    "-o", str(out_tpl),
                    item.url,
                ]
                try:
                    await _run_command(cmd, timeout=int(cfg.get("audio_download_timeout_seconds", 600) or 600),
                                       output_dir=audio_dir, resource_config=cfg)
                except Exception as exc2:
                    analysis.notes.append(f"音频下载失败：{exc2}")
                    self.logger(f"音频下载失败 {item.bvid}：{exc2}", "WARN")
                    return ""
            else:
                analysis.notes.append(f"音频下载失败：{exc}")
                self.logger(f"音频下载失败 {item.bvid}：{exc}", "WARN")
                return ""
        # yt-dlp -x --audio-format wav 会输出 audio.wav
        if wav.exists():
            self.logger(f"音频转写中：{item.bvid}，模型 {cfg.get('asr_model') or 'small'}")
            backend = str(cfg.get("asr_backend") or "faster_whisper").lower()
            try:
                if backend == "whisper":
                    return await asyncio.to_thread(_transcribe_whisper, str(wav), str(cfg.get("asr_model") or "base"))
                return await asyncio.to_thread(_transcribe_faster_whisper, str(wav), str(cfg.get("asr_model") or "small"))
            except Exception as exc:
                analysis.notes.append(f"语音识别失败：{exc}")
                self.logger(f"语音识别失败 {item.bvid}：{exc}", "WARN")
                return ""
        # 回退：如果有其它格式音频，用 ffmpeg 转 wav
        # 注意：过滤 yt-dlp 残留的半成品（.part/.ytdl/.info.json），只取真实音频文件。
        audio_files = [
            p for p in audio_dir.glob("audio.*")
            if p.suffix.lower() != ".wav"
            and p.suffix.lower() not in (".part", ".ytdl", ".json", ".tmp")
        ]
        if audio_files:
            src = audio_files[0]
            cmd = [ffmpeg_bin, "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(wav)]
            try:
                await _run_command(cmd, timeout=int(cfg.get("ffmpeg_timeout_seconds", 180) or 180),
                                   output_dir=audio_dir, resource_config=cfg)
            except Exception as exc:
                analysis.notes.append(f"音频提取失败：{exc}")
                return ""
            backend = str(cfg.get("asr_backend") or "faster_whisper").lower()
            try:
                if backend == "whisper":
                    return await asyncio.to_thread(_transcribe_whisper, str(wav), str(cfg.get("asr_model") or "base"))
                return await asyncio.to_thread(_transcribe_faster_whisper, str(wav), str(cfg.get("asr_model") or "small"))
            except Exception as exc:
                analysis.notes.append(f"语音识别失败：{exc}")
                return ""
        return ""

    async def _transcribe_video_audio(self, video_path: str, item: VideoItem, cfg: dict[str, Any], analysis: MediaAnalysis) -> str:
        ffmpeg_bin = _find_tool("ffmpeg", str(cfg.get("ffmpeg_path") or ""))
        if not ffmpeg_bin:
            analysis.notes.append("未安装 ffmpeg，跳过音频提取")
            return ""
        wav = self.cache_dir / item.bvid / "audio.wav"
        if not wav.exists():
            cmd = [ffmpeg_bin, "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", str(wav)]
            try:
                await _run_command(cmd, timeout=int(cfg.get("ffmpeg_timeout_seconds", 180) or 180),
                                   output_dir=self.cache_dir / item.bvid, resource_config=cfg)
            except Exception as exc:
                analysis.notes.append(f"音频提取失败：{exc}")
                return ""
        backend = str(cfg.get("asr_backend") or "faster_whisper").lower()
        try:
            if backend == "whisper":
                return await asyncio.to_thread(_transcribe_whisper, str(wav), str(cfg.get("asr_model") or "base"))
            return await asyncio.to_thread(_transcribe_faster_whisper, str(wav), str(cfg.get("asr_model") or "small"))
        except Exception as exc:
            analysis.notes.append(f"语音识别失败：{exc}")
            self.logger(f"语音识别失败 {item.bvid}：{exc}", "WARN")
            return ""


def _find_tool(name: str, configured_path: str = "") -> str:
    if configured_path:
        p = Path(configured_path)
        if p.exists():
            return str(p)
    found = shutil.which(name)
    return found or ""


def _probe_duration_seconds(ffmpeg_bin: str, video_path: str) -> float:
    """Probe video duration using ffprobe next to ffmpeg."""
    import subprocess

    ffprobe = str(Path(ffmpeg_bin).with_name("ffprobe.exe"))
    if not Path(ffprobe).exists():
        ffprobe = str(Path(ffmpeg_bin).with_name("ffprobe"))
    if not Path(ffprobe).exists():
        return 0.0
    log_dir = Path("data") / "media" / "_subprocess_logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        out_path = log_dir / f"probe_{token}.out"
        err_path = log_dir / f"probe_{token}.err"
        with out_path.open("wb") as out_f, err_path.open("wb") as err_f:
            proc = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path],
                stdout=out_f, stderr=err_f, timeout=60, env=_proxy_safe_env(),
                **_no_window_kwargs(),
            )
        if proc.returncode != 0:
            return 0.0
        value = out_path.read_text(encoding="utf-8", errors="ignore").strip()
        return float(value) if value else 0.0
    except Exception:
        return 0.0
    finally:
        for p in (locals().get("out_path"), locals().get("err_path")):
            try:
                if p:
                    p.unlink(missing_ok=True)
            except Exception:
                pass


def _proxy_safe_env() -> dict[str, str]:
    """Environment for media subprocesses: do not inherit Clash/system proxy vars."""
    env = os.environ.copy()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        env.pop(key, None)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # 确保 winget 安装的 ffmpeg 能被 yt-dlp 分段下载时找到。
    for extra in [
        r"C:/Users/example/AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin",
    ]:
        if Path(extra).exists() and extra.lower() not in env.get("PATH", "").lower():
            env["PATH"] = extra + ";" + env.get("PATH", "")
    return env


def _probe_remote_duration_seconds(yt: str, url: str) -> float:
    """Probe a remote Bilibili video duration using yt-dlp without downloading."""
    import subprocess

    log_dir = Path("data") / "media" / "_subprocess_logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        out_path = log_dir / f"remote_probe_{token}.out"
        err_path = log_dir / f"remote_probe_{token}.err"
        with out_path.open("wb") as out_f, err_path.open("wb") as err_f:
            proc = subprocess.run(
                [yt, "--no-playlist", "--proxy", "", "--print", "duration", "--skip-download", url],
                stdout=out_f, stderr=err_f, timeout=120, env=_proxy_safe_env(),
                **_no_window_kwargs(),
            )
        if proc.returncode != 0:
            return 0.0
        value = out_path.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
        value = [x.strip() for x in value if x.strip()]
        return float(value[0]) if value else 0.0
    except Exception:
        return 0.0
    finally:
        for p in (locals().get("out_path"), locals().get("err_path")):
            try:
                if p:
                    p.unlink(missing_ok=True)
            except Exception:
                pass


def _media_byte_limit(config: dict, name: str, default: int) -> int:
    return max(64 * 1024, min(16 * 1024 ** 3, int(config.get(name) or default)))


def _check_media_resources(folder: Path, config: dict, logs: tuple[Path, Path]) -> None:
    minimum_free = _media_byte_limit(config, 'media_min_free_bytes', 512 * 1024 ** 2)
    if shutil.disk_usage(folder).free < minimum_free:
        raise RuntimeError('媒体处理磁盘剩余空间不足')
    maximum_logs = _media_byte_limit(config, 'subprocess_max_log_bytes', 4 * 1024 ** 2)
    if any(path.exists() and path.stat().st_size > maximum_logs for path in logs):
        raise RuntimeError('媒体工具输出日志超过大小限制')
    maximum_task = _media_byte_limit(config, 'media_task_max_bytes', 2 * 1024 ** 3)
    total = 0
    count = 0
    for parent, directories, files in os.walk(folder, followlinks=False):
        directories[:] = [name for name in directories
                           if not (Path(parent) / name).is_symlink()
                           and not (Path(parent) / name).is_junction()]
        for name in files:
            path = Path(parent) / name
            if path.is_symlink():
                continue
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                continue
            count += 1
            if total > maximum_task or count > 10000:
                raise RuntimeError('单个视频的媒体文件超过资源限制')


async def _stop_media_process(process) -> None:
    """Terminate the process tree belonging to one launched media command."""
    if os.name == 'nt' and process.returncode is None:
        try:
            stop = await asyncio.create_subprocess_exec(
                'taskkill.exe', '/PID', str(process.pid), '/T', '/F',
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **_no_window_kwargs(),
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except asyncio.TimeoutError:
                stop.kill()
                await stop.wait()
        except OSError:
            pass
    elif os.name != 'nt':
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        process.kill()
    await process.wait()


async def _run_command(cmd: list[str], timeout: int = 300, *,
                       output_dir: Path | None = None, resource_config: dict | None = None) -> None:
    env = _proxy_safe_env()
    # DSH/Windows 受限环境下，子进程 stdout/stderr=PIPE 可能触发 WinError 5（命名管道拒绝访问）。
    # 改用工作区临时文件承接输出，避免 yt-dlp/ffmpeg 分段下载被管道权限误伤；失败时仍读取尾部错误。
    log_dir = Path("data") / "media" / "_subprocess_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    out_path = log_dir / f"cmd_{token}.out"
    err_path = log_dir / f"cmd_{token}.err"
    config = resource_config or {}
    folder = Path(output_dir) if output_dir is not None else log_dir
    folder.mkdir(parents=True, exist_ok=True)
    options = _no_window_kwargs()
    if os.name != 'nt':
        options['start_new_session'] = True
    try:
        _check_media_resources(folder, config, (out_path, err_path))
        with out_path.open("wb") as out_f, err_path.open("wb") as err_f:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=out_f, stderr=err_f, env=env, **options,
            )

            async def wait_checked():
                while proc.returncode is None:
                    _check_media_resources(folder, config, (out_path, err_path))
                    await asyncio.sleep(0.25)
                await proc.wait()
                _check_media_resources(folder, config, (out_path, err_path))

            try:
                await asyncio.wait_for(wait_checked(), timeout=timeout)
            except BaseException:
                # Outer analysis timeouts cancel this coroutine as well. Always
                # stop its download/transcode children before releasing the task.
                await asyncio.shield(_stop_media_process(proc))
                raise
        if proc.returncode != 0:
            with err_path.open('rb') as handle:
                handle.seek(max(0, err_path.stat().st_size - 800))
                msg = handle.read(800).decode('utf-8', errors='replace')
            raise RuntimeError(msg or f"命令失败，退出码 {proc.returncode}")
    except asyncio.TimeoutError as exc:
        raise RuntimeError("媒体命令执行超时") from exc
    finally:
        for p in (out_path, err_path):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass


# 模块级 Whisper 模型缓存：避免每个视频重新加载模型（数秒~数十秒），
# 也避免多视频并发时多个完整模型同时驻留内存导致 OOM。
_WHISPER_CACHE: dict[tuple[str, str], Any] = {}
_WHISPER_LOAD_LOCK = threading.Lock()


_YT_COOKIE_HEADER_CACHE: str | None = None


def _yt_cookie_header() -> str:
    """把 data/bilibili_cookies.json 转成 yt-dlp 的 Cookie 请求头（缓解 B 站 412）。"""
    global _YT_COOKIE_HEADER_CACHE
    if _YT_COOKIE_HEADER_CACHE is not None:
        return _YT_COOKIE_HEADER_CACHE
    hdr = ""
    try:
        p = Path(__file__).resolve().parent.parent / "data" / "bilibili_cookies.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        parts = []
        if isinstance(data, dict):
            for k, v in data.items():
                if v is not None and str(v):
                    parts.append(f"{k}={v}")
        hdr = "; ".join(parts)
    except Exception:
        hdr = ""
    _YT_COOKIE_HEADER_CACHE = hdr
    return hdr


_YT_COOKIES_FILE = Path(__file__).resolve().parent.parent / "data" / "cookies_netscape.txt"
_YT_COOKIES_SRC = Path(__file__).resolve().parent.parent / "data" / "bilibili_cookies.json"


def _yt_cookie_args() -> list[str]:
    """B站 412 修复：用 --add-header Cookie: 传参已被 B站风控拒绝（HTTP 412），
    必须改用标准 --cookies Netscape 文件（实测可正常下载）。

    每次调用前按源文件 mtime 决定是否重写；失败回退旧 header 方案。
    """
    try:
        if _YT_COOKIES_SRC.exists():
            src_mtime = _YT_COOKIES_SRC.stat().st_mtime
            need = (not _YT_COOKIES_FILE.exists()) or (_YT_COOKIES_FILE.stat().st_mtime < src_mtime)
            if need:
                data = json.loads(_YT_COOKIES_SRC.read_text(encoding="utf-8"))
                lines = ["# Netscape HTTP Cookie File", "# generated by Koishi bot"]
                if isinstance(data, dict):
                    for k, v in data.items():
                        if v is None or not str(v):
                            continue
                        lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t0\t{k}\t{v}")
                _YT_COOKIES_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
            if _YT_COOKIES_FILE.exists() and _YT_COOKIES_FILE.stat().st_size > 0:
                return ["--cookies", str(_YT_COOKIES_FILE)]
    except Exception:
        pass
    _ck = _yt_cookie_header()
    if _ck:
        return ["--add-header", f"Cookie:{_ck}"]
    return []


_ACR_CREDS_FILE = Path(__file__).resolve().parent.parent / "data" / "_acr_creds.json"
_ACR_USAGE_FILE = Path(__file__).resolve().parent.parent / "data" / "_acr_usage.json"
_ACR_TMP_DIR = Path(__file__).resolve().parent.parent / "data" / "_acr_tmp"


def _acr_today() -> str:
    import time
    return time.strftime("%Y-%m-%d")


def _acr_today_used() -> int:
    try:
        d = json.loads(_ACR_USAGE_FILE.read_text(encoding="utf-8"))
        if str(d.get("date") or "") == _acr_today():
            return max(0, int(d.get("count") or 0))
    except Exception:
        pass
    return 0


def _acr_bump_usage() -> None:
    try:
        n = _acr_today_used() + 1
        _ACR_USAGE_FILE.write_text(
            json.dumps({"date": _acr_today(), "count": n}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


_ACR_WARNED: set[str] = set()


def _acr_warn(log, key: str, msg: str, level: str = "WARN") -> None:
    """同一个原因只报一次（避免刷屏），但不再完全静默。

    2026-09-15：这功能从 9-07 加进来就一直是坏的——第 2174 行调用了**根本不存在的**
    `_find_ffmpeg()`，每次都在那一步抛 NameError，被下面的 `except: return ""` 吞掉，
    于是日志里一条「音乐识别完成」都没有、`data/_acr_usage.json` 也从未生成，
    而接口和凭证其实都是好的。所以现在失败至少要吭一声。
    """
    if not log or key in _ACR_WARNED:
        return
    _ACR_WARNED.add(key)
    try:
        log(msg, level)
    except Exception:
        pass


def _acr_identify_blocking(wav_path: str, cfg: dict, tmp_dir: Path, log=None) -> str:
    """ACRCloud 免费档识别（无人声 BGM 曲名）。返回识别文本；额度满/失败/低分返回空。

    每日默认上限 80 次（免费档约每天 100 次），阈值 score>=70 才采纳。
    """
    try:
        fp = dict(cfg.get("audio_fingerprint") or {})
        if not fp.get("enabled", True):
            return ""
        daily_limit = max(0, int(fp.get("daily_limit", 80) or 80))
        if daily_limit > 0 and _acr_today_used() >= daily_limit:
            _acr_warn(log, "acr-quota", f"音乐识别跳过：今日额度已用满 {daily_limit} 次")
            return ""
        creds_file = _ACR_CREDS_FILE
        if not creds_file.exists():
            _acr_warn(log, "acr-nocreds", f"音乐识别跳过：没找到凭证文件 {creds_file}")
            return ""
        creds = json.loads(creds_file.read_text(encoding="utf-8"))
        host = str(creds.get("host") or "")
        ak = str(creds.get("access_key") or "")
        sk = str(creds.get("access_secret") or "")
        if not (host and ak and sk):
            _acr_warn(log, "acr-badcreds", "音乐识别跳过：凭证文件里 host/access_key/access_secret 不全")
            return ""
        # 注意：这里必须用 _find_tool（全包统一入口，认 config 里的 ffmpeg_path），
        # 之前误写成不存在的 _find_ffmpeg() → 每次都 NameError 被静默吞掉（2026-09-15 修）。
        ffmpeg = _find_tool("ffmpeg", str(cfg.get("ffmpeg_path") or ""))
        if not ffmpeg:
            _acr_warn(log, "acr-noffmpeg",
                      "音乐识别跳过：没找到 ffmpeg（配 config.media_understanding.ffmpeg_path）")
            return ""
        tmp_dir.mkdir(parents=True, exist_ok=True)
        clip = tmp_dir / f"acr_{Path(wav_path).stem}.wav"
        try:
            subprocess.run([ffmpeg, "-y", "-ss", "0", "-t", "40", "-i", wav_path,
                            "-ac", "1", "-ar", "16000", "-f", "wav", str(clip)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180,
                           **_no_window_kwargs())
            if not clip.exists() or clip.stat().st_size <= 0:
                return ""
            payload_bytes = clip.read_bytes()
        finally:
            try:
                clip.unlink(missing_ok=True)
            except Exception:
                pass
        import time as _t
        import httpx
        import base64 as _b64
        import hashlib as _hl
        import hmac as _hm
        ts = str(_t.time())
        string_to_sign = "\n".join(["POST", "/v1/identify", ak, "audio", "1", ts])
        sig = _b64.b64encode(_hm.new(sk.encode(), string_to_sign.encode(), _hl.sha1).digest()).decode()
        data = {
            "access_key": ak,
            "sample_bytes": str(len(payload_bytes)),
            "data_type": "audio",
            "signature_version": "1",
            "timestamp": ts,
            "signature": sig,
        }
        with httpx.Client(timeout=90) as c:
            r = c.post(f"{host}/v1/identify", data=data,
                       files={"sample": (clip.name or "sample.wav", payload_bytes, "audio/wav")})
        _acr_bump_usage()
        res = r.json()
        status = res.get("status") or {}
        code = status.get("code")
        if code != 0:
            if code == 1001:
                # 1001 = No result：ACR 曲库里没有这首。东方同人曲/游戏实录音频太常见，
                # 属于**正常未命中**，不该报成"失败"（否则一轮下来看着像功能坏了）。
                _acr_warn(log, "acr-nomatch",
                          "音乐识别：ACR 曲库里没匹配到这首（code=1001 No result，正常未命中）",
                          "INFO")
            else:
                # 3001=凭证错、3003=额度超、3002=音频无效 等，这些才要人看
                _acr_warn(log, f"acr-code-{code}",
                          f"音乐识别失败：ACRCloud code={code} msg={status.get('msg')}")
            return ""
        min_score = int(fp.get("min_score", 70) or 70)
        musics = ((res.get("metadata") or {}).get("music") or [])
        best = None
        for m in musics:
            s = float(m.get("score") or 0)
            if s >= min_score and (best is None or s > float(best.get("score") or 0)):
                best = m
        if not best:
            return ""
        title = str(best.get("title") or "")
        artists = "、".join(str(a.get("name")) for a in (best.get("artists") or []))
        album = str((best.get("album") or {}).get("name") or "")
        score = best.get("score")
        parts = [f"《{title}》"]
        if artists:
            parts.append(artists)
        if album:
            parts.append(f"专辑《{album}》")
        parts.append(f"识别分数 {score}")
        return "；".join(parts)
    except Exception as exc:
        _acr_warn(log, f"acr-exc-{type(exc).__name__}",
                  f"音乐识别异常：{type(exc).__name__}: {str(exc)[:150]}")
        return ""


def _transcribe_faster_whisper(wav_path: str, model_name: str) -> str:
    from faster_whisper import WhisperModel

    key = ("faster", model_name)
    with _WHISPER_LOAD_LOCK:
        model = _WHISPER_CACHE.get(key)
        if model is None:
            # 后台只用少量 CPU 线程，避免并发视频同时加载两份模型。
            model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=2)
            _WHISPER_CACHE[key] = model
    segments, _info = model.transcribe(wav_path, vad_filter=True)
    return "\n".join(seg.text.strip() for seg in segments if seg.text.strip())


def _transcribe_whisper(wav_path: str, model_name: str) -> str:
    import whisper

    model = _WHISPER_CACHE.get(("openai", model_name))
    if model is None:
        model = whisper.load_model(model_name)
        _WHISPER_CACHE[("openai", model_name)] = model
    result = model.transcribe(wav_path)
    return str(result.get("text") or "").strip()


def _character_verify_high_risk(item: VideoItem, analysis: MediaAnalysis) -> tuple[bool, str]:
    """Decide whether a video needs expensive GPT role verification.

    Ordinary videos keep the cheap main vision path.  Expensive fallback vision is
    reserved for the cases that historically cause character mistakes: fumo/plush,
    manga/comic screenshots, memes, cosplay/figures, non-standard outfits, multiple
    characters, and uncertain main-vision output.
    """
    text = "\n".join([
        str(getattr(item, "title", "") or ""),
        str(getattr(item, "desc", "") or ""),
        " ".join(str(x) for x in (getattr(item, "tags", None) or [])),
        str(analysis.metadata_text or ""),
        str(analysis.vision_text or ""),
        str(analysis.ai_conclusion_text or ""),
    ])
    low = text.lower()
    checks: list[tuple[str, list[str]]] = [
        ("fumo/毛绒/手办", ["fumo", "fumofumo", "ぬい", "毛绒", "毛絨", "玩偶", "公仔", "手办", "手辦", "figure", "plush", "粘土人"]),
        ("漫画/黑白截图", ["漫画", "漫才", "黑白", "黑白漫画", "醉蝶华", "智灵奇传", "三月精", "儚月抄", "茨歌仙", "铃奈庵", "漫画截图"]),
        ("表情包/梗图", ["表情包", "梗图", "meme", "doge", "给我打钱", "赛钱", "お賽銭", "二维码", "收款码"]),
        ("二创换装/非标准形态", ["换装", "泳装", "水着", "私服", "女仆装", "改色", "二创", "同人设定", "非标准", "cos", "cosplay"]),
        ("多角色同框", ["多人", "多角色", "同框", "组合", "姐妹", "主角组", "红魔馆", "地灵殿", "群像", "群舞", "两位", "三人", "一排"]),
        ("主视觉不确定", ["疑似", "不确定", "无法确认", "看不清", "难以辨认", "信息不足", "可能是", "像是", "无法判断", "非东方"]),
    ]
    reasons = [label for label, keys in checks if any(k.lower() in low for k in keys)]
    return (bool(reasons), "、".join(reasons[:3]))


def _canonical_single_role(label: str, alias_map: dict[str, str]) -> str:
    """Accept only an exact single-character alias, never a name hidden in prose."""
    return canonical_role(label, alias_map)


def _parse_character_vote(first_line: str, alias_map: dict[str, str]) -> tuple[str, str]:
    """Parse the requested name｜confidence format; low confidence gets no vote."""
    line = str(first_line or "").strip().strip("*")
    match = re.fullmatch(
        r"\s*(.{1,60}?)\s*[｜|]\s*(高|中|低)(?:置信度)?\s*[。.]?\s*",
        line,
    )
    if not match:
        return "", "低"
    confidence = match.group(2)
    if confidence == "低":
        return "", confidence
    return _canonical_single_role(match.group(1), alias_map), confidence


def _premium_confirmed_role(text: str, alias_map: dict[str, str]) -> str:
    """Only a structured, visually confirmed premium verdict may veto a vote."""
    first = next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")
    match = re.fullmatch(r"复核结论[:：]\s*确认\s*[｜|]\s*(.{1,60})", first)
    return _canonical_single_role(match.group(1), alias_map) if match else ""


def _analysis_cache_key(item: VideoItem, cfg: dict[str, Any], text_model: str, vision_model: str,
                        persona_prompt: str = "", knowledge_text: str = "") -> str:
    relevant = {
        "bvid": item.bvid,
        "title": item.title,
        "duration": item.duration,
        "desc_hash": hashlib.sha256((item.desc or "").encode("utf-8")).hexdigest()[:16],
        "use_subtitles": bool(cfg.get("use_subtitles", True)),
        "analyze_cover": bool(cfg.get("analyze_cover", True)),
        "analyze_images": bool(cfg.get("analyze_images", True)),
        "premium_vision_enhance": bool(cfg.get("premium_vision_enhance", False)),
        "extract_keyframes": bool(cfg.get("extract_keyframes", False)),
        "transcribe_audio": bool(cfg.get("transcribe_audio", False)),
        "generate_comment": bool(cfg.get("generate_comment", False)),
        "generate_share_reason": bool(cfg.get("generate_share_reason", True)),
        "summarize": bool(cfg.get("summarize", True)),
        "text_model": text_model,
        "vision_model": vision_model,
        "quality_mode": str(cfg.get("quality_mode") or "balanced"),
        "metadata_char_budget": int(cfg.get("metadata_char_budget", 0) or 0),
        "subtitle_char_budget": int(cfg.get("subtitle_char_budget", 0) or 0),
        "vision_char_budget": int(cfg.get("vision_char_budget", 0) or 0),
        "audio_char_budget": int(cfg.get("audio_char_budget", 0) or 0),
        "comment_style": str(cfg.get("comment_style") or "")[:120],
        "share_reason_style": str(cfg.get("share_reason_style") or "")[:120],
        "comment_max_length": int(cfg.get("comment_max_length", 120) or 120),
        "adaptive_comment_length_enabled": bool(cfg.get("adaptive_comment_length_enabled", False)),
        "adaptive_comment_length_effective": int(cfg.get("adaptive_comment_length_effective", cfg.get("comment_max_length", 0)) or 0),
        "adaptive_comment_length_by_keyframes": cfg.get("adaptive_comment_length_by_keyframes") or {},
        "multimodal_max_images": int(cfg.get("multimodal_max_images", 8) or 8),
        "image_selection_strategy": "full-span-v1",
        "segmented_vision": bool(cfg.get("segmented_vision", True)),
        "max_images": int(cfg.get("max_images", 6) or 6),
        "keyframe_mode": str(cfg.get("keyframe_mode") or ""),
        "max_keyframes": int(cfg.get("max_keyframes", 0) or 0),
        "media_source_mode": (
            "jijidown-bilidown-v1"
            if (cfg.get("jijidown") or {}).get("enabled", False)
            and (cfg.get("bilidown") or {}).get("enabled", False)
            else "jijidown-v1" if (cfg.get("jijidown") or {}).get("enabled", False)
            else "bilidown-v1" if (cfg.get("bilidown") or {}).get("enabled", False)
            else "builtin-v1"
        ),
        "use_segment_download": bool(cfg.get("use_segment_download", True)),
        "video_only_download": bool(cfg.get("video_only_download", True)),
        "adaptive_keyframes_enabled": bool(cfg.get("adaptive_keyframes_enabled", False)),
        "adaptive_keyframes_effective": int(cfg.get("adaptive_keyframes_effective", cfg.get("max_keyframes", 0)) or 0),
        "adaptive_keyframes_reason": str(cfg.get("adaptive_keyframes_reason") or ""),
        "storyboard_enabled": bool(cfg.get("storyboard_enabled", False)),
        "storyboard_segments": int(cfg.get("storyboard_segments", 0) or 0),
        "storyboard_frames_per_segment": int(cfg.get("storyboard_frames_per_segment", 0) or 0),
        "storyboard_detail_images": int(cfg.get("storyboard_detail_images", 0) or 0),
        "storyboard_model_image_cap": int(cfg.get("storyboard_model_image_cap", 0) or 0),
        "asr_model": str(cfg.get("asr_model") or ""),
        "asr_backend": str(cfg.get("asr_backend") or ""),
        "use_ai_conclusion": bool(cfg.get("use_ai_conclusion", True)),
        "use_page_first_frames": bool(cfg.get("use_page_first_frames", True)),
        # 角色识别参考会改变视觉分析结果；纳入缓存 key，避免继续命中旧的误识别缓存。
        "character_recognition_version": str(cfg.get("character_recognition_cache_version") or "local-reference-plus-premium-verify-v2"),
        "character_recognition_enabled": bool((cfg.get("character_recognition") or {}).get("enabled", True)),
        "character_reference_char_budget": int((cfg.get("character_recognition") or {}).get("reference_char_budget", 2800) or 2800),
        "character_premium_verify_enabled": bool((cfg.get("character_recognition") or {}).get("premium_verify_enabled", False)),
        "character_premium_verify_max_images": int((cfg.get("character_recognition") or {}).get("premium_verify_max_images", 4) or 4),
        # 人设/知识库变化会改变生成结果，必须纳入缓存 key，否则改人设后命中旧缓存。
        "persona_hash": hashlib.sha256((persona_prompt or "").encode("utf-8")).hexdigest()[:12],
        "knowledge_hash": hashlib.sha256((knowledge_text or "")[:8000].encode("utf-8")).hexdigest()[:12],
    }
    raw = json.dumps(relevant, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _quality_budgets(cfg: dict[str, Any]) -> dict[str, int]:
    mode = str(cfg.get("quality_mode") or "balanced").lower()
    presets = {
        "fast": {"metadata": 650, "subtitle": 1400, "vision": 900, "audio": 600, "max_tokens": 360},
        "balanced": {"metadata": 1000, "subtitle": 3600, "vision": 1800, "audio": 1200, "max_tokens": 620},
        "deep": {"metadata": 1400, "subtitle": 6500, "vision": 2600, "audio": 2200, "max_tokens": 900},
    }
    out = dict(presets.get(mode, presets["balanced"]))
    # Explicit budgets override the preset.
    mapping = {
        "metadata_char_budget": "metadata",
        "subtitle_char_budget": "subtitle",
        "vision_char_budget": "vision",
        "audio_char_budget": "audio",
        "ai_combined_max_tokens": "max_tokens",
    }
    for key, target in mapping.items():
        if key in cfg and cfg.get(key) not in (None, ""):
            try:
                val = int(cfg.get(key))
                out[target] = max(0, val)  # 0 表示无限制
            except Exception:
                pass
    return out


def _compact_evidence(
    analysis: MediaAnalysis,
    metadata_chars: int = 900,
    subtitle_chars: int = 2600,
    vision_chars: int = 1600,
    audio_chars: int = 1200,
) -> str:
    parts: list[str] = []
    role_context = identity_context(getattr(analysis, "role_identity", {}))
    if role_context:
        parts.append(role_context)
    # 以视觉为主：画面整体理解 + 画面细节排在最前（恋恋"亲眼看"到的内容优先）
    if analysis.premium_vision_text:
        parts.append("【画面整体理解】\n" + _smart_excerpt(analysis.premium_vision_text, vision_chars))
    if analysis.vision_text:
        parts.append("【画面细节】\n" + _smart_excerpt(analysis.vision_text, vision_chars))
    if analysis.metadata_text:
        parts.append("【标题/简介】\n" + _smart_excerpt(analysis.metadata_text, metadata_chars))
    if analysis.subtitle_text:
        parts.append("【字幕摘录】\n" + _smart_excerpt(analysis.subtitle_text, subtitle_chars))
    if analysis.audio_text:
        parts.append("【音频摘录】\n" + _smart_excerpt(analysis.audio_text, audio_chars))
    if analysis.music_identify_text:
        parts.append("【音乐识别】\n" + _smart_excerpt(analysis.music_identify_text, min(vision_chars, 300)))
    # B站AI总结作为辅助参考，放最后（不喧宾夺主）
    if analysis.ai_conclusion_text:
        parts.append("【B站AI总结·参考】\n" + _smart_excerpt(analysis.ai_conclusion_text, min(vision_chars, 1200)))
    # 证据缺口与看点纪律说明：防止模型把缺失类型当看过、并引导选“真看点”。
    present = []
    if analysis.premium_vision_text or analysis.vision_text:
        present.append("画面帧描述")
    if analysis.subtitle_text:
        present.append("字幕")
    if analysis.audio_text and str(analysis.audio_text).strip():
        present.append("音频转写")
    if analysis.metadata_text:
        present.append("标题/简介")
    if present:
        parts.append(
            "【证据说明】本片证据包含：" + "、".join(present)
            + "。未列出的类型即缺失：若没有字幕/音频转写，"
            "禁止把台词、剧情因果、歌名或‘讲了什么’写成确定内容（只能写画面能证实的东西，或明确加‘像是/大概’）。"
        )
    return "\n\n".join(parts)


def _smart_excerpt(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if limit <= 0 or len(text) <= limit:
        return text
    if limit < 300:
        return text[:limit]
    head = int(limit * 0.55)
    mid = int(limit * 0.20)
    tail = limit - head - mid - 20
    middle_start = max(0, len(text) // 2 - mid // 2)
    return text[:head] + " …… " + text[middle_start:middle_start + mid] + " …… " + text[-tail:]


def _metadata_text(item: VideoItem) -> str:
    tags = "、".join(item.tags or [])
    return (
        f"标题：{item.title}\n"
        f"UP主：{item.author}\n"
        f"BV号：{item.bvid}\n"
        f"链接：{item.url}\n"
        f"时长：{item.duration} 秒\n"
        f"标签：{tags}\n"
        f"简介：{item.desc[:1500]}"
    ).strip()


def _pick_evenly_spaced(items: list[str], limit: int) -> list[str]:
    if not items:
        return []
    limit = max(1, int(limit or 1))
    if len(items) <= limit:
        return list(items)
    idxs = sorted(set(int(i * (len(items) - 1) / max(1, limit - 1)) for i in range(limit)))
    return [items[i] for i in idxs]


def _select_full_span_images(image_paths: list[str], limit: int, *, keep_leading: int = 1) -> list[str]:
    """Select a small image set that still covers the whole video timeline.

    analysis.image_paths is ordered as cover/page-first-frame/keyframes.  Taking
    image_paths[:N] over-represents the cover and the first few minutes; keep a
    little leading context, then sample extracted keyframes evenly across the
    full timeline so long videos can comment on middle/end content too.
    """
    paths = [str(p) for p in (image_paths or []) if str(p)]
    if not paths:
        return []
    limit = max(1, int(limit or 1))
    if len(paths) <= limit:
        return list(paths)
    keep_leading = max(0, min(int(keep_leading or 0), limit))
    leading: list[str] = []
    for p in paths:
        if len(leading) >= keep_leading:
            break
        if p not in leading:
            leading.append(p)
    frame_paths = [p for p in paths if "/frames/" in p.replace("\\", "/")]
    pool = frame_paths or [p for p in paths if p not in leading] or paths
    picked = list(leading)
    for p in _pick_evenly_spaced(pool, limit - len(picked)):
        if p not in picked:
            picked.append(p)
    if len(picked) < limit:
        for p in _pick_evenly_spaced(paths, limit):
            if p not in picked:
                picked.append(p)
            if len(picked) >= limit:
                break
    return picked[:limit]


def _vision_images_for_model(raw_images: list[str], storyboard_paths: list[str], cfg: dict[str, Any]) -> list[str]:
    """Return the image list actually sent to vision models.

    Raw keyframes are still kept on disk and in analysis.image_paths, but when
    storyboards exist we send compact contact sheets plus a limited set of
    evenly-spaced detail frames.  This keeps the 40/60-frame understanding gain
    without linearly increasing model image count.
    """
    raw = [str(p) for p in (raw_images or []) if str(p)]
    boards = [str(p) for p in (storyboard_paths or []) if str(p)]
    if not (cfg.get("storyboard_enabled", False) and boards):
        return raw
    try:
        cap = int(cfg.get("storyboard_model_image_cap") or 24)
        detail_limit = int(cfg.get("storyboard_detail_images") or 12)
    except Exception:
        cap, detail_limit = 24, 12
    cap = max(4, min(40, cap))
    detail_limit = max(0, min(24, detail_limit))
    out: list[str] = []
    for p in boards[:cap]:
        if p not in out:
            out.append(p)
    remain = max(0, cap - len(out))
    details = _pick_evenly_spaced(raw, min(detail_limit, remain)) if remain and detail_limit else []
    for p in details:
        if p not in out:
            out.append(p)
    return out or raw


def _vision_capture_weak(vision_text: str, metadata_text: str = "") -> bool:
    """判断关键帧视觉结果是否信息不足，需要重新捕捉。

    保守触发：只有视觉文本为空/很短，或明显全是黑屏、转场、无法辨认、没有角色/文字/动作时才重抽，
    避免每个视频都重复消耗下载与视觉模型调用。
    """
    text = " ".join(str(vision_text or "").split())
    meta = str(metadata_text or "")
    if not text or len(text) < 80:
        return True
    if len(text) >= 600:
        # 已产出足够篇幅的具体描述（常见 1500~2100 字），说明画面确实被拍到并认真分析了；
        # 弹幕/静止封面/转场多的内容会诚实写出"看不清角色/没有文字"等词，
        # 据此整片重抽只会白耗 5~10 分钟下载 + 二次视觉（常常再撞一次限流），纯浪费。
        return False
    weak_markers = [
        "无法辨认", "难以辨认", "看不清", "看不出", "无法判断", "不确定", "信息不足",
        "几乎全黑", "全黑", "黑屏", "转场", "过渡", "空白", "模糊", "没有明显内容",
        "没有看到角色", "未看到角色", "没有可见角色", "没有文字", "没有字幕",
    ]
    weak_hits = sum(1 for m in weak_markers if m in text)
    strong_markers = [
        "角色", "人物", "字幕", "文字", "标题", "动作", "场景", "镜头", "画面", "服装", "头饰", "道具",
        "灵梦", "魔理沙", "恋恋", "古明地", "咲夜", "蕾米", "芙兰", "妖梦", "琪露诺", "东方",
        "MMD", "手书", "弹幕", "符卡",
    ]
    strong_hits = sum(1 for m in strong_markers if m in text)
    # 标题/标签明显是东方或视频二创，但视觉结果没有抓到角色/文字/动作时，重抽一次。
    expects_visual = any(k in meta.lower() for k in ["东方", "touhou", "mmd", "手书", "动画", "pv", "fumo", "project"])
    if expects_visual and strong_hits < 2:
        return True
    if weak_hits >= 2 and strong_hits < 4:
        return True
    # 大段都在说黑屏/转场，通常是抽帧点不好。
    if weak_hits >= 3:
        return True
    return False


class _QualityVerdict:
    __slots__ = ("ok", "reason")

    def __init__(self, ok: bool, reason: str = ""):
        self.ok = ok
        self.reason = reason

    def __bool__(self) -> bool:
        return bool(self.ok)


# 模板/空话特征：fallback 模板高频出现的短语（出现即判低质）
_TEMPLATE_MARKERS = [
    "主题挺明确", "摆得很直观", "记进东方小本本", "无意识的小心思有被抓到",
    "可以慢慢补笔记", "恋恋看着看着就想", "氛围比普通剪辑更能留住人",
]


def _pick_degraded_draft(retry_draft: str | None, first_draft: str | None, floor: int) -> str:
    """质量门两次未达标时的降级选择：取较长的稿子。

    - 两个稿子都太短（< floor）→ 返回空串，表示"确实该放弃"。
    - 2026-09-11 策略：不再因为"没达到高标准"就整条丢弃。上游分析成本
      （视觉增强 + 角色识别 + 两次生成）已经花掉，只要稿子还像话就发出去。
    """
    best = str(retry_draft or "")
    old = str(first_draft or "")
    if len(old) > len(best):
        best = old
    best = best.strip()
    try:
        need = max(0, int(floor or 0))
    except (TypeError, ValueError):
        need = 0
    return best if best and len(best) >= need else ""


def _comment_quality(comment: str, analysis: MediaAnalysis, max_len: int = 220,
                     min_len_floor: int = 0) -> "_QualityVerdict":
    """评论质量门：校验长度、具体性（证据相关性）、模板特征。

    标准三条：
    1. 长度 >= min(60, max_len * 0.5) 字（上限内太短多为敷衍；短评论规则不误杀）
    2. 至少 2 个证据关键词命中（从视觉/字幕/音频证据里提取实词，评论里出现才算具体）
    3. 不含模板黑名单短语

    min_len_floor：额外的最低长度要求（用于"兜底档模型生成的评论"——
    实测兜底档更容易写到 480~500 字，用更严的下限避免拉低整体质量）。
    """
    text = str(comment or "").strip()
    if any(m in text for m in _TEMPLATE_MARKERS):
        return _QualityVerdict(False, "含模板空话")
    # 证据关键词命中数
    evidence = " ".join([
        str(analysis.premium_vision_text or ""),
        str(analysis.vision_text or ""),
        str(analysis.subtitle_text or ""),
        str(analysis.audio_text or ""),
        str(analysis.title or ""),
    ])
    if not evidence.strip():
        min_len = max(20, min(60, int(max_len or 220) * 0.5))
        if len(text) < min_len:
            return _QualityVerdict(False, f"太短({len(text)}字)")
        return _QualityVerdict(True, "无证据场景放宽")
    # 有较丰富画面/字幕/音频证据时，不能只写短夸夸；宁可少发，也要保住“像真看过”的质量。
    rich_evidence = len(evidence) >= 700 or bool(analysis.vision_text or analysis.premium_vision_text)
    cfg_len = int(max_len or 220)
    if rich_evidence and cfg_len >= 420:
        # 主动评论配置为 550/650/700 时，正文不能再放行 100 多字的短评。
        # 取配置上限约 70%，同时给短视频/证据单一视频留一点空间。
        min_len = max(280, min(480, int(cfg_len * 0.65)))
    else:
        min_len = max(20, min(90, int(cfg_len * 0.5)))
    if min_len_floor:
        min_len = max(min_len, int(min_len_floor))
    if len(text) < min_len:
        return _QualityVerdict(False, f"太短({len(text)}字，要求≥{min_len})")
    hits = 0
    need_hits = 3 if rich_evidence else 2
    # 从证据提取候选关键词（2-6字的中文词/数字/英文词），检查是否出现在评论里
    for kw in set(re.findall(r"[\u4e00-\u9fff]{2,6}|\d{2,6}|[A-Za-z]{3,12}", evidence)):
        if len(kw) >= 2 and kw in text:
            hits += 1
            if hits >= need_hits:
                break
    if hits < need_hits:
        return _QualityVerdict(False, f"证据细节不足({hits}/{need_hits}处)")
    return _QualityVerdict(True, "ok")


def _fallback_comment_draft(item: VideoItem, analysis: MediaAnalysis, max_len: int = 120) -> str:
    """Generate a specific non-AI Koishi-style comment from metadata/subtitles.

    This is intentionally extractive and conservative: it only mentions signals found
    in title/description/subtitles, so it can be used safely when AI is disabled.
    """
    hay = f"{item.title}\n{item.desc}\n{analysis.subtitle_text[:3000]}".lower()
    title_clean = _clean_title(item.title)
    topic = _classify_topic(hay)
    details = _extract_details(item, analysis)

    if topic == "intro":
        text = f"嘛、这个《{title_clean}》把入坑和基础设定讲得挺清楚，{details}恋恋可以慢慢补笔记了～～"
    elif topic == "anime":
        text = f"诶～～《{title_clean}》这种东方动画/同人演绎看起来挺完整，{details}氛围比普通剪辑更能留住人呢。"
    elif topic == "music":
        text = f"哇哦，《{title_clean}》像是东方音乐/曲目整理向内容，{details}恋恋感觉可以顺着旋律飘一会儿。"
    elif topic == "koishi":
        text = f"唔……这个视频直接碰到恋恋相关内容了，{details}无意识的小心思有被抓到一点点～～"
    elif topic == "mmd":
        text = f"好啦好啦，《{title_clean}》这种MMD/手书同人味道挺明显，{details}角色气氛有在认真做。"
    elif topic == "quiz":
        text = f"诶呀，《{title_clean}》是东方角色问答向内容吧，{details}这种题目很适合拿来偷偷检查记忆呢。"
    else:
        text = f"诶呀，《{title_clean}》主题挺明确，{details}恋恋看着看着就想把它记进东方小本本里。"

    return _limit_text(text, max_len)


def _fallback_share_reason(item: VideoItem, analysis: MediaAnalysis) -> str:
    """Short natural reason used when AI generation is disabled/unavailable.

    Do not echo raw evidence such as "【标题/简介/元数据】..." into DMs.
    """
    hay = f"{item.title}\n{item.desc}\n{analysis.subtitle_text[:3000]}".lower()
    topic = _classify_topic(hay)
    details = _extract_details(item, analysis).rstrip("，")
    if details.endswith("标题信息已经能看出方向"):
        details = "主题方向比较明确"

    if topic == "intro":
        return _limit_text(f"嘛、这是东方入门/科普向内容，{details}，适合用来补基础设定～～", 110)
    if topic == "anime":
        return _limit_text(f"诶～～这是东方动画/同人演绎向内容，{details}，看点不只是标题，氛围也能顺着看下去。", 110)
    if topic == "music":
        return _limit_text(f"哇哦，音乐主题很明显，{details}，适合顺手收藏起来循环听一会儿。", 110)
    if topic == "koishi":
        return _limit_text(f"唔……这个视频碰到恋恋相关内容了，{details}，无意识的小心思可以捡一点。", 110)
    if topic == "mmd":
        return _limit_text(f"好啦好啦，这是MMD/手书同人向内容，{details}，角色表现应该是主要看点。", 110)
    if topic == "quiz":
        return _limit_text(f"诶呀，这是东方角色问答/测验向内容，{details}，适合拿来检查自己记住了多少角色。", 110)
    if analysis.subtitle_text:
        return _limit_text(f"唔……这个视频有{len(analysis.subtitle_text)}字左右字幕可读，{details}，适合边看边捡东方信息。", 110)
    if item.desc:
        return _limit_text(f"标题和简介都比较明确，{details}，恋恋觉得可以先收藏起来慢慢看。", 110)
    return "恋恋刚好路过看见它，主题挺明确，就顺手递给你看看～～"


def _fallback_summary(evidence: str) -> str:
    text = " ".join(evidence.split())
    return text[:400] + ("..." if len(text) > 400 else "")


def _clean_title(title: str) -> str:
    text = re.sub(r"<[^>]+>", "", title or "").strip()
    text = re.sub(r"[【\[]\s*(补档|高清修复|转载|搬运)\s*[】\]]", "", text, flags=re.I).strip()
    if len(text) > 28:
        text = text[:27].rstrip() + "…"
    return text or "这个视频"


def _classify_topic(hay: str) -> str:
    if any(w in hay for w in ["古明地恋", "恋恋", "こいし", "koishi"]):
        return "koishi"
    if any(w in hay for w in ["科普", "入坑", "入门", "基础", "解说", "设定", "世界观"]):
        return "intro"
    if any(w in hay for w in ["幻想万华镜", "动画", "pv", "手书", "mad"]):
        return "anime"
    if any(w in hay for w in ["mmd", "模型", "舞蹈"]):
        return "mmd"
    if any(w in hay for w in ["趣味问答", "问答", "测验", "quiz", "选择题", "答题"]):
        return "quiz"
    if any(w in hay for w in ["音乐", "名歌集", "歌集", "曲集", "原曲", "同人音乐", "vocal", "arrange", "remix", "翻奏", "钢琴", "交响", "曲"]):
        return "music"
    return "general"


def _extract_details(item: VideoItem, analysis: MediaAnalysis) -> str:
    text = f"{item.title}\n{item.desc}\n{analysis.subtitle_text[:3000]}"
    details: list[str] = []
    character_hits = _ordered_hits(text, [
        "博丽灵梦", "雾雨魔理沙", "古明地恋", "古明地觉", "琪露诺", "蕾米莉亚", "芙兰朵露",
        "十六夜咲夜", "魂魄妖梦", "西行寺幽幽子", "八云紫", "射命丸文", "东风谷早苗",
        "灵乌路空", "火焰猫燐", "秦心", "丰聪耳神子", "圣白莲", "藤原妹红", "蓬莱山辉夜",
    ])
    if character_hits:
        details.append("提到" + "、".join(character_hits[:3]))
    work_hits = _ordered_hits(text, [
        "红魔乡", "妖妖梦", "永夜抄", "风神录", "地灵殿", "星莲船", "神灵庙", "辉针城", "绀珠传", "天空璋", "鬼形兽", "虹龙洞",
        "幻想乡", "旧地狱", "博丽神社", "红魔馆", "白玉楼", "永远亭", "守矢神社",
    ])
    if work_hits:
        details.append("围着" + "、".join(work_hits[:2]) + "展开")
    if analysis.subtitle_text:
        details.append(f"还读到了{len(analysis.subtitle_text)}字字幕")
    if not details and item.author:
        details.append(f"UP主{item.author}把主题摆得很直观")
    if not details:
        return "标题信息已经能看出方向，"
    return "，".join(details[:2]) + "，"


def _ordered_hits(text: str, words: list[str]) -> list[str]:
    seen: set[str] = set()
    hits: list[str] = []
    low = text.lower()
    for word in words:
        if word.lower() in low and word not in seen:
            seen.add(word)
            hits.append(word)
    return hits


def _limit_text(text: str, max_len: int) -> str:
    max_len = max(40, max_len or 120)
    text = " ".join((text or "").split())
    if len(text) <= max_len:
        return text
    # Prefer dropping the quoted title before losing the actual details.
    text = re.sub(r"《[^》]{16,}》", "这个视频", text)
    if len(text) <= max_len:
        return text
    return text[:max_len - 1].rstrip("，。,. ") + "。"


class _BilidownPending(Exception):
    """Bilidown已接管下载；等待下轮复用，避免启动重复下载。"""


class _RiskControlBlocked(Exception):
    """B站风控拦截（412 / 人机验证类）：立即放弃本视频分析并进入退避，不硬耗重试。"""


_RISK_BLOCK_MARKERS = [
    "browser instead",
    "sign in to confirm",
    "not a bot",
    "verify you are human",
    "人机验证",
    "风控",
    "访问异常",
    "请求被拦截",
    "412",
    "precondition failed",
    "access denied",
    "403",
    # 本机到视频 CDN 的 TLS 反复失败（0x8009030e 凭证耗尽类）：继续重试只会白耗时间，
    # 同样按“连续多段失败→放弃本视频+退避”处理，避免一次分析烧掉十几分钟。
    "security credentials",
    "0x8009030e",
    "unable to acquire security",
    "tls @",
]


def _is_risk_block_error(exc_text: str) -> bool:
    """判断下载异常是否为 B站 风控/反爬拦截（412、需浏览器验证等）。"""
    low = str(exc_text or "").lower()
    return any(m in low for m in _RISK_BLOCK_MARKERS)
