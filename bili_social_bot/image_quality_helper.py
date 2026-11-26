"""Conservative single-image preprocessing, bounded by pixels and upload bytes.

This only selects/encodes existing pixels. It cannot recover absent detail or
measure model accuracy. No model requests, image-count increase or disk writes.
"""
from __future__ import annotations

import io
import re
import warnings
from dataclasses import dataclass


SOURCE_MAX_BYTES = 16 * 1024 * 1024
SOURCE_MAX_PIXELS = 25_000_000


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    mime: str
    note: str
    source_size: tuple[int, int]
    output_size: tuple[int, int]
    strategy: str


def _skipped(reason: str, source_size: tuple[int, int] = (0, 0)) -> PreparedImage:
    return PreparedImage(b"", "", reason, source_size, (0, 0), "skip")


def _focus_location(request_text: str) -> str:
    text = str(request_text or "")
    # A location in casual praise does not grant a reason to crop. Require a
    # specific detail-reading or object/UI question as well as explicit location.
    if not re.search(r"文字|读(?:一下)?|写(?:了|的|着)|识(?:字|别)|看清|细节|截图|界面|按钮|图标|菜单|代码|表格|数字|标志|是什么|有什么|哪(?:个|种)|什么(?:东西|物体|花|树)", text):
        return ""
    for location, pattern in (
        ("top-left", r"左上(?:角|方)?"), ("top-right", r"右上(?:角|方)?"),
        ("bottom-left", r"左下(?:角|方)?"), ("bottom-right", r"右下(?:角|方)?"),
        ("top", r"顶部|上部"), ("bottom", r"底部|下部"),
        ("center", r"中间|中部|中央"), ("left", r"左侧|左边"), ("right", r"右侧|右边"),
    ):
        if re.search(pattern, text):
            return location
    return ""


def _fit_size(size: tuple[int, int], bounds: tuple[int, int]) -> tuple[int, int]:
    w, h = size
    ratio = min(1.0, bounds[0] / w, bounds[1] / h)
    return max(1, round(w * ratio)), max(1, round(h * ratio))


def _focus_box(size: tuple[int, int], location: str, aspect: float) -> tuple[int, int, int, int]:
    w, h = size
    if h >= w * 2:
        cw = w if location in {"top", "bottom", "center"} else max(1, round(w * .70))
        ch = min(h, max(1, round(cw / aspect)))
    elif w >= h * 2:
        ch = h if location in {"left", "right", "center"} else max(1, round(h * .70))
        cw = min(w, max(1, round(ch * aspect)))
    else:
        cw = max(1, round(w * .55))
        ch = min(h, max(1, round(cw / aspect)))
    center_x = 0 if "left" in location else w if "right" in location else w / 2
    center_y = 0 if "top" in location else h if "bottom" in location else h / 2
    left = min(max(0, round(center_x - cw / 2)), w - cw)
    top = min(max(0, round(center_y - ch / 2)), h - ch)
    return left, top, left + cw, top + ch


