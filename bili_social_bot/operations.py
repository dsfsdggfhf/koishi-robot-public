"""Local, privacy-conscious operations for the always-on social bot.

No network calls are made here. Only old generated ``audio.wav`` files are
eligible for media cleanup; source downloads and analysis records are untouched.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from html import escape
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any

from .utils import project_root, resolve_path


_BVID_DIR = re.compile(r"BV[0-9A-Za-z]{10}\Z", re.I)
_LOG_STAMP = re.compile(r"^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)\]")


def _json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def media_cleanup(cache_dir: Path, *, days: int = 7, delete: bool = False,
                  now: datetime | None = None, max_files: int = 1000,
                  budget_gb: int = 40) -> dict[str, int]:
    """Find/delete stale ASR intermediates, including interrupted analyses.

    The fixed filename and BV directory check deliberately exclude downloaded
    source videos, keyframes and caches. Rechecking mtime immediately before
    deletion avoids touching a file another process has just resumed writing.
    """
    now = now or datetime.now()
    cutoff = (now - timedelta(days=max(1, days))).timestamp()
    result = {"files": 0, "bytes": 0, "deleted_files": 0, "deleted_bytes": 0,
              "cache_bytes": 0, "over_budget_bytes": 0, "errors": 0}
    root = cache_dir.resolve()
    if not root.is_dir():
        return result
    pressure_candidates: list[tuple[float, int, Path]] = []
    for folder in root.iterdir():
        if not folder.is_dir() or folder.is_symlink() or not _BVID_DIR.fullmatch(folder.name):
            continue
        wav = folder / "audio.wav"
        try:
            if wav.is_symlink() or not wav.is_file() or wav.resolve().parent != folder.resolve():
                continue
            stat = wav.stat()
            if stat.st_mtime >= cutoff:
                if stat.st_mtime < (now - timedelta(days=1)).timestamp():
                    pressure_candidates.append((stat.st_mtime, stat.st_size, wav))
                continue
            result["files"] += 1
            result["bytes"] += stat.st_size
            if delete and result["deleted_files"] < max_files:
                latest = wav.stat()
                if latest.st_mtime != stat.st_mtime or latest.st_size != stat.st_size:
                    continue
                wav.unlink()
                result["deleted_files"] += 1
                result["deleted_bytes"] += stat.st_size
        except OSError:
            result["errors"] += 1
    # A 40 GB soft cap: only generated audio older than one day may be removed.
    # If immutable frames/sources alone exceed the cap, report it; never touch
    # unknown files merely to make the number look healthy.
    try:
        result["cache_bytes"] = sum(p.stat().st_size for p in root.rglob("*")
                                    if p.is_file() and not p.is_symlink())
    except OSError:
        result["errors"] += 1
    budget = max(1, budget_gb) * 2**30
    result["over_budget_bytes"] = max(0, result["cache_bytes"] - budget)
    if delete and result["over_budget_bytes"]:
        for mtime, size, wav in sorted(pressure_candidates):
            if result["cache_bytes"] <= budget or result["deleted_files"] >= max_files:
                break
            try:
                latest = wav.stat()
                if latest.st_mtime != mtime or latest.st_size != size:
                    continue
                wav.unlink()
                result["deleted_files"] += 1
                result["deleted_bytes"] += size
                result["cache_bytes"] -= size
            except OSError:
                result["errors"] += 1
        result["over_budget_bytes"] = max(0, result["cache_bytes"] - budget)
    return result


def _recent_log_lines(path: Path, *, bytes_limit: int = 8_000_000) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - bytes_limit))
            data = handle.read()
        if size > bytes_limit:
            data = data.partition(b"\n")[2]
        return data.decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def classified_log_counts(log_file: Path, *, now: datetime | None = None,
                          hours: int = 24) -> dict[str, int]:
    """Count events without returning any log message, UID or text content."""
    now = now or datetime.now()
    cutoff = now - timedelta(hours=hours)
    counts: Counter[str] = Counter()
    for line in _operation_log_lines(log_file, cutoff):
        match = _LOG_STAMP.match(line)
        if not match:
            continue
        try:
            stamp = datetime.fromisoformat(match.group(1))
        except ValueError:
            continue
        if stamp < cutoff or stamp > now + timedelta(minutes=5):
            continue
        # A previous alert contains the same error code. Counting that alert
        # would create a self-sustaining false alarm after the real errors age out.
        if "过去 24 小时" in line or "过去24小时" in line:
            continue
        low = line.lower()
        if "-352" in low:
            counts["bili_relation_risk"] += 1
        if "-509" in low:
            counts["bili_rate_limit"] += 1
        if "429" in low:
            counts["http_429_unattributed"] += 1
        if "额度" in line or "quota" in low or "balance" in low:
            counts["quota_mentions"] += 1
        if "timeout" in low or "超时" in line:
            counts["timeout_mentions"] += 1
    return dict(counts)


def _operation_log_lines(log_file: Path, cutoff: datetime) -> list[str]:
    paths = [log_file]
    try:
        paths.extend(path for path in log_file.parent.glob(f"{log_file.stem}.*.log")
                     if not path.is_symlink() and path.stat().st_mtime >= cutoff.timestamp())
    except OSError:
        pass
    lines: list[str] = []
    for path in paths:
        lines.extend(_recent_log_lines(path))
    return lines


def round_status(log_file: Path, *, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Summarize existing anonymous round metrics by process mode."""
    now = now or datetime.now()
    cutoff = now - timedelta(days=1)
    summary: dict[str, dict[str, Any]] = {}
    for line in _operation_log_lines(log_file, cutoff):
        match = _LOG_STAMP.match(line)
        marker = "ROUND_METRICS "
        if not match or marker not in line:
            continue
        try:
            stamp = datetime.fromisoformat(match.group(1))
            if stamp < cutoff or stamp > now + timedelta(minutes=5):
                continue
            item = json.loads(line.split(marker, 1)[1])
            mode = str(item.get("mode") or "unknown")
            row = summary.setdefault(mode, {"rounds": 0, "success": 0, "failure": 0,
                                            "last_at": "", "last_success_at": ""})
            row["rounds"] += 1
            row["success"] += int(item.get("success_count") or 0)
            row["failure"] += int(item.get("failure_count") or 0)
            row["last_at"] = max(row["last_at"], match.group(1))
            if item.get("success_count"):
                row["last_success_at"] = max(row["last_success_at"], match.group(1))
        except (ValueError, TypeError, AttributeError):
            continue
    return summary


