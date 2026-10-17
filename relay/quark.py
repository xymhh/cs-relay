"""夸克网盘：按文件 id 取下载直链（PC 客户端 UA 通道）。

只做中继真正需要的一件事：fid -> (download_url, headers)。
夸克对下载按 UA 分级限速，网页 UA 即使多连接也上不去，故优先 PC 客户端 UA。
"""

from __future__ import annotations

import time
from typing import Any

import requests

BASE_URL = "https://drive-pc.quark.cn/1/clouddrive"
SHARE_BASE_URL = "https://drive.quark.cn/1/clouddrive"

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 "
    "Safari/537.36"
)
DEFAULT_HEADERS = {
    "user-agent": DESKTOP_UA,
    "referer": "https://pan.quark.cn/",
    "origin": "https://pan.quark.cn",
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9",
}
DOWNLOAD_PARAMS = {"sys": "win32", "ve": "2.5.20", "ut": "", "guid": ""}


class QuarkError(RuntimeError):
    pass


def _params(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    params = {"pr": "ucpro", "fr": "pc", "uc_param_str": "", "__t": int(time.time() * 1000)}
    if extra:
        params.update(extra)
    return params


def _data(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return {}
    return payload.get("data") or {}


def refresh_cookie(cookie: str, timeout: int = 15) -> str:
    """用一次轻量 API 请求刷新 __puus。

    夸克把会话令牌放在 Set-Cookie 的 __puus 里滚动下发；.env/扩展设置里的那份
    很快过期，过期后 /file/download 仍返回直链，但 CDN 一律 412（262 字节）。
    只要把响应里的新 __puus 覆盖回 cookie 就能继续用。
    """
    if not cookie:
        return cookie
    cookie = refresh_cookie(cookie)
    headers = dict(DEFAULT_HEADERS)
    headers["cookie"] = cookie
    try:
        response = requests.get(
            f"{BASE_URL}/member",
            params={"pr": "ucpro", "fr": "pc"},
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException:
        return cookie
    raw = response.headers.get("set-cookie") or ""
    parts = [p.strip() for p in raw.split(",") if "__puus=" in p]
    if not parts:
        return cookie
    new_puus = parts[-1].split(";")[0].strip()
    if not new_puus.startswith("__puus="):
        return cookie
    cookie_parts = [p.strip() for p in cookie.split(";") if p.strip()]
    for index, part in enumerate(cookie_parts):
        if part.startswith("__puus="):
            cookie_parts[index] = new_puus
            break
    else:
        cookie_parts.append(new_puus)
    return "; ".join(cookie_parts)


def resolve_download_link(
        cookie: str, file_id: str, timeout: int = 30,
) -> tuple[str, dict[str, str]]:
    """返回 (直链, 下载请求头)。失败抛 QuarkError。"""
    if not cookie:
        raise QuarkError("夸克 cookie 未配置")
    if not file_id:
        raise QuarkError("夸克文件 id 为空")
    headers = dict(DEFAULT_HEADERS)
    headers["cookie"] = cookie
    last_message = ""
    for base_url, params in ((BASE_URL, None), (SHARE_BASE_URL, DOWNLOAD_PARAMS)):
        try:
            # 参数必须与 Gopeed 扩展完全一致：pr=ucpro&fr=pc。
            # 多带 uc_param_str/__t 时返回的直链签名会被 CDN 判 412（实测 262 字节）。
            response = requests.post(
                f"{base_url}/file/download",
                params={"pr": "ucpro", "fr": "pc"},
                json={"fids": [str(file_id)]},
                headers=headers,
                timeout=timeout,
            )
        except requests.RequestException as error:
            last_message = f"请求异常：{error}"
            continue
        if response.status_code >= 400:
            last_message = f"HTTP {response.status_code}"
            continue
        try:
            payload = response.json()
        except ValueError:
            last_message = "返回非 JSON"
            continue
        items = _data(payload)
        entry = items[0] if isinstance(items, list) and items else {}
        url = str((entry or {}).get("download_url") or "")
        if url:
            # 直链只认最小请求头：UA + Cookie + Referer。
            # 带上 accept/origin/accept-language 会被夸克 CDN 判 412（实测 262 字节空响应），
            # 这也是之前「Gopeed status=error 无详情」的真正原因。
            download_headers = {
                "user-agent": DESKTOP_UA,
                "cookie": cookie,
                "referer": "https://pan.quark.cn/",
            }
            return url, download_headers
        last_message = (
            str(payload.get("message") or payload.get("msg") or "")
            or f"code={payload.get('code')}"
        )
    raise QuarkError(f"夸克未返回下载地址：{last_message or '未知原因'}")
