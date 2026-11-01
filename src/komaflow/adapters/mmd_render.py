# -*- coding: utf-8 -*-
"""模型静帧渲染：把 ``.pmx`` 渲成三视图，供「渲染模型」那块独立区域用。

它解决什么
----------
需求原文：

> 把工作区里的模型渲染能力装进去，让用户不再需要自己打开 Blender 再导入，
> 记得做独立区域，然后制作完毕后自动导入到流程内的模型区域里面

所以这里只做一件事：**用子进程驱动 Blender 跑那个已验证的渲染器**，
把产品（PNG）交回界面。界面怎么摆、什么时候回填「模型」卡由 UI 层决定。

三条硬约束（都是踩过的，别改）
------------------------------
1. **走子进程，不打包 Blender** —— Blender 与 mmd_tools 是 GPL 系；
   子进程调用不传染，打进安装包就要履行 GPL 义务。
2. **必须设超时** —— Blender 偶尔会崩或卡住，不加超时程序会一直挂着。
3. **模型不进仓库、不改用户环境** —— 只读用户给的 ``.pmx``，
   产物写进我们自己的工作目录（``NO_PHYSICS=1`` 也是为此：不生成几百个刚体）。

进度协议
--------
渲染器把「阶段 / 进度 / 产物 / 错误」**整体重写**进 ``PROGRESS_JSON`` 那个文件，
所以文件随时可能被读到一半（EOF）—— 读取必须 try 掉重试。这里用一个 0.25 秒的
轮询线程照抄那份协议，跟 ``MMD集成包/mmd_render_client.py`` 的做法一致
（上游是 MIT，逻辑一致、实现按本项目的适配层风格重写）。

阶段顺序：``init → import → materials → outline → camera → render → done``
（失败时 ``error``）。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

#: 渲染脚本（随包分发的第三方文件，见 ``vendor/来源与许可.md``）
VENDOR_DIR = Path(__file__).parent / "vendor"
RENDER_SCRIPT = VENDOR_DIR / "MMD渲染.py"
#: 拼图脚本（跑在 Blender 里，因为它自带 PIL —— 我们不准引 pip 依赖）
STITCH_SCRIPT = Path(__file__).parent / "scripts" / "stitch_views.py"

#: 支持的视角。``front`` 用的就是 ``frame`` 的取景，另外三个各自一张。
VIEWS = ("front", "side", "back", "side2")

#: 界面上的中文名（日志与状态里用，别在别处再写一遍）
VIEW_LABELS = {"front": "正面", "side": "侧面", "back": "背面", "side2": "另一侧"}
FRAME_LABELS = {"full": "全身", "half": "半身", "face": "脸部"}
STAGE_LABELS = {
    "init": "准备中", "import": "导入模型", "materials": "还原材质",
    "outline": "生成描边", "camera": "架设相机", "render": "出图",
    "done": "完成", "error": "失败",
}

#: 每张图大概给多少秒。纯属"卡住就救"的兜底，不是性能预期 ——
#: 实测 2K 三视图整轮在几十秒量级，给足是为了别误杀慢机器。
SECONDS_PER_SHOT = 300
#: 最少给多少秒（单张也要把 Blender 启动、导入、材质走完）
MIN_TIMEOUT = 300


class MMDError(RuntimeError):
    """渲染失败。``log`` 是 Blender 的完整输出，``result`` 是最后一份进度。"""

    def __init__(self, message: str, log: str = "", result: dict | None = None):
        super().__init__(message)
        self.log = log
        self.result = result or {}


def default_timeout(shots: int, base: int = 900) -> int:
    """按张数给一个超时。**永远不等于"不限时"**。"""
    return max(MIN_TIMEOUT, int(base), int(shots) * SECONDS_PER_SHOT)


def _no_window() -> dict:
    """Windows 下别弹出黑窗口（跟 blender5.py 同一套）。"""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def available() -> tuple[bool, str]:
    """自检：渲染脚本在不在、Blender 给了没。

    :return: ``(能不能渲, 不能的话是为什么)``
    """
    if not RENDER_SCRIPT.is_file():
        return False, f"缺少渲染脚本：{RENDER_SCRIPT}"
    return True, ""


def _read_progress(path: Path) -> dict | None:
    """读一份进度 JSON。**读到一半（EOF）就当没有**，下次再读 —— 它是整体重写的。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def render_model(pmx, outdir, name=None, *, shots=("front", "side", "back"),
                 res="2048x2048", ortho=True, frame="full",
                 edge_skip=None, frame_skip=None, no_edge=False,
                 look=None, bg=None, look_inner=None,
                 save_blend=None, blend_keep_camera=False,
                 blender=None, timeout=None, on_progress=None,
                 cancel=None) -> dict:
    """渲一个 ``.pmx``，返回产物清单；**可以顺便存一份 .blend**。

    :param pmx: 模型路径（用户自己的，**绝不拷进程序目录**）
    :param outdir: 出图目录（不存在会建）
    :param name: 文件名前缀，默认取模型名
    :param shots: 要哪些视角，取值见 :data:`VIEWS`
    :param res: ``宽x高``，例如 ``2048x2048``
    :param ortho: ``True`` 正交（设定图那种平视感），``False`` 透视
    :param frame: ``full`` / ``half`` / ``face``
    :param edge_skip: 这些**材质名子串**不做描边（薄片、翅膀会画成黑块，见踩坑总表 C6）
    :param frame_skip: 同上，但只影响取景（巨大翅膀会把人物压成一个小点）
    :param look: 观感层（渲染器 v2.0.0 起）：``"anime"`` / ``"mmd"``。
        **默认 ``None`` = 不设，听能力包的**（它自己的默认是 ``anime``）。
        ⚠️ 用户定的规矩是「与能力包冲突时以能力包为准」——
           我们不替它定默认值，只在调用方明确要求时才覆盖。
    :param bg: 底色：``none`` / ``white`` / ``grad`` / ``gray``。
        默认 ``None`` = 不设，听能力包的（它按 `LOOK` 自动取）。
    :param look_inner: 内线强度。默认 ``None`` = 不设，听能力包的。
    :param save_blend: 顺便存一份 ``.blend`` 到这个路径。
        ⚠️ **这份 .blend 是"可编辑的模型资产"**，不是渲染工程的快照 ——
        渲染器会把模型单独归到一个集合、把相机/灯放进另一个叫
        ``_预览用（可删）`` 的集合，材质还带驱动器（改 ``mmd_material.alpha``
        立刻生效）。所以它能直接 append 进别人的场景。
    :param blend_keep_camera: 存 .blend 时**是否保留相机与世界**。
        默认 ``False`` —— 要的是"纯素材"；设 ``True`` 则连预览相机一起留。
    :param blender: 用哪台 Blender（界面传用户选定的那一台）
    :param timeout: 秒。``None`` 时按张数给（见 :func:`default_timeout`）
    :param on_progress: ``fn(dict)``，在**轮询线程**里被调用 ——
                        界面要自己搬到主线程（我们自己是用队列）
    :raises MMDError: 模型不存在 / 找不到 Blender 或脚本 / 退出码非 0 / 超时
    """
    pmx_path = Path(pmx)
    if not pmx_path.is_file():
        raise MMDError(f"模型不存在：{pmx_path}")

    exe = str(blender or "").strip()
    if not exe or not Path(exe).is_file():
        raise MMDError("还没选定用哪台 Blender，渲不了。先去设置里选一台。")
    if not RENDER_SCRIPT.is_file():
        raise MMDError(f"缺少渲染脚本：{RENDER_SCRIPT}")

    views = [v for v in (shots if not isinstance(shots, str) else shots.split(","))
             if str(v).strip()]
    views = [str(v).strip() for v in views] or ["front"]
    unknown = [v for v in views if v not in VIEWS]
    if unknown:
        raise MMDError(f"不认识的视角：{', '.join(unknown)}（可用：{', '.join(VIEWS)}）")
    if timeout is None:
        timeout = default_timeout(len(views))

    out_path = Path(outdir)
    out_path.mkdir(parents=True, exist_ok=True)
    prefix = str(name or pmx_path.stem)

    progress_file = out_path / "_render_progress.json"
    try:
        progress_file.unlink()
    except OSError:
        pass

    env = dict(os.environ)
    # ★ 默认**不要明暗/阴影**：人物只出贴图本色。
    #
    # 用户口径：「阴影都不需要」「动作不该让人物明暗变化」。
    # 能力包的 LOOK_SHADE 是"全局明暗乘数"，LOOK_AO 是接触阴影；
    # 两个都归零 ⇒ 赛璐璐色阶不参与，人物就是干净的平色（描边/球面仍保留）。
    # 想恢复能力包原来的观感：设环境变量 LOOK_SHADE=1 / LOOK_AO=0.12 即可。
    env.setdefault("LOOK_SHADE", "0")
    env.setdefault("LOOK_AO", "0")
    env.update({
        "SHOTS": ",".join(views),
        "ORTHO": "1" if ortho else "0",
        "RES": str(res),
        "FRAME": str(frame),
        "PREFIX": prefix,
        "PROGRESS_JSON": str(progress_file),
        # ⚠️ 只有"纯出图"时跳过刚体；**要存 .blend 交付时必须带物理**。
        # 否则存出来的工程里 0 刚体，用户把它放回「角色模型」渲整片时，
        # 头发裙摆永远不飘 —— 用户就是这样撞上的（物理开关开了也没用）。
        "NO_PHYSICS": "0" if save_blend else "1",
    })
    # ── 观感层（渲染器 v2.0.0 起）：**默认不覆盖，听能力包的** ──
    #
    # 用户定的规矩：**和能力包冲突时，以能力包为准**。
    # 所以这几项默认**不设**，让渲染器用它自己的默认值
    # （`LOOK=anime` / `BG` 按是否 mmd 模式自动取 / `LOOK_INNER=0.9`）。
    # 覆盖能力包的默认值 = 我们自己偷偷定标准，那正是要避免的"冲突"。
    #
    # 只有调用方**明确**要求某种观感时才传进来（界面上的选项走这条路）。
    for key, value in (("LOOK", look), ("BG", bg),
                       ("LOOK_INNER", look_inner)):
        if value is None:
            continue
        env[key] = str(value)
    if no_edge:
        env["NO_EDGE"] = "1"
    for key, value in (("EDGE_SKIP", edge_skip), ("FRAME_SKIP", frame_skip)):
        if not value:
            continue
        env[key] = value if isinstance(value, str) else ",".join(str(v) for v in value)
    if save_blend:
        env["SAVE_BLEND"] = str(save_blend)
        # ⚠️ ``BLEND_KEEP_CAM=0`` 是"纯素材"形态：删掉相机与世界，
        # 这样别人 append 进自己场景时不会被我们的预览相机/世界干扰。
        env["BLEND_KEEP_CAM"] = "1" if blend_keep_camera else "0"
        # 把源模型路径一起交下去：这个 .blend 以后被放进「角色模型」走路线 B 时，
        # 我们就能知道它的源 PMX 在哪 —— 缺物理数据可直接告诉用户改放哪个文件。
        env["KOMEFLOW_SOURCE_PMX"] = str(pmx_path)

    command = [
        # ⚠️ **不要加 `--noaudio`** —— Blender 5.2 不认这个参数，
        # 会把它当成"要打开的文件名"，然后一路报错退出（实测：
        # `无法读取文件 "--noaudio"`，渲染直接不跑）。
        # 静帧渲染本来也不出音轨，不需要它。
        exe, "--background",
        "--python", str(RENDER_SCRIPT), "--",
        str(pmx_path), str(out_path), prefix,
    ]

    result: dict = {}
    stop = threading.Event()

    def _watch() -> None:
        last: dict | None = None
        while not stop.is_set():
            data = _read_progress(progress_file)
            if data is not None and data != last:
                last = data
                result.clear()
                result.update(data)
                if on_progress is not None:
                    try:
                        on_progress(dict(data))
                    except Exception: # noqa: BLE001 - 回调坏了不该毁掉渲染
                        pass
            stop.wait(0.25)

    watcher = threading.Thread(target=_watch, daemon=True,
                               name="KomaFlow-mmd-progress")
    watcher.start()

    log_path = None
    timed_out = False
    cancelled = False
    returncode = None
    try:
        handle = tempfile.NamedTemporaryFile(
            "w+", encoding="utf-8", errors="replace", suffix=".log",
            prefix="komaflow_render_", delete=False)
        log_path = Path(handle.name)
        handle.close()
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT, env=env,
                cwd=str(out_path), stdin=subprocess.DEVNULL, **_no_window())
            started = time.time()
            while True:
                returncode = process.poll()
                if returncode is not None:
                    break
                if cancel is not None and cancel.is_set():
                    # 用户关掉「渲染模型」窗：立刻收掉子进程，别继续占 CPU/GPU，
                    # 也别让它和下一个窗口同时写同名 PNG / .blend（审查 R17）。
                    cancelled = True
                    process.kill()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    returncode = -1
                    break
                if timeout and (time.time() - started) > timeout:
                    timed_out = True
                    process.kill()
                    returncode = -1
                    break
                time.sleep(0.2)
    except OSError as exc:
        raise MMDError(f"起不来 Blender：{exc}") from exc
    finally:
        stop.set()
        watcher.join(timeout=1.5)

    # R12：子进程结束后**再同步读一次**最终进度。
    # watcher 每 0.25 秒才轮询一次，最后一次 done（含完整 outputs）完全可能
    # 落在两次轮询之间；只信 watcher 会丢掉最后一张图 / 整份产物清单。
    final = _read_progress(progress_file)
    if isinstance(final, dict) and final:
        result.clear()
        result.update(final)

    log_text = ""
    if log_path is not None:
        try:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        try:
            log_path.unlink()
        except OSError:
            pass
    try:
        progress_file.unlink()
    except OSError:
        pass

    if cancelled:
        raise MMDError("渲染已取消（窗口已关闭）。", log_text, result)
    if timed_out:
        raise MMDError(f"渲染超时（超过 {timeout} 秒）—— Blender 可能卡住了。",
                       log_text, result)
    if returncode != 0:
        raise MMDError(
            "渲染失败：" + str(result.get("error") or f"Blender 退出码 {returncode}"),
            log_text, result)
    if result.get("stage") == "error":
        raise MMDError("渲染失败：" + str(result.get("error") or "未知原因"),
                       log_text, result)
    if not result:
        raise MMDError("渲染没有回报进度（Blender 可能没跑到出图那步）。",
                       log_text, result)
    if result.get("stage") != "done":
        # R12：进程正常退出但最后阶段不是 done —— 进度被人为截断/写坏。
        raise MMDError(
            f"渲染进程结束了，但最后阶段是 {result.get('stage')!r}（不是 done）；"
            "进度可能被截断，产物不可信。", log_text, result)

    outputs = [str(p) for p in (result.get("outputs") or [])]
    # ⚠️ 产物要**按视角排好**（正面在前），不能信 JSON 里的顺序 ——
    # 拼图和回填都按"第一张是正面"这个约定走。
    outputs.sort(key=lambda p: _view_order(Path(p).stem, views, prefix))
    blend_path = Path(save_blend) if save_blend else None
    return {
        "outputs": outputs,
        "stats": result.get("stats") or {},
        "bbox": result.get("bbox") or {},
        "stage": result.get("stage"),
        "views": views,
        "name": prefix,
        "outdir": str(out_path),
        #: 顺便存的 .blend（**可编辑的模型资产**）。没要求存就是 ``None``。
        #: ⚠️ 只有文件真的在才算 —— 渲染器支持这个开关，但存盘失败时
        #: 它不会让整个渲染失败（进度里仍是 done），所以这里要自己核一遍。
        "blend": str(blend_path) if (blend_path is not None
                                     and blend_path.is_file()) else None,
        "log": log_text,
        "returncode": returncode,
        "timeout": timeout,
    }


