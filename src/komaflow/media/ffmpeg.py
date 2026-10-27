"""FFmpeg 封装：视频探测与流拷贝拼接。

对应需求：K-2.10（各段同参数，最后**流拷贝拼接**，不重编码）·
FR-8.4（成片完整性校验：帧数 / 时长 / 可播放）· NFR-17（随包附带 FFmpeg）。

为什么用 FFmpeg 而不是 Blender 来探测
------------------------------------
Blender 侧也能用 ``bpy.data.movieclips.load()`` 探测，但那要多起一个 Blender
进程（每次约 1.5 秒）。``ffprobe`` 是毫秒级的，且我们本来就要用 FFmpeg 做拼接。

**只依赖命令行**，不引入任何 Python 包（NFR-16）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import sys
import tempfile
from pathlib import Path

from ..core.errors import ErrorKind, KomaFlowError
from ..core.verify import VideoInfo

#: 允许用环境变量指定 FFmpeg 位置（随包分发时指向程序自带的那份）
ENV_FFMPEG = "KomaFlow_FFMPEG"

#: 单条命令的超时（秒）。拼接与探测都很快，超过就是出问题了。
DEFAULT_TIMEOUT = 180

#: A7：编码的超时必须**按规模算**，不能恒 180 秒。
#:
#: 实测 ProRes 4:4:4 2K 是 67 ms/帧 —— 180 秒只够 2668 帧；
#: 慢 3~5 倍的机器上 500~900 帧就撞顶，然后被当成"编码失败"。
ENCODE_SECONDS_PER_FRAME = 2.0      # 每帧预留 2 秒（慢机也够）
PROBE_TIMEOUT = 120.0               # ffprobe 只读头部/数帧，不需要很久


def encode_timeout(frame_count: int | None) -> float:
    """按帧数算编码超时（下限 600 秒）。"""
    frames = max(int(frame_count or 0), 0)
    return max(600.0, frames * ENCODE_SECONDS_PER_FRAME)


def concat_timeout(part_count: int | None) -> float:
    """流拷贝拼接：与段数相关，跟体积无关（没有重编码）。"""
    parts = max(int(part_count or 0), 1)
    return max(300.0, parts * 120.0)


def _no_window() -> dict:
    """Windows 下别弹黑窗口。

    用户明确要求："执行时候后台不要弹出命令窗口"。subprocess 默认会为新进程
    开一个控制台，调 ffmpeg/ffprobe 时就会一闪一闪，很掉价。
    """
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def _resolve(name: str, explicit: str | Path | None = None) -> Path:
    """按 显式指定 → 环境变量 → PATH 的顺序找一个可执行文件。"""
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return candidate
        raise KomaFlowError(ErrorKind.ASSET_CORRUPT, detail=f"指定的 {name} 不存在：{candidate}")

    env_dir = os.environ.get(ENV_FFMPEG)
    if env_dir:
        suffix = ".exe" if os.name == "nt" else ""
        candidate = Path(env_dir)
        candidate = (candidate / f"{name}{suffix}") if candidate.is_dir() else candidate
        if candidate.is_file():
            return candidate

    found = shutil.which(name)
    if found:
        return Path(found)

    # 常见安装位置：WinGet / Scoop / Chocolatey / Program Files / 程序自带
    suffix = ".exe" if os.name == "nt" else ""
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[3]
    guesses: list[Path] = [
        exe_dir / f"{name}{suffix}",
        exe_dir / "ffmpeg" / f"{name}{suffix}",
        exe_dir / "ffmpeg" / "bin" / f"{name}{suffix}",
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        # 程序自动下载的 FFmpeg 放这里
        guesses.append(Path(local) / "KomaFlow" / "ffmpeg" / f"{name}{suffix}")
        guesses.append(Path(local) / "Microsoft" / "WinGet" / "Links" / f"{name}{suffix}")
        winget_pkgs = Path(local) / "Microsoft" / "WinGet" / "Packages"
        if winget_pkgs.is_dir():
            for pkg in winget_pkgs.glob("Gyan.FFmpeg*"):
                guesses.extend(pkg.glob(f"**/bin/{name}{suffix}"))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("ProgramData")):
        if base:
            guesses.append(Path(base) / "ffmpeg" / "bin" / f"{name}{suffix}")
            guesses.append(Path(base) / "chocolatey" / "bin" / f"{name}{suffix}")
    home = os.environ.get("USERPROFILE")
    if home:
        guesses.append(Path(home) / "scoop" / "shims" / f"{name}{suffix}")
    for guess in guesses:
        try:
            if guess.is_file():
                return guess
        except OSError:
            continue

    raise KomaFlowError(
        ErrorKind.ASSET_CORRUPT,
        detail=f"找不到 {name}。请把 FFmpeg 放进程序目录，或用环境变量 {ENV_FFMPEG} 指定位置。",
    )


def find_ffmpeg(ffmpeg: str | Path | None = None) -> tuple[Path, Path]:
    """返回 ``(ffmpeg, ffprobe)`` 两个可执行文件路径。"""
    ffmpeg_path = _resolve("ffmpeg", ffmpeg)
    # ffprobe 通常与 ffmpeg 同目录
    probe_guess = ffmpeg_path.with_name(
        "ffprobe.exe" if ffmpeg_path.suffix.lower() == ".exe" else "ffprobe"
    )
    ffprobe_path = probe_guess if probe_guess.is_file() else _resolve("ffprobe")
    return ffmpeg_path, ffprobe_path


def probe_video(path: str | Path, ffprobe: str | Path | None = None,
                *, cancel=None) -> VideoInfo:
    """探测视频的帧数、时长与帧率（FR-8.4 的判定依据）。

    :raises KomaFlowError: 读不出来（文件损坏或不是视频）
    """
    target = Path(path)
    if not target.is_file():
        raise KomaFlowError(ErrorKind.ASSET_CORRUPT, detail=f"产物不存在：{target}")

    _, ffprobe_path = find_ffmpeg(ffprobe)
    command = [
        str(ffprobe_path), "-v", "error",
        "-select_streams", "v:0",
        "-count_frames",
        "-show_entries", "stream=nb_read_frames,r_frame_rate,duration,width,height",
        "-of", "json",
        str(target),
    ]
    code, stdout, stderr, cancelled = _spawn(
        command, what="探测", timeout=PROBE_TIMEOUT, cancel=cancel)
    if cancelled:
        raise KomaFlowError(ErrorKind.USER_CANCELLED)
    if code != 0:
        raise KomaFlowError(
            ErrorKind.ASSET_CORRUPT,
            detail=f"ffprobe 返回 {code}：{stderr.strip()[:200]}",
        )

    try:
        payload = json.loads(stdout)
        stream = payload["streams"][0]
    except (ValueError, KeyError, IndexError) as exc:
        raise KomaFlowError(ErrorKind.ASSET_CORRUPT, detail=f"无法解析探测结果：{target}") from exc

    frame_count = stream.get("nb_read_frames")
    if frame_count in (None, "N/A"):
        frame_count = stream.get("nb_frames")
    fps = _parse_rate(stream.get("r_frame_rate", "0/0"))
    duration = stream.get("duration")
    if duration in (None, "N/A") and fps:
        duration = (int(frame_count) / fps) if frame_count else None

    return VideoInfo(
        frame_count=int(frame_count or 0),
        duration_seconds=float(duration or 0.0),
        fps=fps,
    )


def _spawn(command: list[str], *, what: str, timeout: float | None = None,
           cancel=None) -> tuple[int, str, str, bool]:
    """跑一条外部命令，**可以取消**，超时按调用方给的秒数。

    为什么不用 ``subprocess.run(timeout=...)``：
    它一堵到底 —— 用户在这期间点"终止"完全没反应（编码/拼接能跑几十秒到几分钟）。
    这里改成 ``Popen`` + 轮询，每 0.2 秒看一眼取消标志。

    返回 ``(returncode, stdout, stderr, cancelled)``。超时会直接抛。
    """
    # ⚠️ 不要用 PIPE：这里是在循环里 poll，没人读管道 ——
    # 一旦子进程输出超过管道缓冲（Windows 约 64 KB）就会互相卡死。
    # 写临时文件，进程结束后一次性读回来。
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                errors="replace") as out_file, \
            tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                   errors="replace") as err_file:
        process = subprocess.Popen(
            command, stdout=out_file, stderr=err_file,
            text=True, encoding="utf-8", errors="replace", **_no_window())
        started = time.time()

        def _collect() -> tuple[str, str]:
            out_file.seek(0)
            err_file.seek(0)
            return out_file.read(), err_file.read()

        while True:
            code = process.poll()
            if code is not None:
                out, err = _collect()
                return code, out, err, False
            if cancel is not None and cancel.is_set():
                _kill_tree(process)
                process.wait(timeout=10)
                out, err = _collect()
                return -1, out, err, True
            if timeout is not None and (time.time() - started) > timeout:
                _kill_tree(process)
                process.wait(timeout=10)
                _collect()
                raise KomaFlowError(
                    ErrorKind.ASSET_CORRUPT,
                    detail=f"{what}超时（超过 {int(timeout)} 秒）")
            time.sleep(0.2)


def _kill_tree(process: subprocess.Popen) -> None:
    """连子进程一起收（Windows 上 ffmpeg 会 fork 出编码线程/子进程）。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, check=False)
        else:
            process.kill()
    except Exception: # noqa: BLE001 - 收尾阶段不抛
        pass


