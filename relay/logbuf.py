"""进程内日志环形缓冲。

`docker compose logs` 只能看到容器标准输出、且要有人去机器上敲命令；
中继的排障需要「随时远程读最近 N 行」，所以所有运行时输出统一走 `log()`：
既打 stdout（保留原有容器日志行为），也进内存环形缓冲，由 /debug/logs 读取。

只放日志，不放 cookie/直链；调用方必须先脱敏。
"""

from __future__ import annotations

import threading
import time
from collections import deque

_MAX_LINES = 2000
_BUFFER: deque[str] = deque(maxlen=_MAX_LINES)
_LOCK = threading.Lock()


def log(message: str) -> None:
    """写一行日志：stdout + 环形缓冲。"""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    with _LOCK:
        _BUFFER.append(line)
    print(line, flush=True)


def tail(lines: int = 200) -> list[str]:
    """取最近 N 行（新的在后）。"""
    count = max(1, min(int(lines or 200), _MAX_LINES))
    with _LOCK:
        return list(_BUFFER)[-count:]
