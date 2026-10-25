"""入口：python -m relay"""

from __future__ import annotations

import os
import sys

from . import logbuf
from .config import Config
from .server import serve


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
    logbuf.log(
        f"[relay] listening on {host}:{port}, data={config.data_dir}, "
        f"download=builtin/{config.download_connections}conn/"
        f"{config.download_chunk_bytes // 1048576}MB"
    )
    serve(config, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
