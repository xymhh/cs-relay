"""Gopeed 夸克扩展：自动下载 + 安装 + 写入 cookie。

为什么要它：夸克直链单连接被限速，Gopeed 的多连接下载腿需要自己解析链接。
开源扩展（igwkang/gopeed-extension-quark）能在 Gopeed 内部完成
「分享链接转存 → 取直链 → 下载」，并自带 cookie 续期。

中继只负责把扩展文件放进两块容器共享的 /data，然后通过 Gopeed REST API
按 devMode 文件夹方式安装，再把 QUARK_COOKIE 写进扩展设置。
"""

from __future__ import annotations

import io
import shutil
import tarfile
from pathlib import Path
from typing import Callable

import requests

EXT_NAME = "gopeed-extension-quark"
EXT_MIRRORS = (
    "https://ghfast.top/https://github.com/igwkang/gopeed-extension-quark/archive/refs/heads/main.tar.gz",
    "https://gh-proxy.com/https://github.com/igwkang/gopeed-extension-quark/archive/refs/heads/main.tar.gz",
    "https://github.com/igwkang/gopeed-extension-quark/archive/refs/heads/main.tar.gz",
)


def _log(logger: Callable[[str], None] | None, message: str) -> None:
    if logger:
        logger(f"[quark-ext] {message}")


def _download_source(target: Path, logger=None) -> None:
    """下载扩展仓库（含 dist/index.js）并摊平到 target 目录。"""
    errors: list[str] = []
    blob = b""
    for url in EXT_MIRRORS:
        try:
            response = requests.get(url, timeout=60, allow_redirects=True)
            if response.status_code == 200 and len(response.content) > 5000:
                blob = response.content
                _log(logger, f"源码已下载：{url.split('/')[2]} {len(blob)} 字节")
                break
            errors.append(f"{url.split('/')[2]} HTTP {response.status_code}")
        except requests.RequestException as error:
            errors.append(f"{url.split('/')[2]} {type(error).__name__}")
    if not blob:
        raise RuntimeError(f"扩展源码下载失败：{'; '.join(errors)[:300]}")

    staging = target.parent / f".{EXT_NAME}.tmp"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        members = archive.getmembers()
        roots = {m.name.split("/", 1)[0] for m in members if m.name}
        archive.extractall(staging)  # noqa: S202 - 官方仓库 tarball，来源固定
    root = staging / (sorted(roots)[0] if roots else EXT_NAME)
    if not (root / "manifest.json").exists():
        raise RuntimeError("扩展包内缺少 manifest.json")
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    shutil.move(str(root), str(target))
    shutil.rmtree(staging, ignore_errors=True)
    _log(logger, f"已展开到 {target}")


def _find_extension(gopeed, name: str = EXT_NAME) -> dict | None:
    payload = gopeed.request("GET", "/api/v1/extensions")
    items = payload.get("data") or []
    for item in items:
        if str(item.get("name") or "") == name:
            return item
    return None


def ensure_quark_extension(
        gopeed, data_dir: str | Path, cookie: str,
        logger: Callable[[str], None] | None = None,
) -> dict:
    """确保 Gopeed 装了夸克扩展且 cookie 已写入。幂等，可重复调用。"""
    status = {"ok": False, "installed": False, "identity": "", "message": ""}
    target = Path(data_dir) / "extensions-src" / EXT_NAME
    try:
        existing = _find_extension(gopeed)
        needs_files = not (target / "manifest.json").exists()
        if needs_files and existing:
            # 已装好的扩展自带文件副本，不需要重新下载
            _log(logger, "Gopeed 内已装扩展，跳过下载")
        elif needs_files or not existing:
            _download_source(target, logger)
        if not existing:
            gopeed.request(
                "POST", "/api/v1/extensions",
                {"url": str(target), "devMode": True},
            )
            _log(logger, "已按 devMode 文件夹安装")
            status["installed"] = True
            existing = _find_extension(gopeed)
        if not existing:
            raise RuntimeError("安装后仍未在 Gopeed 扩展列表中找到夸克扩展")

        identity = str(existing.get("identity") or "")
        status["identity"] = identity
        if not identity:
            raise RuntimeError("扩展缺少 identity，无法写设置")
        # 装完必须显式启用，否则 Gopeed 不加载 onResolve（会当普通网页下载）
        try:
            gopeed.request(
                "PUT", f"/api/v1/extensions/{identity}/switch", {"status": True},
            )
            _log(logger, "扩展已启用")
        except Exception as error:  # noqa: BLE001 - 启用失败不阻断安装结果
            _log(logger, f"启用扩展失败：{type(error).__name__}: {error}")
        if cookie:
            gopeed.request(
                "PUT", f"/api/v1/extensions/{identity}/settings",
                {"settings": {
                    "cookie": str(cookie).strip(),
                    "delete_file": "0",
                    "max_file_count": "500",
                }},
            )
            _log(logger, "cookie 已写入扩展设置")
        else:
            _log(logger, "QUARK_COOKIE 为空，扩展解析会因无 cookie 失败")
        status["ok"] = True
        status["message"] = "ready"
    except Exception as error:  # noqa: BLE001 - 自检接口要看到真实原因
        status["message"] = f"{type(error).__name__}: {error}"[:300]
        _log(logger, f"失败：{status['message']}")
    return status
