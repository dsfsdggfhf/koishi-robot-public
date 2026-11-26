from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from bili_social_bot.config import DEFAULT_CONFIG_FILE, ensure_config, load_config
from bili_social_bot.state import BotState
from runtime_identity import process_matches


_PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)


def _protect_from_system_proxy() -> None:
    """Make long-running bot processes independent from Clash/system proxy changes."""
    for key in _PROXY_ENV_KEYS:
        os.environ.pop(key, None)
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


_protect_from_system_proxy()


_MODE_LOCK_ROOT = Path(__file__).resolve().parent


def _no_window_kwargs() -> dict:
    """Windows 下阻止子进程新建"可见的"控制台窗口。

    main.py 由 pythonw.exe 启动（没有控制台），spawn powershell 会被系统新建一个
    控制台窗口。只影响窗口可见性，不改变行为。
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


def _commandline_is_mode(commandline: str, mode: str) -> bool:
    """Match an exact `main.py <mode>` pair, avoiding substring collisions."""
    parts = [p for p in str(commandline or "").replace("\x00", " ").split() if p]
    for index, part in enumerate(parts[:-1]):
        if Path(part.strip('"')).name.lower() == "main.py" and parts[index + 1].strip('"') == mode:
            return True
    return False


def _mode_owner_alive(pid: int, mode: str) -> bool:
    return process_matches(pid, _MODE_LOCK_ROOT, mode) is not False


def _lower_background_priority() -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        if not kernel.SetPriorityClass(kernel.GetCurrentProcess(), subprocess.BELOW_NORMAL_PRIORITY_CLASS):
            print("无法降低后台优先级，继续按原优先级运行。", file=sys.stderr)


@contextmanager
def _mode_lock(mode: str):
    """Serialize one outbound-capable main.py mode, including `*-once` runs."""
    lock_path = _MODE_LOCK_ROOT / f".{mode}.lock"
    acquired = False
    fd: int | None = None
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            fd = None
            acquired = True
            break
        except FileExistsError:
            try:
                old_pid = int(lock_path.read_text(encoding="utf-8").strip())
            except Exception:
                old_pid = 0
            if old_pid and _mode_owner_alive(old_pid, mode):
                break
            try:
                lock_path.unlink()
            except FileNotFoundError:
                continue
            except Exception:
                break
        except Exception:
            break
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
                fd = None
    try:
        yield acquired
    finally:
        if acquired:
            try:
                if lock_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                    lock_path.unlink(missing_ok=True)
            except Exception:
                pass


def _mode_lock_message(mode: str) -> str:
    return f"已有 {mode} 模式实例运行，拒绝并行启动；请先列出进程并确认旧实例状态。"


def _missing_dependency_message(exc: Exception) -> str:
    return (
        f"缺少运行依赖：{exc}\n"
        "请先执行：pip install -r requirements.txt\n"
        "如果曾安装过错误包名，请执行：pip uninstall bilibili-api -y"
    )


def _load_bili_client_class():
    try:
        from bili_social_bot.bili_client import BiliClient
        return BiliClient
    except ModuleNotFoundError as exc:
        print(_missing_dependency_message(exc))
        raise SystemExit(1)


def _load_social_bot_class():
    try:
        from bili_social_bot.bot import SocialBot
        return SocialBot
    except ModuleNotFoundError as exc:
        print(_missing_dependency_message(exc))
        raise SystemExit(1)


async def cmd_login(config_path: str) -> int:
    BiliClient = _load_bili_client_class()
    cfg = load_config(config_path)
    client = BiliClient(cfg.cookie_file, min_gap_seconds=2)
    try:
        ok = await client.login_qrcode()
        if not ok:
            return 1
        ok = await client.ensure_login()
        print("登录验证：", "成功" if ok else "失败")
        if not ok:
            print("失败原因：", getattr(client, "last_login_error", "未知"))
            print("建议：等待1-3分钟后运行 python main.py check；如果仍失败，删除 data/bilibili_cookies.json 后重新 login。")
        return 0 if ok else 1
    finally:
        await client.close()


async def cmd_check(config_path: str) -> int:
    BiliClient = _load_bili_client_class()
    cfg = load_config(config_path)
    client = BiliClient(cfg.cookie_file, min_gap_seconds=2)
    try:
        if not client.load_cookies():
            print(f"未找到有效 Cookie：{cfg.cookie_file}")
            return 1
        print("Cookie 摘要：")
        print(json.dumps(client.cookie_summary(), ensure_ascii=False, indent=2))
        ok = await client.ensure_login()
        print("登录状态：", "有效" if ok else "无效/过期")
        if not ok:
            print("失败原因：", getattr(client, "last_login_error", "未知"))
        if client.uid:
            print(f"UID: {client.uid}")
        return 0 if ok else 1
    finally:
        await client.close()


async def cmd_once(config_path: str, force: bool) -> int:
    cfg = load_config(config_path)
    # --force 只允许在 dry_run 中用于检查规则/模板；生产配置下禁止绕过
    # enabled、时间窗和去重保护，避免一次人工命令触发全量外发。
    if force and not cfg.dry_run:
        print("拒绝执行：--force 仅允许在 dry_run=true 的配置下使用。")
        print("如需生产运行，请去掉 --force，让调度、时间窗和去重保护正常生效。")
        return 2
    with _mode_lock("run") as acquired:
        if not acquired:
            print(_mode_lock_message("run"))
            return 3
        SocialBot = _load_social_bot_class()
        bot = SocialBot(cfg)
        try:
            await bot.run_once(force=force)
            return 0
        finally:
            await bot.close()


async def cmd_run(config_path: str) -> int:
    with _mode_lock("run") as acquired:
        if not acquired:
            print(_mode_lock_message("run"))
            return 3
        _lower_background_priority()
        SocialBot = _load_social_bot_class()
        cfg = load_config(config_path)
        bot = SocialBot(cfg)
        try:
            await bot.run_forever()
            return 0
        except KeyboardInterrupt:
            bot.log("机器人已停止。")
            return 0
        finally:
            await bot.close()


async def cmd_chat_once(config_path: str) -> int:
    with _mode_lock("chat-run") as acquired:
        if not acquired:
            print(_mode_lock_message("chat-run"))
            return 3
        from bili_social_bot.chat_bot import InteractiveChatBot
        cfg = load_config(config_path)
        bot = InteractiveChatBot(cfg)
        try:
            result = await bot.run_once()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        finally:
            await bot.close()


async def cmd_chat_run(config_path: str) -> int:
    with _mode_lock("chat-run") as acquired:
        if not acquired:
            print(_mode_lock_message("chat-run"))
            return 3
        _lower_background_priority()
        from bili_social_bot.chat_bot import InteractiveChatBot
        cfg = load_config(config_path)
        bot = InteractiveChatBot(cfg)
        try:
            await bot.run_forever()
            return 0
        except KeyboardInterrupt:
            bot.log("互动聊天已停止。")
            return 0
        finally:
            await bot.close()


async def cmd_health_check(config_path: str) -> int:
    with _mode_lock("health-run") as acquired:
        if not acquired:
            print(_mode_lock_message("health-run"))
            return 3
        from bili_social_bot.health import HealthMonitor
        cfg = load_config(config_path)
        mon = HealthMonitor(cfg)
        try:
            results = await mon.check_once()
            print(json.dumps(results, ensure_ascii=False, indent=2))
            return 0
        finally:
            await mon.close()


async def cmd_health_run(config_path: str) -> int:
    with _mode_lock("health-run") as acquired:
        if not acquired:
            print(_mode_lock_message("health-run"))
            return 3
        from bili_social_bot.health import HealthMonitor
        cfg = load_config(config_path)
        mon = HealthMonitor(cfg)
        try:
            await mon.run_forever()
            return 0
        except KeyboardInterrupt:
            mon.log("健康检查已停止。")
            return 0
        finally:
            await mon.close()


async def cmd_birthdays_check(config_path: str, max_followings: int, max_followers: int, gap: float, output: str) -> int:
    from bili_social_bot.birthdays import birthday_config, format_birthday_stats, save_birthday_cache, scan_mutual_birthdays
    cfg = load_config(config_path)
    bcfg = birthday_config(cfg.data)
    result = await scan_mutual_birthdays(
        cfg,
        max_followings=max_followings or int(bcfg.get("max_followings") or 2000),
        max_followers=max_followers or int(bcfg.get("max_followers") or 3000),
        max_birthday_check=int(bcfg.get("max_birthday_check") or 0),
        gap=gap or float(bcfg.get("gap_seconds") or 0.8),
    )
    save_birthday_cache(output or str(bcfg.get("cache_file") or "data/mutual_birthdays.json"), result)
    print(format_birthday_stats(result, include_users=True))
    return 0 if result.get("ok") else 1


def cmd_init(config_path: str) -> int:
    p = ensure_config(config_path)
    Path("data").mkdir(exist_ok=True)
    Path("logs").mkdir(exist_ok=True)
    print(f"配置文件已准备：{p}")
    print("请编辑配置文件，然后运行：python main.py login")
    return 0


def cmd_history(config_path: str, limit: int) -> int:
    cfg = load_config(config_path)
    state = BotState(cfg.state_file)
    history = state.data.get("history", [])[-limit:]
    print(json.dumps(history, ensure_ascii=False, indent=2))
    return 0


def _learning_db(config_path: str):
    cfg = load_config(config_path)
    learn_cfg = cfg.data.get("touhou_learning") or {}
    from bili_social_bot.touhou_learning import TouhouLearningDB
    return TouhouLearningDB(
        path=str(learn_cfg.get("memory_file") or "data/touhou_learning.json"),
        max_entries=int(learn_cfg.get("max_entries", 2000) or 2000),
    )


def cmd_learn_add(config_path: str, title: str, content: str, category: str, tags: str, source: str, official: bool) -> int:
    db = _learning_db(config_path)
    item = db.add(
        title=title,
        content=content,
        category=category,
        source=source,
        tags=[x.strip() for x in tags.replace("，", ",").split(",") if x.strip()],
        official=official,
        confidence="high" if official else "medium",
    )
    print("已学习：", json.dumps(item, ensure_ascii=False, indent=2))
    return 0


def cmd_learn_import(config_path: str, file_path: str, category: str) -> int:
    db = _learning_db(config_path)
    count = db.import_markdown(file_path, category=category, source="file")
    print(f"已从 {file_path} 导入 {count} 条东方知识。")
    return 0


def cmd_learn_search(config_path: str, query: str, limit: int) -> int:
    db = _learning_db(config_path)
    hits = db.search(query, limit=limit)
    print(json.dumps(hits, ensure_ascii=False, indent=2))
    return 0


def cmd_learn_summary(config_path: str, limit: int) -> int:
    db = _learning_db(config_path)
    print(db.summary(limit=limit))
    return 0


def cmd_reset_state(config_path: str) -> int:
    cfg = load_config(config_path)
    state_path = Path(cfg.state_file)
    if not state_path.is_absolute():
        state_path = Path.cwd() / state_path
    if state_path.exists():
        bak = state_path.with_suffix(state_path.suffix + ".bak")
        state_path.replace(bak)
        print(f"已备份并重置状态：{bak}")
    else:
        print("状态文件不存在，无需重置。")
    return 0


async def cmd_ai_test(config_path: str) -> int:
    cfg = load_config(config_path)
    from bili_social_bot.ai import test_text_connection
    try:
        result = await test_text_connection(cfg.data.get("ai") or {})
        print(result)
        return 0 if "成功" in result else 1
    except Exception as exc:
        print("文本AI连接测试失败：", exc)
        return 1


async def cmd_vision_test(config_path: str, image_path: str) -> int:
    cfg = load_config(config_path)
    from bili_social_bot.ai import test_vision_connection
    try:
        result = await test_vision_connection(cfg.data.get("ai") or {}, image_path)
        print(result)
        return 0 if result and "不可用" not in result else 1
    except Exception as exc:
        print("视觉AI连接测试失败：", exc)
        return 1


async def cmd_station_preflight(config_path: str, tribee_id: int, text: str, title: str, category_id: int) -> int:
    BiliClient = _load_bili_client_class()
    cfg = load_config(config_path)
    client = BiliClient(cfg.cookie_file, min_gap_seconds=0)
    try:
        if not client.load_cookies() or not await client.ensure_login():
            print("登录失效，无法执行小站预检")
            return 1
        result = await client.station_publish_preflight(tribee_id, sample_text=text, title=title, category_id=category_id or None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        await client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bili-social-bot",
        description="按用户授权规则定时评论 B站视频、并把视频链接私信发送给指定 UID。",
    )
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG_FILE, help="配置文件路径，默认 config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="从 config.example.json 生成 config.json")
    sub.add_parser("login", help="扫码登录 B站并保存 Cookie")
    sub.add_parser("check", help="检查 Cookie 与登录状态")

    p_once = sub.add_parser("once", help="执行一次到期任务")
    p_once.add_argument("--force", action="store_true", help="仅在 dry_run=true 时忽略 enabled=false 和定时条件，强制预览规则")

    sub.add_parser("run", help="常驻运行，按配置轮询定时任务")
    sub.add_parser("chat-once", help="执行一次私信/评论区互动回复检查")
    sub.add_parser("chat-run", help="常驻运行私信/评论区互动回复")
    sub.add_parser("health-check", help="执行一次评论/私信健康检查")
    sub.add_parser("health-run", help="常驻运行健康检查，出错通知指定UID")
    p_birth = sub.add_parser("birthdays-check", help="检测互关生日并输出统计名单")
    p_birth.add_argument("--max-followings", type=int, default=0)
    p_birth.add_argument("--max-followers", type=int, default=0)
    p_birth.add_argument("--gap", type=float, default=0.0)
    p_birth.add_argument("--output", default="")

    p_hist = sub.add_parser("history", help="查看最近动作历史")
    p_hist.add_argument("--limit", type=int, default=30)

    learn = sub.add_parser("learn", help="东方Project长期学习记忆库")
    learn_sub = learn.add_subparsers(dest="learn_command", required=True)
    p_add = learn_sub.add_parser("add", help="手动添加一条东方故事/梗知识")
    p_add.add_argument("--title", required=True, help="标题")
    p_add.add_argument("--content", required=True, help="内容摘要；不要粘贴大段官方原文")
    p_add.add_argument("--category", default="meme", choices=["story", "character", "meme", "bilibili_meme", "fanwork", "music", "comment_style", "safety"])
    p_add.add_argument("--tags", default="", help="逗号分隔标签")
    p_add.add_argument("--source", default="manual", help="来源说明")
    p_add.add_argument("--official", action="store_true", help="标记为官方资料摘要")

    p_imp = learn_sub.add_parser("import", help="从Markdown文件导入东方知识摘要")
    p_imp.add_argument("file", help="Markdown文件路径")
    p_imp.add_argument("--category", default="meme")

    p_search = learn_sub.add_parser("search", help="搜索已学习的东方知识")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=8)

    p_sum = learn_sub.add_parser("summary", help="查看最近学习摘要")
    p_sum.add_argument("--limit", type=int, default=40)

    sub.add_parser("reset-state", help="备份并清空去重状态；谨慎使用")
    sub.add_parser("ai-test", help="测试文本 AI 接口连接")
    p_vtest = sub.add_parser("vision-test", help="测试视觉 AI 接口连接：python main.py vision-test 图片路径")
    p_vtest.add_argument("image", help="本地图片路径，例如 data/media/xxx/cover.jpg")
    p_spf = sub.add_parser("station-preflight", help="小站发帖预检：只检查权限并打印 payload，不真实发布")
    p_spf.add_argument("--tribee-id", type=int, default=29)
    p_spf.add_argument("--text", default="小站发帖预检，不会发送")
    p_spf.add_argument("--title", default="接口预检")
    p_spf.add_argument("--category-id", type=int, default=252)

    danmaku = sub.add_parser("danmaku", help="弹幕 DLC：让机器人自己学躲避（可选模块）", add_help=False)
    danmaku.add_argument("danmaku_args", nargs=argparse.REMAINDER, help="转交给 danmaku_dlc.cli 的参数")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (_MODE_LOCK_ROOT / '.production-root.json').is_file():
        requested = Path(args.config)
        resolved = requested.resolve() if requested.is_absolute() else (_MODE_LOCK_ROOT / requested).resolve()
        if resolved.parent != _MODE_LOCK_ROOT:
            parser.error('仅允许使用生产目录里的配置，避免启动历史副本的独立状态。')
        args.config = str(resolved)
    if args.command == "init":
        return cmd_init(args.config)
    if args.command == "history":
        return cmd_history(args.config, args.limit)
    if args.command == "reset-state":
        return cmd_reset_state(args.config)
    if args.command == "ai-test":
        return asyncio.run(cmd_ai_test(args.config))
    if args.command == "vision-test":
        return asyncio.run(cmd_vision_test(args.config, args.image))
    if args.command == "station-preflight":
        return asyncio.run(cmd_station_preflight(args.config, args.tribee_id, args.text, args.title, args.category_id))
    if args.command == "learn":
        if args.learn_command == "add":
            return cmd_learn_add(args.config, args.title, args.content, args.category, args.tags, args.source, args.official)
        if args.learn_command == "import":
            return cmd_learn_import(args.config, args.file, args.category)
        if args.learn_command == "search":
            return cmd_learn_search(args.config, args.query, args.limit)
        if args.learn_command == "summary":
            return cmd_learn_summary(args.config, args.limit)
    if args.command == "danmaku":
        # 弹幕 DLC 是独立可选模块：没装/没用到时完全不影响主流程。
        from danmaku_dlc.cli import main as danmaku_main

        extra = list(getattr(args, "danmaku_args", []) or [])
        if extra and extra[0] == "--":
            extra = extra[1:]
        if not extra:
            extra = ["info"]
        return danmaku_main(extra)
    if args.command == "login":
        try:
            return asyncio.run(cmd_login(args.config))
        except Exception as exc:
            print("登录流程异常：", exc)
            print("可能原因：B站接口临时断连、代理/VPN不稳定、当前网络被风控。")
            print("可尝试：1) 关闭代理/VPN或换手机热点；2) 设置 $env:BILI_LOGIN_CHANNEL='WEB' 后重试；3) 稍后再试。")
            return 1
    if args.command == "check":
        return asyncio.run(cmd_check(args.config))
    if args.command == "once":
        return asyncio.run(cmd_once(args.config, args.force))
    if args.command == "run":
        return asyncio.run(cmd_run(args.config))
    if args.command == "chat-once":
        return asyncio.run(cmd_chat_once(args.config))
    if args.command == "chat-run":
        return asyncio.run(cmd_chat_run(args.config))
    if args.command == "health-check":
        return asyncio.run(cmd_health_check(args.config))
    if args.command == "health-run":
        return asyncio.run(cmd_health_run(args.config))
    if args.command == "birthdays-check":
        return asyncio.run(cmd_birthdays_check(args.config, args.max_followings, args.max_followers, args.gap, args.output))
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
