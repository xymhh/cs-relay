"""115 上传：p115client 初始化 + p115oss 分片并发，OSS 端点改内网。

只用 p115client 的 upload_file_init（顺带秒传复核）与目录创建；
分片 PUT 走 p115oss，端点从公网换成同地域内网（阿里云 ECS 上约 22 MB/s）。
"""

from __future__ import annotations

import contextlib
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable
from urllib.parse import urlparse, urlunparse

try:  # pragma: no cover - 运行镜像内一定存在
    from p115client import P115Client, check_response
except ImportError:  # pragma: no cover
    P115Client = None

    def check_response(response):
        return response

try:  # pragma: no cover
    from p115oss import (
        oss_multipart_upload_cancel,
        oss_multipart_upload_complete,
        oss_multipart_upload_init,
        oss_multipart_upload_part,
    )
except ImportError:  # pragma: no cover
    oss_multipart_upload_cancel = oss_multipart_upload_complete = None
    oss_multipart_upload_init = oss_multipart_upload_part = None


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
        size = int(size or os.path.getsize(local_path))
        pid = self.ensure_dir(remote_path)
        with open(local_path, "rb") as handle:
            def read_range(sign_check: str, _handle=handle) -> bytes:
                start, end = (int(part) for part in str(sign_check).split("-"))
                _handle.seek(start)
                return _handle.read(end - start + 1)

            init = self.client.upload_file_init(
                filename=name,
                filesha1=str(sha1 or "").upper(),
                filesize=size,
                pid=pid,
                read_range_bytes_or_hash=read_range,
            )
        check_response(init)
        if init.get("reuse"):
            return {"reused": True, "pickcode": str(init.get("pickcode") or "")}
        data = init.get("data") or {}
        url = str(data.get("url") or "")
        callback = data.get("callback")
        if not url or not callback:
            raise P115Error(f"115 未返回上传配置：{str(init)[:200]}")
        if oss_multipart_upload_init is None:
            raise P115Error("p115oss 未安装，无法分片上传")
        target = internalize(url, self._internal_endpoint)

        parts_total = max(1, int(math.ceil(size / self._part_size)))
        upload_id = oss_multipart_upload_init(target)
        if not upload_id:
            raise P115Error("115 分块上传初始化失败")
        uploaded = 0
        lock = threading.Lock()

        def report_delta(delta: int) -> None:
            nonlocal uploaded
            with lock:
                uploaded = min(size, uploaded + delta)
                done = uploaded
            if progress:
                progress(done, size)

        def put_part(number: int) -> dict:
            if cancelled and cancelled():
                raise P115Error("任务已取消")
            start = (number - 1) * self._part_size
            length = min(self._part_size, size - start)
            with open(local_path, "rb") as handle:
                handle.seek(start)
                chunk = handle.read(length)
            if len(chunk) != length:
                raise P115Error(f"读取第 {number} 片失败：{len(chunk)}/{length}")
            result = oss_multipart_upload_part(
                target, upload_id, chunk, part_number=number,
                reporthook=lambda count: report_delta(int(count or 0)),
            )
            return result

        parts: list = []
        try:
            with ThreadPoolExecutor(
                    max_workers=min(self._concurrency, parts_total),
                    thread_name_prefix="relay-115-part",
            ) as executor:
                futures = [executor.submit(put_part, number) for number in range(1, parts_total + 1)]
                for future in as_completed(futures):
                    parts.append(future.result())
            if len(parts) != parts_total:
                raise P115Error(f"分片数不符：{len(parts)}/{parts_total}")
            parts.sort(key=lambda part: int(part["PartNumber"]))
            response = oss_multipart_upload_complete(target, upload_id, parts, callback)
            check_response(response)
        except BaseException:
            with contextlib.suppress(Exception):
                oss_multipart_upload_cancel(target, upload_id)
            raise
        if progress:
            progress(size, size)
        return {
            "reused": False,
            "pickcode": str(init.get("pickcode") or ""),
            "parts": parts_total,
        }