def rotate_log(log_file: Path, *, max_bytes: int = 25_000_000,
               keep_days: int = 14, now: datetime | None = None) -> dict[str, int]:
    """Best-effort rotation; a busy Windows log is retried on the next cycle."""
    now = now or datetime.now()
    result = {"rotated": 0, "expired_deleted": 0, "errors": 0}
    try:
        if log_file.is_file() and log_file.stat().st_size >= max_bytes:
            archive = log_file.with_name(f"{log_file.stem}.{now:%Y%m%d_%H%M%S}.{os.getpid()}.log")
            try:
                log_file.rename(archive)
                result["rotated"] = 1
            except OSError:
                result["errors"] += 1
        cutoff = (now - timedelta(days=max(1, keep_days))).timestamp()
        for archive in log_file.parent.glob(f"{log_file.stem}.*.log"):
            if archive.is_symlink() or archive.stat().st_mtime >= cutoff:
                continue
            try:
                archive.unlink()
                result["expired_deleted"] += 1
            except OSError:
                result["errors"] += 1
    except OSError:
        result["errors"] += 1
    return result


def status_snapshot(config: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    root = project_root()
    state = _json(resolve_path(config.get("state_file", "data/state.json")), {})
    if not isinstance(state, dict):
        state = {}
    today = now.date().isoformat()
    daily = (state.get("daily") or {}).get(today) or {}
    history = state.get("history") or []
    actions: Counter[str] = Counter()
    for item in history:
        if isinstance(item, dict) and str(item.get("time") or item.get("at") or "").startswith(today):
            actions[str(item.get("action") or "unknown")] += 1
    usage: Counter[str] = Counter()
    usage_file = root / "data" / "ai_usage.jsonl"
    try:
        with usage_file.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if f'"{today}' not in line:
                    continue
                item = json.loads(line)
                if not str(item.get("ts") or "").startswith(today):
                    continue
                usage["calls"] += 1
                usage["input_tokens"] += int(item.get("prompt") or 0)
                usage["output_tokens"] += int(item.get("completion") or 0)
                usage["cached_tokens"] += int(item.get("cached") or 0)
    except (OSError, ValueError, TypeError):
        pass
    media_dir = resolve_path((config.get("media_understanding") or {}).get("cache_dir") or "data/media")
    disk = shutil.disk_usage(media_dir if media_dir.exists() else root)
    log_file = resolve_path(config.get("log_file") or "logs/bot.log")
    return {
        "updated": now.isoformat(timespec="seconds"), "date": today,
        "daily": {key: int(value) for key, value in daily.items() if isinstance(value, (int, float))},
        "actions": dict(actions), "ai_usage": dict(usage),
        "events_24h": classified_log_counts(log_file, now=now),
        "rounds_24h": round_status(log_file, now=now),
        "disk_free_gb": round(disk.free / 2**30, 1),
        "stale_audio": media_cleanup(media_dir, now=now),
    }


def operational_alerts(snapshot: dict[str, Any]) -> list[str]:
    events = snapshot.get("events_24h") or {}
    alerts = []
    if snapshot.get("disk_free_gb", 999) < 10:
        alerts.append("媒体磁盘剩余不足 10 GB")
    if (snapshot.get("stale_audio") or {}).get("over_budget_bytes", 0):
        alerts.append("媒体缓存超过 40 GB，等待安全清理或人工检查")
    if events.get("bili_relation_risk", 0):
        alerts.append("过去 24 小时出现 B站关注接口 -352")
    if events.get("bili_rate_limit", 0) >= 10:
        alerts.append("过去 24 小时 B站限频 -509 达到 10 次")
    if events.get("http_429_unattributed", 0) >= 10:
        alerts.append("过去 24 小时 HTTP 429 达到 10 次，请按模型/接口排查")
    return alerts


def status_html(snapshot: dict[str, Any]) -> str:
    cards = [
        ("今日评论", (snapshot.get("daily") or {}).get("comments", 0)),
        ("今日分享", (snapshot.get("daily") or {}).get("shares", 0)),
        ("模型调用", (snapshot.get("ai_usage") or {}).get("calls", 0)),
        ("模型输入 token", (snapshot.get("ai_usage") or {}).get("input_tokens", 0)),
        ("磁盘剩余 GB", snapshot.get("disk_free_gb", "?")),
        ("媒体缓存 GB", round((snapshot.get("stale_audio") or {}).get("cache_bytes", 0) / 2**30, 2)),
        ("待清理音频 GB", round((snapshot.get("stale_audio") or {}).get("bytes", 0) / 2**30, 2)),
    ]
    tiles = "".join(f"<div class='card'><b>{escape(str(v))}</b><span>{escape(k)}</span></div>" for k, v in cards)
    event_rows = "".join(f"<tr><td>{escape(k)}</td><td>{v}</td></tr>" for k, v in sorted((snapshot.get("events_24h") or {}).items()))
    round_rows = "".join(
        f"<tr><td>{escape(mode)}</td><td>{data.get('rounds', 0)}</td><td>{data.get('success', 0)}</td>"
        f"<td>{data.get('failure', 0)}</td><td>{escape(str(data.get('last_success_at') or '—'))}</td></tr>"
        for mode, data in sorted((snapshot.get("rounds_24h") or {}).items()))
    alerts = operational_alerts(snapshot)
    notice = "<p class='warn'>" + "；".join(map(escape, alerts)) + "</p>" if alerts else "<p>未达到告警阈值</p>"
    return ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>社交机器人运营状态</title>"
            "<style>body{font-family:system-ui,'Microsoft YaHei';background:#f5f7fb;color:#213047;max-width:1000px;margin:40px auto;padding:0 20px}"
            ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}.card{background:white;border-radius:12px;padding:22px;box-shadow:0 3px 15px #dce2eb}"
            ".card b{font-size:26px;display:block}.card span{color:#65738a}table{border-collapse:collapse;background:white;width:100%}td{padding:12px;border-bottom:1px solid #e4e9f0}"
            ".warn{color:#a33719;font-weight:bold}</style><h1>社交机器人运营状态</h1>"
            f"<p>更新于 {escape(str(snapshot.get('updated')))}；本页仅显示汇总数字，不含私信内容。</p>"
            f"<div class='grid'>{tiles}</div><h2>需要关注</h2>{notice}<h2>过去 24 小时事件</h2><table>{event_rows}</table>"
            f"<h2>各功能运行</h2><table><tr><td>功能</td><td>轮次</td><td>成功</td><td>失败</td><td>最近成功</td></tr>{round_rows}</table>"
            "<p>HTTP 429 来源未自动归因；“额度”是日志提及次数，不等于实际付费。</p></html>")