def _parse_rate(rate: str) -> float:
    """把 ffprobe 的 ``"30/1"`` 解析成 30.0。"""
    try:
        if "/" in rate:
            numerator, denominator = rate.split("/", 1)
            denominator_value = float(denominator)
            return float(numerator) / denominator_value if denominator_value else 0.0
        return float(rate)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _run(command: list[str], *, what: str, timeout: float = DEFAULT_TIMEOUT,
         cancel=None) -> None:
    """跑一条 ffmpeg 命令，非零退出就报人话错。

    ffmpeg 的报错信息在 stderr 最后几行，截 300 字够定位了（完整的看日志文件）。
    """
    code, _out, stderr, cancelled = _spawn(
        command, what=what, timeout=timeout, cancel=cancel)
    if cancelled:
        raise KomaFlowError(ErrorKind.USER_CANCELLED)
    if code != 0:
        raise KomaFlowError(
            ErrorKind.ASSET_CORRUPT,
            detail=f"{what}失败（{code}）："
                   f"{stderr.strip()[-300:]}",
        )


def _count_png_frames(directory: Path, prefix: str) -> int:
    """数一下 PNG 序列有多少张（只为算超时，读目录即可）。"""
    try:
        return sum(1 for p in directory.glob("*.png")
                   if not prefix or p.name.startswith(prefix))
    except OSError:
        return 0


