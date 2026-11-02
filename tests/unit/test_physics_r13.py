# -*- coding: utf-8 -*-
"""T-36 回归：物理根对象选择 / 物理计数 / 空烤拒绝。

用户反馈 V1.2.1 物理消失（380 帧只烘 1.3 秒）。怀疑：
* R13 的 `_mmd_root_object` 在祖先链找不到 ROOT 时回退 `arm.parent`（可能是网格/普通空物体）
  → build_rig 作用在错误对象；
* build 后 0 刚体 / 0 rigid_track 却照样“烘焙完成” → 假成功。

本测试用 AST 取函数 + 假 bpy 执行，不启动 Blender。
"""

from __future__ import annotations

import ast
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "src/komaflow/adapters/scripts/build.py"


# ---------------------------------------------------------------- 假 bpy 对象
class _Obj:
    def __init__(self, name, type_="EMPTY", parent=None, mmd_type="",
                 children=None, rigid_body=None, rigid_body_constraint=None,
                 mmd_is_built=False, bones=None):
        self.name = name
        self.type = type_
        self.parent = parent
        self.children = list(children or [])
        self.mmd_type = mmd_type
        self.rigid_body = rigid_body
        self.rigid_body_constraint = rigid_body_constraint
        for child in self.children:
            child.parent = self
        self.mmd_root = SimpleNamespace(is_built=mmd_is_built)
        self.pose = SimpleNamespace(bones=list(bones or []))

    def select_set(self, _flag):
        pass


class _Bone:
    def __init__(self, constraints):
        self.constraints = list(constraints)


class _Constraint:
    def __init__(self, name):
        self.name = name


class _Coll(list):
    def __init__(self, items=()):
        super().__init__(items)
        self.objects = list(items)


class _Objects(list):
    def get(self, name):
        return next((o for o in self if o.name == name), None)


class _Scene:
    def __init__(self, world):
        self.rigidbody_world = world

    def frame_set(self, _frame):
        pass


def _world(collection_bodies=0):
    coll = _Coll(collection_bodies if isinstance(collection_bodies, list) else [])
    return SimpleNamespace(collection=coll)


def _load(names: set[str], extra: dict | None = None) -> dict:
    tree = ast.parse(BUILD.read_text(encoding="utf-8"), str(BUILD))
    nodes = [n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name in names]
    missing = names - {n.name for n in nodes}
    assert not missing, f"build.py 缺少：{missing}"
    namespace: dict = dict(extra or {})
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(BUILD), "exec"),
         namespace)
    return namespace


# ------------------------------------------------------------ ① root 选择
class RootSelectionTests(unittest.TestCase):
    def _ns(self, objects, recent):
        ns = {
            "bpy": SimpleNamespace(data=SimpleNamespace(objects=_Objects(objects))),
            "_recent_armature": recent,
            "_log": lambda *a, **k: None,
        }
        return _load({"_iter_descendants", "_target_armature",
                      "_mmd_root_object"}, ns)

    def test_ancestor_root_still_wins(self):
        root = _Obj("Root", mmd_type="ROOT")
        arm = _Obj("Arm", type_="ARMATURE", parent=root)
        ns = self._ns([root, arm], recent="Arm")
        self.assertIs(ns["_mmd_root_object"](), root)

    def test_descendant_root_when_ancestor_chain_broken(self):
        stage_root = _Obj("StageRoot", mmd_type="ROOT")
        plain = _Obj("PlainParent")
        arm = _Obj("CharArm", type_="ARMATURE", parent=plain)
        plain.children.append(arm)
        char_root = _Obj("CharRoot", mmd_type="ROOT", children=[plain])
        # 断掉“祖先链”，但保留树关系（char_root.children 里仍有 plain → arm）
        plain.parent = None
        ns = self._ns([stage_root, arm, char_root, plain], recent="CharArm")

        chosen = ns["_mmd_root_object"]()
        self.assertIs(chosen, char_root,
                      "祖先链断了应按“后代包含骨架”找到角色 ROOT")
        self.assertIsNot(chosen, plain,
                         "绝不能回退 arm.parent（R13 回归点）")
        self.assertIsNot(chosen, stage_root, "不能取舞台的 ROOT")

    def test_no_root_returns_none_not_arm_parent(self):
        plain = _Obj("PlainParent")
        arm = _Obj("Arm", type_="ARMATURE", parent=plain)
        ns = self._ns([plain, arm], recent="Arm")
        self.assertIsNone(ns["_mmd_root_object"](),
                          "没有任何 ROOT 时应返回 None，而不是 arm.parent")


