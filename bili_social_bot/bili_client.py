from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import sqlite3
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx
import urllib.parse
from bilibili_api import Credential, comment, favorite_list, session as bili_session, user, video
from bilibili_api.comment import CommentResourceType
from bilibili_api.utils.picture import Picture

from .utils import ensure_parent, extract_bvid, mask_secret, resolve_path
from .outbound_guard import AccountOutbound, AccountSendBlocked


@dataclass
class VideoItem:
    bvid: str
    aid: int
    title: str
    author: str = ""
    duration: int = 0
    desc: str = ""
    url: str = ""
    cid: int = 0
    pic: str = ""
    tags: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class StationPostItem:
    dyn_id: str
    title: str
    author: str = ""
    time_text: str = ""
    reply_count: int = 0
    url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class OutboundDeliveryUnknown(RuntimeError):
    """The platform may have accepted an outbound message but the result is unknown."""


_MAX_IMAGE_DOWNLOAD_BYTES = 6 * 1024 * 1024
_ALLOWED_IMAGE_HOSTS = {
    "hdslb.com",
    "bilivideo.com",
    "bilibili.com",
    "biliimg.com",  # 私信图片常见域名：message.biliimg.com
}


def _validated_bili_image_url(raw_url: str) -> str:
    """Only accept HTTPS Bilibili/BFS image hosts; callers must not send cookies."""
    value = str(raw_url or "").strip()
    if value.startswith("//"):
        value = "https:" + value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("图片地址必须是 HTTP/HTTPS")
    host = parsed.hostname.rstrip(".").lower()
    if not any(host == allowed or host.endswith("." + allowed) for allowed in _ALLOWED_IMAGE_HOSTS):
        raise ValueError(f"图片地址不在 B站图片域名白名单：{host}")
    # B站接口经常返回 http://i*.hdslb.com 封面；只对已验证白名单域名升级为 HTTPS。
    if parsed.scheme.lower() == "http":
        parsed = parsed._replace(scheme="https")
    if parsed.username or parsed.password:
        raise ValueError("图片地址不允许携带用户信息")
    try:
        if parsed.port is not None:
            raise ValueError("图片地址不允许显式端口")
    except ValueError:
        raise ValueError("图片地址端口无效或不允许显式端口")
    return urllib.parse.urlunparse(parsed._replace(fragment=""))


def _is_definite_comment_rejection(exc: BaseException) -> bool:
    text = str(exc or "")
    return any(marker in text for marker in (
        "12002", "12004", "评论功能已关闭", "评论区已关闭",
        "评论内容不能为空", "非法参数", "权限不足",
    ))


def _is_definite_private_rejection(exc: BaseException) -> bool:
    text = str(exc or "")
    return any(marker in text for marker in (
        "21047", "21007", "最多发送1条消息", "消息过长", "非法参数", "用户不存在",
        "无法向该用户发送消息", "消息内容不能为空", "权限不足",
    ))


def _is_pre_send_network_error(exc: BaseException) -> bool:
    """Network errors where the request very likely never reached B站; safe to retry later."""
    text = str(exc or "").lower()
    cls = type(exc).__name__.lower()
    needles = (
        "connecterror", "connecttimeout", "pooltimeout", "networkerror",
        "getaddrinfo failed", "nodename nor servname", "name or service not known",
        "temporary failure in name resolution", "actively refused", "connection refused",
        "could not connect", "all connection attempts failed", "network is unreachable",
        "连接尝试失败", "由于目标计算机积极拒绝", "不知道这样的主机",
    )
    if any(x in cls or x in text for x in needles):
        return True
    try:
        import httpx as _httpx
        return isinstance(exc, (_httpx.ConnectError, _httpx.ConnectTimeout, _httpx.PoolTimeout, _httpx.NetworkError))
    except Exception:
        return False


