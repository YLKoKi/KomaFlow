# -*- coding: utf-8 -*-
"""R18 / R19 回归：提示窗按钮文字可点、设置「恢复默认位置」不再炸。

R18：`_button` 只把点击绑在圆角矩形上；文字 item 压在矩形上方，
     用户点按钮中心（文字）时 Canvas 命中的是 text，回调不触发。
R19：`SettingsWindow._reset` 里 `from .settings import default_paths` 指向
     不存在的 `komaflow.ui.settings`，点「恢复默认位置」必抛 ModuleNotFoundError。

R18 会短暂建一个 withdrawn 的 Tk 根窗口（不弹界面），测完 destroy。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class NoticeButtonHitTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk

        from komaflow.ui import notice_base

        self.tk = tk
        self.notice_base = notice_base
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:  # 无显示环境就跳过
            self.skipTest(f"建不了 Tk 窗口：{exc}")
        # ⚠️ 不能 withdraw：Canvas 的 item 命中（"current"）需要窗口已映射，
        #     withdrawn 时 event_generate 找不到 current item，测不出真实点击。
        self.root.geometry("260x110+40+40")
        self.canvas = tk.Canvas(self.root, width=260, height=110)
        self.canvas.pack()
        self.root.update()
        self.calls: list[int] = []
        notice_base.NoticeWindow._button(
            object(), self.canvas, 24, 24, "START", self._click, primary=True)
        self.root.update()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def _click(self) -> None:
        self.calls.append(1)

    def _label_center(self) -> tuple[int, int]:
        labels = [item for item in self.canvas.find_all()
                  if self.canvas.type(item) == "text"]
        self.assertTrue(labels, "按钮文字没画出来")
        bbox = self.canvas.bbox(labels[0])
        return (bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2

    def test_click_on_text_triggers_command(self):
        x, y = self._label_center()
        self.canvas.event_generate("<Button-1>", x=x, y=y)
        self.root.update()
        self.assertEqual(self.calls, [1],
                         "点按钮文字（中心）没反应——事件还只绑在矩形上（R18）")


class ResetDefaultPathsTests(unittest.TestCase):
    def test_reset_uses_parent_package_default_paths(self):
        from komaflow.settings import default_paths
        from komaflow.ui.settings_window import SettingsWindow

        defaults = default_paths()
        fake = SimpleNamespace(vars={key: _Var() for key in defaults})
        SettingsWindow._reset(fake)          # 旧代码在这里 ModuleNotFoundError
        for key, value in defaults.items():
            self.assertEqual(fake.vars[key].get(), value, key)

    def test_source_imports_from_parent_package(self):
        text = (ROOT / "src/komaflow/ui/settings_window.py").read_text(
            encoding="utf-8")
        self.assertIn("from ..settings import default_paths", text)
        self.assertNotIn("from .settings import default_paths", text)


if __name__ == "__main__":
    unittest.main()