def _view_order(stem: str, views: list[str], prefix: str) -> tuple[int, str]:
    """产物排序键：``前缀_<视角或取景>.png`` → 按 :data:`VIEWS` 的顺序。

    渲染器的命名规则（见 ``MMD渲染.py`` 末尾）：正面那张用**取景名**
    （``full``/``half``/``face``），其余用视角名。所以正面那张认不出来时
    就靠"不是 side/back/side2"来兜底 —— 排在它们前面。
    """
    tail = stem[len(prefix):].lstrip("_") if stem.startswith(prefix) else stem
    if tail in VIEWS:
        return (VIEWS.index(tail), stem)
    if tail not in ("side", "back", "side2"):
        return (-1, stem) # 取景名 = 正面
    return (len(VIEWS), stem)


def stitch_views(paths, out_path, *, prefix: str = "三视图",
                 blender=None, timeout: int = 240, cancel=None) -> dict:
    """把多张出图拼成一张三视图。

    跑在 **Blender 里**（它自带 PIL）—— 这样不用引 pip 依赖（NFR-16），
    也不用为了拼图再要一个系统 Python 环境。

    :return: ``{"ok": bool, "path": str, "message": str}`` —— **不抛异常**，
             拼图失败不该把已经渲好的图废掉。
    """
    files = [str(p) for p in paths if Path(p).is_file()]
    if not files:
        return {"ok": False, "path": "", "message": "没有可拼的图"}
    exe = str(blender or "").strip()
    if not exe or not Path(exe).is_file():
        return {"ok": False, "path": "", "message": "没选定 Blender，拼不了"}
    if not STITCH_SCRIPT.is_file():
        return {"ok": False, "path": "", "message": f"缺少拼图脚本：{STITCH_SCRIPT}"}

    if cancel is not None and cancel.is_set():
        return {"ok": False, "path": "", "message": "已取消"}

    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [exe, "--background", "--python", str(STITCH_SCRIPT),
               "--", str(prefix), str(target)] + files
    # R17：拼图也是 Blender 子进程 —— 关窗后必须能取消，不能留在后台跑。
    log_handle = tempfile.NamedTemporaryFile(
        "w+", encoding="utf-8", errors="replace", suffix=".log",
        prefix="komaflow_stitch_", delete=False)
    stitch_log = Path(log_handle.name)
    log_handle.close()
    log_tail = ""
    try:
        with open(stitch_log, "w", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, **(_no_window()))
            started = time.time()
            while True:
                code = process.poll()
                if code is not None:
                    break
                if cancel is not None and cancel.is_set():
                    process.kill()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    return {"ok": False, "path": "", "message": "已取消拼图"}
                if timeout and (time.time() - started) > timeout:
                    process.kill()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    return {"ok": False, "path": "",
                            "message": f"拼图超时（{timeout} 秒）"}
                time.sleep(0.2)
    except OSError as exc:
        return {"ok": False, "path": "", "message": f"拼不起来：{exc}"}
    finally:
        try:
            log_tail = stitch_log.read_text(
                encoding="utf-8", errors="replace").strip()[-300:]
        except OSError:
            log_tail = ""
        try:
            stitch_log.unlink()
        except OSError:
            pass

    if code != 0 or not target.is_file():
        return {"ok": False, "path": "", "message": f"拼图没成功。{log_tail}"}
    return {"ok": True, "path": str(target), "message": "已拼成一张三视图"}
