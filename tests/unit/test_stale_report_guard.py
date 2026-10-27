# -*- coding: utf-8 -*-
"""陈旧 build_report / build_progress 防护 —— 回归测试。

对应缺陷（用户工单"动作没导入"的根因）：
    build_report.json / build_progress.json 是固定名、跨轮共用。
    上一轮崩溃没写报告时，下一轮起进程前不清理、结束时无条件读回，
    就会把**上一轮**的报告（连着 ok=True 与上一轮的覆盖率）当成这一轮
    的结果 —— 用户换动作后渲出旧场景，日志显示旧覆盖率。

这里的测试**不需要 Blender**：exe 用 ``sys.executable``（它收到
``--background`` 这类未知参数会以非零码退出，等价于"本轮崩了"）或
一个现写的批处理（模拟"本轮成功写报告"）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from komaflow.adapters.executor import _read_report_if_fresh, run_blender_script
from komaflow.core.errors import KomaFlowError
from komaflow.core.pipeline import _project_usable
from komaflow.media.ffmpeg import _run

IS_WINDOWS = sys.platform == "win32"


def _noop_script(directory: Path) -> Path:
    script = directory / "noop.kfscript"
    script.write_text("pass\n", encoding="utf-8")
    return script


@unittest.skipUnless(IS_WINDOWS, "用 cmd 作假进程，仅 Windows")
class StaleArtifactCleanupTests(unittest.TestCase):
    """起进程前必须清掉上一轮的 report / progress。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.script = _noop_script(self.work)

    def test_stale_report_and_progress_are_removed_and_not_trusted(self):
        report = self.work / "build_report.json"
        progress = self.work / "build_progress.json"
        report.write_text(json.dumps({"ok": True, "stale": True}), encoding="utf-8")
        progress.write_text('{"message": "上一轮的最后一条进度"}', encoding="utf-8")

        with mock.patch("komaflow.adapters.executor.STALL_SECONDS", 2.0):
            outcome = run_blender_script(
                sys.executable,          # python 拒绝 --background → 非零退出 = "本轮崩了"
                self.script,
                {"report": str(report)},
                spec_path=self.work / "spec.json",
                progress_path=progress,
                timeout=30,
            )

        # 旧文件已被起跑前的清理删除；本轮又没写新的 → 报告必须视为缺失
        self.assertFalse(report.exists(), "旧 report 应在起进程前被删除")
        self.assertFalse(progress.exists(), "旧 progress 应在起进程前被删除")
        self.assertIsNone(outcome.report, "旧 report 不能被当成这一轮的结果")
        self.assertFalse(outcome.ok)
        self.assertNotEqual(outcome.returncode, 0)

    def test_stale_progress_message_is_not_replayed(self):
        progress = self.work / "build_progress.json"
        progress.write_text('{"stage": "save", "message": "正在保存（work.blend）…"}',
                            encoding="utf-8")
        seen: list[dict] = []

        outcome = run_blender_script(
            sys.executable,
            self.script,
            {"report": str(self.work / "build_report.json")},
            spec_path=self.work / "spec.json",
            progress_path=progress,
            timeout=30,
            on_progress=seen.append,
        )

        self.assertNotEqual(outcome.returncode, 0)
        self.assertEqual(seen, [], "上一轮的进度消息不允许被回放成本轮消息")


@unittest.skipUnless(IS_WINDOWS, "用 cmd 批处理当成功进程，仅 Windows")
class FreshReportStillTrustedTests(unittest.TestCase):
    """正常完成的轮次：报告照常读回 —— 防止防护误伤正常流程。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.script = _noop_script(self.work)
        self.report = self.work / "build_report.json"

    def test_fresh_report_written_by_process_is_trusted(self):
        # 用批处理当 "blender"：忽略所有参数，把一份新鲜报告写到
        # 环境变量指定的路径，然后退出码 0。
        python = Path(sys.executable)
        bat = self.work / "fake_blender.bat"
        bat.write_text(
            "@echo off\r\n"
            f"\"{python}\" -c \"import json,os;json.dump({{'ok':True,'fresh':True}},"
            "open(os.environ['KF_TEST_REPORT'],'w',encoding='utf-8'))\"\r\n"
            "exit /b 0\r\n",
            encoding="ascii",
        )
        os.environ["KF_TEST_REPORT"] = str(self.report)
        self.addCleanup(os.environ.pop, "KF_TEST_REPORT", None)

        outcome = run_blender_script(
            str(bat),
            self.script,
            {"report": str(self.report)},
            spec_path=self.work / "spec.json",
            progress_path=self.work / "build_progress.json",
            timeout=60,
        )

        self.assertEqual(outcome.returncode, 0)
        self.assertIsNotNone(outcome.report, "本轮写出的报告应被正常读回")
        self.assertTrue(outcome.report.get("fresh"))
        self.assertTrue(outcome.ok)


class ReportFreshnessUnitTests(unittest.TestCase):
    """_read_report_if_fresh 的时间界限。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "build_report.json"

    def test_missing_file_is_not_trusted(self):
        self.assertIsNone(_read_report_if_fresh(self.path, started=1000.0))

    def test_report_older_than_process_start_is_rejected(self):
        self.path.write_text('{"ok": true}', encoding="utf-8")
        old = 1000.0
        os.utime(self.path, (old, old))
        self.assertIsNone(_read_report_if_fresh(self.path, started=old + 3600))

    def test_report_written_this_round_is_accepted(self):
        self.path.write_text('{"ok": true, "fresh": true}', encoding="utf-8")
        now = 100_000.0
        os.utime(self.path, (now, now))
        payload = _read_report_if_fresh(self.path, started=now - 1.0)
        self.assertIsNotNone(payload)
        self.assertTrue(payload["fresh"])


