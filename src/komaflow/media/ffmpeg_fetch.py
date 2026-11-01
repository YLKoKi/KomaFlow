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

#: 首选：**钉死的版本化构建**。URL 与 SHA256 一一对应
#: （哈希取自 GitHub Release 资产自带的官方 digest）。
#: 换新版本时更新这里即可；哈希对不上的下载会被直接丢弃并尝试下一个来源。
_PINNED_SOURCES: list[tuple[str, str]] = [
    ("https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/"
     "ffmpeg-9.0.2-essentials_build.zip",
     "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"),
]

#: 镜像前缀：**只换传输地址，内容必须是同一个钉死版本**（同一份 SHA256）。
#:
#: ⚠️ 之前这里还挂着 GitHub 上"latest 滚动构建"、gyan.dev 的滚动包 ——
#: 它们没有可信摘要，`fetch()` 会在 expected 为空时**免校验解压安装**。
#: 那不叫兜底，叫把"下到的任意 exe"装上用户的机器。现在一律只用带摘要的源：
#: 钉死版本 + 同一版本的镜像；镜像也失败就报错，由用户自己装 FFmpeg。
_PINNED_MIRROR_PREFIXES = [
    "https://ghfast.top/",
    "https://ghproxy.net/",
    "https://gh-proxy.com/",
]

#: URL → 可信 SHA256。**只有在这张表里的地址才会被下载并安装。**
_SHA256_BY_URL: dict[str, str] = dict(_PINNED_SOURCES)
for _pinned_url, _pinned_sha in list(_PINNED_SOURCES):
    for _mirror_prefix in _PINNED_MIRROR_PREFIXES:
        _SHA256_BY_URL[_mirror_prefix + _pinned_url] = _pinned_sha

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
    """按顺序尝试的下载地址：钉死版本在前，同版本的镜像在后。

    **只返回有可信 SHA256 的地址**（:data:`_SHA256_BY_URL`）—— 没有摘要的源
    连尝试都不尝试，避免退化成"免校验安装"。
    """
    urls = [url for url, _sha in _PINNED_SOURCES]
    urls += [prefix + url for prefix in _PINNED_MIRROR_PREFIXES
             for url, _sha in _PINNED_SOURCES]
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        if url and url not in seen and url in _SHA256_BY_URL:
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
        expected = _SHA256_BY_URL.get(url)
        if not expected:
            # 双保险：sources() 已经过滤过，这里再拦一道 —— 将来谁往
            # sources() 里加了没摘要的源，也绝不允许解压安装。
            errors.append(f"{url.split('/')[2]}：没有可信 SHA256，拒绝安装")
            continue
        try:
            if on_note:
                on_note(f"正在从 {url.split('/')[2]} 下载 FFmpeg …")
            with tempfile.TemporaryDirectory() as tmp:
                zip_path = Path(tmp) / "ffmpeg.zip"
                _download(url, zip_path, on_progress)
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
