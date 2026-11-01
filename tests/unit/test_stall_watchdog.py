# -*- coding: utf-8 -*-
"""R05 回归：长单帧不能被 180 秒心跳误杀，真卡死仍要杀。

审查报告（2026-10-30）R05：只要进度文件出现过，执行器固定 180 秒没更新就杀
Blender；而 ``render.py`` 只在**每帧边界**写进度。2K + 复杂场景的单帧
合法地跑 3 分钟以上时会被误判"无响应"并重试。

修复：``run_blender_script(..., stall_seconds=...)``；渲染 / 单帧预览 /
观感迁移传 ``RENDER_STALL_SECONDS``（900 秒）。
这里用虚拟时钟 + 假进程验证两种阈值的行为，不启动 Blender。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from komaflow.adapters import executor


class _Clock:
    def __init__(self, start: float = 1000.0, step: float = 30.0):
        self.value = start
        self.step = step

    def time(self) -> float:
        self.value += self.step
        return self.value


class _FakeProcess:
    def __init__(self, exit_after_polls: int, progress: Path | None = None):
        self.pid = 424242
        self.returncode = None
        self.polls = 0
        self.exit_after_polls = exit_after_polls
        self.stopped = False
        self.progress = progress
        self._wrote_progress = False

    def poll(self):
        self.polls += 1
        # 第一次轮询时写一次进度（之后不再更新 = 真实的"单帧渲染中、没有心跳"）
        if self.progress is not None and not self._wrote_progress:
            self.progress.write_text('{"stage": "rendering", "frames_done": 0}',
                                     encoding="utf-8")
            self._wrote_progress = True
        if self.polls > self.exit_after_polls:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.stopped = True

    def kill(self):
        self.stopped = True

    def wait(self, timeout=None):
        return 0


class StallThresholdTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.script = self.root / "noop.kfscript"
        self.script.write_text("pass\n", encoding="utf-8")
        self.progress = self.root / "progress.json"
        self.progress.write_text('{"stage": "rendering", "frames_done": 0}',
                                 encoding="utf-8")

    def _run(self, *, exit_after_polls: int, stall_seconds=None):
        clock = _Clock()
        proc = _FakeProcess(exit_after_polls, progress=self.progress)

        def fake_terminate(p):
            p.stopped = True

        with mock.patch.object(executor.subprocess, "Popen",
                               return_value=proc), \
                mock.patch.object(executor, "_terminate",
                                  side_effect=fake_terminate), \
                mock.patch.object(executor.time, "time", side_effect=clock.time), \
                mock.patch.object(executor.time, "sleep", return_value=None):
            return executor.run_blender_script(
                Path(__file__), self.script, {}, self.root / "spec.json",
                progress_path=self.progress,
                timeout=None,
                poll_interval=0.0,
                stall_seconds=stall_seconds,
            )

    def test_render_threshold_allows_long_single_frame(self):
        # 10 次轮询 × 30 秒 = 约 300 秒：旧的 180 秒会杀，渲染的 900 秒不该杀
        outcome = self._run(exit_after_polls=10, stall_seconds=900.0)
        self.assertFalse(outcome.stalled, outcome.stall_reason)
        self.assertEqual(outcome.returncode, 0)

    def test_default_threshold_still_kills_stuck_process(self):
        outcome = self._run(exit_after_polls=10)
        self.assertTrue(outcome.stalled, "真卡死必须仍能判出来")
        self.assertIn("180", outcome.stall_reason)

    def test_custom_threshold_kills_eventually(self):
        # 900 秒阈值下，40 次轮询 × 30 = 1200 秒 → 必须判定卡死
        outcome = self._run(exit_after_polls=100, stall_seconds=900.0)
        self.assertTrue(outcome.stalled)
        self.assertIn("900", outcome.stall_reason)


if __name__ == "__main__":
    unittest.main()