class FFmpegRunErrorTests(unittest.TestCase):
    """_run 失败分支的回归：旧代码引用了不存在的 ``completed.stderr``，
    一旦 ffmpeg 命令失败就把人话错误变成 ``NameError``。"""

    @unittest.skipUnless(IS_WINDOWS, "用 cmd 制造非零退出，仅 Windows")
    def test_failure_raises_komaflow_error_with_detail(self):
        with self.assertRaises(KomaFlowError) as ctx:
            _run(["cmd", "/c", "exit", "3"], what="回归实验")
        self.assertIn("失败（3）", ctx.exception.detail)

    @unittest.skipUnless(IS_WINDOWS, "用 cmd 制造非零退出，仅 Windows")
    def test_failure_detail_is_not_nameerror(self):
        try:
            _run(["cmd", "/c", "exit", "3"], what="回归实验")
        except NameError as exc:  # pragma: no cover - 修复前会走到这里
            self.fail(f"错误详情被 NameError 取代：{exc}")
        except KomaFlowError:
            pass  # 修复后的正确行为


class ProjectUsableBakeCheckTests(unittest.TestCase):
    """复用判据必须拒绝"物理烘焙失败"的工程档案。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.blend = Path(self._tmp.name) / "work.blend"
        self.blend.write_bytes(b"\0" * (2 * 1024 * 1024))  # >1MB，过"空壳"检查

    def _report(self, **overrides) -> dict:
        report = {"ok": True, "total_frames": 300, "camera": "KomaFlow_Cam",
                  "physics_bake": {"result": "FINISHED", "seconds": 3.0,
                                   "cache": [0, 299], "baked": True}}
        report.update(overrides)
        return report

    def test_bake_error_is_rejected(self):
        report = self._report(physics_bake={"error": "RuntimeError: boom"})
        self.assertEqual(_project_usable(self.blend, report),
                         (False, "上次物理烘焙失败"))

    def test_unfinished_bake_is_rejected(self):
        report = self._report(physics_bake={"result": "FINISHED", "seconds": 1.0,
                                            "baked": False})
        self.assertEqual(_project_usable(self.blend, report),
                         (False, "上次物理烘焙没有完成"))

    def test_minimal_legacy_report_without_bake_is_accepted(self):
        """缺 physics_bake 的极简/历史档案放行（作者 C5 回归测试的形状）。"""
        report = self._report()
        report.pop("physics_bake")
        self.assertEqual(_project_usable(self.blend, report), (True, ""))

    def test_physics_free_route_b_report_is_still_accepted(self):
        """route B 且模型本来就没有刚体：build.py 不写 physics_bake，属合法状态。"""
        report = self._report(route_b_physics={"verdict": "no_rigid_bodies",
                                               "rigidbodies": 0, "rigid_tracks": 0})
        report.pop("physics_bake")
        self.assertEqual(_project_usable(self.blend, report), (True, ""))

    def test_healthy_baked_report_is_accepted(self):
        self.assertEqual(_project_usable(self.blend, self._report()), (True, ""))


class FFmpegFetchSourcesTests(unittest.TestCase):
    """下载源顺序与白名单：钉死版本在前，直连次之，镜像最后。"""

    def test_sources_order_pinned_first_mirrors_last(self):
        from komaflow.media import ffmpeg_fetch as ff

        urls = ff.sources()
        pinned_url = ff._PINNED_SOURCES[0][0]
        direct = ff._GH_ASSET
        mirrored = ff._MIRROR_PREFIXES[0] + ff._GH_ASSET
        self.assertEqual(urls[0], pinned_url, "钉死版本必须排第一")
        self.assertLess(urls.index(direct), urls.index(mirrored),
                        "直连必须在镜像之前")
        # 白名单里的每个镜像域名都各有一个变体
        for prefix in ff._MIRROR_PREFIXES:
            self.assertIn(prefix + ff._GH_ASSET, urls)

    def test_check_url_rejects_non_https_and_unknown_hosts(self):
        from komaflow.media import ffmpeg_fetch as ff

        ok = ff._check_url("https://github.com/GyanD/codexffmpeg/releases/a.zip")
        self.assertEqual(ok, ok)  # 不抛即通过
        for bad in ("http://github.com/a.zip",                    # 非 https
                    "https://evil.example.com/a.zip",             # 未知域名
                    "https://127.0.0.1/a.zip"):                   # 本机地址
            with self.assertRaises(RuntimeError, msg=bad):
                ff._check_url(bad)

    def test_sha256_of_known_content(self):
        import tempfile

        from komaflow.media import ffmpeg_fetch as ff

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sha_probe.bin"
            target.write_bytes(b"abc")
            self.assertEqual(
                ff._sha256_of(target),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


if __name__ == "__main__":
    unittest.main()
