# -*- coding: utf-8 -*-
"""R04 回归：mmd_tools 自动安装要把 **SCRIPT 本体**交给 Blender。

审查报告（2026-10-30）R04：适配器原来把 ``install_mmd_tools.py`` 直接当
Blender 的 ``--python`` 脚本跑；而那是**系统 Python 的包装器**（``main()`` 里
再起 Blender 子进程）。在 Blender 里 ``sys.argv[1]`` 是 ``--background``，
包装器会去启动一个叫 ``--background`` 的程序 —— 用户点"自动安装"必失败。

现在：从包装器里静态取出 ``SCRIPT`` 常量 → 写临时 .py → 交给 Blender，
再解析 ``RESULT {...}``。全 mock，不启动 Blender。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from komaflow.adapters.blender5 import (Blender5Adapter,
                                        _extract_blender_install_script)

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "src/komaflow/adapters/scripts/install_mmd_tools.py"


class ExtractScriptTests(unittest.TestCase):
    def test_extracts_blender_side_script(self):
        payload = _extract_blender_install_script(WRAPPER)
        self.assertIsInstance(payload, str)
        self.assertIn("import bpy", payload)
        self.assertIn("RESULT ", payload)
        # 取出来的是脚本本体，不是「起子进程」的包装器
        self.assertNotIn("subprocess.run", payload)


class InstallEntryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.exe = self.root / "blender.exe"
        self.exe.write_bytes(b"MZ")
        self.adapter = Blender5Adapter()

    def test_passes_extracted_script_not_wrapper(self):
        captured: dict = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            python_arg = command[command.index("--python") + 1]
            captured["payload"] = Path(python_arg).read_text(encoding="utf-8")
            return subprocess.CompletedProcess(
                command, 0,
                stdout='RESULT {"ok": true, "message": "装好了"}',
                stderr="")

        with mock.patch("komaflow.adapters.blender5.subprocess.run",
                        side_effect=fake_run):
            result = self.adapter.install_mmd_tools(self.exe, timeout=5)

        self.assertTrue(result["ok"], result)
        command = captured["command"]
        self.assertEqual(command[0], str(self.exe))
        python_arg = Path(command[command.index("--python") + 1])
        self.assertNotEqual(python_arg.name, "install_mmd_tools.py",
                            "又把系统 Python 包装器当 Blender 脚本了")
        self.assertIn("import bpy", captured["payload"])
        self.assertIn("RESULT ", captured["payload"])

    def test_failure_without_result_is_reported(self):
        with mock.patch(
                "komaflow.adapters.blender5.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["blender"], 1, stdout="boom", stderr="boom")):
            result = self.adapter.install_mmd_tools(self.exe, timeout=5)
        self.assertFalse(result["ok"])
        self.assertIn("安装没成功", result["message"])

    def test_missing_blender_is_reported(self):
        result = self.adapter.install_mmd_tools(self.root / "nope.exe", timeout=5)
        self.assertFalse(result["ok"])
        self.assertIn("找不到这个 Blender", result["message"])


if __name__ == "__main__":
    unittest.main()
