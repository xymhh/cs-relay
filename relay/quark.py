"""夸克网盘：按文件 id 取下载直链（对齐插件 drive/quark/files.py 的做法）。

只做中继真正需要的一件事：fid -> (download_url, headers)。
夸克对下载按 UA 分级限速，网页 UA 即使多连接也上不去，故优先 PC 客户端 UA。
"""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any

import requests

BASE_URL = "https://drive-pc.quark.cn/1/clouddrive"
SHARE_BASE_URL = "https://drive.quark.cn/1/clouddrive"

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 "
    "Safari/537.36"
)
WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "user-agent": DESKTOP_UA,
    "referer": "https://pan.quark.cn/",
    "origin": "https://pan.quark.cn",
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9",
}
DOWNLOAD_PARAMS = {"sys": "win32", "ve": "2.5.20", "ut": "", "guid": ""}
COOKIE_KEYS = ("__puus", "__pus")


class QuarkError(RuntimeError):
    pass


def _params(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "pr": "ucpro",
        "fr": "pc",
        "uc_param_str": "",
        "__t": int(time.time() * 1000),
        "__dt": random.randint(100, 9999),
    }
    if extra:
        params.update(extra)
    return params


def _data(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return {}
    return payload.get("data") or {}


def load_cookie(path: "Path", fallback: str) -> str:
    """优先用上次刷新后落盘的那份 cookie（__puus 是滚动的）。"""
    try:
        stored = path.read_text().strip()
    except OSError:
        return fallback
    return stored or fallback


def save_cookie(path: "Path", cookie: str) -> None:
    if not cookie:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(cookie)
        tmp.replace(path)
    except OSError:
        return


def _merge_cookies(cookie: str, session: requests.Session) -> str:
    """把 session 里 rolling 下发的 __puus/__pus 合并回 cookie 串。"""
    jar = {c.name: c.value for c in session.cookies}
    if not any(jar.get(k) for k in COOKIE_KEYS):
        return cookie
    parts = [p.strip() for p in cookie.split(";") if p.strip()]
    merged = dict(p.split("=", 1) for p in parts if "=" in p)
    for key in COOKIE_KEYS:
        if jar.get(key):
            merged[key] = jar[key]
    return "; ".join(f"{k}={v}" for k, v in merged.items())


def refresh_cookie(cookie: str, timeout: int = 15) -> str:
    """用一次轻量 API 请求刷新 __puus。

    夸克把会话令牌放在 Set-Cookie 的 __puus 里滚动下发；.env/扩展设置里的那份
    很快过期，过期后 /file/download 可能直接 404/412。
    """
    if not cookie:
        return cookie
    session = requests.Session()
    headers = dict(DEFAULT_HEADERS)
    headers["cookie"] = cookie
    try:
        session.get(
            f"{BASE_URL}/member",
            params=_params(),
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException:
        return cookie
    return _merge_cookies(cookie, session)


def resolve_download_link(
        cookie: str, file_id: str, timeout: int = 30,
) -> tuple[str, dict[str, str]]:
    """返回 (直链, 下载请求头)。失败抛 QuarkError。"""
    if not cookie:
        raise QuarkError("夸克 cookie 未配置")
    if not file_id:
        raise QuarkError("夸克文件 id 为空")

    session = requests.Session()
    cookie = refresh_cookie(cookie, timeout=timeout) or cookie
    last_message = ""
    for channel, base_url, params, ua in (
            ("drive-pc/PC-UA", BASE_URL, None, DESKTOP_UA),
            ("drive/PC-UA", SHARE_BASE_URL, DOWNLOAD_PARAMS, DESKTOP_UA),
            ("drive/网页-UA", SHARE_BASE_URL, DOWNLOAD_PARAMS, WEB_UA),
    ):
        headers = dict(DEFAULT_HEADERS)
        headers["cookie"] = cookie
        headers["user-agent"] = ua
        try:
            response = session.post(
                f"{base_url}/file/download",
                params=_params(params),
                json={"fids": [str(file_id)]},
                headers=headers,
                timeout=timeout,
            )
        except requests.RequestException as error:
            last_message = f"{channel} 请求异常：{error}"
            continue
        cookie = _merge_cookies(cookie, session)
        if response.status_code >= 400:
            last_message = f"{channel} HTTP {response.status_code}"
            continue
        try:
            payload = response.json()
        except ValueError:
            last_message = f"{channel} 返回非 JSON"
            continue
        items = _data(payload)
        entry = items[0] if isinstance(items, list) and items else {}
        url = str((entry or {}).get("download_url") or "")
        if url:
            download_headers = {
                "user-agent": ua,
                "cookie": cookie,
                "referer": "https://pan.quark.cn/",
            }
            return url, download_headers
        last_message = (
            f"{channel} code={payload.get('code')} "
            f"{payload.get('message') or payload.get('msg') or ''}"
        ).strip()
    raise QuarkError(f"夸克未返回下载地址：{last_message or '未知原因'}")
