# -*- coding: utf-8 -*-
"""T-60 回归：手工修改后的可编辑 .blend 优先渲染。

组装后记录 work.blend 的 path/size/mtime/hash；下次渲染前：
* 内容变了 → 跳过装配、直接用该文件（`user_edited`，提示“检测到手工修改”）；
* 没变 → 走原有复用/重建逻辑；
* 改名/删除 → 按卡片重新装配；
* 手改文件无物理 → 仍按 route B 规则 error / 仅外观；
* 卡片参数也变了 → 提示“卡片改动未应用”（仍优先手改文件）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from komaflow.core import pipeline


class EditableBlendStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.blend = self.work / "work.blend"
        self.blend.write_bytes(b"BLENDER" * 100)
        pipeline._write_editable_state(self.work, self.blend)

    def test_unchanged_is_same(self):
        result = pipeline._detect_editable_change(self.work, self.blend)
        self.assertEqual(result["status"], "same")

    def test_content_change_is_edited(self):
        self.blend.write_bytes(b"BLENDER-EDITED" * 100)
        result = pipeline._detect_editable_change(self.work, self.blend)
        self.assertEqual(result["status"], "edited")

    def test_deleted_is_missing(self):
        self.blend.unlink()
        result = pipeline._detect_editable_change(self.work, self.blend)
        self.assertEqual(result["status"], "missing")

    def test_no_record_is_none(self):
        (self.work / pipeline.EDITABLE_STATE_NAME).unlink()
        result = pipeline._detect_editable_change(self.work, self.blend)
        self.assertEqual(result["status"], "none")


class UseEditedBlendTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.blend = self.work / "work.blend"
        self.blend.write_bytes(b"BLENDER" * 100)
        self.spec = {"route": "assets", "model": "a.pmx", "frame_start": 0}
        (self.work / "build_spec.json").write_text(
            json.dumps(self.spec), encoding="utf-8")
        (self.work / "build_report.json").write_text(json.dumps({
            "ok": True, "camera": "Cam", "motion_span": {"max": 100},
            "physics_bake": {"baked": True, "counts": {"rigidbodies": 239}},
        }), encoding="utf-8")
        pipeline._write_editable_state(self.work, self.blend)
        self.params = SimpleNamespace(frame_start=0, frame_end=0,
                                      preroll_frames=0,
                                      allow_no_physics=False)
        self.events: list[str] = []

    def _edit(self):
        self.blend.write_bytes(b"BLENDER-EDITED" * 100)

    def _run(self, spec=None, durable=None):
        return pipeline._use_edited_blend(
            self.work, self.blend, spec or self.spec, self.params,
            on_event=lambda event: self.events.append(event.get("message", ""))
            if event.get("type") == "log" else None,
            durable_blend=durable)

    def test_unchanged_returns_none(self):
        self.assertIsNone(self._run())

    def test_edited_returns_user_edited_report(self):
        self._edit()
        report = self._run()
        self.assertIsNotNone(report)
        self.assertTrue(report["user_edited"])
        self.assertEqual(report["build_skipped"], "user-edited")
        self.assertEqual(report["editable_blend"], str(self.blend))
        self.assertTrue(any("检测到手工修改" in m for m in self.events))

    def test_frame_or_resolution_change_does_not_warn_card_change(self):
        self._edit()
        changed = dict(self.spec, frame_start=100, frame_end=200,
                       width=1920, height=1080)
        report = self._run(changed)
        self.assertIsNotNone(report)
        self.assertFalse(any("卡片改动未应用" in m for m in self.events),
                         "只改渲染区间/分辨率不算卡片变化")

    def test_use_does_not_refresh_baseline_and_keeps_working_next_rounds(self):
        self._edit()
        state_file = self.work / pipeline.EDITABLE_STATE_NAME
        before = state_file.read_bytes()
        first = self._run()
        self.assertIsNotNone(first)
        self.assertEqual(state_file.read_bytes(), before,
                         "用编辑版渲染后不能刷新基线，否则下一轮会被当基线")
        self.assertEqual(pipeline._detect_editable_change(
            self.work, self.blend)["status"], "edited")
        second = self._run()
        self.assertIsNotNone(second)
        self.assertTrue(second["user_edited"])

    def test_edited_durable_in_output_dir_is_detected_and_used(self):
        durable = self.work / "output" / "demo.blend"
        durable.parent.mkdir()
        durable.write_bytes(b"BLENDER" * 100)
        pipeline._write_editable_state(self.work, self.blend, durable)
        durable.write_bytes(b"DURABLE-EDITED" * 100)
        change = pipeline._detect_editable_change(self.work, self.blend, durable)
        self.assertEqual(change["status"], "edited")
        self.assertEqual(change["which"], "durable")
        report = self._run(durable=durable)
        self.assertEqual(report["work_blend"], str(durable))
        self.assertTrue(report["user_edited"])
        self._edit()
        changed = dict(self.spec, model="b.pmx")
        report = self._run(changed)
        self.assertIsNotNone(report)
        self.assertTrue(any("卡片改动未应用" in m for m in self.events))

    def _write_no_physics_report(self):
        (self.work / "build_report.json").write_text(json.dumps({
            "ok": True, "camera": "Cam", "motion_span": {"max": 100},
            "physics_bake": {"skipped": "无物理", "reason": "no-rigid-bodies",
                             "counts": {"rigidbodies": 0}},
        }), encoding="utf-8")
        self._edit()

    def test_no_physics_raises_by_default(self):
        self._write_no_physics_report()
        with self.assertRaises(pipeline.KomaFlowError):
            self._run()

    def test_no_physics_can_continue_as_appearance_only(self):
        self._write_no_physics_report()
        self.params.allow_no_physics = True
        report = self._run()
        self.assertIsNotNone(report)
        self.assertTrue(any("仅外观" in w for w in report["warnings"]))


class RenderFingerprintTests(unittest.TestCase):
    def test_editing_work_blend_invalidates_old_segments(self):
        from komaflow.core.project import Project

        with tempfile.TemporaryDirectory() as tmp:
            blend = Path(tmp) / "work.blend"
            blend.write_bytes(b"A" * 1024)
            project = Project(name="x")
            report = {"frame_start": 0, "frame_end": 0,
                      "work_blend": str(blend)}
            first = pipeline.render_fingerprint(project, report)
            blend.write_bytes(b"B" * 1024)
            second = pipeline.render_fingerprint(project, report)
        self.assertNotEqual(first, second,
                            "手改 work.blend 后旧段必须作废，不能续用旧视频")


if __name__ == "__main__":
    unittest.main()
