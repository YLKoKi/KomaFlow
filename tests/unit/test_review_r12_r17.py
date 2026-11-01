# -*- coding: utf-8 -*-
"""R12 / R17 回归：进度收尾与取消（mmd_render 渲染器）。

R12：结果只由每 250ms 轮询的 watcher 更新；子进程退出后立刻停 watcher，
     最后一次 done（含完整 outputs）可能落在两次轮询之间 → 丢最后一张图。
     现在退出后**同步补读**最终进度，并校验 stage=done / outputs 非空。
R17：close() 只 set cancel，render_model 没有 cancel → 关窗后 Blender 继续跑。
     现在渲染与拼图都接受 cancel，并 terminate/kill/wait。

不启动 Blender：假 Popen + 真 progress 文件。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from komaflow.adapters import mmd_render


class _FakePopen:
    """假 Blender：按轮询次数写 progress，第 3 次退出。"""

    def __init__(self, command, **kwargs):
        self.command = command
        self.env = kwargs.get("env") or {}
        self.progress = (Path(self.env["PROGRESS_JSON"])
                         if "PROGRESS_JSON" in self.env else None)
        self.polls = 0
        self.returncode = None
        self.killed = False
        self.cancel = None
        self.final_payload = None
        self.exit_code = 0
        self.set_cancel_on_poll = None

    def poll(self):
        self.polls += 1
        if self.cancel is not None and self.set_cancel_on_poll == self.polls:
            self.cancel.set()
        if self.polls == 1:
            self.progress.write_text('{"stage": "render", "outputs": []}',
                                     encoding="utf-8")
        if self.polls >= 3:
            if self.cancel is not None and self.cancel.is_set():
                return None
            if self.final_payload is not None:
                self.progress.write_text(self.final_payload, encoding="utf-8")
            self.returncode = self.exit_code
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class RenderModelCancelAndFinalReadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.pmx = self.root / "model.pmx"
        self.pmx.write_bytes(b"PMX")
        self.outdir = self.root / "out"
        self.outdir.mkdir()
        self.front = self.outdir / "front.png"

    def _run(self, proc: _FakePopen, cancel=None):
        # 假 Popen 不经过真实 kwargs，这里补上 render_model 选定的进度文件路径
        proc.progress = self.outdir / "_render_progress.json"
        proc.env = {"PROGRESS_JSON": str(proc.progress)}
        with mock.patch.object(mmd_render.subprocess, "Popen", return_value=proc):
            return mmd_render.render_model(
                self.pmx, self.outdir, "demo", shots=("front",),
                blender=sys.executable, timeout=30, cancel=cancel)

    def test_final_progress_is_read_after_process_exit(self):
        proc = _FakePopen(["fake"])
        self.front.write_bytes(b"PNG")
        proc.final_payload = (
            '{"stage": "done", "outputs": ["%s"]}' % self.front.as_posix())
        result = self._run(proc)
        self.assertEqual(result["stage"], "done")
        self.assertEqual(len(result["outputs"]), 1)
        self.assertEqual(Path(result["outputs"][0]), self.front)

    def test_incomplete_stage_is_rejected(self):
        proc = _FakePopen(["fake"])
        proc.final_payload = '{"stage": "render", "outputs": []}'
        with self.assertRaises(mmd_render.MMDError) as ctx:
            self._run(proc)
        self.assertIn("不是 done", str(ctx.exception))

    def test_cancel_kills_render_subprocess(self):
        proc = _FakePopen(["fake"])
        event = threading.Event()
        proc.cancel = event
        proc.set_cancel_on_poll = 2
        with self.assertRaises(mmd_render.MMDError) as ctx:
            self._run(proc, cancel=event)
        self.assertTrue(proc.killed, "取消后必须 terminate/kill 子进程")
        self.assertIn("已取消", str(ctx.exception))


class StitchCancelTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.images = []
        for i in range(2):
            path = self.root / f"view{i}.png"
            path.write_bytes(b"PNG")
            self.images.append(path)

    def test_pre_cancelled_stitch_does_not_start(self):
        event = threading.Event()
        event.set()
        result = mmd_render.stitch_views(
            self.images, self.root / "sheet.png",
            blender=sys.executable, cancel=event)
        self.assertFalse(result["ok"])
        self.assertIn("取消", result["message"])

    def test_stitch_success_still_works(self):
        target = self.root / "sheet.png"
        target.write_bytes(b"PNG")

        class _Done:
            returncode = 0

            def poll(self):
                return 0

            def kill(self):
                pass

            def wait(self, timeout=None):
                return 0

        with mock.patch.object(mmd_render.subprocess, "Popen", return_value=_Done()):
            result = mmd_render.stitch_views(
                self.images, target, blender=sys.executable)
        self.assertTrue(result["ok"], result)


if __name__ == "__main__":
    unittest.main()
