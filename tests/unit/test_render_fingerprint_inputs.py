# -*- coding: utf-8 -*-
"""R03 回归：渲染内容指纹必须覆盖**构图 / 观感 / 装配逻辑 / 纳秒时间**。

审查报告（2026-10-30）R03：``render_fingerprint`` 只看素材与画幅，
把 ``framing``（full→half）、``camera_mode``、``look_from_blend``（含文件指纹）、
装配/观感脚本都漏了。结果是改了景别仍复用旧段 —— 界面宣告完成、画面是旧的。
另外素材 mtime 被截成整秒，同大小文件在同一秒内被换掉也识别不出。

这里只算指纹（不渲染、不需要 Blender）。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from komaflow.core import pipeline
from komaflow.core.project import Inputs, Output, Params, Project


class RenderFingerprintCoverageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.model = self.root / "model.pmx"
        self.motion = self.root / "motion.vmd"
        self.model.write_bytes(b"pmx" * 100)
        self.motion.write_bytes(b"vmd" * 100)

    def _project(self) -> Project:
        project = Project(name="r03")
        project.inputs = Inputs(route="assets", model=str(self.model),
                                motions=[str(self.motion)])
        project.params = Params()
        project.output = Output(dir=str(self.root), basename="out")
        return project

    @staticmethod
    def _report() -> dict:
        return {"frame_start": 0, "frame_end": 10}

    def test_framing_change_changes_fingerprint(self):
        project = self._project()
        full = pipeline.render_fingerprint(project, self._report())
        project.params.framing = "half"
        half = pipeline.render_fingerprint(project, self._report())
        self.assertNotEqual(full, half, "改景别必须作废旧段（R03）")

    def test_camera_mode_change_changes_fingerprint(self):
        project = self._project()
        project.params.camera_mode = "vmd"
        a = pipeline.render_fingerprint(project, self._report())
        project.params.camera_mode = "auto"
        b = pipeline.render_fingerprint(project, self._report())
        self.assertNotEqual(a, b, "自动机位与镜头 VMD 是两种画面")

    def test_look_source_change_changes_fingerprint(self):
        look = self.root / "look.blend"
        look.write_bytes(b"A" * 2048)
        project = self._project()
        before = pipeline.render_fingerprint(project, self._report())
        project.params.look_from_blend = str(look)
        with_look = pipeline.render_fingerprint(project, self._report())
        self.assertNotEqual(before, with_look, "换了观感来源必须重渲")

    def test_same_size_overwrite_within_same_second_is_detected(self):
        look = self.root / "look.blend"
        look.write_bytes(b"A" * 2048)
        os.utime(look, ns=(1_000_000_000_000_000_000,) * 2)
        project = self._project()
        project.params.look_from_blend = str(look)
        before = pipeline.render_fingerprint(project, self._report())

        # 同大小、同一秒（只动纳秒）替换内容
        look.write_bytes(b"B" * 2048)
        base = 1_000_000_000_000_000_000
        os.utime(look, ns=(base + 500, base + 500))
        after = pipeline.render_fingerprint(project, self._report())
        self.assertNotEqual(before, after,
                            "纳秒级变更被吞了（旧代码用 int(st_mtime)）")

    def test_script_content_changes_fingerprint(self):
        project = self._project()

        def fake_fingerprint(path) -> str:
            text = str(path)
            if "build.py" in text:
                return "BUILD-A"
            if "MMD渲染" in text:
                return "VENDOR-A"
            return "OTHER-A"

        with mock.patch.object(pipeline, "_script_fingerprint",
                               side_effect=fake_fingerprint):
            before = pipeline.render_fingerprint(project, self._report())

        def fake_fingerprint2(path) -> str:
            text = str(path)
            if "build.py" in text:
                return "BUILD-B"
            if "MMD渲染" in text:
                return "VENDOR-B"
            return "OTHER-A"

        with mock.patch.object(pipeline, "_script_fingerprint",
                               side_effect=fake_fingerprint2):
            after = pipeline.render_fingerprint(project, self._report())

        self.assertNotEqual(before, after,
                            "装配脚本 / vendor 渲染器变了，旧段必须作废")


if __name__ == "__main__":
    unittest.main()
