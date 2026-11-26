"""Local-only control layer for the Koishi social bot.

No Bilibili or model API is called here. Writable configuration is limited to
boolean feature switches and a few explicitly listed numeric limits.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent.parent
APP = Path(__file__).resolve().parent
PROCESS_MODES = ("supervisor", "run", "chat-run", "health-run")
LIMITS = {
    "daily_comment_limit": ("每日评论上限", 0, 200),
    "daily_share_limit": ("每日分享上限", 0, 200),
    "poll_interval_seconds": ("主循环间隔（秒）", 10, 3600),
    "global_min_action_gap_seconds": ("动作最小间隔（秒）", 0, 3600),
}
LABELS = {
    "ai": "模型调用", "premium_text_only_trial": "GPT-6.1 Sol 文字试用",
    "media_understanding": "视频理解", "jijidown": "唧唧下载",
    "bilidown": "备用下载", "character_recognition": "角色识别",
    "character_sample_matching": "本地样本识别", "touhou_compliance": "东方内容检查",
    "persona": "恋恋人格", "koishi_lyrics": "歌词点缀", "touhou_knowledge": "东方知识库",
    "touhou_learning": "东方长期学习", "memory_enhance": "对话记忆",
    "learn_daily_report": "学习日报", "wiki_learning": "Wiki 学习",
    "weekly_report_dynamic": "每周动态", "nightly_greeting": "晚安私信",
    "morning_greeting": "早安私信", "new_fan_greeting": "新粉问候",
    "emotes": "表情包", "interactive_chat": "互动聊天",
    "catchup": "漏消息补查", "private_messages": "私信回复",
    "comment_replies": "评论回复", "at_replies": "@ 回复",
    "dynamic_comments": "动态评论", "comment_images": "评论图片",
    "role_recognition_dm": "私信识图", "morph": "形态变化",
    "health_check": "健康检查", "heartbeat": "心跳报告",
    "learn_task": "学习任务", "stickers": "贴纸",
    "knowledge_assist": "知识辅助", "station_greeting_posts": "小站早晚安",
    "station_comment_monitor": "小站评论监控", "station_template_pool": "小站模板池",
    "station_images": "小站配图", "correction_learning": "纠错学习",
    "sample_images": "样本图片", "comment_tag_safety": "评论标签检查",
    "birthday_reminder": "生日提醒", "admin_system": "管理员指令",
    "birthday_story_greeting": "生日故事", "birthday_card": "生日贺卡",
}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"配置不是对象：{path.name}")
    return value


def _walk_features(value: Any, path: tuple[str, ...] = ()) -> list[tuple[str, bool]]:
    if not isinstance(value, dict):
        return []
    result: list[tuple[str, bool]] = []
    if path and type(value.get("enabled")) is bool:
        result.append((".".join(path), value["enabled"]))
    for key, child in value.items():
        if isinstance(child, dict):
            result.extend(_walk_features(child, (*path, key)))
    return result


def _get_section(config: dict[str, Any], path: str) -> dict[str, Any]:
    section: Any = config
    for component in path.split("."):
        if not component or not isinstance(section, dict) or component not in section:
            raise ValueError("未知功能")
        section = section[component]
    if not isinstance(section, dict) or type(section.get("enabled")) is not bool:
        raise ValueError("该功能没有可切换的开关")
    return section


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def _process_command(pid: int) -> str:
    """Read command line only for a numeric PID from this project's lock file."""
    if pid <= 0:
        return ""
    if os.name != "nt":
        try:
            return (Path("/proc") / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            return ""
    script = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
              f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}'; "
              "if($p){$p.CommandLine}")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=10, creationflags=flags)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _process_identity(pid: int) -> tuple[str, float | None, int | None]:
    """Return command, creation timestamp and parent for a lock-file PID."""
    if os.name != "nt":
        return _process_command(pid), None, None
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}'; "
        "if($p){[pscustomobject]@{command=$p.CommandLine; "
        "created=$p.CreationDate.ToString('o'); parent=[int]$p.ParentProcessId} "
        "| ConvertTo-Json -Compress}"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        if out.returncode != 0 or not out.stdout.strip():
            return "", None, None
        item = json.loads(out.stdout)
        return (str(item.get("command") or ""),
                datetime.fromisoformat(item["created"]).timestamp(),
                int(item["parent"]))
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return "", None, None


def _expected_process(command: str, mode: str) -> bool:
    command = command.replace("/", "\\").lower()
    if mode == "supervisor":
        return bool(re.search(r"supervisor\.py[\"']?\s+run(?:\s|$)", command))
    return bool(re.search(r"main\.py[\"']?\s+" + re.escape(mode) + r"(?:\s|$)", command))


