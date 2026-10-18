import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from relay.config import Config
from relay.gopeed import GopeedError
from relay.jobs import JobManager
from relay.p115 import internalize


def make_config(tmp: Path) -> Config:
    return Config(
        token="t0ken",
        quark_cookie="quark=1",
        p115_cookie="uid=1",
        gopeed_url="http://gopeed:9999",
        gopeed_token="",
        gopeed_connections=64,
        data_dir=tmp,
        spend_dir=tmp / "downloads",
        upload_concurrency=2,
        part_size=5 * 1024 * 1024,
        max_file_bytes=25 * 1024 ** 3,
        internal_endpoint="oss-cn-shenzhen-internal.aliyuncs.com",
        job_timeout=60,
        download_engine="gopeed",
    )


class FakeGopeed:
    def __init__(self, payload: bytes, fail: bool = False):
        self.payload = payload
        self.fail = fail
        self.deleted = []
        self.connections = []

    def create_task(self, url, headers, path, name, connections=0):
        self.connections.append(connections)
        if self.fail:
            raise GopeedError("boom")
        Path(path).mkdir(parents=True, exist_ok=True)
        (Path(path) / name).write_bytes(self.payload)
        return "task-1"

    def wait(self, task_id, timeout, on_progress=None, cancelled=None):
        if on_progress:
            on_progress(len(self.payload), len(self.payload))
        return {"status": "done"}

    def delete_task(self, task_id, force=True):
        self.deleted.append(task_id)


class FakeUploader:
    def __init__(self):
        self.calls = []

    def upload(self, local_path, remote_path, name, sha1, size, progress=None,
               cancelled=None):
        self.calls.append({
            "path": local_path, "remote": remote_path, "name": name,
            "sha1": sha1, "size": size,
        })
        if progress:
            progress(size, size)
        return {"reused": False, "pickcode": "pc123"}


class ConfigTest(unittest.TestCase):
    def test_validate_reports_missing(self):
        cfg = Config.from_env({"RELAY_DATA_DIR": "/tmp/x"})
        problems = cfg.validate()
        self.assertEqual(len(problems), 3)

    def test_limits_clamped(self):
        cfg = Config.from_env({
            "RELAY_TOKEN": "t", "QUARK_COOKIE": "q", "P115_COOKIE": "p",
            "P115_UPLOAD_CONCURRENCY": "999", "RELAY_MAX_FILE_GB": "-1",
        })
        self.assertEqual(cfg.upload_concurrency, 16)
        self.assertEqual(cfg.max_file_bytes, 0)


class InternalizeTest(unittest.TestCase):
    def test_rewrites_public_endpoint(self):
        url = "https://fhnfile.oss-cn-shenzhen.aliyuncs.com/a/b.mkv?partNumber=1&uploadId=x"
        out = internalize(url, "oss-cn-shenzhen-internal.aliyuncs.com")
        self.assertIn("fhnfile.oss-cn-shenzhen-internal.aliyuncs.com", out)
        self.assertIn("partNumber=1", out)

    def test_leaves_other_hosts(self):
        url = "https://example.com/x"
        self.assertEqual(internalize(url, "oss-cn-shenzhen-internal.aliyuncs.com"), url)


class JobManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_config(self.tmp)
        self.payload = b"hello world" * 1000
        self.sha1 = hashlib.sha1(self.payload).hexdigest().upper()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _wait(self, manager, job_id, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = manager.get(job_id)
            if job and job["status"] in {"success", "failed", "cancelled"}:
                return job
            time.sleep(0.02)
        self.fail("job did not finish")

    def test_success_flow(self):
        gopeed = FakeGopeed(self.payload)
        uploader = FakeUploader()
        manager = JobManager(
            self.cfg, resolver=lambda cookie, fid: ("http://quark/x", {"ua": "q"}),
            gopeed_factory=lambda: gopeed, uploader_factory=lambda: uploader,
        )
        job = manager.submit({
            "provider": "quark", "file_id": "f1", "file_name": "a.mkv",
            "size": len(self.payload), "sha1": self.sha1,
            "target": {"path": "/整理/待整理", "name": "a.mkv"},
        })
        done = self._wait(manager, job["id"])
        self.assertEqual(done["status"], "success")
        self.assertEqual(done["result"]["sha1"], self.sha1)
        self.assertEqual(done["result"]["pickcode"], "pc123")
        self.assertEqual(uploader.calls[0]["remote"], "/整理/待整理")
        self.assertEqual(gopeed.deleted, ["task-1"])
        self.assertFalse((self.cfg.spend_dir / job["id"]).exists())

    def test_job_connections_override_and_default(self):
        gopeed = FakeGopeed(self.payload)
        manager = JobManager(
            self.cfg, resolver=lambda cookie, fid: ("http://quark/x", {}),
            gopeed_factory=lambda: gopeed, uploader_factory=FakeUploader,
        )
        base = {
            "provider": "quark", "file_id": "f1", "file_name": "a.mkv",
            "size": len(self.payload), "sha1": self.sha1,
            "target": {"path": "/x", "name": "a.mkv"},
        }
        job = manager.submit(dict(base, connections=12))
        self.assertEqual(job["payload"]["connections"], 12)
        self._wait(manager, job["id"])
        self.assertEqual(gopeed.connections, [12])

        job2 = manager.submit(dict(base))
        self.assertEqual(job2["payload"]["connections"], 0)
        self._wait(manager, job2["id"])
        self.assertEqual(gopeed.connections[-1], self.cfg.gopeed_connections)

        # 非法值（负数/超范围）按中继默认处理
        job3 = manager.submit(dict(base, connections=999))
        self._wait(manager, job3["id"])
        self.assertEqual(gopeed.connections[-1], self.cfg.gopeed_connections)

    def test_sha1_mismatch_fails_and_cleans(self):
        gopeed = FakeGopeed(self.payload[:-1])
        manager = JobManager(
            self.cfg, resolver=lambda cookie, fid: ("http://quark/x", {}),
            gopeed_factory=lambda: gopeed, uploader_factory=FakeUploader,
        )
        job = manager.submit({
            "provider": "quark", "file_id": "f1", "file_name": "a.mkv",
            "size": len(self.payload), "sha1": self.sha1,
            "target": {"path": "/x", "name": "a.mkv"},
        })
        done = self._wait(manager, job["id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("大小不符", done["error"])

    def test_rejects_oversize_and_missing_fields(self):
        manager = JobManager(
            self.cfg, resolver=lambda *a: ("", {}),
            gopeed_factory=FakeGopeed, uploader_factory=FakeUploader,
        )
        with self.assertRaises(ValueError):
            manager.submit({"file_name": "a", "size": 1, "target": {}})
        with self.assertRaises(ValueError):
            manager.submit({"file_name": "a", "size": self.cfg.max_file_bytes + 1,
                            "target": {"path": "/x"}})


class ServerTest(unittest.TestCase):
    def test_health_and_auth(self):
        from http.server import ThreadingHTTPServer
        from relay.server import make_handler
        import urllib.error
        import urllib.request

        tmp = Path(tempfile.mkdtemp())
        cfg = make_config(tmp)
        manager = JobManager(
            cfg, resolver=lambda *a: ("", {}),
            gopeed_factory=FakeGopeed, uploader_factory=FakeUploader,
        )
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, manager))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with urllib.request.urlopen(f"{base}/healthz") as response:
                self.assertTrue(json.loads(response.read())["ok"])
            request = urllib.request.Request(f"{base}/jobs")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(request)
            self.assertEqual(ctx.exception.code, 401)
            request = urllib.request.Request(
                f"{base}/jobs", headers={"X-Token": "t0ken"},
            )
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.loads(response.read())["jobs"], [])
        finally:
            httpd.shutdown()
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class DebugApiTest(unittest.TestCase):
    """调试接口：令牌保护、只回脱敏信息、日志可远程读。"""

    def setUp(self):
        from http.server import ThreadingHTTPServer

        from relay import logbuf
        from relay.server import make_handler

        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_config(self.tmp)
        # 指向必然连不上的地址，避免自检真的打网络
        self.cfg = Config(
            **{**self.cfg.__dict__, "gopeed_url": "http://127.0.0.1:1"}
        )
        manager = JobManager(
            self.cfg, resolver=lambda *a: ("", {}),
            gopeed_factory=FakeGopeed, uploader_factory=FakeUploader,
        )
        logbuf.log("[test] debug api ready")
        self.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.cfg, manager)
        )
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        import shutil

        self.httpd.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get(self, path: str, token: str | None = "t0ken"):
        import urllib.request

        headers = {"X-Token": token} if token else {}
        request = urllib.request.Request(f"{self.base}{path}", headers=headers)
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read())

    def test_debug_requires_token(self):
        import urllib.error
        import urllib.request

        for path in ("/debug/status", "/debug/logs", "/debug/selfcheck"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"{self.base}{path}")
            self.assertEqual(ctx.exception.code, 401)

    def test_status_masks_secrets(self):
        payload = self._get("/debug/status")
        self.assertTrue(payload["ok"])
        config = payload["config"]
        self.assertTrue(config["quark_cookie_configured"])
        self.assertTrue(config["p115_cookie_configured"])
        text = json.dumps(payload)
        self.assertNotIn("quark=1", text)
        self.assertNotIn("uid=1", text)
        self.assertIn("jobs", payload)

    def test_logs_tail(self):
        payload = self._get("/debug/logs?lines=50")
        self.assertTrue(any("debug api ready" in line for line in payload["lines"]))

    def test_selfcheck_reports_gopeed_failure_without_raising(self):
        payload = self._get("/debug/selfcheck")
        self.assertIn("checks", payload)
        self.assertTrue(payload["checks"]["disk"]["ok"])
        self.assertFalse(payload["checks"]["gopeed"]["ok"])

    def test_resolve_requires_file_id(self):
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            f"{self.base}/debug/resolve", method="POST",
            data=json.dumps({}).encode(), headers={
                "X-Token": "t0ken", "Content-Type": "application/json",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
