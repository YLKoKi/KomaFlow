# -*- coding: utf-8 -*-
"""低匹配中止的**行为**回归（V1.1.9 引入，V1.2.0 从 _collect_report_notes 拆出）。

为什么单独一层测试：
* V1.1.9 那条测试只断言源码里出现几个字符串，不执行判断逻辑；
* 把"中止"和"警告"混在一个函数里时，PR #10 的"只警告"回归测试会被抛错打断。
所以这里直接喂报告字典，验「22% 中止 / 40% 放行 / 骨骼太少不判 / 开了 override 只告警」。

不依赖 Blender、不建 Tk 窗口。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from komaflow.core.errors import KomaFlowError
from komaflow.core.pipeline import (
    LOW_MATCH_ABORT,
    RunResult,
    _collect_report_notes,
    _enforce_low_bone_match,
)
from komaflow.core.project import Project


def _report(coverage, matched=90, total=406):
    """造一份最小可用的装配报告（字段对得上真实 bone_coverage）。"""
    return {
        "total_frames": 480,
        "scene": {"camera": "KomaFlow_Cam"},
        "bone_coverage": {
            "matched": matched,
            "vmd_unique_bones": total,
            "coverage": coverage,
            "critical_missing": [],
            "missing_sample": ["ｷｬﾗA", "ｷｬﾗB"],
            "matched_sample": ["センター", "上半身"],
        },
    }


class LowMatchAbortTests(unittest.TestCase):
    def test_22_percent_aborts(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("KOMEFLOW_ALLOW_LOW_MATCH", None)
            with self.assertRaises(KomaFlowError) as ctx:
                _enforce_low_bone_match(_report(0.2217), lambda e: None)
        self.assertIn("22%", ctx.exception.detail)
        self.assertIn("已在装配阶段中止", ctx.exception.detail)
        # 报错要告诉用户怎么改，而不是只说"不匹配"
        self.assertIn(".pmx", ctx.exception.detail)

    def test_override_env_only_warns(self):
        events: list[dict] = []
        with mock.patch.dict(os.environ, {"KOMEFLOW_ALLOW_LOW_MATCH": "1"}):
            _enforce_low_bone_match(_report(0.2217), events.append)   # 不抛
        warns = [e for e in events if e.get("level") == "warn"]
        self.assertTrue(warns, f"override 时应留一条告警，实际：{events}")
        self.assertIn("KOMEFLOW_ALLOW_LOW_MATCH", warns[0]["message"])

    def test_other_env_values_do_not_override(self):
        # 只有 "1" 才放行；"true"/"0" 都不行（避免用户写了却没生效还以为开了）
        for value in ("true", "0", ""):
            with self.subTest(value=value), mock.patch.dict(
                    os.environ, {"KOMEFLOW_ALLOW_LOW_MATCH": value}):
                with self.assertRaises(KomaFlowError):
                    _enforce_low_bone_match(_report(0.2217), lambda e: None)

    def test_threshold_boundary_passes(self):
        # 边界取"大于等于不中止"，与 _collect_report_notes 的 < 0.6 警告线分开
        with mock.patch.dict(os.environ):
            os.environ.pop("KOMEFLOW_ALLOW_LOW_MATCH", None)
            _enforce_low_bone_match(_report(LOW_MATCH_ABORT), lambda e: None)
            _enforce_low_bone_match(_report(0.40), lambda e: None)

    def test_missing_coverage_does_not_abort(self):
        # 老报告 / route=blend 可能没有 bone_coverage：不能因此拦住渲染
        report = {"total_frames": 480, "scene": {}}
        with mock.patch.dict(os.environ):
            os.environ.pop("KOMEFLOW_ALLOW_LOW_MATCH", None)
            _enforce_low_bone_match(report, lambda e: None)
            report["bone_coverage"] = {"coverage": None}
            _enforce_low_bone_match(report, lambda e: None)

    def test_too_few_bones_is_not_aborted(self):
        # 单点动作 / 纯表情 / 纯镜头：匹配率没有统计意义，按 22% 误杀过分
        with mock.patch.dict(os.environ):
            os.environ.pop("KOMEFLOW_ALLOW_LOW_MATCH", None)
            _enforce_low_bone_match(_report(0.0, matched=0, total=3), lambda e: None)


class CollectNotesStaysWarnOnlyTests(unittest.TestCase):
    def test_collect_notes_22_percent_does_not_raise(self):
        """PR #10 回归：低匹配警告是"告知"，不能把调用方打断。"""
        project = Project()
        result = RunResult()
        events: list[dict] = []
        with mock.patch.dict(os.environ):
            os.environ.pop("KOMEFLOW_ALLOW_LOW_MATCH", None)
            _collect_report_notes(project, _report(0.2217), result, events.append)
        joined = "\n".join(result.warnings)
        self.assertIn("只对上了 22%", joined)
        self.assertIn("ｷｬﾗA", joined)
        self.assertFalse(any("中止" in w for w in result.warnings))


if __name__ == "__main__":
    unittest.main()
