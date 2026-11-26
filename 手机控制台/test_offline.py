"""Offline API checks; never starts or stops the social bot."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
from http.server import ThreadingHTTPServer

from control_core import ControlCore, _expected_process
from server import APP, STATIC, Auth, make_handler


def main() -> None:
    assert all((APP / name).is_file() for name, _mime in STATIC.values())
    assert _expected_process('python "D:\\x\\main.py" run', "run")
    assert not _expected_process('python "D:\\x\\unrelated.py" run', "run")
    with tempfile.TemporaryDirectory(prefix="koishi_mobile_test_") as tmp:
        root = Path(tmp) / "bot"
        app = Path(tmp) / "app"
        root.mkdir()
        app.mkdir()
        (root / "config.json").write_text(json.dumps({
            "api_key": "secret-must-not-leak",
            "dry_run": True,
            "daily_comment_limit": 5,
            "module": {"enabled": True, "password": "secret-two"},
        }), encoding="utf-8")
        core = ControlCore(root, app, lambda cfg: {"updated": "2026-09-26T12:00:00", "daily": {"comments": 2}, "alerts": []})
        auth = Auth(app)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(core, auth))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        port = server.server_address[1]
        origin = f"http://127.0.0.1:{port}"
        cookie = ""
        csrf = ""

        def call(method: str, route: str, data=None, *, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            request_headers = {"Host": f"127.0.0.1:{port}"}
            if cookie:
                request_headers["Cookie"] = cookie
            if method == "POST":
                request_headers.update({"Origin": origin, "Content-Type": "application/json"})
            if headers:
                request_headers.update(headers)
            conn.request(method, route, body=json.dumps(data).encode() if data is not None else None, headers=request_headers)
            response = conn.getresponse()
            raw = response.read()
            result = json.loads(raw) if raw else {}
            set_cookie = response.getheader("Set-Cookie", "")
            status = response.status
            conn.close()
            return status, result, set_cookie

        try:
            assert call("GET", "/api/features")[0] == 401
            assert call("GET", "/api/session")[1]["logged_in"] is False
            assert call("POST", "/api/login", {"code": "bad"})[0] == 401
            status, result, set_cookie = call("POST", "/api/login", {"code": auth.first_code})
            assert status == 200 and result["ok"]
            cookie, csrf = set_cookie.split(";", 1)[0], result["csrf"]
            assert "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie
            status, _result, remote_cookie = call("POST", "/api/login", {"code": auth.first_code}, headers={
                "Host": "koishi.example.ts.net", "Origin": "https://koishi.example.ts.net",
            })
            assert status == 200 and "Secure" in remote_cookie
            for route in ("/api/features", "/api/settings", "/api/status"):
                status, result, _ = call("GET", route)
                assert status == 200 and "secret-must-not-leak" not in json.dumps(result)
                assert "secret-two" not in json.dumps(result)
            assert call("POST", "/api/feature", {"path": "module", "enabled": False})[0] == 403
            assert call("POST", "/api/feature", {"path": "module", "enabled": False}, headers={"X-CSRF": csrf, "Origin":"https://evil.example"})[0] == 403
            assert call("GET", "/api/status", headers={"Host":"evil.example"})[0] == 403
            assert call("POST", "/api/feature", {"path": "module.password", "enabled": False}, headers={"X-CSRF": csrf})[0] == 400
            status, result, _ = call("POST", "/api/feature", {"path": "module", "enabled": False}, headers={"X-CSRF": csrf})
            assert status == 200 and result["changed"]
            assert json.loads((root / "config.json").read_text(encoding="utf-8"))["module"]["enabled"] is False
            assert list((app / "backups").glob("config_*.json"))
            assert call("POST", "/api/setting", {"key":"daily_comment_limit", "value":201}, headers={"X-CSRF":csrf})[0] == 400
            print("通过：本地登录、会话、CSRF、来源限制、配置白名单、备份和密钥不泄漏。")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == "__main__":
    main()
