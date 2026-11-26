"""SQLite-backed account pacing for every Bilibili write on one host."""
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sqlite3

from .greeting_delivery import is_greeting_risk_error

TZ = timezone(timedelta(hours=8))


class AccountSendBlocked(RuntimeError):
    """No platform write was attempted."""


class AccountOutbound:
    def __init__(self, root: Path, uid: str):
        self.root = root
        self.uid = uid

    def reserve(self, now: float, gap: float, daily_limit: int) -> float:
        if (self.root / "outbound.hold").exists():
            raise AccountSendBlocked("账号发送已手动暂停")
        self.root.mkdir(parents=True, exist_ok=True)
        day = datetime.fromtimestamp(now, TZ).date().isoformat()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT day, count, after, pause FROM account WHERE uid=?", (self.uid,)).fetchone()
            old_day, count, after, pause = row or ("", 0, 0.0, 0.0)
            if pause > now:
                raise AccountSendBlocked("账号发送处于平台保护冷却")
            if after > now:
                return after - now
            count = count if day == old_day else 0
            if count >= daily_limit:
                raise AccountSendBlocked("账号今日发送尝试达到上限")
            db.execute("INSERT OR REPLACE INTO account VALUES(?,?,?,?,?)",
                       (self.uid, day, count + 1, now + gap, pause))
            return 0.0

    def observe_error(self, error: BaseException, now: float) -> None:
        if not is_greeting_risk_error(error):
            return
        with closing(self._connect()) as db, db:
            db.execute("UPDATE account SET pause=MAX(pause,?) WHERE uid=?", (now + 600, self.uid))

    def _connect(self):
        db = sqlite3.connect(self.root / "outbound_accounts.sqlite3", timeout=5)
        db.execute("""CREATE TABLE IF NOT EXISTS account(
            uid TEXT PRIMARY KEY, day TEXT NOT NULL, count INTEGER NOT NULL,
            after REAL NOT NULL, pause REAL NOT NULL)""")
        return db
