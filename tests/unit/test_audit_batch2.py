# -*- coding: utf-8 -*-
"""第二批审计的回归测试（A5 A6 A9 A10 A11 B2 C1 C4 C5 C11）。

都不起 Blender / ffmpeg：能纯函数测的就纯函数测，其余用文本断言锁住行为。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


class TestA5Watchdog(unittest.TestCase):
    """A5：看门狗不再等"首次进度"，装配有总超时。"""

    def test_watchdog_no_longer_requires_first_progress(self):
        text = (ROOT / "src/komaflow/adapters/executor.py").read_text(encoding="utf-8")
        self.assertIn("first_progress_grace", text)
        self.assertNotIn(
            "progress_file is not None and last_progress_stamp is not None",
            text, "看门狗又变回『首次进度之后才生效』了")

    def test_assemble_has_timeout(self):
        text = (ROOT / "src/komaflow/core/pipeline.py").read_text(encoding="utf-8")
        self.assertIn("ASSEMBLE_TIMEOUT", text)
        self.assertIn("timeout=ASSEMBLE_TIMEOUT", text)

    def test_stall_reason_reported(self):
        text = (ROOT / "src/komaflow/core/pipeline.py").read_text(encoding="utf-8")
        self.assertIn("stall_reason", text)


class TestA6Cancellable(unittest.TestCase):
    """A6：编码 / 导出 / 拼接都能取消。"""

    def test_ffmpeg_uses_cancellable_spawn(self):
        text = (ROOT / "src/komaflow/media/ffmpeg.py").read_text(encoding="utf-8")
        self.assertIn("def _spawn(", text)
        self.assertIn("cancel is not None and cancel.is_set()", text)
        self.assertNotIn("subprocess.run(\n            command, capture_output=True, text=True, timeout=",
                         text)

    def test_export_and_encode_accept_cancel(self):
        text = (ROOT / "src/komaflow/core/pipeline.py").read_text(encoding="utf-8")
        self.assertIn("def _export_png_frames(", text)
        self.assertIn("cancel=cancel", text)
        self.assertIn("def _encode_png_tracks(\n    *,\n    cancel=None,", text)


class TestA7Timeouts(unittest.TestCase):
    """A7：编码超时按帧数算，不恒定 180 秒。"""

    def test_encode_timeout_scales(self):
        from komaflow.media.ffmpeg import concat_timeout, encode_timeout
        self.assertGreaterEqual(encode_timeout(0), 600)
        self.assertGreater(encode_timeout(100000), 100000)
        self.assertGreaterEqual(concat_timeout(0), 300)
        self.assertGreater(concat_timeout(20), concat_timeout(1))


class TestA9AlphaCodecNameError(unittest.TestCase):
    """A9：`DEFAULT_ALPHA_CODEC` 没带模块前缀会 NameError。"""

    def test_prefix_present(self):
        text = (ROOT / "src/komaflow/ui/app.py").read_text(encoding="utf-8")
        self.assertNotIn("else DEFAULT_ALPHA_CODEC)", text)
        self.assertIn("budget.DEFAULT_ALPHA_CODEC", text)


class TestA10NoBlockingDownload(unittest.TestCase):
    """A10：渲染前的 FFmpeg 下载不许在 Tk 主线程同步跑。"""

    def test_prerender_download_is_async(self):
        text = (ROOT / "src/komaflow/ui/app.py").read_text(encoding="utf-8")
        start = text.index("def _ensure_ffmpeg(")
        end = text.index("def on_start(", start)
        body = text[start:end]
        self.assertIn("_download_ffmpeg_async", body)
        self.assertNotIn("ffmpeg_fetch.fetch(", body,
                         "渲染前又变成同步下载了（界面会冻死）")


class TestA11FirstRunPromise(unittest.TestCase):
    """A11：答"否"不能把 setup_done 置 True，否则"下次会再问"是假话。"""

    def test_flag_only_after_accept(self):
        text = (ROOT / "src/komaflow/ui/app.py").read_text(encoding="utf-8")
        start = text.index("def _first_run_setup(")
        end = text.index("def _save_settings_quietly(", start)
        body = text[start:end]
        no_branch = body.index("if not answer:")
        first_flag_after_no = body.index("ffmpeg_setup_done = True", no_branch)
        self.assertGreater(first_flag_after_no, no_branch,
                           "答『否』的分支里又把标志置 True 了")

    def test_no_fake_settings_entry_promise(self):
        text = (ROOT / "src/komaflow/ui/app.py").read_text(encoding="utf-8")
        self.assertNotIn("去设置里手动下载", text)


class TestB2DownloadSources(unittest.TestCase):
    """B2：直连优先，镜像只作兜底。"""

    def test_direct_first(self):
        from komaflow.media import ffmpeg_fetch
        urls = ffmpeg_fetch.sources()
        self.assertTrue(urls, "一个下载源都没有")
        self.assertTrue(urls[0].startswith("https://github.com/"), urls[0])
        mirrors = [u for u in urls if "ghfast" in u or "ghproxy" in u or "gh-proxy" in u]
        if mirrors:
            first_mirror = min(urls.index(u) for u in mirrors)
            self.assertGreater(first_mirror, 0, "镜像又排到第一个了")


class TestC1ZoomGuard(unittest.TestCase):
    """C1：设置里 ui_zoom 写坏不能把整次 load 拖崩。"""

    def test_bad_zoom_falls_back(self):
        from komaflow.settings import Settings

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"ui_zoom": "abc"}), encoding="utf-8")
            original = Settings.path
            Settings.path = classmethod(lambda cls: path)
            try:
                settings = Settings.load()
                self.assertAlmostEqual(settings.ui_zoom, 1.0)
            finally:
                Settings.path = original


class TestC4JournalRotation(unittest.TestCase):
    """C4：日志超过上限要轮转，不能无限长。"""

    def test_rotation_keeps_file_small(self):
        from komaflow.core.journal import Journal

        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal()
            journal.bind(tmp)
            journal.MAX_BYTES = 4096          # 测试用小阈值
            for i in range(400):
                journal.write("x" * 40 + f" #{i}")
            log = Path(journal._path)
            self.assertTrue(log.is_file())
            self.assertLessEqual(log.stat().st_size, 4096 * 2,
                                 "日志没有被轮转")
            rotated = list(Path(tmp).rglob("*.log.1"))
            self.assertTrue(rotated, "没看到 .log.1 —— 轮转没生效")


class TestC5SingleFrameReusable(unittest.TestCase):
    """C5：单帧预览过的工程不该被判"不可用"。"""

    def test_one_frame_is_usable(self):
        from komaflow.core import pipeline as P

        with tempfile.TemporaryDirectory() as tmp:
            blend = Path(tmp) / "x.blend"
            blend.write_bytes(b"\0" * (2 * 1024 * 1024))    # 2 MB，过了大小线
            ok, why = P._project_usable(blend, {"total_frames": 1,
                                                "camera": {"name": "KomaFlow_Cam"}})
            self.assertTrue(ok, why)

    def test_zero_frames_rejected(self):
        from komaflow.core import pipeline as P

        with tempfile.TemporaryDirectory() as tmp:
            blend = Path(tmp) / "x.blend"
            blend.write_bytes(b"\0" * (2 * 1024 * 1024))
            ok, _ = P._project_usable(blend, {"total_frames": 0,
                                              "camera": {"name": "Cam"}})
            self.assertFalse(ok)


class TestC11AtomicWrites(unittest.TestCase):
    """C11：三处写盘都走"临时文件 + replace"。"""

    def test_settings_and_project_atomic(self):
        for rel in ("src/komaflow/settings.py", "src/komaflow/core/project.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("os.replace(", text, f"{rel} 还不是原子写")

    def test_build_report_atomic(self):
        text = (ROOT / "src/komaflow/adapters/scripts/build.py").read_text(encoding="utf-8")
        self.assertIn("os.replace(tmp, target)", text)


class TestC6ProcessTree(unittest.TestCase):
    def test_taskkill_used(self):
        text = (ROOT / "src/komaflow/adapters/executor.py").read_text(encoding="utf-8")
        self.assertIn('"taskkill"', text)
        self.assertIn("/T", text)


class TestB7DownloadLogic(unittest.TestCase):
    """B7：下载/解压这条用户路径原来零测试覆盖。"""

    def test_extract_picks_ffmpeg_from_nested_zip(self):
        import zipfile
        from komaflow.media import ffmpeg_fetch

        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "ff.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("ffmpeg-7.0-full_build/bin/ffmpeg.exe", b"MZ" + b"\0" * 64)
                zf.writestr("ffmpeg-7.0-full_build/bin/ffprobe.exe", b"MZ" + b"\0" * 64)
                zf.writestr("ffmpeg-7.0-full_build/LICENSE.txt", b"license")
            dest = Path(tmp) / "out"
            got = ffmpeg_fetch._extract(archive, dest)
            names = sorted(p.name for p in got)
            self.assertEqual(names, ["ffmpeg.exe", "ffprobe.exe"], names)

    def test_sources_are_https_and_unique(self):
        from komaflow.media import ffmpeg_fetch
        urls = ffmpeg_fetch.sources()
        self.assertTrue(all(u.startswith("https://") for u in urls), urls)
        self.assertEqual(len(urls), len(set(urls)), "下载源有重复")

    def test_install_dir_is_under_localappdata(self):
        from komaflow.media import ffmpeg_fetch
        path = ffmpeg_fetch.install_dir()
        self.assertIn("KomaFlow", str(path))


class TestBatchProgressMatchesRealSegments(unittest.TestCase):
    """分批进度的分母必须是**真实段范围**，不能用猜的总帧数。"""

    def test_plan_is_empty_when_range_not_filled(self):
        text = (ROOT / "src/komaflow/ui/app.py").read_text(encoding="utf-8")
        start = text.index("def _plan_segments_now(")
        end = text.index("def _describe_batches(", start)
        body = text[start:end]
        self.assertIn("if end <= start:", body)
        self.assertIn("return []", body)
        # 注释里提到 FALLBACK_FRAMES 是解释历史原因，真正的判断看这行代码在不在
        code_lines = [line for line in body.splitlines()
                      if not line.strip().startswith("#")]
        self.assertFalse(
            any("FALLBACK_FRAMES" in line and "total" in line for line in code_lines),
            "又在用兜底帧数猜分批表了")

    def test_progress_event_carries_segment_range(self):
        text = (ROOT / "src/komaflow/core/pipeline.py").read_text(encoding="utf-8")
        self.assertIn("frame_start=segment.start, segment_end=segment.end,", text)

    def test_remaining_after_counts_only_following_segments(self):
        from komaflow.core.pipeline import _remaining_after
        from komaflow.core.segment import plan_segments

        segment = plan_segments(300, None)[0]
        got = _remaining_after(segment, 300, 0, {"seconds_per_frame": 2.0})
        # 只有一段 → 后续段为空 → 0；不该出现"本段剩余也乘 0"那种含糊
        self.assertEqual(got, 0.0)

    def test_remaining_after_scales_with_leftover(self):
        from komaflow.core.pipeline import _remaining_after
        from komaflow.core.segment import plan_segments

        segments = plan_segments(300, 100)
        first = segments[0]
        got = _remaining_after(first, 300, 0, {"seconds_per_frame": 2.0})
        self.assertGreater(got, 0.0)
        self.assertAlmostEqual(got, (300 - first.count) * 2.0, places=3)


class TestLookAlwaysApplied(unittest.TestCase):
    """观感（卡渲材质 + Standard 色彩管理）必须**两条路线都生效**。

    用户报的原话：

    > 这完全不像人，你别光看局部颜色
    > 之前是没这种色彩异常情况的

    实测：路线 B（角色卡放的是 .blend）时原来**跳过观感迁移**，
    渲出来是 MMD 原始材质 + Blender 默认 AgX —— 发灰发白、脸像白面具。
    """

    def test_route_b_also_migrates(self):
        text = (ROOT / "src/komaflow/core/pipeline.py").read_text(encoding="utf-8")
        start = text.index("def _apply_look_migration(")
        end = text.index("def _project_usable(", start)
        body = text[start:end]
        code = [line for line in body.splitlines()
                if not line.strip().startswith("#")]
        self.assertFalse(
            any('report.get("opened_blend")' in line for line in code),
            "路线 B 又被排除在观感迁移之外了")

    def test_default_view_transform_is_standard(self):
        text = (ROOT / "src/komaflow/adapters/scripts/build.py").read_text(encoding="utf-8")
        self.assertIn('scene.view_settings.view_transform = "Standard"', text)
        self.assertIn('scene.view_settings.look = "None"', text)


class TestLookCoversDuplicateMaterials(unittest.TestCase):
    """观感迁移要覆盖 Blender 的重名材质（含叠加后缀）。

    用户问：「能力包的自发光呢？就是不随周围环境光照影响」
    实测：`顏+.001` / `顏+.002`（脸的副本）没被替换，仍是 Principled ——
    它们会被补光照到，与"自发光、环境无关"的口径不一致。
    """

    def test_base_name_strips_stacked_suffixes(self):
        import importlib.util
        from pathlib import Path as P

        path = ROOT / "src/komaflow/adapters/scripts/apply_look.py"
        spec = importlib.util.spec_from_file_location("apply_look_mod", path)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 这个脚本要 bpy，导入会失败
            self.skipTest("apply_look 依赖 bpy，跳过")
        base = module.base_name
        self.assertEqual(base("顏+.001"), "顏+")
        self.assertEqual(base("肌_mmd.001"), "肌")
        self.assertEqual(base("衣1_look"), "衣1")


class TestLowBoneMatchAborts(unittest.TestCase):
    """骨骼只对上 22% 时，装配阶段就要中止，别渲完 50 分钟才让用户自己发现。"""

    def test_threshold_and_raise_present(self):
        text = (ROOT / "src/komaflow/core/pipeline.py").read_text(encoding="utf-8")
        self.assertIn("LOW_MATCH_ABORT", text)
        self.assertIn("KOMEFLOW_ALLOW_LOW_MATCH", text)
        self.assertIn("渲出来人物基本不会动，已在装配阶段中止", text)

    def test_threshold_is_below_normal_reuse_bar(self):
        import importlib.util
        from pathlib import Path as P
        path = ROOT / "src/komaflow/core/pipeline.py"
        src = path.read_text(encoding="utf-8")
        m = [l for l in src.splitlines() if l.startswith("LOW_MATCH_ABORT")]
        self.assertTrue(m, "没找到阈值定义")
        value = float(m[0].split("=")[1].strip())
        self.assertTrue(0.0 < value < 0.6, value)


if __name__ == "__main__":
    unittest.main()
