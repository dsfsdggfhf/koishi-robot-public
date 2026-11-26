"""Phone-friendly local control server; expose remotely only through Tailscale Serve."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import hmac
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit

from control_core import APP, ROOT, ControlCore


SESSION_SECONDS = 12 * 3600
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/catalog.js": ("catalog.js", "text/javascript; charset=utf-8"),
    "/gallery": ("gallery.html", "text/html; charset=utf-8"),
    "/gallery.css": ("gallery.css", "text/css; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/koishi-dance.png": ("koishi-dance.png", "image/png"),
    "/koishi-welcome.png": ("koishi-welcome.png", "image/png"),
}
for _art in (
    "interaction", "content", "learning", "media", "safety", "settings", "logs",
    "greetings", "persona", "writing", "station", "longlearning", "health",
    "swim-emerald", "swim-aqua", "swim-lilac", "aristocrat",
):
    STATIC[f"/koishi-{_art}.png"] = (f"koishi-{_art}.png", "image/png")


class Auth:
    def __init__(self, app: Path = APP, *, allow_create: bool = True):
        self.app = Path(app)
        self.verifier_file = self.app / "pairing_verifier.json"
        self.sessions: dict[str, dict] = {}
        self.failures: dict[str, list[float]] = {}
        self.lock = threading.Lock()
        self.first_code = ""
        self.viewer_salt = b""
        self.viewer_verifier = b""
        self._load_or_create(allow_create=allow_create)

    @staticmethod
    def _digest(code: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), salt, 200_000)

    def _load_or_create(self, *, allow_create: bool = True) -> None:
        if self.verifier_file.exists():
            saved = json.loads(self.verifier_file.read_text(encoding="utf-8"))
            self.salt = bytes.fromhex(saved["salt"])
            self.verifier = bytes.fromhex(saved["digest"])
            self.viewer_salt = bytes.fromhex(saved.get("viewer_salt", ""))
            self.viewer_verifier = bytes.fromhex(saved.get("viewer_digest", ""))
            return
        if not allow_create:
            raise RuntimeError("配对尚未初始化；请在电脑上手动启动控制台完成首次配对")
        self.app.mkdir(parents=True, exist_ok=True)
        self.first_code = secrets.token_urlsafe(18)
        self.salt = secrets.token_bytes(16)
        self.verifier = self._digest(self.first_code, self.salt)
        self._save_verifiers()

    def _save_verifiers(self) -> None:
        temporary = self.verifier_file.with_suffix(".tmp")
        temporary.write_text(json.dumps({"salt": self.salt.hex(), "digest": self.verifier.hex(),
            "viewer_salt": self.viewer_salt.hex(), "viewer_digest": self.viewer_verifier.hex()}), encoding="utf-8")
        temporary.replace(self.verifier_file)

    def login(self, code: str, address: str, *, read_only: bool = False,
              device_name: str = "") -> tuple[str, str]:
        if type(read_only) is not bool:
            raise ValueError("只读选项必须为布尔值")
        if not isinstance(device_name, str) or len(device_name) > 40 or any(ord(c) < 32 for c in device_name):
            raise ValueError("设备名称最多40字，不能包含换行")
        now = time.monotonic()
        with self.lock:
            recent = [t for t in self.failures.get(address, []) if now - t < 600]
            if len(recent) >= 5:
                raise PermissionError("尝试过多，请 10 分钟后再试")
            candidate = self._digest(code[:128], self.salt)
            admin_ok = hmac.compare_digest(candidate, self.verifier)
            viewer_ok = bool(self.viewer_verifier) and hmac.compare_digest(
                self._digest(code[:128], self.viewer_salt), self.viewer_verifier)
            if not (admin_ok or viewer_ok):
                recent.append(now)
                self.failures[address] = recent
                raise PermissionError("配对码不正确")
            self.failures.pop(address, None)
            self.sessions = {token: entry for token, entry in self.sessions.items() if entry['expires'] > now}
            if len(self.sessions) >= 32:
                raise PermissionError("登录设备较多，请先退出或撤销旧设备")
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            self.sessions[token] = {'csrf': csrf, 'expires': now + SESSION_SECONDS,
                'id': secrets.token_urlsafe(18), 'read_only': read_only or not admin_ok,
                'device_name': device_name.strip() or '未命名设备', 'created_at': time.time()}
            return token, csrf

    def session(self, token: str) -> str:
        with self.lock:
            entry = self.sessions.get(token)
            if not entry:
                return ""
            if time.monotonic() > entry['expires']:
                self.sessions.pop(token, None)
                return ""
            return entry['csrf']

    def read_only(self, token: str) -> bool:
        with self.lock:
            return bool(self.sessions.get(token, {}).get('read_only', True))

    def devices(self, token: str) -> list[dict]:
        if not self.session(token) or self.read_only(token):
            raise PermissionError("需要管理会话")
        with self.lock:
            now = time.monotonic()
            return [{key: entry[key] for key in ('id', 'device_name', 'read_only', 'created_at')}
                    | {'current': other == token, 'expires_in_seconds': max(0, int(entry['expires'] - now))}
                    for other, entry in self.sessions.items() if entry['expires'] > now]

    def revoke(self, token: str, device_id: str) -> bool:
        if not self.session(token) or self.read_only(token):
            raise PermissionError("需要管理会话")
        with self.lock:
            for other, entry in list(self.sessions.items()):
                if hmac.compare_digest(entry['id'], str(device_id)):
                    self.sessions.pop(other, None)
                    return True
        return False

    def logout(self, token: str) -> None:
        with self.lock:
            self.sessions.pop(token, None)

    def reset(self) -> str:
        """Rotate the pairing verifier after the local server has been stopped."""
        self.first_code = secrets.token_urlsafe(18)
        self.salt = secrets.token_bytes(16)
        self.verifier = self._digest(self.first_code, self.salt)
        self._save_verifiers()
        self.sessions.clear()
        return self.first_code

    def reset_viewer(self) -> str:
        code = secrets.token_urlsafe(18)
        self.viewer_salt = secrets.token_bytes(16)
        self.viewer_verifier = self._digest(code, self.viewer_salt)
        self._save_verifiers()
        self.sessions = {token: entry for token, entry in self.sessions.items() if not entry['read_only']}
        return code


def valid_host(host: str) -> bool:
    host = host.lower().split(":", 1)[0].strip("[]")
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".ts.net")


def make_handler(core: ControlCore, auth: Auth):
    class Handler(BaseHTTPRequestHandler):
        server_version = "KoishiControl"
        sys_version = ""

        def log_message(self, fmt: str, *args) -> None:
            # Avoid logging cookies, query strings, or private data.
            pass

        def _headers(self, status: int, content_type: str, length: int,
                     cookie: str = "", cache_control: str = "no-store") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", cache_control)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()

        def _json(self, status: int, data: dict) -> None:
            raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(raw))
            self.wfile.write(raw)

        def _cookie_token(self) -> str:
            try:
                jar = cookies.SimpleCookie()
                jar.load(self.headers.get("Cookie", ""))
                return jar["koishi_session"].value if "koishi_session" in jar else ""
            except cookies.CookieError:
                return ""

        def _csrf(self) -> str:
            return auth.session(self._cookie_token())

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin", "")
            if not origin:
                return False
            parsed = urlsplit(origin)
            host = self.headers.get("Host", "").lower()
            return parsed.netloc.lower() == host and parsed.scheme in {"http", "https"}

        def _body(self) -> dict:
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise ValueError("只接受 JSON 请求")
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 8192:
                raise ValueError("请求内容过长或为空")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("请求内容必须是对象")
            return data

        def _check_host(self) -> bool:
            if not valid_host(self.headers.get("Host", "")):
                self._json(403, {"error": "访问地址不受信任"})
                return False
            return True

        def do_GET(self) -> None:
            if not self._check_host():
                return
            uri = urlsplit(self.path)
            if uri.path in STATIC:
                name, mime = STATIC[uri.path]
                raw = (core.app / name).read_bytes()
                self._headers(200, mime, len(raw), cache_control="private, max-age=86400")
                self.wfile.write(raw)
                return
            if uri.path == "/api/session":
                csrf = self._csrf()
                self._json(200, {"logged_in": bool(csrf), "csrf": csrf,
                                 "read_only": auth.read_only(self._cookie_token())})
                return
            if not self._csrf():
                self._json(401, {"error": "请先输入配对码"})
                return
            try:
                if uri.path == "/api/status":
                    result = core.status()
                elif uri.path == "/api/features":
                    result = {"features": core.features()}
                elif uri.path == "/api/settings":
                    result = core.settings()
                elif uri.path == "/api/logs":
                    which = parse_qs(uri.query).get("which", ["supervisor"])[0]
                    result = {"lines": core.log_tail(which)}
                elif uri.path == "/api/sessions":
                    if auth.read_only(self._cookie_token()):
                        self._json(403, {"error": "只读会话不能查看其他设备"})
                        return
                    result = {"devices": auth.devices(self._cookie_token())}
                else:
                    self._json(404, {"error": "页面不存在"})
                    return
                self._json(200, result)
            except (OSError, ValueError, RuntimeError) as exc:
                self._json(500, {"error": f"读取失败：{exc}"})

        def do_POST(self) -> None:
            if not self._check_host():
                return
            if not self._same_origin():
                self._json(403, {"error": "请求来源不受信任"})
                return
            uri = urlsplit(self.path)
            try:
                data = self._body()
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
                return
            if uri.path == "/api/login":
                try:
                    token, csrf = auth.login(str(data.get("code", "")), self.client_address[0],
                        read_only=data.get('read_only', False), device_name=data.get('device_name', ''))
                except PermissionError as exc:
                    self._json(401, {"error": str(exc)})
                    return
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                host_name = self.headers.get("Host", "").lower().split(":", 1)[0]
                secure = (self.headers.get("X-Forwarded-Proto", "").lower() == "https"
                          or host_name.endswith(".ts.net"))
                cookie = f"koishi_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}"
                if secure:
                    cookie += "; Secure"
                raw = json.dumps({"ok": True, "csrf": csrf, "read_only": auth.read_only(token)}).encode("utf-8")
                self._headers(200, "application/json; charset=utf-8", len(raw), cookie)
                self.wfile.write(raw)
                return
            csrf = self._csrf()
            if not csrf:
                self._json(401, {"error": "登录已过期"})
                return
            if not hmac.compare_digest(self.headers.get("X-CSRF", ""), csrf):
                self._json(403, {"error": "安全校验未通过"})
                return
            if uri.path != '/api/logout' and auth.read_only(self._cookie_token()):
                self._json(403, {"error": "当前为只读会话，请退出后重新以管理模式登录"})
                return
            try:
                if uri.path == "/api/logout":
                    auth.logout(self._cookie_token())
                    raw = b'{"ok":true}'
                    self._headers(200, "application/json; charset=utf-8", len(raw),
                                  "koishi_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
                    self.wfile.write(raw)
                    return
                if uri.path == "/api/feature":
                    result = core.set_feature(data.get("path"), data.get("enabled"))
                elif uri.path == "/api/setting":
                    result = core.set_setting(data.get("key"), data.get("value"))
                elif uri.path == "/api/robot":
                    result = core.operate(data.get("action"))
                elif uri.path == "/api/session/revoke":
                    result = {"ok": auth.revoke(self._cookie_token(), str(data.get('device_id', '')))}
                else:
                    self._json(404, {"error": "操作不存在"})
                    return
                self._json(200, result)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except (OSError, RuntimeError) as exc:
                self._json(409, {"error": str(exc)})

    return Handler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Koishi 手机控制台")
    parser.add_argument("--port", type=int, default=8765)
    reset = parser.add_mutually_exclusive_group()
    reset.add_argument("--reset-code", action="store_true", help="停止控制台后重置管理配对码")
    reset.add_argument("--reset-viewer-code", action="store_true", help="停止控制台后生成或重置独立只读配对码")
    parser.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("端口必须在 1024～65535 之间")
    auth = Auth(allow_create=not args.background)
    if args.reset_code or args.reset_viewer_code:
        try:
            probe = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(ControlCore(), auth))
        except OSError:
            print("控制台仍在运行，请先关闭控制台窗口再重置配对码。", flush=True)
            return 1
        probe.server_close()
        if args.reset_viewer_code:
            print(f"新只读配对码：{auth.reset_viewer()}（请保存）", flush=True)
        else:
            print(f"新配对码：{auth.reset()}（请保存）", flush=True)
        return 0
    core = ControlCore()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(core, auth))
    server.daemon_threads = True
    print(f"手机控制台本机地址：http://127.0.0.1:{args.port}", flush=True)
    if auth.first_code:
        print(f"首次配对码：{auth.first_code}（只显示这一次，请保存）", flush=True)
    print("外出访问请用 Tailscale Serve；控制台不会直接监听公网或局域网。", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
