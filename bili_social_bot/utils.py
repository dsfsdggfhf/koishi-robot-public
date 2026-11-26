from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}", re.I)


def project_root() -> Path:
    """项目根目录：基于本文件位置（bili_social_bot/../），不依赖 CWD。

    原实现用 Path.cwd()，导致从别的目录启动时所有相对路径（config/data/logs）
    全部错位——表现为"改了配置但机器人读的是别处的配置"。
    """
    return Path(__file__).resolve().parent.parent


def resolve_path(path: str | os.PathLike[str]) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return project_root() / p


def ensure_parent(path: str | os.PathLike[str]) -> Path:
    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_json(path: str | os.PathLike[str], default: Any) -> Any:
    p = resolve_path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json_atomic(path: str | os.PathLike[str], data: Any) -> None:
    """原子写 JSON：先写唯一临时文件再 os.replace，避免多进程并发时 .tmp 名冲突。

    Windows 上 os.replace 撞到「目标文件正被别的进程读取」会抛 WinError 5（拒绝访问）：
    读句柄默认不带 FILE_SHARE_DELETE，会挡住替换。这种多半是瞬时竞争（别的进程正好在读
    state.json），所以重试几次；否则一次竞争就让整轮任务异常退出、本轮记账全丢
    （2026-09-11 16:41 就出现过一次）。
    """
    p = ensure_parent(path)
    # 唯一临时文件名：进程内随机后缀 + 时间戳，防止 run/chat-run 两个进程同时写同一文件时
    # 共用同一个 .tmp 导致 os.replace 抛 FileNotFoundError 或互相覆盖。
    import time as _time
    import uuid
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        last_exc: BaseException | None = None
        for attempt in range(5):
            try:
                os.replace(tmp, p)
                return
            except OSError as exc:
                if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 5:
                    last_exc = exc
                    _time.sleep(0.15 * (attempt + 1))
                    continue
                raise
        if last_exc:
            raise last_exc
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def append_log(log_file: str, message: str, level: str = "INFO") -> None:
    line = f"[{now_iso()}] [{level}] {message}"
    print(line)
    p = ensure_parent(log_file)
    with p.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def extract_bvid(text: str) -> str:
    m = BVID_RE.search(text or "")
    return m.group(0) if m else ""


def normalize_bvid(value: str) -> str:
    return extract_bvid(value) or value.strip()


def mask_secret(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep * 2) + value[-keep:]


# 涉及去世/悼念的关键词（硬性过滤，避免在逝者相关场合发言/评论）
BEREAVEMENT_KEYWORDS = [
    "去世", "逝世", "离世", "病逝", "猝死", "过世", "讣告", "悼念", "哀悼", "祭奠",
    "一路走好", "安息", "R.I.P", "rip ", "永别", "忌日", "百日", "头七", "追悼",
    "遗像", "遗作", "绝笔", "最后一条", "天堂", "往生",
]


def is_bereavement_text(text: str) -> bool:
    """判断文本（视频标题/简介/评论内容等）是否涉及去世/悼念。"""
    hay = str(text or "").lower()
    return any(k in hay for k in BEREAVEMENT_KEYWORDS)
