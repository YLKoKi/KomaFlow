"""一次渲染的编排：这是整个工具的发动机。

对应需求：K-2（分段与段级续渲）· K-5（进度）· K-6（预检）· K-10（进程调用）·
FR-3.x（渲染执行）· FR-8.1（失败自动重试 1 次）· FR-8.4（成片校验）·
FR-8.10（成败只看产物，不看退出码）。

数据流
::

    prepare → assemble → [render_segment → verify_artifact]×N → concat → verify_final

设计约束
* 核心层**不直接**导入 bpy；所有 Blender 交互经 ``adapters``
* 进度**以文件为准**，不解析 Blender 屏幕输出（K-10.3）
* 一个 Blender 进程**连续渲完一整个段**（K-10.9，绝不每帧启停）
* 失败自动重试 **1 次**，仍失败即停并明确报错（FR-8.1）
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..adapters import BASELINE, run_blender_script, select_adapter
from ..adapters.discovery import discover_blender
from ..media.ffmpeg import (concat_segments, concat_streams,
                            encode_mov_alpha, probe_video)
from .paths import safe_basename
from .errors import ErrorKind, KomaFlowError, last_meaningful_line
from .journal import JOURNAL, Journal
from .project import Project
from .segment import DEFAULT_SEGMENT_LIMIT, Segment, plan_segments
from .verify import (
    ArtifactState,
    VideoProber,
    atomic_commit,
    inspect_artifact,
    list_png_frames,
    png_frames_of,
    png_sequence_dir,
    segment_path,
    segments_dir,
    stage_frames_zero_based,
    temp_path_for,
    verify_artifact,
    verify_png_sequence,
)

#: 工作目录里记「这次渲的是什么内容」的指纹文件（见 :func:`render_fingerprint`）。
RENDER_FINGERPRINT_FILE = "render_fingerprint.txt"

#: 阶段清单，供界面展示与日志使用。顺序即执行顺序。
STAGES: tuple[tuple[str, str], ...] = (
    ("prepare", "渲前体检：Blender 版本与适配器、mmd_tools、素材、磁盘"),
    ("assemble", "导入素材、挂动作与镜头、施加预热偏移、存工作工程"),
    ("render", "按段循环渲染：跳过已完成段，每段一个长驻 Blender 进程"),
    ("concat", "各段同参数，流拷贝拼接（不重编码，K-2.10）"),
    ("verify_final", "成片总帧数 / 时长 / 可播放性校验（FR-8.4）"),
)

#: 单个段渲染的兜底超时（秒）。按 4.3 s/帧 × 1000 帧并留足余量。
SEGMENT_TIMEOUT_PER_FRAME = 60.0
SEGMENT_TIMEOUT_FLOOR = 600.0


def _emit(on_event: Callable[[dict], None] | None, event_type: str, **payload) -> None:
    if on_event is None:
        return
    on_event({"type": event_type, **payload})


def make_sink(on_event: Callable[[dict], None] | None,
              journal: Journal = JOURNAL) -> Callable[[dict], None]:
    """给事件流加一层"落盘"。

    用户口径：**日志不给用户看**。所以界面只收「步骤 / 进度 / 一句人话报错」，
    完整技术记录在这里统一写进文件。

    progress 事件刻意**不落盘** —— 一段 1000 帧会打 40 条，纯噪音。
    """
    def sink(event: dict) -> None:
        kind = event.get("type")
        if kind == "log":
            journal.write(str(event.get("message", "")),
                          "warn" if event.get("level") == "warn" else "info")
        elif kind == "stage":
            journal.write(f"阶段 {event.get('stage')} —— {event.get('message', '')}")
        elif kind == "segment":
            journal.write(f"第 {(event.get('index') or 0) + 1}/{event.get('total')} 段："
                          f"帧 {event.get('frame_start')} ~ {event.get('frame_end')}")
        elif kind == "still_done":
            journal.write(f"单帧预览：{event.get('path')}")
        elif kind == "done":
            journal.write(f"完成：{event.get('output')}　帧数 {event.get('frames')}　"
                          f"时长 {event.get('duration')} 秒")
        elif kind == "error":
            journal.error(f"[{event.get('kind')}] {event.get('message')} "
                          f"detail={event.get('detail', '')}")
        if on_event is not None:
            on_event(event)

    return sink


# --------------------------------------------------------------------------
# 计划（纯逻辑 + 磁盘探测，不碰 Blender）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentPlan:
    """单段的渲染计划。"""

    segment: Segment
    state: ArtifactState

    @property
    def should_render(self) -> bool:
        """除 VERIFIED 外一律要渲（保守：宁可重跑一段，不可交付残片）。"""
        return self.state is not ArtifactState.VERIFIED

    def describe(self) -> str:
        if self.should_render:
            reason = "缺失" if self.state is ArtifactState.MISSING else f"{self.state.value}（须重跑）"
            return f"{self.segment} —— 待渲染（{reason}）"
        return f"{self.segment} —— 已完成，跳过"


@dataclass(frozen=True)
class RunPlan:
    """整个工程的渲染计划。"""

    total_frames: int
    segments: list[SegmentPlan] = field(default_factory=list)

    @property
    def pending(self) -> list[Segment]:
        return [item.segment for item in self.segments if item.should_render]

    @property
    def completed(self) -> list[Segment]:
        return [item.segment for item in self.segments if not item.should_render]

    @property
    def is_resume(self) -> bool:
        """是否属于"续渲"（已有完成段）。"""
        return bool(self.completed) and bool(self.pending)

    def summary(self) -> str:
        if not self.segments:
            return "没有可渲染的段。"
        if not self.pending:
            return f"共 {len(self.segments)} 段，全部已完成 —— 无需重跑（段级续渲生效）。"
        if self.is_resume:
            return (
                f"共 {len(self.segments)} 段：已完成 {len(self.completed)} 段（跳过），"
                f"本次需渲 {len(self.pending)} 段。"
            )
        return f"共 {len(self.segments)} 段，本次全部需渲染。"


def _render_script_path() -> str:
    """拿到渲染脚本的路径（取不到就返回空串，指纹里会是空值）。"""
    try:
        return str(BASELINE.script("render.py"))
    except Exception: # noqa: BLE001 - 指纹算不出来也不该让渲染跑不起来
        return ""


def render_fingerprint(project: Project, report: dict) -> str:
    """这次要渲的"内容指纹" —— 素材/参数任何一项变了就不一样。

    🚨 **为什么必须有这个东西**

    用户报的现象：

    > 输出的视频依旧是同一个样子，而且瞬间完成，是不是压根没跑，
    > 单纯是把之前的视频复制了一份当成新的了

    **他说得对。** 原来段级续渲的判据只有一条：
    *"那个段文件在不在、帧数对不对"*（``SegmentPlan.should_render``）。
    装配那一步有"素材没变才复用"的检查，**分段渲染没有**。于是：

    1. 换动作/换模型 → 装配识别到变化，重新做出新的 ``work.blend``
    2. 但旧段视频还在、帧数也对得上 → 判为"已完成"→ **全部跳过**
    3. 最后 ``concat`` 把**旧段**拷成新文件名

    结果就是"一秒完事、画面跟上次一模一样"。断点续渲本来是为了
    「崩了少赔」，却变成了「换了素材也不重渲」。

    做法：把"影响画面的全部输入"哈希成一个指纹存进工作目录。
    指纹变了 → 旧段一律作废（删掉重渲）。同一份工程重开还能续渲，
    因为指纹没变。

    ⚠️ 带上素材的**大小与修改时间**：路径没变但文件被换掉（很常见：
       「我又换了个更好的版本，覆盖同名文件」）也必须能被识别出来。
    """
    params = project.params

    def _stamp(path: str) -> list:
        """文件指纹：路径 + 大小 + 修改时间。读不到就给 None（保守重渲）。"""
        if not path:
            return []
        try:
            stat = os.stat(path)
            return [str(path), stat.st_size, int(stat.st_mtime)]
        except OSError:
            return [str(path), None, None]

    payload = {
        "model": _stamp(str(project.inputs.model or "")),
        "blend": _stamp(str(project.inputs.blend or "")),
        "motions": [_stamp(str(p)) for p in project.inputs.all_motions()],
        "stage": _stamp(str(project.inputs.stage or "")),
        "camera": _stamp(str(project.inputs.camera or "")),
        # 画面参数
        "size": [params.width, params.height, params.fps],
        "quality": [params.quality, params.bitrate],
        # 帧范围（含预热）—— 改了范围产物就对不上
        "frames": [report.get("frame_start"), report.get("frame_end")],
        "preroll": params.preroll_frames,
        # 物理档位会改变姿态，必须参与
            # 透明与否决定走 PNG 序列还是 MP4
        "transparent": project.effective_transparent(),
        "codec": project.alpha_codec_name(),
        # 渲染脚本自身变了也要重渲（例如改了采样、编码参数、进度上报）
        "render_logic": _script_fingerprint(_render_script_path()),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def invalidate_stale_segments(
    work_dir: str | Path,
    fingerprint: str,
    *,
    on_event=None,
) -> dict:
    """指纹变了就把旧段产物全删掉，并记下新指纹。

    不删会怎么样：``plan_run`` 会把它们判成"已完成"直接跳过 ——
    那正是用户看到的"瞬间完成、画面没变"。

    返回 ``{"changed": bool, "removed": int}``。
    """
    work_dir = Path(work_dir)
    marker = work_dir / RENDER_FINGERPRINT_FILE
    try:
        previous = marker.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        previous = ""

    if previous == fingerprint:
        return {"changed": False, "removed": 0}

    segments = segments_dir(work_dir)
    removed = 0
    if segments.is_dir():
        for child in segments.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(fingerprint, encoding="utf-8")
    except OSError:
        pass

    if removed:
        _emit(on_event, "log", level="warn",
              message=f"素材或参数变了：作废之前的 {removed} 份分段产物，重新渲染")
    return {"changed": True, "removed": removed}


def plan_run(
    total_frames: int,
    work_dir: str | Path,
    *,
    limit: int | None = DEFAULT_SEGMENT_LIMIT,
    ext: str = "mp4",
    prober: VideoProber | None = None,
    first_frame: int = 0,
) -> RunPlan:
    """算出这次要渲哪几段（K-2.7 段级续渲 / K-2.8 只看产物）。

    ``ext="png"`` 时按 **PNG 张数**判定（透明那条路），否则按视频帧数判定。
    两条判据的取舍完全一样：**宁可重跑一段，也不交付残片。**

    ``first_frame`` 是「只渲这一段」的起点（见 :func:`plan_segments`）。
    """
    plans: list[SegmentPlan] = []
    for segment in plan_segments(total_frames, limit, first_frame=first_frame):
        if ext == "png":
            # ⚠️ 透明路：产物是**一整个目录的帧**，不是单个文件。
            # 所以判据要拿**目录**去数张数，而不是 ``segment_path``
            # （那个返回的是给 Blender 的**文件名前缀**，
            # 当目录用会永远找不到东西）。
            # 目录与前缀都从 ``png_frames_of`` 拿 —— 它是唯一出处。
            frames = png_frames_of(work_dir, segment)
            state, _found = verify_png_sequence(
                frames.directory, segment.count, prefix=frames.prefix)
        else:
            path = segment_path(work_dir, segment, ext)
            if prober is None:
                state = inspect_artifact(path)
            else:
                state, _info = verify_artifact(path, segment.count, prober)
        plans.append(SegmentPlan(segment=segment, state=state))
    return RunPlan(total_frames=total_frames, segments=plans)


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------


@dataclass
class RunResult:
    ok: bool = False
    total_frames: int = 0
    segments_total: int = 0
    segments_rendered: int = 0
    segments_skipped: int = 0
    output: Path | None = None
    #: 透明那条路的主交付（带 alpha 的 MOV）。非透明时是 ``None``。
    alpha_output: Path | None = None
    work_blend: Path | None = None
    build_report: dict | None = None
    #: 这次渲的**内容指纹**（素材/参数的哈希）。见 :func:`render_fingerprint`。
    fingerprint: str = ""
    #: 因为指纹变了而作废的分段产物数量（0 = 沿用上次的段）。
    segments_invalidated: int = 0
    #: 观感迁移的结果（``apply_look.py`` 写出的报告；没做就是空 dict）。
    look_report: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0


@dataclass
class AssembleResult:
    """一次**只装配、不渲染**的结果（用来交付"全部组装好的 .blend"）。"""

    ok: bool = False
    #: 组装好的 Blender 工程（角色 + 动作 + 表情 + 镜头 + 舞台全在里面）
    work_blend: Path | None = None
    total_frames: int = 0
    build_report: dict | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0


def assemble_project(
    project: Project,
    work_dir: str | Path,
    *,
    blender_exe: str | Path | None = None,
    on_event: Callable[[dict], None] | None = None,
    cancel=None,
    control=None,
) -> AssembleResult:
    """**只跑到装配为止**，把「全部组装在一起」的 Blender 工程交给用户自己微调。

    为什么要单独开这个入口
    ----------------------
    渲染跑完，组装好的工程其实已经躺在工作目录里（``work.blend``）——
    可它在一堆中间产物里，用户根本找不到，而且下一次渲染会被覆盖。
    所以这里给它一个正式的出口：**复制到用户指定的地方**，
    文件名跟成片一致，打开就是完整的场景（角色 + 动作 + 表情 + 镜头 + 舞台），
    可以自己调材质、灯光、相机，再导回来接着渲。

    走的还是**同一条装配链**（``_prepare_and_assemble``），
    不是另写一套 —— 两条路装配出来的东西必须一模一样，
    否则"我微调过的工程"和"直接渲染"出来的片子会对不上。
    """
    started = time.time()
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    JOURNAL.bind(work_dir)
    on_event = make_sink(on_event, JOURNAL)
    result = AssembleResult()

    try:
        _adapter, _exe, report = _prepare_and_assemble(
            project, work_dir, blender_exe=blender_exe, on_event=on_event,
            cancel=cancel, control=control,
        )
        result.build_report = report
        result.work_blend = Path(report.get("work_blend") or (work_dir / "work.blend"))
        result.total_frames = int(report.get("total_frames") or 0)
        if not result.work_blend.is_file():
            raise KomaFlowError(ErrorKind.ASSET_CORRUPT,
                              detail=f"装配完成但工程文件不见了：{result.work_blend}")
        result.ok = True
        _emit(on_event, "log", level="info",
              message=f"装配完成：{result.work_blend}（共 {result.total_frames} 帧）")
    except KomaFlowError as exc:
        result.errors.append(exc.human())
        _emit(on_event, "error", kind=exc.kind.value, message=exc.human(),
              detail=exc.detail)
    except Exception as exc: # noqa: BLE001
        result.errors.append(f"{type(exc).__name__}: {exc}")
        # 🚨 把**完整调用栈**记进日志：原来只说"TypeError: …"，用户和我都只能猜。
        import traceback as _tb
        _emit(on_event, "log", level="error",
              message="出错的调用栈：\n" + _tb.format_exc())
        _emit(on_event, "error", kind="unexpected",
              message=f"{type(exc).__name__}: {exc}")

    result.elapsed_seconds = time.time() - started
    return result


def resolve_blender_exe(project: Project) -> Path:
    """确定用哪个 Blender —— **绝不代选**（设计取舍）。

    优先级：工程档里记的 → 环境变量/常见目录探测（唯一命中才用）。
    """
    if project.blender_exe:
        candidate = Path(project.blender_exe)
        if candidate.is_file():
            return candidate
        raise KomaFlowError(
            ErrorKind.BLENDER_MISSING, detail=f"工程档里记的 Blender 不存在：{candidate}"
        )

    candidates = discover_blender()
    if not candidates:
        raise KomaFlowError(ErrorKind.BLENDER_MISSING)
    if len(candidates) == 1:
        return candidates[0].exe
    raise KomaFlowError(
        ErrorKind.BLENDER_MISSING,
        detail=("检测到多个 Blender，请自己选一个（工具不代选）："
                + "；".join(str(c.exe) for c in candidates)),
    )


def run_project(
    project: Project,
    work_dir: str | Path,
    *,
    blender_exe: str | Path | None = None,
    on_event: Callable[[dict], None] | None = None,
    cancel=None,
    control=None,
    ffmpeg: str | Path | None = None,
) -> RunResult:
    """执行一次完整渲染。这是界面与命令行共用的唯一入口。"""
    started = time.time()
    # 一律用绝对路径：Blender 是独立进程，相对路径会受它的工作目录影响，
    # 出问题时极难排查（实测踩过一次）。
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    # 日志落盘（界面不显示日志，只显示步骤与一句人话报错）
    JOURNAL.bind(work_dir)
    on_event = make_sink(on_event, JOURNAL)
    result = RunResult()

    try:
        # ① prepare 与 ② assemble 都在 _prepare_and_assemble 里（阶段事件也在那里发）
        adapter, exe, report = _prepare_and_assemble(
            project, work_dir, blender_exe=blender_exe, on_event=on_event,
            cancel=cancel, control=control,
        )
        result.build_report = report
        total_frames = int(report.get("total_frames") or 0)
        result.total_frames = total_frames
        result.work_blend = Path(report.get("work_blend") or (work_dir / "work.blend"))
        _collect_report_notes(project, report, result, on_event)
        # 骨骼对得上的部分太少时**在装配阶段就停**（V1.1.9 起；V1.2.0 从
        # _collect_report_notes 里搬出来，警告归警告、中止归中止）。
        _enforce_low_bone_match(report, on_event)
        if total_frames <= 0:
            raise KomaFlowError(
                ErrorKind.ASSET_CORRUPT, detail="装配后没算出任何帧，检查动作文件")
        params = project.params

        # ── 输出形态：透明走 PNG 序列（RGBA），否则就是普通的 MP4 ──
        transparent = project.effective_transparent()
        alpha_codec = project.alpha_codec_name()
        # 🚨 「PNG 图片序列」≠「透明」：
        #    透明说的是**要不要 alpha**，PNG 说的是**要不要视频文件**。
        #    以前只用一个 transparent 判断，于是"有舞台 + 选 PNG"就被当成
        #    MP4 去校验 → 回头找 ffmpeg 读产物 → 报"产物读不出来"。
        frames_only = transparent or alpha_codec == "png"
        if frames_only and not transparent:
            _emit(on_event, "log", level="info",
                  message="输出格式：PNG 图片序列（只出图，不编码视频，不需要 FFmpeg）")
        if transparent:
            # ⚠️ 只说"出一条"，不再提"另外垫一条 MP4" —— 那条已去掉，
            # 用户选什么就出什么（用户要求，见 _encode_png_tracks 的说明）。
            _emit(on_event, "log", level="info",
                  message=("透明背景：分段产物改为 PNG 序列（RGBA），"
                           f"成片是「{alpha_codec}」的 MOV"))
        ext = "png" if frames_only else "mp4"

        # ── ②a 物理执行情况（写进日志，别让用户只能猜）──
        #
        # 链路已统一为 **mmd_tools 自带物理**：导入带 PHYSICS → build_rig → 烘焙。
        # 这三步都是 Blender 子进程里真做的事，这里把它们的结果翻到界面日志上，
        # 用户看到的就是证据（而不是"应该开了吧"）。
        bake = (report or {}).get("physics_bake") or {}
        # build_rig 现在在烘焙前做（动作装完之后），结果跟着烘焙报告上来
        rig = (report or {}).get("build_rig") or (bake.get("build_rig") or {})
        if rig.get("rigid_tracks"):
            _emit(on_event, "log", level="info",
                  message=(f"物理：build_rig 建了 {rig.get('rigid_tracks')} 条骨骼跟随约束"
                           f"（共 {rig.get('constraints')} 条约束，is_built="
                           f"{rig.get('is_built')}）"))
        elif rig.get("skipped"):
            _emit(on_event, "log", level="info",
                  message=f"物理：跳过 build_rig（{rig.get('skipped')}）")
        if bake.get("baked"):
            span = bake.get("cache") or []
            _emit(on_event, "log", level="info",
                  message=(f"物理：已烘焙 {span[0]}~{span[1]} 帧"
                           f"（{bake.get('seconds')} 秒）"))
        elif bake.get("skipped"):
            _emit(on_event, "log", level="warn",
                  message=f"物理：没有烘焙（{bake.get('skipped')}）")

        # 观感迁移已经挪到「装配」那一步（见 _prepare_and_assemble）：
        # 预览与整片因此共用同一份带观感的工程，不会各搬各的。
        result.look_report = report.get("look_report") or {}

        # ── ③ 分段计划（段级续渲）────────────────────────────────
        #
        # 🚨 **先按内容指纹作废旧段**，再排计划。
        # 顺序不能反：要是先排了计划，旧段会被判成"已完成"直接跳过，
        # 最后 concat 只是把上一次的段拷成新文件名 ——
        # 那就是用户报的"瞬间完成、画面还是同一个"
        # （详见 render_fingerprint 的说明）。
        result.fingerprint = render_fingerprint(project, report)
        invalidation = invalidate_stale_segments(
            work_dir, result.fingerprint, on_event=on_event)
        result.segments_invalidated = invalidation["removed"]

        def prober(path: Path):
            return probe_video(path, ffmpeg, cancel=cancel)

        # 没有 FFmpeg 就**不能拼接**：改成一次渲完整片（单段），
        # 这样 MP4 也能正常出片，只是这次没有断点续渲。
        single_pass = False
        if not frames_only and ffmpeg is None:
            single_pass = True
            _emit(on_event, "log", level="warn",
                  message=("没找到 FFmpeg：本次改为**单段一次性渲染**"
                           "（不做分段，无法断点续渲）。"
                           "想要断点续渲请安装 FFmpeg。"))
        plan = plan_run(total_frames, work_dir,
                        limit=None if single_pass else params.segment_limit,
                        ext=ext, prober=None if frames_only else prober,
                        first_frame=params.frame_start)
        result.segments_total = len(plan.segments)
        result.segments_skipped = len(plan.completed)
        for item in plan.segments:
            _emit(on_event, "log", level="info", message=item.describe())

        # ── ④ 逐段渲染 ─────────────────────────────────────────────
        _emit(on_event, "log", level="info", message="正在渲染视频…")
        _emit(on_event, "stage", stage="render", message=STAGES[2][1])
        done_before = sum(s.count for s in plan.completed)
        rendered_parts: list[Path] = []
        rendered_frames = 0

        for index, item in enumerate(plan.segments):
            segment = item.segment
            target = segment_path(work_dir, segment, ext)

            if not item.should_render:
                rendered_parts.append(target)
                rendered_frames += segment.count
                continue

            _emit(on_event, "segment", index=index, total=len(plan.segments),
                  frame_start=segment.start, frame_end=segment.end,
                  status="rendering")
            _render_one_segment(
                exe=exe, adapter=adapter, work_blend=result.work_blend, segment=segment,
                target=target, params=params, work_dir=work_dir, index=index,
                total=len(plan.segments), done_before=done_before, total_frames=total_frames,
                ffmpeg=ffmpeg, on_event=on_event, cancel=cancel, control=control,
                transparent=frames_only,
            )
            rendered_parts.append(target)
            rendered_frames += segment.count
            result.segments_rendered += 1
            done_before += segment.count

        # ── ⑤ 合并 / 编码 ──────────────────────────────────────────
        _emit(on_event, "stage", stage="concat", message=STAGES[3][1])
        output_dir = Path(project.output.dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        final_mov: Path | None = None

        if frames_only and alpha_codec == "png":
            # 只出图片序列：不调 FFmpeg，把各段的帧按顺序导出成一个文件夹
            _emit(on_event, "stage", stage="concat", message="正在整理 PNG 序列")
            final, exported = _export_png_frames(
                segments=[item.segment for item in plan.segments],
                work_dir=work_dir, output_dir=output_dir,
                basename=safe_basename(project.output.basename), on_event=on_event,
                cancel=cancel,
            )
            result.output = final
            result.alpha_output = None
            result.ok = True
            _emit(on_event, "done", output=str(final), frames=exported,
                  duration=0.0, alpha_output="")
            # ⚠️ 必须是 ``return result``：原来写的是裸 ``return``，
            # 于是「只出 PNG 图片序列」这条路**成功后返回 None**，
            # 调用方（界面）一取 ``result.ok`` 就 AttributeError ——
            # 片其实已经渲好了，却报成失败。P0 冒烟时抓到。
            return result

        if frames_only:
            # 要编码的段清单：渲过的 + 判为已完成跳过的，都在 ``plan.segments`` 里。
            # ⚠️ 给的是**段**（不是目录）—— 目录与前缀由 png_frames_of 算，
            # 上一版就是因为它俩各算一套才对不上（见 _encode_png_tracks）。
            encoding_segments = [item.segment for item in plan.segments]
            # 没渲过的段走的是"已有序列、跳过"这条路，这里补一道存在性检查：
            # 序列要是空的，说明续渲判据被骗了，早点报错好过编出个空片子
            for order, segment in enumerate(encoding_segments):
                frames = png_frames_of(work_dir, segment)
                if not list_png_frames(frames.directory, frames.prefix):
                    raise KomaFlowError(
                        ErrorKind.ASSET_CORRUPT,
                        detail=(f"第 {order + 1} 段没有 PNG 帧可编："
                                f"{frames.describe()}"),
                    )
            final_mov, final = _encode_png_tracks(
                segments=encoding_segments, work_dir=work_dir,
                output_dir=output_dir,
                basename=safe_basename(project.output.basename), params=params,
                codec=alpha_codec,
                ffmpeg=ffmpeg, on_event=on_event, cancel=cancel,
            )
        else:
            final = unique_output_path(
                output_dir / f"{safe_basename(project.output.basename)}.mp4")
            # ⚠️ 这一段也要报进度：用户看到的是"帧渲完了却没结束"，
            # 其实在拼接/封 MP4。不报进度就又是一次"卡死"投诉。
            _emit(on_event, "stage", stage="encode", message="正在拼接成片")
            _emit(on_event, "progress", segment=0, segment_total=1,
                  frame_done=0, frame_total=0, percent=99.0, encoding=True,
                  message="正在把各段拼成一条")
            if single_pass and len(rendered_parts) == 1:
                # 单段：段文件就是成片，直接改名，不需要 FFmpeg
                source = rendered_parts[0]
                if source.resolve() != final.resolve():
                    # 🚨 这里原来写的是 write_bytes(read_bytes()) ——
                    # 成片多大就把多少字节整体读进内存（2K 长片能到几个 GB，
                    # 渲染到 99% 时爆内存）。改成流式复制，内存占用与文件大小无关。
                    shutil.copy2(source, final)
                    try:
                        source.unlink()
                    except OSError:
                        pass
                _emit(on_event, "log", level="info",
                      message="单段渲染：已直接落成片（未使用 FFmpeg）")
            else:
                concat_segments(rendered_parts, final, ffmpeg=ffmpeg, cancel=cancel)

        # ── ⑥ verify_final：**数产物，不数期望值** ────────────────
        # ⚠️ 原来这里断言"成片帧数 == 工程总帧数"，那条断言在续渲场景下是错的：
        # 用户从第 800 帧开始渲时，工程总帧数仍是整片长度，但成片只有后半段。
        # 所以改成跟"本次实际产出的帧数"比。
        _emit(on_event, "stage", stage="verify_final", message=STAGES[4][1])
        if frames_only and alpha_codec == "png":
            # 上面 _export_png_frames 已按张数核对过，这里不再 probe
            return result
        try:
            info_final = probe_video(final, ffmpeg, cancel=cancel)
        except KomaFlowError:
            # 没有 ffprobe 时用 Blender 内部 ffmpeg 读（用户机器通常只有 Blender）
            from ..adapters.blender5 import probe_movie_with_blender
            payload = probe_movie_with_blender(exe, final)
            from .verify import VideoInfo
            info_final = VideoInfo(
                frame_count=int(payload["frames"]),
                duration_seconds=float(payload["duration"]),
                fps=float(payload["fps"]),
            )
        if info_final.frame_count != rendered_frames:
            raise KomaFlowError(
                ErrorKind.FRAME_FAILED,
                detail=f"成片 {info_final.frame_count} 帧，"
                       f"本次产出应为 {rendered_frames} 帧",
            )

        result.output = final
        result.alpha_output = final_mov
        result.ok = True
        _emit(on_event, "done", output=str(final), frames=info_final.frame_count,
              duration=round(info_final.duration_seconds, 2),
              alpha_output=str(final_mov) if final_mov else "")
    except KomaFlowError as exc:
        result.errors.append(exc.human())
        _emit(on_event, "error", kind=exc.kind.value, message=exc.human(), detail=exc.detail)
    except Exception as exc: # noqa: BLE001
        result.errors.append(f"{type(exc).__name__}: {exc}")
        # 🚨 把**完整调用栈**记进日志：原来只说"TypeError: …"，用户和我都只能猜。
        import traceback as _tb
        # #9② 这段原来写了两遍，每个意外异常都会连发两份一模一样的调用栈
        _emit(on_event, "log", level="error",
              message="出错的调用栈：\n" + _tb.format_exc())
        _emit(on_event, "error", kind="unexpected", message=f"{type(exc).__name__}: {exc}")

    result.elapsed_seconds = time.time() - started
    return result


def _script_fingerprint(path) -> str:
    """返回一个脚本文件的短哈希，用来判断「装配逻辑有没有变」。

    只取前 16 位十六进制：够用来区分版本，又不会让 build_spec 变得难读。
    读不到文件时返回空串 —— 旧 spec 里没有这个键，自然就不相等，
    会走重新装配（保守做法：宁可多装一次，也不拿旧产物糊弄）。
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()[:16]