def write_status_page(snapshot: dict[str, Any], target: Path | None = None) -> Path:
    target = target or project_root() / "运营状态.html"
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(status_html(snapshot), encoding="utf-8")
    os.replace(tmp, target)
    return target


def quality_review(config: dict[str, Any], target: Path | None = None,
                   *, sample_size: int = 20, now: datetime | None = None) -> Path:
    """Create a public-comment review queue; private-message bodies stay out."""
    now = now or datetime.now()
    state = _json(resolve_path(config.get("state_file", "data/state.json")), {})
    history = state.get("history") or [] if isinstance(state, dict) else []
    cutoff = (now - timedelta(days=7)).isoformat(timespec="seconds")
    comments = [item for item in history if isinstance(item, dict)
                and item.get("action") == "comment_sent"
                and str(item.get("time") or item.get("at") or "") >= cutoff]
    target = target or project_root() / "data" / f"回复抽检_{now:%Y%m%d}.md"
    lines = [f"# 回复抽检（{now:%Y-%m-%d}）", "", "仅抽取近 7 天公开评论；私信正文不写入文件。", "",
             "逐条检查：是否认错人物、答非所问、重复套话、违背人格或打扰对方。", ""]
    for index, item in enumerate(comments[-sample_size:], 1):
        body = " ".join(str(item.get("text") or "").split())
        lines.extend([f"## {index}. {item.get('time') or item.get('at') or ''}  {item.get('bvid') or ''}", "",
                      body.replace("<", "&lt;").replace(">", "&gt;"), "",
                      "- [ ] 内容准确  - [ ] 符合人格  - [ ] 没有重复套话  - [ ] 不打扰对方", ""])
    if not comments:
        lines.append("近 7 天没有可抽检的公开评论。")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target
