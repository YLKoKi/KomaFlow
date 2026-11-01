# -*- coding: utf-8 -*-
"""R01 回归：FFmpeg 下载**只允许带可信 SHA256 的源**。

审查报告（2026-10-30）R01：固定版本之后还有滚动构建 / gyan.dev / 第三方镜像，
而 ``_SHA256_BY_URL`` 只有固定版本条目；``fetch()`` 在 ``expected`` 为空时
继续解压安装 —— 固定源失败后就可能装上一个**没校验过的 exe**。

现在规则：
* ``sources()`` 只返回能在 ``_SHA256_BY_URL`` 里查到摘要的地址（钉死版本 + 同版本镜像）；
* ``fetch()`` 再拦一道：没有摘要 = 拒绝；校验失败 = 丢弃，绝不进 ``_extract``。

不联网：全部用 mock。
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from komaflow.media import ffmpeg_fetch


class SourcesArePinnedTests(unittest.TestCase):
    def test_every_source_has_a_sha256(self):
        urls = ffmpeg_fetch.sources()
        self.assertTrue(urls, "一个下载源都没有")
        for url in urls:
            with self.subTest(url=url):
                self.assertIn(url, ffmpeg_fetch._SHA256_BY_URL,
                              "这个源没有可信摘要，fetch() 会免校验安装")
                digest = ffmpeg_fetch._SHA256_BY_URL[url]
                self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_mirrors_share_the_pinned_digest(self):
        pinned = dict(ffmpeg_fetch._PINNED_SOURCES)
        self.assertTrue(pinned, "没有钉死版本")
        mirrors = [u for u in ffmpeg_fetch.sources()
                   if any(p in u for p in ffmpeg_fetch._PINNED_MIRROR_PREFIXES)]
        self.assertTrue(mirrors, "镜像源没了（固定源挂掉时就没兜底了）")
        for url in mirrors:
            host = url.split("/")[2]
            bare = url[len(f"https://{host}/"):]
            with self.subTest(url=url):
                self.assertEqual(ffmpeg_fetch._SHA256_BY_URL[url], pinned[bare],
                                 "镜像必须转发同一个钉死版本，不能换内容")

    def test_unpinned_rolling_builds_are_gone(self):
        urls = ffmpeg_fetch.sources()
        self.assertFalse(any("BtbN/FFmpeg-Builds" in u for u in urls),
                         "滚动构建 latest 又被加回来了（无摘要）")
        self.assertFalse(any("gyan.dev/ffmpeg/builds" in u for u in urls),
                         "gyan.dev 滚动包又被加回来了（无摘要）")


class FetchRefusesUnverifiedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_source_without_digest_is_refused(self):
        url = "https://github.com/some/unpinned.zip"
        with mock.patch.object(ffmpeg_fetch, "install_dir", return_value=self.root), \
                mock.patch.object(ffmpeg_fetch, "sources", return_value=[url]), \
                mock.patch.object(ffmpeg_fetch, "_download") as download, \
                mock.patch.object(ffmpeg_fetch, "_extract") as extract:
            with self.assertRaises(RuntimeError) as ctx:
                ffmpeg_fetch.fetch()
        self.assertIn("没有可信 SHA256", str(ctx.exception))
        download.assert_not_called()
        extract.assert_not_called()

    def test_hash_mismatch_discards_download(self):
        url = ffmpeg_fetch._PINNED_SOURCES[0][0]

        def fake_download(_url, target, _cb=None):
            Path(target).write_bytes(b"x" * 1_200_000)

        with mock.patch.object(ffmpeg_fetch, "install_dir", return_value=self.root), \
                mock.patch.object(ffmpeg_fetch, "sources", return_value=[url]), \
                mock.patch.object(ffmpeg_fetch, "_download", side_effect=fake_download), \
                mock.patch.object(ffmpeg_fetch, "_sha256_of", return_value="0" * 64), \
                mock.patch.object(ffmpeg_fetch, "_extract") as extract:
            with self.assertRaises(RuntimeError) as ctx:
                ffmpeg_fetch.fetch()
        self.assertIn("SHA256 校验失败", str(ctx.exception))
        extract.assert_not_called()

    def test_hash_ok_extracts_and_installs(self):
        url, digest = ffmpeg_fetch._PINNED_SOURCES[0]
        dest = self.root / "ffmpeg"

        def fake_download(_url, target, _cb=None):
            Path(target).write_bytes(b"x" * 1_200_000)

        def fake_extract(_zip, out):
            out = Path(out)
            out.mkdir(parents=True, exist_ok=True)
            (out / "ffmpeg.exe").write_bytes(b"MZ")
            (out / "ffprobe.exe").write_bytes(b"MZ")
            return [out / "ffmpeg.exe", out / "ffprobe.exe"]

        with mock.patch.object(ffmpeg_fetch, "install_dir", return_value=dest), \
                mock.patch.object(ffmpeg_fetch, "sources", return_value=[url]), \
                mock.patch.object(ffmpeg_fetch, "_download", side_effect=fake_download), \
                mock.patch.object(ffmpeg_fetch, "_sha256_of", return_value=digest), \
                mock.patch.object(ffmpeg_fetch, "_extract", side_effect=fake_extract):
            got = ffmpeg_fetch.fetch()

        self.assertEqual(got, dest)
        self.assertTrue((dest / "ffmpeg.exe").is_file())
        self.assertTrue((dest / "ffprobe.exe").is_file())


if __name__ == "__main__":
    unittest.main()
