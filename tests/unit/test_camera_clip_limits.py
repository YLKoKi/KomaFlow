# -*- coding: utf-8 -*-
"""T-46 回归：活动相机裁剪/景深（clip_end=40 → 舞台被切一半）。

现场：``work.blend`` 的**活动相机 KomaFlow_Cam 的 clip_end=40**，而报告只改了
``KomaFlow_Cam.001``；舞台 67 个远于 40 的对象被裁掉。要求：
* build 保存前对 ``scene.camera`` 强制 ``clip_end=100000``、``dof=False``；
* **不要**把 ``clip_start`` 硬改到 0.01（近物会穿帮）；
* 再遍历所有相机兜底；报告必须记录活动相机名/最终值；
* render.py 打开工程后、渲染前再强制一次。

用 AST 取函数 + 假 bpy 执行，不启 Blender。
"""

from __future__ import annotations

import ast
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "src/komaflow/adapters/scripts/build.py"
RENDER = ROOT / "src/komaflow/adapters/scripts/render.py"


def _load(path: Path, names: set[str], extra: dict) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    nodes = [n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name in names]
    missing = names - {n.name for n in nodes}
    assert not missing, f"{path.name} 缺少：{missing}"
    namespace: dict = dict(extra)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"),
         namespace)
    return namespace


class _Obj:
    def __init__(self, name, type_="EMPTY"):
        self.name = name
        self.type = type_


class _Cam:
    def __init__(self, name, clip_start=0.5, clip_end=40.0, dof=False):
        self.name = name
        self.type = "CAMERA"
        self.data = types.SimpleNamespace(
            clip_start=clip_start, clip_end=clip_end,
            dof=types.SimpleNamespace(use_dof=dof))


class _Scene:
    def __init__(self, camera):
        self.camera = camera


def _bpy(scene, objects):
    return types.SimpleNamespace(
        context=types.SimpleNamespace(scene=scene),
        data=types.SimpleNamespace(objects=list(objects)),
    )


class _Base(unittest.TestCase):
    def _run(self, path, name, scene, objects):
        ns = _load(path, {name, "_same_object"},
                   {"bpy": _bpy(scene, objects),
                    "_log": lambda *a, **k: None})
        return ns[name]()


class BuildRelaxCameraTests(_Base):
    def test_active_camera_is_fixed_and_clip_start_untouched(self):
        active = _Cam("KomaFlow_Cam", clip_start=0.5, clip_end=40.0, dof=True)
        other = _Cam("KomaFlow_Cam.001", clip_start=0.8, clip_end=40.0)
        scene = _Scene(active)
        report = self._run(BUILD, "_relax_camera_limits", scene,
                           [active, other])

        self.assertEqual(active.data.clip_end, 100000.0,
                         "活动相机（最终被渲染的那台）必须放开远裁剪")
        self.assertFalse(active.data.dof.use_dof)
        self.assertEqual(active.data.clip_start, 0.5,
                         "近裁剪不能被硬改成 0.01")
        self.assertEqual(other.data.clip_end, 100000.0)
        self.assertEqual(other.data.clip_start, 0.8)
        self.assertEqual(report["active"], "KomaFlow_Cam")
        self.assertEqual(report["active_final"],
                         [0.5, 100000.0, False])

    def test_active_camera_not_in_objects_list_is_still_fixed(self):
        """活动相机先从 scene.camera 取，不能只依赖 bpy.data.objects 的顺序。"""
        active = _Cam("KomaFlow_Cam", clip_end=40.0)
        other = _Cam("Other", clip_end=40.0)
        scene = _Scene(active)
        report = self._run(BUILD, "_relax_camera_limits", scene, [other])
        self.assertEqual(active.data.clip_end, 100000.0)
        self.assertEqual(report["active"], "KomaFlow_Cam")

    def test_no_camera_does_not_crash(self):
        report = self._run(BUILD, "_relax_camera_limits", _Scene(None), [])
        self.assertIsNone(report["active"])
        self.assertIsNone(report["active_final"])


class RenderForceCameraTests(_Base):
    def test_render_forces_active_camera_before_rendering(self):
        active = _Cam("KomaFlow_Cam", clip_start=0.3, clip_end=40.0, dof=True)
        other = _Cam("KomaFlow_Cam.001", clip_end=40.0)
        scene = _Scene(active)
        report = self._run(RENDER, "_force_render_camera_limits", scene,
                           [active, other])
        self.assertEqual(active.data.clip_end, 100000.0)
        self.assertFalse(active.data.dof.use_dof)
        self.assertEqual(active.data.clip_start, 0.3)
        self.assertEqual(report["active"], "KomaFlow_Cam")
        self.assertEqual(report["active_final"],
                         [0.3, 100000.0, False])

    def test_render_no_camera_does_not_crash(self):
        report = self._run(RENDER, "_force_render_camera_limits",
                          _Scene(None), [])
        self.assertIsNone(report["active"])


if __name__ == "__main__":
    unittest.main()