def _frame_pattern(directory: Path, prefix: str) -> str:
    """PNG 序列喂给 ffmpeg 的输入模板。

    ⚠️ 帧号是跟在**整个前缀**后面的 —— 而那个前缀本身就以 ``.png`` 结尾
    （段名形如 ``seg_0000_000000-000005.png``），所以真实文件名是::

        seg_0000_000000-000005.png0000.png

    模板必须照这个来。早先写成 ``{prefix}%04d.png``（默认前缀是
    "去掉 ``.png`` 的段名"），跟实际落盘名字对不上 ——
    ffmpeg 找不到输入帧，编码静默失败、**成片一个都没有**
    （表现：``xxx_parts/`` 目录是空的，日志里却看不出错）。

    ⚠️ **调用方直接给 :meth:`core.verify.PngFrames.inputs` 更好。**
       这个函数留着是给"已经拿到目录 + 前缀"的调用方用的；
       谁要是自己拼前缀，就会重演上面那个 bug。
    """
    return str(Path(directory) / f"{prefix}%04d.png")


def encode_mov_alpha(
    directory: str | Path,
    output: str | Path,
    *,
    prefix: str = "",
    fps: int = 30,
    codec: str = "prores",
    ffmpeg: str | Path | None = None,
    cancel=None,
    timeout: float | None = None,
) -> Path:
    """把 PNG 序列编成**带 alpha 通道的 MOV**（主交付）。

    ``codec``：

    * ``prores`` —— ProRes 4444（``prores_ks`` + ``-profile:v 4444``）。
      带 alpha、体积只有无损 RLE 的三分之一，AE / 达芬奇 / 剪辑软件都认。
    * ``qtrle`` —— QuickTime 无损 RLE（行程编码）。体积最大但绝对无损。

    ⚠️ 像素格式必须是 ``yuva444p10le``（带 a = alpha）。写成 ``yuv444p``
    出来就是**没有透明通道**的黑底 MOV，而且不会报错 —— 静默出错的典型。
    """
    ffmpeg_path, _ = find_ffmpeg(ffmpeg)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    source = _frame_pattern(Path(directory), prefix)

    command = [str(ffmpeg_path), "-y", "-v", "error",
               "-framerate", str(int(fps)), "-i", source]
    if codec == "qtrle":
        command += ["-c:v", "qtrle", "-pix_fmt", "argb"]
    else:
        command += ["-c:v", "prores_ks", "-profile:v", "4444",
                    "-pix_fmt", "yuva444p10le"]
    command.append(str(target))

    # A7：超时按帧数算，不再恒 180 秒
    frames = _count_png_frames(Path(directory), prefix)
    _run(command, what="透明 MOV 编码",
         timeout=timeout or encode_timeout(frames), cancel=cancel)
    return target


