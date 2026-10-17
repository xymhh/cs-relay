"""HTTP 接口（stdlib，无框架）：/healthz、/jobs、/debug/*。

`/debug/*` 全部要求 X-Token（与 /jobs 同一枚令牌），用于「像调试本地服务一样
远程调试中继」：读最近日志、看配置是否真正生效、验证夸克直链、跑自检。

设计约束：
- 永不返回 cookie / 直链（只回域名、长度、请求头键名）；
- 自检里的磁盘探测只写 1 MB 临时文件，用完立即删除；
- 所有请求一行访问日志，401 也留痕。
"""

from __future__ import annotations

import json
import shutil
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from . import logbuf
from .config import Config
from .gopeed import GopeedClient, GopeedError
from .jobs import JobManager
from .quark import QuarkError, resolve_download_link
from .quarkext import ensure_quark_extension

VERSION = "0.4.9"


def _json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _host_of(url: str) -> str:
    try:
        return str(url).split("/")[2]
    except IndexError:
        return "?"


def _debug_status(config: Config, manager: JobManager) -> dict:
    usage = shutil.disk_usage(str(config.data_dir))
    jobs = manager.list()
    recent = sorted(jobs, key=lambda job: float(job.get("created_at") or 0))[-5:]
    return {
        "ok": True,
        "version": VERSION,
        "disk_free_gb": round(usage.free / 1024 ** 3, 1),
        "disk_total_gb": round(usage.total / 1024 ** 3, 1),
        "config": {
            "gopeed_url": config.gopeed_url,
            "gopeed_token_configured": bool(config.gopeed_token),
            "gopeed_connections": config.gopeed_connections,
            "download_engine": config.download_engine,
            "quark_cookie_configured": bool(config.quark_cookie),
            "p115_cookie_configured": bool(config.p115_cookie),
            "max_file_bytes": config.max_file_bytes,
            "job_timeout_seconds": config.job_timeout,
            "data_dir": str(config.data_dir),
        },
        "jobs": {
            "total": len(jobs),
            "running": sum(1 for job in jobs if job.get("status") == "running"),
            "failed": sum(1 for job in jobs if job.get("status") == "failed"),
            "recent": [
                {
                    "id": job.get("id"),
                    "status": job.get("status"),
                    "phase": job.get("phase"),
                    "error": str(job.get("error") or "")[:200],
                    "file_name": (job.get("payload") or {}).get("file_name"),
                }
                for job in recent
            ],
        },
    }


def _debug_selfcheck(config: Config) -> dict:
    checks: dict = {}

    # 1) 磁盘：写入 1 MB 再删，验证容器可写 + 剩余空间
    try:
        probe = Path(config.data_dir) / ".relay_write_probe"
        probe.write_bytes(b"\0" * 1024 * 1024)
        written = probe.stat().st_size
        probe.unlink()
        usage = shutil.disk_usage(str(config.data_dir))
        checks["disk"] = {
            "ok": written == 1024 * 1024,
            "free_gb": round(usage.free / 1024 ** 3, 1),
        }
    except OSError as error:
        checks["disk"] = {"ok": False, "error": str(error)[:300]}

    # 2) Gopeed：读 /api/v1/info（不创建任务）
    try:
        info = GopeedClient(config.gopeed_url, config.gopeed_token).info()
        checks["gopeed"] = {"ok": True, "info": info}
    except GopeedError as error:
        checks["gopeed"] = {"ok": False, "error": str(error)[:300]}

    # 3) 凭证是否配置（只报有无，不回报内容）
    # 4) 当前下载腿引擎（builtin 不需要 Gopeed）
    checks["download_engine"] = {"ok": True, "engine": config.download_engine}

    checks["credentials"] = {
        "quark_cookie": bool(config.quark_cookie),
        "p115_cookie": bool(config.p115_cookie),
        "gopeed_token": bool(config.gopeed_token),
    }
    return {"ok": all(bool(v.get("ok")) for k, v in checks.items() if isinstance(v, dict)),
            "version": VERSION, "checks": checks}


