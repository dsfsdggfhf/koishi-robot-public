from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

_DEFAULT_PEAK_RANGES = [("09:00", "12:00"), ("14:00", "18:00")]
_runtime_config: dict[str, Any] | None = None
_runtime_config_path: Path | None = None
_file_config_cache: tuple[Path, float, dict[str, Any]] | None = None


def configure_scheduler(config: dict[str, Any] | None = None, config_path: str | Path | None = None) -> None:
    """注入当前运行配置，避免 scheduler 偷读项目根 config.json 导致 --config 失效。"""
    global _runtime_config, _runtime_config_path
    _runtime_config = dict(config or {})
    _runtime_config_path = Path(config_path).resolve() if config_path else None


def _current_config() -> dict[str, Any]:
    if _runtime_config is not None:
        return _runtime_config
    # 兼容直接导入 scheduler 的旧调用：按 mtime 读取默认 config.json，但不做永久缓存。
    global _file_config_cache
    try:
        import json as _json
        from .utils import resolve_path
        path = resolve_path("config.json")
        mtime = path.stat().st_mtime
        if _file_config_cache and _file_config_cache[0] == path and _file_config_cache[1] == mtime:
            return _file_config_cache[2]
        data = _json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            _file_config_cache = (path, mtime, data)
            return data
    except Exception:
        pass
    return {}


def _load_peak_ranges() -> list[tuple[str, str]]:
    """读取当前配置中的 peak_hours；配置变更/--config 会实时反映到调度成本提示。"""
    peaks = _current_config().get("peak_hours") or []
    ranges: list[tuple[str, str]] = []
    for p in peaks:
        if isinstance(p, dict) and p.get("start") and p.get("end"):
            ranges.append((str(p["start"]), str(p["end"])))
        elif isinstance(p, (list, tuple)) and len(p) == 2:
            ranges.append((str(p[0]), str(p[1])))
    return ranges or list(_DEFAULT_PEAK_RANGES)


def _now() -> datetime:
    """当前调度时间：按当前 BotConfig 的 timezone（默认 Asia/Shanghai）。"""
    try:
        from zoneinfo import ZoneInfo
        tz_name = str(_current_config().get("timezone") or "Asia/Shanghai")
        return datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None)
    except Exception:
        return datetime.now()


def current_time() -> datetime:
    """返回按当前 BotConfig timezone 计算的无 tzinfo 本地调度时间。"""
    return _now()


def is_peak_hour(dt: datetime | None = None, ranges: list[tuple[str, str]] | None = None) -> bool:
    """判断当前时间是否属于高峰时段（价格贵一倍）。

    默认高峰时段见 _load_peak_ranges（可被 config.json 的 peak_hours 覆盖）。
    """
    dt = dt or _now()
    ranges = ranges if ranges is not None else _load_peak_ranges()
    now_min = dt.hour * 60 + dt.minute
    for start, end in ranges:
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
        s = sh * 60 + sm
        e = eh * 60 + em
        if s <= now_min < e:
            return True
    return False


def check_schedule_cost(schedule: dict[str, Any]) -> str | None:
    """返回 schedule 中落在高峰时段的提示，否则 None。"""
    times = schedule.get("times") or []
    bad = [t for t in times if is_peak_hour(_parse_time(t))]
    if bad:
        return f"以下时间落在高峰时段（费用贵一倍）：{', '.join(bad)}；建议挪到空闲时段。"
    return None


def _parse_time(hhmm: str) -> datetime:
    now = _now()
    try:
        h, m = (int(x) for x in str(hhmm).split(":"))
        return now.replace(hour=h, minute=m, second=0, microsecond=0)
    except Exception:
        return now


def due_key(rule: dict[str, Any], now: datetime | None = None) -> str | None:
    """Return a stable run key when the rule is due, otherwise None.

    Supported schedule:
    - {"type":"interval", "minutes": 30}
    - {"type":"daily", "times":["09:00", "20:30"]}
    - {"type":"once", "at":"2025-01-01 12:00"}
    """
    now = now or _now()
    schedule = rule.get("schedule") or {}
    stype = str(schedule.get("type", "daily")).lower()
    name = str(rule.get("name", "unnamed"))

    if stype == "interval":
        minutes = max(1, int(schedule.get("minutes", 60) or 60))
        bucket = int(now.timestamp() // (minutes * 60))
        return f"{name}:interval:{minutes}:{bucket}"

    if stype == "weekly":
        # {"type":"weekly", "weekday":1, "times":["15:00"]}
        # weekday: 1=Monday ... 7=Sunday; also accepts mon/tue/... strings.
        wd = schedule.get("weekday", 1)
        if isinstance(wd, str):
            wd_map = {"mon": 1, "monday": 1, "周一": 1, "星期一": 1,
                      "tue": 2, "tuesday": 2, "周二": 2, "星期二": 2,
                      "wed": 3, "wednesday": 3, "周三": 3, "星期三": 3,
                      "thu": 4, "thursday": 4, "周四": 4, "星期四": 4,
                      "fri": 5, "friday": 5, "周五": 5, "星期五": 5,
                      "sat": 6, "saturday": 6, "周六": 6, "星期六": 6,
                      "sun": 7, "sunday": 7, "周日": 7, "星期日": 7}
            target_wd = wd_map.get(wd.strip().lower(), 1)
        else:
            target_wd = int(wd or 1)
        if now.isoweekday() != target_wd:
            return None
        times = schedule.get("times") or []
        current = now.strftime("%H:%M")
        if current in [str(x).strip() for x in times]:
            return f"{name}:weekly:{now.isocalendar().year}-W{now.isocalendar().week}:{current}"
        grace = max(0, int(schedule.get("grace_seconds", 1800) or 1800))
        if grace > 0:
            for t in [str(x).strip() for x in times]:
                try:
                    h, m = (int(x) for x in t.split(":"))
                except Exception:
                    continue
                target = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if now >= target and (now - target).total_seconds() <= grace:
                    return f"{name}:weekly:{now.isocalendar().year}-W{now.isocalendar().week}:{t}:grace"
        return None

    if stype == "once":
        at = str(schedule.get("at", "")).strip()
        if not at:
            return None
        target = None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                target = datetime.strptime(at, fmt)
                break
            except ValueError:
                pass
        if target and now >= target:
            return f"{name}:once:{at}"
        return None

    # daily: trigger if current minute is one of configured HH:MM values,
    # 或在到点后 GRACE_SECONDS 宽限窗口内（轮询时刻漂移时仍能触发，避免整日漏跑）。
    times = schedule.get("times") or []
    current = now.strftime("%H:%M")
    if current in [str(x).strip() for x in times]:
        return f"{name}:daily:{now.date().isoformat()}:{current}"
    grace = max(0, int(schedule.get("grace_seconds", 1800) or 1800))
    if grace > 0:
        for t in [str(x).strip() for x in times]:
            try:
                h, m = (int(x) for x in t.split(":"))
            except Exception:
                continue
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now >= target and (now - target).total_seconds() <= grace:
                return f"{name}:daily:{now.date().isoformat()}:{t}:grace"
    return None


def is_rule_due(rule: dict[str, Any], last_key: str, now: datetime | None = None) -> tuple[bool, str]:
    key = due_key(rule, now)
    if key and key != last_key:
        return True, key
    return False, key or ""