# ------------------------------------------------------------ ② 物理计数
class PhysicsCountsTests(unittest.TestCase):
    def _ns(self, objects, arm, root, collection):
        bpy = SimpleNamespace(
            data=SimpleNamespace(objects=_Objects(objects)),
            context=SimpleNamespace(scene=_Scene(_world(collection))),
        )
        return _load({"_iter_descendants", "_subtree_objects", "_physics_counts"},
                     {"bpy": bpy,
                      "_log": lambda *a, **k: None,
                      "_target_armature": lambda: arm,
                      "_mmd_root_object": lambda: root})

    def _fixture(self):
        body1 = _Obj("b1", rigid_body=object())
        body2 = _Obj("b2", rigid_body=object())
        body3 = _Obj("b3", rigid_body=object())
        joint = _Obj("j1", rigid_body_constraint=object())
        bones = [_Bone([_Constraint("mmd_tools_rigid_track"),
                        _Constraint("mmd_tools_rigid_track"),
                        _Constraint("other")])]
        arm = _Obj("Arm", type_="ARMATURE", bones=bones)
        root = _Obj("Root", mmd_type="ROOT", mmd_is_built=True,
                    children=[body1, body2, body3, joint, arm])
        stage_body = _Obj("stage_body", rigid_body=object())
        return dict(body1=body1, body2=body2, body3=body3, joint=joint,
                    arm=arm, root=root, stage_body=stage_body)

    def test_counts_target_subtree_and_scene_context(self):
        """T-39：目标计数只算 ROOT 子树；scene 刚体/关节另作上下文。"""
        f = self._fixture()
        objects = [f["stage_body"], f["root"], f["body1"], f["body2"],
                   f["body3"], f["joint"], f["arm"]]
        ns = self._ns(objects, f["arm"], f["root"],
                      [f["body1"], f["stage_body"]])

        counts = ns["_physics_counts"]()
        self.assertEqual(counts["rigidbodies"], 3)
        self.assertEqual(counts["joints"], 1)
        self.assertEqual(counts["collection_bodies"], 1,
                         "collection 计数也要限定目标子树（舞台刚体不算）")
        self.assertEqual(counts["rigid_tracks"], 2)
        self.assertEqual(counts["constraints"], 3)
        self.assertEqual(counts["scene_rigidbodies"], 4,
                         "scene 上下文要包含舞台刚体")
        self.assertEqual(counts["scene_joints"], 1)
        self.assertEqual(counts["armature"], "Arm")
        self.assertEqual(counts["root"], "Root")
        self.assertIs(counts["is_built"], True)

    def test_root_none_target_counts_zero_but_scene_context_kept(self):
        f = self._fixture()
        ns = self._ns([f["stage_body"], f["arm"]], f["arm"], None,
                      [f["stage_body"]])
        counts = ns["_physics_counts"]()
        self.assertEqual(counts["rigidbodies"], 0)
        self.assertEqual(counts["joints"], 0)
        self.assertEqual(counts["rigid_tracks"], 0)
        self.assertEqual(counts["constraints"], 0)
        self.assertEqual(counts["scene_rigidbodies"], 1)
        self.assertIsNone(counts["root"])


