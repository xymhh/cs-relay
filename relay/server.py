"""HTTP 接口（stdlib，无框架）：/healthz、/jobs。"""

from __future__ import annotations

import json
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from .jobs import JobManager

VERSION = "0.1.0"


def _json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(config: Config, manager: JobManager):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"cs-relay/{VERSION}"

        def log_message(self, fmt, *args):  # noqa: A003 - 保持 stdlib 签名
            return

        def _authorized(self) -> bool:
            token = str(self.headers.get("X-Token") or "")
            return bool(config.token) and token == config.token

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                return {}
            raw = self.rfile.read(min(length, 1024 * 1024))
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ValueError("请求体不是合法 JSON")
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return payload

        def do_GET(self):  # noqa: N802 - stdlib 约定
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/healthz":
                usage = shutil.disk_usage(str(config.data_dir))
                return _json(self, 200, {
                    "ok": True,
                    "version": VERSION,
                    "disk_free_gb": round(usage.free / 1024 ** 3, 1),
                })
            if not self._authorized():
                return _json(self, 401, {"error": "unauthorized"})
            if path == "/jobs":
                return _json(self, 200, {"jobs": manager.list()})
            if path.startswith("/jobs/"):
                job = manager.get(path.rsplit("/", 1)[-1])
                if not job:
                    return _json(self, 404, {"error": "job not found"})
                return _json(self, 200, job)
            return _json(self, 404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if not self._authorized():
                return _json(self, 401, {"error": "unauthorized"})
            if path != "/jobs":
                return _json(self, 404, {"error": "not found"})
            try:
                payload = self._body()
                job = manager.submit(payload)
            except ValueError as error:
                return _json(self, 400, {"error": str(error)})
            except Exception as error:  # noqa: BLE001
                return _json(self, 500, {"error": str(error)[:300]})
            return _json(self, 202, job)

        def do_DELETE(self):  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if not self._authorized():
                return _json(self, 401, {"error": "unauthorized"})
            if path.startswith("/jobs/"):
                job_id = path.rsplit("/", 1)[-1]
                job = manager.get(job_id)
                if not job:
                    return _json(self, 404, {"error": "job not found"})
                manager.cancel(job_id)
                return _json(self, 200, {"ok": True})
            return _json(self, 404, {"error": "not found"})

    return Handler


def serve(config: Config, host: str = "0.0.0.0", port: int = 8787) -> None:
    manager = JobManager(config)
    httpd = ThreadingHTTPServer((host, port), make_handler(config, manager))
    httpd.serve_forever()
