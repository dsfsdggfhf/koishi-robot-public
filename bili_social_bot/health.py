from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any

from bilibili_api import session as bili_session

from .bili_client import BiliClient
from .config import BotConfig, is_private_message_blocked
from .safety import validate_message
from .state import BotState
from .utils import append_log, now_iso, resolve_path, save_json_atomic


class HealthMonitor:
    """定期检查评论/私信功能是否正常，出错时通知指定 UID。"""

    _REMOTE_CHECKS = ("login", "comment", "private", "at", "search", "ai_text", "ai_vision")
    _STATE_LOCK_STALE_SECONDS = 120

    def __init__(self, config: BotConfig):
        self.config = config
        self.client = BiliClient(config.cookie_file, min_gap_seconds=5)
        self.hc = config.data.get("health_check") or {}
        self.notify_uid = str(self.hc.get("notify_uid") or "")
        self.notify_name = str(self.hc.get("notify_name") or "")
        self.interval = int(self.hc.get("interval_seconds") or 1800)
        self._last_errors: set[str] = set()
        # 可自动恢复的抖动（网络/限流/瞬时 AI 故障）不立即打扰主人：
        # 同一组错误连续出现 notify_after 次（默认 2 次检查）仍不恢复，才发通知。
        self._err_streak: dict[str, int] = {}
        self.notify_after = max(1, int(self.hc.get("notify_after_consecutive") or 2))
        self._operations_day = ""

    def log(self, message: str, level: str = "INFO") -> None:
        append_log(self.config.log_file, message, level)

    def _check_local_state(self) -> tuple[dict[str, bool], list[str]]:
        """检查状态读写和锁的可用性，不修改真实运行状态。

        读检查只解析现有 state；写检查写入 data 目录中的唯一临时文件后立即删除；
        锁检查不清理现有锁，遇到新鲜锁只报告 busy，避免健康检查误伤运行中的进程。
        """
        state_path = resolve_path(self.config.state_file)
        values = {
            "state_read": False,
            "state_write": False,
            "lock_check": False,
            "state_lock_busy": False,
        }
        errors: list[str] = []

        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            if state_path.exists():
                loaded = json.loads(state_path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("state_not_object")
            BotState(str(state_path))
            values["state_read"] = True
        except Exception:
            errors.append("state_read_failed")

        probe_path: Path | None = None
        try:
            fd, name = tempfile.mkstemp(prefix=".health-state-", suffix=".json", dir=str(state_path.parent))
            os.close(fd)
            probe_path = Path(name)
            probe_path.unlink(missing_ok=True)
            save_json_atomic(str(probe_path), {"health_probe": now_iso()})
            loaded = json.loads(probe_path.read_text(encoding="utf-8"))
            values["state_write"] = bool(isinstance(loaded, dict) and loaded.get("health_probe"))
            if not values["state_write"]:
                errors.append("state_write_failed")
        except Exception:
            errors.append("state_write_failed")
        finally:
            if probe_path is not None:
                try:
                    probe_path.unlink(missing_ok=True)
                except Exception:
                    pass

        lock_path = state_path.with_suffix(state_path.suffix + ".lock")
        created_lock = False
        fd: int | None = None
        try:
            if lock_path.exists():
                age = time.time() - lock_path.stat().st_mtime
                if age <= self._STATE_LOCK_STALE_SECONDS:
                    values["lock_check"] = True
                    values["state_lock_busy"] = True
                else:
                    errors.append("stale_state_lock")
            else:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                created_lock = True
                values["lock_check"] = True
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age <= self._STATE_LOCK_STALE_SECONDS:
                    values["lock_check"] = True
                    values["state_lock_busy"] = True
                else:
                    errors.append("stale_state_lock")
            except Exception:
                errors.append("lock_check_failed")
        except Exception:
            errors.append("lock_check_failed")
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
            if created_lock:
                try:
                    lock_path.unlink(missing_ok=True)
                except Exception:
                    pass
        return values, errors

    def _emit_health_metrics(
        self,
        started: float,
        results: dict[str, bool],
        *,
        error_category: str | None = None,
    ) -> None:
        from .metrics import emit_round_metrics

        attempts = len(self._REMOTE_CHECKS)
        skipped = sum(1 for key in self._REMOTE_CHECKS if results.get(key) is None)
        success = sum(1 for key in self._REMOTE_CHECKS if results.get(key) is True)
        failed = sum(1 for key in self._REMOTE_CHECKS if results.get(key) is False)
        emit_round_metrics(
            self.log,
            mode="health-run",
            started=started,
            candidate_count=attempts,
            attempt_count=attempts - skipped,
            sent_count=0,
            success_count=success,
            failure_count=failed,
            skipped_count=skipped,
            error_category=error_category or ("HealthProbeFailed" if failed else None),
        )

    async def close(self) -> None:
        await self.client.close()

    async def run_forever(self) -> None:
        self.log(f"健康检查已启动，每 {self.interval} 秒检查一次。")
        # 启动后先做一次检查。
        await self.check_once()
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.check_once()
            except Exception as exc:
                self.log(f"健康检查异常：{exc}", "ERROR")

    @staticmethod
    def _is_rate_limited(exc: Exception) -> bool:
        """判断异常是否为 B站限频（-509/请求过于频繁）。

        限频是临时状态（机器人运行中本就可能撞限频，会自动退避恢复），
        不应判为功能故障通知给主人，否则会造成误报刷屏。
        """
        low = str(exc).lower()
        return "-509" in low or "请求过于频繁" in low or "rate limit" in low

    async def check_once(self) -> dict[str, bool]:
        started = time.perf_counter()
        results: dict[str, bool] = {
            "login": False,
            "comment": False,
            "private": False,
            "at": False,
            "search": False,
            "ai_text": False,
            "ai_vision": False,
        }
        errors: list[str] = []
        local_results, local_errors = self._check_local_state()
        results.update(local_results)
        errors.extend(local_errors)

        # All local maintenance is offline. It cannot call B站 or a model and
        # never writes private-message text into the status page or review file.
        try:
            from .operations import (media_cleanup,
                                     operational_alerts, quality_review, rotate_log,
                                     status_snapshot, write_status_page)

            today = time.strftime("%Y-%m-%d")
            if self._operations_day != today:
                media_cfg = self.config.data.get("media_understanding") or {}
                media_dir = resolve_path(media_cfg.get("cache_dir") or "data/media")
                cleaned = await asyncio.to_thread(media_cleanup, media_dir, delete=True)
                if cleaned["deleted_files"]:
                    self.log(f"历史音频清理：{cleaned['deleted_files']} 个，"
                             f"{cleaned['deleted_bytes'] / 2**30:.2f} GB")
                log_result = await asyncio.to_thread(rotate_log, resolve_path(self.config.log_file))
                if log_result["rotated"] or log_result["expired_deleted"]:
                    self.log(f"日志保留：轮转 {log_result['rotated']} 个，"
                             f"清理旧归档 {log_result['expired_deleted']} 个")
                await asyncio.to_thread(quality_review, self.config.data)
                self._operations_day = today
            snapshot = await asyncio.to_thread(status_snapshot, self.config.data)
            await asyncio.to_thread(write_status_page, snapshot)
            errors.extend(operational_alerts(snapshot))
        except Exception as exc:
            self.log(f"本地运营维护失败：{type(exc).__name__}", "WARN")

        # 1) 登录检查
        try:
            if self.client.load_cookies() and await self.client.ensure_login():
                results["login"] = True
            else:
                errors.append("登录失效")
        except Exception as exc:
            errors.append(f"登录检查失败：{exc}")

        if not results["login"]:
            await self._notify(errors)
            self._emit_health_metrics(started, results, error_category="LoginError")
            return results

        # 2) 评论接口检查
        try:
            await bili_session.get_replies(self.client.credential)
            results["comment"] = True
        except Exception as exc:
            if self._is_rate_limited(exc):
                self.log(f"健康检查评论接口限频（-509），本次跳过不计故障：{str(exc)[:80]}", "WARN")
                results["comment"] = True  # 限频是临时的，不算故障
            else:
                errors.append(f"评论接口异常：{exc}")

        # 3) 私信接口检查
        try:
            await bili_session.get_sessions(self.client.credential)
            results["private"] = True
        except Exception as exc:
            if self._is_rate_limited(exc):
                self.log(f"健康检查私信接口限频（-509），本次跳过不计故障：{str(exc)[:80]}", "WARN")
                results["private"] = True
            else:
                errors.append(f"私信接口异常：{exc}")

        # 4) @通知接口检查
        try:
            await bili_session.get_at(self.client.credential)
            results["at"] = True
        except Exception as exc:
            if self._is_rate_limited(exc):
                self.log(f"健康检查@接口限频（-509），本次跳过不计故障：{str(exc)[:80]}", "WARN")
                results["at"] = True
            else:
                errors.append(f"@通知接口异常：{exc}")

        # 5) 搜索接口检查（搜索一个东方关键词）
        try:
            await self.client.search_videos("东方Project", max_results=2, order="pubdate")
            results["search"] = True
        except Exception as exc:
            if self._is_rate_limited(exc):
                self.log(f"健康检查搜索接口限频（-509），本次跳过不计故障：{str(exc)[:80]}", "WARN")
                results["search"] = True
            else:
                errors.append(f"搜索接口异常：{exc}")

        # Observe genuine business requests. No synthetic text/image heartbeat.
        from .ai_runtime_status import recent_status
        reason_labels = {'endpoint_redirect': 'API 入口发生跳转，请核对新版地址',
                         'legacy_route_key': '旧版密钥不支持统一入口，需要绑定正确渠道的新密钥',
                         'upstream_error': '模型服务返回 5xx 错误',
                         'network_or_service': '网络或服务请求失败'}
        ai_cfg = self.config.data.get('ai') or {}
        event_path = resolve_path(ai_cfg.get('runtime_event_file') or 'data/ai_runtime_events.jsonl')
        for kind, result_key, label in [('text','ai_text','文本'), ('vision','ai_vision','视觉')]:
            enabled = ai_cfg.get('enabled', True) and (kind != 'vision' or ai_cfg.get('vision_enabled', True))
            status = recent_status(event_path, kind)
            results[result_key] = status['usable'] if enabled else True
            categories = '、'.join(reason_labels.get(reason, reason) for reason in status['categories'])
            detail = f'（{categories}）' if enabled and categories else ''
            self.log(f"AI{label}业务状态：{status['state'] if enabled else 'disabled'}{detail}；依据最近真实请求，未调用探测接口")
            if enabled and status['state'] == 'failed':
                errors.append(f"AI{label}最近真实业务请求失败（{categories}）")
        if errors:
            # 只有“同一组错误连续多次仍未恢复”才通知主人；单次抖动只写日志继续观察。
            err_key = "|".join(sorted(errors))
            streak = self._err_streak.get(err_key, 0) + 1
            self._err_streak[err_key] = streak
            for stale_key in [k for k in self._err_streak if k != err_key]:
                self._err_streak.pop(stale_key, None)
            if streak >= self.notify_after:
                await self._notify(errors)
            else:
                self.log(
                    f"健康检查发现错误（连续第 {streak}/{self.notify_after} 次，暂不通知主人，继续观察）：{err_key}",
                    "WARN",
                )
        else:
            self._err_streak.clear()
            if self._last_errors:
                self.log("健康检查恢复正常。")
            self._last_errors.clear()

        self.log(
            f"健康检查完成：login={results['login']} comment={results['comment']} "
            f"private={results['private']} at={results['at']} search={results['search']} "
            f"ai_text={results['ai_text']} ai_vision={results['ai_vision']} "
            f"state_read={results['state_read']} state_write={results['state_write']} "
            f"lock_check={results['lock_check']} state_lock_busy={results['state_lock_busy']}"
        )
        self._emit_health_metrics(
            started,
            results,
            error_category="HealthProbeFailed" if errors else None,
        )
        return results

    async def _notify(self, errors: list[str]) -> None:
        if not self.notify_uid:
            self.log("健康检查发现错误，但未配置通知 UID。", "WARN")
            return
        if is_private_message_blocked(self.config, self.notify_uid):
            self.log("健康检查通知对象已设置私信屏蔽，只记录本地告警。", "WARN")
            return
        # 去重：相同错误集合 30 分钟内不重复通知。
        err_key = "|".join(sorted(errors))
        if err_key in self._last_errors:
            return
        self._last_errors.add(err_key)

        name = self.notify_name or "管理员"
        text = (
            f"诶～{name}，Koishi来报个信：机器人这边有点小状况，"
            f"请看下面的检查结果。\n"
            + "\n".join(f"- {e}" for e in errors)
        )
        # 安全校验（无 AI 标识、长度）。
        ok, reason, safe = validate_message(text, self.config.forbidden_words, marker="", require_marker=False)
        if not ok:
            self.log(f"健康检查通知被拦截：{reason}", "WARN")
            return
        try:
            if self.config.dry_run:
                self.log(f"[DRY-RUN] 将通知 {self.notify_uid}，长度={len(safe)}")
            else:
                await self.client.send_private_text(self.notify_uid, safe)
                self.log(f"已通知 {self.notify_uid}（{name}），长度={len(safe)}", "SUCCESS")
        except Exception as exc:
            self.log(f"发送健康检查通知失败：{exc}", "ERROR")
