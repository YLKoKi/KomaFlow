# -*- coding: utf-8 -*-
"""R02 回归：导出的工程文件绝不能覆盖本次的输入 .blend。

审查报告（2026-10-30）R02：``_durable_paths`` 固定写
``<output.dir>/<basename>.blend``，不检查它是否等于 ``inputs.blend`` /
``stage`` / ``look_from_blend``。用户把上次导出的工程改过、再当路线 B 输入、
输出目录与名字照旧时，装配后的 ``copy2`` 会把用户改过的原文件覆盖掉。

现在冲突时另存为「…（工具工程）.blend」，并返回被避开的输入路径供记日志。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from komaflow.core.pipeline import _durable_paths
from komaflow.core.project import Inputs, Output, Params, Project


class DurablePathCollisionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _project(self, **inputs) -> Project:
        project = Project(name="r02")
        project.inputs = Inputs(**inputs)
        project.params = Params()
        project.output = Output(dir=str(self.root), basename="demo")
        return project

    def _touch(self, name: str) -> Path:
        path = self.root / name
        path.write_bytes(b"USER EDITS IN EXPORTED PROJECT")
        return path

    def test_no_collision_keeps_normal_name(self):
        blend, key, avoided = _durable_paths(self._project())
        self.assertEqual(blend, self.root / "demo.blend")
        self.assertEqual(key, self.root / "demo.blend.json")
        self.assertIsNone(avoided)

    def test_route_b_input_is_not_overwritten(self):
        source = self._touch("demo.blend")
        blend, key, avoided = _durable_paths(
            self._project(route="blend", blend=str(source)))

        self.assertNotEqual(blend.name, source.name,
                            "输出工程与输入的 .blend 同名，会覆盖用户改过的原件")
        self.assertEqual(blend.name, "demo（工具工程）.blend")
        self.assertEqual(avoided, str(source))
        self.assertEqual(key.name, "demo（工具工程）.blend.json")

    def test_stage_blend_input_is_not_overwritten(self):
        stage = self._touch("demo.blend")
        blend, _key, avoided = _durable_paths(
            self._project(route="assets", model=str(self.root / "m.pmx"),
                          stage=str(stage)))
        self.assertEqual(blend.name, "demo（工具工程）.blend")
        self.assertEqual(avoided, str(stage))

    def test_look_from_blend_is_not_overwritten(self):
        look = self._touch("demo.blend")
        project = self._project(route="assets", model=str(self.root / "m.pmx"))
        project.params.look_from_blend = str(look)
        blend, _key, avoided = _durable_paths(project)
        self.assertEqual(blend.name, "demo（工具工程）.blend")
        self.assertEqual(avoided, str(look))


if __name__ == "__main__":
    unittest.main()
