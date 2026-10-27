"""Blender 子进程执行器。

对应需求：K-10.1~K-10.5（无界面调用 / 只经命令行与文件交互 / 进度以文件为准 /
子进程监控）· K-10.9（一个进程渲完一整段）· （超时与心跳）。

约定（**核心层与 Blender 之间唯一的交互方式**）：

    blender.exe --background --factory-startup --python <脚本> -- <spec.json>

* 脚本从 ``spec.json`` 读参数，把结果写回 ``spec["report"]`` 指向的 JSON
* 渲染脚本还会**周期性覆写** ``spec["progress"]`` 指向的 JSON（进度以文件为准）
* 全程**不向 Blender 注入或修改代码**，也不碰它的配置（K-10.2）

``--factory-startup`` 的意义：实测用户装的第三方插件（``AnimCharTools``、
``MBTs-*``、``MikuMikuImport_Pro``、``blender_mcp_addon`` 等）会在退出时抛异常，
让退出码变成 1；隔离启动可以避开它们（设计取舍 / K-10.10）。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..core.errors import ErrorKind, KomaFlowError

#: 轮询间隔（秒）—— 足够灵敏，又不至于把 CPU 烧掉
POLL_INTERVAL = 0.4

#: 判定"子进程无响应"的秒数：进度文件超过这么久没更新就认为卡死
STALL_SECONDS = 180.0


@dataclass
class ScriptOutcome:
    """一次脚本执行的结果。"""

    returncode: int
    report: dict | None = None
    duration: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    stalled: bool = False
    #: 被判"卡死"时的一句话原因（写进日志/弹窗，别让用户只看到"超时"）
    stall_reason: str = ""
    log_path: Path | None = None

    @property
    def ok(self) -> bool:
        """**成功与否只看产物与报告，不看退出码**（设计取舍 / FR-8.10）。"""
        return bool(self.report and self.report.get("ok"))


def _no_window_kwargs() -> dict:
    """Windows 下别弹出黑窗口。"""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def run_blender_script(
    exe: str | Path,
    script: str | Path,
    spec: dict,
    spec_path: str | Path,
    *,
    log_path: str | Path | None = None,
    timeout: float | None = None,
    progress_path: str | Path | None = None,
    on_progress: Callable[[dict], None] | None = None,
    cancel=None,
    control=None,
    poll_interval: float = POLL_INTERVAL,
) -> ScriptOutcome:
    """跑一个 Blender 侧脚本，并在运行期间监视进度文件。

    :param cancel: 任何带 ``is_set()`` 的对象（如 ``threading.Event``）
    :param control: 可选的 :class:`~komaflow.adapters.proc.ProcessControl`，
        用来挂起 / 恢复渲染进程（"暂停"）
    :param on_progress: 每次进度文件更新时回调，参数是解析后的 dict
    """
    exe = Path(exe)
    script = Path(script)
    spec_path = Path(spec_path)
    if not exe.is_file():
        raise KomaFlowError(ErrorKind.BLENDER_MISSING, detail=f"找不到：{exe}")
    if not script.is_file():
        raise KomaFlowError(ErrorKind.ASSET_CORRUPT, detail=f"找不到脚本：{script}")

    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

    # 🚨 起进程前清掉**上一轮**的产物。报告与进度文件都是固定名、全工程共用：
    # 本轮若在写报告之前崩掉，不删的话结尾会把上一轮的报告（连着它的
    # ok=True、上一轮的覆盖率）当成这一轮的结果读回来——实测工单：
    # 用户换动作后渲出旧场景，日志却显示上一轮的"骨骼匹配 22%"。
    for stale_artifact in (spec.get("report"), progress_path):
        if stale_artifact:
            try:
                Path(stale_artifact).unlink()
            except OSError:
                pass  # 删不掉（被占用等）也照跑，下面还有新鲜度校验兜底

    command = [
        str(exe), "--background", "--factory-startup",
        "--python", str(script),
        "--", str(spec_path),
    ]

    if log_path is None:
        log_handle = None
        stdout_target = subprocess.DEVNULL
    else:
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("w", encoding="utf-8", errors="replace")
        stdout_target = log_handle

    progress_file = Path(progress_path) if progress_path else None
    last_progress_stamp: float | None = None
    last_progress_change = time.time()
    # 🚨 首次进度之前的宽限。
    #
    # 原来的看门狗条件是 ``last_progress_stamp is not None`` ——
    # 也就是"进度文件**出现过一次**之后才开始盯"。可装配最前面的
    # 启用 mmd_tools、导入 PMX、打开 .blend 全程都不写进度，
    # 一旦卡在那里，看门狗形同虚设，界面就永远挂着。
    # 现在从进程启动就开始计时，只是首次进度前给 3 倍宽限
    # （大模型导入 + 慢盘确实可能慢，但不会慢到 9 分钟）。
    first_progress_grace = STALL_SECONDS * 3

    started = time.time()
    outcome = ScriptOutcome(returncode=-1, log_path=log_path)

    try:
        process = subprocess.Popen(
            command,
            stdout=stdout_target,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **_no_window_kwargs(),
        )
        if control is not None:
            control.register(process.pid)
        try:
            while True:
                code = process.poll()
                if code is not None:
                    outcome.returncode = code
                    break

                now = time.time()

                if cancel is not None and cancel.is_set():
                    outcome.cancelled = True
                    _terminate(process)
                    break

                if timeout is not None and (now - started) > timeout:
                    outcome.timed_out = True
                    _terminate(process)
                    break

                # 落实"暂停"：挂起 / 恢复渲染进程
                if control is not None:
                    control.apply()
                    if control.paused.is_set():
                        # 挂起期间进度当然不动 —— 不能算作"无响应"
                        last_progress_change = now
                        time.sleep(poll_interval)
                        continue

                if progress_file is not None and progress_file.is_file():
                    try:
                        stamp = progress_file.stat().st_mtime
                    except OSError:
                        stamp = last_progress_stamp
                    if stamp != last_progress_stamp:
                        last_progress_stamp = stamp
                        last_progress_change = now
                        if on_progress is not None:
                            payload = _read_json(progress_file)
                            if payload:
                                on_progress(payload)

                # 心跳：长时间没有进度就认为无响应（含"从没出现过进度"这种情况）
                if progress_file is not None:
                    limit = (STALL_SECONDS if last_progress_stamp is not None
                             else first_progress_grace)
                    if (now - last_progress_change) > limit:
                        outcome.stalled = True
                        outcome.stall_reason = (
                            "Blender 一直没有给出进度"
                            if last_progress_stamp is None
                            else f"进度停了超过 {int(limit)} 秒")
                        _terminate(process)
                        break

                time.sleep(poll_interval)
        finally:
            if control is not None:
                control.clear()
            if process.poll() is None:
                _terminate(process)
    finally:
        if log_handle is not None:
            log_handle.close()

    outcome.duration = time.time() - started

    report_path = spec.get("report")
    if report_path:
        outcome.report = _read_report_if_fresh(Path(report_path), started)
    return outcome


def _read_report_if_fresh(path: Path, started: float, *, grace: float = 2.0) -> dict | None:
    """只采信**本轮**写出的报告，陈旧/缺失一律按"本轮没有报告"处理。

    报告文件是固定名、跨轮共用：本轮若在写报告之前硬崩（segfault/OOM
    这类 Python 接不住的死法），不校验时间就会把上一轮的报告连着它的
    ``ok=True`` 一起继承。以进程启动时刻为界，早于它的报告视为陈旧；
    ``grace`` 容忍文件系统时间精度与浮点误差。
    """
    try:
        if not path.is_file() or path.stat().st_mtime < started - grace:
            return None
    except OSError:
        return None
    return _read_json(path)


def _terminate(process: subprocess.Popen) -> None:
    """先礼后兵：terminate，5 秒还不走就 kill，**并且要连子进程一起收**。

    Blender 的 ``--background`` 本身一般不起子进程，但它会拉 ffmpeg / 解压线程，
    而且用户点"终止"时只杀父进程会留下孤儿（Windows 上尤其常见）。
    ``taskkill /T`` 是按进程树收，比 ``kill()`` 干净。
    """
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T"],
                capture_output=True, check=False)
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, check=False)
            process.kill()
            process.wait(timeout=5)
    except Exception: # noqa: BLE001 - 收尾阶段不再抛错
        pass


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
