# -*- coding: utf-8 -*-
"""R06 回归：角色卡的「PMX」与「整份 .blend 工程」必须互斥。

审查报告（2026-10-30）R06：先拖入 old.blend、再换 new.pmx 时，``_assign``
只写 ``vars["model"]``，不清 ``vars["blend"]``；``_collect_project`` 见
blend 非空仍走 blend 路线 —— 卡片显示新 PMX，实际渲的是旧工程。
清空角色卡也只清 model，旧 blend 路由还在。

这里用 AST 取出真实方法 + 假对象执行（不建 Tk 窗口）。
"""

from __future__ import annotations

import ast
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


class _FakeApp:
    def __init__(self):
        self.vars = {name: _Var() for name in
                     ("model", "blend", "stage", "motion", "camera", "status")}
        self.motion_list: list[str] = []
        self.transparent = _Var("关")
        self.remembered: list[str] = []
        self.logs: list[tuple[str, str]] = []
        self.refreshed = 0
        self.asked = 0

    # -- 被 _assign / _clear_zone 用到的少量方法 --
    def _motion_paths(self):
        return [p for p in self.motion_list if p]

    def _set_motion_paths(self, paths):
        self.motion_list = list(paths)

    def _remember_model(self, path):
        self.remembered.append(path)

    def _log(self, message, level="info"):
        self.logs.append((level, message))

    def _refresh_cards(self):
        self.refreshed += 1


def _load_methods():
    """从 app.py 里取出 ``_assign`` / ``_clear_zone``（不 import 整个 UI）。"""
    tree = ast.parse(APP.read_text(encoding="utf-8"), str(APP))
    klass = next(n for n in tree.body
                 if isinstance(n, ast.ClassDef) and n.name == "KomaFlowApp")
    wanted = {"_assign", "_clear_zone"}
    nodes = [n for n in klass.body
             if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert {n.name for n in nodes} == wanted, "app.py 里找不到这两个方法"

    class _MessageBox:
        @staticmethod
        def askyesno(*_args, **_kwargs):
            return True

    namespace = {"os": __import__("os"), "messagebox": _MessageBox}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP), "exec"),
         namespace)
    return namespace


class CharacterSourceExclusiveTests(unittest.TestCase):
    def setUp(self):
        self.ns = _load_methods()
        self.app = _FakeApp()
        for name in ("_assign", "_clear_zone"):
            setattr(self.app, name, types.MethodType(self.ns[name], self.app))

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


if __name__ == "__main__":
    unittest.main()
