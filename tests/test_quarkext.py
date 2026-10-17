"""quarkext：安装流程状态机（不联网，用假 Gopeed 客户端）。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from relay.quarkext import EXT_NAME, ensure_quark_extension


class FakeGopeed:
    def __init__(self, installed: bool = False):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.installed = installed
        self.settings: dict | None = None

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        self.calls.append((method, path, body))
        if method == "GET" and path == "/api/v1/extensions":
            items = [{"identity": "Gwkang@gopeed-extension-quark", "name": EXT_NAME}] \
                if self.installed else []
            return {"code": 0, "data": items}
        if method == "POST" and path == "/api/v1/extensions":
            self.installed = True
            return {"code": 0, "data": {"identity": "Gwkang@gopeed-extension-quark"}}
        if method == "PUT" and path.endswith("/settings"):
            self.settings = (body or {}).get("settings")
            return {"code": 0}
        return {"code": 0}


class EnsureQuarkExtensionTest(unittest.TestCase):
    def test_installs_and_writes_cookie(self):
        with TemporaryDirectory() as tmp:
            src = Path(tmp) / "extensions-src" / EXT_NAME
            src.mkdir(parents=True)
            (src / "manifest.json").write_text("{}", encoding="utf-8")
            client = FakeGopeed(installed=False)
            status = ensure_quark_extension(client, tmp, "k=v")
            self.assertTrue(status["ok"], status)
            self.assertTrue(status["installed"])
            posts = [c for c in client.calls if c[0] == "POST"]
            self.assertEqual(posts[0][1], "/api/v1/extensions")
            self.assertTrue(posts[0][2]["devMode"])
            self.assertEqual(client.settings["cookie"], "k=v")

    def test_skip_install_when_present(self):
        with TemporaryDirectory() as tmp:
            client = FakeGopeed(installed=True)
            status = ensure_quark_extension(client, tmp, "")
            self.assertTrue(status["ok"], status)
            self.assertFalse(status["installed"])
            self.assertFalse([c for c in client.calls if c[0] == "POST"])
            self.assertIsNone(client.settings)


if __name__ == "__main__":
    unittest.main()
