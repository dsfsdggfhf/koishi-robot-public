from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .utils import load_json, now_iso, resolve_path, save_json_atomic


FOLLOWER_ENDPOINT = "followers"
RISK_CODE = "-352"


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def bili_error_code(exc: BaseException) -> str:
    for name in ("code", "status", "status_code"):
        value = getattr(exc, name, None)
        if value is not None and str(value).strip():
            return str(value).strip()
    text = str(exc or "")
    if RISK_CODE in text:
        return RISK_CODE
    return type(exc).__name__


def is_follower_risk_error(exc: BaseException) -> bool:
    return bili_error_code(exc) == RISK_CODE or RISK_CODE in str(exc or "")


class FollowerEndpointGuard:
    """Persistent circuit breaker for Bilibili's follower-list endpoint.

    A process restart must not erase the cooldown.  The state is therefore kept
    next to state.json instead of only in memory.  The guard never probes the
    endpoint by itself; callers explicitly mark a real attempt/success/failure.
    """

    def __init__(
        self,
        state_file: str | Path,
        *,
        first_cooldown_hours: float = 24.0,
        max_cooldown_hours: float = 72.0,
    ) -> None:
        parent = resolve_path(state_file).parent
        self.path = parent / "bili_endpoint_guard.json"
        self.first_cooldown_hours = max(1.0, float(first_cooldown_hours))
        self.max_cooldown_hours = max(self.first_cooldown_hours, float(max_cooldown_hours))

    def _load(self) -> dict[str, Any]:
        raw = load_json(self.path, {})
        return raw if isinstance(raw, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
        save_json_atomic(self.path, data)

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now()
        item = dict(self._load().get(FOLLOWER_ENDPOINT) or {})
        blocked_until = _parse_time(item.get("blocked_until"))
        item["blocked"] = bool(blocked_until and blocked_until > now)
        item["blocked_until"] = blocked_until.isoformat(timespec="seconds") if blocked_until else ""
        return item

    def can_attempt(
        self,
        *,
        min_interval_seconds: float = 0.0,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        now = now or datetime.now()
        item = self.status(now)
        if item.get("blocked"):
            return False, f"接口熔断至 {item.get('blocked_until')}"
        last_attempt = _parse_time(item.get("last_attempt_at"))
        interval = max(0.0, float(min_interval_seconds or 0.0))
        if last_attempt and interval > 0:
            next_at = last_attempt + timedelta(seconds=interval)
            if next_at > now:
                return False, f"接口冷却至 {next_at.isoformat(timespec='seconds')}"
        return True, ""

    def mark_attempt(self, source: str, now: datetime | None = None) -> None:
        now = now or datetime.now()
        data = self._load()
        item = dict(data.get(FOLLOWER_ENDPOINT) or {})
        item["last_attempt_at"] = now.isoformat(timespec="seconds")
        item["last_source"] = str(source or "unknown")
        data[FOLLOWER_ENDPOINT] = item
        data["updated_at"] = now_iso()
        self._save(data)

    def mark_success(self, source: str, now: datetime | None = None) -> None:
        now = now or datetime.now()
        data = self._load()
        item = dict(data.get(FOLLOWER_ENDPOINT) or {})
        item.update(
            {
                "blocked_until": "",
                "consecutive_risk_failures": 0,
                "last_error_code": "",
                "last_success_at": now.isoformat(timespec="seconds"),
                "last_source": str(source or "unknown"),
            }
        )
        data[FOLLOWER_ENDPOINT] = item
        data["updated_at"] = now_iso()
        self._save(data)

    def mark_failure(
        self,
        exc: BaseException,
        source: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now()
        data = self._load()
        item = dict(data.get(FOLLOWER_ENDPOINT) or {})
        code = bili_error_code(exc)
        item["last_failure_at"] = now.isoformat(timespec="seconds")
        item["last_error_code"] = code
        item["last_source"] = str(source or "unknown")
        if is_follower_risk_error(exc):
            failures = max(0, int(item.get("consecutive_risk_failures") or 0)) + 1
            hours = min(
                self.max_cooldown_hours,
                self.first_cooldown_hours * (2 ** min(failures - 1, 2)),
            )
            item["consecutive_risk_failures"] = failures
            item["blocked_until"] = (now + timedelta(hours=hours)).isoformat(timespec="seconds")
        else:
            # Ordinary network/server failures still receive a short cooldown;
            # the caller's normal interval usually makes the effective wait longer.
            existing = _parse_time(item.get("blocked_until"))
            short_until = now + timedelta(minutes=15)
            if not existing or existing < short_until:
                item["blocked_until"] = short_until.isoformat(timespec="seconds")
        data[FOLLOWER_ENDPOINT] = item
        data["updated_at"] = now_iso()
        self._save(data)
        return self.status(now)


def guard_file_for_state(state_file: str | Path) -> Path:
    return resolve_path(state_file).parent / "bili_endpoint_guard.json"
