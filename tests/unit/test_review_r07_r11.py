# -*- coding: utf-8 -*-
"""R07–R11 回归（发布检查 / 工程复用缓存 / 单帧帧号 / 相机字段）。

审查报告（2026-10-30）：
* R07 运行库许可缺失不阻断发布（build_installer 的 bad 漏了这个标志）
* R08 sidecar 缺失时 usable/why_not 未初始化（UnboundLocalError）
* R09 缓存报告解析失败 cached=None 仍被解引用（AttributeError）
* R10 单帧预览用 total_frames-1 截绝对帧号（渲错帧）
* R11 _project_usable 只认 report.camera（自动机位工程每次被当坏缓存）

全部不启 Blender：R08/R09 用 mock 装配执行器，其余直接调纯函数。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from komaflow.core import pipeline
from komaflow.core.pipeline import (_clamp_still_frame, _project_usable,
                                    _prepare_and_assemble)
from komaflow.core.project import Inputs, Output, Params, Project

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "src/komaflow/adapters/scripts"


# --------------------------------------------------------------------- R07
class ReleaseAuditBadFlagTests(unittest.TestCase):
    def _run_main(self, runtime_licenses_ok: bool):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "build_installer_under_test", ROOT / "tools" / "build_installer.py")
        bi = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(bi)

        report = {
            "文件数": 1, "体积MB": 1.0,
            "隐私命中": [], "运行时路径提示": [],
            "不该带的文件": [], "编译垃圾目录": [],
            "第三方许可在包里": True,
            "运行库许可原文在包里": runtime_licenses_ok,
            "合规文档在包里": True,
            "误带Blender": False,
        }
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(bi, "DIST", Path(tmp)), \
                mock.patch.object(bi, "stage_sources",
                                  return_value={"剥了": 0, "原KB": 0,
                                                "新KB": 0, "省KB": 0}), \
                mock.patch.object(bi, "build_exe", return_value=Path(tmp) / "app"), \
                mock.patch.object(bi, "verify_stripped",
                                  return_value={"包内py文件": 0, "还含注释的": [],
                                                "按设计保留的": [], "素版存在": True}), \
                mock.patch.object(bi, "add_portable_ffmpeg", return_value="（未打包）"), \
                mock.patch.object(bi, "write_readme"), \
                mock.patch.object(bi, "add_language_pack", return_value="教程"), \
                mock.patch.object(bi, "add_english_docs", return_value=[]), \
                mock.patch.object(bi, "tidy_root"), \
                mock.patch.object(bi, "audit", return_value=report), \
                mock.patch.object(sys, "argv", ["build_installer.py", "--no-zip"]):
            return bi.main()

    def test_missing_runtime_license_fails_release(self):
        self.assertEqual(self._run_main(False), 1,
                         "运行库许可缺失时体检必须非零退出（R07）")

    def test_all_licenses_present_passes(self):
        self.assertEqual(self._run_main(True), 0)


# --------------------------------------------------------------- R08 / R09
class _FakeAdapter:
    def script(self, name):
        return SCRIPTS / name


def _report_payload() -> dict:
    return {"ok": True, "total_frames": 10,
            "scene": {"camera": "KomaFlow_Cam"},
            "physics_bake": {"baked": True}}


class _FakeRunner:
    def __init__(self):
        self.calls = 0

    def __call__(self, exe, script, spec, spec_path, **_kwargs):
        self.calls += 1
        Path(spec_path).write_text(json.dumps(spec, ensure_ascii=False),
                                   encoding="utf-8")
        work_blend = Path(spec["work_blend"])
        work_blend.write_bytes(b"W" * 1_100_000)
        Path(spec["report"]).write_text(json.dumps(_report_payload()),
                                        encoding="utf-8")
        return SimpleNamespace(ok=True, report=_report_payload(), returncode=0,
                               stalled=False, timed_out=False, cancelled=False,
                               duration=0.0)


class AssembleCacheRecoveryTests(unittest.TestCase):
    """R08/R09：sidecar 缺失、缓存报告损坏，都不能把整次任务炸掉。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.work = self.root / "work"
        self.work.mkdir(parents=True, exist_ok=True)
        self.out = self.root / "out"
        model = self.root / "model.pmx"
        model.write_bytes(b"PMX")
        project = Project(name="r08")
        project.inputs = Inputs(route="assets", model=str(model))
        project.params = Params()
        # 不透明、也就不用 FFmpeg 预检
        project.params.transparent_touched = True
        project.params.transparent_background = False
        project.output = Output(dir=str(self.out), basename="demo")
        project.blender_exe = sys.executable
        self.project = project
        self.runner = _FakeRunner()
        self.probe = SimpleNamespace(version=(5, 2, 3),
                                     version_string="5.2.3 LTS",
                                     mmd_tools_installed=True,
                                     mmd_tools_version="4.5.7")

    def _assemble(self):
        with mock.patch.object(pipeline.BASELINE, "probe", return_value=self.probe), \
                mock.patch.object(pipeline, "select_adapter",
                                  return_value=_FakeAdapter()), \
                mock.patch.object(pipeline, "run_blender_script",
                                  side_effect=self.runner):
            return _prepare_and_assemble(self.project, self.work,
                                         blender_exe=sys.executable)

    def test_missing_sidecar_is_cache_miss_not_crash(self):
        self._assemble()                      # 正常装配一次，写出 work/durable
        sidecar = self.out / "demo.blend.json"
        self.assertTrue(sidecar.is_file(), "首次装配应写出 sidecar")
        sidecar.unlink()                      # R08 场景：只保留 .blend、没有 sidecar

        calls_before = self.runner.calls
        _adapter, _exe, report = self._assemble()   # 以前这里 UnboundLocalError

        self.assertTrue(report)
        self.assertEqual(self.runner.calls, calls_before,
                         "work 目录里的工程仍可复用，不该重新装配")

    def test_truncated_cached_report_skips_reuse(self):
        self._assemble()
        # R09：清掉 durable（避免走 durable 复用早退），再把缓存报告写坏
        for path in self.out.glob("demo.blend*"):
            path.unlink()
        (self.work / "build_report.json").write_text("{", encoding="utf-8")

        calls_before = self.runner.calls
        _adapter, _exe, report = self._assemble()   # 以前这里 AttributeError

        self.assertTrue(report)
        self.assertEqual(self.runner.calls, calls_before + 1,
                         "缓存损坏应当重新装配，而不是崩")

    def test_non_dict_cached_report_skips_reuse(self):
        self._assemble()
        for path in self.out.glob("demo.blend*"):
            path.unlink()
        (self.work / "build_report.json").write_text("[1, 2, 3]", encoding="utf-8")

        calls_before = self.runner.calls
        _adapter, _exe, report = self._assemble()
        self.assertTrue(report)
        self.assertEqual(self.runner.calls, calls_before + 1)


