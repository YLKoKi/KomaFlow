# -*- coding: utf-8 -*-
"""装配脚本 ``build.py`` 的 ``_write()`` 行为回归（P0：str 当 Path 用）。

背景（2026-10-27 QA RW-20261027-03）：
V1.1.3 的 C11 原子写把 ``target = spec.get("report")`` 直接当 ``Path`` 用，
``target.with_suffix(...)`` 抛 AttributeError → 装配报告写不出来 →
主流程把装配失败误报成「素材损坏」，**任何渲染都出不了片**，V1.1.3–V1.2.0-beta 一直带着。

为什么用 AST 取函数而不是 import：
``build.py`` 是**注入 Blender 执行**的脚本（顶部 ``import bpy``、模块底部直接 ``main()``），
普通导入会立刻尝试装配。这里只从源码里取出 ``_write`` 单独执行，
既不依赖 Blender，又能真正跑一遍写文件逻辑（不是"读源码字符串"式断言）。
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "src" / "komaflow" / "adapters" / "scripts" / "build.py"


def _load_write():
    """从 build.py 源码里取出 ``_write`` 函数（不执行模块顶层）。"""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT))
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "_write")
    namespace = {
        "json": json,
        "os": os,
        "Path": Path,
        "_log": lambda *args, **kwargs: None,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SCRIPT), "exec"),
         namespace)
    return namespace["_write"]


class WriteReportTests(unittest.TestCase):
    def setUp(self):
        self.write = _load_write()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_string_report_path_writes_json(self):
        """spec 来自 JSON ⇒ report 是 str。这条就是 P0 的最小复现。"""
        target = Path(self.tmp.name) / "sub" / "build_report.json"
        self.write({"report": str(target)}, {"ok": True, "total_frames": 42})

        self.assertTrue(target.is_file(), "报告没写出来（装配会被误报成素材损坏）")
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["total_frames"], 42)
        # 原子写：不许留下 .tmp，也不许多写别的文件
        self.assertEqual(list(target.parent.glob("*.tmp")), [])
        self.assertEqual(list(target.parent.iterdir()), [target])

    def test_path_object_still_supported(self):
        target = Path(self.tmp.name) / "b.json"
        self.write({"report": target}, {"ok": True})
        self.assertTrue(target.is_file())

    def test_missing_report_key_prints_to_stdout(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.write({}, {"ok": True})
        self.assertIn('"ok": true', buf.getvalue())


if __name__ == "__main__":
    unittest.main()
