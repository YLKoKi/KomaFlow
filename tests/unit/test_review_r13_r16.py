# -*- coding: utf-8 -*-
"""R13–R16 回归：物理根对象、景别取景、vendor 网格门槛、表情 VMD 归类。

全部用 AST 从脚本里取函数单独执行（这些是注入 Blender / 大 UI 的文件，
不能直接 import），配最小假对象；不需要 Blender / Tk 窗口。
"""

from __future__ import annotations

import ast
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "src/komaflow/adapters/scripts/build.py"
VENDOR = ROOT / "src/komaflow/adapters/vendor/MMD渲染.py"
APP = ROOT / "src/komaflow/ui/app.py"


def _load_functions(path: Path, names: set[str], extra_ns: dict | None = None):
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            nodes.append(node)
    missing = names - {n.name for n in nodes}
    if missing:
        raise AssertionError(f"{path.name} 缺少函数：{missing}")
    namespace: dict = dict(extra_ns or {})
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"),
         namespace)
    return namespace


class _Obj:
    def __init__(self, name, type_="EMPTY", parent=None, vertices=10,
                 slots=1, mmd_type=""):
        self.name = name
        self.type = type_
        self.parent = parent
        self.mmd_type = mmd_type
        self.data = types.SimpleNamespace(vertices=[None] * vertices)
        self.material_slots = [None] * slots


class _Objects(list):
    """``bpy.data.objects``：可迭代 + 支持 ``.get(name)``。"""

    def get(self, name):
        return next((o for o in self if o.name == name), None)


class _BpyData:
    def __init__(self, objects):
        self.objects = _Objects(objects)


# --------------------------------------------------------------------- R13
class RootObjectTests(unittest.TestCase):
    def _namespace(self, objects, recent):
        ns = {
            "bpy": types.SimpleNamespace(data=_BpyData(objects)),
            "_recent_armature": recent,
            "_log": lambda *a, **k: None,
        }
        return _load_functions(BUILD, {"_target_armature", "_mmd_root_object"}, ns)

    def test_root_follows_target_armature_not_first_in_scene(self):
        stage_root = _Obj("StageRoot", mmd_type="ROOT")
        stage_arm = _Obj("StageArm", type_="ARMATURE", parent=stage_root)
        char_root = _Obj("CharRoot", mmd_type="ROOT")
        char_arm = _Obj("CharArm", type_="ARMATURE", parent=char_root)
        # 场景顺序：舞台先入场（旧代码会取 StageRoot）
        ns = self._namespace([stage_root, stage_arm, char_root, char_arm],
                             recent="CharArm")
        self.assertIs(ns["_target_armature"](), char_arm)
        self.assertIs(ns["_mmd_root_object"](), char_root,
                      "多模型场景取了舞台的 ROOT，build_rig 会构错对象")

    def test_no_root_marker_returns_none_not_arm_parent(self):
        """T-36 改了契约：没有 ROOT 时返回 None，**不再**回退 arm.parent。

        回退 arm.parent 可能是网格/普通空物体，拿它当 mmd_root 会 build 到错对象
        （R13 回归点）。后代包含骨架的 ROOT 由 T-36 的新测试覆盖。
        """
        plain = _Obj("PlainParent")
        arm = _Obj("Arm", type_="ARMATURE", parent=plain)
        ns = self._namespace([arm], recent="Arm")
        self.assertIsNone(ns["_mmd_root_object"]())


# --------------------------------------------------------------------- R14
class FramingGeometryTests(unittest.TestCase):
    def test_target_height_is_multiplied_not_divided(self):
        ns = _load_functions(BUILD, {"_framing_target_height"})
        fn = ns["_framing_target_height"]
        self.assertAlmostEqual(fn(2.0, 1.0), 2.0)
        self.assertAlmostEqual(fn(2.0, 0.5), 1.0, msg="半身必须拉近，不是拉远")
        self.assertAlmostEqual(fn(2.0, 0.7), 1.4)

    def test_camera_uses_helper_and_aims_at_framed_center(self):
        text = BUILD.read_text(encoding="utf-8")
        self.assertIn("target_height = _framing_target_height(height, fraction)", text)
        self.assertNotIn("target_height = height / fraction", text)
        self.assertIn("camera_center_z = max_corner.z - target_height / 2.0", text)


# --------------------------------------------------------------------- R15
class VendorMeshPickTests(unittest.TestCase):
    def setUp(self):
        self.pick = _load_functions(VENDOR, {"_pick_mesh"})["_pick_mesh"]

    def test_single_material_mesh_is_accepted(self):
        mesh = _Obj("lowpoly", type_="MESH", slots=1)
        self.assertIs(self.pick([mesh]), mesh, "材质槽少不是无效模型")

    def test_most_vertices_wins_when_multiple_meshes(self):
        small = _Obj("small", type_="MESH", vertices=10)
        big = _Obj("big", type_="MESH", vertices=999)
        self.assertIs(self.pick([small, big]), big)

    def test_no_mesh_raises_clear_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.pick([_Obj("empty", type_="EMPTY")])
        self.assertIn("没有任何网格", str(ctx.exception))


# --------------------------------------------------------------------- R16
class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class ExpressionAssignTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(APP.read_text(encoding="utf-8"), str(APP))
        klass = next(n for n in tree.body
                     if isinstance(n, ast.ClassDef) and n.name == "KomaFlowApp")
        node = next(n for n in klass.body
                    if isinstance(n, ast.FunctionDef) and n.name == "_assign")
        namespace = {"os": __import__("os"), "messagebox": object()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(APP), "exec"),
             namespace)

        class _MessageBox:
            @staticmethod
            def askyesno(*_a, **_k):
                return True

        namespace["messagebox"] = _MessageBox
        self.app = types.SimpleNamespace(
            vars={k: _Var() for k in ("model", "blend", "stage", "motion",
                                      "camera", "status")},
            motion_list=[],
            transparent=_Var("关"),
            logs=[],
            remembered=[],
            added=[],
        )
        self.app._motion_paths = lambda: [p for p in self.app.motion_list if p]
        self.app._add_motion_file = lambda p: self.app.added.append(p)
        self.app._remember_model = lambda p: self.app.remembered.append(p)
        self.app._log = lambda message, level="info": self.app.logs.append(message)
        self.assign = types.MethodType(namespace["_assign"], self.app)

    def test_expression_vmd_goes_to_motion_list(self):
        self.assign("expression", "face.vmd")
        self.assertEqual(self.app.added, ["face.vmd"],
                         "纯表情 VMD 不能去写已删除的 vars['expression']")

    def test_expression_does_not_touch_removed_var(self):
        self.assign("expression", "face.vmd")
        self.assertNotIn("expression", self.app.vars)


if __name__ == "__main__":
    unittest.main()