# ------------------------------------------------------------ ③ 空烤拒绝
class EmptyBakeGuardTests(unittest.TestCase):
    def _run_bake(self, objects, arm, root, build_rig=None, collection=()):
        calls = {"bake": 0}

        def fake_ops():
            calls["bake"] += 1
            return {"FINISHED"}

        bpy = SimpleNamespace(
            data=SimpleNamespace(objects=_Objects(objects)),
            context=SimpleNamespace(
                scene=_Scene(_world(list(collection))),
                view_layer=SimpleNamespace(update=lambda: None),
                selected_objects=[],
            ),
            ops=SimpleNamespace(
                mmd_tools=SimpleNamespace(
                    ptcache_rigid_body_bake=fake_ops)),
        )
        ns = _load({"_iter_descendants", "_subtree_objects", "_physics_counts",
                    "_bake_physics"}, {
            "bpy": bpy,
            "_target_armature": lambda: arm,
            "_mmd_root_object": lambda: root,
            "_build_rig": build_rig or (lambda: {}),
            "_motion_span": lambda: None,
            "_ensure_physics_collection": lambda: 0,
            "_progress": lambda *a, **k: None,
            "_log": lambda *a, **k: None,
            "time": __import__("time"),
            "threading": __import__("threading"),
        })
        return ns["_bake_physics"]({"frame_end": 10}), calls

    def test_zero_target_bodies_skips_bake(self):
        arm = _Obj("Arm", type_="ARMATURE")
        root = _Obj("Root", mmd_type="ROOT", mmd_is_built=True)
        result, calls = self._run_bake([], arm, root)
        self.assertEqual(result.get("reason"), "no-rigid-bodies")
        self.assertIn("skipped", result)
        self.assertEqual(result["counts"]["rigidbodies"], 0)
        self.assertEqual(calls["bake"], 0, "0 刚体不许调用烘焙操作符")

    def test_stage_physics_does_not_mask_empty_character(self):
        """舞台有刚体、角色没有：目标子树计数为 0，仍必须拒烤。"""
        stage_body = _Obj("stage_body", rigid_body=object())
        arm = _Obj("Arm", type_="ARMATURE")
        root = _Obj("Root", mmd_type="ROOT", mmd_is_built=True)  # 无角色刚体
        result, calls = self._run_bake([stage_body, root, arm], arm, root,
                                       collection=[stage_body])
        self.assertIn("skipped", result)
        self.assertEqual(result.get("reason"), "no-rigid-bodies")
        self.assertEqual(result["counts"]["rigidbodies"], 0)
        self.assertEqual(result["counts"]["scene_rigidbodies"], 1,
                         "scene 上下文能看到舞台刚体，但目标计数不能")
        self.assertEqual(calls["bake"], 0, "舞台刚体不能掩盖角色无物理")

    def test_zero_rigid_tracks_is_an_error_and_skips_bake(self):
        body = _Obj("b1", rigid_body=object())
        arm = _Obj("Arm", type_="ARMATURE", bones=[_Bone([])])
        root = _Obj("Root", mmd_type="ROOT", mmd_is_built=True,
                    children=[body, arm])
        result, calls = self._run_bake([body, arm, root], arm, root)
        self.assertIn("error", result)
        self.assertEqual(result.get("reason"), "no-rigid-tracks")
        self.assertEqual(result["counts"]["rigid_tracks"], 0)
        self.assertEqual(calls["bake"], 0, "build_rig 没建出 tracks 不许烘焙")

    def test_no_armature_is_skipped_not_physics_failure(self):
        body = _Obj("b1", rigid_body=object())
        result, calls = self._run_bake([body], None, None)
        self.assertIn("skipped", result)
        self.assertEqual(result.get("reason"), "no-armature")
        self.assertNotIn("error", result)
        self.assertEqual(calls["bake"], 0)

    def test_no_root_is_skipped_not_physics_failure(self):
        body = _Obj("b1", rigid_body=object())
        arm = _Obj("Arm", type_="ARMATURE", bones=[_Bone([])])
        result, calls = self._run_bake([body, arm], arm, None)
        self.assertIn("skipped", result)
        self.assertEqual(result.get("reason"), "no-root")
        self.assertNotIn("error", result)
        self.assertEqual(calls["bake"], 0)


class BuildRigRootFallbackTests(unittest.TestCase):
    def _run_build_rig(self, arm, root):
        bpy = SimpleNamespace(context=SimpleNamespace(selected_objects=[]))
        ns = _load({"_build_rig"}, {
            "bpy": bpy,
            "_target_armature": lambda: arm,
            "_mmd_root_object": lambda: root,
            "_log": lambda *a, **k: None,
        })
        return ns["_build_rig"]()

    def test_no_armature_skipped_with_reason(self):
        result = self._run_build_rig(None, None)
        self.assertEqual(result.get("reason"), "no-armature")

    def test_no_root_skipped_and_does_not_fallback_to_arm_parent(self):
        arm = _Obj("Arm", type_="ARMATURE",
                   parent=_Obj("MeshParent", type_="MESH"))
        result = self._run_build_rig(arm, None)
        self.assertIn("skipped", result)
        self.assertEqual(result.get("reason"), "no-root",
                         "没有 ROOT 时不能拿 arm.parent 去 build")


if __name__ == "__main__":
    unittest.main()