class BiliClient:
    """Small Bilibili client inspired by the reference project.

    It deliberately exposes only the operations needed by this assistant:
    login checking, video search/meta, video comments and text DM sharing.
    """

    def __init__(self, cookie_file: str, min_gap_seconds: int = 2):
        # 与机器人自带 httpx 客户端保持一致：忽略 Windows 系统代理（trust_env=False）。
        # bilibili-api 默认 trust_env=True，会在客户端创建时读取系统代理；若用户中途
        # 开关代理软件（如 Clash 127.0.0.1:7890），长驻进程的客户端会一直走已失效的代理，
        # 造成"登录/搜索正常但会话/评论/私信/粉丝检查全部连接失败"的假离线。
        # 这里在启动时全局关闭 trust_env，让 B站请求行为确定，不随系统代理开关漂移。
        try:
            from bilibili_api import request_settings
            request_settings.set_trust_env(False)
        except Exception:
            pass
        self.cookie_file = resolve_path(cookie_file)
        self.min_gap_seconds = max(0, min_gap_seconds)
        self.credential: Credential | None = None
        self.cookies: dict[str, str] = {}
        self.uid: int | None = None
        self._http: httpx.AsyncClient | None = None
        self._last_request = 0.0
        self.last_login_error = ""

    async def close(self) -> None:
        if self._http and not self._http.is_closed:
            await self._http.aclose()
        self._http = None

    async def throttle(self) -> None:
        if self.min_gap_seconds <= 0:
            return
        now = asyncio.get_event_loop().time()
        wait = self.min_gap_seconds - (now - self._last_request)
        if wait > 0:
            await asyncio.sleep(wait + random.uniform(0.1, 0.8))
        self._last_request = asyncio.get_event_loop().time()

    async def _outbound(self, send):
        # Only a platform-verified account can share pending write limits.
        uid = str(getattr(self, "uid", "") or "")
        if not uid.isdigit():
            return await send()
        guard = AccountOutbound(resolve_path("data"), uid)
        while True:
            delay = await asyncio.to_thread(guard.reserve, time.time(),
                                            max(2, self.min_gap_seconds), 1500)
            if delay <= 0:
                break
            await asyncio.sleep(delay)
        if (guard.root / "outbound.hold").exists():
            raise AccountSendBlocked("账号发送已手动暂停")
        try:
            return await send()
        except Exception as exc:
            try:
                await asyncio.to_thread(guard.observe_error, exc, time.time())
            except (OSError, sqlite3.Error):
                pass
            raise

    def load_cookies(self) -> bool:
        if not self.cookie_file.exists():
            return False
        try:
            data = json.loads(self.cookie_file.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        self.cookies = {str(k): str(v) for k, v in data.items() if v is not None}
        sessdata = self.cookies.get("SESSDATA") or self.cookies.get("sessdata", "")
        bili_jct = self.cookies.get("bili_jct") or self.cookies.get("BILI_JCT", "")
        dede = self.cookies.get("DedeUserID") or self.cookies.get("dedeuserid", "")
        buvid3 = self.cookies.get("buvid3", "")
        # buvid3 存在多种合法格式（UUID+infoc / 数字点分隔+infoc 等）。
        # 用宽松校验：只要非空、以 infoc 结尾、且前面是合理的设备标识符就保留，
        # 避免误判合法 buvid3 为无效而每次启动都替换（换指纹可能诱发 412/-509）。
        if buvid3 and not re.match(r"^[0-9a-fA-F.-]{20,50}infoc$", buvid3):
            buvid3 = ""
        if not buvid3:
            buvid3 = str(uuid.uuid1()) + "infoc"
            self.cookies["buvid3"] = buvid3
            try:
                p = ensure_parent(self.cookie_file)
                p.write_text(json.dumps(self.cookies, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
        if not sessdata or not bili_jct or not dede:
            return False
        self.credential = Credential(
            sessdata=sessdata,
            bili_jct=bili_jct,
            buvid3=buvid3,
            dedeuserid=dede,
        )
        try:
            self.uid = int(dede)
        except Exception:
            self.uid = None
        return True

    def save_cookies_from_credential(self, credential: Credential) -> None:
        cookies = {
            "SESSDATA": getattr(credential, "sessdata", "") or getattr(credential, "SESSDATA", ""),
            "bili_jct": getattr(credential, "bili_jct", "") or getattr(credential, "BILI_JCT", ""),
            "DedeUserID": getattr(credential, "dedeuserid", "") or getattr(credential, "DedeUserID", ""),
            "buvid3": getattr(credential, "buvid3", ""),
            "ac_time_value": getattr(credential, "ac_time_value", ""),
        }
        p = ensure_parent(self.cookie_file)
        p.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        self.cookies = cookies
        self.credential = credential

    async def ensure_login(self) -> bool:
        if not self.credential and not self.load_cookies():
            self.last_login_error = "Cookie 文件不存在或缺少 SESSDATA/bili_jct/DedeUserID"
            return False
        try:
            await self.throttle()
            info = await user.get_self_info(self.credential)
            self.uid = int(info.get("mid") or self.uid or 0)
            self.last_login_error = ""
            return True
        except Exception as exc:
            # bilibili-api-python 的 user.get_self_info 偶尔会因版本/风控失败；
            # 再用网页 nav 接口做一次备用验证，便于判断 Cookie 是否真的有效。
            self.last_login_error = str(exc)
            try:
                await self.throttle()
                client = await self._http_client()
                resp = await client.get("https://api.bilibili.com/x/web-interface/nav", cookies=self.cookies or None)
                data = _safe_json(resp, "B站nav接口")
                if data.get("code") == 0 and (data.get("data") or {}).get("isLogin"):
                    self.uid = int((data.get("data") or {}).get("mid") or self.uid or 0)
                    self.last_login_error = ""
                    return True
                self.last_login_error = f"nav接口返回：code={data.get('code')} message={data.get('message')} isLogin={(data.get('data') or {}).get('isLogin')}"
            except Exception as exc2:
                self.last_login_error += f"；备用nav验证也失败：{exc2}"
            return False

    async def login_qrcode(self, timeout_seconds: int = 180) -> bool:
        from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents, QrCodeLoginChannel
        import qrcode

        # WEB 渠道在部分 bilibili-api-python / B站接口组合下可能只返回 refresh_token，
        # 导致 SESSDATA ***REDACTED*** 渠道通常更完整，但在部分网络/代理下可能断连。
        channel_name = os.environ.get("BILI_LOGIN_CHANNEL", "TV").strip().upper()
        channel = QrCodeLoginChannel.WEB if channel_name == "WEB" else QrCodeLoginChannel.TV
        qr = QrCodeLogin(channel)
        try:
            await qr.generate_qrcode()
        except Exception as exc:
            if channel == QrCodeLoginChannel.TV:
                print(f"TV登录二维码获取失败，自动切换WEB渠道重试：{exc}")
                qr = QrCodeLogin(QrCodeLoginChannel.WEB)
                await qr.generate_qrcode()
            else:
                raise
        url = qr._QrCodeLogin__qr_link  # same stable private fields used by reference project
        qr_dir = Path("qr_codes")
        qr_dir.mkdir(exist_ok=True)
        qr_path = qr_dir / "bilibili_login_qr.png"
        img = qrcode.make(url)
        img.save(qr_path)
        print(f"请用 B站 APP 扫码登录：{qr_path.resolve()}")
        try:
            preview = qrcode.QRCode(border=1)
            preview.add_data(url)
            preview.make(fit=True)
            preview.print_ascii(invert=True)
        except Exception:
            pass

        started = asyncio.get_event_loop().time()
        last_msg = ""
        errors = 0
        while True:
            if asyncio.get_event_loop().time() - started > timeout_seconds:
                print("\n登录等待超时，请重新运行 python main.py login 生成新二维码。")
                return False
            try:
                status = await qr.check_state()
                errors = 0
            except Exception as exc:
                errors += 1
                if errors >= 5:
                    print(f"\n二维码状态检查连续失败：{exc}")
                    return False
                await asyncio.sleep(3)
                continue
            if status == QrCodeLoginEvents.DONE:
                cred = qr.get_credential()
                # 先校验 SESSDATA ***REDACTED*** 渠道偶尔只返回 refresh_token（SESSDATA ***REDACTED***
                # 此时写文件会覆盖掉原本有效的 Cookie，导致机器人掉线。
                if not getattr(cred, "sessdata", None):
                    print("扫码完成，但未拿到 SESSDATA（可能走了 WEB 渠道）。")
                    print("已保留原有 Cookie，请设置 $env:BILI_LOGIN_CHANNEL='TV' 后重试，或更新 bilibili-api-python。")
                    return False
                self.save_cookies_from_credential(cred)
                print("\n登录成功，Cookie 已保存。")
                return True
            if status == QrCodeLoginEvents.TIMEOUT:
                print("\n二维码已过期，请重新运行 login。")
                return False
            msg = "已扫码，请在手机上确认..." if status == QrCodeLoginEvents.CONF else "等待扫码..."
            if msg != last_msg:
                print(msg)
                last_msg = msg
            await asyncio.sleep(2)

    async def _http_client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=True,
                trust_env=False,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                    "Referer": "https://www.bilibili.com/",
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Origin": "https://www.bilibili.com",
                },
            )
        return self._http

    async def search_videos(self, keyword: str, page: int = 1, max_results: int = 10, order: str = "pubdate") -> list[VideoItem]:
        await self.throttle()
        client = await self._http_client()
        order = str(order or "pubdate").strip() or "pubdate"
        try:
            resp = await client.get(
                "https://api.bilibili.com/x/web-interface/search/type",
                params={"keyword": keyword, "search_type": "video", "page": page, "order": order},
                cookies=self.cookies or None,
            )
            resp.raise_for_status()
            data = _safe_json(resp, "B站搜索接口")
            if data.get("code") not in (0, "0", None):
                raise RuntimeError(f"B站搜索接口失败：code={data.get('code')} message={data.get('message')}")
            results = ((data.get("data") or {}).get("result") or [])[:max_results]
        except Exception as exc:
            # /search/type 很容易被 412；退回 /search/all/v2，实测更稳定。
            fallback_error = str(exc)
            await self.throttle()
            resp = await client.get(
                "https://api.bilibili.com/x/web-interface/search/all/v2",
                params={"keyword": keyword, "page": page, "order": order},
                cookies=self.cookies or None,
            )
            resp.raise_for_status()
            data = _safe_json(resp, "B站搜索备用接口")
            if data.get("code") not in (0, "0", None):
                raise RuntimeError(f"B站搜索接口失败：{fallback_error}；备用接口 code={data.get('code')} message={data.get('message')}")
            results = _extract_video_results_from_search_all(data)[:max_results]
        items: list[VideoItem] = []
        for row in results:
            bvid = row.get("bvid") or extract_bvid(row.get("arcurl", ""))
            if not bvid:
                continue
            title = str(row.get("title") or "").replace("<em class=\"keyword\">", "").replace("</em>", "")
            duration = _duration_to_seconds(row.get("duration"))
            items.append(VideoItem(
                bvid=bvid,
                aid=int(row.get("aid") or 0),
                title=title,
                author=str(row.get("author") or ""),
                duration=duration,
                desc=str(row.get("description") or ""),
                url=f"https://www.bilibili.com/video/{bvid}",
                pic=str(row.get("pic") or ""),
                raw=row,
            ))
        return items

    async def get_user_videos(self, uid: int | str, page: int = 1, max_results: int = 10, keyword: str = "") -> list[VideoItem]:
        """Fetch public videos from a user's space.

        Prefer direct web API with our browser-like httpx client because the
        bilibili-api-python user.get_videos path is prone to 412 in some networks.
        """
        uid_text = str(uid).strip()
        if not uid_text.isdigit():
            raise ValueError(f"无效 UID：{uid}")
        await self.throttle()
        client = await self._http_client()
        params = {
            "mid": uid_text,
            "pn": page,
            "ps": max(1, min(int(max_results or 10), 30)),
            "tid": 0,
            "keyword": keyword or "",
            "order": "pubdate",
            "platform": "web",
            "web_location": "1550101",
        }
        signed_params = await self._wbi_sign_params(dict(params))
        errors: list[str] = []
        for url, label, use_params in [
            ("https://api.bilibili.com/x/space/wbi/arc/search", "B站用户公开视频接口", signed_params),
            ("https://api.bilibili.com/x/space/arc/search", "B站用户公开视频备用接口", params),
        ]:
            try:
                resp = await client.get(url, params=use_params, cookies=self.cookies or None)
                resp.raise_for_status()
                data = _safe_json(resp, label)
                if data.get("code") not in (0, "0", None):
                    raise RuntimeError(f"{label}失败：code={data.get('code')} message={data.get('message')}")
                vlist = (((data.get("data") or {}).get("list") or {}).get("vlist") or [])
                if not isinstance(vlist, list):
                    vlist = []
                return [_video_item_from_space_row(row, fallback_author=uid_text) for row in vlist[:max_results] if isinstance(row, dict)]
            except Exception as exc:
                errors.append(str(exc))
                await self.throttle()
        raise RuntimeError("；".join(errors[-2:]) or "获取用户公开视频失败")

    async def _wbi_sign_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Sign params for Bilibili WBI endpoints."""
        try:
            client = await self._http_client()
            resp = await client.get("https://api.bilibili.com/x/web-interface/nav", cookies=self.cookies or None)
            data = _safe_json(resp, "B站nav接口")
            wbi_img = ((data.get("data") or {}).get("wbi_img") or {})
            img_url = str(wbi_img.get("img_url") or "")
            sub_url = str(wbi_img.get("sub_url") or "")
            img_key = Path(urllib.parse.urlparse(img_url).path).stem
            sub_key = Path(urllib.parse.urlparse(sub_url).path).stem
            mixin_key = _get_wbi_mixin_key(img_key + sub_key)
            if not mixin_key:
                return params
            signed = dict(params)
            signed["wts"] = int(time.time())
            clean: dict[str, str] = {}
            for k, v in signed.items():
                value = str(v)
                # B站 WBI 签名会过滤这些字符。
                value = re.sub(r"[!'()*]", "", value)
                clean[str(k)] = value
            query = urllib.parse.urlencode(sorted(clean.items()))
            clean["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
            return clean
        except Exception:
            return params

    async def get_video_meta(self, bvid: str) -> VideoItem:
        await self.throttle()
        try:
            info = await asyncio.wait_for(video.Video(bvid=bvid, credential=self.credential).get_info(), timeout=30)
        except asyncio.TimeoutError:
            raise RuntimeError(f"获取视频信息超时（30秒）：{bvid}")
        pages = info.get("pages") or []
        cid = 0
        if pages and isinstance(pages[0], dict):
            cid = int(pages[0].get("cid") or 0)
        return VideoItem(
            bvid=str(info.get("bvid") or bvid),
            aid=int(info.get("aid") or 0),
            title=str(info.get("title") or ""),
            author=str((info.get("owner") or {}).get("name") or ""),
            duration=int(info.get("duration") or 0),
            desc=str(info.get("desc") or ""),
            url=f"https://www.bilibili.com/video/{bvid}",
            cid=cid,
            pic=str(info.get("pic") or ""),
            raw=info,
        )

    async def get_video_tags(self, bvid: str) -> list[str]:
        """获取视频标签（tag）。带 15 秒硬超时，防止网络挂起拖死整个规则。"""
        await self.throttle()
        client = await self._http_client()

        async def _fetch() -> list[str]:
            resp = await client.get(
                "https://api.bilibili.com/x/tag/archive/tags",
                params={"bvid": bvid},
                cookies=self.cookies or None,
            )
            data = _safe_json(resp, "B站视频标签接口")
            rows = data.get("data") or []
            return [str(x.get("tag_name") or "").strip() for x in rows if isinstance(x, dict) and str(x.get("tag_name") or "").strip()]

        try:
            return await asyncio.wait_for(_fetch(), timeout=15)
        except asyncio.TimeoutError:
            raise RuntimeError(f"获取视频标签超时（15秒）：{bvid}")

    async def get_home_videos(self, max_results: int = 20) -> list[VideoItem]:
        """获取首页推荐视频（带登录个性化推荐）。带 30 秒硬超时。"""
        from bilibili_api import homepage
        await self.throttle()
        try:
            data = await asyncio.wait_for(homepage.get_videos(credential=self.credential), timeout=30)
        except asyncio.TimeoutError:
            raise RuntimeError("获取首页推荐超时（30秒）")
        rows = (data.get("item") or [])[:max_results]
        items: list[VideoItem] = []
        for row in rows:
            bvid = str(row.get("bvid") or "")
            if not bvid:
                continue
            owner = row.get("owner") or {}
            items.append(VideoItem(
                bvid=bvid,
                aid=int(row.get("id") or row.get("aid") or 0),
                title=str(row.get("title") or ""),
                author=str(owner.get("name") or ""),
                duration=int(row.get("duration") or 0),
                desc="",
                url=f"https://www.bilibili.com/video/{bvid}",
                cid=int(row.get("cid") or 0),
                pic=str(row.get("pic") or ""),
                raw=row,
            ))
        return items

    async def get_subtitle_text(self, item: VideoItem, preferred_languages: list[str] | None = None) -> str:
        """Fetch subtitle text when the video exposes web subtitles.

        This uses Bilibili's player/wbi/v2 endpoint through the shared httpx client.
        It returns an empty string when subtitles are unavailable.
        """
        preferred_languages = preferred_languages or ["zh-CN", "zh-Hans", "zh", "ai-zh"]
        if not item.cid:
            item = await self.get_video_meta(item.bvid)
        if not item.cid:
            return ""
        await self.throttle()
        client = await self._http_client()
        last_err = ""
        for attempt in range(2):
            try:
                resp = await client.get(
                    "https://api.bilibili.com/x/player/wbi/v2",
                    params={"bvid": item.bvid, "cid": item.cid},
                    cookies=self.cookies or None,
                )
                data = _safe_json(resp, "B站字幕接口")
                subtitles = (((data.get("data") or {}).get("subtitle") or {}).get("subtitles") or [])
                if not subtitles:
                    # 空列表可能是瞬时 412/风控导致，先重试一次；仍空说明视频确实没字幕。
                    if attempt == 0:
                        await self.throttle()
                        continue
                    return ""
                chosen = None
                for lang in preferred_languages:
                    chosen = next((x for x in subtitles if str(x.get("lan") or "").lower() == lang.lower()), None)
                    if chosen:
                        break
                chosen = chosen or subtitles[0]
                url = str(chosen.get("subtitle_url") or "")
                if not url:
                    return ""
                if url.startswith("//"):
                    url = "https:" + url
                await self.throttle()
                sub_resp = await client.get(url, cookies=self.cookies or None)
                sub_data = _safe_json(sub_resp, "B站字幕文件")
                body = sub_data.get("body") or []
                lines = []
                for row in body:
                    content = str(row.get("content") or "").strip()
                    if content:
                        lines.append(content)
                if lines:
                    return "\n".join(lines)
                if attempt == 0:
                    await self.throttle()
            except Exception as exc:
                last_err = str(exc)
                if attempt == 0:
                    await self.throttle()
        if last_err:
            raise RuntimeError(f"字幕接口失败：{str(last_err)[:120]}")
        return ""

    async def get_page_first_frames(self, bvid: str, max_pages: int = 6) -> list[str]:
        """获取多分P视频各分P的首帧封面 URL（参考 astrbot_plugin_parser 的分P首帧方案）。

        多P视频（如合集、系列教程）每个分P有独立首帧画面，
        比单封面信息量大得多。返回去重后的 URL 列表，单P或无首帧返回空。
        """
        try:
            await self.throttle()
            info = await asyncio.wait_for(video.Video(bvid=bvid, credential=self.credential).get_info(), timeout=30)
            pages = info.get("pages") or []
            urls: list[str] = []
            for p in pages[:max_pages]:
                if not isinstance(p, dict):
                    continue
                ff = str(p.get("first_frame") or "").strip()
                if ff:
                    if ff.startswith("//"):
                        ff = "https:" + ff
                    if ff not in urls:
                        urls.append(ff)
            return urls
        except Exception as exc:
            from .utils import append_log
            append_log("logs/bot.log", f"获取分P首帧失败 {bvid}：{exc}", "WARN")
            return []

    async def get_ai_conclusion(self, bvid: str, cid: int | None = None) -> str:
        """获取 B站官方 AI 视频总结（零成本增强“听懂”能力）。

        需要登录凭证；失败返回空字符串，不影响主流程。
        """
        if not self.credential:
            return ""
        try:
            if not cid:
                meta = await self.get_video_meta(bvid)
                cid = meta.cid
            if not cid:
                return ""
            await self.throttle()
            v = video.Video(bvid=bvid, credential=self.credential)
            data = await asyncio.wait_for(v.get_ai_conclusion(cid), timeout=30)
            # 结构：{"model_result": {"summary": "..."}, "code": 0}
            model = data.get("model_result") if isinstance(data, dict) else None
            if isinstance(model, dict):
                summary = str(model.get("summary") or "").strip()
                if summary:
                    return summary
            return ""
        except Exception as exc:
            from .utils import append_log
            append_log("logs/bot.log", f"获取B站AI总结失败 {bvid}：{exc}", "WARN")
            return ""

    async def get_dash_audio_url(self, item: VideoItem, page_index: int = 0) -> str:
        """直接获取 DASH 音频流 URL（最高质量），不经过 yt-dlp 解析整站。

        参考 astrbot_plugin_parser 的 extract_download_urls 思路：
        用 bilibili-api 的 get_download_url + 流检测器挑出最佳音频流。
        失败返回空字符串。
        """
        if not self.credential:
            return ""
        try:
            await self.throttle()
            v = video.Video(bvid=item.bvid, credential=self.credential)
            data = await asyncio.wait_for(v.get_download_url(page_index=page_index), timeout=30)
            dash = (data or {}).get("dash") or {}
            audio_streams = dash.get("audio") or []
            if not audio_streams:
                return ""
            # 优先选最高带宽的音频流（dolby/标准都行，取带宽最大）
            best = max(audio_streams, key=lambda s: int(s.get("bandwidth") or 0) if isinstance(s, dict) else 0)
            url = str((best or {}).get("baseUrl") or (best or {}).get("base_url") or "")
            if url.startswith("//"):
                url = "https:" + url
            return url
        except Exception as exc:
            from .utils import append_log
            append_log("logs/bot.log", f"获取DASH音频流失败 {item.bvid}：{exc}", "WARN")
            return ""

    async def download_image_url(self, url: str, out_path: str) -> str:
        """下载一张网络图片到本地（独立轻量节流，只读静态资源不走主节流），返回保存路径或空串。"""
        if not url:
            return ""
        part_path: Path | None = None
        try:
            safe_url = _validated_bili_image_url(url)
            # 独立节流：两次图片下载间隔 1 秒即可（静态资源，非写操作）
            now = asyncio.get_event_loop().time()
            wait = 1.0 - (now - getattr(self, "_last_img_download", 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_img_download = asyncio.get_event_loop().time()
            client = await self._http_client()
            p = Path(out_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            part_path = p.with_name(p.name + ".part")
            async with client.stream("GET", safe_url, follow_redirects=False) as resp:
                if resp.status_code != 200:
                    return ""
                ctype = str(resp.headers.get("content-type") or "").lower()
                if ctype and not ctype.startswith("image/"):
                    return ""
                try:
                    content_length = int(resp.headers.get("content-length") or 0)
                except Exception:
                    content_length = 0
                if content_length > _MAX_IMAGE_DOWNLOAD_BYTES:
                    return ""
                total = 0
                with part_path.open("wb") as handle:
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > _MAX_IMAGE_DOWNLOAD_BYTES:
                            raise ValueError("图片超过 6 MiB 大小上限")
                        handle.write(chunk)
            os.replace(part_path, p)
            return str(p)
        except Exception as exc:
            from .utils import append_log
            append_log("logs/bot.log", f"下载图片失败：{type(exc).__name__}: {str(exc)[:120]}", "WARN")
            if part_path:
                try:
                    part_path.unlink(missing_ok=True)
                except Exception:
                    pass
            return ""

    async def download_cover(self, item: VideoItem, out_dir: str = "data/media") -> str:
        if not item.pic:
            return ""
        out = Path(out_dir) / item.bvid / "cover.jpg"
        return await self.download_image_url(item.pic, str(out))

    def _picture_from_file(self, path: str | Path) -> Picture:
        """兼容旧调用：仅构造 Picture，不再用于上传（库上传有 BiliAPIFile.read 兼容问题）。"""
        p = resolve_path(str(path))
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(str(p))
        return Picture.from_file(str(p))

    async def upload_image_raw(self, path: str | Path) -> dict[str, Any]:
        """直接调用 B站图片上传接口，绕过 bilibili-api-python 的 BiliAPIFile.read 兼容问题。"""
        if not self.credential:
            raise RuntimeError("未登录，不能上传图片")
        p = resolve_path(str(path))
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(str(p))
        await self.throttle()
        client = await self._http_client()
        mime = "image/jpeg"
        suf = p.suffix.lower()
        if suf == ".png":
            mime = "image/png"
        elif suf == ".gif":
            mime = "image/gif"
        elif suf == ".webp":
            mime = "image/webp"
        data = {"biz": "draw", "category": "daily", "csrf": self.credential.bili_jct}
        files = {"file_up": (p.name, p.read_bytes(), mime)}
        resp = await client.post(
            "https://api.bilibili.com/x/dynamic/feed/draw/upload_bfs",
            data=data,
            files=files,
            cookies=self.cookies or None,
        )
        resp.raise_for_status()
        js = resp.json()
        if int(js.get("code", 0) or 0) != 0:
            raise RuntimeError(f"图片上传失败：{js}")
        return js.get("data") or {}

    async def send_comment_with_optional_picture(self, *, text: str, oid: int, type_: CommentResourceType,
                                                 root: int | None = None, parent: int | None = None,
                                                 pic_path: str | Path | None = None, preserve_quotes: bool = False) -> Any:
        """发送评论/回复；有本地图片时先 raw 上传，再通过评论 API 带 pictures 字段发送。"""
        from .safety import sanitize_bilibili_comment_risk_text

        reviewed = sanitize_bilibili_comment_risk_text(text)
        # 指定读字模式只恢复空白，禁止撤回平台风险词替换。
        text = text if preserve_quotes and re.sub(r"\s", "", text) == re.sub(r"\s", "", reviewed) else reviewed
        if not self.credential:
            raise RuntimeError("未登录，不能评论")
        if not pic_path:
            try:
                await self.throttle()
                return await self._outbound(lambda: comment.send_comment(
                    text=text, oid=oid, type_=type_, root=root, parent=parent, credential=self.credential))
            except (OutboundDeliveryUnknown, AccountSendBlocked):
                raise
            except Exception as exc:
                if _is_definite_comment_rejection(exc) or _is_pre_send_network_error(exc):
                    raise
                raise OutboundDeliveryUnknown("评论发送结果未知，禁止自动重试") from exc
        up = await self.upload_image_raw(pic_path)
        pictures = [{
            "img_src": up.get("image_url") or up.get("url") or up.get("img_src"),
            "img_width": int(up.get("image_width") or up.get("img_width") or 0),
            "img_height": int(up.get("image_height") or up.get("img_height") or 0),
            "img_size": float(up.get("img_size") or up.get("size") or 0),
        }]
        if not pictures[0]["img_src"]:
            raise RuntimeError(f"图片上传返回缺少地址：{up}")
        data: dict[str, Any] = {
            "oid": int(oid),
            "type": type_.value,
            "message": text,
            "plat": 1,
            "statistics": {"appId": 100, "platform": 5},
            "gaia_source": "main_web",
            "pictures": json.dumps(pictures, ensure_ascii=False),
        }
        if root is not None and parent is None:
            data["root"] = int(root)
            data["parent"] = int(root)
        elif root is not None and parent is not None:
            data["root"] = int(root)
            data["parent"] = int(parent)
        try:
            await self.throttle()
            return await self._outbound(lambda: comment.Api(**comment.API["comment"]["send"],
                                      credential=self.credential).update_data(**data).result)
        except (OutboundDeliveryUnknown, AccountSendBlocked):
            raise
        except Exception as exc:
            if _is_definite_comment_rejection(exc) or _is_pre_send_network_error(exc):
                raise
            raise OutboundDeliveryUnknown("带图评论发送结果未知，禁止自动重试") from exc

    async def send_video_comment(self, item: VideoItem, text: str, pic_path: str | Path | None = None) -> Any:
        if not self.credential:
            raise RuntimeError("未登录，不能评论")
        aid = item.aid
        if not aid:
            item = await self.get_video_meta(item.bvid)
            aid = item.aid
        return await self.send_comment_with_optional_picture(
            text=text,
            oid=aid,
            type_=CommentResourceType.VIDEO,
            pic_path=pic_path,
        )

    async def send_video_comment_guarded(self, item: VideoItem, text: str, pic_path: str | Path | None = None) -> tuple[Any, bool]:
        """发送视频评论：发送前安全预检；可疑内容直接拦截，不发送。

        Returns:
            (result, suspicious): result 为发送接口返回（预检拦截时为 None）；
            suspicious=True 表示发送前已拦截。
        """
        from .safety import looks_like_analysis, sanitize_bilibili_comment_risk_text

        text = sanitize_bilibili_comment_risk_text(text)
        # 发送前预检：可疑直接不发送（最稳妥）
        if looks_like_analysis(text):
            return None, True
        result = await self.send_video_comment(item, text, pic_path=pic_path)
        return result, False

    async def send_comment_reply_guarded(self, aid: int, root_id: int, parent_id: int, text: str, pic_path: str | Path | None = None, *, preserve_quotes: bool = False) -> tuple[Any, bool]:
        """回复评论（root/parent）：发送前安全预检；可疑内容直接拦截，不发送。

        Returns:
            (result, suspicious): suspicious=True 表示发送前已拦截。
        """
        from .safety import looks_like_analysis, looks_like_prompt_injection, sanitize_bilibili_comment_risk_text

        if not self.credential:
            raise RuntimeError("未登录，不能回复评论")
        reviewed = sanitize_bilibili_comment_risk_text(text)
        text = text if preserve_quotes and re.sub(r"\s", "", text) == re.sub(r"\s", "", reviewed) else reviewed
        # 发送前预检：可疑直接不发送
        if looks_like_prompt_injection(text) or (not preserve_quotes and looks_like_analysis(text)):
            return None, True
        result = await self.send_comment_with_optional_picture(
            text=text,
            oid=aid,
            type_=CommentResourceType.VIDEO,
            root=root_id,
            parent=parent_id,
            pic_path=pic_path,
            preserve_quotes=preserve_quotes,
        )
        return result, False

    async def _delete_own_comment(self, aid: int, send_result: Any) -> bool:
        """从发送结果提取 rpid 并删除自己刚发出的评论；失败返回 False。"""
        rpid = None
        if isinstance(send_result, dict):
            data = send_result.get("data")
            if isinstance(data, dict):
                rpid = data.get("rpid") or data.get("reply_id")
            if not rpid:
                rpid = send_result.get("rpid") or send_result.get("reply_id")
        if not rpid:
            return False
        try:
            await self.throttle()
            c = comment.Comment(oid=aid, type_=CommentResourceType.VIDEO, rpid=int(rpid), credential=self.credential)
            await c.delete()
            return True
        except Exception:
            return False

    async def send_private_text(self, receiver_id: int | str, text: str,
                                *, allow_dispatch: Callable[[], bool] | None = None) -> Any:
        if not self.credential:
            raise RuntimeError("未登录，不能发私信")
        receiver = int(receiver_id)
        async def dispatch():
            if allow_dispatch is not None and not allow_dispatch():
                raise AccountSendBlocked("收件授权已撤销或不可核实")
            return await bili_session.send_msg(credential=self.credential, receiver_id=receiver,
                                              msg_type=bili_session.EventType.TEXT, content=text)
        try:
            await self.throttle()
            return await self._outbound(dispatch)
        except (OutboundDeliveryUnknown, AccountSendBlocked):
            raise
        except Exception as exc:
            if _is_definite_private_rejection(exc) or _is_pre_send_network_error(exc):
                raise
            raise OutboundDeliveryUnknown("私信发送结果未知，禁止自动重试") from exc

    async def send_private_picture(self, receiver_id: int | str, pic_path: str | Path, *,
                                   on_dispatch: Callable[[], None] | None = None,
                                   allow_dispatch: Callable[[], bool] | None = None) -> Any:
        """发送本地图片私信。先 raw 上传，再用远程 URL 发图，避开库内文件上传兼容问题。"""
        if not self.credential:
            raise RuntimeError("未登录，不能发私信图片")
        receiver = int(receiver_id)
        up = await self.upload_image_raw(pic_path)
        url = str(up.get("image_url") or up.get("url") or up.get("img_src") or "")
        if not url:
            raise RuntimeError("图片上传返回缺少地址")
        pic = Picture(
            url=url,
            width=int(up.get("image_width") or up.get("img_width") or 0),
            height=int(up.get("image_height") or up.get("img_height") or 0),
            size=up.get("img_size") or up.get("size") or 0,
            imageType=Path(str(pic_path)).suffix.lower().lstrip(".") or "jpg",
        )
        # Upload and throttling do not dispatch a private message.  Cancellation
        # here remains retryable; only cancellation after send_msg is ambiguous.
        await self.throttle()
        async def dispatch():
            if allow_dispatch is not None and not allow_dispatch():
                raise AccountSendBlocked("收件授权已撤销或不可核实")
            if on_dispatch is not None:
                on_dispatch()
            return await bili_session.send_msg(credential=self.credential, receiver_id=receiver,
                                              msg_type=bili_session.EventType.PICTURE, content=pic)
        try:
            return await self._outbound(dispatch)
        except asyncio.CancelledError as exc:
            raise OutboundDeliveryUnknown("图片私信派发被取消，投递结果未知，禁止自动重试") from exc
        except (OutboundDeliveryUnknown, AccountSendBlocked):
            raise
        except Exception as exc:
            if _is_definite_private_rejection(exc) or _is_pre_send_network_error(exc):
                raise
            raise OutboundDeliveryUnknown("图片私信发送结果未知，禁止自动重试") from exc

    async def fetch_private_messages_before(self, talker_id: int | str, end_seqno: int, *, size: int = 30) -> dict[str, Any]:
        """Fetch an older page using the SDK's signed API and exclusive upper bound.

        The SDK fetch_session_msgs begin_seqno is a lower bound used by its
        listener for new messages.  History therefore uses end_seqno instead.
        """
        if not self.credential:
            raise RuntimeError("未登录，不能读取私信")
        self.credential.raise_for_no_sessdata()
        end_seqno = int(end_seqno)
        if end_seqno <= 0:
            raise ValueError("历史消息上界必须大于零")
        return await bili_session.Api(**bili_session.API["session"]["fetch"], credential=self.credential).update_params(
            talker_id=int(talker_id), session_type=1, size=max(1, min(30, int(size))),
            end_seqno=end_seqno,
        ).result

    async def get_followings_checked(self, max_users: int = 200) -> tuple[list[dict[str, Any]], bool]:
        """获取关注列表，并明确报告是否完整；不完整时问候任务不得发送。"""
        if not self.credential or not self.uid:
            return [], False
        out: list[dict[str, Any]] = []
        complete = True
        try:
            from bilibili_api import user as bili_user
            u = bili_user.User(uid=self.uid, credential=self.credential)
            pn = 1
            ps = 50
            while len(out) < max_users:
                await self.throttle()
                page = await u.get_followings(pn=pn, ps=ps)
                rows = page.get("list") if isinstance(page, dict) else (getattr(page, "list", None) or [])
                if isinstance(rows, dict):
                    rows = rows.get("list") or rows.get("items") or []
                if not rows:
                    break
                for row in rows:
                    mid = str(row.get("mid") or row.get("uid") or "")
                    uname = str(row.get("uname") or row.get("name") or "")
                    if mid and mid != str(self.uid):
                        out.append({"mid": mid, "uname": uname})
                    if len(out) >= max_users:
                        complete = False
                        break
                if len(out) >= max_users:
                    break
                if len(rows) < ps:
                    break
                pn += 1
        except Exception as exc:
            from .utils import append_log
            append_log("logs/bot.log", f"获取关注列表失败：{type(exc).__name__}", "WARN")
            complete = False
        return out, complete

    async def get_followings(self, max_users: int = 200) -> list[dict[str, Any]]:
        rows, _complete = await self.get_followings_checked(max_users=max_users)
        return rows

    async def get_user_official(self, uid: int | str) -> dict[str, Any]:
        """查询一位用户的官方认证信息，用于判断是否真人。

        返回 {"mid": str, "role": int, "title": str, "alive": bool}：
        - role: 0=普通用户, 3=官方认证（2=机构/企业认证，1=个人认证等）
        - alive: False 表示账号已注销/不存在（查询返回 -404）

        注意：这是只读查询，用独立轻量节流（不占用发私信的主节流），
        避免全量判定 20+ 用户时要等几十分钟。
        """
        if not self.credential:
            return {"mid": str(uid), "role": 0, "title": "", "alive": True, "known": False}
        try:
            from bilibili_api import user as bili_user
            u = bili_user.User(uid=int(uid), credential=self.credential)
            # 独立节流：两次认证查询之间至少间隔 2 秒（只读接口，足够安全）
            now = asyncio.get_event_loop().time()
            wait = 2.0 - (now - getattr(self, "_last_official_check", 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_official_check = asyncio.get_event_loop().time()
            info = await u.get_user_info()
            official = info.get("official") or {}
            role = int(official.get("role") or 0)
            title = str(official.get("title") or "")
            return {"mid": str(uid), "role": role, "title": title, "alive": True, "known": True}
        except Exception as exc:
            # -404 表示账号已注销/不存在
            if "-404" in str(exc):
                return {"mid": str(uid), "role": 0, "title": "", "alive": False, "known": True}
            from .utils import append_log
            append_log("logs/bot.log", f"查询用户认证失败 {uid}：{type(exc).__name__}", "WARN")
            return {"mid": str(uid), "role": 0, "title": "", "alive": True, "known": False}

    async def get_user_face(self, uid: int | str, *, cache_dir: str = "data/avatars_cache") -> str:
        """取用户头像的本地路径（带缓存），失败返回空串。

        用途：生日贺卡要把生日者的头像贴到蛋糕上。
        两级来源：先 bilibili_api 的 user.get_user_info()["face"]，
        失败再走 web-interface/card。都是只读接口，用独立轻量节流。
        """
        uid = str(uid)
        if not uid.isdigit():
            return ""
        p = Path(cache_dir) / f"{uid}.jpg"
        try:
            if p.exists() and p.stat().st_size > 1024:
                return str(p)
        except Exception:
            pass
        face = ""
        try:
            if self.credential:
                u = user.User(uid=int(uid), credential=self.credential)
                now = asyncio.get_event_loop().time()
                wait = 2.0 - (now - getattr(self, "_last_face_check", 0.0))
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_face_check = asyncio.get_event_loop().time()
                info = await u.get_user_info()
                face = str((info or {}).get("face") or "")
        except Exception as exc:
            from .utils import append_log
            append_log("logs/bot.log", f"取头像失败(bilibili_api) {uid}：{type(exc).__name__}", "WARN")
        if not face:
            try:
                client = await self._http_client()
                resp = await client.get(
                    "https://api.bilibili.com/x/web-interface/card",
                    params={"mid": uid},
                    headers={"Referer": "https://www.bilibili.com/",
                             "User-Agent": "Mozilla/5.0"},
                    cookies=self.cookies or None,
                )
                card = ((resp.json() or {}).get("data") or {}).get("card") or {}
                face = str(card.get("face") or "")
            except Exception:
                face = ""
        if not face:
            return ""
        try:
            return await self.download_image_url(face, str(p))
        except Exception:
            return ""

    async def send_dynamic(self, text: str, pic_paths: list[str | Path] | None = None) -> Any:
        """发布一条动态；可选带图片。"""
        if not self.credential:
            raise RuntimeError("未登录，不能发动态")
        from bilibili_api import dynamic as bili_dynamic
        await self.throttle()
        pics = []
        for p in pic_paths or []:
            try:
                pics.append(self._picture_from_file(p))
            except Exception:
                continue
        info = bili_dynamic.BuildDynamic.create_by_args(text=str(text), pics=pics)
        return await self._outbound(lambda: bili_dynamic.send_dynamic(info, credential=self.credential))

    def _station_device_req_json(self) -> str:
        return json.dumps({"platform": "web", "device": "pc", "spmid": "333.40171"}, ensure_ascii=False, separators=(",", ":"))

    async def station_init_check_tribee(self, tribee_id: int | str) -> dict[str, Any]:
        """小站发帖初始化预检（不发布）。返回分区、权限、字数/图片限制等。"""
        if not self.credential:
            raise RuntimeError("未登录，不能做小站发布预检")
        await self.throttle()
        client = await self._http_client()
        csrf = self.credential.bili_jct
        resp = await client.post(
            "https://api.bilibili.com/x/dynamic/feed/create/init_check_tribee",
            params={"csrf": csrf, "x-bili-device-req-json": self._station_device_req_json()},
            json={"tribee_id": int(tribee_id), "check": {"scene": 6}},
            headers={"Referer": "https://www.bilibili.com/", "Origin": "https://www.bilibili.com"},
            cookies=self.cookies or None,
        )
        data = _safe_json(resp, "B站小站发帖初始化预检接口")
        if int(data.get("code") or 0) != 0:
            raise RuntimeError(f"B站小站发帖初始化预检失败：code={data.get('code')} message={data.get('message')}")
        return data

    @staticmethod
    def build_station_text_opus_payload(*, tribee_id: int | str, text: str, title: str = "", category_id: int | str | None = None,
                                        sync_dynamic: bool = False, aigc: bool = False,
                                        images: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """构造小站 Opus 发布负载预览；按浏览器真实结构本地构造，不发送。"""
        title = str(title or "").strip()[:60]
        text = str(text or "").strip()
        try:
            cid = int(category_id) if category_id not in (None, "") else None
        except Exception:
            cid = None
        if cid is not None and cid <= 0:
            cid = None
        paragraphs: list[dict[str, Any]] = []
        if text:
            paragraphs.append({
                "para_type": 1,
                "format": {"indent": {"first_line_indent": 0, "indent": 0}},
                "text": {
                    "nodes": [
                        {
                            "node_type": 1,
                            "word": {
                                "words": text,
                                "font_size": 17,
                                "color": "",
                                "dark_color": "",
                                "style": {},
                                "font_level": "regular",
                            },
                        }
                    ]
                },
            })
        for img in (images or []):
            url = str(img.get("url") or img.get("image_url") or img.get("img_src") or "").strip()
            if not url:
                continue
            paragraphs.append({
                "para_type": 2,
                "format": {"align": 1},
                "pic": {
                    "style": 0,
                    "pics": [{
                        "url": url,
                        "width": int(img.get("width") or img.get("image_width") or img.get("img_width") or 0),
                        "height": int(img.get("height") or img.get("image_height") or img.get("img_height") or 0),
                    }],
                },
            })
        payload = {
            "opus_req": {
                "opus": {
                    "opus_source": 9,
                    "title": title,
                    "content": {
                        "paragraphs": paragraphs
                    },
                    "pub_info": {"editor_version": "eva3-4.0.0"},
                    "attachments": {
                        "tribee": {"tribee_id": int(tribee_id)},
                        "poster_switch": 1,
                        "poster_source": 3 if images else 1,
                    },
                },
                "scene": 13,
                "meta": {"app_meta": {"from": "create.tribee.web", "mobi_app": "web"}},
                "option": {"aigc": 1 if aigc else 0, "sync_dynamic_enable": bool(sync_dynamic)},
            },
        }
        if cid is not None:
            payload["opus_req"]["tribee_arg"] = {
                "category_ids": [cid],
                "selections": [{"tribee_id": int(tribee_id), "category_id": cid}],
            }
        return payload

    async def station_publish_preflight(self, tribee_id: int | str, *, sample_text: str = "小站发布预检", title: str = "",
                                        category_id: int | str | None = None) -> dict[str, Any]:
        """小站发布非发送预检：调用 init_check 并本地生成 payload 预览，不创建草稿、不发布。"""
        init = await self.station_init_check_tribee(tribee_id)
        data = init.get("data") or {}
        categories = data.get("categories") or []
        if category_id is None and categories:
            try:
                category_id = int(categories[0].get("category_id") or 0)
            except Exception:
                category_id = None
        payload = self.build_station_text_opus_payload(
            tribee_id=tribee_id,
            text=sample_text,
            title=title,
            category_id=category_id,
            sync_dynamic=False,
            aigc=False,
        )
        return {"init_check": init, "payload_preview": payload, "note": "未调用草稿/发布接口；该结果仅用于确认小站发布 API 参数结构。"}

    async def send_station_text_post(self, *, tribee_id: int | str, text: str, title: str = "",
                                     category_id: int | str | None = None, sync_dynamic: bool = False,
                                     aigc: bool = False, pic_path: str | Path | None = None) -> Any:
        """真正发布一条小站帖子（支持文字 + 单张本地图片，非普通动态）。"""
        if not self.credential:
            raise RuntimeError("未登录，不能发小站帖子")
        init = await self.station_init_check_tribee(tribee_id)
        data = init.get("data") or {}
        categories = data.get("categories") or []
        if category_id is None:
            for cat in categories:
                if str(cat.get("name") or "") == "人里闲话":
                    category_id = int(cat.get("category_id") or 0)
                    break
        if category_id is None and categories:
            try:
                category_id = int(categories[0].get("category_id") or 0)
            except Exception:
                category_id = None
        images: list[dict[str, Any]] = []
        if pic_path:
            up = await self.upload_image_raw(pic_path)
            images.append({
                "url": up.get("image_url") or up.get("url") or up.get("img_src") or "",
                "width": int(up.get("image_width") or up.get("img_width") or 0),
                "height": int(up.get("image_height") or up.get("img_height") or 0),
            })
        payload = self.build_station_text_opus_payload(
            tribee_id=tribee_id,
            text=text,
            title=title,
            category_id=category_id,
            sync_dynamic=sync_dynamic,
            aigc=aigc,
            images=images,
        )
        params = await self._wbi_sign_params({
            "gaia_source": "main_web",
            "x-bili-device-req-json": self._station_device_req_json(),
            "w_opus_req.opus.attachments.tribee.tribee_id": int(tribee_id),
        })
        csrf = self.credential.bili_jct
        params["csrf"] = csrf
        await self.throttle()
        client = await self._http_client()
        resp = await self._outbound(lambda: client.post(
            "https://api.bilibili.com/x/dynamic/feed/create/opus",
            params=params,
            json=payload,
            headers={"Referer": "https://www.bilibili.com/", "Origin": "https://www.bilibili.com"},
            cookies=self.cookies or None,
        ))
        data = _safe_json(resp, "B站小站发帖接口")
        if int(data.get("code") or 0) != 0:
            raise RuntimeError(f"B站小站发帖失败：code={data.get('code')} message={data.get('message')} data={data.get('data')}")
        return data

    async def get_station_posts(self, tribee_id: int | str, *, page_size: int = 20, sort_type: int = 2) -> list[StationPostItem]:
        """读取 B站小站帖子列表（只读）。sort_type: 1=新回复，2=新发布。"""
        await self.throttle()
        client = await self._http_client()
        resp = await client.get(
            "https://api.bilibili.com/x/tribee/v1/dyn/all",
            params={"tribee_id": int(tribee_id), "page_size": max(1, int(page_size or 20)), "sort_type": int(sort_type or 2)},
            headers={"Referer": f"https://www.bilibili.com/bubble/home/{tribee_id}"},
            cookies=self.cookies or None,
        )
        data = _safe_json(resp, "B站小站帖子接口")
        if int(data.get("code") or 0) != 0:
            raise RuntimeError(f"B站小站帖子接口失败：code={data.get('code')} message={data.get('message')}")
        content = ((data.get("data") or {}).get("content") or {})
        rows = content.get("dyn_list") or []
        out: list[StationPostItem] = []
        for row in rows if isinstance(rows, list) else []:
            meta = row.get("meta") or {}
            try:
                reply_count = int(str(meta.get("reply_count") or "0").strip() or 0)
            except Exception:
                reply_count = 0
            dyn_id = str(row.get("dyn_id") or "").strip()
            if not dyn_id:
                continue
            out.append(StationPostItem(
                dyn_id=dyn_id,
                title=str(row.get("title") or "").strip(),
                author=str(meta.get("author") or "").strip(),
                time_text=str(meta.get("time_text") or "").strip(),
                reply_count=reply_count,
                url=str(row.get("jump_uri") or f"https://www.bilibili.com/opus/{dyn_id}"),
                raw=row,
            ))
        return out

    async def send_dynamic_comment(self, dyn_id: int | str, text: str, pic_path: str | Path | None = None,
                                   type_: CommentResourceType | None = None) -> Any:
        """给动态/小站帖子发表评论；未知动态类型时在 DYNAMIC/DYNAMIC_DRAW 间安全兜底。"""
        if not self.credential:
            raise RuntimeError("未登录，不能评论动态")
        from bilibili_api import dynamic as bili_dynamic
        dyn = bili_dynamic.Dynamic(dynamic_id=int(dyn_id), credential=self.credential)
        await self.throttle()
        rid = await dyn.get_rid()
        if not rid:
            raise RuntimeError(f"动态 {dyn_id} 缺少 rid，无法评论")
        preferred = type_ or CommentResourceType.DYNAMIC
        fallback = CommentResourceType.DYNAMIC_DRAW if preferred == CommentResourceType.DYNAMIC else CommentResourceType.DYNAMIC
        try:
            return await self.send_comment_with_optional_picture(
                text=text,
                oid=int(rid),
                type_=preferred,
                pic_path=pic_path,
            )
        except OutboundDeliveryUnknown:
            raise
        except Exception as exc:
            # 纯文字动态/opus 常用 DYNAMIC(17)，图文动态常用 DYNAMIC_DRAW(11)；类型不匹配时自动换一次。
            if any(s in str(exc) for s in ("12002", "评论功能已关闭", "评论区已关闭")):
                return await self.send_comment_with_optional_picture(
                    text=text,
                    oid=int(rid),
                    type_=fallback,
                    pic_path=pic_path,
                )
            raise

    async def like_video(self, item: VideoItem) -> Any:
        if not self.credential:
            raise RuntimeError("未登录，不能点赞")
        await self.throttle()
        v = video.Video(bvid=item.bvid, credential=self.credential)
        try:
            liked = await v.has_liked()
        except Exception:
            liked = False
        if liked:
            return {"already_liked": True}
        return await self._outbound(lambda: v.like(status=True))

    async def like_comment(self, aid: int, rpid: int, type_: CommentResourceType | None = None) -> Any:
        """点赞某条评论（默认视频评论区；动态评论区需传对应 type）。"""
        if not self.credential:
            raise RuntimeError("未登录，不能点赞评论")
        await self.throttle()
        c = comment.Comment(oid=aid, type_=type_ or CommentResourceType.VIDEO, rpid=rpid, credential=self.credential)
        return await self._outbound(lambda: c.like(status=True))

    async def favorite_video(self, item: VideoItem, folder_name: str) -> Any:
        """Create (if needed) a daily-named favorite folder and add the video.

        Returns the folder id and whether the video was already collected.
        """
        if not self.credential:
            raise RuntimeError("未登录，不能收藏")
        aid = item.aid
        if not aid:
            item = await self.get_video_meta(item.bvid)
            aid = item.aid
        await self.throttle()
        folders = await favorite_list.get_video_favorite_list(self.uid or 0, credential=self.credential)
        rows = (folders.get("data") or {}).get("list") or folders.get("list") or []
        media_id = None
        for row in rows:
            if str(row.get("title") or "").strip() == folder_name:
                media_id = int(row.get("id") or row.get("media_id") or 0)
                break
        if not media_id:
            await self.throttle()
            created = await self._outbound(lambda: favorite_list.create_video_favorite_list(
                title=folder_name,
                introduction=f"每日自动收藏 {folder_name}",
                private=True,
                credential=self.credential,
            ))
            # 兼容两种返回结构：{id: ...} 或 {data: {id: ...}}
            d = created.get("data") if isinstance(created.get("data"), dict) else created
            media_id = int(d.get("id") or d.get("media_id") or 0)
            if not media_id:
                raise RuntimeError(f"创建收藏夹失败：{created}")
        await self.throttle()
        v = video.Video(bvid=item.bvid, credential=self.credential)
        result = await self._outbound(lambda: v.set_favorite(add_media_ids=[media_id]))
        return {"media_id": media_id, "result": result}

    def cookie_summary(self) -> dict[str, str]:
        secret_keys = {
            "SESSDATA", "sessdata", "bili_jct", "BILI_JCT", "DedeUserID",
            "dedeuserid", "buvid3", "ac_time_value",
        }
        return {k: mask_secret(v) if k in secret_keys else str(v) for k, v in self.cookies.items()}


WBI_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32,
    15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19,
    29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63,
    57, 62, 11, 36, 20, 34, 44, 52,
]


def _get_wbi_mixin_key(raw_key: str) -> str:
    if not raw_key:
        return ""
    return "".join(raw_key[i] for i in WBI_MIXIN_KEY_ENC_TAB if i < len(raw_key))[:32]


def _video_item_from_space_row(row: dict[str, Any], fallback_author: str = "") -> VideoItem:
    bvid = str(row.get("bvid") or row.get("bvid_str") or "").strip()
    aid = int(row.get("aid") or 0)
    title = str(row.get("title") or "").strip()
    author = str(row.get("author") or row.get("name") or fallback_author or "")
    duration = _duration_to_seconds(row.get("length") or row.get("duration"))
    pic = str(row.get("pic") or "")
    if pic.startswith("//"):
        pic = "https:" + pic
    desc = str(row.get("description") or row.get("desc") or "")
    return VideoItem(
        bvid=bvid,
        aid=aid,
        title=title,
        author=author,
        duration=duration,
        desc=desc,
        url=f"https://www.bilibili.com/video/{bvid}" if bvid else "",
        pic=pic,
        raw=row,
    )


def _extract_video_results_from_search_all(data: dict[str, Any]) -> list[dict[str, Any]]:
    result = (data.get("data") or {}).get("result") or []
    for module in result:
        if module.get("result_type") == "video":
            rows = module.get("data") or []
            return rows if isinstance(rows, list) else []
    return []


def _safe_json(resp: httpx.Response, label: str) -> dict[str, Any]:
    text = resp.text or ""
    ctype = resp.headers.get("content-type", "")
    try:
        data = resp.json()
    except Exception as exc:
        snippet = " ".join(text[:300].split())
        raise RuntimeError(
            f"{label}返回的不是JSON：status={resp.status_code} content-type={ctype} body={snippet!r}"
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{label}返回JSON不是对象：{type(data).__name__}")
    return data


def _duration_to_seconds(value: Any) -> int:
    if isinstance(value, int):
        return value
    text = str(value or "")
    if not text:
        return 0
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return nums[0] if nums else 0
