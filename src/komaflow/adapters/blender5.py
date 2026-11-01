"""Blender 5.x 适配器（当前唯一支持，也是统一基准版本）。

依据：设计取舍 / 32（只支持 5.x；4.2 LTS 暂不支持但预留位置）
与需求书「附录 F · Blender 5.2 API 适配清单」。

这个类存在的意义，就是把「5.x 和别的版本写法不一样」这件事**关在这里**。
核心逻辑层永远只看到 ``engine_eevee`` 这样的属性，看不到版本号。
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import tempfile
from pathlib import Path

from ..core.errors import ErrorKind, KomaFlowError
from .base import BlenderAdapter, BlenderInfo, VersionTuple

#: 探测超时（秒）。实测纯启动约 1.5 秒，给足余量。
PROBE_TIMEOUT = 120


def _extract_blender_install_script(source: Path) -> str:
    """从 ``install_mmd_tools.py`` 里取出给 Blender 执行的 ``SCRIPT`` 常量。

    ⚠️ 为什么需要拆：那个文件是**系统 Python 的包装器**（``main()`` 里再起
    Blender 子进程）。把它直接当 Blender 的 ``--python`` 脚本跑时，
    ``sys.argv[1]`` 是 ``--background``，包装器会去启动一个叫 ``--background``
    的程序 —— 插件安装必然失败（审查 R04）。

    这里只做静态取值（模块级 ``SCRIPT = '...'`` 字面量），不执行该文件。
    """
    tree = ast.parse(source.read_text(encoding="utf-8"), str(source))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "SCRIPT"
                for target in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and "bpy" in value and "RESULT " in value:
                return value
    raise ValueError("install_mmd_tools.py 里找不到可执行的 SCRIPT 常量")


class Blender5Adapter(BlenderAdapter):
    """Blender 5.x。"""

    key = "blender5"
    label = "Blender 5.x"
    supported_min: VersionTuple = (5, 0)
    # 上界卡在 5.x 之内：6.0 会被拒绝（R11 —— 等 mmd_tools 跟进后再说）
    supported_max: VersionTuple = (5, 99)
    is_baseline = True

    # ── 附录 F：5.2 的写法差异，全部变成属性 ──
    engine_eevee = "BLENDER_EEVEE" # 4.2 才是 BLENDER_EEVEE_NEXT
    needs_media_type_first = True # 先 media_type='VIDEO'，再 file_format='FFMPEG'
    material_transparency_attr = "surface_render_method" # blend_method 写入无效
    compositor_attr = "compositing_node_group" # scene.node_tree 已删

    def probe(self, exe: str | Path) -> BlenderInfo:
        """探测本机 Blender 与 mmd_tools（K-7.4 / K-7.6 / K-7.9）。

        做法：``--background --factory-startup --python probe.py -- <out.json>``，
        读回 JSON。**不解析屏幕输出**（K-10.3 同理）。

        ``--factory-startup`` 是刻意的：实测用户装的第三方插件会在退出时抛异常
        把退出码变成 1，而且可能干扰导入行为（设计取舍 / K-10.10）。
        """
        exe_path = Path(exe)
        if not exe_path.is_file():
            raise KomaFlowError(ErrorKind.BLENDER_MISSING, detail=f"找不到：{exe_path}")

        probe_script = self.script("probe.py")
        if not probe_script.is_file():
            raise KomaFlowError(
                ErrorKind.ASSET_CORRUPT, detail=f"缺少探针脚本：{probe_script}"
            )

        with tempfile.TemporaryDirectory(prefix="KomaFlow_probe_") as tmp:
            out_path = Path(tmp) / "probe.json"
            command = [
                str(exe_path), "--background", "--factory-startup",
                "--python", str(probe_script), "--", str(out_path),
            ]
            try:
                completed = subprocess.run(
                    command, capture_output=True, text=True,
                    timeout=PROBE_TIMEOUT, encoding="utf-8", errors="replace",
                    stdin=subprocess.DEVNULL, **_no_window(),
                )
            except subprocess.TimeoutExpired as exc:
                raise KomaFlowError(
                    ErrorKind.BLENDER_MISSING,
                    detail=f"探测超时（{PROBE_TIMEOUT}s）：{exe_path}",
                ) from exc

            if not out_path.is_file():
                raise KomaFlowError(
                    ErrorKind.BLENDER_MISSING,
                    detail=(f"探测失败（退出码 {completed.returncode}）："
                            f"{(completed.stderr or completed.stdout or '')[-300:]}"),
                )
            try:
                payload = json.loads(out_path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise KomaFlowError(
                    ErrorKind.BLENDER_MISSING, detail=f"探针输出无法解析：{exc}"
                ) from exc

        version = tuple(int(x) for x in payload.get("blender_version_tuple", [])[:3])
        return BlenderInfo(
            exe=exe_path,
            version_string=str(payload.get("blender_version", "?")),
            version=version,
            mmd_tools_installed=bool(payload.get("has_mmd_ops")),
            mmd_tools_version=payload.get("mmd_tools_version"),
        )

    def script(self, name: str) -> Path:
        """取 ``adapters/scripts/<name>`` 下的脚本。"""
        return Path(__file__).parent / "scripts" / name

    # ------------------------------------------------------------ 装插件
    def install_mmd_tools(self, exe: str | Path, *, timeout: int = 900) -> dict:
        """联网给**指定的这台** Blender 装 mmd_tools 扩展。

        用户定的路线：**不随包分发**，所以这一步必然需要网络。
        走 Blender 自己的扩展安装操作符（原因见
        ``adapters/scripts/install_mmd_tools.py`` 的模块说明）。

        :param exe: 要装到哪台 Blender（跟 :meth:`probe` 一样**收参数**，
                    因为适配器本身是无状态的）。
        :return: ``{"ok": bool, "message": str, ...}`` —— **不抛异常**，
                 这是用户点按钮之后的交互流程，失败也要能把话说完。
        """
        exe_path = Path(exe)
        if not exe_path.is_file():
            return {"ok": False, "message": f"找不到这个 Blender：{exe_path}"}

        wrapper = self.script("install_mmd_tools.py")
        if not wrapper.is_file():
            return {"ok": False, "message": f"缺少安装脚本：{wrapper}"}
        try:
            payload = _extract_blender_install_script(wrapper)
        except (OSError, SyntaxError, ValueError) as exc:
            return {"ok": False, "message": f"安装脚本内容不完整：{exc}"}

        # ⚠️ 这里**不能用 --factory-startup**：那会跳过用户的扩展仓库配置，
        # 而装插件正需要读它（官方仓库地址就存在用户偏好里）。
        # 这也是本方法跟 probe() 唯一不同的地方。
        #
        # 🚨 交给 Blender 的必须是**SCRIPT 本身**（临时 .py），不能是那个
        # 系统 Python 包装器 —— 否则 sys.argv[1]='--background' 会被当成
        # blender.exe 去启动（审查 R04，用户点"自动安装"必失败）。
        with tempfile.TemporaryDirectory(prefix="komaflow-install-") as tmp:
            script = Path(tmp) / "install_mmd_tools_in_blender.py"
            script.write_text(payload, encoding="utf-8")
            command = [str(exe_path), "--background", "--python-exit-code", "0",
                       "--python", str(script)]
            try:
                completed = subprocess.run(
                    command, capture_output=True, text=True, timeout=timeout,
                    encoding="utf-8", errors="replace",
                    stdin=subprocess.DEVNULL, **_no_window(),
                )
            except subprocess.TimeoutExpired:
                return {"ok": False,
                        "message": f"安装超时（{timeout} 秒）—— 可能网络太慢或不通。"}
            except OSError as exc:
                return {"ok": False, "message": f"起不来 Blender：{exc}"}

        for row in (completed.stdout or "").splitlines():
            if row.startswith("RESULT "):
                try:
                    return json.loads(row[len("RESULT "):])
                except ValueError:
                    break
        tail = (completed.stderr or completed.stdout or "").strip()[-400:]
        return {"ok": False,
                "message": f"安装没成功（退出码 {completed.returncode}）。{tail}"}


def _no_window() -> dict:
    """Windows 下别弹出黑窗口。"""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }

def probe_movie_with_blender(exe: str | Path, video: str | Path) -> dict:
    """用 Blender 读视频信息（替代 ffprobe）。

    返回 ``{"frames": int, "fps": float, "duration": float, "width": int,
    "height": int}``。Blender 读不动时抛 :class:`KomaFlowError`。
    """
    script = Path(__file__).resolve().parent / "scripts" / "probe_movie.py"
    command = [
        str(exe), "--background", "--factory-startup",
        "--python", str(script), "--", str(video),
    ]
    proc = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300, **_no_window(),
    )
    for line in (proc.stdout or "").splitlines():
        if line.startswith("PROBE_OK "):
            payload = json.loads(line[len("PROBE_OK "):])
            return payload
    raise KomaFlowError(
        ErrorKind.ASSET_CORRUPT,
        detail=f"Blender 读不出这个视频：{Path(video).name}",
    )
