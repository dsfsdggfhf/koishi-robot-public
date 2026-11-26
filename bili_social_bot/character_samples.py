from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .utils import load_json, now_iso, resolve_path, save_json_atomic


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _safe_name(text: str) -> str:
    out = "".join(ch for ch in str(text or "") if ch.isalnum() or ch in "._-·")
    return out[:60] or "unknown"


def _image_hashes(path: str | Path) -> dict[str, str]:
    """Return simple local perceptual hashes. Empty dict when Pillow/image is unavailable."""
    try:
        from PIL import Image
    except Exception:
        return {}
    try:
        img = Image.open(path).convert("L")
        # Average hash: 8x8 grayscale, compare to mean.
        small = img.resize((8, 8))
        vals = list(small.getdata())
        avg = sum(vals) / max(1, len(vals))
        ahash_bits = "".join("1" if v >= avg else "0" for v in vals)
        ahash = f"{int(ahash_bits, 2):016x}"
        # Difference hash: 9x8 grayscale, compare adjacent pixels.
        diff = img.resize((9, 8))
        dv = list(diff.getdata())
        bits = []
        for y in range(8):
            row = dv[y * 9:(y + 1) * 9]
            for x in range(8):
                bits.append("1" if row[x] > row[x + 1] else "0")
        dhash = f"{int(''.join(bits), 2):016x}"
        return {"ahash": ahash, "dhash": dhash}
    except Exception:
        return {}


def _hamming_hex(a: str, b: str) -> int:
    try:
        return (int(str(a), 16) ^ int(str(b), 16)).bit_count()
    except Exception:
        return 999


def _load_db(db_path: str | Path) -> dict[str, Any]:
    data = load_json(db_path, {"version": 1, "entries": []})
    if not isinstance(data, dict):
        data = {"version": 1, "entries": []}
    data.setdefault("version", 1)
    data.setdefault("entries", [])
    return data


def _candidate_images_for_video(video_id: str, *, max_images: int = 8) -> list[Path]:
    vid = str(video_id or "").strip()
    if not vid:
        return []
    root = resolve_path("data/media") / vid
    if not root.exists():
        return []
    preferred: list[Path] = []
    # Cover and page-first frames often contain the representative character.
    for rel in ("cover.jpg", "cover.png", "page_first_frames/page_001.jpg"):
        p = root / rel
        if p.exists() and p.is_file():
            preferred.append(p)
    # Then add sampled extracted frames.
    frames_dir = root / "frames"
    if frames_dir.exists():
        frames = sorted([p for p in frames_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS])
        if frames:
            if len(frames) <= max_images:
                preferred.extend(frames)
            else:
                # Evenly sample across the video instead of only taking the beginning.
                for i in range(max_images):
                    idx = round(i * (len(frames) - 1) / max(1, max_images - 1))
                    preferred.append(frames[idx])
    out: list[Path] = []
    seen: set[str] = set()
    for p in preferred:
        key = str(p.resolve()).lower()
        if key not in seen and p.exists() and p.is_file():
            seen.add(key)
            out.append(p)
        if len(out) >= max_images:
            break
    return out


