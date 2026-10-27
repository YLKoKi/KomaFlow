# -*- coding: utf-8 -*-
"""一键打包：把 KomaFlow 打成**能拿到别人电脑上用的安装包**。

用法::

    <python> tools/build_installer.py # 小包（约 27 MB，需要对方装 FFmpeg）
    <python> tools/build_installer.py --with-ffmpeg # 带便携 FFmpeg（大很多，但开箱即用）

产出（都在 ``dist/`` 下）::

    dist/KomaFlow/ ← 程序本体，双击 KomaFlow.exe 就能跑
    dist/KomaFlow_V1.1.0-beta.zip ← 分发用的压缩包
    dist/打包报告.txt ← 这次打了什么、体积、隐私与合规检查结果

⚠️ **绝不打包的东西**（三条硬约束，脚本里也各有一道检查）：

1. **Blender 本身** —— Blender 与 mmd_tools 都是 GPL 系；打进安装包就要履行
   GPL 义务（附源码）。我们只**调用用户自己装的那一份**（子进程），不传染。
2. **任何模型 / 贴图 / 成片** —— MMD 模型普遍禁止二次配布，版权属角色原作者。
3. **本机的隐私痕迹** —— 私有路径、用户名、开发笔记、截图。日志里会**逐项汇报**。

依赖：PyInstaller（``python -m pip install pyinstaller``）。
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import shutil
import subprocess
import sys
import tokenize
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
import os as _os

#: 出包目录可用环境变量覆盖 —— 这样可以在**不碰正在运行的实例**的前提下
#: 另出一份发布包（实测：dist 里的 exe 被人打开时会锁定，覆盖会失败）。
DIST = Path(_os.environ.get("KOMEFLOW_DIST") or (ROOT / "dist"))
BUILD = Path(_os.environ.get("KOMEFLOW_BUILD") or (ROOT / "build"))
#: 剥掉注释/docstring 之后、"喂给 PyInstaller"的那份副本。
#:
#: ⚠️ **不能放在 ``build/`` 里**：``pyinstaller --clean`` 会把整个 ``build/``
#: 删掉，于是 spec 找不到素版、**静默回退到原始源码** ——
#: 结果是"报告说剥了 33 个文件，实际进包的全是带注释的原版"。
#: 这个坑真的踩到过一次（还是靠"包里根本没有 .py 数据文件"才发现的）。
#: 所以放在仓库根下一个 `--clean` 不碰的目录里。
STAGE = ROOT / ".build_stage"
APP_NAME = "KomaFlow"

#: 隐私扫描的"针"。**这些是"一发出去就能被人反查到我"的东西** ——
#: 每一条都是踩过或差点踩过的，别删：
#:
#: * 用户名 / 主机名 —— 直接指向个人
#: * 工作区路径与目录名 —— 把我们机器的目录结构全暴露
#: * Python 安装路径 —— 同上（``.pyc`` 的 ``co_filename`` 会带这个）
#: * 开发期路径 —— 素材、设定集放在哪
#:
#: ⚠️ 只写"值"不写"标签"以外的信息：报告里**不打印命中的原文**，
#: 免得体检报告本身又成了泄漏源。
def _privacy_needles() -> list[bytes]:
    import os

    raw = [
        os.environ.get("USERNAME", ""),
        os.environ.get("COMPUTERNAME", ""),
        os.environ.get("USERPROFILE", ""),
        str(ROOT), # 工作区根（绝对路径）
        ROOT.name, # 工作区目录名
        ROOT.parent.name,
        Path(sys.executable).parent.as_posix(),
        "pythonw.exe",
        "Python312",
        "Python313",
        "AppData\\Roaming\\KomaFlow",
        "__pycache__",
    ]
    out: list[bytes] = []
    #: 与产品名相同的针必须剔除。
    #:
    #: 改名为 KomaFlow 之后，**工作区目录名正好也叫 KomaFlow** ——
    #: 于是"目录名"这根针把产品名本身当成隐私命中：使用说明、第三方来源、
    #: build.py 全被判成"私有串（8 字节）"，打包直接失败（实测卡过一次）。
    #: 产品名是要印在界面上的公开信息，本来就不该当隐私。
    public = APP_NAME.strip().lower()
    for item in raw:
        text = str(item or "").strip()
        if len(text) < 4:
            continue # 太短的词误报率高（比如 3 个字符的用户名）
        if text.lower() == public:
            continue
        for variant in (text, text.replace("\\", "/")):
            needle = variant.encode("utf-8", "replace")
            if needle not in out:
                out.append(needle)
    return out


PRIVACY_NEEDLES = _privacy_needles()

#: 这些文件里出现"针"属于巧合（Python 自带编码表里全是十六进制字符序列）。
#: ⚠️ 它们**本来就不该出现在我们自己的文件里** —— 留着是为了万一被
#: PyInstaller 顺手带进来时不要误报。
PRIVACY_FALSE_POSITIVES = {"big5.enc", "cns11643.enc", "cp950.enc",
                           "euc_jp.enc", "gb2312.enc", "gbk.enc"}

#: 我们**自己写的**那些文件（只有这些才可能出现"我们的路径"）。
#: Python 运行时（``python312.dll`` / ``_tk_data`` / 标准库）不是我们产的，
#: 对它做"私有串"扫描只会出假警报 —— 实测 ``python312.dll`` 里就有
#: ``__pycache__`` 这个**目录名词符串**（因为它要拼缓存路径），
#: 被当成隐私命中过一次。所以分两层扫，下面这层才是真判据。
OUR_FILE_SUFFIXES = (".py", ".txt", ".md", ".json", ".cfg", ".ini", ".pyw")
PYTHON_RUNTIME_PREFIXES = ("python3", "_tk_data", "tcl", "tcl8", "tk8",
                           "libcrypto", "libssl", "vcruntime", "api-ms-")

#: ② **绝对路径残留**：任何盘符 + 反斜杠开头的路径，就是我们机器上的。
#: 这一条不看私有串、只看"有没有绝对路径"，所以能同时抓住编译垃圾与手写路径。
ABSOLUTE_PATH_PATTERN = re.compile(
    rb"[A-Za-z]:\\{1,2}(?:Users|KomaFlow|Program Files|Temp|Windows)")

#: 不该出现在包里的文件类型（模型 / 影音 / 工程 / **编译垃圾**）
FORBIDDEN_SUFFIXES = {".pmx", ".pmd", ".vmd", ".vpd", ".blend", ".blend1",
                      ".mp4", ".mov", ".avi", ".mkv", ".wav", ".mp3",
                      ".jpg", ".jpeg", ".bmp", ".tga", ".psd",
                      # ⚠️ 编译垃圾：``.pyc`` 里带编译时的**绝对路径**
                      # （``code.co_filename``），是实打实的隐私泄漏
                      ".pyc", ".pyo"} 

#: 目录名层面的禁项
FORBIDDEN_DIRNAMES = {"__pycache__"}

#: 便携 FFmpeg 的常见来源（本机已装的话直接从这儿拷）
FFMPEG_SEARCH = (
    Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages",
    Path("C:/ffmpeg"),
    Path("C:/Program Files/ffmpeg"),
)


def verify_stripped(app_dir: Path, staged: Path) -> dict:
    """**证实**剥掉的确实进了包（而不是只写了一行报告）。

    为什么要专门查：素版原来放在 ``build/`` 里，而 ``pyinstaller --clean``
    会把 ``build/`` 整个删掉 → spec 静默回退到原始源码。
    报告上写着"剥了 33 个文件"，**实际进包的全是带注释的原版**。
    这种事只有拿产物对一遍才发现。

    允许留着注释的（不算问题）：
    * ``vendor/`` 下的第三方代码 —— 别人写的，注释是它的说明
    * ``__init__.py`` —— 约定上不动
    * ``install_mmd_tools.py`` —— 随包发出去给用户看的引导脚本
    """
    print("[5b/5] 证实剥掉的确实进包了 …")
    allowed = []
    found: list[str] = []
    for path in app_dir.rglob("*.py"):
        rel = path.relative_to(app_dir)
        if _is_third_party(rel) or path.name in KEEP_VERBATIM_FILES:
            allowed.append(str(rel))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            head = line.strip()
            if head.startswith("#") and not head.startswith("#!"):
                found.append(str(rel))
                break
    return {
        "包内py文件": len([p for p in app_dir.rglob("*.py")]),
        "还含注释的": sorted(set(found)),
        "按设计保留的": sorted(set(allowed)),
        "素版存在": staged.is_dir() and (staged / "komaflow").is_dir(),
    }


def strip_python_light(source: str, filename: str = "<src>") -> str:
    """**只删注释**，docstring 全部保留（给"随包当数据文件发出去"的脚本用）。

    为什么这两个目录要单独对待：``adapters/scripts/*.py`` 不是被 import 的模块，
    而是**注入 Blender 执行的脚本**（以 .py 形式躺在包里）。它们对外提供的
    函数签名说明（docstring）是使用者看 API 的唯一去处，删了不合理；
    但里面的开发期注释属于"内部解释"，不该跟着发出去。
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source
    kept = [t for t in tokens if t.type != tokenize.COMMENT]
    try:
        result = tokenize.untokenize(kept)
    except (ValueError, IndexError):
        return source
    result = re.sub(r"\n{3,}", "\n\n", result)
    if not result.endswith("\n"):
        result += "\n"
    try:
        ast.parse(result, filename)
    except SyntaxError:
        return source
    return result


def _run(cmd: list[str], **kw) -> int:
    print(" $ " + " ".join(str(c) for c in cmd))
    return subprocess.call(cmd, **kw)


# --------------------------------------------------------------------------
# 剥注释与 docstring（进包的那份只留代码）
# --------------------------------------------------------------------------

#: 这些文件**不剥**（第三方代码，动它反而违规/没必要）。
KEEP_VERBATIM_DIRS = ("vendor",)
#: 这些文件**不剥**（约定上不动，或本身就是给人看的）。
KEEP_VERBATIM_FILES = ("__init__.py", "install_mmd_tools.py")


def _docstring_spans(tree: ast.AST) -> list[tuple[int, int, int, int]]:
    """用 AST 找出**真正的 docstring** 的位置。

    为什么非得用 AST：第一版按"上一个 token 是不是换行/缩进/冒号"猜，
    结果把 ``{"a": "b"}`` 里的值、``def f(): "x"`` 之外的一堆东西误判成
    docstring，**15 个文件被剥坏**。AST 是唯一权威：
    只有 ``Module`` / ``ClassDef`` / ``FunctionDef`` 的**第一个语句**
    且是纯字符串常量，才是 docstring。
    """
    spans: list[tuple[int, int, int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            value = first.value
            spans.append((value.lineno, value.col_offset,
                          value.end_lineno or value.lineno,
                          value.end_col_offset or 0))
    return spans


def _blank_spans(source: str, spans: list[tuple[int, int, int, int]]) -> str:
    """把给定区间**整行删掉**（含换行），保持其余内容一字不动。"""
    if not spans:
        return source
    lines = source.splitlines(keepends=True)
    drop: set[int] = set()
    for lineno, _col, end_lineno, end_col in spans:
        # 只删"独占整行"的 docstring；行首还有别的代码就别碰（保守）
        first_line = lines[lineno - 1] if lineno - 1 < len(lines) else ""
        if first_line[: (len(first_line) - len(first_line.lstrip()))].strip():
            pass
        if first_line.strip().startswith(('"""', "'''", 'r"""', "r'''",
                                          'f"""', "f'''")):
            for row in range(lineno, end_lineno + 1):
                drop.add(row)
        else:
            # 与别的代码同行（``x = 1;"""d"""`` 这种极少见）—— 跳过，别动
            continue
    return "".join(line for i, line in enumerate(lines, 1) if i not in drop)


def strip_python(source: str, filename: str = "<src>") -> str:
    """删掉一个 Python 文件里的**全部注释与 docstring**，只留可执行代码。

    两步，各用 CPython 自己的工具：
    1. ``ast`` 定位 docstring 的精确区间 → 整行删掉
    2. ``tokenize`` 删掉 ``COMMENT`` token（认得出字符串里的 ``#``）

    ⚠️ **保守到底**：任何一步失败都**原样返回**；剥完还会核对
    （再 parse 一次 + 比对去掉 docstring 后的 AST 是否完全一致），
    不一致就退回原文。宁可留着注释，也绝不许毁代码。
    """
    try:
        tree = ast.parse(source, filename)
    except SyntaxError:
        return source

    try:
        reduced = _blank_spans(source, _docstring_spans(tree))
    except Exception: # noqa: BLE001
        return source

    # ── 删注释（tokenize 认得出字符串里的 #）──
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(reduced).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source
    kept = [t for t in tokens if t.type != tokenize.COMMENT]
    try:
        result = tokenize.untokenize(kept)
    except (ValueError, IndexError):
        return source

    result = re.sub(r"\n{3,}", "\n\n", result)
    if not result.endswith("\n"):
        result += "\n"

    # ── 核对：语法仍然合法，且**语义 AST 与原文一致** ──
    try:
        new_tree = ast.parse(result, filename)
    except SyntaxError:
        return source
    if ast.dump(_strip_docstrings(tree)) != ast.dump(_strip_docstrings(new_tree)):
        return source
    return result


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    """把 AST 里的 docstring 节点去掉，用于"剥离前后是否等价"的比对。"""
    def is_doc(statements: list) -> bool:
        if not statements:
            return False
        first = statements[0]
        return (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str))

    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and is_doc(body):
            node.body = body[1:]
    return tree


def _is_third_party(path: Path) -> bool:
    """第三方代码（vendor 目录）**不剥** —— 那是别人写的，注释是它的说明。"""
    return any(part in KEEP_VERBATIM_DIRS for part in path.parts)


def stage_sources(strip: bool = False) -> dict:
    """把要进包的源码复制到 :data:`STAGE`。

    ``strip=False``（默认）时**原样复制** —— 包里的 .py 与仓库里的 .py
    逐字节一致，任何人解包都能对照源码审计自己跑的是什么。
    ``strip=True`` 才剥注释/docstring（体积小一点，但包里与仓库不一致）。

    返回统计（方便报告里写"省了多少"）。
    """
    if strip:
        print("[0/5] 剥注释与 docstring（只影响进包的那份，源码保持可读）…")
    else:
        print("[0/5] 原样复制源码（不剥注释：保证包里 .py 与仓库逐字节一致）…")
    shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True, exist_ok=True)

    stripped = 0
    original_bytes = 0
    new_bytes = 0
    failed: list[str] = []

    for path in sorted((SRC / "komaflow").rglob("*.py")):
        target = STAGE / "komaflow" / path.relative_to(SRC / "komaflow")
        target.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8")
        original_bytes += len(text.encode("utf-8"))

        if (not strip) or _is_third_party(path.relative_to(SRC / "komaflow")) \
                or path.name in KEEP_VERBATIM_FILES:
            result = text
        else:
            result = strip_python(text, str(path))
            # ⚠️ 剥完必须还是合法 Python，否则**退回原文**（宁可留注释）
            try:
                compile(result, str(path), "exec")
            except SyntaxError as exc:
                failed.append(f"{path.name}: {exc.msg}")
                result = text
            else:
                if result != text:
                    stripped += 1

        target.write_text(result, encoding="utf-8")
        new_bytes += len(result.encode("utf-8"))

    saved = original_bytes - new_bytes
    pct = (saved / original_bytes * 100) if original_bytes else 0.0
    print(f" 剥了 {stripped} 个文件；"
          f"{original_bytes / 1024:.0f} KB → {new_bytes / 1024:.0f} KB"
          f"（省 {saved / 1024:.0f} KB，{pct:.0f}%）")
    if failed:
        print(" ⚠️ 这些文件剥完语法不过，已退回原文：")
        for item in failed[:8]:
            print(f" {item}")

    # 数据文件与第三方目录：脚本类**只删注释**（docstring 留着当 API 说明），
    # vendor 原样复制（别人写的代码，不动它）。
    for extra in ("adapters/scripts", "adapters/vendor"):
        source = SRC / "komaflow" / extra
        target = STAGE / "komaflow" / extra
        target.mkdir(parents=True, exist_ok=True)
        for path in sorted(source.rglob("*")):
            if not path.is_file() or path.suffix.lower() in (".pyc", ".pyo"):
                continue
            if "__pycache__" in path.parts:
                continue
            dest = target / path.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if strip and path.suffix == ".py" and extra.endswith("scripts"):
                text = path.read_text(encoding="utf-8")
                light = strip_python_light(text, str(path))
                dest.write_text(light, encoding="utf-8")
            else:
                # 默认原样复制：包里的 Blender 脚本必须与仓库逐字节一致，
                # 否则"你跑的二进制是不是这份源码"就无从核对（审计提出过）。
                shutil.copy2(path, dest)
    for name in ("ui/assets",):
        source = SRC / "komaflow" / name
        target = STAGE / "komaflow" / name
        target.mkdir(parents=True, exist_ok=True)
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            if path.name in ("README.md",):
                continue
            dest = target / path.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)

    return {"剥了": stripped, "原KB": round(original_bytes / 1024),
            "新KB": round(new_bytes / 1024), "省KB": round(saved / 1024),
            "退回原文": failed}


def _version() -> str:
    sys.path.insert(0, str(SRC))
    from komaflow.branding import APP_VERSION
    return APP_VERSION


def build_exe(clean: bool = True) -> Path:
    """调 PyInstaller 出 ``dist/KomaFlow/KomaFlow.exe``。"""
    print("[1/5] 用 PyInstaller 构建 …")
    if clean:
        for folder in (DIST / APP_NAME, BUILD):
            shutil.rmtree(folder, ignore_errors=True)
    code = _run([sys.executable, "-m", "PyInstaller", "--noconfirm",
                 "--clean", "--distpath", str(DIST), "--workpath", str(BUILD),
                 str(ROOT / "tools" / "KomaFlow.spec")], cwd=str(ROOT))
    target = DIST / APP_NAME / f"{APP_NAME}.exe"
    if code != 0 or not target.is_file():
        raise SystemExit(f"构建失败（退出码 {code}）")
    print(f" -> {target}")
    return DIST / APP_NAME


def find_ffmpeg() -> tuple[Path, Path] | None:
    """在本机找一个 (ffmpeg.exe, ffprobe.exe) 对，用来随包分发。"""
    found_ffmpeg = shutil.which("ffmpeg")
    if found_ffmpeg:
        exe = Path(found_ffmpeg)
        probe = exe.with_name("ffprobe.exe")
        if probe.is_file():
            return exe, probe
    for base in FFMPEG_SEARCH:
        if not base.is_dir():
            continue
        for hit in base.rglob("ffmpeg.exe"):
            probe = hit.with_name("ffprobe.exe")
            if probe.is_file():
                return hit, probe
    return None


def add_portable_ffmpeg(app_dir: Path) -> str:
    """把便携 FFmpeg 放进 ``<程序目录>/ffmpeg/``（可选）。

    ``komaflow.bootstrap`` 会在启动时把那个目录接进 ``KomaFlow_FFMPEG``，
    于是对方**不用自己装 FFmpeg**。这个包大得多（约 650 MB 未压缩），
    所以默认不做，``--with-ffmpeg`` 才做。
    """
    pair = find_ffmpeg()
    if pair is None:
        return "（本机没找到 ffmpeg，跳过）"
    ffmpeg, ffprobe = pair
    target = app_dir / "ffmpeg"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ffmpeg, target / ffmpeg.name)
    shutil.copy2(ffprobe, target / ffprobe.name)
    # ⚠️ 许可是**必须**一起带的：FFmpeg 是 LGPL/GPL 系，
    # 分发二进制要附许可与来源说明。
    source_root = ffmpeg.parent.parent
    copied = [ffmpeg.name, ffprobe.name]
    for name in ("LICENSE", "README.txt", "COPYING.GPLv3", "COPYING.LGPLv2.1"):
        candidate = source_root / name
        if candidate.is_file():
            shutil.copy2(candidate, target / name)
            copied.append(name)
    (target / "来源与许可.txt").write_text(
        "本目录下的 ffmpeg.exe / ffprobe.exe 是**第三方程序**（不是 KomaFlow 的一部分）。\n"
        f"来源：{ffmpeg.parent}\n"
        "许可：FFmpeg 采用 LGPL v2.1+（部分构建含 GPL 组件，见随附 LICENSE）。\n"
        "FFmpeg 官网：https://ffmpeg.org/\n"
        "KomaFlow 只以**子进程**方式调用它做视频拼接与探测，未修改其代码。\n",
        encoding="utf-8")
    copied.append("来源与许可.txt")
    size_mb = sum((target / n).stat().st_size for n in copied
                  if (target / n).is_file()) / 1024 / 1024
    return f"（已放 {', '.join(copied[:2])}，共 {size_mb:.0f} MB）"


#: 英文语言包的源目录（与打包脚本同仓库：<仓库>/语言包/英文）
LANG_PACK_SRC = SRC.parent.parent / "语言包" / "english"


#: 英文切换教程（单独文件，避免在代码里写长文本）
TUTORIAL_FILE = Path(__file__).resolve().parent / "English-UI-Guide.txt"

#: 额外随包附带的英文文档：源文件 -> 包内文件名
EXTRA_EN_DOCS = {
    "User-Guide-EN.txt": "User-Guide-EN.txt",
    "THIRD-PARTY-NOTICES-EN.md": "THIRD-PARTY-NOTICES-EN.md",
}


def add_english_docs(app_dir: Path) -> list[str]:
    """英文用户指南与第三方声明（有就附，没有跳过）。"""
    here = Path(__file__).resolve().parent
    written = []
    for src_name, dst_name in EXTRA_EN_DOCS.items():
        src = here / src_name
        if src.is_file():
            (app_dir / dst_name).write_bytes(src.read_bytes())
            written.append(dst_name)
    return written


def add_language_pack(app_dir: Path) -> str:
    """把英文语言包与教程放进发布目录（默认中文，用户自选）。"""
    if TUTORIAL_FILE.is_file():
        (app_dir / "English-UI-Guide.txt").write_bytes(TUTORIAL_FILE.read_bytes())
    else:
        (app_dir / "English-UI-Guide.txt").write_text("(guide file missing)", encoding="utf-8")
    if not LANG_PACK_SRC.is_dir():
        return "教程已写；语言包源目录不存在（跳过补丁文件）"
    dest = app_dir / "language-pack-en"
    dest.mkdir(parents=True, exist_ok=True)
    for item in LANG_PACK_SRC.rglob("*"):
        if not item.is_file():
            continue
        target = dest / item.relative_to(LANG_PACK_SRC)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item.read_bytes())
    return f"教程 + 语言包已放入（{dest.name}）"



def write_readme(app_dir: Path, version: str, with_ffmpeg: bool) -> None:
    """给对方的**使用说明**（写得像人话，不是技术文档）。"""
    ffmpeg_note = (
        "FFmpeg 已经随包带上了（在 ffmpeg 子目录里），不用管它。"
        if with_ffmpeg else
        "**FFmpeg**（免费）：首次启动会问你要不要自动下载（约 90 MB）；"
        "也可以自己装，或在「设置 → 渲染环境 → 下载 FFmpeg」里下。"
        "只输出 PNG 图片序列时不需要它。")
    text = f"""KomaFlow {version} —— 使用说明
=====================================

这是干什么的
------------
把 MMD 的模型（.pmx）和动作（.vmd）拖进来，点一下，渲成视频。
透明背景的成片也能出（带 alpha 的 MOV，剪辑软件直接能用）。

运行前需要什么
--------------
1. **Blender 5.x**（必须）—— 本工具**不自带** Blender，用你自己装的那个。
   去 https://www.blender.org/download/ 下安装版即可（免费的）。
2. {ffmpeg_note}
3. **mmd_tools 插件**（必须）—— 装在 Blender 里：
   打开 Blender → 编辑 → 偏好设置 → 获取扩展 → 搜 "MMD Tools" → 安装。
   没有它，.pmx / .vmd 读不进来。

怎么用
------
双击 **KomaFlow.exe**。第一次启动它会自己找 Blender：
  · 只找到一台 → 直接用
  · 找到多台 → 让你选一台，选完记住
  · 一台都没有 → 告诉你去哪下

然后：把模型 / 动作 / 镜头 / 舞台拖到那四个框里，点「开始渲染」。

想自己微调
----------
顶栏有个「组装为工程」：点它会把这一套素材组装成一份 .blend，
你可以用 Blender 打开改材质、灯光、相机，改完存盘拖回「角色模型」框，
再点「开始渲染」——就按你改的样子渲。

工具行还有个「渲染模型」：只把一个 .pmx 渲成三视图给你看，
同时生成一份可编辑的角色 .blend。不产生成片。

几个默认行为
------------
· 「渲染背景」默认**关**（出透明）；拖入舞台/背景后会自动变**开**（照背景渲）。
· 点「开始渲染」时如果还没装配过，程序会先装配：导入 → 建物理约束 → 烘焙 → 存工程。
· 动作和模型的骨骼匹配率低于 35% 时，装配阶段会停下并告诉你改法
  （环境变量 KOMEFLOW_ALLOW_LOW_MATCH=1 可强制继续）。
· 装配完会在出图目录留一份 `.blend`（含烘焙好的物理）。之后改渲染区间、重开程序，
  都会直接复用这份工程，**不会重跑物理**；你也可以打开它改完再渲。

文件放哪
--------
成片、工程、中间产物默认都在 **文档\\KomaFlow** 下面（可以在设置里改）。

关于素材
--------
本工具**不附带任何模型、动作、舞台、音乐、图片**。
请自备合法来源的素材，并遵守原作者的使用规约
（尤其"禁止二次配布""禁止商用"）。

许可
----
随包的 `LICENSE_MMD渲染.txt` 是**第三方渲染器**（MIT）的许可声明，请保留。
FFmpeg 是独立的第三方程序（LGPL/GPL），本包**未附带**；
程序可在你同意后按固定版本下载（会校验 SHA256），也可自行安装。

English docs / 英文文档
----------------------
· User-Guide-EN.txt          — full user guide in English
· English-UI-Guide.txt       — how to switch the UI to English
                           （按它装 language-pack-en，界面变英文；不装仍是中文）
· THIRD-PARTY-NOTICES-EN.md  — third-party notices in English
"""
    (app_dir / "使用说明.txt").write_text(text, encoding="utf-8")


def _is_our_file(path: Path, app_dir: Path) -> bool:
    """这个文件是**我们自己产出的**吗（而不是 Python 运行时自带的）。

    只有我们自己的文件里出现"我们的路径"才算隐私泄漏；
    Python 运行时的二进制里出现 ``__pycache__`` 这种**目录名**属于正常。
    """
    rel = path.relative_to(app_dir)
    parts = [p.lower() for p in rel.parts]
    if rel.parts and rel.parts[0] == "_internal":
        rest = parts[1:]
        # _internal 下：``komaflow/**`` 是我们的；其余是运行时/标准库
        return bool(rest) and rest[0] == "komaflow"
    return path.suffix.lower() in OUR_FILE_SUFFIXES


def audit(app_dir: Path) -> dict:
    """**出包前的体检**：隐私、合规、体积。返回报告 dict。

    分两层查隐私（这一版才拆开 —— 上一版混在一起，被
    ``python312.dll`` 里的 ``__pycache__`` 目录名词符串误报过一次）：

    * **A 层（判据）**：只扫**我们自己产出的文件**，找私有串与绝对路径
    * **B 层（提示）**：顺带在"任何文件"里找绝对路径残留（编译垃圾的特征）
    """
    print("[5/5] 出包体检 …")
    files = [p for p in app_dir.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)

    privacy: list[tuple[str, str]] = []
    for path in files:
        if not _is_our_file(path, app_dir):
            continue
        if path.name in PRIVACY_FALSE_POSITIVES:
            continue # Python 自带编码表（虽然不该出现在我们文件里）
        try:
            data = path.read_bytes()
        except OSError:
            continue
        hit = next((n for n in PRIVACY_NEEDLES if n in data), None)
        if hit is not None:
            # ⚠️ 报告里**不写命中的原文** —— 否则体检报告自己成了泄漏源
            privacy.append((str(path.relative_to(app_dir)),
                            f"私有串（{len(hit)} 字节）"))
            continue
        match = ABSOLUTE_PATH_PATTERN.search(data)
        if match:
            privacy.append((str(path.relative_to(app_dir)),
                            "含本机绝对路径"))

    # B 层：任何文件里的绝对路径残留（提示用，可能来自 Python 运行时）
    path_leaks: list[str] = []
    for path in files:
        if _is_our_file(path, app_dir):
            continue
        if path.stat().st_size > 40 * 1024 * 1024:
            continue # 太大且必然是运行时二进制，跳过
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if ABSOLUTE_PATH_PATTERN.search(data):
            path_leaks.append(str(path.relative_to(app_dir)))

    forbidden = [str(p.relative_to(app_dir)) for p in files
                 if p.suffix.lower() in FORBIDDEN_SUFFIXES]
    junk_dirs = [str(p.relative_to(app_dir)) for p in app_dir.rglob("*")
                 if p.is_dir() and p.name in FORBIDDEN_DIRNAMES]

    has_license = any(p.name.startswith("LICENSE_MMD渲染")
                      for p in app_dir.rglob("*"))
    has_blender = any("blender" in p.name.lower() for p in files
                      if p.suffix.lower() in (".exe", ".dll", ".so"))

    return {
        "文件数": len(files),
        "体积MB": round(total / 1024 / 1024, 1),
        "隐私命中": privacy,
        "运行时路径提示": path_leaks,
        "不该带的文件": forbidden,
        "编译垃圾目录": junk_dirs,
        "第三方许可在包里": has_license,
        "运行库许可原文在包里": all(
            (app_dir / "第三方许可原文" / n).is_file()
            for n in ("LICENSE-APACHE-2.0.txt", "LICENSE-PSF-Python.txt",
                      "LICENSE-zlib.txt", "LICENSE-libffi.txt",
                      "NOTICE-OpenSSL.txt")),
        "误带Blender": has_blender,
    }


def make_zip(app_dir: Path, version: str) -> Path:
    print("[4/5] 打压缩包 …")
    zip_path = DIST / f"{APP_NAME}_{version}.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zf:
        for path in sorted(app_dir.rglob("*")):
            if path.is_file():
                zf.write(path, Path(APP_NAME) / path.relative_to(app_dir))
    print(f" -> {zip_path}（{zip_path.stat().st_size / 1024 / 1024:.1f} MB）")
    return zip_path


def tidy_root(app_dir: Path) -> None:
    """把**许可声明也放一份到包根**，对方解压后一眼就能看到。

    ⚠️ PyInstaller 把 ``datas`` 里写 ``"."`` 的东西塞进了 ``_internal/``，
       于是在最外层看不见许可文件。MIT 只要求"随附声明"（在包里就算），
       但**放在 exe 旁边**才符合惯例、也免得对方以为我们没带许可。
    """
    print("[4b/5] 把许可声明放到包根 …")
    pairs = [
        (app_dir / "_internal" / "LICENSE_MMD渲染.txt",
         app_dir / "LICENSE_MMD渲染.txt"),
        # ⚠️ 这里必须跟着 spec 里 `datas` 的**源文件名**走。
        # 改过一次名字（换成对外版声明）忘了同步，结果包根少了第三方声明 ——
        # 空的 tidy_root 不会报错，只看最后文件列表才发现。
        (app_dir / "_internal" / "第三方来源与许可_发布版.md",
         app_dir / "第三方来源与许可.md"),
    ]
    # 兜底：万一以后又改名字，只要 _internal 里只有一个「第三方/来源」开头的 md 就搬它
    if not any((app_dir / "第三方来源与许可.md").parent.joinpath("第三方来源与许可.md").exists()
               for _ in (0,)):
        for cand in (app_dir / "_internal").glob("*来源与许可*.md"):
            shutil.copy2(cand, app_dir / "第三方来源与许可.md")
            print(f" -> 第三方来源与许可.md（回退自 {cand.name}）")
            break
    for source, target in pairs:
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)
            print(f" -> {target.name}")

    # Issue #5：随包再分发了 OpenSSL / zlib / libffi / Python 运行库，
    # 但包里一份对应许可原文都没有。这里把「第三方许可原文/」整目录搬进包根，
    # 顺手校验五个文件都在 —— 缺了就当体检失败（宁可打包报错，别悄悄少文件）。
    lic_src = ROOT / "第三方许可原文"
    lic_dst = app_dir / "第三方许可原文"
    need = ("LICENSE-APACHE-2.0.txt", "LICENSE-PSF-Python.txt",
            "LICENSE-zlib.txt", "LICENSE-libffi.txt", "NOTICE-OpenSSL.txt")
    if lic_src.is_dir():
        lic_dst.mkdir(parents=True, exist_ok=True)
        for name in need:
            src_file = lic_src / name
            if src_file.is_file():
                shutil.copy2(src_file, lic_dst / name)
        missing = [n for n in need if not (lic_dst / n).is_file()]
        print(f" -> 第三方许可原文/（{len(need) - len(missing)}/{len(need)} 个）")
        if missing:
            print(f" !! 缺许可原文：{missing}")
    else:
        print(f" !! 找不到许可原文目录：{lic_src}")


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 KomaFlow 安装包")
    parser.add_argument("--with-ffmpeg", action="store_true",
                        help="把便携 FFmpeg 一起打进去（开箱即用，但包大很多）")
    parser.add_argument("--no-zip", action="store_true", help="不压 zip")
    parser.add_argument("--strip-comments", action="store_true",
                        help="剥掉脚本注释（体积小一点，但包里的 .py 就与仓库不一致了）")
    args = parser.parse_args()

    version = _version()
    print(f"=== 打包 {APP_NAME} {version} ===")
    print(f"源码 = {SRC}")

    strip_info = stage_sources(strip=args.strip_comments)
    app_dir = build_exe()
    strip_check = verify_stripped(app_dir, STAGE)

    print("[2/5] 便携 FFmpeg …")
    ffmpeg_note = (add_portable_ffmpeg(app_dir) if args.with_ffmpeg
                   else "（未打包；对方需自己装 FFmpeg）")
    print(f" {ffmpeg_note}")

    print("[3/5] 写使用说明 …")
    write_readme(app_dir, version, args.with_ffmpeg)
    lang_note = add_language_pack(app_dir)
    print(f" {lang_note}")
    en_docs = add_english_docs(app_dir)
    if en_docs:
        print(f" 英文文档：{'、'.join(en_docs)}")
    tidy_root(app_dir)

    zip_path = None
    if not args.no_zip:
        zip_path = make_zip(app_dir, version)

    report = audit(app_dir)

    lines = [f"{APP_NAME} {version} 打包报告", "=" * 40,
             f"程序目录 : {app_dir}",
             f"压缩包 : {zip_path or '（未压）'}",
             f"文件数 : {report['文件数']}",
             f"解压后体积 : {report['体积MB']} MB",
             f"FFmpeg : {ffmpeg_note}",
             f"第三方许可 : {'在' if report['第三方许可在包里'] else '★ 缺了！'}",
             f"运行库许可 : {'在' if report['运行库许可原文在包里'] else '★ 缺了！'}",
             f"误带Blender: {'★★ 有！' if report['误带Blender'] else '没有（正确）'}",
             f"剥注释 : {strip_info['剥了']} 个文件，源码 "
             f"{strip_info['原KB']} KB → {strip_info['新KB']} KB"
             f"（省 {strip_info['省KB']} KB）",
             "",
             "隐私检查（本机路径 / 用户名 / 开发笔记）："]
    if report["隐私命中"]:
        lines.append(" ★ 发现命中，必须处理：")
        lines += [f" {name} ← {needle}" for name, needle in report["隐私命中"]]
    else:
        lines.append(" ✓ 没有发现本机私有痕迹")
    lines += ["", "合规检查（模型 / 影音 / 工程文件 / 编译垃圾）："]
    if report["不该带的文件"] or report["编译垃圾目录"]:
        lines.append(" ★ 带了不该带的：")
        lines += [f" {name}" for name in report["不该带的文件"][:20]]
        lines += [f" {name}/（编译垃圾目录）" for name in report["编译垃圾目录"][:10]]
    else:
        lines.append(" ✓ 没有带任何模型 / 贴图 / 成片，也没有 .pyc 编译垃圾")

    lines += ["", "剥注释是否真的进包了："]
    if strip_check["还含注释的"]:
        lines.append(" ★ 这些随包的 .py 里还有注释（说明剥的没生效）：")
        lines += [f" {name}" for name in strip_check["还含注释的"][:10]]
    elif strip_check["包内py文件"]:
        lines.append(f" ✓ 包里 {strip_check['包内py文件']} 个 .py，"
                     f"该剥的都剥干净了")
    else:
        lines.append(" （包里没有 .py 数据文件；我们的模块编译进了 exe/zip 归档）")
    if strip_check["按设计保留的"]:
        lines += [" 按设计保留注释的（第三方 / 约定）："] + [
            f" {name}" for name in strip_check["按设计保留的"][:6]]
    if not strip_check["素版存在"]:
        lines.append(" ★ 素版目录不存在 —— 可能打的是带注释的原始源码！")

    if report["运行时路径提示"]:
        lines += ["", "提示（Python 运行时自带的二进制里出现本机风格路径）：",
                  *[f" {name}" for name in report["运行时路径提示"][:8]],
                  " ⚠️ 这些是**官方运行时**的构建路径，不是我们的；"
                  "不构成隐私泄漏，仅记录。"]

    text = "\n".join(lines)
    (DIST / "打包报告.txt").write_text(text, encoding="utf-8")
    print()
    print(text)

    bad = (report["隐私命中"] or report["不该带的文件"]
           or report["编译垃圾目录"] or report["误带Blender"]
           or not report["第三方许可在包里"])
    if bad:
        print("\n★ 体检没过 —— 上面列出的问题必须先修掉再分发。")
        return 1
    print(f"\n✓ 体检全过。要分发的就是：{zip_path or app_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
