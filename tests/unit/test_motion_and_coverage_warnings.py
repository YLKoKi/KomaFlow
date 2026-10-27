# -*- coding: utf-8 -*-
"""两处非阻断警告的回归：

* route=blend 且没放动作 —— 必须明说"角色不会动"（用户工单里最常见的误用）；
* 覆盖率过低警告必须带上**骨骼名样例** —— 没有样例，用户只能对着一句
  "不匹配"瞎猜（样例是乱码 = 编码问题；样例正常但模型侧没有 = 改名模型）。
"""

from __future__ import annotations

import unittest

from komaflow.core.pipeline import RunResult, _collect_report_notes
from komaflow.core.project import Project


class BlendRouteMotionWarningTests(unittest.TestCase):
    def test_blend_route_without_motion_warns(self):
        project = Project()
        project.inputs.route = "blend"
        project.inputs.blend = r"X:\scene.blend"
        warnings = project.scope_warnings()
        self.assertTrue(any("没有放置动作文件" in w for w in warnings),
                        f"应提示动作缺失，实际：{warnings}")

    def test_blend_route_with_motion_does_not_warn(self):
        project = Project()
        project.inputs.route = "blend"
        project.inputs.blend = r"X:\scene.blend"
        project.inputs.motions = [r"X:\dance.vmd"]
        warnings = project.scope_warnings()
        self.assertFalse(any("没有放置动作文件" in w for w in warnings))

    def test_assets_route_unaffected(self):
        project = Project()          # route=assets、没放模型：由 validate 负责拦截
        warnings = project.scope_warnings()
        self.assertFalse(any("没有放置动作文件" in w for w in warnings))


class CoverageWarningSampleTests(unittest.TestCase):
    def test_low_coverage_warning_includes_bone_name_samples(self):
        project = Project()
        result = RunResult()
        events: list[dict] = []
        report = {
            "total_frames": 480,
            "scene": {"camera": "KomaFlow_Cam"},
            "bone_coverage": {
                "matched": 90,
                "vmd_unique_bones": 406,
                "coverage": 0.2217,
                "critical_missing": [],
                "missing_sample": ["ｷｬﾗA", "ｷｬﾗB"],
                "matched_sample": ["センター", "上半身"],
            },
        }
        _collect_report_notes(project, report, result, events.append)

        joined = "\n".join(result.warnings)
        self.assertIn("只对上了 22%", joined)
        self.assertIn("动作里有、模型上找不到的骨骼", joined)
        self.assertIn("ｷｬﾗA", joined)
        self.assertIn("能对上的骨骼", joined)
        self.assertIn("センター", joined)
        # 事件流里也要有同一条（界面只看事件）
        self.assertTrue(any("样例" in str(e.get("message", ""))
                            for e in events if e.get("level") == "warn"))

    def test_high_coverage_does_not_warn(self):
        project = Project()
        result = RunResult()
        report = {
            "total_frames": 380,
            "scene": {"camera": "KomaFlow_Cam"},
            "bone_coverage": {"matched": 57, "vmd_unique_bones": 57,
                              "coverage": 1.0, "critical_missing": []},
        }
        _collect_report_notes(project, report, result, lambda e: None)
        self.assertFalse(any("只对上了" in w for w in result.warnings))


if __name__ == "__main__":
    unittest.main()
