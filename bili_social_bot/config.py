from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .secrets_loader import load_secrets
from .utils import resolve_path

DEFAULT_CONFIG_FILE = "config.json"
EXAMPLE_CONFIG_FILE = "config.example.json"


@dataclass
class BotConfig:
    data: dict[str, Any]
    path: Path

    @property
    def dry_run(self) -> bool:
        return bool(self.data.get("dry_run", True))

    @property
    def log_file(self) -> str:
        return str(self.data.get("log_file", "logs/bot.log"))

    @property
    def state_file(self) -> str:
        return str(self.data.get("state_file", "data/state.json"))

    @property
    def cookie_file(self) -> str:
        return str(self.data.get("cookie_file", "data/bilibili_cookies.json"))

    @property
    def poll_interval_seconds(self) -> int:
        return max(10, int(self.data.get("poll_interval_seconds", 60) or 60))

    @property
    def global_min_action_gap_seconds(self) -> int:
        return max(0, int(self.data.get("global_min_action_gap_seconds", 45) or 45))

    @property
    def daily_comment_limit(self) -> int:
        return max(0, int(self.data.get("daily_comment_limit", 20) or 20))

    @property
    def daily_share_limit(self) -> int:
        return max(0, int(self.data.get("daily_share_limit", 20) or 20))

    @property
    def ai_marker(self) -> str:
        return str(self.data.get("ai_marker", ""))

    @property
    def require_ai_marker(self) -> bool:
        return bool(self.data.get("require_ai_marker", False))

    @property
    def forbidden_words(self) -> list[str]:
        words = self.data.get("forbidden_words", [])
        return [str(x) for x in words if str(x).strip()]

    @property
    def comment_rules(self) -> list[dict[str, Any]]:
        rules = self.data.get("comment_rules", [])
        return rules if isinstance(rules, list) else []

    @property
    def share_rules(self) -> list[dict[str, Any]]:
        rules = self.data.get("share_rules", [])
        return rules if isinstance(rules, list) else []


def ensure_config(path: str = DEFAULT_CONFIG_FILE) -> Path:
    target = resolve_path(path)
    if not target.exists():
        src = resolve_path(EXAMPLE_CONFIG_FILE)
        if src.exists():
            shutil.copyfile(src, target)
        else:
            target.write_text("{}", encoding="utf-8")
    return target


def is_private_message_blocked(config: BotConfig, uid: Any) -> bool:
    """Return whether *uid* is in the live private-message deny list.

    The bot has separate long-running ``run`` and ``chat-run`` processes.  Admin
    commands can update config.json after ``run`` started, so reading only the
    in-memory config can allow scheduled greetings to ignore a newly added block.
    Read the config file at the final outbound boundary; fall back to memory if the
    file is temporarily unavailable.
    """
    target = str(uid or "").strip()
    if not target:
        return False
    data: dict[str, Any] = config.data
    try:
        with config.path.open("r", encoding="utf-8-sig") as handle:
            live = json.load(handle)
        if isinstance(live, dict):
            data = live
    except Exception:
        pass
    chat = data.get("interactive_chat") or {}
    private = chat.get("private_messages") or {}
    blocked = list(private.get("blocked_uids") or []) + list(chat.get("blocked_uids") or [])
    return target in {str(value).strip() for value in blocked if str(value).strip()}


def apply_cloud_handoff(data: dict[str, Any]) -> None:
    """Keep cloud-owned jobs disabled in the Windows runner after a restart."""
    handoff = data.get("cloud_handoff") or {}
    if not handoff.get("active"):
        return
    owned = set(handoff.get("owned_functions") or [])
    for key in ("morning_greeting", "nightly_greeting"):
        if key in owned:
            data.setdefault(key, {})["enabled"] = False
    if "private_messages" in owned:
        data.setdefault("interactive_chat", {}).setdefault("private_messages", {})["enabled"] = False
    if "birthdays" in owned:
        for key in ("birthday_reminder", "birthday_story_greeting", "birthday_card"):
            data.setdefault(key, {})["enabled"] = False


def local_recommendation_allowed(config: BotConfig, uid: str = "") -> bool:
    """Cloud owns current DM opt-outs. Local recommendations require local ownership."""
    data = config.data
    try:
        data = json.loads(config.path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        if config.path.exists():
            return False
    except (OSError, ValueError, AttributeError):
        return False
    if not isinstance(data, dict):
        return False
    handoff = data.get("cloud_handoff") or {}
    if handoff.get("active") and "private_messages" in (handoff.get("owned_functions") or []):
        return False
    if not uid:
        return True
    if is_private_message_blocked(config, uid):
        return False
    source = (data.get("notification_optouts") or {}).get("file") or "data/notification_optouts.json"
    try:
        optouts = json.loads(resolve_path(source).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return True
    except (OSError, ValueError, AttributeError):
        return False
    values = optouts.get("uids", {}) if isinstance(optouts, dict) else optouts
    return isinstance(values, (dict, list)) and str(uid) not in {str(value) for value in values}


def load_config(path: str = DEFAULT_CONFIG_FILE) -> BotConfig:
    # 先注入 secrets.env 里的密钥，再把配置读进来：
    # config.json 只保留 *_env 键名，真实密钥不进仓库。
    load_secrets()
    p = ensure_config(path)
    # Accept UTF-8 with or without BOM because Windows PowerShell may write BOM.
    with p.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("配置文件顶层必须是 JSON 对象")
    try:
        from .scheduler import configure_scheduler
        configure_scheduler(data, p)
    except Exception:
        pass
    apply_cloud_handoff(data)
    return BotConfig(data=data, path=p)
