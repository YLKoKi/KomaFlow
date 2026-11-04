# -*- coding: utf-8 -*-
"""R06 回归：角色卡的「PMX」与「整份 .blend 工程」必须互斥（所有入口）。

审查报告（2026-10-30）R06 + reviewer T-28 P1：三个入口都漏清另一条 ——
``_assign``、``_clear_zone``、``accept_model_render``；启动恢复还可能把两条
同时填回来（第四入口）。卡片显示新 PMX、``_collect_project`` 却按旧 blend
走 → 实际渲的是旧工程。

这里用 AST 取出真实方法 + 假对象执行（不建 Tk 窗口）。
"""

from __future__ import annotations

import ast
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "src" / "komaflow" / "ui" / "app.py"


class _Var:
    def __init__(self, value: str = ""):
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class _Settings:
    def __init__(self):
        self.last_model = ""
        self.last_blend = ""
        self.last_look_blend = ""
        self.saves = 0

    def save(self):
        self.saves += 1


class _FakeApp:
    def __init__(self):
        self.vars = {name: _Var() for name in
                     ("model", "blend", "stage", "motion", "camera", "status")}
        self.motion_list: list[str] = []
        self.transparent = _Var("关")
        self.remembered: list[str] = []
        self.logs: list[tuple[str, str]] = []
        self.refreshed = 0
        self.panels = 0
        self.reset_calls = 0
        self.settings = _Settings()
        self.outputs: list[str] = []

    def _motion_paths(self):
        return [p for p in self.motion_list if p]

    def _set_motion_paths(self, paths):
        self.motion_list = list(paths)

    def _remember_model(self, path):
        self.remembered.append(path)
        # 真实实现会按后缀二选一写设置；这里复刻关键副作用，便于断言
        if str(path).lower().endswith(".blend"):
            self.settings.last_blend, self.settings.last_model = str(path), ""
        else:
            self.settings.last_model, self.settings.last_blend = str(path), ""

    def _log(self, message, level="info"):
        self.logs.append((level, message))

    def _refresh_cards(self):
        self.refreshed += 1

    def _refresh_panels(self):
        self.panels += 1

    def reset_cards_to_drop(self):
        self.reset_calls += 1


def _load_methods(names: set[str] | None = None):
    """从 app.py 里取出指定方法（不 import 整个 UI）。"""
    wanted = names or {"_assign", "_clear_zone", "_set_character_source"}
    tree = ast.parse(APP.read_text(encoding="utf-8"), str(APP))
    klass = next(n for n in tree.body
                 if isinstance(n, ast.ClassDef) and n.name == "KomaFlowApp")
    nodes = [n for n in klass.body
             if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert {n.name for n in nodes} == wanted, f"app.py 里缺少：{wanted}"

    class _MessageBox:
        @staticmethod
        def askyesno(*_args, **_kwargs):
            return True

    namespace = {"os": __import__("os"), "messagebox": _MessageBox,
                 "Path": Path}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP), "exec"),
         namespace)
    return namespace


def _bind(app: _FakeApp, ns: dict, names: set[str]) -> None:
    for name in names:
        setattr(app, name, types.MethodType(ns[name], app))


class CharacterSourceExclusiveTests(unittest.TestCase):
    def setUp(self):
        self.ns = _load_methods()
        self.app = _FakeApp()
        _bind(self.app, self.ns, {"_assign", "_clear_zone",
                                  "_set_character_source"})

    def test_pmx_after_blend_clears_blend_route(self):
        self.app.vars["blend"].set(r"X:\old.blend")
        self.app._assign("model", r"X:\new.pmx")
        self.assertEqual(self.app.vars["model"].get(), r"X:\new.pmx")
        self.assertEqual(self.app.vars["blend"].get(), "",
                         "留着旧 blend → _collect_project 仍走旧工程")

    def test_whole_blend_after_pmx_clears_model(self):
        self.app.vars["model"].set(r"X:\new.pmx")
        self.app._assign("model", r"X:\old.blend")
        self.assertEqual(self.app.vars["blend"].get(), r"X:\old.blend")
        self.assertEqual(self.app.vars["model"].get(), "")

    def test_clear_role_card_clears_both(self):
        self.app.vars["model"].set(r"X:\new.pmx")
        self.app.vars["blend"].set(r"X:\old.blend")
        self.app._clear_zone("model")
        self.assertEqual(self.app.vars["model"].get(), "")
        self.assertEqual(self.app.vars["blend"].get(), "",
                         "只清 model 会留下旧 blend 路由")

    def test_stage_blend_keeps_character(self):
        """舞台是「环境」，不能顺手把角色清掉。"""
        self.app.vars["model"].set(r"X:\new.pmx")
        self.app._assign("stage", r"X:\stage.blend")
        self.assertEqual(self.app.vars["stage"].get(), r"X:\stage.blend")
        self.assertEqual(self.app.vars["model"].get(), r"X:\new.pmx")


