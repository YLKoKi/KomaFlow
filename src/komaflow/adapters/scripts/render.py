"""渲染一个段：一个 Blender 进程连续渲完整个段（K-10.9，绝不每帧启停）。

用法::

    blender.exe --background --factory-startup --python render.py -- <spec.json>

进度回传
--------
进度写文件、由工具侧读取，**不解析 Blender 的屏幕输出**（K-10.3）。
在 ``render_pre`` / ``render_post`` 两个钩子里**按时间**覆写（最多每 2 秒一次），
含本段 ETA（K-5.1 / K-5.2）。

⚠️ 早先是"每 N 帧写一次"（``progress_every=25``）—— 慢工程一帧好几秒，
25 帧就是好几分钟才更新一次，界面上看着像卡死（用户报过）。
改成按时间之后，无论帧快帧慢，进度都是活的。
``render_pre`` 那一路是为了"一帧要渲很久"的情况：至少能看到正在渲第几帧。

出 MP4 的顺序坑（附录 F）
------------------------
必须先 ``image_settings.media_type = "VIDEO"`` **再** ``file_format = "FFMPEG"``；
顺序反了会报 enum 不存在。

质量档
------
Blender 内建的 FFmpeg 编码器**没有数值 CRF 参数**，只有 ``constant_rate_factor``
枚举。所以 1.0 的三档映射到 HIGH / MEDIUM / LOW，"进阶"提供 ``video_bitrate``（kbps）。
需要精确数值 CRF 时走后续版本的 FFmpeg CLI 通路。
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback

import bpy

MMD_EXTENSION = "bl_ext.blender_org.mmd_tools"

#: 画质档 → Blender FFmpeg 的 constant_rate_factor（Blender 没有数值 CRF）
QUALITY_TO_CRF = {
    "preview": "LOW",
    "standard": "MEDIUM",
    "high": "HIGH",
}


def _log(message: str) -> None:
    print(f"[RENDER] {message}", flush=True)


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path) # 原子写：读到的永远是完整文件


def _same_object(a, b) -> bool:
    """按 Blender 持久指针比较（T-53 P3）；fake/旧对象退回身份比较。"""
    pa = getattr(a, "as_pointer", None)
    pb = getattr(b, "as_pointer", None)
    if callable(pa) and callable(pb):
        try:
            return pa() == pb()
        except Exception: # noqa: BLE001
            pass
    return a is b


def _force_render_camera_limits(far: float = 100000.0) -> dict:
    """渲染前强制**活动相机**（scene.camera）的远裁剪/景深（T-46）。

    现场：``work.blend`` 里活动相机 ``KomaFlow_Cam`` 的 ``clip_end=40``，
    舞台远于 40 的部分被整块裁掉。build 时已修，但渲染用的可能是旧工程，
    或镜头 VMD/驱动把值改回去 —— 所以打开工程后、真正渲染前再强制一次。

    规则：活动相机优先，再遍历所有相机兜底；``clip_end=far``、``dof=False``；
    **不动 clip_start**。
    """
    changed = []
    scene = bpy.context.scene
    active = scene.camera
    cameras = []
    if active is not None:
        cameras.append(active)
    for obj in bpy.data.objects:
        if obj.type != "CAMERA":
            continue
        if any(_same_object(obj, cam) for cam in cameras):
            continue
        cameras.append(obj)

    for cam in cameras:
        data = cam.data
        before = (data.clip_start, data.clip_end, data.dof.use_dof)
        if data.clip_end < far:
            data.clip_end = far
        if data.dof.use_dof:
            data.dof.use_dof = False
        after = (data.clip_start, data.clip_end, data.dof.use_dof)
        if after != before:
            changed.append({"camera": cam.name,
                            "from": list(before), "to": list(after)})
    active_final = None
    if active is not None:
        data = active.data
        active_final = [data.clip_start, data.clip_end, data.dof.use_dof]
    _log(f"渲染前相机裁剪：活动={getattr(active, 'name', None)} "
         f"最终={active_final}；共改 {len(changed)} 台")
    return {"active": getattr(active, "name", None),
            "active_final": active_final,
            "changed": changed, "far": far}


def main() -> int:
    argv = sys.argv
    if "--" not in argv:
        _log("缺少 spec 参数")
        return 1
    spec_path = argv[argv.index("--") + 1:][0]
    with open(spec_path, encoding="utf-8") as handle:
        spec = json.load(handle)

    progress_path = spec["progress"]
    frame_start = int(spec["frame_start"])
    frame_end = int(spec["frame_end"])
    frames_total = frame_end - frame_start + 1
    started_at = time.time()

    state = {
        "ok": False,
        "phase": "starting",
        "segment": spec.get("segment", 0),
        "segment_count": spec.get("segment_count", 1),
        "frame_start": frame_start,
        "frame_end": frame_end,
        "frames_total": frames_total,
        "frames_done": 0,
        "seconds_per_frame": None,
        "eta_seconds": None,
        "elapsed_seconds": 0.0,
        "output": spec.get("out"),
        "blender_pid": os.getpid(),
        "written_at": time.time(),
    }
    _write_json(progress_path, state)

    try:
        import addon_utils

        try:
            addon_utils.enable(MMD_EXTENSION, default_set=True, persistent=False)
        except Exception as exc: # noqa: BLE001
            _log(f"mmd_tools 启用失败（渲染通常不受影响）: {exc}")

        work_blend = spec.get("work_blend")
        if work_blend:
            # 打开工具自有的工作工程；不碰用户原文件（K-10.13）
            bpy.ops.wm.open_mainfile(filepath=work_blend)

        scene = bpy.context.scene

        # ── 只覆盖分辨率与帧率（设计取舍）──
        scene.render.resolution_x = int(spec.get("width", scene.render.resolution_x))
        scene.render.resolution_y = int(spec.get("height", scene.render.resolution_y))
        scene.render.resolution_percentage = 100
        scene.render.fps = int(spec.get("fps", 30))
        scene.render.fps_base = 1.0

        images = scene.render.image_settings

        # 相机是硬前提：没有相机渲出来是空的
        if scene.camera is None:
            state["error"] = "场景里没有相机，无法渲染"
            state["ok"] = False
            _write_json(progress_path, state)
            return 1

        # T-46：work.blend 里活动相机的 clip_end 可能被写成 40（镜头 VMD/旧版本），
        # 打开工程后、真正渲染前再强制一次 —— 单帧与整片都覆盖。
        state["camera_limits"] = _force_render_camera_limits()

        still = bool(spec.get("still"))

        if still:
            # ── 单帧预览（FR-4.3 指定帧单渲）：出 PNG，用来快速看构图 ──
            images.media_type = "IMAGE"
            images.file_format = "PNG"
            images.color_mode = "RGBA"
            scene.render.use_file_extension = False
            scene.render.filepath = spec["out"]
            frame = int(spec.get("still_frame", frame_start))
            scene.frame_set(frame)
            state.update({
                "phase": "still",
                "engine": scene.render.engine,
                "resolution": [scene.render.resolution_x, scene.render.resolution_y],
                "camera": scene.camera.name,
                "still_frame": frame,
            })
            _write_json(progress_path, state)
            bpy.ops.render.render(write_still=True)
            state.update({
                "ok": True, "phase": "done", "frames_done": 1,
                "elapsed_seconds": round(time.time() - started_at, 3),
                "written_at": time.time(),
            })
            _write_json(progress_path, state)
            _log(f"单帧预览完成：帧 {frame} -> {spec['out']}")
            return 0

        # ── 输出：要么 MP4，要么 PNG 序列（RGBA）──
        #
        # 透明背景必须走 PNG 序列：**Blender 内建的 FFmpeg 通路不出带 alpha 的视频**，
        # 所以透明只能先落一串 RGBA 的 PNG，再由工具侧的 FFmpeg 编成
        # MOV（ProRes 4444 / QTRLE）+ 垫底色的 MP4。
        if spec.get("png_sequence"):
            images.media_type = "IMAGE"
            images.file_format = "PNG"
            images.color_mode = "RGBA"
            images.compression = 15 # PNG 压缩级别，15 = 无损但不太慢
            # ⚠️ **现在的约定**（跟工具侧 ``core/verify.png_frames_of`` 一对一对齐）：
            #
            # filepath = <work>/segments/<段名>.png ← 段落名**带 .png**
            # 落盘 = <work>/segments/<段名>.png0000.png、…0001.png
            # （帧号 + .png 由 Blender 自己补，见 use_file_extension）
            #
            # 「去哪个目录找」由工具侧算（就是 ``filepath`` 的父目录），
            # 「认哪个前缀」就是 ``filepath`` 的名字本身。
            #
            # 踩过的两个坑，方向相反，别只记住一个：
            #
            # ① **前缀带 .png + 关掉自动扩展名** → Blender 把它当**文件名**用，
            # 帧落在旁边、还不带扩展名::
            #
            # segments/<段名>.png_frames ← 空目录（Blender 建的）
            # segments/<段名>.png_frames0000 ← 真图在它旁边
            #
            # 工具侧按"目录里的 *.png"去找 → 一张都找不到 → 整段判 MISSING →
            # 白渲两遍 → 报"产物 missing（0/6 张）（退出码 0）"。
            #
            # ② **编码那一步自己另拼一套**（把段落名当目录 + 按老的 ``_frames``
            # 约定切一刀）→ 输入模板指向一个不存在的目录 → ffmpeg 零产出。
            # 现在目录与前缀都从 ``png_frames_of`` 拿，见 ``core/verify.py``。
            scene.render.use_file_extension = True
            scene.render.filepath = spec["out"]
            # ⚠️ 目录必须**先存在**：Blender 写序列时不会替你建目录，
            # 目录不在就"报告成功但一个文件都没有" —— 又是一种静默失败。
            os.makedirs(os.path.dirname(spec["out"]) or ".", exist_ok=True)
        else:
            # 顺序不可颠倒：先 media_type 再 file_format（附录 F）
            images.media_type = "VIDEO"
            images.file_format = "FFMPEG"

            ffmpeg = scene.render.ffmpeg
            ffmpeg.format = "MPEG4"
            ffmpeg.codec = "H264"
            bitrate = spec.get("bitrate")
            if bitrate:
                ffmpeg.constant_rate_factor = "NONE"
                ffmpeg.video_bitrate = int(bitrate)
            else:
                ffmpeg.constant_rate_factor = QUALITY_TO_CRF.get(
                    str(spec.get("quality", "standard")), "MEDIUM"
                )
            ffmpeg.audio_codec = "NONE" # 1.0 不含音频合流

            # 段产物路径：为了拿到确定的文件名，关掉自动加扩展名，自己给全名
            scene.render.use_file_extension = False
            scene.render.filepath = spec["out"]

        scene.frame_start = frame_start
        scene.frame_end = frame_end
        scene.frame_set(frame_start)

        state["phase"] = "rendering"
        state["engine"] = scene.render.engine
        state["resolution"] = [scene.render.resolution_x, scene.render.resolution_y]
        state["camera"] = scene.camera.name if scene.camera else None
        _write_json(progress_path, state)

        # ⚠️ 进度要**按时间**刷，不能按帧数。
        # 原来是 ``done % progress_every == 0``（每 25 帧写一次）——
        # 慢工程一帧好几秒，25 帧就是**好几分钟才更新一次**，
        # 界面上看着就是"卡住了、没有进度"（用户报过）。
        # 改成"距上次写盘超过 PROGRESS_SECONDS 就写"，这样无论快慢，
        # 界面最多隔两秒就有新数字。
        PROGRESS_SECONDS = 2.0
        last_write = [0.0]

        def _flush(force: bool = False, phase: str | None = None) -> None:
            now = time.time()
            if not force and now - last_write[0] < PROGRESS_SECONDS:
                return
            last_write[0] = now
            if phase:
                state["phase"] = phase
            _write_json(progress_path, state)

        #: 最近若干帧的**单帧耗时**，用来算滑动平均。
        #:
        #: 用户的要求：界面要显示"预计多少秒一帧"，而且是 **10 帧一平均**。
        #: 为什么不能直接用总平均（``elapsed / done``）：
        #: * 头几帧带着"编译着色器 / 烘焙光照"的一次性开销，会把平均值拉得很高
        #: * 渲染中途遇到复杂帧（粒子、透明堆叠）会突然变慢，总平均反应迟钝
        #: 滑动窗口能跟着"当前真实速度"走，ETA 才不会一直骗人。
        RECENT = 10
        recent_seconds: list[float] = []
        #: 上一帧结束的时刻，用来量"这一帧花了多久"
        last_tick = [started_at]

        def on_render_pre(scene_, depsgraph=None):
            """**每帧开始渲之前**报一次。

            ``render_post`` 只在渲完之后触发 —— 一帧要渲 30 秒的话，
            那 30 秒里界面上什么都不会动，看着就像卡死。
            这里把"正在渲第 N 帧"先写进去，用户至少知道它在干活。
            """
            started = scene_.frame_current
            done = started - frame_start
            state.update({
                "frames_done": done,
                "frame_current": started,
                "elapsed_seconds": round(time.time() - started_at, 3),
            })
            _flush()
            # 记这一帧的起跑时刻（render_post 里减它得到单帧耗时）
            last_tick[0] = time.time()

        def on_render_post(scene_, depsgraph=None):
            done = scene_.frame_current - frame_start + 1
            elapsed = time.time() - started_at
            per_frame = elapsed / done if done else None

            # ── 10 帧滑动平均（用户点名要的显示口径）──
            frame_seconds = max(0.0, time.time() - last_tick[0])
            if frame_seconds > 0:
                recent_seconds.append(frame_seconds)
                if len(recent_seconds) > RECENT:
                    del recent_seconds[0]
            avg10 = (sum(recent_seconds) / len(recent_seconds)
                     if recent_seconds else None)
            remaining = max(0, frames_total - done)
            state.update({
                "frames_done": done,
                "frame_current": scene_.frame_current,
                "elapsed_seconds": round(elapsed, 3),
                "seconds_per_frame": round(per_frame, 4) if per_frame else None,
                #: **平均到最近 10 帧的单帧耗时** —— 界面就显示这个
                "seconds_per_frame_10": round(avg10, 4) if avg10 else None,
                "eta_seconds": (round(avg10 * remaining, 1)
                                if avg10 else None),
                "written_at": time.time(),
            })
            # 首帧和末帧一定要写，中间按时间节流
            _flush(force=(done == 1 or done == frames_total),
                   phase="done" if done == frames_total else None)

        bpy.app.handlers.render_pre.append(on_render_pre)
        bpy.app.handlers.render_post.append(on_render_post)
        bpy.ops.render.render(animation=True) # ← 一个进程连续渲完整段
        bpy.app.handlers.render_post.remove(on_render_post)
        bpy.app.handlers.render_pre.remove(on_render_pre)

        elapsed = time.time() - started_at
        state.update({
            "ok": True,
            "phase": "done",
            "frames_done": frames_total,
            "elapsed_seconds": round(elapsed, 3),
            "seconds_per_frame": round(elapsed / frames_total, 4) if frames_total else None,
            "eta_seconds": 0.0,
            "written_at": time.time(),
        })
        _write_json(progress_path, state)
        _log(f"完成 {frames_total} 帧，用时 {elapsed:.1f}s -> {spec['out']}")
        return 0

    except Exception as exc: # noqa: BLE001
        state.update({
            "ok": False,
            "phase": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-2000:],
            "elapsed_seconds": round(time.time() - started_at, 3),
            "written_at": time.time(),
        })
        _write_json(progress_path, state)
        _log(f"FAILED: {exc}")
        return 1


raise SystemExit(main())
