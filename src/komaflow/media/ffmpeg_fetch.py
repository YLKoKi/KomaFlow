"""按需下载 FFmpeg（不自带、不分发，只在用户点“下载”时抓取官方构建）。

为什么要下载而不是随包带：FFmpeg 是 LGPL/GPL，随包分发要履行再分发义务；
“替用户下载”则不涉及再分发，义务留在上游。程序会自动尝试多个来源
（含国内可用的 GitHub 镜像），把 ffmpeg.exe / ffprobe.exe 放到
``%LOCALAPPDATA%\\KomaFlow\\ffmpeg`` 下，之后自动被 ``find_ffmpeg`` 找到。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

#: B2：**先直连官方构建站，镜像只作最后兜底**。
#:
#: 原来的顺序是"镜像优先"，也就是用户一点同意，最先建立连接的是
#: ghfast.top / ghproxy.net / gh-proxy.com 这些第三方转发站 ——
#: 这与"来自官方构建站"的文案不符，也不该是默认行为。
_DIRECT = ""
_MIRROR_PREFIXES = [
    "https://ghfast.top/",
    "https://ghproxy.net/",
    "https://gh-proxy.com/",
]

_GH_ASSET = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
             "ffmpeg-master-latest-win64-gpl.zip")

#: 备用：gyan.dev 的滚动 essentials 构建（直连，无需镜像）
_EXTRA_SOURCES = [
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
]

#: 首选：**钉死的版本化构建**。URL 与 SHA256 一一对应
#: （哈希取自 GitHub Release 资产自带的官方 digest）。
#: 换新版本时更新这里即可；哈希对不上的下载会被直接丢弃并尝试下一个来源。
_PINNED_SOURCES: list[tuple[str, str]] = [
    ("https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/"
     "ffmpeg-9.0.2-essentials_build.zip",
     "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"),
]

_SHA256_BY_URL = dict(_PINNED_SOURCES)

#: 允许发起下载的域名白名单（https only）。下载地址全部来自上方的代码内
#: 常量表，不接任何外部输入；这里再设一道边界，防止未来改动时
#: 意外引入不可信主机。
_ALLOWED_HOSTS = {
    "github.com", "objects.githubusercontent.com", "www.gyan.dev",
    "ghfast.top", "ghproxy.net", "gh-proxy.com",
}


def _check_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
        raise RuntimeError(f"下载地址不在允许列表内：{url}")
    return url


def install_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "KomaFlow" / "ffmpeg"


def sources() -> list[str]:
    """按顺序尝试的下载地址：钉死版本在前，直连次之，镜像最后兜底。"""
    urls = [url for url, _sha in _PINNED_SOURCES]
    urls.append(_DIRECT + _GH_ASSET)
    urls += [*_EXTRA_SOURCES]
    urls += [prefix + _GH_ASSET for prefix in _MIRROR_PREFIXES]
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def _download(url: str, target: Path,
              on_progress: Callable[[str, int, int], None] | None = None) -> None:
    _check_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "KomaFlow"})
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        with target.open("wb") as handle:
            while True:
                chunk = response.read(262144)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(url, done, total)
    if target.stat().st_size < 1_000_000:
        raise RuntimeError("下载内容过小，可能被拦截")


def _extract(zip_path: Path, dest: Path) -> list[Path]:
    """从压缩包里挑出 ffmpeg.exe / ffprobe.exe 放平到 dest。"""
    dest.mkdir(parents=True, exist_ok=True)
    wanted = {"ffmpeg.exe", "ffprobe.exe"}
    found: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise RuntimeError(f"压缩包损坏：{bad}")
        for item in zf.infolist():
            name = Path(item.filename).name.lower()
            if name in wanted:
                target = dest / Path(item.filename).name
                with zf.open(item) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
                found.append(target)
    if len(found) < 2:
        raise RuntimeError("压缩包里没找到 ffmpeg/ffprobe")
    return found


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(on_progress: Callable[[str, int, int], None] | None = None,
          on_note: Callable[[str], None] | None = None) -> Path:
    """下载并解压 FFmpeg，返回安装目录。全部源都失败时抛 :class:`RuntimeError`。"""
    dest = install_dir()
    dest.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for url in sources():
        try:
            if on_note:
                on_note(f"正在从 {url.split('/')[2]} 下载 FFmpeg …")
            with tempfile.TemporaryDirectory() as tmp:
                zip_path = Path(tmp) / "ffmpeg.zip"
                _download(url, zip_path, on_progress)
                expected = _SHA256_BY_URL.get(url)
                if expected:
                    actual = _sha256_of(zip_path)
                    if actual != expected:
                        raise RuntimeError(
                            "SHA256 校验失败（内容与钉死版本不符，已丢弃）："
                            f"{actual}")
                _extract(zip_path, dest)
            if (dest / "ffmpeg.exe").is_file() and (dest / "ffprobe.exe").is_file():
                if on_note:
                    on_note(f"FFmpeg 已就绪：{dest}")
                return dest
            errors.append(f"{url}：解压后缺少可执行文件")
        except Exception as exc:                    # noqa: BLE001
            errors.append(f"{url.split('/')[2]}：{exc}")
            continue
    raise RuntimeError("所有下载源都失败了：\n" + "\n".join(errors))


if __name__ == "__main__":
    def _p(url: str, done: int, total: int) -> None:
        pct = (done / total * 100) if total else 0
        print(f"\r{done/1048576:6.1f} MB / {total/1048576:6.1f} MB  {pct:5.1f}%", end="")

    print(fetch(on_progress=_p, on_note=print))
