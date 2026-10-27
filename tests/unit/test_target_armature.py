# -*- coding: utf-8 -*-
"""``build.py::_target_armature()`` 的行为回归（PR #10 多骨架修复）。

背景（用户工单实测）：场景里已有别的骨架时（route B 打开用户工程、或一次拖了多个模型），
``bpy.data.objects`` 里排第一的是**先入场**的骨架；动作挂到它上面，刚导入的模型
一根关键帧都没有 —— 表现成“动作没导入、人物全程静止”。
所以规则是：``_import_model`` 记下最近导入的骨架名，有记录用它，没有（纯 route B）
才回退第一套；一套都没有返回 ``None``。

为什么用 AST + 假 bpy：``build.py`` 是注入 Blender 执行的脚本（顶部 ``import bpy``、
模块底部直接 ``main()``），普通导入会立刻开始装配。这里只从源码里取出
``_target_armature`` 单独执行，用最小假 ``bpy`` 覆盖三种情形，**不依赖 Blender**。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "src" / "komaflow" / "adapters" / "scripts" / "build.py"


class _Obj:
    """假的 Blender 物体：只需要 name / type / parent。"""

    def __init__(self, name: str, type_: str = "ARMATURE", parent=None):
        self.name = name
        self.type = type_
        self.parent = parent


class _Objects(list):
    """``bpy.data.objects``：可迭代 + 支持 ``.get(name)``。"""

    def get(self, name: str):
        return next((obj for obj in self if obj.name == name), None)


class _Bpy:
    def __init__(self, objects):
        self.data = SimpleNamespace(objects=_Objects(objects))


def _load(recent: str | None = None):
    """从 build.py 源码取出 ``_target_armature``，连同可改的模块全局。"""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT))
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "_target_armature")
    namespace = {
        "bpy": _Bpy([]),
        "_recent_armature": recent,
        "_log": lambda *args, **kwargs: None,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SCRIPT), "exec"),
         namespace)
    return namespace


class TargetArmatureTests(unittest.TestCase):
    def test_multi_armature_picks_most_recent_import(self):
        """多套骨架：必须选**最近导入**那套，而不是列表第一套。"""
        old, fresh = _Obj("OldRig"), _Obj("FreshRig")
        ns = _load(recent="FreshRig")
        ns["bpy"] = _Bpy([old, fresh])

        chosen = ns["_target_armature"]()
        self.assertIs(chosen, fresh)
        self.assertIsNot(chosen, old)

    def test_no_record_falls_back_to_first_armature(self):
        """没有记录（纯 route B）：回退第一套骨架。"""
        first, second = _Obj("RigA"), _Obj("RigB")
        ns = _load(recent=None)
        ns["bpy"] = _Bpy([first, second])

        self.assertIs(ns["_target_armature"](), first)

    def test_stale_or_non_armature_record_falls_back_to_first(self):
        """记录指向已不存在的名字、或不是骨架时，也不能崩，回退第一套。"""
        first = _Obj("RigA")
        mesh = _Obj("Body", type_="MESH")
        for recent in ("已删掉的骨架", "Body"):
            with self.subTest(recent=recent):
                ns = _load(recent=recent)
                ns["bpy"] = _Bpy([first, mesh])
                self.assertIs(ns["_target_armature"](), first)

    def test_no_armature_returns_none(self):
        """场景里一套骨架都没有：返回 None（调用方据此报“无法挂动作”）。"""
        ns = _load(recent="RigA")
        ns["bpy"] = _Bpy([_Obj("Body", type_="MESH")])

        self.assertIsNone(ns["_target_armature"]())


if __name__ == "__main__":
    unittest.main()