# --------------------------------------------------------------------- R10
class StillFrameNumberTests(unittest.TestCase):
    def test_absolute_range_is_respected(self):
        report = {"frame_start": 120, "frame_end": 127, "total_frames": 8}
        self.assertEqual(_clamp_still_frame(report, 124), 124,
                         "total_frames 是区间长度，不能拿来截绝对帧号")
        self.assertEqual(_clamp_still_frame(report, 200), 127)
        self.assertEqual(_clamp_still_frame(report, 1), 120)

    def test_falls_back_to_motion_span(self):
        report = {"frame_start": 30, "motion_span": {"max": 300.0}}
        self.assertEqual(_clamp_still_frame(report, 240), 240)
        self.assertEqual(_clamp_still_frame(report, 999), 300)

    def test_falls_back_to_total_frames_when_no_bounds(self):
        report = {"total_frames": 8}
        self.assertEqual(_clamp_still_frame(report, 124), 7)


# --------------------------------------------------------------------- R11
class ProjectUsableCameraTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.blend = Path(self._tmp.name) / "demo.blend"
        self.blend.write_bytes(b"B" * 1_100_000)

    def _usable(self, report):
        return _project_usable(self.blend, report)

    def test_top_level_camera_is_accepted(self):
        self.assertEqual(self._usable({"total_frames": 10, "camera": "Cam"}),
                         (True, ""))

    def test_scene_camera_is_accepted(self):
        ok, why = self._usable({"total_frames": 10,
                                "scene": {"camera": "KomaFlow_Cam"}})
        self.assertTrue(ok, why)

    def test_auto_camera_is_accepted(self):
        ok, why = self._usable({"total_frames": 10,
                                "auto_camera": {"camera": "AutoCam"}})
        self.assertTrue(ok, why)

    def test_no_camera_rejected(self):
        ok, why = self._usable({"total_frames": 10})
        self.assertFalse(ok)
        self.assertIn("相机", why)


if __name__ == "__main__":
    unittest.main()
