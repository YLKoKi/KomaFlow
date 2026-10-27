"""装配：把素材搭成一个可渲染的工程，并存成工具自有的 work.blend。

用法::

    blender.exe --background --factory-startup --python build.py -- <spec.json>

``spec.json`` 字段见 KomaFlow/adapters/spec.py。结果写在 ``spec["report"]``。

**核心实测结论（K-1.8，本脚本据此实现）**：

mmd_tools 4.5.7 的帧号公式是 ::

    frame = vmd_frame + scene.frame_current + frame_margin

其中 ``frame_margin`` **只在** ``scene.frame_current in {0, 1}`` 时生效。
所以 mmd_tools **默认不加任何偏移**（``min_keyframe`` 就是 0）—— 那 +30 必须我们自己施加。

施加方式选「``scene.frame_current = N``、``margin = 0``」，而不是「``frame_current = 0``、
``margin = N``」：后者会让 mmd_tools 在第 0 帧补一个**静止姿势**关键帧并带 BEZIER 插值，
预热段变成"从静止姿势缓慢过渡到第一个动作姿势"，**违反 K-1.2 的"冻住不动"**。
前者让关键帧整段落在 N 之后，第 0 ~ N-1 帧处于首个关键帧之前、由 Blender 常量外推
⇒ 真正冻住（这正是 ``前30帧检查.txt`` 里"前 30 帧位移 0.00000"的成因）。

无论哪种方式，骨骼 / 相机 / 光照 / 表情 / 显示都走同一行偏移，因此
**动作与镜头的相对关系天然不变**（K-1.3）。

另外两个实测坑：

* ``mmd_tools.import_vmd`` 的 ``execute()`` 循环的是 ``self.files``。程序化调用时
  只给 ``filepath`` 会**静默什么都不导入**却仍返回 ``FINISHED`` —— 必须同时给
  ``directory`` 与 ``files``。
* 骨骼映射必须用 ``bone_mapper="PMX"``（按 ``mmd_bone.name_j`` 映射）。
  ``BLENDER`` / ``RENAMED_BONES`` 在非标准命名模型上会大量失配。

**不做什么**（设计取舍）：不碰引擎、色彩管理、合成器、材质。只写分辨率与帧率。
PMX 路线下场景是从零建的，因此会补一盏最小可用光（``auto_light``），
这是"从零建场景"的必要条件，.blend 路线下完全不会执行。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback

import bpy

MMD_EXTENSION = "bl_ext.blender_org.mmd_tools"

#: 导入模型时不含 PHYSICS —— 否则会生成几百个刚体球与关节辅助物（附录 C 实测）。
#: 导入 PMX 时**固定**要的基础部分。
#:
#: 🚨 **`PHYSICS` 单独处理**（见 :func:`_model_types`）——
#: 漏了它，mmd_tools 一个刚体都不生成，头发/裙摆/胸/饰品**全部静止**
#: （用户报的"物理没生效"就是这个）。
#:
#: 实测（示例模型 240504，604 个刚体的模型）：
#: ================================== ========== ===============
#: 导入参数 刚体数量 骨骼上的物理约束
#: ================================== ========== ===============
#: MESH/ARMATURE/MORPHS/DISPLAY（旧） 0 0
#: 上面这些 + **PHYSICS** **604** 62（TRANSFORM/
#: COPY_TRANSFORMS/IK…）
#: ================================== ========== ===============
#:
#: 别被"604 个刚体"吓到，也别照着旧笔记去"关掉物理省事"：
#: 刚体在 Blender 里是 ``kinematic=True`` 的**从属物**（跟着骨骼走），
#: 真正算物理的是骨骼上那批约束。它们不出现在渲染里
#: （``hide_render=True``），只是求解用的辅助对象。
MODEL_TYPES = {"MESH", "ARMATURE", "MORPHS", "DISPLAY"}


def _model_types(*, physics: bool) -> set[str]:
    """这次导入要模型里的哪几部分。

    ⚠️ **物理关掉时连刚体都不导入**，不是"导进来再停掉求解"。
       两种做法差别很大：
       * 只停求解 → 场景里仍有 604 个刚体 + 62 个约束，装配慢、工程大，
         而且用户在 Blender 里打开会看到一堆他不想要的辅助物
       * 干脆不导入 → 干净、快，代价是那份工程里确实没有物理数据

       用户要的是"开关"，所以按后者做：关 = 真的没有。
    """
    types = set(MODEL_TYPES)
    if physics:
        types.add("PHYSICS")
    return types


def _progress(spec: dict, stage: str, message: str) -> None:
    """把"正在干什么"写进进度文件，供界面实时显示。"""
    path = spec.get("progress") if isinstance(spec, dict) else None
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"stage": stage, "message": message}, handle,
                      ensure_ascii=False)
    except OSError:
        pass


def _log(message: str) -> None:
    # 崩溃时缓冲区输出会全丢，必须 flush（踩坑总表）
    print(f"[BUILD] {message}", flush=True)


#: 物理三档：``档位 -> (substeps_per_frame, solver_iterations)``。
#:
#: 需求书 FR-2.6 要求"稳定 / 自然 / 飘逸"三档。这两个数就是全部旋钮：
#:
#: * ``substeps`` 越大，单帧内动力学被切得越细 → 穿透更少、更稳，但更慢
#: * ``iterations`` 越大，接触/约束求解越收敛 → 抖动更少，同样更慢
#:
#: 数值是按 MMD 的常见经验配的（Blender 默认都是 10）：
#: ``natural`` 用默认值（跟 Blender/MMD 常规一致，不会"怪"），
#: ``stable`` 收小步长、提高迭代（头发裙摆不抖，代价是慢），
#: ``flowing`` 放大步长、减少迭代（更"飘"，也更快，但薄片之间可能穿）。
def _mmd_root_object():
    """找 MMD 的根空物体。

    ⚠️ **不能**用 ``hasattr(o, "mmd_root")`` —— mmd_tools 把这个属性注册在
    Object 类型上，**每个物体都有**（连刚体辅助空物体 ``ncc.*`` 都有，
    读出来 is_built=False）。按 hasattr 找会抓到辅助物体，于是"build 成功但
    is_built 仍是 False"，把后面的烘焙误判成"没 build 过"。
    正确判据是 ``mmd_type == "ROOT"``。
    """
    for obj in bpy.data.objects:
        if getattr(obj, "mmd_type", "") == "ROOT":
            return obj
    # 退路：没有 ROOT 标记时，用骨架的父级
    for obj in bpy.data.objects:
        if obj.type == "ARMATURE" and obj.parent is not None:
            return obj.parent
    return None


#: 最近一次 ``_import_model`` 成功导入的骨架名。见 :func:`_target_armature`。
_recent_armature: str | None = None


def _target_armature():
    """挑出动作与物理应该作用的骨架。

    ⚠️ **不能无脑取 ``armatures[0]``。** route B 打开用户工程、或场景里
    已经有别的骨架时，``bpy.data.objects`` 里排在第一位的是**先入场**
    的那套 —— 动作挂到它上面，刚导入的模型一根关键帧都没有，
    表现就是"动作没导入、人物全程静止"（用户工单实测）。

    规则：``_import_model`` 每次成功导入后把新出现的骨架名记进
    ``_recent_armature``；有记录就用它，没有（纯 route B）才回退第一套。
    """
    arms = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    if not arms:
        return None
    if len(arms) > 1:
        _log(f"场景中有多套骨架（{len(arms)} 套），动作与物理将作用于最近导入模型的骨架")
    if _recent_armature:
        chosen = bpy.data.objects.get(_recent_armature)
        if chosen is not None and chosen.type == "ARMATURE":
            return chosen
    return arms[0]


def _build_rig() -> dict:
    """把 MMD 的骨骼 / 刚体 / 约束真正"搭起来"（``mmd_tools.build_rig``）。

    🚨 **这是物理能不能生效的关键一步，之前一直漏着。**

    ``import_model`` 只负责把数据搬进来：网格、骨架、刚体物件都在了，
    但**骨骼上没有任何约束去跟随刚体** —— 于是物理即使算出来也没人接收，
    头发裙摆永远静止。用户报的"物理开关没设置并且没生效"根子就在这。

    ``build_rig`` 做的事（实测对照）：

    ======================== =========== =============
    步骤 约束总数 rigid_track
    ======================== =========== =============
    只 import_model 62 0
    import_model + build_rig 633 **571**
    ======================== =========== =============

    ⚠️ 必须选中**根对象**（骨架的父级那个 EMPTY），不是骨架本身 ——
       选中骨架调它会 ``poll() failed``。
    ⚠️ 副作用：这 571 个约束会把骨骼"按"在初始姿势上，所以**之后必须烘焙**，
       否则角色比不建约束还要僵（实测烘焙前头发位移 0.0cm）。
    """
    arm = _target_armature()
    if arm is None:
        return {"skipped": "场景里没有骨架"}

    # 根对象 = 带 mmd_root 的那个（一般是骨架的父级空物体）。
    # ⚠️ 必须选它：选骨架调 build_rig 会 poll() failed。
    root = _mmd_root_object()
    if root is None:
        root = arm.parent or arm

    # 🚨 **不要重复 build**：每 build 一次就会再堆一批非碰撞距离约束（211 → 3623 → 更多）。
    # 《物理接入说明》硬约束 2：is_built=True 就跳过。
    already = bool(getattr(getattr(root, "mmd_root", None), "is_built", False))
    if already:
        _log("build_rig：mmd_root.is_built 已经是 True，跳过重复构建")
        tracks = sum(1 for pb in arm.pose.bones for c in pb.constraints
                     if c.name.startswith("mmd_tools_rigid_track"))
        total = sum(len(pb.constraints) for pb in arm.pose.bones)
        return {"skipped": "已经 build 过", "is_built": True,
                "rigid_tracks": tracks, "constraints": total}

    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    root.select_set(True)
    bpy.context.view_layer.objects.active = root
    try:
        # 参数按说明给：1.5 是默认值，实测 1.5/3.0/5.0 差别很小（0.4175/0.4199/0.4191）
        result = bpy.ops.mmd_tools.build_rig(
            non_collision_distance_scale=1.5, collision_margin=1e-06)
    except Exception as exc: # noqa: BLE001 - 失败要说清楚，但别中断装配
        _log(f"build_rig 失败：{type(exc).__name__}: {exc}")
        return {"error": f"{type(exc).__name__}: {exc}"}

    # build 完把物理世界打开（MMD 导入后默认是 False）
    scene = bpy.context.scene
    if scene.rigidbody_world:
        scene.rigidbody_world.enabled = True
    tracks = sum(1 for pb in arm.pose.bones for c in pb.constraints
                 if c.name.startswith("mmd_tools_rigid_track"))
    total = sum(len(pb.constraints) for pb in arm.pose.bones)
    is_built = getattr(getattr(root, "mmd_root", None), "is_built", None)
    _log(f"build_rig: {result} 约束 {total} 个（rigid_track {tracks} 个）"
         f" is_built {is_built}")
    return {"result": str(result), "constraints": total, "rigid_tracks": tracks,
            "is_built": bool(is_built)}


def _ensure_physics_collection() -> int:
    """把场景里的刚体对象挂进 ``rigidbody_world.collection``。

    返回**新挂进去的数量**（0 表示本来就对）。

    ⚠️ 为什么必须做：Blender 的刚体演算只认这个集合里的对象。
       用户自带工程（路线 B）常见情况是刚体都在场景里、集合却是空的 ——
       这时烘焙会"成功"但什么都不算，表现为"物理开了没反应"。
    """
    scene = bpy.context.scene
    world = scene.rigidbody_world
    if world is None:
        return 0
    bodies = [o for o in bpy.data.objects if getattr(o, "rigid_body", None) is not None]
    if not bodies:
        return 0
    if world.collection is None:
        coll = bpy.data.collections.new("RigidBodyWorld")
        scene.collection.children.link(coll)
        world.collection = coll
    added = 0
    for obj in bodies:
        if obj.name in world.collection.objects:
            continue
        try:
            world.collection.objects.link(obj)
            added += 1
        except RuntimeError:
            continue
    return added


def _bake_physics(spec: dict) -> dict:
    """烘焙物理缓存（``ptcache_rigid_body_bake``）。

    🚨 **不烘焙 = 物理不生效。** 建了 571 个 rigid_track 约束之后，
    骨骼会被"钉"在初始姿势上等刚体来驱动；而刚体在没烘焙时不会演算，
    于是头发比不建约束还僵。烘焙才是"把物理跑一遍、把结果存成缓存"。

    用户的操作顺序（照抄，缺一步都不对）：

    > 用户选中角色，在 MMD TOOLS 中打开物理选项，然后把指针拖到 0 帧，
    > 点击烘焙，这时候物理才被加载

    实测（示例模型 604 刚体 / 739 帧）：
    * 烘焙前头发位移 **0.0 cm**
    * 烘焙耗时约 **175 秒**（所以用户特意交代"烘焙时别跑别的任务"）
    * 烘焙后头发位移 **12.0 cm** ✅

    ⚠️ **必须先把帧设到 0** 再烘焙。在别的帧上烘，缓存起点会错位。
    ⚠️ 烘焙是**串行重活**：这一步不要去并行跑别的 Blender 任务，
       既会抢 CPU、也容易让 Blender 的缓存写入出错。
    """
    scene = bpy.context.scene
    if scene.rigidbody_world is None:
        return {"skipped": "没有刚体世界（模型不带物理）"}

    # 「装载完动作之后再打开物理，然后烘焙」—— 用户指定的顺序。
    # 动作已经在场景里，先把帧拉回 0，让姿势回到初始，再 build_rig。
    scene.frame_set(0)
    bpy.context.view_layer.update()
    _progress(spec, "physics", "正在开启物理…")
    rig = _build_rig()
    _log(f"build_rig（动作装载后）：{rig}")

    # 🚨 **没 build 就烘焙 = 烤一份坏结果，而且会存进 .blend，之后救不回来。**
    # 《物理接入说明》硬约束 3：要烤就烤在 build 之后；装配清单第 4 条要求断言。
    root = _mmd_root_object()
    if root is not None and not getattr(root.mmd_root, "is_built", False):
        _log("拒绝烘焙：mmd_root.is_built 还是 False（说明没做过 build_rig）")
        return {"skipped": "没 build 过，拒绝烘焙",
                "is_built": False, "reason": "physics-not-built"}

    arm = _target_armature()
    if arm is not None:
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        arm.select_set(True)
        bpy.context.view_layer.objects.active = arm

    scene.frame_set(0) # ← 用户强调的那一步

    # 🚨 **刚体集合可能是空的 —— 那样烘焙等于空算。**
    #
    # 路线 B（用户自带的 .blend）里，刚体对象都在场景里，但
    # ``scene.rigidbody_world.collection`` 里**一个都没有**（实测 239 个刚体、
    # 集合 0 个物体）。Blender 只演算集合里的刚体，于是：
    # 烘焙"成功"、耗时 1.4 秒（真算要 7 秒+）、缓存标成已烘，但骨骼纹丝不动。
    # 这正是"物理开关开了却没效果"的最后一道坑。
    fixed = _ensure_physics_collection()
    if fixed:
        _log(f"刚体集合是空的，已把 {fixed} 个刚体挂进集合")

    # 缓存范围要覆盖**整段动作**，不是只覆盖"这次渲的那一段"。
    #
    # ⚠️ 原来跟 ``frame_end`` 走：用户只是把「渲染区间」从 0~300 改成 100~200，
    # 装配就判定参数变了 → 重新 build_rig + 重新烘焙（几分钟白等）。
    # 而物理本身跟"渲哪一段"无关，所以这里取两者较大值：
    # 动作有多长就烘多长，以后分段渲染直接复用同一份缓存。
    span = _motion_span()
    span_end = int(span["max"]) if span else 0
    end = max(span_end, int(spec.get("frame_end") or 0), 0)
    world = scene.rigidbody_world
    cache = world.point_cache
    cache.frame_start = 0
    cache.frame_end = max(1, end)

    _progress(spec, "bake", "正在烘焙物理…")
    started = time.time()

    # 🚨 **烘焙期间必须持续写心跳**。
    #
    # 实测：4600 帧的烘焙要 6 分钟以上，而执行器有"进度文件 180 秒没更新 ⇒ 判定卡死"的
    # 看门狗 —— 不心跳就会被当成卡死**杀掉**（用户看到的就是"烘焙到一半没了"）。
    # 这里起个后台线程，每 10 秒写一次已用时，顺带让用户看到它在动。
    _stop_beat = threading.Event()

    def _beat() -> None:
        while not _stop_beat.wait(10):
            spent = int(time.time() - started)
            _progress(spec, "bake", f"正在烘焙物理…（已用 {spent} 秒）")

    _beat_thread = threading.Thread(target=_beat, daemon=True, name="komaflow-bake-beat")
    _beat_thread.start()
    _log(f"开始烘焙物理：帧 0 ~ {cache.frame_end}"
         f"（{world.substeps_per_frame} 子步 / {world.solver_iterations} 迭代）")
    try:
        result = bpy.ops.mmd_tools.ptcache_rigid_body_bake()
    except Exception as exc: # noqa: BLE001
        _stop_beat.set()
        _log(f"烘焙失败：{type(exc).__name__}: {exc}")
        return {"error": f"{type(exc).__name__}: {exc}"}
    _stop_beat.set()
    elapsed = round(time.time() - started, 1)
    _progress(spec, "bake_done",
              f"物理烘焙完成（{cache.frame_start}~{cache.frame_end} 帧，{elapsed} 秒）")
    _log(f"物理烘焙完成：{result}，用时 {elapsed}s，"
         f"缓存 {cache.frame_start}~{cache.frame_end}，已烘={bool(cache.is_baked)}")
    return {"result": str(result), "seconds": elapsed,
            "cache": [cache.frame_start, cache.frame_end],
            "baked": bool(cache.is_baked),
            # build_rig 是在烘焙前做的（动作装完之后），结果一起带上去给上层看
            "build_rig": rig}


def _enable_mmd_tools() -> str:
    import addon_utils

    try:
        module = addon_utils.enable(MMD_EXTENSION, default_set=True, persistent=False)
        return f"OK: {getattr(module, '__name__', module)}"
    except Exception as exc: # noqa: BLE001
        return f"FAIL: {type(exc).__name__}: {exc}"


def _repair_textures(model_dir: str) -> dict:
    """贴图路径失效自动修复（FR-9.11）。

    PMX 里存的是**作者机器的绝对路径**，换台机器就全丢。做法：路径无效时
    按文件名去模型目录找并 reload。找不到的记入清单（FR-8.6 贴图丢失检测）。
    """
    repaired, missing = [], []
    for image in bpy.data.images:
        if image.source != "FILE":
            continue
        path = bpy.path.abspath(image.filepath) if image.filepath else ""
        if path and os.path.isfile(path):
            continue
        candidates = [
            os.path.join(model_dir, image.name),
            os.path.join(model_dir, os.path.basename(image.filepath or "")),
        ]
        hit = next((c for c in candidates if c and os.path.isfile(c)), None)
        if hit:
            try:
                image.filepath = hit
                image.reload()
                repaired.append({"image": image.name, "path": hit})
            except Exception as exc: # noqa: BLE001
                missing.append({"image": image.name, "reason": f"reload 失败: {exc}"})
        else:
            missing.append({"image": image.name, "reason": "文件不存在"})
    return {"repaired": repaired, "missing": missing}


def _bone_coverage(vmd_path: str) -> dict:
    """动作与模型的骨骼兼容性预检（FR-1.4）。

    零依赖解析 VMD 的骨骼名（附录 C 的二进制布局），与模型骨骼的**日文名**
    （``mmd_bone.name_j`` —— 也就是 ``bone_mapper="PMX"`` 实际用的键）求交集。
    失配清单交给用户看，不做静默失败。
    """
    names: list[str] = []
    try:
        import struct

        with open(vmd_path, "rb") as handle:
            handle.seek(50) # 30B 签名 + 20B 模型名
            count = struct.unpack_from("<I", handle.read(4))[0]
            for _ in range(count):
                raw = handle.read(111)
                if len(raw) < 111:
                    break
                raw_name = raw[:15].split(b"\x00")[0]
                try:
                    names.append(raw_name.decode("shift-jis"))
                except Exception: # noqa: BLE001
                    names.append(raw_name.decode("cp932", errors="replace"))
    except Exception as exc: # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}

    unique = sorted(set(names))
    targets = set()
    for armature in (o for o in bpy.data.objects if o.type == "ARMATURE"):
        for pose_bone in armature.pose.bones:
            try:
                key = pose_bone.mmd_bone.name_j
            except Exception: # noqa: BLE001
                key = None
            targets.add(key or pose_bone.name)

    matched = [n for n in unique if n in targets]
    missing = [n for n in unique if n not in targets]

    # 这几根是"没有它动作就不像样"的关键骨骼 —— 单独点名（FR-1.4）
    critical = [
        "センター", "上半身", "上半身2", "下半身", "頭", "首", "腰",
        "右足ＩＫ", "左足ＩＫ", "右足IK", "左足IK",
        "右足", "左足", "右ひざ", "左ひざ", "右足首", "左足首", "右つま先", "左つま先",
        "右腕", "左腕", "右ひじ", "左ひじ", "右手首", "左手首", "右肩", "左肩",
    ]
    critical_missing = [n for n in critical if n in unique and n not in targets]

    return {
        "vmd_bone_keys": len(names),
        "vmd_unique_bones": len(unique),
        "matched": len(matched),
        "missing_count": len(missing),
        "missing_sample": missing[:40],
        "matched_sample": matched[:40],
        "critical_missing": critical_missing,
        "coverage": round(len(matched) / len(unique), 3) if unique else None,
    }


def _inspect_physics_data() -> dict:
    """数一数当前工程里到底有多少物理数据（路线 B 用）。

    返回 ``{"rigidbodies": n, "joints": n, "world": bool, "rigid_tracks": n,
    "verdict": "..."}``。

    ⚠️ 判据是**结构**，不是开关：没有刚体就永远不会有物理，
       把"物理"下拉开到再高也没用 —— 这种情况必须提示用户换素材路径。
    """
    scene = bpy.context.scene
    bodies = [o for o in bpy.data.objects if getattr(o, "rigid_body", None) is not None]
    joints = [o for o in bpy.data.objects
              if getattr(o, "rigid_body_constraint", None) is not None]
    arms = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    tracks = 0
    if arms:
        tracks = sum(1 for pb in arms[0].pose.bones for c in pb.constraints
                     if c.name.startswith("mmd_tools_rigid_track"))
    world = bool(scene.rigidbody_world)
    # 源模型路径：由「渲染模型」存 .blend 时写在场景自定义属性上（读不到当没有）
    source_pmx = ""
    try:
        source_pmx = str(bpy.context.scene.get("komaflow_source_pmx") or "")
    except Exception: # noqa: BLE001
        source_pmx = ""
    _root = _mmd_root_object()
    if _root is not None:
        state_is_built = bool(getattr(_root.mmd_root, "is_built", False))
    else:
        state_is_built = False
    if not bodies:
        verdict = "no_rigid_bodies"
    elif tracks == 0:
        verdict = "bodies_without_tracks"
    else:
        verdict = "ok"
    return {"rigidbodies": len(bodies), "joints": len(joints),
            "world": world, "rigid_tracks": tracks, "verdict": verdict,
            "source_pmx": source_pmx, "is_built": state_is_built}


def _append_blend_stage(path: str, label: str) -> dict:
    """把另一个 ``.blend`` 里的物件**追加**进当前场景，当环境用。

    为什么用 ``bpy.data.libraries.load`` 而不是 ``bpy.ops.wm.append``：
    **操作符依赖 UI 上下文**（要有正确的 window / area / view layer），
    在 ``--background`` 里非常容易抛 "context is incorrect"。这条 API 是纯数据的，
    后台跑稳得多 —— 而我们的渲染全程都是 ``--background``。

    注意：追加进来的物件**不属于任何集合**，必须自己挂进场景集合才会出现；
    子物件也要一起挂（挂上父级不会自动带上子级）。
    """
    _log(f"append_blend_stage: {path}")
    with bpy.data.libraries.load(path, link=False) as (source, target):
        target.objects = list(source.objects)

    scene = bpy.context.scene
    linked: list[str] = []
    for obj in target.objects:
        if obj is None:
            continue
        try:
            scene.collection.objects.link(obj)
        except RuntimeError:
            pass # 已经在集合里（同名物件重复追加）就跳过
        linked.append(obj.name)

    return {"format": "blend", "label": label, "path": path,
            "objects": len(linked), "names": linked[:20]}


def _import_model(path: str, label: str, *,
                  physics: bool = True) -> dict:
    """导入 PMX / PMD 模型。

    ``physics=False`` 时**连刚体都不导入**（见 :func:`_model_types`）。

    ⚠️ 这里**包一层 try**，只为了把**文件名**带上。
    模型文件往往有好几个（角色、舞台），底层报的错里不带路径，
    用户看到"某个素材读不出来"根本不知道是哪个（用户截图来问过）。
    ``raise ... from exc`` 保留原始堆栈，排查不受影响。
    """
    # 记录导入前的骨架名集合 —— 导入后新出现的那个就是本模型的骨架，
    # 之后动作与物理都作用在它身上（见 :func:`_target_armature`；
    # route B / 多模型场景下对象列表顺序不可靠，不能靠 armatures[0]）。
    armature_names_before = {o.name for o in bpy.data.objects
                             if o.type == "ARMATURE"}
    try:
        result = bpy.ops.mmd_tools.import_model(
            filepath=path,
            scale=0.08, # mmd_tools 的 MMD→Blender 常规比例
            types=_model_types(physics=physics),
            clean_model=True,
            rename_bones=False, # 保持原骨骼名；bone_mapper="PMX" 本就按日文名映射
        )
    except Exception as exc: # noqa: BLE001 - 要原样往上报，只是加文件名
        name = os.path.basename(path)
        raise RuntimeError(f"{label} 文件「{name}」读不出来：{exc}") from exc
    global _recent_armature
    new_armatures = [o.name for o in bpy.data.objects
                     if o.type == "ARMATURE" and o.name not in armature_names_before]
    if new_armatures:
        _recent_armature = new_armatures[-1]
    _log(f"import {label}: {result}")
    return {"result": str(result), "physics": bool(physics)}


def _import_motion(path: str, preroll: int, label: str, *,
                   layer: bool = False) -> dict:
    """导入一个 ``.vmd``。

    ``layer=False``（第一个文件）：正常导入，成为骨架当前的 action。
    ``layer=True``（第 2 个起）：**叠加上去**，而不是覆盖前面的。

    🚨 为什么需要 ``layer``
    -----------------------
    ``import_vmd`` 的默认行为是**覆盖同一个 action**：连导两个文件，
    第二个会把第一个整个换掉，工程里只剩 1 个 action（实测：
    导 3 个文件，``bpy.data.actions`` 始终只有 1 个）。
    这跟用户要的"可以添加多个"完全相反。

    mmd_tools 自己给了两个开关正好解决这件事：

    * ``create_new_action=True`` —— 每个文件建自己的 action，不覆盖
    * ``use_nla=True`` —— 并把新 action 放进一条 **NLA 轨道**

    NLA 的语义正是"分层叠加"：上层只覆盖它自己有的通道（骨骼），
    没碰到的骨骼沿用下层 —— 也就是 MMD 圈说的"上半身一个文件、
    下半身一个文件"那种用法。

    ⚠️ **还得手动把轨道的 ``influence`` 设成 1**。mmd_tools 建出来的条带
       默认是 0，等于不参与混合（实测：3 个 action 都在，但姿势只有第 1 个）。
       这是它留给用户自己调的地方，不是 bug。
    ⚠️ 顺序有意义：后导入的在**上层**，所以会盖住同名骨骼。
    """
    arm = _target_armature()
    if arm is None:
        return {"error": "场景里没有 armature，无法挂动作"}

    # ⚠️ **必须连网格一起选中**，否则表情（morph）永远导不进来。
    #
    # mmd_tools 的 ``VMDImporter.assign()`` 是**按选中对象分派**的：
    # * 有 ``shape_keys`` 的对象 → ``__assignToMesh``（写形态键动画）
    # * ARMATURE → ``__assignToArmature``（写骨骼动画）
    # 只选骨架的话，VMD 里的 morph 段会被读到、却**一句警告都不报**地丢掉
    # （实测：``loading ShapeKeyAnimation... 34112`` 之后直接结束，
    # ``bpy.data.actions`` 是空的）。用户报的"表情没加上、嘴部尤其明显"就是它。
    #
    # 加上网格后 ``face_facial`` 动作能正常建出来，形态键按帧变化（已验证）。
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    arm.select_set(True)
    for obj in bpy.data.objects:
        if obj.type == "MESH" and getattr(obj.data, "shape_keys", None):
            obj.select_set(True)
    bpy.context.view_layer.objects.active = arm

    # ── K-1.7 的实现式：场景帧 = VMD帧 + N ──
    #
    # 手法是「借 mmd_tools 自己的偏移」而不是事后改关键帧：
    # frame = vmd_frame + scene.frame_current + frame_margin
    # 把 scene.frame_current 设成预热帧数 N、margin 留 0，所有关键帧就整体落到
    # N 之后。此时第 0 ~ N-1 帧处在**首个关键帧之前**，Blender 按常量外推
    # ⇒ 角色真正"冻住不动"（K-1.2），而物理仍在正常运算（K-1.4）。
    #
    # ⚠️ 不要改用 margin=N + frame_current=0：那样 mmd_tools 会在第 0 帧
    # 补一个"静止姿势"关键帧并带 BEZIER 插值，预热段会变成从静止姿势**缓慢
    # 过渡**到第一个动作姿势 —— 与 K-1.2 的"冻住"不符（已实测）。
    #
    # 骨骼 / 相机 / 光照 / 表情 / 显示走的是同一行偏移代码，因此动作与镜头的
    # 相对关系天然不变（K-1.3）。
    bpy.context.scene.frame_current = preroll
    result = bpy.ops.mmd_tools.import_vmd(
        filepath=path,
        directory=os.path.dirname(path),
        files=[{"name": os.path.basename(path)}], # ← 不给这个就静默不导入
        scale=0.08,
        margin=0,
        bone_mapper="PMX", # ← 只有这个在非标准命名模型上能用
        rename_bones=False,
        update_scene_settings=False, # 帧范围由我们自己管，不让插件改场景
        create_new_action=True,
        use_nla=layer,
    )
    info: dict = {"result": str(result), "layer": bool(layer)}
    if layer:
        info.update(_enable_nla_layers())
    _log(f"import {label}: {result} {info.get('nla', '')}")
    return info


def _enable_nla_layers() -> dict:
    """把 NLA 条带的 ``influence`` 拉满，让它们真正参与混合。

    ⚠️ mmd_tools 建出来的条带 ``influence`` 是 **0**（留给用户手调）。
       不设成 1 的话，action 建了、轨道也有了，**姿势却一点没变** ——
       表现为"加了第二个动作，画面跟只有一个时一模一样"。
    """
    arm = _target_armature()
    if arm is None:
        return {"nla": "没有骨架"}
    anim = arm.animation_data
    if anim is None:
        return {"nla": "没有动画数据"}
    tuned = 0
    for track in anim.nla_tracks:
        for strip in track.strips:
            if strip.influence != 1.0:
                strip.influence = 1.0
                tuned += 1
            # MMD 的语义是"后来的盖住同名骨骼、其余保持" ——
            # NLA 默认的 REPLACE 正好是这个意思，别改成 COMBINE（那会相加变形）
            strip.blend_type = "REPLACE"
    return {"nla": f"轨道 {len(anim.nla_tracks)} 条，调整 {tuned} 个条带"}


def _set_transparent_background() -> dict:
    """把胶片设成透明背景，并且**把世界背景也压掉**。

    两件事都得做，少一件就出不来干净透明：

    * ``scene.render.film_transparent = True`` —— 胶片本身带 alpha 通道
    * 世界背景亮度归零 —— 否则环境光还会被算进画面，
      透明区域会挂着一层淡淡的灰（不是全透明）

    ⚠️ **只允许在"没放舞台"这一条路径上调用**（决策记录 4）。
    用户自己搭了舞台、或者拖进来一个配好的 ``.blend``，都不该动他的世界与胶片设置 ——
    那是设计取舍 划的线：我们只覆盖分辨率和帧率。
    """
    scene = bpy.context.scene
    scene.render.film_transparent = True

    world_zeroed = False
    world = scene.world
    if world is None:
        world = bpy.data.worlds.new("KomaFlow_World")
        scene.world = world
    if world.use_nodes:
        background = world.node_tree.nodes.get("Background")
        if background is not None:
            background.inputs[0].default_value = (0.0, 0.0, 0.0, 1.0)
            background.inputs[1].default_value = 0.0
            world_zeroed = True
    else:
        world.use_nodes = True
        background = world.node_tree.nodes.get("Background")
        if background is not None:
            background.inputs[0].default_value = (0.0, 0.0, 0.0, 1.0)
            background.inputs[1].default_value = 0.0
            world_zeroed = True

    return {"film_transparent": True, "world_zeroed": world_zeroed,
            "world": world.name}


def _add_minimal_light(*, set_world: bool = True) -> dict:
    """PMX 路线下从零建场景，必须给光，否则渲出来是黑的。

    这是**最小可用补光**，不是"调渲染"（设计取舍 禁止的是覆盖用户已有的渲染设置）。
    ``.blend`` 路线完全不执行本函数。

    三盏灯，各管一件事
    ------------------
    ================== ====================================================
    灯 干什么
    ================== ====================================================
    ``KomaFlow_Key`` 主光（投影），沿相机视线略上方 —— 给角色体积感
    ``KomaFlow_Fill`` 反侧补光（投影），把暗部提起来
    ``KomaFlow_Cam`` **角色自带光**：沿相机视线、**不投影**（见下）
    ================== ====================================================

    🚨 两条踩出来的规矩，都别改
    ---------------------------

    **① 光必须跟着相机走。** 原来是一盏固定方向的 SUN
    （``rotation_euler = (0.9, 0, 0.35)``，约等于垂直向下，N·L ≈ 0.05），
    而相机是被镜头动作驱动的 —— 两者对不上，角色就是一坨近黑剪影。
    实测：固定朝下 energy 3 → 角色身上 **5.4/255**；沿相机方向 → **41.8/255**。

    **② 角色必须有一盏不投影的自带光。** 用户要求「让角色自带打光，
    不受环境影响」—— 这条不能靠普通太阳实现，因为**舞台会把阳光挡死**：

    实测（观景车厢舞台，封闭场景）：
    ======================================== ==================
    做法 画面平均亮度
    ======================================== ==================
    原来（两盏太阳 + 世界背景 0.35） 48.5/255（全靠环境光）
    关掉世界、太阳开多亮（15→250） 0.1/255（全黑）
    相机跟随光 **开投影** 20 48.5/255（被舞台挡住 = 白加）
    相机跟随光 **不投影** 5 / 10 / 20 102 / 127 / 152 ✅
    ======================================== ==================

    ``use_shadow = False`` 是关键：光不被舞台遮挡，于是相机看到的那一面
    **永远有一份稳定的亮度**，不再随背景/舞台明暗漂移。
    代价是这盏灯不产生投影 —— 但对"角色主体要看得清"这件事，
    它换来的是稳定，而投影感由另两盏投影灯负责。

    ``set_world=False`` 时只加灯、**不动世界背景** —— 透明背景那条路上
    世界要被压到全黑（见 :func:`_set_transparent_background`），
    这里再给它上一层灰就是互相打架。
    """
    created = []
    scene = bpy.context.scene
    global _lights
    if not any(o.type == "LIGHT" for o in bpy.data.objects):
        # 光的方向全部按相机视线算 → 相机看哪儿就照哪儿
        forward = _camera_forward()

        # 主光（投影）：从相机侧略上方打，给体积感
        light_data = bpy.data.lights.new("KomaFlow_Key", type="SUN")
        # ⚠️ energy 由 3.0 提到 15.0：3.0 是"按固定朝下"那个错误方向配的数
        light_data.energy = 15.0
        key = bpy.data.objects.new("KomaFlow_Key", light_data)
        scene.collection.objects.link(key)
        _aim_sun(key, (forward[0], forward[1], forward[2] - 0.35))
        created.append(key.name)

        # 补光（投影）：从另一侧、略低，把主光照不到的那面提起来
        fill_data = bpy.data.lights.new("KomaFlow_Fill", type="SUN")
        fill_data.energy = 2.0
        fill = bpy.data.objects.new("KomaFlow_Fill", fill_data)
        scene.collection.objects.link(fill)
        _aim_sun(fill, (-forward[0], -forward[1], forward[2] + 0.25))
        created.append(fill.name)

        # 角色自带光（**不投影**）：见上面 ② 的说明。
        # ⚠️ ``use_shadow = False`` 不是随手写的 —— 去掉它，
        # 封闭舞台会把这盏灯一起挡掉，等于没加（实测 152 → 48.5）。
        cam_data = bpy.data.lights.new("KomaFlow_Cam", type="SUN")
        cam_data.energy = 5.0
        cam_data.use_shadow = False
        cam_light = bpy.data.objects.new("KomaFlow_Cam", cam_data)
        scene.collection.objects.link(cam_light)
        _aim_sun(cam_light, (forward[0], forward[1], forward[2] - 0.25))
        created.append(cam_light.name)

        # 登记，好让 _lock_lights_to_camera 逐帧跟相机
        _lights = {"key": key, "fill": fill, "cam": cam_light, "handler": False}

    if not set_world:
        return {"created": created, "world": "skipped（透明背景要保留全黑世界）"}

    world = scene.world
    if world is None:
        world = bpy.data.worlds.new("KomaFlow_World")
        scene.world = world
        created.append(world.name)
    if world is not None and world.use_nodes:
        background = world.node_tree.nodes.get("Background")
        if background is not None:
            background.inputs[0].default_value = (0.35, 0.35, 0.38, 1.0)
            background.inputs[1].default_value = 1.0
    return {"created": created}


#: 自建的三盏灯（``KomaFlow_Key`` / ``KomaFlow_Fill`` / ``KomaFlow_Cam``）。
#: 建好之后登记在这里，:func:`_lock_lights_to_camera` 才能逐帧把它们摆到相机朝向。
#: 只在 PMX 路线用；``.blend`` 路线永远是 ``None``。
_lights: dict | None = None


def _camera_forward() -> tuple[float, float, float]:
    """当前相机**看出去**的方向（单位向量）；没有相机时退回一个保守值。

    没有相机的情形不该发生（``_ensure_camera_for_import`` 之前已经保证有一台），
    但补光这种事**不能因为拿不到相机就报错** —— 退回"从正前方略上方照"
    总比把整段渲成黑的好。
    """
    import mathutils

    camera = bpy.context.scene.camera
    if camera is None:
        return (0.0, -1.0, -0.3)
    try:
        direction = camera.matrix_world.to_quaternion() @ mathutils.Vector((0, 0, -1))
    except Exception: # noqa: BLE001
        return (0.0, -1.0, -0.3)
    if direction.length < 1e-6:
        return (0.0, -1.0, -0.3)
    direction.normalize()
    return (direction.x, direction.y, direction.z)


def _aim_sun(obj, direction) -> None:
    """把一盏 SUN 摆成"沿 direction 照过去"。

    SUN 是**沿自身 -Z 照**的，所以把 -Z 对齐到 direction。
    """
    import mathutils

    vector = mathutils.Vector(direction)
    if vector.length < 1e-6:
        vector = mathutils.Vector((0.0, -1.0, -0.3))
    obj.rotation_euler = (-vector.normalized()).to_track_quat("Z", "Y").to_euler()


def _ensure_camera_for_import() -> dict:
    """导入镜头动作**之前**，保证场景里有一个可以挂动画的相机。

    为什么非要有这一步
    ------------------
    mmd_tools 的 ``import_vmd`` 处理相机段的逻辑是
    "**把动画挂到当前选中的物件上**"。所以：

    * 场景里**没有**相机 → 它在别处建了相机对象、但没设成 ``scene.camera``，
      渲染时仍然是"没有相机"
    * 场景里**有**相机、但一个物件都没选中 → ``poll()`` 直接失败，
      报 ``Operator ... poll() failed, context is incorrect``
      （``ImportVmd.poll()`` 只要求 ``len(context.selected_objects) > 0``）

    三种情况的实测对照（``work/probe_camera_attach.py``）：

    ========================== ==========================================
    导入前状态 结果
    ========================== ==========================================
    没有相机 ``poll() failed``
    有相机、没选中 ``poll() failed``
    **有相机、并选中它** ✅ ``FINISHED``，相机挂上相机动作
    ========================== ==========================================

    所以这里做两件事：**没相机就建一个**（用户指定的做法："生成一个默认相机
    供导入用"），**然后把它选中并设为 active**。
    相机动作具体怎么摆位由 VMD 自己决定 —— 这里只提供"落点"，
    不需要算景别（那是 :func:`_ensure_camera` 在**没有**镜头文件时才做的事）。

    🚨 用户补充指示（原话）
    --------------------
    > 设置把地图的摄像机删除，然后创建默认镜头，导入镜头动作

    **舞台（地图）自带的相机必须删掉。** 它跟镜头动作会冲突：

    * 那是**地图作者**配的机位（朝向他摆场景的角度），不是给角色用的
    * 一旦我们把它 ``convertToMMDCamera``，它就带上了一套 MMD 相机结构
      （父级空物体 + 驱动）；此时再导镜头，mmd_tools 会在**这个已有结构**上操作，
      和"新建一台干净相机"的结果不一致 —— 实测就是**镜头一帧都没挂上**
      （``scene.camera`` 还是那台，但 ``action=None``，画面完全静止，某些帧纯灰）

    所以现在的做法是：**先把场景里所有相机删掉，再新建一台 ``KomaFlow_Cam``**，
    在这台干净相机上做转换与导入。
    """
    scene = bpy.context.scene

    # ── 删掉地图/工程自带的相机（用户指定）──
    # ⚠️ 先摘掉 ``scene.camera`` 与父子关系再删对象，否则会留下悬空引用。
    #
    # ⚠️ **名字必须在 ``remove`` 之前读出来** —— 对象删掉之后再碰它的任何属性
    # （包括 ``.name``）都会抛 ``ReferenceError: StructRNA of type Object
    # has been removed``。我第一版写成 ``remove(...)` 之后 ``append(obj.name)``，
    # 就是这么炸的。
    removed: list[str] = []
    scene.camera = None
    for obj in [o for o in bpy.data.objects if o.type == "CAMERA"]:
        name = obj.name # ← 先存名字
        obj.parent = None
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            removed.append(name)
        except Exception as exc: # noqa: BLE001
            _log(f"删相机 {name} 失败：{exc}")
    # 顺带清掉它们留下的 MMD 相机空物体，免得残留结构与新相机打架
    stale_roots = [o for o in bpy.data.objects
                   if o.type == "EMPTY"
                   and (getattr(o, "mmd_type", "") == "CAMERA"
                        or o.name.startswith("MMD_Camera"))]
    for obj in stale_roots:
        name = obj.name # ← 同样先存名字
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            removed.append(f"{name}(空物体)")
        except Exception: # noqa: BLE001
            pass
    if removed:
        _log(f"已删除场景自带相机：{removed}")

    # ── 新建一台干净相机 ──
    data = bpy.data.cameras.new("KomaFlow_Cam")
    data.lens = 50.0
    data.sensor_width = 36.0
    camera = bpy.data.objects.new("KomaFlow_Cam", data)
    scene.collection.objects.link(camera)
    scene.camera = camera

    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    camera.select_set(True)
    bpy.context.view_layer.objects.active = camera
    bpy.context.view_layer.update()
    _log(f"新建相机 {camera.name}")

    # ── 转成 MMD 相机 ──
    # 这一步是**必需**的：``VMDImporter.assign`` 只在 ``isMMDCamera(obj)`` 为真时
    # 才走 ``__assignToCamera``（普通相机掉进 ``else: pass``，什么都不挂）。
    # 转换会建一个 ``MMD_Camera`` 空物体、把相机挂到它下面。
    converted = False
    try:
        from bl_ext.blender_org.mmd_tools.core.camera import MMDCamera
        if not MMDCamera.isMMDCamera(camera):
            MMDCamera.convertToMMDCamera(camera, scale=0.08)
            converted = True
    except Exception as exc: # noqa: BLE001
        _log(f"转 MMD 相机失败（镜头可能挂不上）：{exc}")

    # 转换完再选一次（转换会改父子关系与选择状态）
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    camera.select_set(True)
    bpy.context.view_layer.objects.active = camera
    bpy.context.view_layer.update()

    _log(f"ensure_camera_for_import: created {camera.name} converted={converted}")
    return {"action": "created", "camera": camera.name,
            "removed": removed, "converted": converted}


def _reset_camera_to_plain() -> None:
    """把场景相机恢复成**普通相机**（从 MMD 相机根下摘出来），然后重新选中。

    用途：镜头导入第一次没挂上时的**重试**。

    ``MMDCamera.convertToMMDCamera`` 是"建一个 ``MMD_Camera`` 空物体、把相机挂进去"。
    要退回普通相机就得反过来：清掉父级、删掉那个空物体、把相机变换恢复成单位。

    ⚠️ 只在**自检失败**时才调 —— 正常路径上不要动用户已经配好的相机结构。
    """
    scene = bpy.context.scene
    camera = scene.camera
    if camera is None:
        return
    root = camera.parent
    camera.parent = None
    camera.matrix_parent_inverse.identity()
    camera.location = (0.0, 0.0, 0.0)
    camera.rotation_mode = "XYZ"
    camera.rotation_euler = (0.0, 0.0, 0.0)
    camera.scale = (1.0, 1.0, 1.0)
    if root is not None and getattr(root, "mmd_type", "") == "CAMERA":
        try:
            bpy.data.objects.remove(root, do_unlink=True)
        except Exception: # noqa: BLE001
            pass
    # 清掉可能挂上的空动作，免得重试时被当成"已经有了"
    if camera.animation_data is not None:
        camera.animation_data_clear()
    scene.camera = camera
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    camera.select_set(True)
    bpy.context.view_layer.objects.active = camera
    bpy.context.view_layer.update()
    _log(f"reset_camera_to_plain: {camera.name}")


def _check_camera_height() -> dict:
    """量相机高度，判断是不是"掉进地里"。

    判据：相机最低点比角色脚底还低 0.1 米以上 → 视为异常。
    典型原因：镜头 VMD 导出时没做 1/0.08 换算，导入后整体缩小 12.5 倍。
    """
    scene = bpy.context.scene
    camera = scene.camera
    if camera is None:
        return {"ok": False, "reason": "没有相机"}

    import mathutils

    meshes = [o for o in bpy.data.objects
              if o.type == "MESH" and o.parent and o.parent.type == "ARMATURE"]
    if not meshes:
        return {"ok": True, "skipped": "没有角色网格"}
    feet = min((o.matrix_world @ mathutils.Vector(c)).z
               for o in meshes for c in o.bound_box)

    lows = []
    original = scene.frame_current
    span = range(int(scene.frame_start), int(scene.frame_end) + 1)
    step = max(1, len(span) // 12) if span else 1
    for frame in list(span)[::step]:
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        lows.append(camera.matrix_world.translation.z)
    scene.frame_set(original)

    low = min(lows) if lows else 0.0
    return {"ok": low >= feet - 0.1, "camera_lowest": round(low, 3),
            "feet": round(feet, 3), "delta": round(low - feet, 3)}


def _verify_camera_animated() -> dict:
    """导完镜头后**读回来验一遍**：相机真的动了没有。

    为什么非要有这一步
    ------------------
    ``import_vmd`` 在处理镜头时，**"什么都没挂上"也会返回 ``FINISHED``**。
    用户那次的现象就是：

    * 日志里 ``loading CameraAnimation... 570`` —— **570 帧确实读到了**
    * ``import camera: {'FINISHED'}``
    * 报告里全是成功
    * 但相机在 0 / 300 / 560 帧的世界坐标**一模一样**，画面完全静止

    静默失败是最难查的一类。所以这里直接**采样几个帧**，看相机世界坐标
    有没有变化 —— 有变化才算真挂上了。

    ⚠️ 相机动画不一定挂在相机自己身上：mmd_tools 把它挂在**父级空物体**
    （``MMD_Camera``）上，相机自己只收一条"距离"。所以要**连父级一起采样**。
    """
    scene = bpy.context.scene
    camera = scene.camera
    if camera is None:
        return {"ok": False, "reason": "没有场景相机"}

    # ⚠️ **采样点必须落在动画有效范围内，不能落在预热段里！**
    #
    # 镜头关键帧是**整体偏移到预热帧之后**的
    # （``frame = vmd_frame + preroll``，见 :func:`_import_camera_motion`），
    # 而预热段的**设计意图就是"冻住不动"**（K-1.2）。
    # 采样点如果全落在 ``0..preroll`` 之间，会看到位置完全一致，
    # **误报成"镜头没挂上"** —— 我自己就被这个骗了一轮
    # （当时 ``scene.frame_end=12``，而动画范围是 30..600，采样全在预热段）。
    #
    # 所以从**动作关键帧的实际范围**里取点，而不是从 ``0..frame_end`` 均匀取。
    watched = [o for o in (camera, camera.parent) if o is not None]
    spans: list[float] = []
    for obj in watched:
        action = obj.animation_data.action if obj.animation_data else None
        if action is None:
            continue
        try:
            lo, hi = action.frame_range
            spans.extend([float(lo), float(hi)])
        except Exception: # noqa: BLE001
            pass
    if not spans:
        return {"ok": False, "reason": "相机没有挂上任何动作", "actions": []}
    lo, hi = min(spans), max(spans)
    if hi - lo < 1e-6:
        return {"ok": False, "reason": f"动作范围退化（{lo}..{hi}）"}

    samples = sorted({int(round(f)) for f in
                      (lo, lo + (hi - lo) * 0.25, lo + (hi - lo) * 0.5,
                       lo + (hi - lo) * 0.75, hi)})
    if len(samples) < 2:
        return {"ok": False, "reason": f"动作范围太短（{lo}..{hi}），验不了"}

    tracks: dict[str, list[tuple]] = {o.name: [] for o in watched}
    original = scene.frame_current
    try:
        for frame in samples:
            scene.frame_set(frame)
            bpy.context.view_layer.update()
            for obj in watched:
                world = obj.matrix_world.translation
                tracks[obj.name].append(
                    (round(world.x, 4), round(world.y, 4), round(world.z, 4)))
    finally:
        scene.frame_set(original)

    moved = [name for name, points in tracks.items() if len(set(points)) > 1]
    has_action = [o.name for o in watched
                  if o.animation_data and o.animation_data.action]
    report = {
        "ok": bool(moved),
        "moved": moved,
        "actions": has_action,
        "frames": samples,
        "range": [lo, hi],
        "tracks": tracks,
    }
    if moved:
        _log(f"camera_verify: 相机在 {samples}（动作范围 {lo:.0f}..{hi:.0f}）"
             f"之间确实在动：{moved}")
    else:
        report["reason"] = "相机在动作范围内的采样帧上位置完全一致 —— 镜头没挂上"
        _log(f"camera_verify: 警告 —— {report['reason']}")
        _log(f" actions={has_action} tracks={tracks}")
    return report


def _import_camera_motion(path: str, preroll: int) -> dict:
    """导入**镜头** VMD —— 必须选中**相机**，不能走 :func:`_import_motion`。

    🚨 这是那个"镜头动作静默不生效"的根因，值得写清楚：

    mmd_tools 的 ``VMDImporter.assign(obj)`` 是**遍历"当前选中的对象"**逐个挂动画的
    （``operators/fileio.py`` 里 ``for i in selected_objects: importer.assign(i)``），
    而挂到哪一类对象取决于**那个对象是什么**：

    * 选中的是 armature → 挂骨骼动画
    * 选中的是 MMD 相机 → 挂镜头动画

    :func:`_import_motion` 是给**动作/表情**用的，它会把 armature 选中并设为 active::

        armatures[0].select_set(True)
        bpy.context.view_layer.objects.active = armatures[0]

    于是拿它去导镜头时，**选中的是 armature、不是相机** ——
    相机动画就没有落点。而 ``import_vmd`` 在这种情况下**照样返回 ``FINISHED``**，
    日志里还会正常打印 ``loading CameraAnimation... 570``（570 帧确实读到了），
    报告里也是成功 —— **只是相机一帧都不动**。

    用户那张纯灰的预览（第 600 帧）就是这么来的：相机纹丝不动，
    有些帧角色正好在画面外，于是渲出一整片灰。

    实测对照（``work/probe_frame2.py``）：

    ==================================== ==================
    做法 结果
    ==================================== ==================
    选中**相机**后 ``import_vmd`` ✅ ``摄像机`` 挂 ``camera_camera_dis``
    走 ``_import_motion``（选中 armature） ❌ 相机 ``action=None``，一帧不动
    ==================================== ==================

    帧偏移仍然沿用 :func:`_import_motion` 那套（``frame_current = preroll``），
    这样镜头和动作的相对关系不变（K-1.3）。
    """
    camera = bpy.context.scene.camera
    if camera is None:
        return {"error": "场景里没有相机，无法挂镜头"}

    # ⚠️ **只选相机本身，别把它的父级空物体也选上。**
    # ``MMDCamera.isMMDCamera(obj)`` 查的是"这个对象有没有 MMD 相机根"，
    # 对 ``MMD_Camera`` 那个**空物体**也返回 True。于是 mmd_tools 会拿
    # 空物体去调 ``convertToMMDCamera``，而它没有 ``.data``::
    #
    # cameraObj.data.sensor_fit = "VERTICAL"
    # AttributeError: 'NoneType' object has no attribute 'sensor_fit'
    #
    # （我把父级也选上时就是这么炸的。）
    # 相机动画是挂在相机自己身上的（``__assignToCamera`` 内部会处理父级），
    # 所以只选相机就够了。
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    camera.select_set(True)
    bpy.context.view_layer.objects.active = camera
    bpy.context.view_layer.update()

    # 帧偏移：跟动作走同一套（frame = vmd_frame + frame_current + margin）
    bpy.context.scene.frame_current = preroll
    result = bpy.ops.mmd_tools.import_vmd(
        filepath=path,
        directory=os.path.dirname(path),
        files=[{"name": os.path.basename(path)}], # ← 不给这个就静默不导入
        scale=0.08,
        margin=0,
        bone_mapper="PMX",
        rename_bones=False,
        update_scene_settings=False,
    )
    _log(f"import camera: {result} (选中 {camera.name})")
    return {"result": str(result), "selected": [camera.name]}


def _lock_lights_to_camera() -> dict:
    """把自补光**逐帧绑死相机朝向** —— 相机相对的光照永远一样。

    为什么要在装配时"钉"这一步
    --------------------------
    用户要的是：

    > 角色的明暗不受背景环境影响，而是建模始终不变，哪怕他做动作

    只在装配时按**某一帧**的相机算一次方向是不够的：镜头一动，
    光与相机的相对角度就变了，角色会出现"某个角度特别暗"的帧。
    装配时算好还不够 —— 需要在**每一帧**保证"相机看到的那面永远同样地被照"。

    做法：注册一个 ``frame_change_post`` 处理器，逐帧把**那盏不投影的
    相机光**重新摆到"沿当前相机视线"的方向。处理器会被**存进 ``work.blend``**，
    所以之后那个长驻 Blender 进程渲染每一帧时它也照样生效。

    ⚠️ **带投影的 Key / Fill 不跟** —— 跟了会让背景"抽风闪烁"
       （详见 ``_follow`` 里的说明）。

    ⚠️ 处理器里**不做帧号缓存**。第一版想"同一帧别重复算"，
       但 Blender 在渲染前会重估依赖图，缓存反而会让渲染用上**过期方向**。
       改成"算完比一下、真变了才写回"，既不抖也不会有过期值。

    ⚠️ 没有相机时**直接返回**，不做任何事 —— 补光不该因为拿不到相机就报错。
    """
    if _lights is None:
        return {"locked": False, "reason": "没有相机"}
    if _lights.get("handler"):
        return {"locked": True, "reason": "已经装过"}

    def _follow(_scene=None, _depsgraph=None) -> None:
        """逐帧只转**那盏不投影的相机光**。

        🚨 **带投影的两盏主光（Key / Fill）故意不跟** —— 这是用户报的
           "背景光莫名抽风闪烁"的根因。

        为什么原来会闪：三盏灯都逐帧跟着相机转，而 Key / Fill **有投影**。
        相机一动，它们的**影子在背景/舞台上就跟着扫**，每一帧的阴影图都得重算；
        64 采样的 EEVEE 在这种"每帧都在变的软阴影"上压不住噪点，
        于是背景一亮一暗地抽 —— 角色本身反而没问题（它被那盏不投影的
        相机光稳稳托着）。

        现在分工：
        * ``KomaFlow_Cam``（**不投影**）跟着相机 → 角色朝向相机那面亮度恒定，
          满足用户"角色的明暗不受背景环境影响"
        * ``KomaFlow_Key`` / ``KomaFlow_Fill``（有投影）**固定不动** →
          环境光照稳定，背景不再闪

        代价：角色转到某些角度时，跟随光的"体积感"略降（因为它不投影）。
        但稳定 > 那点体积感 —— 用户要的是"明暗始终不变"。
        """
        camera = bpy.context.scene.camera
        if camera is None:
            return
        try:
            forward = _camera_forward()
        except Exception: # noqa: BLE001 - 处理器里绝不能抛
            return
        obj = _lights.get("cam")
        if obj is None or obj.name not in bpy.data.objects:
            return
        _aim_sun(obj, (forward[0], forward[1], forward[2] - 0.25))

    bpy.app.handlers.frame_change_post.append(_follow)
    _lights["handler"] = True
    _follow() # 当前帧也立刻摆一次
    return {"locked": True, "follow": ["KomaFlow_Cam"],
            "static": ["KomaFlow_Key", "KomaFlow_Fill"]}


def _ensure_camera(framing: str) -> dict:
    """没有镜头文件时，自动生成一个正面机位（V1.0 冻结范围要求）。

    景别档位：``full`` 全身 / ``seven_tenths`` 七分身 / ``half`` 半身。
    做法是按场景网格的包围盒算出需要的高度，再按相机竖直视场角反推距离。
    只在场景里**没有**相机时才建，绝不覆盖用户已有的相机。
    """
    scene = bpy.context.scene
    if scene.camera is not None:
        return {"action": "keep", "camera": scene.camera.name}

    fractions = {"full": 1.0, "seven_tenths": 0.7, "half": 0.5}
    fraction = fractions.get(framing, 1.0)

    import mathutils

    lows = []
    highs = []
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        for corner in obj.bound_box:
            point = obj.matrix_world @ mathutils.Vector(corner)
            lows.append(point)
            highs.append(point)
    if not lows:
        return {"action": "none", "reason": "场景里没有网格，无法自动取景"}

    min_corner = mathutils.Vector((
        min(p.x for p in lows), min(p.y for p in lows), min(p.z for p in lows)))
    max_corner = mathutils.Vector((
        max(p.x for p in highs), max(p.y for p in highs), max(p.z for p in highs)))
    height = max_corner.z - min_corner.z
    center_z = (max_corner.z + min_corner.z) / 2.0

    camera_data = bpy.data.cameras.new("KomaFlow_Cam")
    camera_data.lens = 50.0
    camera_data.sensor_width = 36.0
    camera_data.sensor_fit = "AUTO"
    camera = bpy.data.objects.new("KomaFlow_Cam", camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera

    import math

    res_x = scene.render.resolution_x
    res_y = scene.render.resolution_y
    # sensor_fit=AUTO 时，36mm 作用在较长边；据宽高比推有效竖直传感器尺寸
    sensor_h = camera_data.sensor_width * (res_y / res_x) if res_y <= res_x \
        else camera_data.sensor_width
    fov_v = 2.0 * math.atan(sensor_h / (2.0 * camera_data.lens))

    target_height = height / fraction
    distance = (target_height / 2.0) / math.tan(fov_v / 2.0) * 1.06 # 6% 余量
    # 面向 +Y（MMD 模型默认朝 +Y），机位在 -Y 一侧
    camera.location = (0.0, -distance, center_z)
    camera.rotation_euler = (math.radians(90.0), 0.0, 0.0)

    return {
        "action": "created",
        "camera": camera.name,
        "framing": framing,
        "model_height": round(height, 4),
        "center_z": round(center_z, 4),
        "distance": round(distance, 4),
        "fov_vertical_deg": round(math.degrees(fov_v), 3),
    }


def main() -> int:
    argv = sys.argv
    if "--" not in argv:
        _log("缺少 spec 参数")
        return 1
    spec_path = argv[argv.index("--") + 1:][0]
    with open(spec_path, encoding="utf-8") as handle:
        spec = json.load(handle)

    report: dict = {
        "ok": False,
        "blender_version": bpy.app.version_string,
        "blender_version_tuple": list(bpy.app.version),
        "factory_startup": "--factory-startup" in argv,
        "warnings": [],
        "errors": [],
    }

    report["mmd_tools"] = _enable_mmd_tools()
    if not hasattr(bpy.ops, "mmd_tools"):
        report["errors"].append("mmd_tools 不可用，无法导入 MMD 素材")
        _write(spec, report)
        return 1

    from_blend = spec.get("blend_in")

    try:
        if from_blend:
            # 路线 B：打开用户已配好的工程。**只读语义** —— 绝不写回原文件（K-10.13）
            bpy.ops.wm.open_mainfile(filepath=from_blend)
            report["opened_blend"] = from_blend
            _log(f"open_mainfile: {from_blend}")
            # 🚨 **路线 B 的物理检查。**
            #
            # 用户报「物理依旧没启用（人物的）」，查出来是这条：他放进来的是
            # 「渲染模型」存出的 .blend，而那条路导入时带了 NO_PHYSICS=1 ——
            # 那份工程里 **0 个刚体、0 个关节、连刚体世界都没有**，
            # 所以无论物理开关怎么开，头发裙摆都不可能飘。
            #
            # 这里必须**如实报出来**，不能让用户以为是"开关没生效"。
            state = _inspect_physics_data()
            report["route_b_physics"] = state

            # 🚨 **build_rig 必须在"挂动作之前"做。**
            #
            # 这是那 28 米爆炸的真因：build_rig 会给每根骨骼建刚性跟随约束，
            # 约束的参考偏移取自**当时的姿势**。路线 A 是"导入完就建约束"（静止姿势）；
            # 我原先在路线 B 里把建约束放在挂动作之后，于是它把**动作第 0 帧的摆姿**
            # 当成了静止参考 —— 约束偏移全错，物理一开就把头发拽飞。
            #
            # 所以这里先把姿势清回静止、建好约束，再往下挂动作。
            # ⚠️ 下面这段"给用户 .blend 补建约束"的尝试**默认关闭**。
            #
            # 试过三种修法（把刚体挂进集合 / 清姿势后再建约束 / 建完再烘焙），
            # 结果都一样：头发被拽到 2318~2843 cm（28 米级），比没物理更糟。
            # 原因是这份 .blend 的刚体来自静帧渲染器，跟整片渲染的求解尺度不配套，
            # mmd_tools 的 build_rig 在它身上重建出来的约束不成立。
            #
            # 结论：**能修**。能力包 v2.0.1 起刚体与绑定原样保留，
            # 我们只要按「动作装完 → 回第 0 帧 → 开物理 → 烘焙」的顺序做就稳
            # （实测物理开 39.42 cm / 关 38.12 cm，与 PMX 路线一致）。
            #
            # 这里只负责"清掉工程里可能带的旧烘焙"，真正的 build_rig + 烘焙
            # 统一在动作装完之后做（见 _bake_physics）。
            if state.get("verdict") in ("ok", "bodies_without_tracks"):
                bpy.context.scene.frame_set(0)
                try:
                    bpy.ops.ptcache.free_bake_all()
                    _log("路线 B：已清掉旧烘焙（避免把坏结果烤进去）")
                except Exception: # noqa: BLE001 - 本来没烘过就会失败
                    pass
                bpy.context.view_layer.update()
                report["route_b_physics"] = _inspect_physics_data()
        else:
            bpy.ops.wm.read_homefile(use_empty=True)

        preroll = int(spec.get("preroll_frames", 30))
        report["preroll_frames"] = preroll

        # ── 舞台 ──
        # 舞台可以是 mmd 模型，也可以是**另一个 .blend**（用户拿 Blender 搭好的环境）。
        # 两者导入方式完全不同，所以在这里分岔。
        stage = spec.get("stage")
        if stage and from_blend:
            # 🚨 用户报过「场景导进去出来场景消失了」。
            # 整份工程路线是"打开你的工程、只加动作"，不往里塞舞台 ——
            # 这是设计取舍，不是 bug，但**必须说出来**，
            # 否则他只看到"场景没了"，会以为是导入失败。
            note = ("整份工程模式：已忽略「舞台」%s（要它参与，角色卡请用 .pmx）"
                    % os.path.basename(str(stage)))
            report["warnings"].append(note)
            _log(note)
        if stage:
            if stage.lower().endswith(".blend"):
                report["stage"] = _append_blend_stage(stage, "stage")
            else:
                report["stage"] = _import_model(stage, "舞台")

        # ── 人物 ──
        model = spec.get("model")
        if model:
            # 物理开关要在**导入之前**决定加载哪些部分：关掉就连刚体都不导。
            # 物理恒开：走 mmd_tools 自带那一套（导入带 PHYSICS + build_rig + 烘焙）
            physics_on = True
            _progress(spec, "load", f"正在装载（{os.path.basename(model)}）…")
            report["model"] = _import_model(model, "角色模型",
                                            physics=physics_on)
            model_dir = os.path.dirname(model)
            report["textures"] = _repair_textures(model_dir)

            # ── 物理（模型导入之后才有刚体世界）──
            #
            # ⚠️ **物理是模型自身的属性**，只在这条路上处理；
            # 路线 B（用户自带 .blend）不碰（设计取舍 的另一面：
            # 人家工程里怎么配的就怎么算，我们连求解参数都不该改）。
        tracks = [p for p in (spec.get("motions") or []) if p]
        report["motions"] = []
        seen_paths: set[str] = set()
        imported_count = 0
        for index, path in enumerate(tracks, 1):
            key = os.path.normcase(os.path.abspath(path))
            if key in seen_paths:
                report["motions"].append(
                    {"path": path, "skipped": "同一个文件，已导入过"})
                _log(f"跳过重复的动作文件：{path}")
                continue
            seen_paths.add(key)
            label = f"motion{index}"
            try:
                # **每个动作都走 NLA**（包括第一个）。
                #
                # ⚠️ 第一个如果留在"当前 action"上，后面建的 NLA 轨道会把
                # active action 置空，于是第一个 action 变成**零引用** ——
                # 存盘时被 Blender 当垃圾清掉（实测：3 个文件只剩 2 个 action）。
                # 全部进 NLA 就没有这个问题：每个 action 都被一条轨道引用着。
                _progress(spec, "motion", f"正在装载（{os.path.basename(path)}）…")
                info = _import_motion(path, preroll, label, layer=True)
                imported_count += 1
                report["motions"].append({"path": path, **info})
            except Exception as exc: # noqa: BLE001 - 一个坏了不该拖垮全部
                report["motions"].append(
                    {"path": path, "error": f"{type(exc).__name__}: {exc}"})
                _log(f"动作文件读不出来（跳过）：{path} —— {exc}")
        if imported_count:
            report["layers"] = _enable_nla_layers()
        # 骨骼覆盖检查用**第一个**动作文件（那是用户心里"主动作"）
        if tracks:
            report["bone_coverage"] = _bone_coverage(tracks[0])

        # ── 物理烘焙（必须在动作挂完之后）──
        #
        # ⚠️ **顺序**：刚体跟着骨骼走，所以得先有动作、再去烘 —— 反过来烘的是
        # "角色还没动"的空缓存。这也是用户流程里"打开物理 → 拖到 0 帧 → 烘焙"
        # 中间夹着动作的原因。
        # ⚠️ 这一步很慢（实测 604 刚体 / 739 帧约 **175 秒**），
        # 而且期间**不要并行跑别的 Blender 任务**。
        if model:
            report["physics_bake"] = _bake_physics(spec)
        elif from_blend:
            # 路线 B：用户工程里**已经有刚体**但没建约束/没烘焙时，在工作副本上补一次。
            # ⚠️ 只动 work 里的副本，绝不写回用户的 .blend（K-10.13）。
            state = report.get("route_b_physics") or {}
            verdict = state.get("verdict")
            # ⚠️ **只在"本来就有约束"的工程上补烘焙。**
            #
            # 实测：用户 .blend 里那 239 个刚体是"渲染模型"导入时带过来的，
            # 它们的尺度/参数跟整片渲染不配套。若在这里补 build_rig + 烘焙，
            # 结果是**头发炸飞 28 米**（2843 cm），比没有物理还糟。
            #
            # 所以只在工程已经自带 rigid_track（说明是 mmd_tools 正常装配出来的）
            # 时才烘焙；缺约束的一律交给上层提示用户改用 PMX。
            # 两种情况都要走烘焙：
            # ok → 已有约束，直接烤
            # bodies_without_tracks → 烘焙里会先 build_rig（动作已装完），再烤
            if verdict in ("ok", "bodies_without_tracks"):
                report["physics_bake"] = _bake_physics(spec)
            else:
                _log(f"路线 B 物理数据不完整（{verdict}），跳过烘焙："
                     f"刚体 {state.get('rigidbodies')} 个、约束 "
                     f"{state.get('rigid_tracks')} 个")

        # ── 镜头 ──
        # ⚠️ **导入之前必须先有一个相机，并且把它选中** —— 否则 mmd_tools 的
        # ``import_vmd`` 会 ``poll() failed``（它要求 selected_objects 非空，
        # 而且相机动画是往"选中的那个物件"上挂的）。
        # 详细实测对照见 :func:`_ensure_camera_for_import`。
        camera_motion = spec.get("camera_motion")
        if camera_motion:
            report["camera_ready"] = _ensure_camera_for_import()
            # ⚠️ 必须用**镜头专用**的导入：它会选中相机，
            # 而 ``_import_motion`` 会选中 armature（见函数文档）。
            report["camera"] = _import_camera_motion(camera_motion, preroll)
            verify = _verify_camera_animated()
            if not verify.get("ok"):
                _log("camera_verify 没过，重试一次镜头导入")
                _reset_camera_to_plain()
                report["camera_retry"] = _import_camera_motion(camera_motion,
                                                               preroll)
                verify = _verify_camera_animated()
                verify["retried"] = True
            report["camera_verify"] = verify
            # 🚨 单位不匹配的兜底检测：镜头 VMD 如果是按"米"导出的，
            # 再按 0.08 导入就会被缩小 12.5 倍 —— 表现是**相机贴在地面里**。
            # 这里量一下相机高度，明显低于角色脚底就明确警告（不擅自改构图）。
            try:
                report["camera_height"] = _check_camera_height()
            except Exception as exc: # noqa: BLE001
                _log(f"相机高度检测失败：{exc}")

        # ── 补光（仅 PMX 路线）──
        # 透明背景时不让它碰世界：那片黑是透明通道要的，别覆盖
        wants_transparent = bool(spec.get("transparent")) and not from_blend \
            and not spec.get("stage")
        if spec.get("auto_light", True) and not from_blend:
            report["auto_light"] = _add_minimal_light(set_world=not wants_transparent)

        # ── 只覆盖分辨率与帧率（设计取舍）──
        scene = bpy.context.scene
        # 🚨 色彩管理：**MMD 材质是按 sRGB 作者色调的**，Blender 4/5 默认 AgX
        # 会把它压得发灰发白（实测同一帧：AgX 饱和度 ~14，Standard ~28）。
        # 观感迁移如果带了自己的设置会覆盖这里，所以先给一个正确默认值。
        try:
            scene.view_settings.view_transform = "Standard"
            scene.view_settings.look = "None"
            scene.view_settings.exposure = 0.0
            scene.view_settings.gamma = 1.0
            report["view_transform"] = "Standard"
        except (AttributeError, TypeError) as exc:  # noqa: BLE001 - 老版本 Blender 没有该枚举
            _log(f"设置色彩管理失败（继续用默认）：{exc}")
        scene.render.fps = int(spec.get("fps", 30))
        scene.render.fps_base = 1.0
        scene.render.resolution_x = int(spec.get("width", 2560))
        scene.render.resolution_y = int(spec.get("height", 1440))
        scene.render.resolution_percentage = 100

        # ── 透明背景（决策记录 4：只在"没放舞台"这条路径上动世界与胶片）──
        # 判断放在工具侧（core.project.effective_transparent），这里只认 spec。
        # 有一条硬前提：真放了舞台就不许动 —— 双保险，免得 spec 被手改后越界。
        if spec.get("transparent"):
            if spec.get("stage"):
                report["warnings"].append(
                    "要求了透明背景，但这次放了舞台 —— 已忽略，不动你的背景设置")
            elif from_blend:
                report["warnings"].append(
                    "自带 .blend 的路线不替用户改背景，已忽略透明背景请求")
            else:
                report["transparent"] = _set_transparent_background()

        # ── 工程时间轴：含预热帧（设计取舍 / K-2.1）──
        span = _motion_span()
        report["motion_span"] = span
        span_max = int(span["max"]) if span else 0
        requested_end = spec.get("frame_end")
        # ⚠️ **起始帧要听界面的「只渲这一段」。**
        # 以前这里写死 ``scene.frame_start = 0``，于是用户填了
        # 「30 到 330」也照样从头渲到尾（实测设 120~127 渲了 128 帧）。
        first = int(spec.get("frame_start") or 0)
        last = int(requested_end) if requested_end is not None else span_max
        # ⚠️ 界面上"渲染区间"没填（0..0）时必须用**动作长度**，
        # 否则 total_frames=1，用户点了渲染只出一帧（就是这么撞上的）。
        if last <= first:
            last = max(last, span_max, first)
        scene.frame_start = max(0, first)
        scene.frame_end = max(scene.frame_start, last)
        #: ``total_frames`` 是**这一段要渲多少帧**，不是"到第几帧"。
        report["total_frames"] = scene.frame_end - scene.frame_start + 1

        # ── 相机：兜底 ──
        # 分两种情况：
        # * **没有**镜头文件 → 自动生成正面机位（V1.0 冻结范围要求）
        # * **有**镜头文件，但用户自己的工程里连相机都没有 → 也得补一个。
        # ``auto_camera`` 在有镜头文件时是 False（正常语义是"构图听镜头文件的"），
        # 可镜头文件只提供**运动轨迹**、不提供"相机这个物件"。
        # 少了这一步，装配完的场景里没有相机，渲染会直接报
        # "场景里没有相机，无法渲染"。
        if scene.camera is None:
            if spec.get("auto_camera", True):
                report["auto_camera"] = _ensure_camera(spec.get("framing", "full"))
            else:
                # 有镜头文件却还缺相机 —— 建一个落点即可，摆位由 VMD 决定
                report["camera_fallback"] = _ensure_camera_for_import()

        # ── 把自补光绑死相机朝向（PMX 路线）──
        # ⚠️ 位置有讲究：必须在**相机最终就位之后**（上面那段兜底之后）。
        # 相机还没定下来就锁方向，锁的是个错的方向。
        # 处理器会随 work.blend 一起存盘，之后长驻进程渲每一帧都生效。
        if spec.get("auto_light", True) and not from_blend:
            report["light_lock"] = _lock_lights_to_camera()

        # ── 相机裁剪与景深：**默认不设上限** ──
        #
        # 用户要求「景深默认无上限」。查到两处会"切画面"的来源：
        # * 能力包把相机远裁剪写死 ``clip_end = 200``（静帧够用，舞台一大就切）
        # * Blender 相机默认远裁剪只有 100
        # 所以这里统一放开：远裁剪拉到很大，近裁剪压到很小；
        # 景深**默认关**（关掉才等于"全都清晰、没有景深范围限制"）。
        # ⚠️ 路线 B 是**用户自己的工程**：景深如果他自己配了，就该保留（设计取舍 的另一面）。
        # 裁剪端点仍然放开 —— 那属于"别把东西切掉"，不是构图偏好。
        report["camera_limits"] = _relax_camera_limits(
            keep_dof=bool(spec.get("keep_dof", False)) or bool(from_blend))

        # ── 统计 ──
        report["objects"] = len(bpy.data.objects)
        report["object_types"] = _count_types()
        report["actions"] = [
            {"name": a.name, "fcurves": len(a.fcurves),
             "frame_range": [round(x, 3) for x in a.frame_range]}
            for a in bpy.data.actions
        ]
        # 由动作范围推工程总帧数（含预热）——已在上面算过，这里只保留场景快照
        report["scene"] = {
            "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
            "fps": scene.render.fps,
            "resolution": [scene.render.resolution_x, scene.render.resolution_y],
            "engine": scene.render.engine,
            "camera": scene.camera.name if scene.camera else None,
        }

        # ── 存工具自有的工作工程（绝不覆盖用户文件）──
        work_blend = spec.get("work_blend")
        if work_blend:
            _progress(spec, "save",
                      "正在保存（%s）…" % os.path.basename(work_blend))
            os.makedirs(os.path.dirname(work_blend), exist_ok=True)
            bpy.ops.wm.save_as_mainfile(filepath=work_blend)
            report["work_blend"] = work_blend
            # 这条日志是给用户看的：**这个 .blend 就是"全部组装在一起"的那份**，
            # 想自己微调（改材质 / 灯光 / 相机 / 加东西）就打开它。
            print(f"[BUILD] 组装好的工程 -> {work_blend}"
                  f"（角色 + 动作 + 表情 + 镜头 + 舞台都在里面）", flush=True)

        report["ok"] = not report["errors"]

    except Exception as exc: # noqa: BLE001
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        report["traceback"] = traceback.format_exc()[-2000:]
        _log(f"FAILED: {exc}")

    _write(spec, report)
    return 0 if report["ok"] else 1


def _relax_camera_limits(*, keep_dof: bool = False, far: float = 100000.0) -> dict:
    """把场景里所有相机的**裁剪与景深**放开到"无上限"。

    :param keep_dof: ``True`` 时不关景深（用户工程里自己配好的景深保留）。
    :param far: 远裁剪端点。默认 10 万单位 —— 对这个尺度来说是"无上限"。

    为什么要做：
      * Blender 相机默认远裁剪只有 **100**，能力包又把它写死成 **200**；
        舞台/背景一大，远处的东西就被**整个切掉**（表现为"背景缺一块"）
      * 景深（DOF）一旦开着就有"清晰范围"，超出就糊；默认关掉才符合
        "景深无上限"——整帧都清晰

    ⚠️ 只改**裁剪与景深**，不动焦距/位置/朝向 —— 那些是构图，改了就是另一种画面。
    """
    changed = []
    for cam in [o for o in bpy.data.objects if o.type == "CAMERA"]:
        data = cam.data
        before = (data.clip_start, data.clip_end, data.dof.use_dof)
        if data.clip_end < far:
            data.clip_end = far
        if data.clip_start > 0.01:
            data.clip_start = min(data.clip_start, 0.01)
        if not keep_dof and data.dof.use_dof:
            data.dof.use_dof = False
        after = (data.clip_start, data.clip_end, data.dof.use_dof)
        if after != before:
            changed.append({"camera": cam.name, "from": list(before), "to": list(after)})
    if changed:
        _log(f"相机裁剪/景深已放开：{len(changed)} 台")
    return {"changed": changed, "keep_dof": keep_dof, "far": far}


def _count_types() -> dict:
    counts: dict = {}
    for obj in bpy.data.objects:
        counts[obj.type] = counts.get(obj.type, 0) + 1
    return counts


def _motion_span() -> dict | None:
    lo, hi = None, None
    for action in bpy.data.actions:
        if not action.fcurves:
            continue
        a_lo, a_hi = action.frame_range
        lo = a_lo if lo is None else min(lo, a_lo)
        hi = a_hi if hi is None else max(hi, a_hi)
    if lo is None:
        return None
    return {"min": round(lo, 3), "max": round(hi, 3)}


def _write(spec: dict, report: dict) -> None:
    target = spec.get("report")
    if not target:
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        return
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    # C11：先写 .tmp 再替换 —— 渲染中途被杀不会留下截断的 report，
    # 也就不会让下次复用时 json.loads 抛错（那会整次渲染失败）。
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, target)
    _log(f"report -> {target}")


main()
