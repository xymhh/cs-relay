"""Gopeed REST 客户端：只负责下载腿（夸克单连接限速，必须多连接）。"""

from __future__ import annotations

import time
from typing import Callable

import requests


class GopeedError(RuntimeError):
    pass


class GopeedClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 15.0):
        self._base_url = str(base_url or "").strip().rstrip("/")
        self._token = str(token or "").strip()
        self._timeout = max(1.0, float(timeout or 15.0))

    def _request(self, method: str, path: str, json_body: dict | None = None) -> dict:
        if not self._base_url:
            raise GopeedError("Gopeed 地址未配置")
        headers = {"Accept": "application/json"}
        if self._token:
            headers["X-Api-Token"] = self._token
        try:
            response = requests.request(
                method, f"{self._base_url}{path}",
                headers=headers, json=json_body, timeout=self._timeout,
            )
        except requests.RequestException as error:
            raise GopeedError(f"连接 Gopeed 失败：{error}") from error
        if response.status_code == 401:
            raise GopeedError("Gopeed 鉴权失败（HTTP 401），检查 GOPEED_TOKEN")
        if response.status_code >= 400:
            detail = response.text[:200].replace("\n", " ")
            raise GopeedError(
                f"Gopeed 接口异常：HTTP {response.status_code} {detail}"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise GopeedError("Gopeed 返回非 JSON") from error
        if isinstance(payload, dict) and payload.get("code") not in (0, None):
            raise GopeedError(f"Gopeed 错误码：{payload.get('code')}")
        return payload if isinstance(payload, dict) else {}

    def create_task(
            self, url: str, headers: dict, path: str, name: str,
            connections: int = 0,
    ) -> str:
        body = {
            "req": {"url": str(url or ""), "labels": {}, "extra": {"header": dict(headers or {})}},
            "opts": {"name": str(name or ""), "path": str(path or ""), "selectFiles": []},
        }
        if int(connections or 0) > 0:
            body["opts"]["extra"] = {"connections": int(connections)}
        payload = self._request("POST", "/api/v1/tasks", body)
        task_id = str(payload.get("data") or "")
        if not task_id:
            raise GopeedError("Gopeed 未返回任务 ID")
        return task_id

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        """通用透传（调试用），便于远程安装扩展 / 读扩展设置。"""
        return self._request(method, path, body)

    def info(self) -> dict:
        """读 Gopeed 版本信息（自检用，不创建任务）。"""
        data = self._request("GET", "/api/v1/info").get("data")
        return data if isinstance(data, dict) else {}

    def get_task(self, task_id: str) -> dict:
        data = self._request("GET", f"/api/v1/tasks/{task_id}").get("data")
        return data if isinstance(data, dict) else {}

    def delete_task(self, task_id: str, force: bool = True) -> None:
        suffix = "?force=true" if force else ""
        self._request("DELETE", f"/api/v1/tasks/{task_id}{suffix}")

    def wait(
            self, task_id: str, timeout: float,
            on_progress: Callable[[int, int], None] | None = None,
            cancelled: Callable[[], bool] | None = None,
            interval: float = 2.0,
            stall_timeout: float = 600.0,
    ) -> dict:
        """阻塞等待任务结束；返回最终任务数据。超时/失败/停滞抛 GopeedError。"""
        started = time.monotonic()
        last_progress_at = started
        last_done = -1
        while True:
            if cancelled and cancelled():
                raise GopeedError("任务已取消")
            if time.monotonic() - started > timeout:
                raise GopeedError(f"Gopeed 下载超时（{int(timeout)} 秒）")
            task = self.get_task(task_id)
            status = str(task.get("status") or "").strip().lower()
            progress = task.get("progress") if isinstance(task.get("progress"), dict) else {}
            done = max(0, int(progress.get("downloaded") or 0))
            total = max(0, int(progress.get("length") or 0))
            if done > last_done:
                last_done = done
                last_progress_at = time.monotonic()
            if on_progress:
                on_progress(done, total)
            if status == "done":
                return task
            if status in {"error", "pause", "removed"}:
                # 把 Gopeed 自带的失败原因一起抛出去：status=error 本身无法判断
                # 是 403（直链/请求头被丢）、磁盘权限还是链接失效。
                detail = str(task.get("error") or task.get("msg") or "").strip()
                downloaded = f"，已下载 {done}/{total}" if total else ""
                raise GopeedError(
                    f"Gopeed 任务未完成：status={status}"
                    + (f"，原因={detail[:300]}" if detail else "，无错误详情")
                    + downloaded
                )
            if stall_timeout > 0 and time.monotonic() - last_progress_at > stall_timeout:
                raise GopeedError(f"Gopeed 下载停滞超过 {int(stall_timeout)} 秒")
            time.sleep(interval)
