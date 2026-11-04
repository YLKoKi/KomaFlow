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


class PostSavePhysicsVerdictTests(unittest.TestCase):
    """T-53 P1/P3：保存后复核不能把 PMX 静态模型判失败。"""

    def setUp(self):
        self.fn = _load({"_post_save_physics_verdict"})["_post_save_physics_verdict"]

    def test_assets_zero_bodies_is_warning_not_error(self):
        result = self.fn("assets", {"rigidbodies": 0, "world": False},
                         baked=False)
        self.assertEqual(result["errors"], [])
        self.assertTrue(any("物理已跳过" in w for w in result["warnings"]))

    def test_assets_bodies_without_bake_is_error(self):
        result = self.fn("assets", {"rigidbodies": 239, "world": True},
                         baked=False)
        self.assertTrue(result["errors"])
        self.assertIn("未烘焙", result["errors"][0])

    def test_blend_zero_bodies_default_error(self):
        result = self.fn("blend", {"rigidbodies": 0, "world": False},
                         baked=False)
        self.assertTrue(result["errors"])
        self.assertIn("请改用 PMX", result["errors"][0])

    def test_blend_zero_bodies_appearance_only_warning(self):
        result = self.fn("blend", {"rigidbodies": 0, "world": False},
                         baked=False, allow_no_physics=True)
        self.assertEqual(result["errors"], [])
        self.assertTrue(any("仅外观" in w for w in result["warnings"]))

    def test_blend_unbaked_is_warning(self):
        result = self.fn("blend", {"rigidbodies": 239, "world": True},
                         baked=False)
        self.assertEqual(result["errors"], [])
        self.assertTrue(any("未烘焙" in w for w in result["warnings"]))


class AllowNoPhysicsPersistenceTests(unittest.TestCase):
    """T-53 P2-2：仅外观许可随工程保存/重开保持。"""

    def test_project_roundtrip_keeps_allow_flag(self):
        import tempfile
        from pathlib import Path
        from komaflow.core.project import Project

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.kfproj"
            project = Project(name="x")
            project.params.allow_no_physics = True
            project.save(path)
            loaded = Project.load(path)
        self.assertTrue(loaded.params.allow_no_physics)


class AppearanceOnlyOfferTests(unittest.TestCase):
    """T-53 P2-1：路线 B 无物理失败时的「仅外观」入口。"""

    def setUp(self):
        from komaflow.ui import app as app_module

        tree = ast.parse((ROOT / "src/komaflow/ui/app.py").read_text(
            encoding="utf-8"))
        klass = next(n for n in tree.body
                     if isinstance(n, ast.ClassDef) and n.name == "KomaFlowApp")
        node = next(n for n in klass.body
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "_offer_appearance_only")
        self.answer = True
        outer = self

        class _MB:
            @staticmethod
            def askyesno(*_a, **_k):
                return outer.answer

        ns = {"messagebox": _MB}
        exec(compile(ast.Module(body=[node], type_ignores=[]),
                     "app.py", "exec"), ns)

        class _Var:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

            def get(self):
                return self.value

        import types
        logs: list[tuple[str, str]] = []
        self.app = types.SimpleNamespace(
            _allow_no_physics=False, _pending_appearance_only=False,
            vars={"status": _Var()},
            logs=logs,
            _log=lambda msg, level="info": logs.append((level, msg)),
        )
        self.fn = types.MethodType(ns["_offer_appearance_only"], self.app)
        self.assertIsNotNone(app_module)

    def test_offers_and_sets_flag_when_user_accepts(self):
        self.answer = True
        self.assertTrue(self.fn("该 .blend 无物理数据（没有刚体/关节）"))
        self.assertTrue(self.app._allow_no_physics)
        self.assertTrue(self.app._pending_appearance_only)

    def test_cancel_leaves_flag_off(self):
        self.answer = False
        self.assertFalse(self.fn("该 .blend 无物理数据（没有刚体/关节）"))
        self.assertFalse(self.app._allow_no_physics)
        self.assertFalse(self.app._pending_appearance_only)

    def test_other_errors_do_not_offer(self):
        self.assertFalse(self.fn("Blender 退出码 1"))
        self.assertFalse(self.app._allow_no_physics)


if __name__ == "__main__":
    unittest.main()