#: 只影响"渲染"、不影响"物理与场景结构"的参数 —— 改这些不该触发重新装配。
_RENDER_ONLY_KEYS = frozenset({
    "frame_start", "frame_end", "width", "height", "work_blend", "report",
})


def _apply_look_migration(report: dict, exe, adapter, work_dir: Path,
                          project, on_event, cancel, control) -> dict:
    """把「渲染模型」那份工程的材质搬到装配工程上（路线 A 才做）。

    返回 ``apply_look.py`` 的报告。没配来源 / 路线 B / 失败都返回 ``{}``。
    """
    params = project.params
    look_blend = str(getattr(params, "look_from_blend", "") or "").strip()
    if not look_blend:
        return {}
    # ⚠️ 路线 B（用户自己的 .blend）**也要迁移**。
    #
    # 原来这里写着 ``or report.get("opened_blend")`` —— 也就是"打开了别人的工程就
    # 不套观感"。结果用户把三视图 .blend 拖进角色卡时，渲出来是 MMD 原始材质 +
    # AgX，看起来发灰发白（他原话："完全不像人""之前没这种色彩异常"）。
    # 观感只是替换**同名材质**、并复制色彩管理，不动物件与骨架，对路线 B 同样安全。
    look_path = Path(look_blend)
    if not look_path.is_file():
        _emit(on_event, "log", level="warn",
              message=f"观感来源不存在，按 MMD 原味渲染：{look_blend}")
        return {}
    _emit(on_event, "log", level="info",
          message=f"正在迁移观感（{look_path.name}）…")
    spec = {
        "work_blend": str(report.get("work_blend") or (work_dir / "work.blend")),
        "look_blend": str(look_path),
        "report": str(work_dir / "look_report.json"),
    }
    run_blender_script(
        exe, adapter.script("apply_look.py"), spec,
        work_dir / "look_spec.json",
        log_path=work_dir / "look.log", timeout=900,
        cancel=cancel, control=control,
    )
    try:
        look_report = json.loads(
            (work_dir / "look_report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        look_report = {}
    if look_report.get("ok"):
        _emit(on_event, "log", level="info",
              message=(f"观感迁移完成：替换 {look_report.get('replaced_count', 0)} 个材质、"
                       f"色彩变换 {look_report.get('view_settings', {}).get('view_transform')}"))
    else:
        _emit(on_event, "log", level="warn",
              message="观感迁移没成功（材质名对不上？），本次按 MMD 原味渲染")
    return look_report


def _project_usable(blend_path: Path, report: dict) -> tuple[bool, str]:
    """这份"已保存的工程"到底能不能拿来渲？

    只比素材指纹是不够的：终止/崩溃时可能留下一个**空壳工程**
    （实测 0.08 MB、没有相机、只有 1 帧），拿去渲就是"退出码 1 + 产物 missing"。
    """
    if not blend_path.is_file():
        return False, "工程文件不存在"
    size_mb = blend_path.stat().st_size / 1048576
    if size_mb < 1.0:
        return False, f"工程文件太小（{size_mb:.2f} MB），像是空壳"
    # C5：只拒"0 帧"。原来写 ``<= 1``，于是用户先用「单帧预览」渲一张（区间 0–0），
    # 存档里 total_frames=1，此后整片渲染都判这份工程不可用 —— 每次白等一遍装配+烘焙，
    # 而"单帧预览与整片共用同一份工程"正是宣传里的点。空壳工程由上面 <1 MB 兜住。
    if int(report.get("total_frames") or 0) < 1:
        return False, "工程里一帧都没有"
    if not report.get("camera"):
        return False, "工程里没有相机"
    # 物理：报告里带烘焙错误（或烘了却没烘成）的工程不能复用 ——
    # 头发裙摆会静止甚至比关物理还僵，而且怎么重渲都是旧的坏结果。
    # durable 与 work 两条复用通道都从这里过，堵一处即堵全部。
    # ⚠️ route B 且模型本来就没有刚体（verdict=no_rigid_bodies）时
    #    build.py 本来就不写 physics_bake —— 那是合法状态，要放行；
    #    缺 physics_bake 键的极简/历史档案也放行（多装配一次无意义）。
    bake = report.get("physics_bake") or {}
    if bake.get("error"):
        return False, "上次物理烘焙失败"
    route_b_verdict = (report.get("route_b_physics") or {}).get("verdict")
    if route_b_verdict != "no_rigid_bodies":
        if bake.get("baked") is False and not bake.get("skipped"):
            return False, "上次物理烘焙没有完成"
    return True, ""


def _refresh_frame_fields(report: dict, params) -> dict:
    """复用时把"这次渲哪一段"重新算进去。

    🚨 踩过：复用的是上一次的报告，里面 ``total_frames`` 是**上次区间**算出来的。
       于是用户把区间从 0~40 改成 60~100，程序照样只渲 0~40 —— 界面还显示"完成"。
       物理缓存与场景跟区间无关，所以只刷新帧相关字段，装配结果照用。
    """
    report = dict(report)
    first = max(0, int(getattr(params, "frame_start", 0) or 0))
    last = int(getattr(params, "frame_end", 0) or 0)
    if last < first:
        last = first
    # ⚠️ 界面上"渲染区间"若还是 0..0（用户没填），必须用**动作本身的长度**，
    # 否则装配报告里 total_frames=1 —— 结果只渲 1 帧（用户就是这么撞上的）。
    span = report.get("motion_span") or {}
    span_max = int(span.get("max") or 0)
    if last <= first:
        last = max(last, span_max, first)
    report["frame_start"] = first
    report["frame_end"] = last
    report["total_frames"] = max(1, last - first + 1)
    report["preroll_frames"] = int(getattr(params, "preroll_frames", 0) or 0)
    return report


#: 骨骼匹配率低于这个值就**中止渲染**（出片里人物几乎不动，没必要跑）。
LOW_MATCH_ABORT = 0.35

#: 装配（含物理烘焙）的总超时上限。正常只要十几分钟，这里是兜底。
ASSEMBLE_TIMEOUT = 3600.0

#: 决定"要不要重新装配"的关键输入 —— 换任何一个都必须重装。
_INPUT_KEYS = ("route", "model", "blend_in", "motions", "camera_motion",
               "stage", "preroll_frames", "transparent", "look_from_blend")


def _file_fingerprint(path) -> list | None:
    """(大小, 修改时间) —— 用来识别"路径没变但内容换了"。

    同名覆盖素材是最常见的一种：用户把 **同一个文件名** 的模型换成了另一版，
    路径字符串一模一样。只比路径的话，装配层会认为"没变"，直接复用旧工程，
    用户改完的模型等于没生效。
    """
    try:
        stat = Path(path).stat()
    except (OSError, TypeError, ValueError):
        return None
    return [int(stat.st_size), int(stat.st_mtime_ns)]


def _key_inputs(build_spec: dict) -> dict:
    """把关键输入挑出来（用于复用前的逐项核对）。"""
    out = {}
    stats: dict[str, list] = {}
    for key in _INPUT_KEYS:
        value = build_spec.get(key)
        if isinstance(value, list):
            value = [str(v) for v in value]
            stats[key] = [_file_fingerprint(v) for v in value]
        elif isinstance(value, str) and value:
            stats[key] = [_file_fingerprint(value)]
        out[key] = value
    # 路径之外再看一眼文件本身：同名覆盖也要重装
    out["_stat"] = stats
    return out


def _same_inputs(saved: dict, build_spec: dict) -> bool:
    """逐项比对关键输入；路径按小写、反斜杠统一后再比。"""
    current = _key_inputs(build_spec)

    def norm(value):
        if isinstance(value, list):
            return [norm(v) for v in value]
        if value is None:
            return None
        return str(value).replace("/", "\\").lower()

    for key in _INPUT_KEYS:
        if norm(saved.get(key)) != norm(current.get(key)):
            return False
    # 文件指纹：老工程档里没有这一项 → 当作"变了"，重装一次最稳
    if saved.get("_stat") != current.get("_stat"):
        return False
    return True


def _assembly_fingerprint(build_spec: dict) -> str:
    """算"装配指纹"：**只含影响物理与场景结构的项**。

    为什么要单独算：用户改「渲哪一段」或「出多大」，物理一个字都不用变；
    原来整份 ``build_spec`` 一起比，改个区间就重新 build_rig + 烘焙，
    白等几分钟（用户原话："也不用重新跑物理"）。
    """
    relevant = {k: v for k, v in build_spec.items() if k not in _RENDER_ONLY_KEYS}
    blob = json.dumps(relevant, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


def _durable_paths(project: Project) -> tuple[Path, Path]:
    """非临时工程文件的位置（出图目录里，跟成片放一起）。"""
    out_dir = Path(project.output.dir)
    name = safe_basename(project.output.basename)
    safe = _safe_filename(name)
    blend = out_dir / f"{safe}.blend"
    return blend, blend.with_suffix(".blend.json")


def _prepare_and_assemble(
    project: Project,
    work_dir: Path,
    *,
    blender_exe=None,
    on_event=None,
    cancel=None,
    control=None,
):
    """① prepare + ② assemble。

    返回 ``(adapter, exe, report)``；失败抛 :class:`KomaFlowError`。

    界面的"单帧预览"也走这里 —— 保证预览与正式渲染用的是**同一份装配结果**，
    而且素材与参数没变时直接复用已装配好的工作工程，预览才够快。
    """
    _emit(on_event, "stage", stage="prepare", message=STAGES[0][1])
    for item in project.validate():
        _emit(on_event, "log", level="warn", message=item)

    exe = Path(blender_exe) if blender_exe else resolve_blender_exe(project)
    info = BASELINE.probe(exe)
    adapter = select_adapter(info.version) # 版本不符会在此明确报错（K-8.9）
    _emit(on_event, "log", level="info",
          message=f"Blender {info.version_string} | mmd_tools: "
                  f"{info.mmd_tools_version or ('已装' if info.mmd_tools_installed else '未装')}")
    if not info.mmd_tools_installed:
        raise KomaFlowError(ErrorKind.MMD_TOOLS_MISSING)

    params = project.params

    # 🚨 FFmpeg 预检：视频输出要靠它拼接与校验。
    #    以前不查，等渲了几百帧、走到"读产物"才炸，用户白等几十分钟
    #    （用户那边真机报过：586/1000 帧才弹"找不到 ffmpeg"）。
    #    只要图片序列就不需要 FFmpeg，所以先算清输出形态再决定查不查。
    # 🚨 FFmpeg 只在两种情况下必须要：透明 MOV、多段拼接。
    #    MP4 单段输出用 Blender 内置 ffmpeg 就够了 —— 缺 FFmpeg 时降级为单段，
    #    而不是把用户拦下来（用户机器上常常只有 Blender）。
    needs_ffmpeg = project.effective_transparent() and project.alpha_codec_name() != "png"
    if needs_ffmpeg:
        try:
            from ..media.ffmpeg import find_ffmpeg
            find_ffmpeg()
        except KomaFlowError as exc:
            raise KomaFlowError(
                ErrorKind.ASSET_CORRUPT,
                detail=(exc.detail or "找不到 FFmpeg")
                + "｜透明 MOV 必须用它；想免装就把输出格式换成 MP4 或 PNG 图片序列。",
            ) from exc

    work_blend = work_dir / "work.blend"
    build_report_path = work_dir / "build_report.json"
    build_spec = {
        "route": project.inputs.route,
        "model": project.inputs.model,
        # 动作 / 表情：**可以多个**，顺序有意义（后导入的覆盖同名骨骼）。
        # 由 core.project.Inputs.all_motions() 合并成"动作在前、表情在后"的
        # 一个有序列表 —— 装配脚本只认这一个列表。
        "motions": project.inputs.all_motions(),
        "camera_motion": project.inputs.camera,
        "stage": project.inputs.stage,
        "blend_in": project.inputs.blend,
        "preroll_frames": params.preroll_frames,
        "auto_camera": params.camera_mode != "vmd" or not project.inputs.camera,
        "framing": params.framing,
        "fps": params.fps,
        "width": params.width,
        "height": params.height,
        # 「只渲这一段」的起止（含预热帧，都是工程时间轴的绝对帧号）
        "frame_start": params.frame_start,
        # 物理：恒开，走 mmd_tools 自带那一套（导入带 PHYSICS + build_rig + 烘焙）。
        # 没有开关也没有档位 —— 参数全用 mmd_tools 的默认值。
        "frame_end": params.frame_end,
        # 透明背景：只在"没放舞台、也不是自带 .blend"这条路径上才为真
        # （派生规则见 core/project.py 的 effective_transparent）
        "transparent": project.effective_transparent(),
        "work_blend": str(work_blend),
        "report": str(build_report_path),
        # 装配过程的"人话进度"（正在装载 / 开启物理 / 烘焙 / 保存）
        "progress": str(work_dir / "build_progress.json"),
        # 🚨 **装配脚本自身的指纹。**
        #
        # 用户报「物理依旧未启动」，根子就在这里：复用判据只比 build_spec
        # （素材路径与参数），**不看装配脚本本身变没变**。于是脚本加了
        # build_rig + 物理烘焙之后，旧的 work.blend 仍被当成「素材没变」直接
        # 复用 —— 那里面根本没有刚体约束，也没有烘焙缓存。
        #
        # 把脚本内容哈希进去，脚本一改就自动重装配，不需要谁记得手动清缓存。
        "logic": _script_fingerprint(adapter.script("build.py")),
        # 观感来源变了要重新迁移材质（但**不需要重新烘焙物理**，所以放在
        # 装配指纹里是安全的：它只在真改了来源时触发重建）。
        "look_from_blend": params.look_from_blend,
        "look_logic": _script_fingerprint(adapter.script("apply_look.py")),
    }

    # ── 非临时工程文件（分段渲染的落点）──
    #
    # 用户要求：「确认开始渲染，先把文件存下来（非临时），之后在文件里分段渲，
    # 不用重跑物理」。所以装配完就把它复制到**出图目录**，并在旁边放一份指纹，
    # 下次（包括只改渲染区间）直接用它，不再 build_rig / 烘焙。
    durable_blend, durable_key = _durable_paths(project)
    assembly_key = _assembly_fingerprint(build_spec)

    if durable_blend.is_file() and durable_key.is_file():
        try:
            saved = json.loads(durable_key.read_text(encoding="utf-8"))
        except ValueError:
            saved = {}
        # ⚠️ 只比哈希不够：素材换了但哈希文件是旧的（或算漏了某个键），
        # 就会拿旧工程渲 —— 用户界面上选了新舞台也不生效。
        # 所以再逐项比一遍**关键输入**。
        same_inputs = _same_inputs(saved.get("inputs") or {}, build_spec)
        usable, why_not = _project_usable(durable_blend, saved.get("report") or {})
        if (saved.get("assembly_key") == assembly_key
                and same_inputs
                and saved.get("report")
                and usable):
            _emit(on_event, "log", level="info",
                  message=f"复用已保存的工程文件（含烘焙好的物理）：{durable_blend}")
            try:
                # ⚠️ 比 (大小, 修改时间)，不是只比大小 —— 用户可能自己在 Blender 里
                # 改了那份工程再渲，只比大小会漏掉"改了但大小一样"的情况。
                needs_copy = True
                if work_blend.is_file():
                    a, b = work_blend.stat(), durable_blend.stat()
                    needs_copy = (a.st_size != b.st_size
                                  or int(a.st_mtime) != int(b.st_mtime))
                if needs_copy:
                    shutil.copy2(durable_blend, work_blend)
                    _emit(on_event, "log", level="info",
                          message="已用保存的工程文件覆盖工作副本（如果你改过它，改动会生效）")
            except OSError as exc:
                _emit(on_event, "log", level="warn",
                      message=f"工程文件复制失败，改为重新装配：{exc}")
                copy_failed = True
            else:
                return adapter, exe, _refresh_frame_fields(saved["report"], params)

    copy_failed = False   # 复制失败 ≠ 素材变了，日志要分开说

    # 到这里说明没有可复用的"已保存工程"
    if durable_blend.is_file() and not usable:
        _emit(on_event, "log", level="warn",
              message=f"已保存的工程不可用（{why_not}），重新装配")
    elif durable_blend.is_file() and not copy_failed:
        _emit(on_event, "log", level="info",
              message="素材有变化，按当前选择重新装配（不会用旧工程）")

    # 复用：素材与参数一字未变、工作工程还在，就不必再装配一遍
    spec_path = work_dir / "build_spec.json"
    if spec_path.is_file() and work_blend.is_file() and build_report_path.is_file():
        try:
            previous = json.loads(spec_path.read_text(encoding="utf-8"))
        except ValueError:
            previous = None
        if previous == build_spec:
            # #9① build_report.json 是 build.py 非原子写出来的，渲染中途被杀
            # 就会留下截断文件。这里裸 json.loads 一抛，整次渲染直接 unexpected
            # 失败 —— 对 spec 文件都做了保护，这个缓存文件同风险也该保护。
            try:
                cached = json.loads(build_report_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = None
            ok_work, why_work = _project_usable(work_blend, cached)
            if cached.get("ok") and ok_work:
                _emit(on_event, "log", level="info",
                      message="素材与参数没变，复用已有的装配结果")
                return adapter, exe, _refresh_frame_fields(cached, params)
            if cached.get("ok") and not ok_work:
                _emit(on_event, "log", level="warn",
                      message=f"上一次的装配结果不可用（{why_work}），重新装配")

    _emit(on_event, "stage", stage="assemble", message=STAGES[1][1])
    seen_progress: set[str] = set()

    def _on_build_progress(payload: dict) -> None:
        message = str(payload.get("message") or "").strip()
        if not message or message in seen_progress:
            return
        seen_progress.add(message)
        _emit(on_event, "log", level="info", message=message)

    # 🚨 装配也必须有总超时。
    #
    # 原来这里干脆没传 ``timeout``，而看门狗又只在"进度出现过一次"之后才生效 ——
    # 只要卡在导入 PMX / 打开 .blend 之前，就是**无限挂着**。
    # 1 小时是上限（正常装配最多十几分钟，含物理烘焙）。
    outcome = run_blender_script(
        exe, adapter.script("build.py"), build_spec, spec_path,
        log_path=work_dir / "build.log", cancel=cancel, control=control,
        timeout=ASSEMBLE_TIMEOUT,
        progress_path=work_dir / "build_progress.json",
        on_progress=_on_build_progress,
    )
    if not outcome.ok:
        # ⚠️ 这里的 detail 会出现在弹窗里（经 KomaFlowError.human），
        # 所以要**压成一行、只留真正的原因**。
        # 实测 mmd_tools 的 InvalidFileError 消息里自带完整 traceback，
        # 原样传上去就是"一屏英文堆栈"，用户看不懂也帮不上忙。
        raw = ""
        if outcome.report:
            raw = "；".join(outcome.report.get("errors", [])) or str(outcome.report)
        reason = last_meaningful_line(raw) or "Blender 没给出原因"
        raise KomaFlowError(ErrorKind.ASSET_CORRUPT, detail=reason)

    report = outcome.report or {}

    # 🚨 **被终止的装配不许当成成功。**
    # 实测：用户在烘焙中途点终止 → 烘焙被打断 → 却照样存出一个
    # 0.08 MB、没有相机、只有 1 帧的"工程"，还被写进复用清单，
    # 下一次渲染直接拿它去渲 → Blender 退出码 1、产物 missing。
    if cancel is not None and cancel.is_set():
        raise KomaFlowError(ErrorKind.USER_CANCELLED)

    # 🚨 **物理烘焙失败 = 这次装配失败，不许往下走。**
    #
    # 原来这里只写一行 "物理：没有烘焙（…）" 就继续，
    # 然后把这份**没有物理缓存**的工程存成"可复用工程"，
    # 下次渲染直接拿它来用 —— 用户看到的是"物理明明开了却没动"，
    # 而且怎么重渲都复现（因为复用的是坏的）。
    bake_report = report.get("physics_bake") or {}
    if bake_report.get("error"):
        raise KomaFlowError(
            ErrorKind.PHYSICS_FAILED,
            detail="物理烘焙失败，本次装配已作废：" + str(bake_report["error"]),
        )

    # ── 观感迁移（能力包材质 → 我们的工程）──
    #
    # 放在这里而不是整片渲染那里，是为了让**单帧预览与整片共用同一份工程**：
    # 谁先跑谁装配 + 迁移，另一个直接用（用户明确要求"不用二次组装"）。
    report["look_report"] = _apply_look_migration(
        report, exe, adapter, work_dir, project, on_event, cancel, control)

    # ── 落盘成"非临时"工程文件 ──
    #
    # 只存一份复制 + 一份指纹，不动用户任何东西。
    # 这一步**必须在渲染开始之前**做完 —— 万一后面某段渲到一半崩了，
    # 用户手里还有一份含完整物理缓存的工程可以继续。
    try:
        _emit(on_event, "log", level="info",
              message=f"正在保存（{durable_blend.name}）…")
        durable_blend.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work_blend, durable_blend)
        durable_key.write_text(
            json.dumps({"assembly_key": assembly_key,
                        "inputs": _key_inputs(build_spec),
                        "report": report},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
        if durable_blend.is_file():
            _emit(on_event, "log", level="warn",
                  message=f"素材或参数变了，工程文件已重新生成并覆盖：{durable_blend}")
        else:
            _emit(on_event, "log", level="info",
                  message=f"工程文件已保存：{durable_blend}"
                          f"（分段渲染直接用它，不会重跑物理；也可以自己打开改）")
    except OSError as exc:
        _emit(on_event, "log", level="warn",
              message=f"工程文件另存失败（不影响本次渲染）：{exc}")

    return adapter, exe, report


def _low_bone_match(report: dict) -> tuple[float, dict] | None:
    """取「动作与模型骨骼匹配率」；没有这项数据、或动作骨骼太少时返回 None。

    骨骼只有一两个（单点动作、纯表情、纯镜头）时，匹配率没有统计意义，
    按 0% 处理会误伤 —— 所以和警告/中止的判断共用 ``>= 20`` 这条门槛。
    """
    coverage = report.get("bone_coverage") or {}
    if coverage.get("coverage") is None:
        return None
    if (coverage.get("vmd_unique_bones") or 0) < 20:
        return None
    return float(coverage.get("coverage") or 0.0), coverage


def _enforce_low_bone_match(report: dict, on_event=None) -> None:
    """骨骼匹配率低于 :data:`LOW_MATCH_ABORT` 时，**装配阶段就中止**。

    为什么单独一层：警告（``_collect_report_notes``）是"让用户知道"，
    中止是"别再白渲"。两件事混在一个函数里，测试与调用方互相牵连
    （V1.1.9 的"只警告"回归测试就被抛错打断了）。这里只判"止不止"。

    想强行渲：设环境变量 ``KOMEFLOW_ALLOW_LOW_MATCH=1``（只告警不中止）。
    """
    low = _low_bone_match(report)
    if low is None:
        return
    rate, coverage = low
    if rate >= LOW_MATCH_ABORT:
        return

    if os.environ.get("KOMEFLOW_ALLOW_LOW_MATCH") == "1":
        _emit(on_event, "log", level="warn",
              message=(f"骨骼匹配只有 {rate:.0%}，已按 KOMEFLOW_ALLOW_LOW_MATCH=1 "
                       "继续渲染；出片里大部分部位可能不会动。"))
        return

    raise KomaFlowError(
        ErrorKind.ASSET_CORRUPT,
        detail=(f"动作和模型的骨骼只对上了 {rate:.0%}"
                f"（{coverage.get('matched')}/"
                f"{coverage.get('vmd_unique_bones')}），"
                "渲出来人物基本不会动，已在装配阶段中止。\n"
                "改法：「角色模型」卡换成这个角色原本的 .pmx"
                "（.blend 只放「舞台」卡当场景）。"),
    )


def _collect_report_notes(project: Project, report: dict, result: "RunResult", on_event) -> None:
    """把装配报告里该让用户知道的事，转成人话（K-6.3）。"""
    result.warnings.extend(project.scope_warnings())

    # 🚨 路线 B（用户自带的 .blend）里根本没有物理数据时，必须明说。
    # 这种情况物理开关开得再高也不会飘 —— 用户以为是开关坏了，
    # 实际是那份工程里连一个刚体都没有（常见于「渲染模型」存出来的三视图工程）。
    route_b = report.get("route_b_physics") or {}
    if route_b.get("verdict") == "no_rigid_bodies":
        src = str(route_b.get("source_pmx") or "")
        note = ("这份工程里没有任何物理数据（刚体 0 个）：头发、裙摆不会飘。"
                "想有物理，请把**原始 .pmx 模型**放进「角色模型」，"
                "而不是用「渲染模型」存出来的 .blend。")
        if src:
            note += f"（它的源模型是：{src}）"
        result.warnings.append(note)
        _emit(on_event, "log", level="warn", message=note)
    elif route_b.get("verdict") == "bodies_without_tracks":
        # 能力包 v2.0+物理 修好后，这里可以安全补建约束了（实测 39.42 vs 38.12 cm，无爆炸）。
        # 补建是在**工作副本**上做的，用户的 .blend 原文件不动。
        note = ("这份工程有刚体但缺骨骼约束，已在工作副本上补建并烘焙物理"
                "（不会改动你的原文件）。")
        result.warnings.append(note)
        _emit(on_event, "log", level="info", message=note)

    # 相机高度异常（掉进地里）—— 多半是镜头 VMD 的单位被缩小了 12.5 倍
    cam_h = report.get("camera_height") or {}
    if cam_h.get("ok") is False and cam_h.get("camera_lowest") is not None:
        note = (f"镜头机位偏低：相机最低 {cam_h.get('camera_lowest')} 米，"
                f"角色脚底 {cam_h.get('feet')} 米 —— 像是 VMD 单位没换算"
                f"（会渲出「摄像机在地里」的画面）")
        result.warnings.append(note)
        _emit(on_event, "log", level="warn", message=note)

    coverage = report.get("bone_coverage") or {}
    if coverage.get("critical_missing"):
        note = ("动作里有这些关键骨骼在模型上找不到，相应部位不会动："
                + "、".join(coverage["critical_missing"][:12]))
        result.warnings.append(note)
        _emit(on_event, "log", level="warn", message=note)
    if coverage.get("coverage") is not None:
        _emit(on_event, "log", level="info",
              message=f"骨骼匹配 {coverage.get('matched')}/{coverage.get('vmd_unique_bones')}"
                      f"（{coverage.get('coverage'):.0%}）；缺的多为该模型没有的裙骨/饰品骨")
        # 🚨 匹配率过低时**必须升级成警告**。
        #
        # 用户报「动作导进去出来动作消失了」—— 其中一次的真因就是这个：
        # 角色卡里放的是一份三视图 .blend（骨骼命名跟动作对不上），
        # 匹配只有 22%，出片里人物几乎不动，看着就像"动作没了"。
        # 只打一行 info 没人看得见，这里把它变成显式警告。
        rate = float(coverage.get("coverage") or 0.0)
        if rate < 0.6 and (coverage.get("vmd_unique_bones") or 0) >= 20:
            note = (f"动作和模型的骨骼只对上了 {rate:.0%}"
                    f"（{coverage.get('matched')}/{coverage.get('vmd_unique_bones')}）"
                    " —— 出片里大部分部位不会动。"
                    "多半是「角色模型」卡里放的是一份 .blend（或换过模型），"
                    "请改用这个角色原本的 .pmx。")
            # 🚨 把**骨骼名样例**一起给出来：没有样例，用户只能对着一句
            # "不匹配"瞎猜。样例是乱码 → 多为动作文件编码不是标准 Shift-JIS；
            # 样例是正常日文但模型侧没有 → 模型被改过名或用了非标准骨架。
            missing_sample = [str(x) for x in (coverage.get("missing_sample") or [])[:8]]
            matched_sample = [str(x) for x in (coverage.get("matched_sample") or [])[:8]]
            if missing_sample:
                note += "\n动作里有、模型上找不到的骨骼（样例）：、" + "、".join(missing_sample)
            if matched_sample:
                note += "\n能对上的骨骼（样例）：、" + "、".join(matched_sample)
            note += ("\n（完整清单在装配报告 bone_coverage 里；"
                     "若上面的样例显示为乱码，多半是动作文件编码问题）")
            result.warnings.append(note)
            _emit(on_event, "log", level="warn", message=note)

    textures = report.get("textures") or {}
    for item in textures.get("missing", []):
        _emit(on_event, "log", level="warn",
              message=f"贴图缺失：{item.get('image')}（{item.get('reason')}）")
    if textures.get("repaired"):
        _emit(on_event, "log", level="info",
              message=f"自动修复了 {len(textures['repaired'])} 张贴图路径（FR-9.11）")

    _emit(on_event, "log", level="info",
          message=f"工程总帧数 {report.get('total_frames')}，"
                  f"镜头：{(report.get('scene') or {}).get('camera')}")


def default_still_name(project: Project, frame: int) -> str:
    """单帧预览的**默认名字**：动作文件名去掉后缀。

    用户要求：

    > 这里的右面应该出现一个命名窗口，没动就是显示默认名称（动作名称不带后缀）

    为什么默认取**动作名**而不是工程名或固定值：
    用户渲一堆单帧图是为了挑动作，用动作名才认得出哪张是哪个动作
    （``示例动作动作.png`` 一眼就知道；``still_000370.png`` 谁也认不出）。

    没有动作文件时退回模型名，最后兜底"单帧预览"。
    """
    for path in project.inputs.all_motions():
        stem = Path(str(path)).stem.strip()
        if stem:
            return _safe_filename(stem)
    model = str(project.inputs.model or "").strip()
    if model:
        stem = Path(model).stem.strip()
        if stem:
            return _safe_filename(stem)
    return "单帧预览"


def _safe_filename(name: str) -> str:
    """把用户填的名字清成能当文件名的样子（去非法字符、去首尾空白/点）。"""
    text = str(name or "").strip()
    for bad in '<>:"/\\|?*':
        text = text.replace(bad, "_")
    text = text.strip().strip(".")
    return text or "单帧预览"


def render_still(
    project: Project,
    work_dir: str | Path,
    frame: int,
    *,
    name: str = "",
    blender_exe: str | Path | None = None,
    on_event: Callable[[dict], None] | None = None,
    cancel=None,
    control=None,
) -> Path:
    """渲一张单帧图看构图（FR-4.3 指定帧单渲）。

    本产品**没有实时视口、没有时间轴**，用户校验"角色站对位置了吗、镜头构图对吗"
    的唯一手段就是渲一张图看，所以这条路径必须够快：复用已装配的工作工程，
    只花一次 1.5 秒左右的 Blender 启动 + 一帧渲染时间。

    :param name: 存成什么名字（不带扩展名）。空 -> :func:`default_still_name`
        （动作名去后缀）。

    ⚠️ 产物落在**输出目录**（用户找得到的地方），不再只塞在 ``work/preview``
       里面。原来固定叫 ``still_<帧号>.png`` 藏在中间产物目录 ——
       名字不是用户的、位置他也找不到，用户直接问"这不是我导入的"。
    """
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    JOURNAL.bind(work_dir)
    on_event = make_sink(on_event, JOURNAL)

    adapter, exe, report = _prepare_and_assemble(
        project, work_dir, blender_exe=blender_exe, on_event=on_event,
        cancel=cancel, control=control,
    )
    # ⚠️ 装配阶段被取消时**不要再往下渲**。
    # 实际情况：用户按终止 → 装配中断 → 这里仍去起渲染进程 → 没有产物 →
    # 报「素材损坏」（谎报）。所以取消要在每个阶段边界上检查。
    if cancel is not None and cancel.is_set():
        raise KomaFlowError(ErrorKind.USER_CANCELLED)

    total_frames = int(report.get("total_frames") or 0)
    frame = max(0, min(int(frame), max(0, total_frames - 1)))
    params = project.params

    # 文件名：用户填的 > 动作名 > 兜底
    stem = _safe_filename(name) if str(name or "").strip() \
        else default_still_name(project, frame)
    try:
        output_dir = Path(project.output.dir).resolve()
    except (AttributeError, OSError):
        output_dir = work_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    # ⚠️ 走 unique_output_path：连渲几张单帧不会互相覆盖
    output = unique_output_path(output_dir / f"{stem}.png")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)

    _emit(on_event, "stage", stage="still", message=f"渲单帧预览：第 {frame} 帧")
    spec = {
        "work_blend": str(work_dir / "work.blend"),
        "out": str(output),
        "still": True,
        "still_frame": frame,
        "frame_start": frame,
        "frame_end": frame,
        "fps": params.fps,
        "width": params.width,
        "height": params.height,
        "progress": str(work_dir / "progress_still.json"),
        "progress_every": 1,
        "report": str(work_dir / "still_report.json"),
    }
    run_blender_script(
        exe, adapter.script("render.py"), spec, work_dir / "still_spec.json",
        log_path=work_dir / "still.log", timeout=900, cancel=cancel, control=control,
    )

    if not output.is_file():
        # 🚨 **先看是不是用户按了终止。**
        # 用户报「渲完模型之后单帧渲染的图片无法查看」，日志里其实是：
        # 17:58:16 已请求终止 → 17:58:17 still 阶段照样跑 → 报「素材损坏」。
        # 把"用户取消"说成"素材损坏"是谎报，会把人带去查模型文件。
        if cancel is not None and cancel.is_set():
            raise KomaFlowError(ErrorKind.USER_CANCELLED)
        raise KomaFlowError(
            ErrorKind.ASSET_CORRUPT, detail=f"单帧预览没有产出文件，请看日志：{work_dir / 'still.log'}"
        )
    _emit(on_event, "still_done", path=str(output), frame=frame)
    return output


def _render_one_segment(
    *,
    exe: Path,
    adapter,
    work_blend: Path,
    segment: Segment,
    target: Path,
    params,
    work_dir: Path,
    index: int,
    total: int,
    done_before: int,
    total_frames: int,
    ffmpeg,
    on_event,
    cancel,
    control=None,
    transparent: bool = False,
) -> None:
    """渲染一段，失败自动重试 1 次（FR-8.1），最后按产物判定成败（FR-8.10）。

    ``transparent=True`` 时段产物是 **PNG 序列（RGBA）** 而不是 MP4 ——
    Blender 内建的 FFmpeg 通路不出带 alpha 的视频，所以透明只能先落 PNG，
    再由工具侧的 FFmpeg 编成 MOV 与 MP4 两条轨。此时 ``target`` 是**序列目录**。

    两条路的**重试与判定逻辑是同一套**：渲完先看产物在不在、完不完整，
    再原子落定。区别只在"完整"怎么定义 —— 视频看帧数，序列看张数。
    """
    attempts = 2 # 首次 + 重试 1 次
    last_error = ""
    # ⚠️ 前缀必须跟**实际落盘的文件名**对得上。
    # Blender 的序列命名是 ``<filepath><帧号><扩展名>``，
    # 所以前缀取段名 + ``.png``（``seg_0000_000000-000005.png``），
    # 落盘即 ``seg_0000_000000-000005.png0000`` 这种，
    # 跟 ``png_sequence_dir``（帧所在目录）配对使用。
    prefix = segment.filename("png")

    for attempt in range(1, attempts + 1):
        progress_path = work_dir / f"progress_seg{index:04d}.json"
        progress_path.unlink(missing_ok=True)

        if transparent:
            # ⚠️ 这里必须分清两个东西：
            # * ``target`` = 交给 Blender 的**文件名前缀**（``<段名>.png``）
            # * ``sequence_dir`` = 帧**实际落在哪个目录**（``<work>/segments``）
            # 早先把 ``target`` 当目录用（``target.mkdir()`` / ``rmtree(target)``），
            # 结果建出一个叫 ``seg_..._000005.png`` 的真目录，
            # 而 Blender 写出来的帧全在它**旁边** —— 校验自然一张都找不到。
            sequence_dir = png_sequence_dir(work_dir, segment, "png")
            # 重渲之前把这个目录里**本段自己**的帧清干净（别动别的段）。
            # 不清的话上次崩剩的半串 PNG 会让"数张数"永远对不上，
            # 于是每次重试都判残片 —— 白渲两遍还是不通过。
            sequence_dir.mkdir(parents=True, exist_ok=True)
            for stale in sequence_dir.glob(segment.filename("png") + "*"):
                try:
                    stale.unlink()
                except OSError:
                    pass
            temp_target = target # 给渲染脚本当 filepath 前缀
        else:
            temp_target = temp_path_for(target)
            temp_target.parent.mkdir(parents=True, exist_ok=True)
            sequence_dir = None

        spec = {
            "work_blend": str(work_blend),
            # ⚠️ PNG 序列时 "out" 就是**帧前缀**：Blender 会补 ``帧号 + .png``。
            # 因为帧直接落在 ``segments/`` 里（见 ``png_sequence_dir``），
            # 前缀就是 ``<段名>.png``，落盘为 ``<段名>.png0000`` ——
            # 跟 ``list_png_frames`` 用的 ``prefix*`` 正好对上。
            "out": str(temp_target),
            "frame_start": segment.start,
            "frame_end": segment.end,
            "fps": params.fps,
            "width": params.width,
            "height": params.height,
            "quality": params.quality,
            "bitrate": params.bitrate,
            "png_sequence": transparent,
            "progress": str(progress_path),
            "segment": index,
            "segment_count": total,
            "report": str(work_dir / f"render_report_seg{index:04d}.json"),
        }

        def on_progress(payload, index=index, segment=segment, done_before=done_before):
            frames_done = done_before + int(payload.get("frames_done") or 0)
            # ⚠️ 优先用**最近 10 帧的滑动平均**（用户要的显示口径）：
            # 总平均（elapsed/done）会被头几帧的着色器编译开销拉高，
            # 遇到复杂帧又反应迟钝，ETA 会一直骗人。
            per_frame = payload.get("seconds_per_frame_10") \
                or payload.get("seconds_per_frame")
            _emit(
                on_event, "progress",
                segment=index, segment_total=total,
                # 这批的真实起止帧 —— 界面拿它当分母，别再猜
                frame_start=segment.start, segment_end=segment.end,
                frame_done=frames_done, frame_total=total_frames,
                percent=round(frames_done / total_frames * 100, 2) if total_frames else 0.0,
                segment_elapsed=payload.get("elapsed_seconds"),
                fps_render=(1.0 / per_frame if per_frame else None),
                seconds_per_frame=per_frame,
                eta_seconds=(payload.get("eta_seconds") or 0)
                + _remaining_after(segment, total_frames, done_before,
                                   {"seconds_per_frame": per_frame}),
            )

        timeout = max(SEGMENT_TIMEOUT_FLOOR,
                      segment.count * SEGMENT_TIMEOUT_PER_FRAME)
        outcome = run_blender_script(
            exe, adapter.script("render.py"), spec,
            work_dir / f"render_spec_seg{index:04d}.json",
            log_path=work_dir / f"render_seg{index:04d}.log",
            timeout=timeout,
            progress_path=progress_path,
            on_progress=on_progress,
            cancel=cancel,
            control=control,
        )

        if cancel is not None and cancel.is_set():
            # 用户终止：**必须告诉他已渲的帧在哪**，否则他会以为"什么都没出来"
            done_note = _describe_partial_outputs(work_dir, segments_dir(work_dir))
            raise KomaFlowError(
                ErrorKind.USER_CANCELLED,
                detail=(f"已终止。{done_note}" if done_note else "已终止。"))

        why = (f"（退出码 {outcome.returncode}"
               f"{'，超时' if outcome.timed_out else ''}"
               f"{'，无响应' if outcome.stalled else ''}"
               f"{'：' + outcome.stall_reason if getattr(outcome, 'stall_reason', '') else ''}）")

        # 成败只看产物与探测结果，不看退出码（设计取舍）
        if transparent:
            state, found = verify_png_sequence(sequence_dir, segment.count,
                                               prefix=prefix)
            detail = f"{found}/{segment.count} 张"
        else:
            state = _wait_for_artifact(temp_target)
            detail = state.value
        if state in (ArtifactState.MISSING, ArtifactState.PARTIAL):
            # Blender 侧往往已经把真因写进它自己的报告了（例如"场景里没有相机"）。
            # 不把它带上来，用户只能看到"产物 missing"，等于没说。
            reason = ""
            seg_report = work_dir / f"render_report_seg{index:04d}.json"
            try:
                payload = json.loads(seg_report.read_text(encoding="utf-8"))
                reason = str(payload.get("error") or "")
            except (OSError, ValueError):
                reason = ""
            last_error = (f"第 {index + 1} 段第 {attempt} 次：产物{state.value}"
                          f"（{detail}）{why}"
                          + (f"；Blender 说：{reason}" if reason else ""))
            _emit(on_event, "log", level="warn", message=last_error)
            continue

        if transparent:
            # PNG 序列已经是最终产物，直接落定（目录本身就在 segments/ 下）
            _emit(on_event, "log", level="info",
                  message=f"第 {index + 1} 段 PNG 序列完成：{segment.count} 张")
            return

        try:
            probe_video(temp_target, ffmpeg, cancel=cancel)
        except KomaFlowError as exc:
            last_error = f"第 {index + 1} 段第 {attempt} 次：产物读不出来（{exc.detail}）"
            _emit(on_event, "log", level="warn", message=last_error)
            continue

        atomic_commit(temp_target, target) # ：成功后原子改名
        return

    raise KomaFlowError(
        ErrorKind.RENDER_PROCESS_DIED,
        detail=last_error or "重试 1 次后仍失败",
    )


def unique_output_path(target: Path) -> Path:
    """给成片找一个**不会覆盖已有文件**的路径。

    用户报过：渲一次把上一次的作品顶掉了。原因就是成片文件名固定是
    ``<工程名>.mov``，同一个工程渲两遍自然就覆盖。

    做法：``名字.mov`` 被占了就试 ``名字 (2).mov``、``名字 (3).mov`` …
    **不改用户的工程名**，只是给文件让个位 —— 用户想要固定名字，
    自己在界面上把名字改掉就行。
    """
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for index in range(2, 1000):
        candidate = target.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return target.with_name(f"{stem}_{stamp}{suffix}")


def _export_png_frames(*, segments, work_dir: Path, output_dir: Path,
                       basename: str, on_event=None,
                       cancel=None) -> tuple[Path, int]:
    """把各段的 PNG 帧按顺序导出到 `<输出目录>/<成片名>_frames/`。

    PNG 序列模式下分段产物散在 ``segments/`` 里，用户要的是一个能直接用的
    文件夹。导出时**连续重新编号**（第 2 段不会从 0 重新数），并核对总张数。
    """
    # 🚨 先导出到临时目录，**全部成功之后**才替换正式目录。
    #
    # 原来是一进来就把 ``<名>_frames/`` 里的 PNG 全删掉再开始拷 ——
    # 只要中途任一段失败（素材损坏 / 磁盘满 / 用户终止），
    # 用户上一次的 PNG 序列已经被删光了，等于"失败还顺手清了现场"。
    target_dir = output_dir / f"{safe_basename(basename)}_frames"
    staging = target_dir.with_name(target_dir.name + ".tmp")
    if staging.is_dir():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    index = 0
    try:
        for order, segment in enumerate(segments):
            frames = png_frames_of(work_dir, segment)
            files = sorted(list_png_frames(frames.directory, frames.prefix))
            if not files:
                raise KomaFlowError(
                    ErrorKind.ASSET_CORRUPT,
                    detail=f"第 {order + 1} 段没有 PNG 帧：{frames.describe()}",
                )
            for source in files:
                # 帧图也走流式复制（一张 2K PNG 有大几 MB，积少成多）
                shutil.copy2(source,
                             staging / f"{safe_basename(basename)}_{index:04d}.png")
                index += 1
            if cancel is not None and cancel.is_set():
                raise KomaFlowError(ErrorKind.USER_CANCELLED)
            if on_event is not None:
                _emit(on_event, "progress", segment=order,
                      segment_total=len(segments), frame_done=index,
                      frame_total=index,
                      percent=99.0 if order + 1 == len(segments) else 90.0,
                      encoding=True, message=f"正在导出第 {order + 1} 段")
    except BaseException:
        # 任何一步失败（素材坏了 / 盘满 / 用户终止）都要把半成品清掉，
        # 同时**旧的那批 PNG 一个都不许动**。
        shutil.rmtree(staging, ignore_errors=True)
        raise

    if index == 0:
        shutil.rmtree(staging, ignore_errors=True)
        raise KomaFlowError(ErrorKind.ASSET_CORRUPT, detail="一张 PNG 都没导出")

    # 全部导出成功 → 用新目录整体替换旧目录（旧的那批这时才删）。
    # 该目录是**工具专属**的输出目录（成功即清空重建，防止越攒越多）；
    # 用户不要把自己的文件放在这里。
    if target_dir.is_dir():
        shutil.rmtree(target_dir, ignore_errors=True)
    staging.replace(target_dir)
    return target_dir, index


def _encode_png_tracks(
    *,
    cancel=None,
    segments: list,
    work_dir: Path,
    output_dir: Path,
    basename: str,
    params,
    codec: str,
    ffmpeg=None,
    on_event=None,
) -> tuple[Path, Path]:
    """把各段的 PNG 序列编成**用户选的那一种**成片。

    🚨 **用户选什么就出什么，不再额外追加 MP4。**
       原来在透明路径上会额外编一条"垫深褐色的 MP4"（``points_possible``），
       理由是"方便直接发给别人看"。用户明确要求：

       > 用户选择什么就输出什么，不用明明选择 MOV 还额外追加 MP4，浪费资源。

       编两条轨要**多编一遍、多占一倍磁盘**，而选 MOV 的人就是要 alpha。
       需要 MP4 的人自己选 MP4（走另一条分支）就行。

    ⚠️ 实现上先逐段编出小文件、再用拼接器合起来，而不是把成百上千张 PNG
       一股脑塞给一次 ffmpeg —— 后者在 Windows 上命令行长度会爆掉。

    ⚠️ 输出路径走 :func:`unique_output_path` —— **不覆盖上一次的成片**。
    """
    if not segments:
        raise KomaFlowError(ErrorKind.ASSET_CORRUPT, detail="没有可编码的 PNG 序列")

    target = unique_output_path(output_dir / f"{safe_basename(basename)}.mov")
    parts_dir = output_dir / f"{safe_basename(basename)}_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    staging_root = parts_dir / "_frames"

    # 🚨 **编码阶段必须有进度反馈。**
    #
    # 用户报的"渲到 300/300 之后一直不结束、然后突然提示完成"就是这里：
    # 帧渲完之后还要把 PNG 序列逐段编成 MOV、再流拷贝拼接 ——
    # 2K 的长片这一步要几分钟，而界面上进度条早就 100% 了、什么都不会动，
    # 看着就像挂了。所以这里按段报进度，让它"有动静"。
    _emit(on_event, "stage", stage="encode",
          message=f"正在编码成片（{len(segments)} 段）")

    parts: list[Path] = []
    for order, segment in enumerate(segments):
        frames = png_frames_of(work_dir, segment)
        # ⚠️ 编码之前先确认帧真的在。ffmpeg 遇到找不到的输入序列时
        # 报的是 "No such file or directory"，那时候已经很难看出是
        # "编号不匹配"还是"帧真的没渲出来" —— 在这里就断掉、说清哪一段。
        found = list_png_frames(frames.directory, frames.prefix)
        if not found:
            raise KomaFlowError(
                ErrorKind.ASSET_CORRUPT,
                detail=(f"第 {order + 1} 段找不到 PNG 帧：{frames.describe()}"
                        f"（期待形如 {frames.prefix}0000.png）"),
            )
        _emit(on_event, "progress",
              segment=order, segment_total=len(segments),
              frame_done=0, frame_total=0,
              percent=round(order / max(1, len(segments)) * 100, 2),
              encoding=True,
              message=f"编码第 {order + 1}/{len(segments)} 段"
                      f"（{len(found)} 张帧）")
        stage_dir, stage_prefix = stage_frames_zero_based(
            frames, staging_root / f"seg{order:04d}")
        _emit(on_event, "log", level="info",
              message=f"第 {order + 1} 段：{len(found)} 张帧（{frames.describe()}）"
                      f" → 暂存为从 0 起的编号")

        part = parts_dir / f"part_{order:04d}.mov"
        encode_mov_alpha(stage_dir, part, prefix=stage_prefix,
                         fps=params.fps, codec=codec, ffmpeg=ffmpeg,
                         cancel=cancel)
        parts.append(part)
        _emit(on_event, "log", level="info",
              message=f"已编码第 {order + 1}/{len(segments)} 段")

    _emit(on_event, "progress", segment=len(segments), segment_total=len(segments),
          frame_done=0, frame_total=0, percent=99.0, encoding=True,
          message="正在把各段拼成一条")
    concat_streams(parts, target, ffmpeg=ffmpeg, cancel=cancel)
    shutil.rmtree(parts_dir, ignore_errors=True)
    return target, target


def _describe_partial_outputs(work_dir: Path, segments_dir: Path | None) -> str:
    """数一下中间产物里已经落了多少帧，返回一句人话。

    终止或失败时用它 —— 用户最想知道的就两件事：
    "我这一趟白跑了吗？"（没白跑）和"东西在哪？"。
    """
    try:
        directory = segments_dir or (work_dir / "segments")
        if not directory.is_dir():
            return ""
        files = [p for p in directory.iterdir() if p.is_file()]
        if not files:
            return ""
        total_mb = sum(p.stat().st_size for p in files) / 1048576
        pngs = sum(1 for p in files if p.suffix.lower() == ".png")
        count = f"{pngs} 张 PNG" if pngs else f"{len(files)} 个文件"
        return (f"已经渲好的 {count}（{total_mb:.0f} MB）在中间产物目录：{directory}"
                "（成片要等全部渲完才会生成）")
    except OSError:
        return ""


def _remaining_after(segment: Segment, total_frames: int, done_before: int, payload: dict) -> float:
    """**本段之后**还没渲的帧 × 单帧耗时。

    ⚠️ 这里只算"后续段"，本段剩余由渲染脚本自己的 ``eta_seconds`` 提供，
       调用方把两者相加 = 全片剩余。原来写了 ``frames_left_in_segment * 0.0``
       （等于把本段剩余乘 0 丢掉），语义含糊，已改正并写清。
    """
    per_frame = payload.get("seconds_per_frame")
    if not per_frame:
        return 0.0
    frames_after = max(0, total_frames - done_before - segment.count)
    return frames_after * float(per_frame)


def _wait_for_artifact(path: Path, timeout: float = 8.0, interval: float = 0.25) -> ArtifactState:
    """等产物落盘。

    Blender 进程已经退出，文件理论上立刻可见；但 Windows 上杀毒扫描、写缓存回刷
    都可能让 `is_file()` 短暂为假。宁可多等几秒，也不要误判成"残片"然后把一段
    渲了几十分钟的成果重跑一遍。
    """
    deadline = time.time() + timeout
    state = inspect_artifact(path)
    while state is ArtifactState.MISSING and time.time() < deadline:
        time.sleep(interval)
        state = inspect_artifact(path)
    return state
