# -*- coding: utf-8 -*-
"""生日贺卡/生日小故事的状态汇总（给 launcher 与自查脚本共用，只有这一份逻辑）。

collect() 收集，format_report() 出详细报告，format_brief() 出一行摘要。
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _today() -> str:
    return date.today().isoformat()


def birthday_people(today: str | None = None) -> list[dict[str, Any]]:
    """今天过生日的互关（来自 data/mutual_birthdays.json）。"""
    today = today or _today()
    md = today[5:]
    try:
        raw = json.loads((ROOT / "data/mutual_birthdays.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    return [u for u in (raw.get("users") or [])
            if str(u.get("birthday_md") or u.get("birthday") or "")[5:] == md
            or str(u.get("birthday_md") or "") == md]


def running_processes() -> list[str]:
    """在跑的机器人进程（python* 且命令行含 main.py/supervisor）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoLogo", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and "
             "$_.CommandLine -match 'main.py|supervisor' } | "
             "ForEach-Object { \"$($_.Name)|$($_.ProcessId)\" }"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=40).stdout or ""
        return [x.strip() for x in out.splitlines() if x.strip()]
    except Exception:
        return []


def _log_lines(today: str) -> list[str]:
    p = ROOT / "logs/bot.log"
    if not p.exists():
        return []
    txt = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return [l for l in txt if l.startswith(f"[{today}")]


def collect(today: str | None = None) -> dict[str, Any]:
    today = today or _today()
    lines = _log_lines(today)

    def pick(*keys: str) -> list[str]:
        return [l.strip() for l in lines if any(k in l for k in keys)]

    story = pick("生日小故事开始发送", "生日小故事发送失败", "生日小故事跳过")
    wish = pick("生日祝福文案：", "生日祝福（", "生日祝福所有模型")
    card = pick("生日贺卡已发送", "生日贺卡发送失败", "取生日者头像失败",
                "[DRY-RUN] 将发送生日贺卡")
    files = []
    d = ROOT / "data/birthday_cards"
    if d.exists():
        files = sorted(d.glob("*.jpg"), key=lambda p: p.stat().st_mtime)[-6:]
    history = []
    try:
        st = json.loads((ROOT / "data/state.json").read_text(encoding="utf-8"))
        history = [h for h in (st.get("history") or [])
                   if str(h.get("time", "")).startswith(today)
                   and str(h.get("action")) in ("birthday_story_sent", "birthday_card_sent")]
    except Exception:
        pass
    procs = running_processes()
    sent = [l for l in card if "已发送" in l]
    fails = [l for l in card if "失败" in l] + [l for l in wish if "失败" in l or "所有模型" in l]
    people = birthday_people(today)
    try:
        cache = json.loads((ROOT / 'data/mutual_birthdays.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cache = {}
    scan_incomplete = not cache.get('birthday_scan_complete', False) or bool(cache.get('birthday_query_failed_count'))
    return {"today": today, "procs": procs, "people": people, "story": story, "wish": wish,
            "card": card, "files": files, "history": history, "sent": sent, "fails": fails, "scan_incomplete": scan_incomplete}


def _verdict(info: dict[str, Any]) -> tuple[str, str]:
    if info["sent"] and not info["fails"]:
        return "OK", f"✓ 贺卡已正常发出（今天 {len(info['sent'])} 张）"
    if info["sent"] and info["fails"]:
        return "WARN", f"△ 发出 {len(info['sent'])} 张，另有 {len(info['fails'])} 条告警"
    if not info["procs"]:
        return "DOWN", "✗ 机器人没在运行（双击 启动机器人.bat）"
    if info.get('scan_incomplete') and not info['people']:
        return 'WARN', '△ 生日资料查询不完整，不能断定今天无人生日；保留已知生日并等待退避重试'
    if not info["story"]:
        return "IDLE", ("今天没有过生日的互关" if not info["people"]
                        else f"今天 {len(info['people'])} 位生日，但还没到 10:00 / 还没跑")
    return "WARN", "△ 小故事跑了但没看到贺卡记录，看下面的告警"


def format_brief(today: str | None = None) -> str:
    """一行摘要，给 launcher 的状态窗口用。"""
    info = collect(today)
    lvl, text = _verdict(info)
    extra = ""
    if info["people"]:
        names = "、".join(str(p.get("uname") or p.get("mid")) for p in info["people"][:3])
        extra = f"　今日寿星：{names}"
    return f"生日贺卡：{text}{extra}"


def format_report(today: str | None = None) -> str:
    info = collect(today)
    L: list[str] = [f"===== 生日贺卡自查  {info['today']} =====", ""]
    L.append(f"[1] 机器人进程：{len(info['procs'])} 个")
    for p in info["procs"]:
        L.append(f"      {p}")
    if not info["procs"]:
        L.append("      ✗ 机器人没在运行 —— 今天 10:00 不会发贺卡")

    people = info["people"]
    L.append(f"\n[2] 今天过生日的互关：{len(people)} 位")
    for p in people:
        L.append(f"      {p.get('uname')}  uid={p.get('mid')}  生日={p.get('birthday_md') or p.get('birthday')}")

    L.append(f"\n[3] 生日小故事（今天 {len(info['story'])} 条）")
    L += [f"      {l[:150]}" for l in info["story"][-4:]]

    L.append(f"\n[4] 祝福文案生成（今天 {len(info['wish'])} 条）")
    L += [f"      {l[:150]}" for l in info["wish"][-6:]]

    L.append(f"\n[5] 贺卡发送（今天 {len(info['card'])} 条）")
    L += [f"      {l[:170]}" for l in info["card"][-8:]]

    L.append("\n[6] 生成的贺卡图片")
    if not info["files"]:
        L.append("      （还没有文件）")
    for f in info["files"]:
        tag = "  ← 今天" if info["today"].replace("-", "") in f.name else ""
        L.append(f"      {f.name}  {f.stat().st_size // 1024} KB{tag}")

    L.append("\n[7] state.history 生日记录")
    if not info["history"]:
        L.append("      （今天还没有）")
    for h in info["history"][-6:]:
        L.append("      " + json.dumps(h, ensure_ascii=False)[:190])

    lvl, text = _verdict(info)
    L += ["", "=" * 46, f"结论：{text}"]
    return "\n".join(L)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(format_report())