def concat_streams(
    parts: list[str | Path],
    output: str | Path,
    ffmpeg: str | Path | None = None,
    cancel=None,
) -> Path:
    """流拷贝拼接（不重编码），容器由输出扩展名决定。

    跟 :func:`concat_segments` 是同一件事，只是不再限定 MP4 ——
    透明那条路拼的是 MOV（ProRes 4444），**绝不能重编码**：
    ProRes 4444 本身已经够大，再解一遍纯属浪费，而且会掉 alpha 精度。

    前提不变：各段必须是**完全相同的编码参数**（同一份工作工程、同一套设置，
    只有帧范围不同）。
    """
    if not parts:
        raise KomaFlowError(ErrorKind.ASSET_CORRUPT, detail="没有可拼接的分段")

    ffmpeg_path, _ = find_ffmpeg(ffmpeg)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8", newline="\n"
    ) as handle:
        list_path = Path(handle.name)
        for part in parts:
            safe = str(Path(part).resolve()).replace("'", "'\\''")
            handle.write(f"file '{safe}'\n")

    try:
        command = [
            str(ffmpeg_path), "-y", "-v", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            str(target),
        ]
        _run(command, what="分段拼接", cancel=cancel)
    finally:
        list_path.unlink(missing_ok=True)

    return target


def concat_segments(
    parts: list[str | Path],
    output: str | Path,
    ffmpeg: str | Path | None = None,
    cancel=None,
) -> Path:
    """把各段**流拷贝**拼成一个成片（K-2.10：不重编码）。

    前提是各段使用**完全相同的编码参数** —— 这正是我们分段时保证的
    （同一份工作工程、同一套输出设置，只有帧范围不同）。
    """
    if not parts:
        raise KomaFlowError(ErrorKind.ASSET_CORRUPT, detail="没有可拼接的段")

    ffmpeg_path, _ = find_ffmpeg(ffmpeg)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)

    # concat demuxer 的清单文件。路径里的单引号要转义。
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8", newline="\n"
    ) as handle:
        list_path = Path(handle.name)
        for part in parts:
            safe = str(Path(part).resolve()).replace("'", "'\\''")
            handle.write(f"file '{safe}'\n")

    try:
        command = [
            str(ffmpeg_path), "-y", "-v", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            str(target),
        ]
        code, _out, stderr, cancelled = _spawn(
            command, what="拼接", timeout=concat_timeout(len(parts)), cancel=cancel)
        if cancelled:
            raise KomaFlowError(ErrorKind.USER_CANCELLED)
        if code != 0:
            raise KomaFlowError(
                ErrorKind.ASSET_CORRUPT,
                detail=f"拼接失败（{code}）：{stderr.strip()[:300]}",
            )
    finally:
        list_path.unlink(missing_ok=True)

    return target
