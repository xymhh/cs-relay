import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from relay.config import Config
from relay.downloader import DownloadError
from relay.jobs import JobManager
from relay.p115 import internalize


def make_config(tmp: Path) -> Config:
    return Config(
        token="t0ken",
        quark_cookie="quark=1",
        p115_cookie="uid=1",
        download_connections=4,
        data_dir=tmp,
        spend_dir=tmp / "downloads",
        upload_concurrency=2,
        part_size=5 * 1024 * 1024,
        max_file_bytes=25 * 1024 ** 3,
        internal_endpoint="oss-cn-shenzhen-internal.aliyuncs.com",
        job_timeout=60,
    )


class FakeDownload:
    def __init__(self, payload: bytes, fail: bool = False):
        self.payload = payload
        self.fail = fail

    def __call__(self, url, headers, dest, size, connections=4,
                 progress=None, cancelled=None):
        if self.fail:
            raise DownloadError("boom")
        Path(dest).write_bytes(self.payload)
        if progress:
            progress(len(self.payload), len(self.payload))
        return len(self.payload)


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
        downloader = FakeDownload(self.payload)
        uploader = FakeUploader()
        manager = JobManager(
            self.cfg, resolver=lambda cookie, fid: ("http://quark/x", {"ua": "q"}),
            download_func=downloader, uploader_factory=lambda: uploader,
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
        self.assertFalse((self.cfg.spend_dir / job["id"]).exists())

    def test_sha1_mismatch_fails_and_cleans(self):
        downloader = FakeDownload(self.payload[:-1])
        manager = JobManager(
            self.cfg, resolver=lambda cookie, fid: ("http://quark/x", {}),
            download_func=downloader, uploader_factory=FakeUploader,
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
            download_func=FakeDownload(b""), uploader_factory=FakeUploader,
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
            download_func=FakeDownload(b""), uploader_factory=FakeUploader,
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


if __name__ == "__main__":
    unittest.main()
