"""环境变量配置。所有密钥只从环境变量读取，不落日志。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int(value: str, default: int, low: int, high: int) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(parsed, high))


def _read_secret(env: dict, direct_key: str, file_key: str) -> str:
    value = str(env.get(direct_key) or "").strip()
    if value:
        return value
    path = str(env.get(file_key) or "").strip()
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


@dataclass(frozen=True)
class Config:
    token: str
    quark_cookie: str
    p115_cookie: str
    gopeed_url: str
    gopeed_token: str
    gopeed_connections: int
    data_dir: Path
    spend_dir: Path
    upload_concurrency: int
    part_size: int
    max_file_bytes: int
    internal_endpoint: str
    job_timeout: int

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        data_dir = Path(str(env.get("RELAY_DATA_DIR") or "/data"))
        max_gb = _int(env.get("RELAY_MAX_FILE_GB", "25"), 25, 0, 500)
        return cls(
            token=str(env.get("RELAY_TOKEN") or "").strip(),
            quark_cookie=_read_secret(env, "QUARK_COOKIE", "QUARK_COOKIE_FILE"),
            p115_cookie=_read_secret(env, "P115_COOKIE", "P115_COOKIE_FILE"),
            gopeed_url=str(env.get("GOPEED_URL") or "http://gopeed:9999").strip().rstrip("/"),
            gopeed_token=str(env.get("GOPEED_TOKEN") or "").strip(),
            gopeed_connections=_int(env.get("GOPEED_CONNECTIONS", "64"), 64, 0, 512),
            data_dir=data_dir,
            spend_dir=data_dir / "downloads",
            upload_concurrency=_int(env.get("P115_UPLOAD_CONCURRENCY", "4"), 4, 1, 16),
            part_size=_int(env.get("P115_PART_SIZE_MB", "16"), 16, 5, 512) * 1024 * 1024,
            max_file_bytes=max_gb * 1024 * 1024 * 1024,
            internal_endpoint=str(
                env.get("P115_INTERNAL_ENDPOINT")
                or "oss-cn-shenzhen-internal.aliyuncs.com"
            ).strip(),
            job_timeout=_int(env.get("RELAY_JOB_TIMEOUT_SECONDS", "10800"), 10800, 60, 86400),
        )

    def validate(self) -> list[str]:
        problems = []
        if not self.token:
            problems.append("RELAY_TOKEN 未设置（插件鉴权用）")
        if not self.quark_cookie:
            problems.append("QUARK_COOKIE 未设置（解析夸克直链用）")
        if not self.p115_cookie:
            problems.append("P115_COOKIE 未设置（上传 115 用）")
        return problems
