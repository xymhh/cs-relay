"""多连接分段下载（替代 Gopeed）：夸克单连接有限速，分段并发才跑得起来。

策略：
- 已知文件大小时按 N 段并发 Range 下载，每段独立重试；
- 服务端忽略 Range（返回 200）时自动退回单流下载；
- 任何一段失败重试 3 次，仍失败则整体失败，由任务层清理临时文件。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable

import requests

MIN_SEGMENT = 4 * 1024 * 1024
READ_CHUNK = 256 * 1024


class DownloadError(RuntimeError):
    pass


def _merge_headers(headers: dict | None) -> dict:
    merged = {"Accept-Encoding": "identity"}
    merged.update({str(k): str(v) for k, v in (headers or {}).items()})
    return merged


def _download_segment(
        url: str, headers: dict, dest: Path, start: int, end: int,
        report: Callable[[int], None], cancelled: Callable[[], bool] | None,
        attempts: int = 3,
) -> None:
    position = start
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            with requests.get(
                    url, headers={**headers, "Range": f"bytes={position}-{end}"},
                    stream=True, timeout=(15, 60),
            ) as response:
                if response.status_code == 200 and start > 0:
                    raise DownloadError("服务端忽略了 Range 请求")
                if response.status_code not in (200, 206):
                    raise DownloadError(f"HTTP {response.status_code}")
                with dest.open("r+b") as handle:
                    handle.seek(position)
                    for chunk in response.iter_content(READ_CHUNK):
                        if not chunk:
                            continue
                        if cancelled and cancelled():
                            raise DownloadError("任务已取消")
                        handle.write(chunk)
                        position += len(chunk)
                        report(len(chunk))
                        if position > end:
                            raise DownloadError("服务端返回超出分段的字节")
            if position <= end:
                raise DownloadError(f"分段未下载完整：{position}/{end}")
            return
        except (requests.RequestException, OSError, DownloadError) as error:
            last_error = str(error)
            if isinstance(error, DownloadError) and "已取消" in last_error:
                raise
            if attempt < attempts:
                time.sleep(min(10, 2 ** attempt))
    raise DownloadError(f"分段下载失败（{start}-{end}）：{last_error}")


def download(
        url: str, headers: dict | None, dest: Path, size: int,
        connections: int = 8,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
) -> int:
    """下载到 dest，返回实际字节数。size<=0 时单流下载。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = _merge_headers(headers)
    total = int(size or 0)
    done = 0
    lock = threading.Lock()

    def report(count: int) -> None:
        nonlocal done
        with lock:
            done += count
            current = min(done, total) if total else done
        if progress:
            progress(current, total)

    def single_stream() -> int:
        with requests.get(url, headers=headers, stream=True, timeout=(15, 60)) as response:
            if response.status_code >= 400:
                raise DownloadError(f"HTTP {response.status_code}")
            with dest.open("wb") as handle:
                for chunk in response.iter_content(READ_CHUNK):
                    if chunk:
                        handle.write(chunk)
                        report(len(chunk))
        return dest.stat().st_size

    if total <= 0:
        return single_stream()
    try:
        probe = requests.get(
            url, headers={**headers, "Range": "bytes=0-0"}, stream=True, timeout=(15, 60),
        )
        supports_range = probe.status_code == 206
        probe.close()
    except requests.RequestException:
        supports_range = False
    if not supports_range:
        return single_stream()

    dest.write_bytes(b"")
    with dest.open("r+b") as handle:
        handle.truncate(total)
    segments = max(1, min(int(connections or 1), 16))
    step = max(MIN_SEGMENT, (total + segments - 1) // segments)
    ranges = []
    start = 0
    while start < total:
        end = min(total - 1, start + step - 1)
        ranges.append((start, end))
        start = end + 1
    errors: list[str] = []

    def worker(bounds: tuple[int, int]) -> None:
        try:
            _download_segment(url, headers, dest, bounds[0], bounds[1], report, cancelled)
        except Exception as error:  # noqa: BLE001
            errors.append(str(error))

    threads = [threading.Thread(target=worker, args=(bounds,), daemon=True) for bounds in ranges]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise DownloadError(errors[0])
    actual = dest.stat().st_size
    if actual != total:
        raise DownloadError(f"下载大小不符：{actual}/{total}")
    return actual
