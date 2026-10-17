"""内置下载腿测试：Range 分片、请求头透传、403、取消。"""

import hashlib
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile

from relay import downloader

PAYLOAD = bytes(range(256)) * 4096  # 1 MiB


class RangeHandler(BaseHTTPRequestHandler):
    """支持 Range 的极简文件服务，并记录收到的请求头。"""

    payload = PAYLOAD
    seen: list[dict] = []
    status_override = 0

    def log_message(self, fmt, *args):  # noqa: A003
        return

    def do_GET(self):  # noqa: N802
        RangeHandler.seen.append(dict(self.headers))
        if RangeHandler.status_override:
            self.send_response(RangeHandler.status_override)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = str(self.headers.get("Range") or "")
        total = len(self.payload)
        if raw.startswith("bytes="):
            start_text, _, end_text = raw[len("bytes="):].partition("-")
            start = int(start_text or 0)
            end = int(end_text) if end_text else total - 1
            end = min(end, total - 1)
            body = self.payload[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(total))
        self.end_headers()
        self.wfile.write(self.payload)


class DownloaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}/file.bin"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        RangeHandler.seen = []
        RangeHandler.status_override = 0
        self.tmp = Path(tempfile.mkdtemp())

    def test_multi_chunk_download_matches_and_forwards_headers(self):
        target = self.tmp / "out.bin"
        progress = []
        size = downloader.download(
            self.url, {"Cookie": "quark=1", "User-Agent": "ua"},
            target, len(PAYLOAD), progress=lambda done, total: progress.append((done, total)),
            connections=4, chunk_size=128 * 1024,
        )
        self.assertEqual(size, len(PAYLOAD))
        self.assertEqual(hashlib.sha1(target.read_bytes()).hexdigest(),
                         hashlib.sha1(PAYLOAD).hexdigest())
        self.assertGreaterEqual(len(RangeHandler.seen), 4)
        # 每个分片请求都要带上 cookie：这正是 Gopeed 侧丢头导致的 403 场景
        self.assertTrue(all(h.get("Cookie") == "quark=1" for h in RangeHandler.seen))
        self.assertTrue(all(h.get("User-Agent") == "ua" for h in RangeHandler.seen))
        self.assertEqual(progress[-1][0], len(PAYLOAD))

    def test_single_connection_when_connections_is_one(self):
        target = self.tmp / "one.bin"
        downloader.download(self.url, {}, target, len(PAYLOAD),
                            connections=1, chunk_size=128 * 1024)
        self.assertEqual(target.read_bytes(), PAYLOAD)

    def test_403_raises_clear_error(self):
        RangeHandler.status_override = 403
        with self.assertRaises(downloader.DownloadError) as ctx:
            downloader.download(self.url, {}, self.tmp / "x.bin", len(PAYLOAD))
        self.assertIn("403", str(ctx.exception))

    def test_cancel_stops_download(self):
        target = self.tmp / "cancel.bin"
        with self.assertRaises(downloader.DownloadError) as ctx:
            downloader.download(self.url, {}, target, len(PAYLOAD),
                                cancelled=lambda: True)
        self.assertIn("取消", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
