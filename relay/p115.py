"""115 上传：p115client 建目录 + p115oss.upload_file 分片 PUT（OSS 端点改内网）。

阿里云深圳 ECS 上走 oss-cn-shenzhen-internal 端点，实测约 22 MB/s。
"""

from __future__ import annotations

import os
import threading
from typing import Callable
from urllib.parse import urlparse, urlunparse

try:  # pragma: no cover - 运行镜像内一定存在
    from p115client import P115Client, check_response
except ImportError:  # pragma: no cover
    P115Client = None

    def check_response(response):
        return response

try:  # pragma: no cover
    from p115oss import upload_file
except ImportError:  # pragma: no cover
    upload_file = None


class P115Error(RuntimeError):
    pass


def internalize(url: str, endpoint: str) -> str:
    """把 OSS 公网端点换成内网端点，保留 path/query 与端口。"""
    if not url or not endpoint:
        return url
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host or "aliyuncs.com" not in host or "-internal" in host:
        return url
    index = host.find("oss-")
    if index < 0:
        return url
    netloc = f"{host[:index]}{endpoint}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _dir_id(response) -> int:
    if not isinstance(response, dict):
        raise P115Error("115 目录接口返回异常")
    data = response.get("data")
    if isinstance(data, dict):
        for key in ("id", "file_id", "cid"):
            if data.get(key) not in (None, ""):
                return int(data[key])
    for key in ("id", "file_id", "cid"):
        if response.get(key) not in (None, ""):
            return int(response[key])
    raise P115Error(f"115 目录 id 解析失败：{str(response)[:200]}")


class Uploader:
    """115 上传。

    直接复用 p115oss.upload_file：它内部处理 init（含 sha1 秒传复核、status=1/2
    两种响应）与分片 PUT，并把端点交给我们换成同地域内网地址。
    自己拼 init 响应是错的——115 的 status=1 响应把上传配置放在 data 顶层
    （bucket/object/callback），没有 url 字段。
    """

    def __init__(
            self, cookie: str, concurrency: int = 4,
            part_size: int = 16 * 1024 * 1024,
            internal_endpoint: str = "oss-cn-shenzhen-internal.aliyuncs.com",
    ):
        self._cookie = cookie
        self._concurrency = max(1, min(int(concurrency or 1), 16))
        self._part_size = max(5 * 1024 * 1024, int(part_size or 16 * 1024 * 1024))
        self._internal_endpoint = internal_endpoint
        self._client = None

    @property
    def client(self):
        if self._client is None:
            if P115Client is None:
                raise P115Error("p115client 未安装")
            if not self._cookie:
                raise P115Error("115 cookie 未配置")
            self._client = P115Client(self._cookie)
        return self._client

    def ensure_dir(self, remote_path: str) -> int:
        return _dir_id(self.client.fs_makedirs(str(remote_path or "/").rstrip("/") or "/"))

    def upload(
            self, local_path: str, remote_path: str, name: str,
            sha1: str, size: int,
            progress: Callable[[int, int], None] | None = None,
            cancelled: Callable[[], bool] | None = None,
    ) -> dict:
        if not os.path.isfile(local_path):
            raise P115Error(f"本地文件不存在：{local_path}")
        if upload_file is None:
            raise P115Error("p115oss 未安装，无法上传")
        size = int(size or os.path.getsize(local_path))
        pid = self.ensure_dir(remote_path)
        if cancelled and cancelled():
            raise P115Error("任务已取消")

        uploaded = 0
        lock = threading.Lock()

        def reporthook(count: int) -> None:
            nonlocal uploaded
            with lock:
                uploaded = min(size, uploaded + int(count or 0))
                done = uploaded
            if progress:
                progress(done, size)

        client = self.client
        headers = {k: v for k, v in dict(client.headers).items()
                   if k.lower() != "authorization"}
        response = upload_file(
            local_path,
            pid=pid,
            filename=name,
            filesha1=str(sha1 or "").upper(),
            filesize=size,
            partsize=self._part_size,
            endpoint=f"https://{self._internal_endpoint}",
            reporthook=reporthook,
            # 给了 user_id/user_key 才走 cookie 那套接口；否则 p115oss 会打 /open/
            # 接口并要求 authorization: Bearer，cookie 登录的客户端拿不到
            user_id=client.user_id,
            user_key=client.user_key,
            headers=headers,
            cookies=client.cookies,
        )
        check_response(response)
        data = response.get("data") or {}
        reused = bool(response.get("reuse")) or int(data.get("status") or 0) == 2
        if progress:
            progress(size, size)
        return {
            "reused": reused,
            "pickcode": str(response.get("pickcode") or data.get("pickcode") or ""),
        }