def remember_correction_sample_images(
    *,
    correct: str,
    wrong: str,
    video_id: str = "",
    source: str = "",
    image_paths: list[str | Path] | None = None,
    config: dict[str, Any] | None = None,
) -> int:
    """Persist local sample images for a character correction, plus perceptual hashes.

    No network/model call is made. Samples are copied from already downloaded media frames
    or explicitly supplied local image paths.
    """
    cfg = config or {}
    if not cfg.get("enabled", True):
        return 0
    db_path = str(cfg.get("db_file") or "data/character_correction_samples.json")
    sample_dir = resolve_path(str(cfg.get("directory") or "data/character_samples"))
    max_images = max(1, int(cfg.get("max_samples_per_case") or 8))
    paths = [Path(p) for p in (image_paths or []) if str(p)]
    if not paths:
        paths = _candidate_images_for_video(video_id, max_images=max_images)
    if not paths:
        return 0

    data = _load_db(db_path)
    entries: list[dict[str, Any]] = data.setdefault("entries", [])
    existing_keys = {
        (str(e.get("correct") or ""), str(e.get("wrong") or ""), str(e.get("video_id") or ""), str(e.get("source_image") or ""))
        for e in entries
    }
    char_dir = sample_dir / _safe_name(correct)
    char_dir.mkdir(parents=True, exist_ok=True)
    added = 0
    for p in paths[:max_images]:
        try:
            p = resolve_path(p) if not p.is_absolute() else p
            if not p.exists() or not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
                continue
            key = (str(correct), str(wrong), str(video_id or ""), str(p))
            if key in existing_keys:
                continue
            hashes = _image_hashes(p)
            if not hashes:
                continue
            name = f"{_safe_name(video_id or source)}_{added + 1:02d}{p.suffix.lower()}"
            dest = char_dir / name
            n = 1
            while dest.exists():
                n += 1
                dest = char_dir / f"{_safe_name(video_id or source)}_{added + 1:02d}_{n}{p.suffix.lower()}"
            shutil.copy2(p, dest)
            rel_dest = os.path.relpath(dest, resolve_path("."))
            entries.append({
                "correct": str(correct),
                "wrong": str(wrong),
                "video_id": str(video_id or ""),
                "source": str(source or ""),
                "source_image": str(p),
                "sample_path": rel_dest.replace("\\", "/"),
                "ahash": hashes.get("ahash", ""),
                "dhash": hashes.get("dhash", ""),
                "created_at": now_iso(),
            })
            existing_keys.add(key)
            added += 1
        except Exception:
            continue
    if added:
        data["updated_at"] = now_iso()
        save_json_atomic(db_path, data)
    return added


def match_character_samples(
    image_paths: list[str | Path],
    *,
    config: dict[str, Any] | None = None,
    max_results: int = 3,
) -> list[dict[str, Any]]:
    """Match local images against learned correction samples via perceptual hashes."""
    cfg = config or {}
    if not cfg.get("enabled", True) or not cfg.get("match_enabled", True):
        return []
    data = _load_db(str(cfg.get("db_file") or "data/character_correction_samples.json"))
    entries = [e for e in (data.get("entries") or []) if isinstance(e, dict)]
    if not entries or not image_paths:
        return []
    threshold = int(cfg.get("match_threshold") or 14)
    results: list[dict[str, Any]] = []
    for img in image_paths:
        hashes = _image_hashes(img)
        if not hashes:
            continue
        for e in entries:
            da = _hamming_hex(hashes.get("ahash", ""), str(e.get("ahash") or ""))
            dd = _hamming_hex(hashes.get("dhash", ""), str(e.get("dhash") or ""))
            score = da + dd
            # Require combined similarity and at least one hash to be quite close.
            if score <= threshold and min(da, dd) <= max(6, threshold // 2):
                item = dict(e)
                item["matched_image"] = str(img)
                item["distance"] = score
                item["ahash_distance"] = da
                item["dhash_distance"] = dd
                results.append(item)
    results.sort(key=lambda x: int(x.get("distance", 999)))
    # Deduplicate by correct character/video.
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for r in results:
        key = (str(r.get("correct") or ""), str(r.get("video_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= max_results:
            break
    return out


def format_sample_match_hint(matches: list[dict[str, Any]]) -> str:
    if not matches:
        return ""
    lines = ["【本地角色样本匹配】以下是本地保存的相似画面样本。使用顺序：先按《东方角色识别特征库》对照当前画面的发色、帽子、服装、道具等特征；如果仍不稳，再把这些相似样本当作二次校正参考。"]
    for m in matches:
        correct = str(m.get("correct") or "")
        wrong = str(m.get("wrong") or "")
        vid = str(m.get("video_id") or "")
        source = str(m.get("source") or "")
        dist = str(m.get("distance")) if m.get("distance") is not None else ""
        if wrong == "official-reference" or source.startswith("THBWiki"):
            lines.append(f"- 官方参考样本标注为『{correct}』；来源 {source or vid or '未知'}；相似距离 {dist}。")
        elif wrong in {"manual-confirmed", "人工确认", "用户确认"} or source.startswith("training-image-confirmed"):
            lines.append(f"- 人工确认训练样本标注为『{correct}』；来源 {vid or source or '未知'}；相似距离 {dist}。")
        else:
            lines.append(f"- 相似样本曾被纠正为『{correct}』，不是『{wrong}』；来源 {vid or source or '未知'}；相似距离 {dist}。")
    return "\n".join(lines)
