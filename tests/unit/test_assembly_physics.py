# -*- coding: utf-8 -*-
"""T-50 回归：.blend 无物理的装配策略 + 「继续」自动配对。

* 路线 B（用户自带 .blend）没有刚体/关节时**默认报错**，不能假装有物理；
* 只有用户明确选「仅外观」才允许跳过烘焙；
* 模型预览（0 刚体/0 关节）在「继续」时：有源 PMX → 自动配对
  （PMX 当角色/物理底座，预览 .blend 当观感来源）；无 PMX → 明确弹窗/取消。

假 bpy / AST，不启动 Blender。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "src" / "komaflow" / "adapters" / "scripts" / "build.py"


def _load(names: set[str]) -> dict:
    tree = ast.parse(BUILD.read_text(encoding="utf-8"), str(BUILD))
    nodes = [n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name in names]
    missing = names - {n.name for n in nodes}
    assert not missing, f"build.py 缺少：{missing}"
    namespace: dict = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(BUILD), "exec"),
         namespace)
    return namespace


class RouteBPhysicsDecisionTests(unittest.TestCase):
    def setUp(self):
        self.fn = _load({"_route_b_physics_decision"})["_route_b_physics_decision"]

    def test_no_rigid_bodies_is_error_by_default(self):
        result = self.fn({"verdict": "no_rigid_bodies", "rigidbodies": 0,
                          "joints": 0, "rigid_tracks": 0})
        self.assertEqual(result["action"], "error")
        self.assertEqual(result["reason"], "no-rigid-bodies")
        self.assertIn("无物理数据", result["message"])
        self.assertIn("PMX", result["message"])

    def test_no_rigid_bodies_can_be_appearance_only(self):
        result = self.fn({"verdict": "no_rigid_bodies", "rigidbodies": 0,
                          "joints": 0, "rigid_tracks": 0},
                         allow_no_physics=True)
        self.assertEqual(result["action"], "skip")
        self.assertEqual(result["reason"], "appearance-only")
        self.assertIn("仅外观", result["message"])

    def test_existing_physics_is_baked(self):
        result = self.fn({"verdict": "ok", "rigidbodies": 239,
                          "joints": 3623, "rigid_tracks": 187})
        self.assertEqual(result["action"], "bake")
        self.assertEqual(result["reason"], "ok")

    def test_bodies_without_tracks_still_bakes_after_build_rig(self):
        result = self.fn({"verdict": "bodies_without_tracks",
                          "rigidbodies": 239, "joints": 3623,
                          "rigid_tracks": 0})
        self.assertEqual(result["action"], "bake")
        self.assertEqual(result["reason"], "build-rig-needed")


if __name__ == "__main__":
    unittest.main()