class AcceptModelRenderSourceTests(unittest.TestCase):
    """R06 第三入口：模型渲染「继续」回填角色卡。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.pmx = self.root / "new.pmx"
        self.pmx.write_bytes(b"PMX")
        self.blend = self.root / "new.blend"
        self.blend.write_bytes(b"BLEND")
        self.ns = _load_methods({"_set_character_source", "accept_model_render"})
        self.app = _FakeApp()
        _bind(self.app, self.ns, {"_set_character_source",
                                  "accept_model_render"})

    def test_accept_pmx_clears_old_blend(self):
        self.app.vars["blend"].set(r"X:\old.blend")
        self.app.accept_model_render(str(self.pmx), outputs=[],
                                     source_pmx=str(self.pmx))
        self.assertEqual(self.app.vars["model"].get(), str(self.pmx))
        self.assertEqual(self.app.vars["blend"].get(), "",
                         "旧 blend 没清 → route 仍是 blend（reviewer T-28 P1）")
        self.assertEqual(self.app.remembered, [str(self.pmx)])
        self.assertFalse(self.app._allow_no_physics)

    def test_accept_pmx_pairs_preview_blend_as_look(self):
        self.app.accept_model_render(str(self.blend), outputs=[],
                                     source_pmx=str(self.pmx))
        self.assertEqual(self.app.vars["model"].get(), str(self.pmx))
        self.assertEqual(self.app.settings.last_look_blend, str(self.blend))
        self.assertIn("自动配对", self.app.vars["status"].get())

    def test_accept_blend_fallback_goes_to_blend_field(self):
        # 拿不到源 PMX、用户确认“仅外观”：必须进 blend，且允许无物理
        self.app.vars["model"].set(r"X:\old.pmx")
        self.app.accept_model_render(str(self.blend), outputs=[],
                                     source_pmx="")
        self.assertEqual(self.app.vars["blend"].get(), str(self.blend))
        self.assertEqual(self.app.vars["model"].get(), "")
        self.assertTrue(self.app._allow_no_physics)
        self.assertIn("仅外观", self.app.vars["status"].get())

    def test_cancel_appearance_only_keeps_old_source(self):
        # 用户选「否」：不能静默退回 route B，角色卡保持原样
        self.app.vars["model"].set(r"X:\old.pmx")

        class _No:
            @staticmethod
            def askyesno(*_a, **_k):
                return False

        self.ns["messagebox"] = _No
        self.app.accept_model_render(str(self.blend), outputs=[],
                                     source_pmx="")
        self.assertEqual(self.app.vars["model"].get(), r"X:\old.pmx")
        self.assertEqual(self.app.vars["blend"].get(), "",
                         "取消后不能把 .blend 塞进角色卡")
        self.assertFalse(getattr(self.app, "_allow_no_physics", False))
        self.assertTrue(any("已取消" in msg for _lvl, msg in self.app.logs))


class RestoreLastCharacterTests(unittest.TestCase):
    """R06 第四入口：启动恢复不能同时填 model 与 blend。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.model = self.root / "m.pmx"
        self.model.write_bytes(b"PMX")
        self.blend = self.root / "m.blend"
        self.blend.write_bytes(b"BLEND")
        self.ns = _load_methods({"_set_character_source",
                                 "_restore_last_model"})
        self.app = _FakeApp()
        _bind(self.app, self.ns, {"_set_character_source",
                                  "_restore_last_model"})

    def test_both_saved_keeps_only_whole_project(self):
        self.app.settings.last_model = str(self.model)
        self.app.settings.last_blend = str(self.blend)
        self.app._restore_last_model()
        self.assertEqual(self.app.vars["blend"].get(), str(self.blend))
        self.assertEqual(self.app.vars["model"].get(), "",
                         "旧设置两条都有值时不能都填回来（第四入口）")
        self.assertEqual(self.app.remembered, [str(self.blend)],
                         "恢复后要把设置收敛成一条")

    def test_only_model_restores_model_and_clears_blend(self):
        self.app.vars["blend"].set(r"X:\stale.blend")
        self.app.settings.last_model = str(self.model)
        self.app._restore_last_model()
        self.assertEqual(self.app.vars["model"].get(), str(self.model))
        self.assertEqual(self.app.vars["blend"].get(), "")


if __name__ == "__main__":
    unittest.main()