class ControlCore:
    def __init__(self, root: Path = ROOT, app: Path = APP,
                 snapshot_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None):
        self.root = Path(root).resolve()
        self.app = Path(app).resolve()
        self.config_path = self.root / "config.json"
        self.snapshot_fn = snapshot_fn
        self.mutation_lock = threading.Lock()
        self.operation_lock = threading.Lock()
        self.metrics_lock = threading.Lock()
        self.pending_restart = False
        self.last_operation = ""
        self._metrics_cache: tuple[float, dict[str, Any]] = (0.0, {})

    def config(self) -> dict[str, Any]:
        return _read_json(self.config_path)

    def features(self) -> list[dict[str, Any]]:
        return [
            {"path": path, "label": LABELS.get(path.split(".")[-1], path.split(".")[-1]),
             "group": LABELS.get(path.split(".")[0], path.split(".")[0]), "enabled": enabled}
            for path, enabled in _walk_features(self.config())
        ]

    def settings(self) -> dict[str, Any]:
        cfg = self.config()
        return {
            "dry_run": bool(cfg.get("dry_run", True)),
            "limits": {key: {"label": label, "value": cfg.get(key), "min": lo, "max": hi}
                       for key, (label, lo, hi) in LIMITS.items() if type(cfg.get(key)) is int},
        }

    def _mutate(self, update: Callable[[dict[str, Any]], tuple[bool, str]]) -> dict[str, Any]:
        with self.mutation_lock:
            original = self.config_path.read_bytes()
            data = json.loads(original.decode("utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("配置文件损坏，已拒绝修改")
            changed, label = update(data)
            if not changed:
                return {"changed": False, "message": "状态本来就是这样", "restart_required": self.pending_restart}
            if self.config_path.read_bytes() != original:
                raise RuntimeError("配置刚被其他进程修改，请刷新后重试")
            backups = self.app / "backups"
            backups.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            (backups / f"config_{stamp}.json").write_bytes(original)
            encoded = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            tmp = self.config_path.with_name(f".config.mobile.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            try:
                tmp.write_bytes(encoded)
                if self.config_path.read_bytes() != original:
                    raise RuntimeError("配置在保存前被其他进程修改，请刷新后重试")
                os.replace(tmp, self.config_path)
            finally:
                tmp.unlink(missing_ok=True)
            self.pending_restart = True
            with self.metrics_lock:
                self._metrics_cache = (0.0, {})
            self._audit(f"配置修改：{label}")
            return {"changed": True, "message": "已保存，重启机器人后生效", "restart_required": True}

    def set_feature(self, path: str, enabled: bool) -> dict[str, Any]:
        if type(enabled) is not bool or not isinstance(path, str) or len(path) > 150:
            raise ValueError("开关参数无效")
        def update(data: dict[str, Any]) -> tuple[bool, str]:
            section = _get_section(data, path)
            old = section["enabled"]
            section["enabled"] = enabled
            return old != enabled, f"{path}: {old} → {enabled}"
        return self._mutate(update)

    def set_setting(self, key: str, value: Any) -> dict[str, Any]:
        if key == "dry_run":
            if type(value) is not bool:
                raise ValueError("预演模式必须是真或假")
        elif key in LIMITS:
            lo, hi = LIMITS[key][1:]
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError(f"{LIMITS[key][0]}需在 {lo}～{hi} 之间")
        else:
            raise ValueError("该设置不允许通过手机修改")
        def update(data: dict[str, Any]) -> tuple[bool, str]:
            if key not in data:
                raise ValueError("配置里没有该设置")
            old = data[key]
            data[key] = value
            return old != value, f"{key}: {old} → {value}"
        return self._mutate(update)

    def _lock_pid(self, mode: str) -> int:
        try:
            return int((self.root / f".{mode}.lock").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0

    def processes(self) -> dict[str, dict[str, Any]]:
        result = {}
        for mode in PROCESS_MODES:
            pid = self._lock_pid(mode)
            live = _pid_alive(pid)
            result[mode] = {"running": live, "pid": pid if live else None}
        return result

    def _metrics(self) -> dict[str, Any]:
        with self.metrics_lock:
            timestamp, cached = self._metrics_cache
            if time.monotonic() - timestamp < 120:
                return cached
            if self.snapshot_fn is None:
                sys.path.insert(0, str(self.root))
                from bili_social_bot.operations import status_snapshot, operational_alerts
                data = status_snapshot(self.config())
                data["alerts"] = operational_alerts(data)
            else:
                data = self.snapshot_fn(self.config())
            self._metrics_cache = (time.monotonic(), data)
            return data

    def status(self) -> dict[str, Any]:
        metrics = self._metrics()
        return {"processes": self.processes(), "updated": metrics.get("updated"),
                "daily": metrics.get("daily", {}), "actions": metrics.get("actions", {}),
                "ai_usage": metrics.get("ai_usage", {}),
                "events_24h": metrics.get("events_24h", {}),
                "rounds_24h": metrics.get("rounds_24h", {}),
                "disk_free_gb": metrics.get("disk_free_gb"),
                "stale_audio": metrics.get("stale_audio", {}),
                "alerts": metrics.get("alerts", []),
                "pending_restart": self.pending_restart,
                "last_operation": self.last_operation}

    def log_tail(self, which: str = "supervisor", lines: int = 70) -> list[str]:
        allowed = {"supervisor": self.root / "logs" / "supervisor.log",
                   "health": self.root / "logs" / "supervised_health-run.log"}
        if which not in allowed:
            raise ValueError("只允许查看守护或健康日志")
        path = allowed[which]
        if not path.is_file():
            return []
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 80_000))
            raw = handle.read().decode("utf-8", errors="replace")
        result = []
        for line in raw.splitlines()[-max(1, min(100, lines)):]:
            line = re.sub(r"(?i)(bearer\s+|api[_-]?key\s*[=:]\s*|cookie\s*[=:]\s*)\S+", r"\1[隐藏]", line)
            result.append(line[:400])
        return result

    def _audit(self, message: str) -> None:
        self.app.mkdir(parents=True, exist_ok=True)
        with (self.app / "control_audit.log").open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")

    def _validated_pid(self, mode: str) -> int:
        pid = self._lock_pid(mode)
        if not _pid_alive(pid):
            return 0
        command, started_at, parent_pid = _process_identity(pid)
        if not _expected_process(command, mode):
            raise RuntimeError(f"{mode} 锁文件中的进程身份无法核实，已拒绝停止")
        try:
            lock_written_at = (self.root / f".{mode}.lock").stat().st_mtime
        except OSError as exc:
            raise RuntimeError(f"{mode} 锁文件已变化，已拒绝操作") from exc
        if started_at is not None and not -2 <= lock_written_at - started_at <= 60:
            raise RuntimeError(f"{mode} 锁文件与进程启动时间不符，已拒绝操作")
        supervisor_pid = self._lock_pid("supervisor")
        if mode != "supervisor" and _pid_alive(supervisor_pid) and parent_pid is not None and parent_pid != supervisor_pid:
            raise RuntimeError(f"{mode} 不是当前守护进程的子进程，已拒绝操作")
        return pid

    def _stop(self) -> str:
        candidates = {mode: self._validated_pid(mode) for mode in PROCESS_MODES}
        (self.root / ".supervisor_stop").write_text("stop", encoding="utf-8")
        for mode in ("run", "chat-run", "health-run"):
            pid = candidates[mode]
            if pid and _pid_alive(pid):
                os.kill(pid, signal.SIGTERM)
        supervisor_pid = candidates["supervisor"]
        if supervisor_pid:
            deadline = time.monotonic() + 6
            while _pid_alive(supervisor_pid) and time.monotonic() < deadline:
                time.sleep(0.2)
            if _pid_alive(supervisor_pid):
                os.kill(supervisor_pid, signal.SIGTERM)
        if any(_pid_alive(pid) for pid in candidates.values() if pid):
            raise RuntimeError("仍有机器人进程未停止，请在电脑上检查")
        return "机器人已停止，手机控制台仍可使用"

    def _start(self) -> str:
        supervisor_pid = self._validated_pid("supervisor")
        if supervisor_pid:
            return "机器人已经在运行"
        running_children = [mode for mode in PROCESS_MODES[1:] if self._validated_pid(mode)]
        if running_children:
            raise RuntimeError(f"仍有子进程运行（{', '.join(running_children)}），请先停止，避免重复启动")
        (self.root / ".supervisor_stop").unlink(missing_ok=True)
        (self.root / "logs").mkdir(exist_ok=True)
        with (self.root / "logs" / "mobile_control_launch.log").open("ab") as log:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.Popen([sys.executable, "-u", str(self.root / "supervisor.py"), "run"],
                             cwd=self.root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             creationflags=flags, close_fds=True)
        self.pending_restart = False
        return "机器人启动中，请稍后刷新状态"

    def operate(self, action: str) -> dict[str, str]:
        if action not in {"start", "stop", "restart"}:
            raise ValueError("未知控制操作")
        if not self.operation_lock.acquire(blocking=False):
            raise RuntimeError("已有启停操作正在进行，请稍后")
        try:
            if action == "start":
                message = self._start()
            elif action == "stop":
                message = self._stop()
            else:
                self._stop()
                message = self._start()
            self.last_operation = f"{datetime.now():%H:%M:%S} {message}"
            self._audit(f"机器人操作：{action}；{message}")
            return {"message": message}
        finally:
            self.operation_lock.release()
