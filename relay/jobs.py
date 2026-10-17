"""任务队列：解析直链 -> Gopeed 下载 -> 115 内网上传 -> 校验 -> 清理。

串行执行（35 GB 盘，同时只处理一个文件）。任务状态挂在内存里，并周期性
落盘到 jobs.json，relay 重启后仍能查到最近结果。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .gopeed import GopeedClient, GopeedError
from .p115 import P115Error, Uploader
from .quark import QuarkError, resolve_download_link


def _now() -> float:
    return time.time()


class JobManager:
    def __init__(
            self,
            config: Config,
            resolver: Callable[[str, str], tuple[str, dict]] = resolve_download_link,
            gopeed_factory: Callable[[], GopeedClient] | None = None,
            uploader_factory: Callable[[], Uploader] | None = None,
    ):
        self.config = config
        self._resolver = resolver
        self._gopeed_factory = gopeed_factory or (
            lambda: GopeedClient(config.gopeed_url, config.gopeed_token)
        )
        self._uploader_factory = uploader_factory or (
            lambda: Uploader(
                config.p115_cookie,
                concurrency=config.upload_concurrency,
                part_size=config.part_size,
                internal_endpoint=config.internal_endpoint,
            )
        )
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._cancelled: set[str] = set()
        self._state_file = config.data_dir / "jobs.json"
        self._load_state()
        self._worker = threading.Thread(target=self._run_loop, name="relay-worker", daemon=True)
        self._worker.start()

    # ---------- 对外接口 ----------

    def submit(self, payload: dict) -> dict:
        target = payload.get("target") or {}
        if not isinstance(target, dict):
            raise ValueError("target 必须是对象")
        size = int(payload.get("size") or 0)
        if size <= 0:
            raise ValueError("size 必须大于 0")
        if self.config.max_file_bytes and size > self.config.max_file_bytes:
            raise ValueError(
                f"文件超过中继上限（{size} > {self.config.max_file_bytes} 字节）"
            )
        name = str(payload.get("file_name") or "").strip()
        if not name:
            raise ValueError("file_name 不能为空")
        if not str(payload.get("file_id") or "").strip():
            raise ValueError("file_id 不能为空")
        job = {
            "id": uuid.uuid4().hex[:12],
            "status": "queued",
            "phase": "queued",
            "progress": 0,
            "transferred": 0,
            "total": size,
            "speed": 0,
            "message": "排队中",
            "error": "",
            "created_at": _now(),
            "started_at": 0,
            "finished_at": 0,
            "payload": {
                "provider": str(payload.get("provider") or "quark"),
                "file_id": str(payload.get("file_id") or ""),
                "file_name": name,
                "size": size,
                "sha1": str(payload.get("sha1") or "").upper(),
                "target": {
                    "path": str(target.get("path") or "/"),
                    "name": str(target.get("name") or name),
                },
            },
            "result": {},
        }
        with self._lock:
            self._jobs[job["id"]] = job
        self._save_state()
        self._queue.put(job["id"])
        return dict(job)

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def list(self, limit: int = 50) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda item: item["created_at"], reverse=True)
            return [dict(item) for item in jobs[:limit]]

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["status"] in {"success", "failed", "cancelled"}:
                return False
            self._cancelled.add(job_id)
            job["message"] = "正在取消"
            return True

    # ---------- 内部 ----------

    def _is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancelled

    def _update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.update(fields)

    def _save_state(self) -> None:
        try:
            self.config.data_dir.mkdir(parents=True, exist_ok=True)
            with self._lock:
                snapshot = list(self._jobs.values())
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._state_file)
        except OSError:
            pass

    def _load_state(self) -> None:
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, list):
            return
        for job in raw:
            if isinstance(job, dict) and job.get("id"):
                if job.get("status") in {"queued", "running"}:
                    job["status"] = "failed"
                    job["error"] = "relay 重启，任务中断"
                    job["finished_at"] = _now()
                self._jobs[job["id"]] = job

    def _run_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._run_job(job_id)
            except Exception as error:  # noqa: BLE001 - 任何异常都要落到任务状态
                self._update(
                    job_id, status="failed", phase="failed",
                    error=str(error)[:500], message="任务失败",
                    finished_at=_now(),
                )
            finally:
                self._save_state()
                self._queue.task_done()

    def _run_job(self, job_id: str) -> None:
        job = self.get(job_id)
        if not job:
            return
        payload = job["payload"]
        name = payload["file_name"]
        size = int(payload["size"])
        sha1 = payload["sha1"]
        job_dir = Path(self.config.spend_dir) / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        gopeed_task = ""
        started = _now()
        self._update(
            job_id, status="running", phase="resolve", progress=2,
            message="正在解析源网盘直链", started_at=started,
        )
        try:
            if payload["provider"] != "quark":
                raise ValueError(f"暂不支持源网盘：{payload['provider']}")
            url, headers = self._resolver(self.config.quark_cookie, payload["file_id"])
            # 只打域名和请求头键名（绝不打 cookie/直链令牌），用于判断
            # Gopeed 拿到的到底是什么；403 时靠这一行区分「没带 cookie」还是「链接失效」
            host = ""
            try:
                host = url.split("/")[2]
            except IndexError:
                host = "?"
            print(
                f"[job] {job_id} 直链 host={host} header_keys={sorted(headers)}",
                flush=True,
            )
            if self._is_cancelled(job_id):
                raise GopeedError("任务已取消")
            self._update(
                job_id, phase="download", progress=5,
                message="正在下载源文件（Gopeed 多连接）",
            )
            gopeed = self._gopeed_factory()
            last = {"bytes": 0, "at": time.monotonic()}

            def on_download(done: int, total: int) -> None:
                total = int(total or size)
                now = time.monotonic()
                elapsed = max(0.001, now - last["at"])
                speed = max(0, int((done - last["bytes"]) / elapsed))
                last.update(bytes=done, at=now)
                self._update(
                    job_id, transferred=done, progress=min(94, int(done * 90 / total) if total else 5),
                    speed=speed,
                )

            gopeed_task = gopeed.create_task(
                url, headers, str(job_dir), name, connections=self.config.gopeed_connections,
            )
            gopeed.wait(
                gopeed_task, timeout=self.config.job_timeout,
                on_progress=on_download,
                cancelled=lambda: self._is_cancelled(job_id),
            )
            local_path = job_dir / name
            if not local_path.is_file():
                raise GopeedError(f"下载完成但文件不存在：{local_path}")
            actual_size = local_path.stat().st_size
            if size and actual_size != size:
                raise GopeedError(f"下载大小不符：{actual_size}/{size}")
            if not sha1:
                sha1 = self._sha1(local_path)
            elif self._sha1(local_path) != sha1:
                raise GopeedError("下载后 SHA1 校验失败")
            self._update(
                job_id, phase="upload", progress=95,
                message="正在上传到 115（内网端点）",
            )
            uploader = self._uploader_factory()
            upload_result = uploader.upload(
                str(local_path), payload["target"]["path"], payload["target"]["name"],
                sha1, actual_size,
                progress=lambda done, total: self._update(
                    job_id, transferred=int(done),
                    progress=95 + min(4, int(done * 4 / total) if total else 0),
                ),
                cancelled=lambda: self._is_cancelled(job_id),
            )
            self._update(
                job_id, status="success", phase="done", progress=100,
                transferred=actual_size, speed=0,
                message="已上传到 115",
                result={
                    "file_name": payload["target"]["name"],
                    "target_path": payload["target"]["path"],
                    "size": actual_size,
                    "sha1": sha1,
                    "reused": bool(upload_result.get("reused")),
                    "pickcode": str(upload_result.get("pickcode") or ""),
                },
                finished_at=_now(),
            )
        except (QuarkError, GopeedError, P115Error, ValueError, OSError) as error:
            cancelled = self._is_cancelled(job_id)
            self._update(
                job_id,
                status="cancelled" if cancelled else "failed",
                phase="cancelled" if cancelled else "failed",
                error="" if cancelled else str(error)[:500],
                message="已取消" if cancelled else "任务失败",
                finished_at=_now(),
            )
        finally:
            if gopeed_task:
                try:
                    self._gopeed_factory().delete_task(gopeed_task, force=True)
                except Exception:  # noqa: BLE001 - 清理失败不影响任务结论
                    pass
            shutil.rmtree(job_dir, ignore_errors=True)

    @staticmethod
    def _sha1(path: Path) -> str:
        digest = hashlib.sha1()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest().upper()
