"""内置多连接下载腿：不依赖 Gopeed/扩展，单进程多线程 Range 分片。

存在理由：Gopeed 侧下载夸克直链需要额外的扩展/配置，失败时只回
"status=error"，把整条中继链卡死。夸克直链本身支持 Range，用 8–16 个
并发 Range 请求即可达到接近满速，且请求头完全由我们自己控制
（cookie / UA / referer 一定带对）。

实现要点：
- 先发一个 `Range: bytes=0-0` 探明总大小与是否支持分段；
- 按 chunk 切分，线程池并发，各自 `Range: bytes=a-b` 拉取；
- 用 `os.pwrite` 按偏移写入同一个文件描述符，避免多线程 seek 竞争；
- 任一分片失败按次数重试，仍失败则整体报错并清理；
- 全程支持取消（`cancelled()`）与进度回调。

这是中继唯一的下载腿（0.7.0 起移除了 Gopeed）：夸克直链支持 Range，
32 个并发分片足以跑满国内到夸克的带宽，且请求头完全可控。
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import requests

DEFAULT_CHUNK = 8 * 1024 * 1024
READ_BUFFER = 256 * 1024


class DownloadError(RuntimeError):
    pass


class _Meter:
    """线程安全的进度累加器（按增量回调，避免每次重算总量）。"""

    def __init__(self, callback: Callable[[int], None] | None):
        self._callback = callback
        self._lock = threading.Lock()
        self._done = 0

    def add(self, delta: int) -> None:
        if delta <= 0:
            return
        with self._lock:
            self._done += delta
            total = self._done
        if self._callback:
            self._callback(total)

    @property
    def done(self) -> int:
        with self._lock:
            return self._done


def _probe(url: str, headers: dict, timeout: float) -> tuple[int, bool]:
    """返回 (总大小, 是否支持 Range)。总大小未知时抛错。"""
    probe_headers = dict(headers)
    probe_headers["Range"] = "bytes=0-0"
    try:
        response = requests.get(url, headers=probe_headers, stream=True, timeout=timeout)
    except requests.RequestException as error:
        raise DownloadError(f"探测直链失败：{error}") from error
    try:
        status = response.status_code
        if status == 403:
            raise DownloadError("直链返回 403（cookie 失效或请求头不够）")
        if status == 404:
            raise DownloadError("直链返回 404（文件已删除或链接失效）")
        if status not in (200, 206):
            raise DownloadError(f"直链探测异常：HTTP {status}")
        if status == 206:
            content_range = str(response.headers.get("Content-Range") or "")
            total = 0
            if "/" in content_range:
                try:
                    total = int(content_range.rsplit("/", 1)[-1])
                except ValueError:
                    total = 0
            if not total:
                length = int(response.headers.get("Content-Length") or 0)
                total = length
            return total, True
        # 200：服务器忽略了 Range，只能单连接
        total = int(response.headers.get("Content-Length") or 0)
        return total, False
    finally:
        response.close()


def _download_chunk(
        url: str, headers: dict, fd: int, start: int, end: int,
        meter: _Meter, timeout: float, cancelled: Callable[[], bool],
        retries: int,
) -> None:
    """下载 [start, end] 闭区间，写入 fd 对应偏移。"""
    attempt = 0
    while True:
        if cancelled():
            raise DownloadError("任务已取消")
        offset = start
        chunk_headers = dict(headers)
        chunk_headers["Range"] = f"bytes={offset}-{end}"
        try:
            with requests.get(url, headers=chunk_headers, stream=True,
                              timeout=timeout) as response:
                if response.status_code not in (200, 206):
                    raise DownloadError(f"分片 HTTP {response.status_code}")
                for block in response.iter_content(READ_BUFFER):
                    if cancelled():
                        raise DownloadError("任务已取消")
                    if not block:
                        continue
                    if offset + len(block) > end + 1:
                        block = block[: end + 1 - offset]
                    os.pwrite(fd, block, offset)
                    offset += len(block)
                    meter.add(len(block))
                    if offset > end:
                        break
            if offset != end + 1:
                raise DownloadError(f"分片长度不足：{offset - start}/{end + 1 - start}")
            return
        except (requests.RequestException, DownloadError, OSError) as error:
            if cancelled():
                raise DownloadError("任务已取消") from error
            attempt += 1
            if attempt > retries:
                raise DownloadError(f"分片多次失败（{start}-{end}）：{error}") from error
            time.sleep(min(2.0 * attempt, 8.0))


def download(
        url: str, headers: dict, target: Path, size: int,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        connections: int = 8,
        chunk_size: int = DEFAULT_CHUNK,
        timeout: float = 60.0,
        retries: int = 2,
) -> int:
    """多连接下载到 target，返回实际字节数。失败抛 DownloadError。"""
    cancelled = cancelled or (lambda: False)
    connections = max(1, min(int(connections or 8), 128))
    probe_timeout = min(timeout, 30.0)
    total, ranged = _probe(url, headers, probe_timeout)
    if not total:
        total = int(size or 0)
    if not total:
        raise DownloadError("无法确定文件大小（直链未返回 Content-Length）")
    target.parent.mkdir(parents=True, exist_ok=True)
    meter = _Meter(progress and (lambda done: progress(done, total)))

    if not ranged or connections == 1 or total <= chunk_size:
        # 单连接顺序下载
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            _download_chunk(url, headers, fd, 0, total - 1,
                            meter, timeout, cancelled, retries)
        finally:
            os.close(fd)
    else:
        parts = max(1, (total + chunk_size - 1) // chunk_size)
        chunks = []
        for index in range(parts):
            start = index * chunk_size
            end = min(total - 1, start + chunk_size - 1)
            chunks.append((start, end))
        fd = os.open(str(target), os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.ftruncate(fd, total)
            with ThreadPoolExecutor(max_workers=min(connections, len(chunks))) as pool:
                futures = [
                    pool.submit(_download_chunk, url, headers, fd, start, end,
                                meter, timeout, cancelled, retries)
                    for start, end in chunks
                ]
                for future in futures:
                    future.result()
        finally:
            os.close(fd)

    actual = target.stat().st_size
    if actual != total:
        raise DownloadError(f"下载大小不符：{actual}/{total}")
    return actual