def _debug_resolve(config: Config, payload: dict) -> dict:
    """解析夸克直链并真的取 64 KB：验证「链接有效 + 请求头够用」。"""
    file_id = str(payload.get("file_id") or "").strip()
    if not file_id:
        raise ValueError("file_id 不能为空")
    url, headers = resolve_download_link(config.quark_cookie, file_id)
    result = {
        "host": _host_of(url),
        "url_length": len(url),
        "header_keys": sorted(headers),
        "status": None,
        "bytes": 0,
        "error": "",
    }
    try:
        response = requests.get(url, headers=headers, stream=True, timeout=30)
        result["status"] = response.status_code
        try:
            chunk = next(response.iter_content(64 * 1024), b"")
            result["bytes"] = len(chunk or b"")
        finally:
            response.close()
    except requests.RequestException as error:
        result["error"] = str(error)[:300]
    return result


def make_handler(config: Config, manager: JobManager):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"cs-relay/{VERSION}"

        def log_message(self, fmt, *args):  # noqa: A003 - 保持 stdlib 签名
            # 访问日志：插件到底有没有连过来只能靠这一行判断，
            # 401（令牌不匹配）也要留下痕迹，否则外部看起来「完全没有日志」。
            logbuf.log(f"[access] {self.address_string()} {fmt % args}")

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

        def _query(self, key: str, default: str = "") -> str:
            if "?" not in self.path:
                return default
            query = self.path.split("?", 1)[1]
            for part in query.split("&"):
                name, _, value = part.partition("=")
                if name == key:
                    return value
            return default

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
            if path == "/debug/status":
                return _json(self, 200, _debug_status(config, manager))
            if path == "/debug/logs":
                try:
                    lines = int(self._query("lines", "200") or 200)
                except ValueError:
                    lines = 200
                return _json(self, 200, {"lines": logbuf.tail(lines)})
            if path == "/debug/selfcheck":
                return _json(self, 200, _debug_selfcheck(config))
            if path == "/debug/gopeed/test":
                # 用真实 file_id 走一遍「中继解析直链 → Gopeed 多连接下载」，
                # 8 秒后回报 status/速度/错误原文，测完删任务避免占盘。
                try:
                    payload = self._body()
                except ValueError as error:
                    return _json(self, 400, {"error": str(error)})
                file_id = str(payload.get("file_id") or "").strip()
                if not file_id:
                    return _json(self, 400, {"error": "file_id 不能为空"})
                try:
                    gopeed = GopeedClient(config.gopeed_url, config.gopeed_token)
                    cookie = config.quark_cookie
                    try:
                        listing = gopeed.request("GET", "/api/v1/extensions")
                        for ext in listing.get("data") or []:
                            if "quark" not in str(ext.get("name") or "").lower():
                                continue
                            identity = str(ext.get("identity") or "")
                            if not identity:
                                continue
                            detail = gopeed.request(
                                "GET", f"/api/v1/extensions/{identity}"
                            )
                            for item in (detail.get("data") or {}).get("settings") or []:
                                if item.get("name") == "cookie" and item.get("value"):
                                    cookie = str(item["value"])
                                    break
                    except GopeedError:
                        pass
                    url, headers = resolve_download_link(cookie, file_id)
                    task_id = gopeed.create_task(
                        url, headers, "/data/gopeed-test",
                        str(payload.get("name") or "probe.bin"),
                        connections=min(config.gopeed_connections or 8, 16),
                    )
                except (QuarkError, GopeedError) as error:
                    return _json(self, 502, {"error": str(error)[:400]})
                time.sleep(8)
                try:
                    task = gopeed.get_task(task_id)
                except GopeedError as error:
                    task = {"error": str(error)[:300]}
                progress = task.get("progress") if isinstance(task.get("progress"), dict) else {}
                result = {
                    "task": task_id,
                    "status": task.get("status"),
                    "downloaded": progress.get("downloaded"),
                    "length": progress.get("length"),
                    "error": str(task.get("error") or "")[:300],
                }
                try:
                    gopeed.delete_task(task_id, force=True)
                except GopeedError:
                    pass
                return _json(self, 200, result)
            if path == "/debug/gopeed/install-quark":
                status = ensure_quark_extension(
                    GopeedClient(config.gopeed_url, config.gopeed_token),
                    config.data_dir, config.quark_cookie, logbuf.log,
                )
                return _json(self, 200 if status.get("ok") else 502, status)
            if path == "/debug/gopeed/proxy":
                target = self._query("path", "")
                if not target.startswith("/api/v1/"):
                    return _json(self, 400, {"error": "path 必须以 /api/v1/ 开头"})
                try:
                    payload = GopeedClient(
                        config.gopeed_url, config.gopeed_token
                    ).request("GET", target)
                except GopeedError as error:
                    return _json(self, 502, {"error": str(error)[:400]})
                return _json(self, 200, payload)
            return _json(self, 404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if not self._authorized():
                return _json(self, 401, {"error": "unauthorized"})
            if path == "/debug/gopeed/test":
                # 实现在 do_GET 分支；curl -d 默认 POST，这里转发
                return self.do_GET()
            if path == "/jobs":
                try:
                    payload = self._body()
                    job = manager.submit(payload)
                except ValueError as error:
                    logbuf.log(f"[jobs] 400：{error}")
                    return _json(self, 400, {"error": str(error)})
                except Exception as error:  # noqa: BLE001
                    return _json(self, 500, {"error": str(error)[:300]})
                return _json(self, 202, job)
            if path == "/debug/gopeed/proxy":
                # 透传到 Gopeed 的 /api/v1/*：用于远程安装夸克扩展、写扩展设置
                try:
                    payload = self._body()
                except ValueError as error:
                    return _json(self, 400, {"error": str(error)})
                target = str(payload.get("path") or "")
                method = str(payload.get("method") or "POST").upper()
                body = payload.get("body") or None
                if not target.startswith("/api/v1/"):
                    return _json(self, 400, {"error": "path 必须以 /api/v1/ 开头"})
                if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    return _json(self, 400, {"error": "method 不支持"})
                try:
                    result = GopeedClient(
                        config.gopeed_url, config.gopeed_token
                    ).request(method, target, body if isinstance(body, dict) else None)
                except GopeedError as error:
                    return _json(self, 502, {"error": str(error)[:400]})
                logbuf.log(f"[debug] gopeed {method} {target} -> ok")
                return _json(self, 200, result)
            if path == "/debug/quark/probe":
                # 逐个 header 组合试探直链，定位 412 到底卡在哪一项
                try:
                    payload = self._body()
                except ValueError as error:
                    return _json(self, 400, {"error": str(error)})
                file_id = str(payload.get("file_id") or "").strip()
                if not file_id:
                    return _json(self, 400, {"error": "file_id 不能为空"})
                gopeed = GopeedClient(config.gopeed_url, config.gopeed_token)
                cookie = config.quark_cookie
                try:
                    ext = gopeed.request(
                        "GET", "/api/v1/extensions/Gwkang@gopeed-extension-quark"
                    )
                    for item in (ext.get("data") or {}).get("settings") or []:
                        if item.get("name") == "cookie" and item.get("value"):
                            cookie = str(item["value"])
                            break
                except GopeedError:
                    pass
                try:
                    url, _ = resolve_download_link(cookie, file_id)
                except QuarkError as error:
                    return _json(self, 502, {"error": str(error)[:300]})
                ua = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 "
                    "Safari/537.36"
                )
                variants = {
                    "min": {"user-agent": ua, "cookie": cookie, "referer": "https://pan.quark.cn/"},
                    "range": {"user-agent": ua, "cookie": cookie, "referer": "https://pan.quark.cn/", "range": "bytes=0-1023"},
                    "accept_all": {"user-agent": ua, "cookie": cookie, "referer": "https://pan.quark.cn/", "accept": "*/*"},
                    "ua_only": {"user-agent": ua, "referer": "https://pan.quark.cn/"},
                    "cookie_only": {"cookie": cookie, "referer": "https://pan.quark.cn/"},
                }
                results = {}
                for label, headers in variants.items():
                    try:
                        resp = requests.get(url, headers=headers, stream=True, timeout=20)
                        chunk = next(resp.iter_content(1024), b"")
                        results[label] = {"status": resp.status_code, "bytes": len(chunk)}
                        resp.close()
                    except requests.RequestException as error:
                        results[label] = {"error": type(error).__name__}
                from urllib.parse import parse_qs, urlparse
                query = parse_qs(urlparse(url).query)
                return _json(self, 200, {
                    "url_length": len(url),
                    "param_keys": sorted(query.keys()),
                    "ud": (query.get("ud") or [""])[0],
                    "cookie_sha8": __import__("hashlib").sha256(
                        cookie.encode()).hexdigest()[:8],
                    "results": results,
                })
            if path == "/debug/resolve":
                try:
                    return _json(self, 200, _debug_resolve(config, self._body()))
                except (ValueError, QuarkError) as error:
                    return _json(self, 400, {"error": str(error)[:300]})
                except Exception as error:  # noqa: BLE001
                    return _json(self, 500, {"error": str(error)[:300]})
            return _json(self, 404, {"error": "not found"})

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
