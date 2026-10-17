"""入口：python -m relay"""

from __future__ import annotations

import os
import sys
import threading
import time

from . import logbuf
from .config import Config
from .gopeed import GopeedClient
from .quarkext import ensure_quark_extension
from .server import serve


def _bootstrap_extension(config: Config) -> None:
    """Gopeed 可能比中继晚就绪：最多等 90 秒再装扩展。"""
    client = GopeedClient(config.gopeed_url, config.gopeed_token)
    for attempt in range(1, 19):
        try:
            client.request("GET", "/api/v1/info")
        except Exception:  # noqa: BLE001 - 未就绪就继续等
            time.sleep(5)
            continue
        ensure_quark_extension(client, config.data_dir, config.quark_cookie, logbuf.log)
        return
    logbuf.log("[quark-ext] Gopeed 90 秒内未就绪，跳过自动安装")


def main() -> int:
    config = Config.from_env()
    problems = config.validate()
    if problems:
        for problem in problems:
            print(f"[config] {problem}", file=sys.stderr)
        return 2
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.spend_dir.mkdir(parents=True, exist_ok=True)
    host = os.environ.get("RELAY_HOST", "0.0.0.0")
    port = int(os.environ.get("RELAY_PORT", "8787"))
    logbuf.log(f"[relay] listening on {host}:{port}, data={config.data_dir}")
    if config.download_engine == "gopeed" and config.quark_ext_auto:
        threading.Thread(target=_bootstrap_extension, args=(config,), daemon=True).start()
    serve(config, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
