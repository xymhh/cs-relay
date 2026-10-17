"""分段下载器：真实本地 HTTP 服务，验证 206 分段与 200 退化路径。"""

import hashlib
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from relay.downloader import DownloadError, download


class Handler(BaseHTTPRequestHandler):
    payload = b""
    supports_range = True

    def log_message(self, *args):
        return

    def do_GET(self):  # noqa: N802
        header = self.headers.get("Range") or ""
        if header and self.supports_range:
            start, end = (int(part) for part in header.split("=")[1].split("-"))
            end = min(end, len(self.payload) - 1)
            body = self.payload[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(self.payload)}")
        else:
            body = self.payload
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DownloaderTest(unittest.TestCase):
    def setUp(self):
        Handler.payload = bytes(range(256)) * 8192  # 2 MiB
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/f.bin"
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        self.server.shutdown()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ranged_download_matches(self):
        Handler.supports_range = True
        seen = []
        dest = self.tmp / "a.bin"
        size = download(
            self.url, {}, dest, len(Handler.payload), connections=4,
            progress=lambda done, total: seen.append(done),
        )
        self.assertEqual(size, len(Handler.payload))
        self.assertEqual(hashlib.sha1(dest.read_bytes()).hexdigest(),
                         hashlib.sha1(Handler.payload).hexdigest())
        self.assertEqual(seen[-1], len(Handler.payload))

    def test_falls_back_when_range_ignored(self):
        Handler.supports_range = False
        dest = self.tmp / "b.bin"
        size = download(self.url, {}, dest, len(Handler.payload), connections=4)
        self.assertEqual(size, len(Handler.payload))
        self.assertEqual(dest.read_bytes(), Handler.payload)

    def test_size_mismatch_raises(self):
        Handler.supports_range = True
        dest = self.tmp / "c.bin"
        with self.assertRaises(DownloadError):
            download(self.url, {}, dest, len(Handler.payload) + 100, connections=2)