def _focus_canvas(image, location: str, max_side: int):
    from PIL import Image, ImageDraw
    # A single explicit overview/detail pair. Keep all original content in the
    # overview; never present a local crop as if it were the whole input.
    canvas = Image.new("RGB", (max_side, max_side), "white")
    label_h = max(12, min(20, max_side // 24))
    margin = max(2, max_side // 160)
    overview_w = max(1, round(max_side * .25))
    main_x = overview_w + margin
    main_w = max_side - main_x
    usable_h = max_side - label_h - margin
    overview_size = _fit_size(image.size, (overview_w - margin, usable_h))
    overview = image.resize(overview_size, Image.Resampling.LANCZOS)
    canvas.paste(overview, ((overview_w - overview.width) // 2, label_h))
    box = _focus_box(image.size, location, main_w / usable_h)
    detail = image.crop(box)
    detail_size = _fit_size(detail.size, (main_w - margin, usable_h))
    detail = detail.resize(detail_size, Image.Resampling.LANCZOS)
    canvas.paste(detail, (main_x + (main_w - detail.width) // 2, label_h))
    draw = ImageDraw.Draw(canvas)
    draw.text((margin, 1), "FULL", fill=(0, 0, 0))
    draw.text((main_x + margin, 1), "DETAIL", fill=(0, 0, 0))
    draw.line((overview_w, 0, overview_w, max_side), fill=(190, 190, 190), width=1)
    return canvas, box


def _encode(image, *, prefer_png: bool, max_bytes: int):
    from PIL import Image
    current = image
    for attempt in range(8):
        if prefer_png:
            buffer = io.BytesIO()
            current.save(buffer, format="PNG", optimize=True)
            raw = buffer.getvalue()
            if len(raw) <= max_bytes:
                return raw, "image/png", current.size
        for quality in (85, 76, 65, 50):
            buffer = io.BytesIO()
            current.save(buffer, format="JPEG", quality=quality, optimize=True)
            raw = buffer.getvalue()
            if len(raw) <= max_bytes:
                return raw, "image/jpeg", current.size
        if min(current.size) <= 16:
            break
        size = (max(1, round(current.width * .75)), max(1, round(current.height * .75)))
        current = current.resize(size, Image.Resampling.LANCZOS)
    return b"", "", (0, 0)


def prepare_image_for_vision(data: bytes, request_text: str = "", *,
                             max_side: int = 640, max_bytes: int = 6_000_000,
                             allow_focus: bool = True,
                             upscale_small: bool = False) -> PreparedImage:
    """Validate once, EXIF-rotate/flatten, preserve aspect, return one bounded image.

    Whole-image default never upscales. Focus is permitted only with an explicit
    region + detail request, and disabled for tiny images and caller opt-out.
    Very long images retain their full content and warn when little text remains
    readable. Source limit is 16 MiB/25 MP; output uses caller's original byte cap.
    """
    if not isinstance(data, bytes) or not data:
        return _skipped("图片为空或不是有效字节")
    if len(data) > SOURCE_MAX_BYTES:
        return _skipped("原图超过16MiB本地处理限制")
    try:
        max_side = max(32, min(640, int(max_side)))
        max_bytes = max(1, min(6_000_000, int(max_bytes)))
    except (ValueError, TypeError, OverflowError):
        return _skipped("图片处理限制无效")
    source_size = (0, 0)
    try:
        from PIL import Image, ImageOps
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                source_size = source.size
                if source.width <= 0 or source.height <= 0 or source.width * source.height > SOURCE_MAX_PIXELS:
                    return _skipped("原图超过2500万像素本地处理限制", source_size)
                animated = bool(getattr(source, "is_animated", False))
                source.seek(0)
                source.load()  # Verify decoding before trusting small compressed size.
                orientation = source.getexif().get(274, 1)
                image = ImageOps.exif_transpose(source)
                transparent = image.mode in {"RGBA", "LA"} or "transparency" in image.info
                if transparent:
                    rgba = image.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, "white")
                    image = Image.alpha_composite(background, rgba).convert("RGB")
                else:
                    image = image.convert("RGB")
                original_format = str(source.format or "")
        notes: list[str] = []
        if orientation not in {0, 1, None}:
            notes.append("已按原图方向纠正旋转")
        if transparent:
            notes.append("透明部分使用白色背景")
        if animated:
            notes.append("动图仅分析首帧，不能推断后续动作")
        w, h = image.size
        low_resolution = min(w, h) < 160
        if low_resolution:
            notes.append("原图像素较少，缩放无法补回细节")
        location = _focus_location(request_text) if allow_focus and max(w, h) > max_side else ""
        strategy = "whole"
        if location:
            image, box = _focus_canvas(image, location, max_side)
            strategy = "overview+focus"
            labels = {"top-left": "左上", "top-right": "右上", "bottom-left": "左下", "bottom-right": "右下",
                      "top": "顶部", "bottom": "底部", "center": "中间", "left": "左侧", "right": "右侧"}
            notes.append(f"单张排版：左侧FULL是完整原图缩略，右侧DETAIL是{labels[location]}局部；局部不是新增场景")
        else:
            factor = min(1.8, max_side / max(w, h)) if upscale_small and min(w, h) < 360 else 1.0
            if factor > 1:
                output_size = (max(1, round(w * factor)), max(1, round(h * factor)))
                notes.append("小图仅按同一比例重采样，不会生成原图缺失的细节")
            else:
                output_size = _fit_size(image.size, (max_side, max_side))
            if output_size != image.size:
                image = image.resize(output_size, Image.Resampling.LANCZOS)
            if max(w / h, h / w) >= 3:
                notes.append("已保留完整长图；小字可能无法看清，需要局部原图确认，不能补写文字")
        prefer_png = (transparent or strategy == "overview+focus"
                      or bool(re.search(r"文字|读|截图|界面|代码|表格|数字", request_text)))
        # Avoid re-encoding an already bounded, verified JPEG with no transforms.
        if (original_format == "JPEG" and image.size == source_size and orientation in {0, 1, None}
                and strategy == "whole" and not transparent and not animated and len(data) <= max_bytes):
            raw, mime, output_size = data, "image/jpeg", image.size
        else:
            raw, mime, output_size = _encode(image, prefer_png=prefer_png, max_bytes=max_bytes)
        if not raw:
            return _skipped("无法在现有上传字节限制内保留有效图片", source_size)
        if output_size != image.size:
            notes.append("为满足上传字节限制进一步缩小；无法确认的小字不要猜测")
        return PreparedImage(raw, mime, "；".join(notes), source_size, output_size, strategy)
    except Exception:
        return _skipped("图片损坏、格式不支持或不能安全解码", source_size)
