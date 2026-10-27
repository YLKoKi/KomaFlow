"""KomaFlow 主界面。

布局（参考线框图，但**以好用为准**）：

::

    ┌───────────────────────────────────────────────┬────────┐
    │ ● KomaFlow 19:52:39 [开始][暂停][终止] │ 徽标 │ 深蓝顶栏
    ├──────────────┬────────────────────────────────┴────────┤
    │ 环境 │ ┌────┐┌────┐┌────┐┌────┐ │
    │ Blender │ │①角色││②动作││③镜头││④舞台│ ← 投放卡/步骤卡
    │ 输出目录 │ └────┘└────┘└────┘└────┘ │
    │ 工程 │ [单帧预览] 帧[30] [打开输出目录] │
    ├──────────────┤ ────────────────────────────────────── │
    │ 参数 │ ▓▓▓▓▓░░░░ 62.00% 帧 1860/3000 剩余… │
    │ … │ [日志] │
    └──────────────┴────────────────────────────────────────────┘

四张卡片**一物两用**：配置时是素材投放区（支持从资源管理器直接拖进来），
执行时变成步骤状态卡。省掉一整页切换，这就是"更方便"的地方。

本层只做展示与输入，**不直接调用 Blender**：重活全交给 ``core.pipeline``，
在工作线程里跑，用队列把事件搬回主线程刷新界面。

⚠️ 所有 Tk 调用必须在主线程。工作线程里**绝不能**碰 ``self.vars[...].get()``
（会抛 "main thread is not in main loop"，实测踩过）。
"""

from __future__ import annotations

import os
import queue
import shutil
import struct
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

from ..adapters import BASELINE
from ..adapters.discovery import discover_blender
from ..adapters.proc import ProcessControl
# ⚠️ bootstrap **必须排在前面** import：它把"随包的便携 FFmpeg"接进环境变量，
# 而后面任何要用 ffmpeg 的路径都读那个变量（打包版的关键一步）。
from .. import bootstrap as _bootstrap # noqa: F401
from ..branding import APP_NAME, APP_VERSION
from ..core.errors import KomaFlowError
from ..core.journal import JOURNAL
from ..core.pipeline import (RunResult, assemble_project,
                             render_still, run_project)
from ..core.project import Inputs, Output, Project
from ..settings import Settings
from . import assets as ui_assets
from . import dnd
from . import model_render_window
from . import theme as T
from . import tip
from . import settings_window
from .widgets import (DONE, EMPTY, FAILED, PENDING, RUNNING, BatchProgress, Card,
                      HelperCard, MaterialList, Select, ThinProgress)


def _shorten(text: str, limit: int = 40) -> str:
    """清单里那列文字太长就截断。Label 不会自己裁，只能我们裁。

    跟 :func:`shorten_filename` 不一样：这里的输入常常根本不是文件名
    （"留空 → 自动生成正面机位"这类），不能按扩展名去省略。
    """
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _middle_ellipsis(text: str, limit: int = 30) -> str:
    """中间省略，保住两端。

    用在"两头都重要"的地方：``Blender 5.2.2 LTS · 能直接开工（共 2 台）``
    从头截会把结论切掉，中间省略则两端都留得住。
    """
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    keep = limit - 1
    head = keep // 2
    tail = keep - head
    return f"{text[:head]}…{text[-tail:]}"


from ..core import budget
from ..core.segment import DEFAULT_SEGMENT_LIMIT, plan_segments
from . import env_picker


def _claim_windows_app_id() -> None:
    """给进程一个**自己的 AppUserModelID**，否则任务栏图标是 Python 的。

    不设这个 ID 时，Windows 会把进程按可执行文件（``pythonw.exe``）分组，
    任务栏按钮和"固定到任务栏"用的都是 **pythonw.exe 的图标**（一个带
    Python 标志的脚本文件），我们 ``iconphoto`` 设的 LOGO 只在标题栏生效。
    设完之后系统才把它当成一个独立应用，任务栏才认我们的图标。

    必须在**窗口创建之前**调用，所以放在模块导入时。
    """
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "KomaFlow.LocalRenderPipeline")
    except (ImportError, AttributeError, OSError):
        pass # 非 Windows 或受限环境：没有也不影响功能


_claim_windows_app_id()

# DPI 感知也必须**早于建任何窗口**，所以同样放在模块导入时。
# 不声明的话，Windows 会把这张 96DPI 画出来的界面**拉伸 1.5 倍** ——
# 文字发虚、圆角带锯，怎么调素材都没用。声明之后界面会缩到 2/3，
# 所以界面里所有尺寸都过一遍 theme.S() 补回来（见 tools/apply_dpi_scaling.py）。
#
# ⚠️ **这两件事必须同时做完才有效**：
# * 只开声明、不包尺寸 → 界面缩到 2/3（曾经为此撤回过一次）
# * 只包尺寸、不开声明 → Windows 再把补好的尺寸拉伸一遍，等于白干
# 现在 tools/apply_dpi_scaling.py 已经跑完（100+ 处），两边齐了。
T.claim_dpi_awareness()

#: 静态资源目录（LOGO 等）
ASSETS = Path(__file__).parent / "assets"

#: 四张卡片。配置时是投放区，执行时是步骤。
ZONES: list[tuple[str, str, str]] = [
    # 第 1 张卡也收 .blend —— 用户自己在 Blender 里配好 / 渲完的工程，
    # 直接拖进来就能用，不用再拆成 pmx + vmd（这就是"路线 B"）。
    ("model", "角色模型 / 工程", ".pmx / .pmd / .blend"),
    ("motion", "动作", ".vmd"),
    ("camera", "镜头", ".vmd（可留空）"),
    ("stage", "舞台", ".pmx / .pmd / .blend"),
]

#: 「动作」那张卡的占位槽（**只用来标记"这是一张列表卡"**）。
#:
#: ⚠️ 数量**不再固定为 2**。用户要求：
#: > 这个动作和表情，可以添加多个而不是局限于2个
#: 所以卡片上的行数跟着实际文件数走（见 ``Card.show_slots``），
#: 这里只留一个空占位，表示"这张卡走列表模式"。
MOTION_SLOTS: tuple[str, ...] = ("",)

#: 执行时的四步。措辞刻意避开行话（"分段""装配""校验"这类词用户看着费劲）。
STEPS: list[tuple[str, str]] = [
    ("assemble", "整理素材"),
    ("render", "分批渲染"),
    ("pack", "合成与检查"),
    ("done", "导出成片"),
]

STAGE_TO_INDEX = {
    "prepare": 0, "assemble": 0, "still": 0,
    "render": 1,
    "concat": 2, "verify_final": 2,
}

ORIENTATION_PRESETS = {
    "横屏 16:9": (2560, 1440),
    "竖屏 9:16": (1440, 2560),
    "方形 1:1": (1440, 1440),
}

#: 清晰度档位。**换成大家熟悉的叫法**（4K / 2K / 1080P / 360P），
#: 不再用"快速预览档 / 标准成品档 / 高质档"——
#: 那三档其实只映射到 Blender 自带的 FFmpeg CRF，而我们的流程是
#: **出 PNG 序列再用外部 FFmpeg 编码**，那个参数压根没用上，
#: 等于给用户一个没有实际作用的旋钮。改成选分辨率，是真的会变的东西。
#:
#: 值是"长边尺寸"：横屏时给宽、竖屏时给高，具体换算见 ``_on_resolution``。
RESOLUTION_PRESETS: dict[str, int] = {
    "360P": 640,
    "1080P": 1920,
    "2K": 2560,
    "4K": 3840,
}

QUALITY_LABELS = {"快速预览档": "preview", "标准成品档": "standard", "高质档": "high"}
QUALITY_REVERSE = {v: k for k, v in QUALITY_LABELS.items()}

#: 透明成片的四种格式：``内部键 → 下拉里显示的名字``。
#:
#: ⚠️ 方向是**键→显示名**，因为 ``Select`` 拿变量里的值去这张表里查脸。
#: 写反了的话下拉框会把内部键（``prores``）直接显示给用户 —— 踩过。
#: 顺序就是下拉里的顺序，短的在前：用户认的是"MP4 / MOV / PNG"这几个词。
ALPHA_CODEC_CHOICES: dict[str, str] = {
    "h264": "MP4（最通用，不带透明）",
    "prores": "MOV（推荐，体积小）",
    "qtrle": "MOV 无损（体积最大）",
    "png": "PNG 图片序列",
}

#: 每种格式的详细说明，鼠标停上去展开。
#: 写法：第一行说"它是什么"，后面说代价和该什么时候用。
#:
#: ⚠️ **别写技术细节**。第一版把 MP4 那条写成"角色背后会被垫上一层深褐色" ——
#: 用户直接问"褐色什么鬼"。那个 ``#4A3728`` 是我们内部给 MP4 垫的底色，
#: 是**实现细节**，用户不需要知道；他需要知道的是
#: "这个格式不能透明、所以不能拿去叠画面"。
#: 同理也别写"alpha 通道""逐帧对拍"这类词。
CODEC_INTRO: dict[str, str] = {
    "h264": ("MP4　最通用\n"
             "发人、传网站、手机上放都行。\n"
             "但它装不了透明：透明的地方会填成一块纯色。\n"
             "只能拿去看，不能叠在别的画面上。"),
    "prores": ("MOV · ProRes　推荐\n"
               "透明的，剪辑软件（PR、达芬奇、剪映）直接能用。\n"
               "体积比「无损」那个小很多，画质看不出差别。\n"
               "要做后期就用它。"),
    "qtrle": ("MOV · 无损\n"
              "也是透明的，一个像素都不压。\n"
              "代价是体积大得多，2K 三分钟可能上百 GB。\n"
              "除非客户点名要，否则用 ProRes 就行。"),
    "png": ("PNG 图片序列\n"
            "不出视频，只给一堆透明图片。\n"
            "适合自己在别的软件里拼，或者挑单张出来用。\n"
            "它不会生成 MOV 或 MP4。"),
}

#: 每种格式的**一句话摘要**，常驻显示在格式卡里。
#: 用户不点开也得知道"我选的是什么、代价是什么"；
#: 完整介绍太长，留给悬停展开（``CODEC_INTRO``）。
CODEC_SUMMARY: dict[str, str] = {
    "h264": "最通用，但不能透明 —— 透明的地方会填成一块纯色。",
    "prores": "透明的，剪辑软件都能用，体积也不大。一般选它。",
    "qtrle": "也是透明的，更清晰，但体积大好几倍。",
    "png": "只给一堆透明图片，不会生成视频。",
}


def codec_overview() -> str:
    """四种格式的**对照说明**，鼠标停在格式下拉上就展开。

    为什么是"四种一起列"而不是"只讲当前选的那个"：
    用户做的是**选择**，选择需要对比。一次只看到一种，他还是得挨个点过去看。
    一起列出来，停在上面一眼就能挑。

    ⚠️ 每段开头用的名字要跟下拉里的**一模一样**，别两种叫法 ——
    不然用户对不上"这段在说哪一项"。
    """
    blocks = []
    for codec, _name in ALPHA_CODEC_CHOICES.items():
        text = CODEC_INTRO.get(codec, "")
        if text:
            blocks.append(text)
    return "\n\n".join(blocks)

FRAMING_LABELS = {"全身": "full", "七分身": "seven_tenths", "半身": "half"}
FRAMING_REVERSE = {v: k for k, v in FRAMING_LABELS.items()}

#: 预览区水印允许的最大不透明度（0~255）。超过它**就不画**。
#: Tk 没有 alpha 合成，"极淡"只能靠预渲染；这张图被写坏过（变成不透明的大白圆，
#: 还叠在引导卡上），所以加一道闸 —— 与其糊个扎眼的白圆，不如留白。
WATERMARK_MAX_ALPHA = 90

FILE_TYPES = {
    # ⚠️ 模型卡也能接 **PNG**：模型渲染那块渲出来的三视图会回填到这张卡上，
    # 用户点卡片想换一个时，得能一眼选到那些图。
    "model": [("模型 / 渲好的三视图 / 已配好的工程",
               "*.pmx *.pmd *.png *.blend"), ("所有文件", "*.*")],
    "motion": [("VMD 动作/镜头", "*.vmd"), ("所有文件", "*.*")],
    "expression": [("VMD 表情", "*.vmd"), ("所有文件", "*.*")],
    "camera": [("VMD 镜头", "*.vmd"), ("所有文件", "*.*")],
    "stage": [("舞台（模型或 Blender 工程）", "*.pmx *.pmd *.blend"),
              ("所有文件", "*.*")],
    "blender": [("Blender", "blender.exe"), ("所有文件", "*.*")],
    "project": [("KomaFlow 工程", "*.kfproj"), ("所有文件", "*.*")],
}

#: 「渲染背景」是个开关，但用下拉做 —— 一屏里的选择控件得长一个样，
#: 单独插一个自绘 Switch 反而突兀。语义就是开/关。
ON_OFF = ("开", "关")

#: 那个开关的说明。四种状态各有一条（由 ``_transparent_state()`` 选）。
#:
#: ⚠️ 这里的措辞是用户逐字定的：
#: * 「有角色没背景 → 自动打开」= 出透明，这是最常用的用法
#: * 「有背景没角色」也**能点**（旧版会灰掉）—— 用户可能就是想先摆空景
TRANSPARENT_TIP = (
    "关掉背景，成片带透明通道，方便后期自己垫画面。\n"
    "放了角色、没放舞台时会自动打开。\n"
    "放了舞台也能用 —— 关掉它角色下面就是透明的。"
)

#: 两样都没有 → 只有这种情况才灰
NOTHING_TO_RENDER_TIP = (
    "还没有角色，也没有舞台 —— 现在渲出来是空的，这一项先不用管。\n"
    "放个角色模型进来，它会自动打开。"
)

#: 放了舞台、**也**放了角色 → 可选，但要告诉他默认是照着背景渲
STAGE_TIP = (
    "你放了舞台，所以默认照着舞台渲（这一项默认是开）。\n"
    "想让成片带透明通道、后期自己垫画面，就把它关掉。"
)

#: 还没放角色、但放了舞台 → 可选，说清现在为什么是"关"
STAGE_ONLY_TIP = (
    "现在只有舞台、没有角色，所以这一项是开着的（照着舞台渲）。\n"
    "放进角色模型之后，它会自动关掉（= 出透明）。"
)

#: 走「已配好的 .blend」那条路 → 一律不可选，因为我们不碰用户的工程
BLEND_TIP = (
    "这条路用的是你自己配好的 .blend —— 背景怎么设，我按你文件里的来，\n"
    "不会替你改。所以这一项现在不起作用。"
)

#: 观感固定用能力包（「渲染模型」存出的那份材质），不再给用户选。
#: 对应设置项：``settings.last_look_blend``。


#: 估不了真实时长时的兜底帧数（30fps 一分钟）。只用来估磁盘，宁可偏大。
FALLBACK_FRAMES = 1800

# ── 布局尺寸：**全部是设计像素**（1x），过一遍 ``T.S()`` 就是实际像素 ──
# 这些数字来自 ``work/mockup_full.png`` 那张 1600×1000 的设计图，
# 一一对得上（侧栏 300 + 间距 12 + 主区）。改布局先改这里，别散在代码里。
ZONE_CARD_H = 146 # 四张投放卡
TOOL_ROW_H = 32 # "单帧预览 / 预览帧号"那一行
PREVIEW_H = 336 # 预览区**固定高度**（不再 expand 吃掉全部高度）
INFO_GAP = 12 # 卡与卡之间的缝
STATUS_STRIP_H = 56 # 底部状态条
GAP_X = 12 # 侧栏与主区之间的缝
BODY_PAD_X = 14
BODY_PAD_Y = 12
CARD_GAP = 10 # 四张卡之间的缝


class KomaFlowApp:
    """主窗口。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.queue: queue.Queue = queue.Queue()
        self.cancel: threading.Event | None = None
        self.control = ProcessControl()
        self.worker: threading.Thread | None = None
        self.started_at: float | None = None
        self.output_path: Path | None = None
        self.last_percent = 0.0
        self.mode = "config" # config | run
        self.cards: dict[str, Card] = {}
        # 用户设置（三类保存位置 + 快照开关 + 界面缩放）。
        # 读失败会拿到一套默认值，不会让程序起不来（见 core.settings.load）。
        self.settings = Settings.load()
        T.set_zoom(self.settings.ui_zoom)
        # 字号也跟着 DPI 走（``tk scaling`` 是"每点几个像素"）
        T.apply_scaling(self.root)
        self._panels: list[tk.Frame] = []
        self._drop_targets = []
        self._blender_probed: list[dict] = []
        self._blender_labels: list[str] = []
        #: 选中的那台**曾经缺过** mmd_tools —— 补上之后才值得庆祝一次
        #: （见 _celebrate_ready）。这里显式初始化，别靠 getattr 兜底。
        self._was_missing_tools = False
        # 分批进度用：这次要分几批、每批多少张。**开始渲染时才定**。
        self._planned_segments: list[tuple[int, int]] = []
        self._guide_button: tuple[float, float, float, float] | None = None

        # 标题栏不写版本号 —— 顶着 "0.0.1" 是"没做完"的观感；版本号在「关于」里
        root.title(APP_NAME)
        root.configure(bg=T.CREAM)
        # 窗口尺寸交给 _apply_geometry（它在 _build_ui 里也会被调到，
        # 因为改界面缩放时窗口也要跟着变）

        self.vars = {
            "blender": tk.StringVar(), "model": tk.StringVar(),
            "camera": tk.StringVar(),
            "stage": tk.StringVar(), "blend": tk.StringVar(),
            "name": tk.StringVar(value="KomaFlow"),
            "output_dir": tk.StringVar(value=self.settings.output_dir),
            "work_dir": tk.StringVar(value=self.settings.work_dir),
            "preroll": tk.StringVar(value="30"), "limit": tk.StringVar(value="1000"),
            "fps": tk.StringVar(value="30"), "width": tk.StringVar(value="2560"),
            "height": tk.StringVar(value="1440"),
            "frame_start": tk.StringVar(value="0"), "frame_end": tk.StringVar(value=""),
            "orientation": tk.StringVar(value="横屏 16:9"),
            # 分辨率档位。它是**派生值**：从当前宽高反推出来显示，
            # 宽高不匹配任何档位时显示为空（用户自己填的精细值）。
            "resolution": tk.StringVar(value="2K"),
            "framing": tk.StringVar(value="全身"),
            "still_frame": tk.StringVar(value="30"),
            #: 单帧预览的命名。**留空 = 跟着动作名走**（去掉 .vmd 后缀）——
            #: 用户要求"没动就是显示默认名称（动作名称不带后缀）"。
            "still_name": tk.StringVar(value=""),
            "probe": tk.StringVar(value="尚未探测 —— 点「探测本机」"),
            "percent": tk.StringVar(value="0.00%"),
            "status": tk.StringVar(value="还没放素材。拖进来，或者点卡片自己选。"),
            "clock": tk.StringVar(value=time.strftime("%H:%M:%S")),
        }
        # 透明成片的格式：让用户自己选，选完立刻弹窗告诉他这次要占多少磁盘。
        # ⚠️ 必须**在建界面之前**就备好 —— 侧栏底部那张"输出预估"卡一建出来
        # 就要读它算数字，晚一步就是 AttributeError（踩过）。
        #
        # ⚠️ 这里存的是**内部键**（``prores`` / ``qtrle`` / ``png`` / ``h264``），
        # 不是界面上的字。显示名交给 ``Select`` 的 ``labels`` 映射去翻 ——
        # 这样改文案不会让旧工程文件读不出来。
        self.alpha_codec = tk.StringVar(value=budget.DEFAULT_ALPHA_CODEC)
        # 「渲染背景」的显式开关（关 = 出透明成片）。默认值由素材决定，
        # （派生规则在 core/project.effective_transparent）。
        #: 「渲染背景」默认**关**（= 出透明）；加舞台之后会自动变开（见 _assign）
        self.transparent = tk.StringVar(value="关")
        #: 界面缩放下拉的当前值（换档时重建界面，见 set_zoom）
        self.zoom_var = tk.StringVar(value=f"{int(T.UI_ZOOM * 100)}%")
        #: 用户是否**明确**把它拨到了"关"。
        #: 少了这个记号就会出矛盾：放了舞台时我们替他把开关拨到"关"，
        #: 之后他清空舞台，开关还是"关"，可实际却按"自动开"渲了透明 ——
        #: 界面说的和做的对不上。有了这个记号，清空舞台时才敢拨回"开"。
        #: 用户是否**亲手拨过**「渲染背景」。
        #:
        #: 少了这个记号就分不清"用户明确选了开"和"还没轮到默认规则"：
        #: 「渲染背景」默认是"关"，而"有舞台+有角色"时默认也是"关"（照背景渲）——
        #: 两种在控件取值上一模一样。有了它才敢在素材变化时按默认规则回填，
        #: 也不会把用户亲手做的选择覆盖掉。
        self._transparent_user_set = False

        #: 「动作」卡里的全部文件（**有序，数量不限**）。
        #:
        #: ⚠️ 为什么不用 tk.StringVar：动作可以有任意多个，StringVar 只能存一个值。
        #: 这一个列表就是唯一的真相，卡片显示 / 工程保存 / 装配都读它。
        self.motion_list: list[str] = []


        self._build_menu()
        # 先加载装饰图，免得第一次显示时闪一下
        ui_assets.preload("deco_notes_header.png", "deco_note_card.png",
                          "deco_tail_l.png", "deco_tail_r.png",
                          "deco_notes_soft.png", "logo.png", "mascot.png",
                          "deco_fox_ears.png", "deco_bell.png",
                          "avatar.png", "avatar_work.png",
                          "face_idle.png", "face_work.png",
                          "face_done.png", "face_fail.png")
        # 图标要在窗口真正映射之后再设一次（Tk 自己的设置经常不生效）
        self._schedule("_icon_job", 120, self._force_window_icon)
        # ⚠️ 标题栏处理**不能只调一次**。
        # `after(140)` 时窗口往往还没被系统映射出来，此刻调 DWM 那些属性
        # 可能被系统随后的映射重置掉，所以映射之后还要再来一次
        # （见 _on_window_mapped）。
        self.root.bind("<Map>", self._on_window_mapped, add="+")
        self._schedule("_titlebar_job", 140, self._polish_title_bar)
        # 拖放轮询：窗口过程只往队列里塞路径，真正处理在这里（Tk 主循环里，安全）
        self._schedule("_drop_job", 150, self._poll_drops)
        self._build_ui()
        self._poll_queue()
        self._tick()
        # 启动就自动检测并**自动选用** Blender —— 不让用户自己去绑路径
        self._detect_blender_async()

    # ------------------------------------------------------ 界面（可重建）
    def _build_ui(self) -> None:
        """把整个界面搭起来。**改界面缩放时会再调一次**（见 :meth:`set_zoom`）。

        所以这个方法必须是**幂等**的：进来时先把上一轮的界面拆干净，
        再按当前的 ``theme.total_scale()`` 重新画一遍。
        状态（输入框里的字、工程内容、日志）都在 ``self.vars`` 与工程文件里，
        重建不会丢。
        """
        # 拆掉上一轮：所有界面控件都是 root 的直接子窗（标题栏/顶栏/内容区）
        for child in list(self.root.winfo_children()):
            if child is getattr(self, "_popup_menu", None):
                continue
            child.destroy()
        self.cards = {}
        self._panels = []
        self._drop_targets = []
        self.preview = None
        self.batch = None
        self.materials = None
        self._preview_source = None
        self._preview_resize_job = None
        self._guide_button = None

        # 字号跟着缩放比走（重建时必须重设：tk scaling 是全局的）
        T.apply_scaling(self.root)
        # 窗口大小也要跟着缩放走，否则放大后内容挤在小窗里
        self._apply_geometry()

        self._build_header()
        self._build_body()
        self._refresh_cards()
        # 摘要条 / 素材清单要跟着这些参数走：填一个数字就更新一次。
        # ⚠️ **不能重复挂 trace**：重建一次就多一份，改一个数会触发十几次重画。
        # 所以第一次挂过之后就不再挂（下面的旗标）。
        if not getattr(self, "_panel_traces_installed", False):
            for key in ("width", "height", "fps", "frame_start",
                        "frame_end", "preroll", "limit", "stage", "blend",
                        "camera", "model", "motion"):
                try:
                    self.vars[key].trace_add("write",
                                             lambda *_: self._refresh_panels())
                except (KeyError, AttributeError):
                    pass
            # 宽高改了 → 档位跟着显示。用户手填的数字对不上任何档位时显示空白。
            for key in ("width", "height"):
                try:
                    self.vars[key].trace_add(
                        "write", lambda *_: self._sync_resolution_face())
                except (KeyError, AttributeError):
                    pass
            # 档位是**派生值**，只在用户主动点下拉时才算宽高
            # （`_on_resolution` 里显式调）。挂 trace 会形成回环：
            # 反推档位 → 触发 trace → 又把宽高改回去，把用户手填的数冲掉。
            self._panel_traces_installed = True
        self._refresh_panels()
        JOURNAL.bind(self.vars["work_dir"].get())
        self._install_dnd()
        # 角色卡要有记忆：把上次确定用过的模型放回去（放最后，等界面齐了再填，
        # 免得在卡片还没建好时就去刷它）
        self._restore_last_model()

    # --------------------------------------------- 角色卡记忆（上次用的模型）
    def _restore_last_model(self) -> None:
        """启动时把**上次确定用过的**角色模型填回第 1 张卡。

        用户要求：

        > 角色板块应该带有记忆性，上一次用户确定使用之后，下一次打开应该
        > 默认还是那个，直到用户渲染下一个模型或者用户导入模型

        所以这不是"最近打开过的文件"，而是**上次真正确定用过的那个**：
        只有用户拖/选了模型、或模型渲染点了「继续」，才会被记住（见
        :meth:`_remember_model`）。

        ⚠️ **文件可能已经不在了**（被移走/删掉/换盘）。这时不能悄悄填一个
           不存在的路径（一渲就报错，还看不出为什么），要清掉记忆并说一声。
        """
        model = str(getattr(self.settings, "last_model", "") or "").strip()
        blend = str(getattr(self.settings, "last_blend", "") or "").strip()
        if not model and not blend:
            return

        alive_model = model if model and Path(model).is_file() else ""
        alive_blend = blend if blend and Path(blend).is_file() else ""
        gone = [p for p in (model, blend) if p and not Path(p).is_file()]

        if gone:
            self._log(f"上次用的模型已经不在了：{'、'.join(gone)}", "warn")
            self.vars["status"].set(
                f"上次用的模型找不到了（{Path(gone[0]).name}），请重新放一个")
            # 记忆作废 —— 否则每次启动都提示一次
            try:
                self.settings.last_model = alive_model
                self.settings.last_blend = alive_blend
                self.settings.save()
            except Exception: # noqa: BLE001 - 存不下也不该影响启动
                pass

        if alive_model:
            self.vars["model"].set(alive_model)
        if alive_blend:
            self.vars["blend"].set(alive_blend)
        if alive_model or alive_blend:
            name = Path(alive_model or alive_blend).name
            self._log(f"沿用上次的角色：{alive_model or alive_blend}")
            self.vars["status"].set(f"沿用上次用的角色：{name}")
            self._refresh_cards()

    def _remember_model(self, path: str) -> None:
        """记住"这次确定用哪个角色"（写进设置，下次启动自动填回来）。

        ⚠️ 只在**用户真的确定要用**时调：
           * 拖/选了一个模型 → 记住
           * 模型渲染点了「继续」→ 记住
           渲染中途取消、只是点开看一眼 —— **都不该改记忆**，
           否则用户下次打开会发现"怎么变成我刚才试的那个了"。
        """
        if not path:
            return
        target = Path(path)
        try:
            if target.suffix.lower() == ".blend":
                self.settings.last_blend = str(target)
                self.settings.last_model = ""
            else:
                self.settings.last_model = str(target)
                self.settings.last_blend = ""
            self.settings.save()
        except Exception as exc: # noqa: BLE001 - 记忆失败不该挡住渲染
            self._log(f"记住角色失败（不影响使用）：{exc}", "warn")

    def _apply_geometry(self) -> None:
        """按设计尺寸 × 总缩放 定窗口大小与最小尺寸。"""
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        # 目标尺寸是**设计像素**（跟 mockup 那张 1600×1000 同一套坐标）。
        # 1500×978 是设计稿的窗口大小，× 缩放比就是实际像素。
        design_w = int(1500 * T.total_scale())
        design_h = int(978 * T.total_scale())
        floor_w = int(1080 * T.total_scale())
        floor_h = int(660 * T.total_scale())
        width = min(design_w, max(floor_w, screen_w - 40))
        height = min(design_h, max(floor_h, screen_h - 60))
        self.root.geometry(
            f"{width}x{height}+{max(0, (screen_w - width) // 2)}"
            f"+{max(0, (screen_h - height) // 3)}")
        self.root.minsize(floor_w, floor_h)

    def set_zoom(self, zoom: float) -> None:
        """切换界面缩放：整个界面重建一遍。

        Tk 没有"整体缩放"这个开关 —— 尺寸是建控件时算死的，改不了。
        好在我们的尺寸都是过了 ``theme.S()`` 的，所以"重新走一遍构建流程"
        就等于整体缩放；代价是控件会被销毁重建（输入框里的光标位置会丢，
        内容不丢，因为值在 ``self.vars`` 里）。
        """
        if abs(float(zoom) - T.UI_ZOOM) < 0.001:
            return
        T.set_zoom(zoom)
        tip.close_all() # 重建前先收掉悬停提示，别在屏幕上留孤儿小窗
        self._build_ui()
        self._refresh_panels()
        self._log(f"界面缩放改为 {int(T.UI_ZOOM * 100)}%")
        self.vars["status"].set(f"界面缩放：{int(T.UI_ZOOM * 100)}%")

    def _on_zoom(self, label: str) -> None:
        """下拉换档 → 重建界面。"""
        try:
            zoom = int(str(label).rstrip("%")) / 100.0
        except ValueError:
            return
        self.set_zoom(zoom)

    # ------------------------------------------------- 启动完成钩子（开发期）
    def _build_menu(self) -> None:
        """构建**弹出菜单**（不挂到窗口菜单栏）。

        Windows 原生菜单栏是这套界面里最"廉价"的一处：灰底宋体、跟深蓝顶栏
        完全不搭。改成顶栏那个"⋯"按钮弹出，观感统一得多。
        """
        menu = tk.Menu(self.root, tearoff=0, bg="#FFFFFF", fg=T.TEXT,
                       activebackground=T.NAVY_TINT, activeforeground=T.NAVY,
                       relief="flat", bd=0, font=T.pick_font(9))
        menu.add_command(label="打开工程…", command=self.on_open_project)
        menu.add_command(label="保存工程…", command=self.on_save_project)
        menu.add_separator()
        menu.add_command(label="打开渲染快照目录", command=self.on_open_snapshots)
        menu.add_command(label="打开输出目录", command=self.on_open_output)
        menu.add_separator()
        menu.add_command(label="设置…", command=self.on_settings)
        menu.add_command(label="关于本软件", command=self.on_about)
        menu.add_separator()
        menu.add_command(label="退出", command=self.on_close)
        self._popup_menu = menu

    def _show_menu(self) -> None:
        """在鼠标位置弹出 ⋯ 菜单。"""
        try:
            self._popup_menu.tk_popup(self.root.winfo_pointerx(),
                                      self.root.winfo_pointery())
        finally:
            self._popup_menu.grab_release()

    # ------------------------------------------------------------------ 顶栏
    def _build_header(self) -> None:
        # ── 用**系统标准窗口**，不做无边框 ──
        #
        # 早先的做法是 ``overrideredirect(True)`` 去掉系统窗口框、自己画标题栏，
        # 追求"一体化"。**这个方向是错的**，代价是拖边改大小、双击最大化、
        # 系统贴边、多屏 DPI 这些全都得自己重写 —— 补了几样，剩下的就一直缺。
        # 抖音 / B站 那种"看着一体化又能随便拖边"，用的是**系统窗口 + 自绘内容区**，
        # 系统框一直在（见 ui/window.py 顶上那段说明）。
        #
        # 所以现在：系统标题栏 + 系统边框照常，我们的自绘顶栏接在它下面，
        # 用 DWM 暗色模式让系统标题栏跟品牌深蓝接近（`_polish_title_bar`）。

        header = tk.Frame(self.root, bg=T.NAVY, height=T.S(52))
        header.pack(fill="x")
        header.pack_propagate(False)
        # 顶栏下面不画通栏硬线：压一条线读起来就是"这里被切开了"

        # 顶栏左侧的 LOGO 画布。
        # ⚠️ **必须 pack**，否则 Tk 不绘制未映射的 canvas，图就白画了（踩过）。
        # 尺寸按品牌规范：图标四周留白 ≥ 图标宽度的 12%。
        # 40px 图标 → 左右各留 5 → 画布 52 宽、44 高（上下各留 2）。
        logo_holder = tk.Canvas(header, width=T.S(52), height=T.S(44), bg=T.NAVY,
                                highlightthickness=0)
        logo_holder.pack(side="left", padx=(T.S(6), T.S(0)))
        self._apply_logo(logo_holder)

        # 左上角的版本号 —— 放在 **LOGO 左边**，让它真正落在最左上。
        #
        # ⚠️ 消息里常说的"左上角"就是这里：顶栏最左、LOGO 之前。
        # 先 pack 的靠左，所以这一句必须在 logo_holder.pack 之前才排得出去。
        # （我先写成在 LOGO 之后，结果它跑到 LOGO 右边去了。）
        # ⚠️ 颜色**不能用 TEAL**（#3EC9C0）：在深蓝底上对比度只有 2.77:1，
        # 品牌规范明确禁止拿它当文字色。这里用淡一档的蓝白，够读又不抢眼。
        version = tk.Label(header, text=APP_VERSION, bg=T.NAVY, fg="#9DC0E0",
                           font=T.pick_font(9))
        version.pack(side="left", padx=(T.S(14), T.S(8)))
        self.lbl_version = version
        tip.attach(version,
                   f"当前版本 {APP_VERSION}。\n"
                   "beta 表示还在测：主流程能用，个别功能还没全通。")

        # 重新 pack 一次 LOGO，把它排到版本号**右边**。
        # Tk 的 pack 是"先来先占位"，所以要重排就得 forget 再 pack。
        logo_holder.pack_forget()
        logo_holder.pack(side="left", padx=(T.S(0), T.S(6)))

        # 最右边：设置（齿轮）+ 更多（⋯）。都用自绘按钮，风格统一。
        more = T.FlatButton(header, "⋯", self._show_menu, width=T.S(42), height=T.S(32),
                            bg=T.NAVY, fill=T.NAVY_SOFT, hover="#2A5E95")
        more.pack(side="right", padx=(T.S(0), T.S(6)))
        tip.attach(more, "打开工程、保存工程、看快照和输出目录、关于、退出。")

        self.btn_settings = T.FlatButton(header, "⚙", self.on_settings,
                                         width=T.S(42), height=T.S(32), bg=T.NAVY,
                                         fill=T.NAVY_SOFT, hover="#2A5E95",
                                         font=T.pick_font(12))
        self.btn_settings.pack(side="right", padx=(T.S(0), T.S(6)))
        tip.attach(self.btn_settings,
                   "设置。东西存哪儿、界面多大，都在这儿改。\n"
                   "成片、工程文件、渲染快照，三处位置可以分开指。")

        buttons = tk.Frame(header, bg=T.NAVY)
        buttons.pack(side="right", padx=T.S(8))
        # 「组装为工程」放在「开始渲染」**左边**（pack side=left 先来的靠左）。
        # 它是**渲染之前的另一条出口**：不渲片，只把这一套素材组装成一份
        # Blender 工程交给你自己改（改完能导回来接着渲）。
        #
        # ⚠️ 必须**先 pack** 才排在最左 —— Tk 的 pack 是先来先占位。
        self.btn_assemble = T.FlatButton(
            buttons, "组装为工程", self.on_assemble, width=T.S(104),
            height=T.S(34), bg=T.NAVY, fill="#3A5A82", hover="#4A6E9C")
        self.btn_assemble.pack(side="left", padx=T.S(4))
        tip.attach(self.btn_assemble,
                   "不渲片，只把现在的素材组装成一份 Blender 工程（.blend）。\n"
                   "角色、动作、表情、镜头、舞台全在里面，可以直接在 Blender 里\n"
                   "调材质 / 灯光 / 相机；改完存盘，拖回「角色模型」卡就能接着渲。\n"
                   "想省事就直接「开始渲染」——那一步会自己装配，不用先按这个。")
        self.btn_start = T.FlatButton(buttons, "开始渲染", self.on_start, width=T.S(100),
                                      bg=T.NAVY, fill=T.TEAL, hover=T.TEAL_DEEP,
                                      fg="#08303A")
        self.btn_start.pack(side="left", padx=T.S(4))
        tip.attach(self.btn_start,
                   "整片开始渲。素材没放齐会先告诉你缺哪样。\n"
                   "要停就按「终止」，已经渲好的那几批不会白做。")
        self.btn_pause = T.FlatButton(buttons, "暂停", self.on_pause, width=T.S(72),
                                      bg=T.NAVY, fill="#2A5E95", hover=T.NAVY_SOFT)
        self.btn_pause.pack(side="left", padx=T.S(4))
        tip.attach(self.btn_pause,
                   "暂停。再按一次继续，已经渲好的不会重来。")
        # 「终止」原来填的是砖红 #8E3B3B —— 一屏里最"凶"的元素，很尖锐。
        # 改成**降饱和的暗红**，只有在真的要终止时才有存在感。
        self.btn_stop = T.FlatButton(buttons, "终止", self.on_cancel, width=T.S(72),
                                     bg=T.NAVY, fill="#6B4141", hover="#7E4E4E")
        self.btn_stop.pack(side="left", padx=T.S(4))
        tip.attach(self.btn_stop,
                   "这一轮不渲了。正在渲的那批作废，已经渲好的留着，\n"
                   "下次开始会自动跳过。")

        tk.Label(header, textvariable=self.vars["clock"], bg=T.NAVY, fg="#FFFFFF",
                 font=T.pick_font(15, bold=True)).pack(side="right", padx=(T.S(22), T.S(4)))

        # ── 界面缩放 ──
        # 用下拉而不是 +/- 按钮：档位一目了然，也不会被连点按坏。
        # 换档会**整个重建界面**（Tk 没有整体缩放开关，尺寸是建控件时算死的），
        # 所以刻意只给几档、不做连续滑块 —— 连续拖动会一直重建，很卡。
        zoom_box = tk.Frame(header, bg=T.NAVY)
        zoom_box.pack(side="right", padx=(T.S(10), T.S(6)))
        self.zoom_caption = tk.Label(zoom_box, text="界面", bg=T.NAVY, fg="#9FC0DE",
                                     font=T.pick_font(9))
        self.zoom_caption.pack(side="left", padx=(0, T.S(6)))
        self.zoom_select = Select(zoom_box, self.zoom_var,
                                  [f"{int(z * 100)}%" for z in T.ZOOM_LEVELS],
                                  width=T.S(70), height=T.S(28), bg=T.NAVY,
                                  command=self._on_zoom, font_size=9)
        self.zoom_select.pack(side="left")
        tip.attach(self.zoom_select,
                   "整个界面放大缩小。它跟系统的显示缩放是叠加的，\n"
                   "两个都往大调会过大。")

        # MIKU 味的小点缀：几个音符。
        # 用**预渲染的抗锯齿 PNG**（Tk 画曲线没有抗锯齿，直接用 Canvas 画会带台阶）。
        notes = tk.Canvas(header, width=T.S(58), height=T.S(32), bg=T.NAVY,
                          highlightthickness=0)
        notes.pack(side="right", padx=(T.S(0), T.S(10)))
        if ui_assets.blit(notes, "deco_notes_header.png", 29, 16) is None:
            # 图丢了就退回自绘，只是会糙一点
            T.draw_note_cluster(notes, 12, 20, 16, "#5FD6CE")
            T.draw_note_cluster(notes, 34, 11, 11, T.BEIGE, gap=1.4)

        # ⚠️ 这里**不再重复产品名**。名字已经在自绘标题栏上了，再写一遍就是
        # 两条标题叠在一起，是最典型的割裂感。顶栏只留一句流程说明。
        # 顶栏这五个词是**给用户看的**，不是内部阶段名。
        # 原来的"整理素材 → 预热 → 分批渲染 → 合并 → 出片"像产品流程图：
        # "合并"合的是什么、"出片"算什么词，用户看不懂。
        #
        # ⚠️ 但顶栏宽度很紧：长一点的版本量出来 470px、可用 476px，
        # 只剩 6px 余量，窗口一窄就折行。所以每步压到 2~4 个字。
        flow = tk.Label(header, text="导入素材 → 预热 → 分段渲染 → 合并 → 出片",
                        bg=T.NAVY, fg="#9FC0DE", font=T.pick_font(10))
        flow.pack(side="left", padx=T.S(18))
        tip.attach(flow,
                   "这条视频要走的五步：\n"
                   "导入素材 → 预热 → 分段渲染 → 合并 → 出片。\n"
                   "「预热」= 开头先空放一会儿，让头发和裙摆别抖。")

    def _apply_logo(self, canvas: tk.Canvas) -> None:
        """把 LOGO 画进顶栏那块画布，并顺带设好窗口 / 任务栏图标。

        图挂了不能让程序起不来：退回自绘徽标（E-3.x 健壮性）。

        ⚠️ **画布必须是 pack 过的**。这块 canvas 原来是 1×1 且**没有 pack**，
        只有"设窗口图标"这个副作用真的生效了；画上去的图 Tk 根本不会绘制
        （未映射的 canvas 不渲染内容）。现象是"顶栏左边空着"，而代码看着完全正常 ——
        从截图上一眼没看出来，靠**数像素**才发现（``ui_v5`` 那片区域有 624 个
        非底色像素，之后的版本全只有 63 个，也就是只剩流程文字）。

        ⚠️ 坐标是**画布局部**的。画布按品牌规范留了 12% 的白边，
        所以图标画在画布正中，不是画布左上角。
        """
        try:
            # ⚠️ **不要用 subsample 缩 LOGO**：Tk 的 subsample 是最近邻，
            # 512→40 会把圆边啃出一圈锯齿。用按显示尺寸原生渲染好的 logo_40.png。
            source = ui_assets.image("logo_40.png")
            if source is None:
                raise tk.TclError("缺少 logo_40.png")
            self._logo_source = source # 留引用，防 GC
            self._logo_shown = source
            canvas.create_image(T.S(26), T.S(24), image=self._logo_shown)
            try:
                ico = ui_assets.image("logo_96.png") or source
                self._window_icon = ico
                self.root.iconphoto(True, ico)
                self.root.iconbitmap(default=str(ASSETS / "logo.ico"))
            except tk.TclError:
                pass
        except tk.TclError as exc:
            self._log(f"LOGO 加载失败，退回自绘徽标：{exc}", "warn")
            T.draw_emblem(canvas, T.S(26), T.S(24), T.S(17))

    def _force_window_icon(self) -> None:
        """用 Win32 直接给窗口设图标。

        ⚠️ Tk 的 ``iconphoto`` / ``iconbitmap`` 在 Windows 上**经常不生效**
        （特别是 ``pythonw`` 启动、窗口已经映射之后），标题栏会退回系统默认图标 ——
        用户截图里那个"通用方块图标"就是这么来的。

        ``LoadImageW`` + ``WM_SETICON`` 是唯一稳的做法：直接从 .ico 载入指定尺寸的
        图标句柄，再塞给窗口。大/小两个尺寸都要设，任务栏用的是大的那个。
        """
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.LoadImageW.argtypes = [
                wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                ctypes.c_int, ctypes.c_int, wintypes.UINT]
            user32.LoadImageW.restype = wintypes.HANDLE

            image_icon, load_from_file = 1, 0x0010
            wm_seticon, icon_small, icon_big = 0x0080, 0, 1
            hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            path = str(ASSETS / "logo.ico")
            for kind, edge in ((icon_small, 16), (icon_big, 32)):
                handle = user32.LoadImageW(None, path, image_icon, edge, edge,
                                           load_from_file)
                if handle:
                    user32.SendMessageW(hwnd, wm_seticon, kind, handle)
        except (ImportError, AttributeError, OSError) as exc:
            self._log(f"设置窗口图标失败（不影响功能）：{exc}", "warn")

    def _schedule(self, job: str, delay_ms: int, callback) -> None:
        """排一个延迟任务，并**保证旧的那个先取消掉**。

        ⚠️ 直接写 ``self._xxx_job = self.root.after(...)`` 是错的：
        同一个槽位被排第二次时，前一个句柄就丢了、永远取消不掉。
        ``<Map>`` 那种每次显示都触发的回调尤其容易漏一堆 ——
        它们会在窗口销毁后依次执行，往 stderr 刷
        ``invalid command name "..._polish_title_bar"``。

        关窗时 :meth:`_shutdown` 只认这些槽位，所以**必须**走这里排。
        """
        previous = getattr(self, job, None)
        if previous is not None:
            try:
                self.root.after_cancel(previous)
            except (tk.TclError, ValueError):
                pass
        setattr(self, job, self.root.after(delay_ms, callback))

    def _on_window_mapped(self, _event=None) -> None:
        """窗口每次被映射（首次显示、从最小化恢复）都重设一遍 DWM 属性。

        DWM 的那些设置会在窗口映射/还原时被重置，只调一次不够 ——
        最小化再恢复之后标题栏会悄悄变回系统浅色，这种"偶发"最难查。
        延后一拍再设：``<Map>`` 触发时窗口可能还在成型中。

        ⚠️ **拖放也要在这里补装一次**：它需要外层 wrapper 窗口，
        而 wrapper 在 ``_build_ui`` 那一刻**还不存在**（``GetParent`` 返回 0）。
        ``_install_dnd`` 内部有"装齐了就不再装"的判断，重复调是安全的。
        """
        self._schedule("_titlebar_map_job", 60, self._polish_title_bar)
        self._schedule("_dnd_map_job", 80, self._install_dnd)

    def _polish_title_bar(self) -> None:
        """把系统标题栏设成品牌深蓝 + 白字，让它跟自绘顶栏连成一片。

        **做法：不做客户区延伸，只是给系统标题栏指定颜色。**
        这一条是绕了五轮才定下的，别再改回去，理由见 :meth:`_paint_caption`。

        ⚠️ 这里是走过弯路的，别再退回去：
        最初用 ``overrideredirect(True)`` 把系统窗口框整个去掉、自己画标题栏，
        以为那样才能"一体化"。代价是拖边改大小、双击最大化、系统贴边、
        多屏 DPI、任务栏按钮**全部要自己重写** —— 补了几样，剩下的永久缺失
        （用户一句"为什么抖音 B站 能拖边"就问穿了）。

        之后又试了 ``DwmExtendFrameIntoClientArea``（抖音看起来用的那个），
        但延伸之后 DWM 会在那块上叠自己的渐变/强调色，**颜色不受我们控制**，
        标题栏下方必定留一条异色横带。五种组合的对照图见
        ``work/compare_caption_views.py``。

        需要 Windows 11（22000+）。老系统上这些调用会被忽略，只影响观感，不影响功能。
        """
        # ⚠️ 这个方法是延迟调用的，可能在窗口已经销毁之后才轮到它 ——
        # 测试里反复 destroy 就会刷一屏 ``invalid command name ...polish_title_bar``。
        # 先确认窗口还在，再干活。
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return

        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            dwm = ctypes.windll.dwmapi

            # ⚠️ **必须声明 argtypes**：不声明的话 ctypes 会把 64 位的 HWND 当成
            # 32 位 int 传进去，高 32 位被截断，调用就**静默失败** ——
            # 现象是"看起来什么都没发生"，非常难查。这个坑踩过。
            dwm.DwmSetWindowAttribute.argtypes = [
                wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            dwm.DwmSetWindowAttribute.restype = ctypes.c_long

            hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()

            dark = ctypes.c_int(1)
            round_corner = ctypes.c_int(2)
            for attribute, value, name in (
                    (20, dark, "暗色模式"),
                    (33, round_corner, "窗口圆角")):
                result = dwm.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
                if result != 0:
                    self._log(f"标题栏属性「{name}」未生效（错误码 {result}）", "warn")
            # ⚠️ **不要设 DWMWA_NCRENDERING_POLICY (2)，也不要做客户区延伸。**
            # 那两个都试过，结论见 _paint_caption 的说明与对照图：
            # 延伸必定在标题栏下方留一条不受控的异色横带。
            # 现在走的是"不延伸 + 给系统标题栏指定颜色"，颜色零误差、无分割。
            self._paint_caption(hwnd, dwm)
        except (ImportError, AttributeError, OSError) as exc:
            self._log(f"标题栏处理失败（不影响功能）：{exc}", "warn")

    def _paint_caption(self, hwnd, dwm) -> None:
        """把系统标题栏设成品牌深蓝 + 白字。

        ⚠️ **不要做"客户区延伸"**（``DwmExtendFrameIntoClientArea``）。
        那条路我试了五轮，结论是它**必定**在标题栏下方留一条异色横带：

        ================================ ==================================
        不延伸 + 标题栏深蓝（**现在用的**） 标题栏和顶栏连成一片，无分割 ✅
        延伸 39 + 标题栏深蓝 中间横一条**系统强调蓝** ``#1872CE``
        延伸 39 + 标题栏不指定色 标题栏灰黑、顶栏发亮，还是两截
        延伸 39 + 标题栏纯黑 标题栏纯黑，跟深蓝顶栏有硬边
        ================================ ==================================

        原因是延伸之后 DWM 会在那块区域上叠自己的标题栏渐变/强调色，
        颜色不受我们控制（对照图见 ``work/compare_caption_views.py`` 出的图）。

        所以正解是**反过来**：不做延伸，让系统按我们指定的颜色画它自己的标题栏。
        `DWMWA_CAPTION_COLOR` (35) 在没有延伸时**完全生效** ——
        早先文档里写它"返回 0 但颜色不生效"，那是当时**没做延伸却别的属性打架**
        留下的错误结论，已纠正。现在实测：设 ``#0C3967``，屏幕上量到的就是
        ``(12,57,103)``，零误差（``work/calibrate_caption_color.py``）。

        代价是标题栏高度由系统定（约 26 设计像素），跟我们 52 设计像素的顶栏不等高。
        但**颜色连着，看着就是一片**，比"等高但有条亮带"好得多。
        """
        import ctypes

        # COLORREF 是 0x00BBGGRR —— 跟网页的 #RRGGBB 是**反的**。
        # 品牌深蓝 #0C3967 → 0x0067390C，写反了就是另一种蓝。
        navy = ctypes.c_uint(0x0067390C)
        white = ctypes.c_uint(0x00FFFFFF)
        for attribute, value, name in (
                (35, navy, "标题栏底色"),
                (36, white, "标题栏文字"),
                (34, navy, "窗口边框")):
            result = dwm.DwmSetWindowAttribute(hwnd, attribute,
                                               ctypes.byref(value),
                                               ctypes.sizeof(value))
            if result != 0:
                self._log(f"标题栏属性「{name}」未生效（错误码 {result}）", "warn")

    # 这里原来还有一个 _extend_frame_into_caption()（把客户区延伸进标题栏）。
    # **已经删掉，别再写回来** —— 它必定在标题栏下方留一条不受控的异色横带。
    # 五种组合的实测对照见 _paint_caption 的表格，对照图用
    # ``work/compare_caption_views.py`` 生成。现在走的是"不延伸 + 给系统标题栏指定颜色"。

    # ------------------------------------------------------------------ 主体
    def _build_body(self) -> None:
        """整页用 **pack** 而不是 grid 分左右两栏 —— 这是踩出来的选择。

        ⚠️ 想让侧栏**固定宽度**，``grid`` 做不到：给侧栏 Frame 设了 ``width``
        再加 ``grid_propagate(False)``，实测列照样被内容撑到 772 实际像素
        （助手卡、参数行整条错位）。``pack`` + ``pack_propagate(False)``
        才是可靠的：侧栏就 450 实际像素，主区吃掉剩下的。
        """
        body = tk.Frame(self.root, bg=T.CREAM)
        body.pack(fill="both", expand=True, padx=T.S(BODY_PAD_X),
                  pady=T.S(BODY_PAD_Y))
        self._build_sidebar(body)
        self._build_main(body)

    # ------------------------------------------------------------- 侧栏
    def _panel(self, parent, title: str, accent: str = T.NAVY, *,
               foldable: bool = False, start_open: bool = True,
               summary_provider=None) -> tk.Frame:
        """带标题条的小白卡。``foldable=True`` 时标题条右侧多一个折叠箭头。

        折叠态会在标题行显示一行摘要 —— 收起来也得让人看到关键信息。
        """
        wrapper = tk.Frame(parent, bg=T.CARD, highlightthickness=1,
                           highlightbackground=T.BORDER)
        head = tk.Frame(wrapper, bg=T.CARD)
        head.pack(fill="x", padx=T.S(12), pady=(T.S(9), T.S(5)))
        bar = tk.Canvas(head, width=T.S(3), height=T.S(13), bg=T.CARD, highlightthickness=0)
        bar.create_rectangle(0, 0, 3, 13, fill=accent, outline="")
        bar.pack(side="left")
        tk.Label(head, text=title, bg=T.CARD, fg=T.TEXT,
                 font=T.pick_font(11, bold=True)).pack(side="left", padx=T.S(7))

        summary = tk.Label(head, text="", bg=T.CARD, fg=T.TEXT_SUB,
                           font=T.pick_font(9), anchor="w", justify="left")
        inner = tk.Frame(wrapper, bg=T.CARD)
        wrapper.body = inner # type: ignore[attr-defined]
        wrapper.summary = summary # type: ignore[attr-defined]
        wrapper.opened = start_open # type: ignore[attr-defined]
        wrapper.summary_provider = summary_provider # type: ignore[attr-defined]
        self._panels.append(wrapper)

        if foldable:
            summary.pack(side="left", padx=T.S(6), fill="x", expand=True)
            chevron = T.FlatButton(head, "▾" if start_open else "▸",
                                   lambda: self._toggle_panel(wrapper), width=26,
                                   height=22, bg=T.CARD, fill="#FFFFFF",
                                   hover=T.BEIGE_SOFT, fg=T.NAVY, outline=T.BORDER,
                                   font=T.pick_font(9))
            chevron.pack(side="right")
            wrapper.chevron = chevron # type: ignore[attr-defined]
            if start_open:
                inner.pack(fill="both", expand=True, padx=T.S(12), pady=(T.S(0), T.S(12)))
            else:
                self._update_panel_summary(wrapper)
        else:
            inner.pack(fill="both", expand=True, padx=T.S(12), pady=(T.S(0), T.S(12)))
        return wrapper

    def _toggle_panel(self, panel: tk.Frame) -> None:
        """展开 / 收起一个折叠面板。

        ⚠️ 收起时**要清掉这一卡的滚轮绑定**，展开时**重新绑上**。
        Tk 的 ``<MouseWheel>`` 只发给鼠标底下那个控件、不冒泡给外层 Canvas，
        所以展开后新出现的那些控件必须补绑，否则鼠标停在它们上面就滚不动
        （面板铺满整条侧栏，等于完全滚不动）。
        """
        panel.opened = not panel.opened # type: ignore[attr-defined]
        if panel.opened: # type: ignore[attr-defined]
            panel.body.pack(fill="both", expand=True, padx=T.S(12), pady=(T.S(0), T.S(12))) # type: ignore[attr-defined]
            panel.chevron.set_text("▾") # type: ignore[attr-defined]
            # 展开出来的是新控件，补绑滚轮 + 更新滚动范围
            self._refresh_sidebar_scroll()
        else:
            panel.body.pack_forget() # type: ignore[attr-defined]
            panel.chevron.set_text("▸") # type: ignore[attr-defined]
        self._update_panel_summary(panel)
        # 面板高度变了，滚动范围和指示条都得跟着更新
        self.root.after_idle(self._on_sidebar_content)

    def _update_panel_summary(self, panel: tk.Frame) -> None:
        getter = getattr(panel, "summary_provider", None)
        if getter is None:
            return
        text = "" if panel.opened else (getter() or "") # type: ignore[attr-defined]
        panel.summary.configure(text=text) # type: ignore[attr-defined]

    # ------------------------------------------------------- 磁盘占用预估
    def _int_var(self, key: str, fallback: int) -> int:
        """读一个整数输入框；填得不对就用兜底值（预估而已，不能因此报错）。"""
        try:
            text = str(self.vars[key].get()).strip()
            return int(text) if text else fallback
        except (KeyError, ValueError):
            return fallback

    def _planned_frames(self) -> tuple[int, bool]:
        """这次大概渲多少帧。

        返回 ``(帧数, 是不是瞎估的)``。**只用于估磁盘** ——
        估不准没关系，但不能因此把程序搞崩，也不能因为估不准就不告诉用户。
        """
        start = self._int_var("frame_start", 0)
        end = self._int_var("frame_end", 0)
        if end > start:
            return end - start, False
        return FALLBACK_FRAMES, True

    def _disk_text(self, codec: str) -> str:
        frames, guessed = self._planned_frames()
        target = (self.vars["output_dir"].get()
                  or self.vars["work_dir"].get())
        text = budget.describe(frames, self._int_var("width", 2560),
                               self._int_var("height", 1440),
                               alpha_codec=codec, path=target)
        if guessed and text:
            text = f"（还没定时长，先按 {frames} 帧估）{text}"
        return text

    def _current_codec(self) -> str:
        """当前选的编码器内部键。变量里存的就是键，不认的值退回默认。"""
        value = self.alpha_codec.get()
        if value in ALPHA_CODEC_CHOICES:
            return value
        return budget.DEFAULT_ALPHA_CODEC

    def _transparent_state(self) -> tuple[bool, str, str]:
        """「渲染背景」这一项的**四态**：``(可选, 该显示的开/关, 提示)``。

        ⚠️ **控件语义是反的，别记错**：
           「渲染背景」= **开** → 照背景/舞台渲（**不出**透明）
           「渲染背景」= **关** → 出透明成片

        规则（用户 2026-10-17 定，与 ``core/project`` 那套互为镜像）：

        ============ ============ ================================
        角色 舞台 这一项
        ============ ============ ================================
        有 无 **关**（= 出透明），可选
        无 有 **开**（照舞台渲），可选
        无 无 **开**，**变灰**（没东西可渲）
        有 有 **开**（默认照背景渲），可选
        ============ ============ ================================

        ``.blend`` 那条路一律不可选（不碰用户的工程，设计取舍）。
        """
        # ⚠️ 这里读**界面上的值**而不是 ``self._collect_project()``：
        # 这个函数在刷新面板时被调，每敲一个字都去构造 Project 太重，
        # 而且会和"参数还没填完"的状态打架。
        has_character = bool(self.vars["model"].get().strip())
        has_stage = bool(self.vars["stage"].get().strip())
        has_blend = bool(self.vars["blend"].get().strip())

        # 用户定的规则：**默认关，加了背景才是开**。
        # 所以先看有没有舞台/背景，没背景就一律显示"关"。
        if has_stage:
            shown = "开" if self._current_transparent() else "关"
            if not getattr(self, "_transparent_user_set", False):
                shown = "开" # 加了背景 → 自动开
            if has_blend:
                return False, shown, BLEND_TIP
            if not has_character:
                return True, shown, STAGE_ONLY_TIP
            return True, shown, STAGE_TIP

        if has_blend:
            # 没有舞台：.blend 自带什么就是什么，这一项显示"关"且不可选
            return False, "关", BLEND_TIP
        if not has_character:
            return False, "关", NOTHING_TO_RENDER_TIP

        if getattr(self, "_transparent_user_set", False):
            shown = "开" if self._current_transparent() else "关"
        else:
            shown = "关" # 没舞台 → 默认关
        return True, shown, TRANSPARENT_TIP

    def _current_transparent(self) -> bool:
        """界面上「渲染背景」当前是不是开着。"""
        return self.transparent.get() != "关"

    def _on_transparent(self, _value=None) -> None:
        # 能点到这个控件，就说明这一刻它是可选的（灰着点不动），
        # 所以此刻的取值就是他本人的意思 —— 记下来，往后别再用默认规则盖掉。
        self._transparent_user_set = True
        self._refresh_panels()

    def _disk_ok(self, project: Project) -> bool:
        """渲前查一次盘。**不够就拦住**，返回 False。

        原来的 `budget` 只会"估算 + 提示"，没有任何拦截 —— 大工程真的会在
        写到一半时把盘写满（透明那条路尤其狠：2K 三分钟 QTRLE 能到 20GB 以上）。
        所以真正开始前必须过这一道。

        设计上只拦**按上限算都不够**的情况（``DiskCheck`` 用的是 ``needed`` = 上限），
        这样宁可偶尔多问一句，也不会白渲几个小时后失败。

        :return: ``True`` 表示可以开跑。用户选"照样渲"时也是 ``True``。
        """
        frames, guessed = self._planned_frames()
        codec = self._current_codec() if project.effective_transparent() else ""
        size = budget.estimate_total(
            frames, self._int_var("width", 2560), self._int_var("height", 1440),
            alpha_codec=codec or budget.DEFAULT_ALPHA_CODEC,
            # ⚠️ 不再有"额外那条 MP4"了（用户要求选什么就出什么），
            # 所以估算也只算一条轨 —— 否则会把占用报成两倍。
            also_mp4=False,
        )
        target = self.vars["output_dir"].get() or self.vars["work_dir"].get()
        check = budget.preflight(target, size)
        if check.ok:
            return True

        head = ("盘可能不够，别渲到一半卡住。\n\n" if not guessed
                else f"还没定时长，先按 {frames} 帧估：盘可能不够。\n\n")
        answer = messagebox.askyesno(
            "磁盘空间可能不够",
            f"{head}{check.message()}\n\n"
            "· 选「是」= 我知道，照样开始\n"
            "· 选「否」= 我先把输出目录换到别的盘，或改成体积小的格式",
            icon="warning", default="no",
        )
        self._log(("磁盘预检不通过，用户选择照样开始：" if answer else
                   "磁盘预检不通过，用户取消：") + check.message(), "warn")
        return bool(answer)

    def _codec_intro(self, codec: str) -> str:
        """取某个格式的完整介绍（用于悬停展开）。"""
        return CODEC_INTRO.get(codec, "")

    def _on_alpha_codec(self, codec: str) -> None:
        """用户选了透明成片的格式 —— 更新悬停介绍，并**立刻把代价告诉他**。

        参数是**内部键**（``Select`` 的 ``command`` 传回来的就是值本身）。

        用户的原话：磁盘是用户自己的，别替他省，但要提前算出来告诉他。
        所以这里不拦、不建议，就是**弹一个窗把数摆清楚**；
        盘不够时 ``describe()`` 里会自己带上"还差多少、怎么办"。
        """
        self._update_estimate_card()
        self._refresh_panels()
        # 悬停提示要换成**这一种**的介绍，不能还显示上一种的
        self._refresh_format_card()
        text = self._disk_text(codec)
        if not text:
            text = "还看不出要占多少。先在「参数」里把清晰度和时长填完，再回来选。"
        messagebox.showinfo(f"选了「{budget.CODEC_LABELS.get(codec, codec)}」", text)

    # ── 首次运行：把外部依赖一次准备好 ───────────────────────────────
    def _ffmpeg_missing(self) -> bool:
        try:
            from komaflow.media.ffmpeg import find_ffmpeg
            find_ffmpeg()
            return False
        except Exception:      # noqa: BLE001
            return True

    def _env_summary(self) -> str:
        """环境面板收起时显示什么 —— 一句话说清"能不能开工"。"""
        probed = getattr(self, "_blender_probed", [])
        base = env_picker.summary_of(probed, self.vars["blender"].get())
        # 这一行只有 ~350px：去掉版本号，给 FFmpeg 状态留位置，否则会被界面截成
        # "✓ 可用（Blender 5.2.3 LTS）· FFr" 这种半截话
        if base.startswith("✓ 可用"):
            base = "✓ 可用"
        base += " · FFmpeg " + ("✗" if self._ffmpeg_missing() else "✓")
        return base

    def _first_run_setup(self) -> None:
        """第一次打开就检查外部依赖。

        只处理**能自动解决**的那一项：FFmpeg（拼视频/编码要用）。
        Blender 与 mmd_tools 必须用户自己装，这里只提示，不代劳。
        处理完写 ``settings.ffmpeg_setup_done``，以后不再弹。
        """
        if getattr(self.settings, "ffmpeg_setup_done", False):
            return
        if not self._ffmpeg_missing():
            self.settings.ffmpeg_setup_done = True
            self._save_settings_quietly()
            return

        answer = messagebox.askyesno(
            "首次使用：准备 FFmpeg",
            "要把渲染结果拼成视频（MP4 / MOV），需要一个叫 FFmpeg 的小工具。\n"
            "它不随本软件分发，我可以现在替你下载（约 90 MB，来自第三方构建站，"
            "不是 FFmpeg 项目官方发布）。\n\n"
            "选「否」也能用：输出格式里选「PNG 图片序列」就不需要它。\n"
            "以后想装：下次打开程序会再问一次；你自己装好 FFmpeg 也行，程序会自动找到它。",
        )
        if not answer:
            # A11：原来不管用户答什么都把 ffmpeg_setup_done 置 True，
            # 于是"下次打开会再问"这句承诺是假的。只有真的处理过才记。
            self._log("已跳过 FFmpeg 下载（PNG 图片序列不需要它）；下次启动会再问")
            return
        self.settings.ffmpeg_setup_done = True
        self._save_settings_quietly()
        self._download_ffmpeg_async()

    def _save_settings_quietly(self) -> None:
        try:
            self.settings.save()
        except Exception:      # noqa: BLE001
            pass

    def _download_ffmpeg_async(self) -> None:
        from komaflow.media import ffmpeg_fetch
        from komaflow.media.ffmpeg import ENV_FFMPEG
        import threading as _th

        self._log("正在下载 FFmpeg（约 90 MB）…")

        def job() -> None:
            def progress(url: str, done: int, total: int) -> None:
                pct = int(done / total * 100) if total else 0
                self.root.after(0, self._log,
                                f"  FFmpeg {pct}%  ({done // 1048576}/"
                                f"{max(total, 1) // 1048576} MB)")
            try:
                target = ffmpeg_fetch.fetch(on_progress=progress, on_note=self._log)
                os.environ[ENV_FFMPEG] = str(target)
                self.root.after(0, self._log, f"FFmpeg 已就绪：{target}")
                self.root.after(0, self._refresh_panels)
            except Exception as exc:      # noqa: BLE001
                self.root.after(0, self._log, f"FFmpeg 下载失败：{exc}")

        _th.Thread(target=job, daemon=True).start()


    def _entry(self, parent, var, width: int = 18) -> tk.Entry:
        return tk.Entry(parent, textvariable=var, width=width, relief="flat",
                        bg="#FFFFFF", fg=T.TEXT, font=T.pick_font(10),
                        highlightthickness=1, highlightbackground=T.BORDER,
                        highlightcolor=T.NAVY, insertbackground=T.NAVY,
                        disabledbackground="#F2EEE8", disabledforeground=T.TEXT_SUB)

    def _label(self, parent, text: str, *, sub: bool = False, bg: str = T.CARD,
               width: int = 0) -> tk.Label:
        return tk.Label(parent, text=text, bg=bg, fg=T.TEXT_SUB if sub else T.TEXT,
                        font=T.pick_font(9 if sub else 10), width=width, anchor="w")

    def _build_sidebar(self, body: tk.Frame) -> None:
        """侧栏：助手卡 →（**可滚动**）环境 / 渲染格式 / 参数 →（**贴底**）输出预估。

        ⚠️ **顺序有讲究**：``输出预估`` 必须**先** pack 到 ``side="bottom"``，
        它才会永远贴在侧栏最底部。之前它是挤在参数面板里的一行小字，
        而参数面板 ``expand=True`` 一路撑到底 —— 结果就是"预估跟着参数跑"，
        窗口一拉高，底部反而空出一块。

        ⚠️ **中间那一段必须能滚**。展开「更多设置」之后总高度会超过窗口，
        底部内容（取景范围那些）直接被切掉、还够不着 ——
        用户截图里"取景范围"只露出半截字就是这么来的。
        所以套一层 ``Canvas`` + 竖向滚动条，内容比可视区高时就能滚。
        助手卡和输出预估**不参与滚动**：一个要一直看得见，一个是贴底的结论。
        """
        column = tk.Frame(body, bg=T.CREAM, width=T.S(300))
        # ⚠️ 侧栏宽度用 ``pack`` + ``pack_propagate(False)`` 钉死 ——
        # ``grid`` 那套（width + grid_propagate）实测拦不住内容撑宽。
        # 钉死之后 100%/125%/150% 三档都是设计宽度。
        column.pack_propagate(False)
        column.pack(side="left", fill="y", padx=(0, T.S(GAP_X)))

        # ── 输出预估：**先放**，贴底 ──
        self._build_estimate_card(column)

        # ── 助手卡：固定在顶部，不跟着滚 ──
        # 她的 Q 版头像会跟着状态换脸。侧栏原来只有面板 + 一大片空白，
        # 空白本身就显"半成品"；放张会变脸的助手在这里，既不空也有人味。
        self.helper = HelperCard(column, bg=T.CREAM)
        self.helper.pack(side="top", fill="x", pady=(0, T.S(12)))

        # ── 可滚动区 ──
        #
        # ⚠️ **不用 ``tk.Scrollbar``**，两个原因：
        # 1. 它的原生外观（灰底方角 + 系统箭头）跟这套卡片配色完全不搭
        # 2. 侧栏宽度是**钉死**的（``column.pack_propagate(False)``），
        # Canvas 先 ``fill="both"`` 就把 450 全占了，滚动条分不到宽度、
        # 变成 1×1 且**永远不显示**（实测 ``winfo_ismapped()=0``）
        #
        # 改成**画一根细指示条**：位置和长度按"看到多少 / 总共多少"算，
        # 颜色用主题色，跟卡片是一套。不可点，纯指示 + 滚轮操作。
        shell = tk.Frame(column, bg=T.CREAM)
        shell.pack(side="top", fill="both", expand=True)
        self._sidebar_canvas = tk.Canvas(shell, bg=T.CREAM, highlightthickness=0,
                                         bd=0)
        self._sidebar_canvas.pack(fill="both", expand=True)

        holder = tk.Frame(self._sidebar_canvas, bg=T.CREAM)
        self._sidebar_holder = holder
        self._sidebar_window = self._sidebar_canvas.create_window(
            (0, 0), window=holder, anchor="nw")
        holder.bind("<Configure>", lambda _e: self._on_sidebar_content())
        # 让内容宽度跟着可视区走（不然窄窗口下会横向溢出）
        self._sidebar_canvas.bind(
            "<Configure>", lambda e: self._on_sidebar_resize(e))

        self._build_env_card(holder)
        self._build_format_card(holder)
        self._build_params_card(holder)

        # ⚠️ 滚轮绑定必须**等上面的卡片都建完**再做。
        # 它是递归绑到每个子控件上的；在这里直接调的话，此刻卡片还是空的，
        # 绑完只有 2/69 个控件有绑定 —— 鼠标停在面板上完全滚不动
        # （用户报的"缩放后没有滚轮，啥也看不见"就是这个）。
        # 放到 ``after_idle``：等这一轮布局跑完，控件都在了再绑。
        self.root.after_idle(self._refresh_sidebar_scroll)

    def _refresh_sidebar_scroll(self) -> None:
        """把滚轮绑定和滚动范围都重刷一遍。

        界面重建、面板展开之后都要调一次 —— 那两种情况都会产生**新控件**，
        而滚轮是按控件绑的（Tk 的 ``<MouseWheel>`` 不冒泡），不重绑就滚不动。
        """
        holder = getattr(self, "_sidebar_holder", None)
        if holder is None or not holder.winfo_exists():
            return
        self._bind_wheel(holder)
        self._on_sidebar_content()

    def _on_sidebar_content(self) -> None:
        """可滚动区内容变了（展开面板、切档重建）—— 更新滚动范围并重画指示条。"""
        canvas = getattr(self, "_sidebar_canvas", None)
        if canvas is None:
            return
        try:
            canvas.configure(scrollregion=canvas.bbox("all"))
        except tk.TclError:
            return
        self._redraw_scroll_hint()

    def _on_sidebar_resize(self, event) -> None:
        """可视区尺寸变了 —— 让内容跟着宽度走，并重画滚动指示条。"""
        canvas = getattr(self, "_sidebar_canvas", None)
        if canvas is None:
            return
        canvas.itemconfigure(self._sidebar_window, width=event.width)
        self._redraw_scroll_hint()

    def _redraw_scroll_hint(self) -> None:
        """画（或擦掉）侧栏右边缘那根细指示条。

        算的是"可视比例 + 滚动位置"：内容装得下就不画，
        装不下才在右边竖一根，长度反映可见比例、位置反映滚到哪儿了。
        """
        canvas = getattr(self, "_sidebar_canvas", None)
        holder = getattr(self, "_sidebar_holder", None)
        if canvas is None or holder is None:
            return
        try:
            canvas.delete("scrollhint")
            total = holder.winfo_height()
            visible = canvas.winfo_height()
            if total <= visible or visible <= 0:
                return
            width = canvas.winfo_width()
            bar_w = max(2, T.S(3))
            pad = T.S(2)
            # 长度按可见比例，最矮给 24px，否则内容一多就细成一条线
            length = max(T.S(24), int(visible * visible / total))
            top, _bottom = canvas.yview()
            y = int(top * total)
            y = max(pad, min(y, visible - length - pad))
            canvas.create_rectangle(
                width - bar_w - pad, y,
                width - pad, y + length,
                fill=T.BORDER, outline="", tags="scrollhint")
        except tk.TclError:
            pass

    def _bind_wheel(self, widget) -> None:
        """给整个可滚动区绑滚轮。

        ⚠️ Tk 的滚轮事件**只发给鼠标下面的那个控件**，不会冒泡到外面的 Canvas。
        所以必须**递归绑到每一个子控件**上，否则鼠标停在某个面板上就滚不动
        （面板铺满了整个侧栏，等于完全滚不动）。

        ⚠️ 一次滚 3 格、一格 120：Windows 上 ``event.delta`` 是 120 的倍数，
        Linux 是 4/5，这里只处理 Windows，其余退化成 1 格。

        ⚠️ ``add="+"`` 会**叠加**而不是替换，所以这个方法会被调很多次
        （重建、展开都调）。用 ``bound`` 记一份已经绑过的集合，避免重复绑 ——
        不然一次滚轮会触发好几遍，滚得飞快。

        ⚠️ **必须防环**。``holder`` 是嵌在 ``Canvas`` 里的窗口，而
        ``Canvas.winfo_children()`` 会把**它内部的窗口当成子控件**返回 ——
        于是 ``holder → canvas → holder → …`` 无限递归，
        直接 ``RecursionError``（表现是"只绑上了 1 个控件"，滚轮等于没绑）。
        所以按控件路径名记一份"已访问过"，碰到就停。
        """
        def on_wheel(event) -> None:
            canvas = getattr(self, "_sidebar_canvas", None)
            if canvas is None:
                return
            steps = -1 * int(event.delta / 120) * 3 or -1
            canvas.yview_scroll(steps, "units")
            self._redraw_scroll_hint()

        seen = getattr(self, "_wheel_bound", None)
        if seen is None:
            seen = self._wheel_bound = set()

        def walk(node) -> None:
            key = str(node)
            if key in seen:
                return # 已经绑过（也顺便挡住了环）
            seen.add(key)
            try:
                node.bind("<MouseWheel>", on_wheel, add="+")
            except tk.TclError:
                return
            for child in node.winfo_children():
                walk(child)

        walk(widget)

    def _build_env_card(self, column: tk.Frame) -> None:
        """环境卡（侧栏里只报一句结论，完整的选台界面在设置窗口）。"""
        # ⚠️ 这里**只留一句结论**，完整的选台界面在**设置窗口**里（``env_picker``）。
        # 理由："用哪台 Blender"是设置项，跟"成片存哪儿"一类，
        # 不是每次渲片都要看的东西。搬走之后侧栏清爽了，
        # 而且设置窗口更宽，下拉里能显示完整路径，比挤在 412px 的侧栏里强。
        env = self._panel(column, "环境", accent=T.TEAL, foldable=True,
                          start_open=False, summary_provider=self._env_summary)
        env.pack(side="top", fill="x", pady=(0, T.S(12)))
        box = env.body
        self.env_note = tk.Label(box, text="正在检测…", bg=T.CARD, fg=T.TEXT_SUB,
                                 font=T.pick_font(9), anchor="w", justify="left",
                                 wraplength=T.S(252))
        self.env_note.pack(fill="x")
        go_settings = T.FlatButton(box, "去设置里改…", self.on_settings,
                                   width=T.S(96), height=T.S(28), bg=T.CARD,
                                   fill="#FFFFFF", hover=T.BEIGE_SOFT, fg=T.NAVY,
                                   outline=T.BORDER, font=T.pick_font(9))
        go_settings.pack(anchor="w", pady=(T.S(8), 0))
        tip.attach(go_settings,
                   "换一台 Blender、重新找、或者手动指路径，都在设置窗口里。")

        row = tk.Frame(box, bg=T.CARD)
        row.pack(fill="x", pady=(T.S(10), T.S(2)))
        self._label(row, "输出目录", width=8).pack(side="left", anchor="w")
        self._entry(row, self.vars["output_dir"], 10).pack(side="left", fill="x",
                                                           expand=True)
        T.FlatButton(row, "…", lambda: self._browse_dir("output_dir"), width=T.S(30),
                     height=T.S(26), bg=T.CARD, fill="#FFFFFF", hover=T.BEIGE_SOFT,
                     fg=T.NAVY, outline=T.BORDER,
                     font=T.pick_font(9)).pack(side="left", padx=(T.S(6), 0))

        row = tk.Frame(box, bg=T.CARD)
        row.pack(fill="x", pady=T.S(2))
        name_label = self._label(row, "文件名", width=8)
        name_label.pack(side="left", anchor="w")
        self._entry(row, self.vars["name"], 10).pack(side="left", fill="x", expand=True)
        # 成片默认跟着**动作名**走（用户要求），用户自己改过就不再覆盖
        self.vars["name"].trace_add("write", self._mark_name_touched)
        tip.attach(name_label,
                   "成片叫什么名字（不带扩展名）。\n"
                   "没动过就自动跟着第一个动作文件的名字走；\n"
                   "重名不会覆盖旧成片，会自动加 (2)、(3)。")

        row = tk.Frame(box, bg=T.CARD)
        row.pack(fill="x", pady=(T.S(8), 0))
        for text, command in (("打开工程", self.on_open_project),
                              ("保存工程", self.on_save_project),
                              ("输出目录", self.on_open_output),
                              ("日志", self.on_open_logs)):
            T.FlatButton(row, text, command, width=T.S(62), height=T.S(28), bg=T.CARD,
                         fill="#FFFFFF", hover=T.BEIGE_SOFT, fg=T.NAVY,
                         outline=T.BORDER,
                         font=T.pick_font(9)).pack(side="left", padx=T.S(1))

    def _build_params_card(self, column: tk.Frame) -> None:
        """参数卡：画面方向 / 清晰度 / 帧率 / 渲染背景 /（折叠）更多设置。"""
        params = self._panel(column, "参数", accent=T.TEAL)
        params.pack(side="top", fill="x")
        box = params.body

        def line(parent=None) -> tk.Frame:
            frame = tk.Frame(parent or box, bg=T.CARD)
            frame.pack(fill="x", pady=T.S(3))
            return frame

        row = line()
        label = self._label(row, "画面方向", width=8)
        label.pack(side="left", anchor="w")
        self._option(row, self.vars["orientation"], ORIENTATION_PRESETS,
                     self._on_orientation, 9).pack(side="left")
        tip.attach(label, "要横的还是竖的。选完下面清晰度自动填好。")

        row = line()
        # 「清晰度」标签从 6 收窄到 5：这一行控件多，得给右边的档位下拉让位
        label = self._label(row, "清晰度", width=5)
        label.pack(side="left", anchor="w")
        self._entry(row, self.vars["width"], 6).pack(side="left")
        self._label(row, " × ", sub=True).pack(side="left")
        self._entry(row, self.vars["height"], 6).pack(side="left")
        # 输入框旁边放**档位下拉**：想省事的点档位，想精细的（比如导到某个
        # 特定平台）自己改数字。改数字不会反过来动档位，互不干扰。
        #
        # ⚠️ 宽度要给够。原来传 7，实际可用宽度不够放 ``1080P`` + 箭头，
        # Tk 把它折成两行（``1080`` / ``P``）—— 用户一眼就看出是坏的。
        # 这类"值比预期长"的下拉（4K / 1080P / PNG 图片序列）都要留富余。
        self._option(row, self.vars["resolution"], list(RESOLUTION_PRESETS),
                     self._on_resolution, 9).pack(side="left", padx=(T.S(8), 0))
        tip.attach(label, "要多大。点「2K」这类档位会自动填；\n"
                          "也可以直接改数字，适合有固定要求的场合。")

        row = line()
        label = self._label(row, "帧率", width=6)
        label.pack(side="left", anchor="w")
        self._entry(row, self.vars["fps"], 4).pack(side="left")
        tip.attach(label, "一秒钟几幅画。30 是常规档，别的一般用不上。")

        # ── 渲染背景（关 = 出透明成片）──
        # 语义是**「渲染背景」的反面**：关 → 成片带透明通道（默认）；
        # 开 → 把背景/舞台照原样渲出来。
        #
        # 能不能点、默认值多少，由 _refresh_panels 里的 _transparent_state()
        # 按"有没有角色 / 有没有舞台"四种组合决定（规则写在 core/project）。
        row = line()
        self.transparent_label = self._label(row, "渲染背景", width=8)
        self.transparent_label.pack(side="left", anchor="w")
        self.transparent_option = self._option(
            row, self.transparent, list(ON_OFF), self._on_transparent, 8)
        self.transparent_option.pack(side="left")
        self.transparent_tip = tip.attach(self.transparent_label, TRANSPARENT_TIP)
        tip.attach(self.transparent_option, TRANSPARENT_TIP)

        # ── 折叠区：不常用的设置收起来，界面才清爽 ──
        self._fold_open = False
        self._fold_button = T.FlatButton(box, "▸ 更多设置", self._toggle_fold,
                                         width=T.S(110), height=T.S(28), bg=T.CARD,
                                         fill="#FFFFFF", hover=T.BEIGE_SOFT,
                                         fg=T.NAVY, outline=T.BORDER,
                                         font=T.pick_font(9))
        self._fold_button.pack(anchor="w", pady=(T.S(8), 0))
        self._fold = tk.Frame(box, bg=T.CARD)

        # 折叠区里的说明一律收进悬停提示 —— 常驻的括号尾巴会把行撑破
        # （实测「开头静置」那行 415 > 面板 412），而且平时也没人看。
        row = line(self._fold)
        label = self._label(row, "只渲这一段", width=8)
        label.pack(side="left", anchor="w")
        self._entry(row, self.vars["frame_start"], 5).pack(side="left")
        self._label(row, " 到 ", sub=True).pack(side="left")
        self._entry(row, self.vars["frame_end"], 5).pack(side="left")
        tip.attach(label, "挑一小段先渲出来看看。留空就渲整片。")

        row = line(self._fold)
        label = self._label(row, "开头静置", width=8)
        label.pack(side="left", anchor="w")
        self._entry(row, self.vars["preroll"], 5).pack(side="left")
        # 这一条**故意写得直白、甚至有点粗**：它就是给懂行的人用的，
        # 不懂的人乱改只会渲出一堆抖动的片子，所以直接说"不懂不要动"。
        # 不要改回"让头发和裙摆别抖"那种比方 —— 那是 AI 腔。
        tip.attach(label, "物理预热留空，不懂不要动。")

        row = line(self._fold)
        label = self._label(row, "一次最多渲", width=8)
        label.pack(side="left", anchor="w")
        self._entry(row, self.vars["limit"], 6).pack(side="left")
        tip.attach(label, "一批最多渲几张，渲完一批就先存下来。\n"
                          "填小一点，中间出问题白做的就少。留空表示不分批。")

        row = line(self._fold)
        label = self._label(row, "取景范围", width=8)
        label.pack(side="left", anchor="w")
        self.framing_option = self._option(row, self.vars["framing"], FRAMING_LABELS,
                                           None, 8)
        self.framing_option.pack(side="left")
        self.framing_tip = tip.attach(
            label, "镜头卡空着时，自动给一个正面机位；这里选拍到哪。\n"
                   "放了镜头文件，构图听镜头文件的，这一项会灰掉。")

    # ------------------------------------------------------- 渲染格式（独立卡）
    def _build_format_card(self, parent: tk.Frame) -> None:
        """侧栏里**独立成一张卡**：「渲染格式」。

        为什么单独拉出来，而不是塞在「参数」里当一行：

        * 它决定的是**产出什么文件**，跟"画面多大、几帧"不是一类事
        * 四种格式的说明都很长（见 ``CODEC_INTRO``），挤在参数面板那一行里
          根本放不下，只能靠悬停展开 —— 独立成卡之后才写得下一点摘要
        * 用户要找它的时候，第一反应是"输出相关"，跟「输出预估」挨着更合理
        """
        card = self._panel(parent, "渲染格式", accent=T.MAPLE)
        card.pack(side="top", fill="x", pady=(T.S(12), 0))
        box = card.body

        row = tk.Frame(box, bg=T.CARD)
        row.pack(fill="x")
        self.alpha_label = self._label(row, "格式", width=5)
        self.alpha_label.pack(side="left", anchor="w")
        # 值存内部键，显示名走 labels 映射（见 Select 的说明）
        self.alpha_option = self._option(
            row, self.alpha_codec, list(ALPHA_CODEC_CHOICES),
            self._on_alpha_codec, 15, labels=ALPHA_CODEC_CHOICES)
        self.alpha_option.pack(side="left", fill="x", expand=True)

        # 当前这一种的**一句话摘要**：不点开也知道选的是什么。
        # 四种的完整对照走悬停展开 —— 一屏里铺四段太长。
        self.alpha_note = tk.Label(box, text="", bg=T.CARD, fg=T.TEXT_SUB,
                                   font=T.pick_font(9), anchor="w",
                                   justify="left", wraplength=T.S(252))
        self.alpha_note.pack(fill="x", pady=(T.S(6), 0))
        # 悬停给的是**四种的对照**（而不是当前那一种）：
        # 用户是在做选择，选择需要对比，一次只看一种等于还得挨个点。
        self.alpha_tip = tip.attach(self.alpha_label, codec_overview())
        tip.attach(self.alpha_note, codec_overview())
        tip.attach(self.alpha_option, codec_overview())
        self._refresh_format_card()

    def _refresh_format_card(self) -> None:
        """把当前格式的"一句话摘要"换成这一种。

        悬停里的**对照说明不用换** —— 它本来就列着全部四种，
        换掉反而会让用户没法对比。
        """
        note = CODEC_SUMMARY.get(self._current_codec(), "")
        label = getattr(self, "alpha_note", None)
        if label is not None:
            label.configure(text=note)

    # ------------------------------------------------------- 输出预估（贴底）
    def _build_estimate_card(self, parent: tk.Frame) -> None:
        """侧栏底部那张「输出预估」卡 —— 独立成卡、**钉死在最底**。

        三行分别是：成片轨（按所选编码器）/ 所在盘还剩多少。
        ⚠️ 不再有"另外垫一份 MP4"那一行 —— 那条附加轨已删，写在卡里会误导。
        数值全部实时算（改分辨率、改时长、换编码器都会跟着变）。
        """
        wrapper = tk.Frame(parent, bg=T.CARD, highlightthickness=1,
                           highlightbackground=T.BORDER)
        wrapper.pack(side="bottom", fill="x", pady=(T.S(12), 0))
        head = tk.Frame(wrapper, bg=T.CARD)
        head.pack(fill="x", padx=T.S(12), pady=(T.S(9), T.S(4)))
        bar = tk.Canvas(head, width=T.S(3), height=T.S(13), bg=T.CARD,
                        highlightthickness=0)
        bar.create_rectangle(0, 0, T.S(3), T.S(13), fill=T.MAPLE, outline="")
        bar.pack(side="left")
        tk.Label(head, text="输出预估", bg=T.CARD, fg=T.TEXT,
                 font=T.pick_font(10, bold=True)).pack(side="left", padx=T.S(7))

        inner = tk.Frame(wrapper, bg=T.CARD)
        inner.pack(fill="x", padx=T.S(12), pady=(0, T.S(10)))
        self.estimate_rows: dict[str, tk.Label] = {}
        for key in ("alpha", "disk"):
            label = tk.Label(inner, text="", bg=T.CARD, fg=T.TEXT_SUB,
                             font=T.pick_font(9), anchor="w", justify="left")
            label.pack(fill="x")
            self.estimate_rows[key] = label
        # 这两行字都比较长，用悬停提示给全文，卡里只放短的
        self.estimate_tips = {
            "alpha": tip.attach(self.estimate_rows["alpha"],
                                "估计的，仅供参考。画面越复杂越大。"),
        }
        self._update_estimate_card()

    def _update_estimate_card(self) -> None:
        """把三条预估数字算出来填进去。**算不出来就写"还看不出"，不报错。**"""
        rows = getattr(self, "estimate_rows", None)
        if not rows:
            return
        frames, guessed = self._planned_frames()
        width = self._int_var("width", 2560)
        height = self._int_var("height", 1440)
        codec = self._current_codec()
        ranges = budget.estimate(frames, width, height, alpha_codec=codec)

        alpha_label = budget.CODEC_LABELS.get(codec, codec)
        alpha_text = str(ranges[codec]) if codec in ranges else "—"
        # ⚠️ 这两行**必须短**：侧栏只有 412 实际像素，而
        # 「透明 MOV（ProRes，体积小得多）　1.9 GB ~ 4.7 GB」要 821 像素 ——
        # 装不下就折行、把卡顶变形（``work/check_overlap.py`` 抓到的）。
        # 所以卡里只放短标签与数字，完整名字和"按多少帧估"都走悬停提示。
        short = {"prores": "透明 MOV", "qtrle": "透明 MOV 无损",
                 "png": "PNG 序列"}.get(codec, alpha_label)
        rows["alpha"].configure(text=f"{short}　{alpha_text}")
        # 提示里给全名 + 估算依据，想看细节再停上去
        detail = (f"{alpha_label}　{alpha_text}"
                  + (f"\n还没定时长，先按 {frames} 帧估的。" if guessed else ""))
        try:
            self.estimate_tips["alpha"].text = detail
        except (AttributeError, KeyError):
            pass

        target = self.vars["output_dir"].get() or self.vars["work_dir"].get()
        check = budget.preflight(target, budget.total(ranges))
        if check.free is None:
            rows["disk"].configure(text="所在盘剩余：查不到", fg=T.TEXT_FAINT)
        elif check.ok:
            rows["disk"].configure(
                text=f"所在盘剩余 {budget.format_size(check.free)} · 够用", fg=T.OK)
        else:
            rows["disk"].configure(
                text=f"所在盘只剩 {budget.format_size(check.free)}，"
                     f"还差约 {budget.format_size(check.shortfall)}", fg=T.DANGER)

    def _toggle_fold(self) -> None:
        """展开/收起"更多设置"。"""
        self._fold_open = not self._fold_open
        if self._fold_open:
            self._fold.pack(fill="x", pady=(T.S(4), T.S(0)))
            self._fold_button.set_text("▾ 收起设置")
        else:
            self._fold.pack_forget()
            self._fold_button.set_text("▸ 更多设置")
        self._sync_framing_state()

    def _sync_framing_state(self) -> None:
        """取景范围只在**镜头卡留空**时才可选。

        有镜头文件时构图由镜头决定，这个下拉没有意义，干脆禁用。
        原来这里还会在下面补一句常驻说明，现在那句话进了悬停提示。
        """
        has_camera = bool(self.vars["camera"].get().strip())
        self.framing_option.set_enabled(not has_camera)
        if not has_camera:
            self.framing_tip.text = (
                "镜头卡留空时，自动生成正面机位；这里决定取到哪儿。\n"
                "放了镜头文件就由镜头决定构图，这里会置灰。")
        else:
            self.framing_tip.text = (
                "已经放了镜头文件，构图由镜头决定，这一项不起作用。\n"
                "想自己定取景范围，就把镜头卡清空。")

    def _option(self, parent, var, values, command, width: int, *,
                labels: dict | None = None) -> Select:
        """扁平下拉框。

        原来用的是 ``tk.OptionMenu`` —— 原生灰底方角 + 系统字体，一屏自绘控件里
        最刺眼的那处廉价感。现在换成自绘的 :class:`Select`。
        ``width`` 沿用"字符数"的调用习惯，这里换算成像素。

        ``labels`` 用在"值是内部键、显示要说人话"的场合 ——
        比如透明格式内部存 ``prores``、界面写 ``MOV（推荐，体积小）``。
        """
        return Select(parent, var, list(values),
                      width=max(T.S(112), T.S(width * 9)),
                      bg=parent.cget("bg"), command=command, labels=labels)

    # ------------------------------------------------------------- 主区
    def _build_main(self, body: tk.Frame) -> None:
        """主区重排 —— 照 ``work/mockup_full.png`` 那张设计图。

        ⚠️ **关键认识**：原来预览区 ``rowconfigure(weight=1)`` 吃掉全部剩余高度，
        拉高窗口只会得到一大片空渐变 —— 那才是"太空"的根因，不是缺装饰。

        这是个**分批**渲染的工具：分批进度和素材状态**本来就该在界面上**。
        所以预览改成固定高度，下面的位置让给这两张卡。

        ::

            row 0 四张投放卡（固定高）
            row 1 工具行：单帧预览 / 预览帧号 / 就绪
            row 2 预览区（**固定高**，含引导卡 + 摘要条）
            row 3 分批进度 + 素材清单（吃剩余高度）
            row 4 底部状态条（固定高）
        """
        main = tk.Frame(body, bg=T.CREAM)
        main.pack(side="left", fill="both", expand=True)

        # ── row 0：四张卡 ──
        cards = tk.Frame(main, bg=T.CREAM, height=T.S(ZONE_CARD_H))
        cards.pack(side="top", fill="x")
        cards.pack_propagate(False)
        for index in range(len(ZONES)):
            cards.columnconfigure(index, weight=1, uniform="card", minsize=T.S(150))
        cards.rowconfigure(0, weight=1)
        for index, (key, title, subtitle) in enumerate(ZONES):
            # 「动作」卡：**一个框，不显示分类标签**。
            #
            # ⚠️ 用户要求"无序分类"：
            # > 表情和动作可以直接导入不用现在一样分两个吧，毕竟有些作者
            # > 他不写那个是动作哪个是表情
            # > 无序分类
            #
            # 所以界面上**不让用户分类** —— 拖进来什么就列什么。
            # 谁承担哪个角色由 ``import_vmd`` 自己处理（它一次把骨骼/表情/相机
            # 全读进去），"归类"只影响存在哪个字段，**不影响导入结果**。
            #
            # ``slots`` 内部**数量可变**（见 Card.show_slots）：动作卡现在是
            # 一个有序列表，挂几个画几行，但画出来是**一个框**。
            #
            # ⚠️ 回调必须能收 ``None``：``Card._on_click`` 在拿不到事件坐标时
            # （键盘触发、代码直接调）会传 ``None``。
            slots = MOTION_SLOTS if key == "motion" else ()
            if slots:
                card = Card(cards, index=index + 1, bg=T.CREAM, slots=slots,
                            command=lambda k=key: self._browse_zone(k),
                            clear_command=lambda k=key: self._clear_zone(k),
                            # 每一行的小 × ：删掉那一个文件（整卡清空是右上角那个）
                            remove_command=self._remove_motion_file)
            else:
                card = Card(cards, index=index + 1, bg=T.CREAM,
                            command=lambda k=key: self._browse_zone(k),
                            clear_command=lambda k=key: self._clear_zone(k))
            card.grid(row=0, column=index, sticky="nsew",
                      padx=(0 if index == 0 else T.S(CARD_GAP), 0))
            self.cards[key] = card

        # ── row 1：工具行 ──
        actions = tk.Frame(main, bg=T.CREAM, height=T.S(TOOL_ROW_H))
        actions.pack(side="top", fill="x", pady=(T.S(12), T.S(12)))
        actions.pack_propagate(False)
        self.btn_still = T.FlatButton(actions, "单帧预览", self.on_still, width=T.S(96),
                                      height=T.S(TOOL_ROW_H), bg=T.CREAM,
                                      fill="#FFFFFF", hover=T.BEIGE_SOFT, fg=T.NAVY,
                                      outline=T.BORDER)
        self.btn_still.pack(side="left")
        tip.attach(self.btn_still,
                   "先渲一张，看看构图和站位对不对，没问题再渲整片。\n"
                   "渲好的图显示在下面。")

        # 「渲染模型」—— 独立的一块区域（点开是单独一个窗）。
        #
        # 用户需求原话：
        # > 把工作区里的模型渲染能力装进去，让用户不再需要自己打开 Blender
        # > 再导入，记得做独立区域，然后制作完毕后自动导入到流程内的模型区域
        #
        # 为什么不直接把参数摊在主界面：主界面侧栏只有 400 多像素，
        # 而这块要放模型路径、四个视角、取景、分辨率、两个关键字。
        # 所以做成**独立窗口**，主界面只留一个入口 —— 不渲就用不着看见它。
        self.btn_model = T.FlatButton(
            actions, "渲染模型", self.on_model_render, width=T.S(96),
            height=T.S(TOOL_ROW_H), bg=T.CREAM, fill="#FFFFFF",
            hover=T.BEIGE_SOFT, fg=T.NAVY, outline=T.BORDER)
        self.btn_model.pack(side="left", padx=(T.S(8), 0))
        tip.attach(self.btn_model,
                   "把一个 .pmx 模型渲成三视图（正面 / 侧面 / 背面）。\n"
                   "不用自己开 Blender：选好模型点开始就行，\n"
                   "渲完先给你看，满意了点「继续」就放进「角色模型」卡。")

        label = tk.Label(actions, text="预览帧号", bg=T.CREAM, fg=T.TEXT_SUB,
                         font=T.pick_font(10))
        label.pack(side="left", padx=(T.S(12), T.S(4)))
        tip.attach(label, "要渲第几帧。30 大概是动作刚开始的地方。")
        tk.Entry(actions, textvariable=self.vars["still_frame"], width=6, relief="flat",
                 bg="#FFFFFF", fg=T.TEXT, font=T.pick_font(10), highlightthickness=1,
                 highlightbackground=T.BORDER,
                 highlightcolor=T.NAVY).pack(side="left", ipady=T.S(4))

        # ── 命名（用户要求：右面要有命名框，不动就用默认名 = 动作名去后缀）──
        #
        # 用户原话：
        # > 这里的右面应该出现一个命名窗口，没动就是显示默认名称
        # >（动作名称不带后缀）
        #
        # 原来单帧预览固定存成 ``still_000370.png``，还塞在中间产物目录里 ——
        # 名字不是用户的、位置他也找不到。现在这里给他改名，默认跟着动作走。
        name_label = tk.Label(actions, text="命名", bg=T.CREAM, fg=T.TEXT_SUB,
                              font=T.pick_font(10))
        name_label.pack(side="left", padx=(T.S(12), T.S(4)))
        self._still_name_tip = tip.attach(
            name_label,
            "单帧预览存成什么名字。\n"
            "留空就跟着动作文件的名字走（去掉 .vmd 后缀）。")
        self._still_name_entry = tk.Entry(
            actions, textvariable=self.vars["still_name"], width=26, relief="flat",
            bg="#FFFFFF", fg=T.TEXT, font=T.pick_font(10), highlightthickness=1,
            highlightbackground=T.BORDER, highlightcolor=T.NAVY)
        self._still_name_entry.pack(side="left", ipady=T.S(4))
        # 用户一动手就不再用默认名（否则改好名字、一换动作就被冲掉）
        self.vars["still_name"].trace_add("write", self._mark_still_name_touched)

        # ⚠️ 这行原来还有一句常驻的「支持拖放」，加了命名框之后放不下了
        # （实测会被挤出右边界）。那本来就是最该让位的一句：
        # 每张卡片上已经写着"点这里选，或拖进来"，提示里也还有一份。
        # 留白比挤掉一个能用的控件好。

        self.pill = T.Pill(actions, "就绪", bg=T.CREAM, fill=T.NAVY_TINT, fg=T.NAVY,
                           width=T.S(78))
        self.pill.pack(side="right")

        # ── row 2：构图预览区（**固定高度**）──
        # 单帧预览的结果直接显示在窗口里，不用另开看图程序（tkinter 自带 PNG）。
        # 背景用极浅渐变 + 狐狸尾巴水印；空态正中摆一张「开始做一条片子」引导卡。
        preview_box = tk.Frame(main, bg=T.CARD, highlightthickness=1,
                               highlightbackground=T.BORDER,
                               height=T.S(PREVIEW_H + 8))
        preview_box.pack(side="top", fill="x")
        preview_box.pack_propagate(False)
        self.preview = tk.Canvas(preview_box, bg=T.PREVIEW_TOP,
                                 highlightthickness=0, bd=0)
        self.preview.pack(side="top", fill="both", expand=True,
                          padx=T.S(4), pady=T.S(4))
        self.preview.bind("<Configure>", self._on_preview_resize)
        self.preview.bind("<Button-1>", self._on_preview_click)
        self._preview_shown = None # ⚠️ 必须留引用，否则图片会被 GC 掉变空白
        self._preview_source: Path | None = None
        self._preview_resize_job = None

        # ── row 3：分批进度 + 素材清单（两张并排，吃剩余高度）──
        # 这两块的信息管线里**本来就有**，只是之前没摆出来 —— 一摆出来，
        # 下面那一大块空就自然没了。
        info = tk.Frame(main, bg=T.CREAM)
        info.pack(side="top", fill="both", expand=True, pady=(T.S(INFO_GAP), 0))
        info.columnconfigure(0, weight=1, uniform="info")
        info.columnconfigure(1, weight=1, uniform="info")
        info.rowconfigure(0, weight=1)
        self.batch = BatchProgress(info, bg=T.CREAM)
        self.batch.grid(row=0, column=0, sticky="nsew",
                        padx=(0, T.S(INFO_GAP) // 2))
        self.materials = MaterialList(info, bg=T.CREAM)
        self.materials.grid(row=0, column=1, sticky="nsew",
                            padx=(T.S(INFO_GAP) // 2, 0))
        tip.attach(self.batch,
                   "整片按「一次最多渲」切成几批，一批渲完就先存下来。\n"
                   "中间出问题只赔这一批，前面渲好的留着，接着跑会跳过。")
        tip.attach(self.materials,
                   "四样素材放齐了没有。空着的那几样有备用办法：\n"
                   "镜头空着自动给正面机位；舞台空着不画背景，成片带透明。")

        # ── row 4：底部状态条（就很细的一条，与设计图一致）──
        strip = tk.Frame(main, bg=T.CARD, highlightthickness=1,
                         highlightbackground=T.BORDER)
        strip.pack(side="top", fill="x", pady=(T.S(INFO_GAP), 0))
        line = tk.Frame(strip, bg=T.CARD)
        line.pack(fill="x", padx=T.S(10), pady=(T.S(7), T.S(6)))
        self.progress = ThinProgress(line, bg=T.CARD, width=T.S(210), height=T.S(10))
        self.progress.pack(side="left", pady=T.S(2))
        percent = tk.Label(line, textvariable=self.vars["percent"], bg=T.CARD,
                           fg=T.NAVY, font=T.pick_font(11, bold=True), width=8,
                           anchor="w")
        percent.pack(side="left", padx=(T.S(10), 0))
        tip.attach(percent, "整片跑到哪儿了。")
        status = tk.Label(line, textvariable=self.vars["status"], bg=T.CARD,
                          fg=T.TEXT_SUB, font=T.pick_font(9), anchor="w")
        status.pack(side="left", fill="x", expand=True)
        tip.attach(status,
                   "当前这一步在做什么。详细日志在输出目录旁边的 logs 里。")
        # 简易报错：只在出问题时出现，一句话说清 + 建议（K-6.3）
        self.notice = tk.Label(strip, text="", bg=T.CARD, fg=T.DANGER,
                               font=T.pick_font(9), anchor="w", justify="left",
                               wraplength=T.S(760))
        self.notice.pack(fill="x", padx=T.S(10), pady=(0, T.S(6)))

    # ------------------------------------------------------------- 拖放
    def _install_dnd(self) -> None:
        """给窗口装原生文件拖放。**等窗口映射之后再调**（见 ``_on_window_mapped``）。

        ⚠️ **时机**：Tk 顶层在 Windows 上是"内层窗口 + 外层 wrapper"两层，
        ``dnd.install`` 两层都装。但在 ``_build_ui`` 里调它时 wrapper **还没建出来**
        （``GetParent(winfo_id())`` 返回 0），只装上内层 —— 而真正接收拖放消息的是
        wrapper，所以"拖进去没反应"。窗口映射之后再调才装得齐。

        ⚠️⚠️ **窗口过程里绝对不能碰 Tcl**（这是崩因，异常码 ``0xC0000409``）。
        早期实现在 WndProc 里直接 ``root.after(0, ...)`` —— 那是一次 Tcl 调用，
        会重入 Tk 的消息处理，而 Tk 会在那次派发里把排好的任务也跑掉，
        等于**在窗口过程里跑完整套界面逻辑**，把 wndproc 链搞坏。
        同一个码当初在 ``_restore_taskbar_button``（那对 ShowWindow）上也崩过。

        现在的做法：WndProc 只把路径**塞进一个纯 Python 队列**（不碰 Tcl），
        Tk 侧用常规定时器去取。队列与定时器都是纯 Python，跨上下文是安全的。
        """
        if getattr(self, "_dnd_ready", False):
            return
        if not hasattr(self, "_drop_inbox"):
            #: ⚠️ 必须是**普通 Python 队列**，不能是 Tcl 变量 —— 见上面的说明
            self._drop_inbox: queue.Queue[list[str]] = queue.Queue()

        try:
            def on_drop(paths, point) -> None:
                # 只做一件事：入队。**不碰 Tcl、不碰 Tk 对象、不排 after。**
                try:
                    self._drop_inbox.put_nowait(list(paths))
                except Exception: # noqa: BLE001
                    pass
                #: 顺带记下落点，主循环取的时候可以判断拖到哪张卡上
                try:
                    self._drop_point = (int(point[0]), int(point[1]))
                except Exception: # noqa: BLE001
                    self._drop_point = (0, 0)

            self._drop_targets = dnd.install(self.root, on_drop)
        except Exception as exc: # noqa: BLE001 - 拖放装不上不该影响程序能用
            self._log(f"拖放安装失败（不影响其它功能）：{exc}", "warn")
            self._drop_targets = []

        if self._drop_targets:
            #: 两层都装上才算"齐了"：只装上内层的话，拖放多半收不到消息
            self._dnd_ready = len(self._drop_targets) >= 2
            labels = "、".join(t.label for t in self._drop_targets)
            self._log(f"支持文件拖放（已装：{labels}）。")
        else:
            self._log("当前平台不支持原生拖放，请用卡片上的按钮选文件。", "warn")

    def _poll_drops(self) -> None:
        """定时把拖放队列里的路径取出来处理（**在 Tk 主循环里跑，安全**）。

        这才是真正"回主线程"的那一步 —— 窗口过程只负责入队。
        """
        try:
            while True:
                paths = self._drop_inbox.get_nowait()
                self._handle_drop(paths)
        except queue.Empty:
            pass
        except Exception as exc: # noqa: BLE001 - 一次拖放坏了不该拖垮界面
            self._log(f"处理拖放失败：{type(exc).__name__}: {exc}", "warn")
        self._schedule("_drop_job", 120, self._poll_drops)

    def _handle_drop(self, paths: list[str]) -> None:
        """处理一次拖放。落点在哪张卡上就放哪张；在卡片之外则按扩展名自动归类。"""
        if self.mode == "run":
            self._log("正在渲染，先等这一轮结束再换素材。")
            return

        # 用户又开始动手了：把上一轮留下的"步骤态"卡片恢复成投放态
        # （失败标记会保留到这一刻，让他先看清是哪一步挂了）
        self.reset_cards_to_drop()

        try:
            pointer = self.root.winfo_pointerxy()
            widget = self.root.winfo_containing(*pointer)
        except Exception: # noqa: BLE001
            widget = None
        hovered = next((key for key, card in self.cards.items() if card is widget), None)

        # 「动作」卡是**一个有序列表**，数量不限。
        #
        # ⚠️ 往这张卡上再拖 = **追加**，不再"先清空再按内容归位"。
        # 用户要的是"可以添加多个"（原来拖第二个会把第一个冲掉，
        # 而且靠 `_classify_vmd` 猜它是动作还是表情 —— 现在不需要猜了，
        # 全按导入顺序排，谁是什么由 mmd_tools 自己读）。

        assigned: list[str] = []
        for path in paths:
            if not os.path.isfile(path):
                continue
            key = hovered if hovered and len(paths) >= 1 else None
            if key is None:
                key = self._classify(path)
            if not key:
                continue
            self._assign(key, path)
            assigned.append(f"{os.path.basename(path)} → {self._zone_label(key)}")

        self._refresh_cards()
        if assigned:
            self._log("拖入：" + "；".join(assigned))
            self.vars["status"].set(f"已放入 {len(assigned)} 个文件")
        else:
            self._log("拖进来的文件类型不认识（只认 .pmx/.pmd/.vmd/.vpd/.blend/音频）")

    def _classify(self, path: str) -> str:
        """按扩展名猜这个文件该放哪儿（拖到窗口空白处时用）。"""
        ext = os.path.splitext(path)[1].lower()
        if ext == ".blend":
            # .blend 就是"已配好的工程"，不分放哪张卡，统一归到 blend
            return "blend"
        if ext in (".pmx", ".pmd"):
            return "stage" if self.vars["stage"].get() else "model"
        if ext == ".vmd":
            return self._classify_vmd(path)
        if ext == ".vpd":
            return "model"
        return ""

    @staticmethod
    def vmd_parts(path: str) -> dict[str, int] | None:
        """数一个 .vmd 里各段有多少帧。

        **这是"不看文件名也能分清动作和表情"的关键。**
        用户说得很对："有些作者他不写那个是动作哪个是表情" ——
        靠文件名猜（带"表情"两个字）根本不靠谱，同一套素材换个作者就失效。

        但 VMD 是**结构化**的：签名之后依次是骨骼帧、表情帧、相机帧，
        每段的长度都能算出来。所以"这个文件里有没有骨骼动画 / 有没有表情"
        是**可以准确判定**的，不需要猜。

        数不出来（格式不对 / 读不了）时返回 ``None``，交给调用方兜底。
        """
        try:
            with open(path, "rb") as handle:
                data = handle.read()
            if not data.startswith(b"Vocaloid Motion Data"):
                return None
            offset = 50 # 30 字节签名 + 20 字节模型名
            bones = struct.unpack_from("<I", data, offset)[0]
            offset += 4 + bones * 111
            morphs = struct.unpack_from("<I", data, offset)[0]
            offset += 4 + morphs * 23
            cameras = struct.unpack_from("<I", data, offset)[0]
            return {"bones": bones, "morphs": morphs, "cameras": cameras}
        except (OSError, struct.error, IndexError):
            return None

    def _classify_vmd(self, path: str) -> str:
        """这个 .vmd 该放哪个槽位。

        ★ **动作卡里只有一个 vmd 时，它就是"多合一"。**

        用户原话：

        > 如果用户只导入一个文件，代表那是多合一，懂我意思吧（除了摄像机）

        MMD 圈很多动作作者把骨骼、表情全打包在**一个** vmd 里。
        所以单文件不是"只有骨骼"，而是"这一份就齐了" ——
        导入时骨架和网格都得挂上去，两样才都生效
        （见 ``adapters/scripts/build.py::_import_motion``）。

        **（除了摄像机）**：摄像机是独立一栏，不属于"多合一"的一部分。
        所以下面判定时，带相机帧的文件**仍然归动作** ——
        否则整个文件被拽进「镜头」格，动作就丢了。
        纯相机帧的文件（那确实不是多合一）才归镜头。

        **先看内容，再退回文件名** —— 顺序不能反：

        1. 数帧数（``vmd_parts``）
           * 只有表情帧，没有骨骼帧 → 表情
           * **只有**相机帧 → 镜头
           * 有骨骼帧（不管有没有表情、有没有相机）→ 动作
             一次导入两样都进去了，归哪格都不影响结果，
             但归动作更符合"这是主文件"的直觉
        2. 数不出来 / 没有帧 → 退回文件名里的关键词
        3. 再退回"哪个格还空着"

        ⚠️ 光看文件名（``表情``/``morph``/``face``）是不够的 ——
        用户明确说了有些作者根本不写。
        """
        parts = self.vmd_parts(path)
        if parts is not None:
            if parts["cameras"] and not parts["bones"] and not parts["morphs"]:
                return "camera"
            if parts["morphs"] and not parts["bones"]:
                return "expression"
            if parts["bones"]:
                return "motion"
            # 三种帧都是 0：空文件，往下走文件名兜底

        # ── 兜底：文件名关键词 ──
        name = os.path.basename(path).lower()
        if any(word in name for word in ("表情", "morph", "face", "facial")):
            return "expression"
        if any(word in name for word in ("镜头", "カメラ", "camera")):
            return "camera"

        # ── 兜底：按内容能判的都判过了，剩下的归位 ──
        # 动作卡是列表，永远能塞，所以直接归动作（不再看"哪格还空着"）。
        if not self.vars["camera"].get().strip():
            return "camera"
        return "motion"

    def _assign(self, key: str, path: str) -> None:
        """把文件放进工程。

        ``.blend`` 有**两种完全不同的含义**，靠拖到哪张卡来区分：

        * 拖到**舞台卡** → 它是「环境」：只把这个 .blend 里的场景物件挂进
          我们组装好的场景（角色和动作照样走 pmx + vmd）
        * 拖到**其它任何卡** → 它是「已配好的整个工程」：不再组装，直接拿它渲

        用户不需要理解"路线"，拖哪儿就是哪个意思。
        """
        if os.path.splitext(path)[1].lower() == ".blend":
            if key == "stage":
                self.vars["stage"].set(path)
                # 加了背景 → 「渲染背景」自动开（用户要求：加了背景才是开）
                self.transparent.set("开")
                self._log(f"按「环境」接收 Blender 工程：{path}")
            else:
                # 🚨 已经放了场景或动作时，别让这份 .blend 默默把它们顶掉。
                #
                # 用户的原话就是「场景导进去出来场景消失了」：
                # 他把工程 .blend 拖到角色卡，整条路线切成「整份工程」，
                # 舞台与动作当场失效，看起来就是"东西没了"。
                # 这时让他自己选，并把两条路的后果写明白。
                has_context = bool(self.vars["stage"].get().strip()) or bool(
                    self._motion_paths())
                use_as_whole = True
                if has_context:
                    use_as_whole = messagebox.askyesno(
                        "这份 .blend 要怎么用？",
                        "你已经放了场景或动作。\n\n"
                        "「是」= 当作整份工程：直接渲它，场景和动作不参与\n"
                        "「否」= 放进「舞台」当场景：角色和动作照常参与")
                if use_as_whole:
                    self.vars["blend"].set(path)
                    # 记住"这次确定用这份工程"（下次启动自动填回来）
                    self._remember_model(path)
                    self._log(f"按「已配好的工程」接收：{path}")
                else:
                    self.vars["stage"].set(path)
                    self.transparent.set("开")
                    self._log(f"按「环境」接收 Blender 工程：{path}（角色与动作照常参与）")
            return
        # 渲好的三视图（PNG）放进「角色模型」卡 = 拿它当参考图。
        #
        # ⚠️ 它**不能**当渲染流程的模型用：整片那条路要的是 .pmx 或 .blend
        # （有骨骼、能挂动作），静帧图只是个平面。所以这里把话说清楚，
        # 并告诉他正确的做法（模型渲染那个窗会顺便存 .blend）。
        if key == "model" and os.path.splitext(path)[1].lower() in (".png", ".jpg"):
            self.vars["model"].set(path)
            self.vars["status"].set(
                "「角色模型」放的是一张图（只能当参考图）——"
                "要能渲整片，用「渲染模型」渲一次，它会顺便存出可用的 .blend。")
            self._log(f"「角色模型」接收了一张图（参考图，不是模型）：{path}", "warn")
            return
        # 动作 / 表情卡：**一个有序列表**，数量不限。
        if key == "motion":
            self._add_motion_file(path)
            return
        self.vars[key].set(path)
        if key == "stage":
            # 加了背景 → 「渲染背景」自动开
            self.transparent.set("开")
        # 「角色模型」卡被填了 → 记住它（下次打开默认还是这个）。
        # ⚠️ 只记 model / blend 这两条"角色"路线，舞台和镜头不记 ——
        # 用户说的是"角色板块"。
        if key == "model":
            self._remember_model(path)

    # ---------------------------------------------------- 动作列表（可多个）
    def _motion_paths(self) -> list[str]:
        """「动作」卡里现在的全部文件（按导入顺序）。"""
        return [p for p in self.motion_list if p]

    def _set_motion_paths(self, paths: list[str]) -> None:
        """整体替换「动作」卡的文件列表（去重，保持顺序）。"""
        seen: set[str] = set()
        clean: list[str] = []
        for path in paths:
            if not path:
                continue
            key = os.path.normcase(os.path.abspath(path))
            if key in seen:
                continue
            seen.add(key)
            clean.append(path)
        self.motion_list = clean
        self._refresh_cards()

    def _add_motion_file(self, path: str) -> None:
        """往「动作」卡里**追加**一个文件。

        用户要求：

        > 这个动作和表情，可以添加多个而不是局限于2个

        ⚠️ 追加而不是替换 —— 这正是"能加多个"的意思。想换掉某个就用
           卡上那一行的小 × 单独删。
        ⚠️ 同一个文件再加一次只提示、不重复加：重复导入会在同一个骨架上
           建出两套同名 action，后面按名字取 action 就说不准拿到哪一套。
        """
        existing = {os.path.normcase(os.path.abspath(p))
                    for p in self._motion_paths()}
        if os.path.normcase(os.path.abspath(path)) in existing:
            self._log(f"这个动作文件已经在里面了，没重复添加：{path}", "warn")
            self.vars["status"].set("这个文件已经在里面了")
            return
        self._set_motion_paths([*self._motion_paths(), path])
        count = len(self._motion_paths())
        self.vars["status"].set(
            f"已加入第 {count} 个动作文件：{os.path.basename(path)}")
        self._log(f"动作列表 += {path}（现在 {count} 个）")

    def _remove_motion_file(self, index: int) -> None:
        """删掉「动作」卡里第 ``index`` 个文件（点那一行的小 ×）。"""
        paths = self._motion_paths()
        if not 0 <= index < len(paths):
            return
        removed = paths.pop(index)
        self._set_motion_paths(paths)
        self.vars["status"].set(f"已移除：{os.path.basename(removed)}")
        self._log(f"动作列表 -= {removed}（剩 {len(paths)} 个）")

    def _zone_label(self, key: str) -> str:
        return {"model": "角色模型", "motion": "动作", "expression": "表情",
                "camera": "镜头", "stage": "舞台", "blend": "工程"}.get(key, key)

    def _clear_zone(self, key: str) -> None:
        """清空一张卡的素材（点卡右上角「×」）。

        「动作」卡现在是**多个文件**，点整卡的「×」= 全部清空；
        只想删某一个，点那一行右边的小 ×（见 ``_remove_motion_file``）。
        """
        if key == "motion":
            had = len(self._motion_paths())
            self._set_motion_paths([])
            if had:
                self._log(f"已清空动作卡（{had} 个文件）")
                self.vars["status"].set(f"已清空 {had} 个动作文件，可以重新拖入")
            return
        name = key
        cleared = self._zone_label(name) if self.vars[name].get().strip() else ""
        self.vars[name].set("")
        self._refresh_cards()
        if cleared:
            self._log(f"已清空：{cleared}")
            self.vars["status"].set(f"已清空 {cleared}，可以重新拖入")

    def _browse_zone(self, key: str, slot: int | None = None) -> None:
        """点卡片开文件对话框。

        「动作」卡不再挑"空槽" —— 它就是一个列表，选中的文件**追加**进去。
        参数 ``slot`` 保留只为兼容老调用方。
        """
        if self.mode == "run":
            return
        # 用户又开始动手了：卡片恢复成投放态（失败标记保留到这一刻）
        self.reset_cards_to_drop()
        if key == "motion":
            paths = filedialog.askopenfilenames(
                title="选动作文件（.vmd，可以一次选多个）",
                filetypes=FILE_TYPES["motion"])
            for path in paths or ():
                self._add_motion_file(path)
            self._refresh_cards()
            return
        self._browse_into(key, "stage" if key == "stage" else key)
        self._refresh_cards()

    # ------------------------------------------------------------- 卡片刷新
    def _refresh_cards(self) -> None:
        """配置态：卡片显示已选文件名；执行态：卡片显示步骤状态。"""
        if self.mode == "run":
            return
        blend = self.vars["blend"].get()
        for key, title, subtitle in ZONES:
            card = self.cards[key]
            if key == "motion":
                # 「动作」卡：**任意多个**文件，按顺序列出来（不分动作/表情）。
                # ⚠️ 它没有 ``vars["motion"]`` —— 动作在 ``self.motion_list`` 里。
                card.show_slots(title, [os.path.basename(p)
                                        for p in self._motion_paths()])
                continue
            filename = self.vars[key].get()
            sub = subtitle
            if key == "model" and not filename and blend:
                # 没放 pmx、但有配好的 .blend：第 1 张卡就显示它
                filename, sub = blend, "已配好的工程 · 直接就能用"
            card.show_drop(title, sub, filename)
        self._sync_framing_state()
        self._refresh_panels()

    # ------------------------------------------------- 摘要条 / 进度 / 清单
    def _material_count(self) -> tuple[int, int]:
        """``(齐了几样, 共几样)``。

        **舞台算"透明背景"那条路**，所以放不放都不算缺件（决策记录 3/4）——
        这里仍然把它算进 4 样里，只是注明"留空 → 不渲背景（出透明）"。
        """
        have = 0
        if self.vars["model"].get().strip() or self.vars["blend"].get().strip():
            have += 1
        if self._motion_paths():
            have += 1
        if self.vars["camera"].get().strip():
            have += 1
        if self.vars["stage"].get().strip() or self.vars["blend"].get().strip():
            have += 1
        return have, 4

    def _summary_items(self) -> list[tuple[str, bool]]:
        """配置摘要条上那几块文字：``(文案, 是不是"高亮"的)``。"""
        have, total = self._material_count()
        width = self.vars["width"].get().strip() or "2560"
        height = self.vars["height"].get().strip() or "1440"
        fps = self.vars["fps"].get().strip() or "30"
        # 摘要条上顺带报一下档位（"2K"这种），比光看数字直观
        preset = self.vars["resolution"].get().strip()
        size_text = f"{width} × {height}" + (f"（{preset}）" if preset else "")
        if self._current_transparent():
            # 报**实际会出什么**，别写死的"透明 MOV + MP4" ——
            # 选了 PNG 序列就只有图，没有成片
            codec = self._current_codec()
            if codec == "png":
                output = "输出：PNG 图片序列"
            elif codec == "h264":
                output = "输出：MP4"
            else:
                # ⚠️ 不再有"垫底色的 MP4"那条附加轨（用户要求选什么出什么），
                # 所以这里只报一条。原来写「透明 MOV + MP4」会把用户误导成
                # "会多给我一个文件"（实测界面截图里就露着这句）。
                output = "输出：透明 MOV"
        else:
            output = "输出：MP4"
        return [
            (f"素材 {have} / {total}", True),
            (f"{size_text} · {fps} 帧/秒", False),
            (output, False),
        ]

    def _compact_size(self) -> str:
        """摘要条最右边那句"预计 5.8 ~ 14.8 GB"。"""
        frames, _guessed = self._planned_frames()
        size = budget.estimate_total(frames, self._int_var("width", 2560),
                                     self._int_var("height", 1440),
                                     alpha_codec=self._current_codec())
        return budget.compact_range(size)

    def _sync_still_name_default(self) -> None:
        """「命名」框空着时，把默认名（动作名去后缀）**显示**出来。

        用户原话：

        > 这里的右面应该出现一个命名窗口，没动就是显示默认名称
        >（动作名称不带后缀）

        "没动就显示默认名称"意味着框里得**看得见**那个默认值，不能是空的 ——
        空框用户不知道会存成什么。

        ⚠️ 只在"用户没自己填过"时改它（``_still_name_touched``）。
           填过就绝不覆盖，否则用户改好名字、一换动作就被冲掉。
        """
        entry = getattr(self, "_still_name_entry", None)
        if entry is None:
            return
        touched = getattr(self, "_still_name_touched", False)
        if not touched:
            # ⚠️ 程序自己填默认值时要把"用户动过"的钩子挡住 ——
            # ``trace_add`` 对**任何**写入都触发（包括我们自己写的），
            # 不挡的话第一轮同步就把自己标成"用户改过了"，之后再也不会更新。
            self._still_name_programmatic = True
            try:
                self.vars["still_name"].set(self._default_still_name_hint())
            finally:
                self._still_name_programmatic = False
        # 提示里始终写清"留空会存成什么"，改过名也不影响查看
        try:
            tip_obj = getattr(self, "_still_name_tip", None)
            if tip_obj is not None:
                tip_obj.text = (f"留空会存成：{self._default_still_name_hint()}.png\n"
                                "改了名就按你填的存。")
        except Exception: # noqa: BLE001 - 提示更新失败不该影响功能
            pass

    def _default_still_name_hint(self) -> str:
        """默认名：动作文件名去后缀（没有动作就用模型名，再兜底）。"""
        motions = self._motion_paths()
        if motions:
            return Path(motions[0]).stem
        model = self.vars["model"].get().strip()
        if model:
            return Path(model).stem
        return "单帧预览"

    # ------------------------------------------------ 成片文件名（跟动作走）
    def _default_output_name(self) -> str:
        """成片默认名：第一个动作文件名去后缀。

        用户说得对：

        > 视频命名并未按照动作名称生效

        原来这里固定默认 ``KomaFlow``，渲十条片子出来全叫同一个名字
        （靠自动加序号区分），根本认不出哪条是哪个动作。
        现在没动过就跟着动作走 —— 跟「单帧预览」的命名规则保持一致。
        """
        motions = self._motion_paths()
        if motions:
            return Path(motions[0]).stem
        return "KomaFlow"

    def _sync_output_name_default(self) -> None:
        """用户没自己填过文件名时，把它同步成动作名。

        ⚠️ 只在 ``_name_touched`` 为假时改；用户一旦手填，永不覆盖
           （否则改好名字、换个动作就被冲掉）。
        ⚠️ 程序自己写入要挡住 trace 钩子，不然第一轮同步就把自己标成"用户改过"。
        """
        if getattr(self, "_name_touched", False):
            return
        hint = self._default_output_name()
        if self.vars["name"].get() == hint:
            return
        self._name_programmatic = True
        try:
            self.vars["name"].set(hint)
        finally:
            self._name_programmatic = False

    def _mark_name_touched(self, *_args) -> None:
        """用户动过「文件名」→ 以后不再自动覆盖（trace 会传 3 个参数）。"""
        if getattr(self, "_name_programmatic", False):
            return
        self._name_touched = True

    def _mark_still_name_touched(self, *_args) -> None:
        """用户动过命名框 → 以后不再自动覆盖。

        ``trace_add`` 回调会传 3 个参数（变量名 / 索引 / 操作），所以收 ``*_args``。
        """
        if getattr(self, "_still_name_programmatic", False):
            return
        self._still_name_touched = True

    def _refresh_panels(self) -> None:
        """把"摘要条 / 分批进度 / 素材清单 / 输出预估"一起刷一遍。

        这四个地方看的是**同一份输入**（素材 + 参数），所以统一一个入口，
        免得某个变量改了、某一块没跟上。
        """
        self._update_estimate_card()
        self._sync_still_name_default()
        self._sync_output_name_default()
        batch = getattr(self, "batch", None)
        if batch is not None and self.mode != "run":
            batch.set_plan(self._describe_batches())
        materials = getattr(self, "materials", None)
        if materials is not None:
            materials.set_rows(self._material_rows())
        # 「渲染背景」的四态同步（规则见 ``_transparent_state``）。
        #
        # 跟「取景范围」同一套思路：能选就选，选了没用才灰掉 ——
        # 而且是**留着能点却不起作用**最糟，用户会以为工具坏了。
        # 所以灰掉时一定把"为什么"写进悬停提示。
        option = getattr(self, "transparent_option", None)
        if option is not None:
            allowed, shown, tip_text = self._transparent_state()
            option.set_enabled(allowed)
            self.transparent.set(shown)
            tip_obj = getattr(self, "transparent_tip", None)
            if tip_obj is not None:
                tip_obj.text = tip_text
        # 物理档位：物理关掉时灰掉（关了就没什么档位可调）
        if self._preview_source is None:
            # 摘要条画在预览画布上，参数一变就得重画
            self._draw_preview_background()

    def _plan_segments_now(self) -> list:
        """按当前参数算一遍要分几批。**纯粹为了显示，算不出来就返回空表。**

        ⚠️ 「渲染区间」没填时**不要编一个总帧数**。
        原来这里用 ``FALLBACK_FRAMES``（1800）去猜，猜出「90 张一批」挂在界面上，
        可真正渲染用的是装配后才知道的动作长度（实测 601 帧）——
        于是用户看到「41 / 90 张」而 Blender 一路渲到 601 帧，
        还以为是"到目标帧不停"。分批表必须以管线发来的真实段为准（见 _reset_batches）。
        """
        start = self._int_var("frame_start", 0)
        end = self._int_var("frame_end", 0)
        if end <= start:
            return []
        limit = self._int_var("limit", DEFAULT_SEGMENT_LIMIT)
        try:
            return plan_segments(max(1, end - start), limit if limit > 0 else None)
        except ValueError:
            return []

    def _describe_batches(self) -> list[tuple[str, int]]:
        segments = self._planned_segments
        return [(f"第 {index + 1} 批", end - start + 1)
                for index, (start, end) in enumerate(segments)]

    def _reset_batches(self) -> None:
        """开始渲染前把分批表**定死**：这次要分几批、每批起止帧。

        ⚠️ 定下来之后就不能再跟着输入框变了 —— 否则用户中途改一下"一次最多渲"，
        界面上的批次就跟实际跑的段对不上（这比不显示更糟）。
        """
        self._planned_segments = []
        batch = getattr(self, "batch", None)
        if batch is None:
            return
        # 空表 → 卡片显示"装配完才知道分几批"，而不是一个猜出来的 90 张
        batch.set_plan([])

    def _mark_all_batches_done(self) -> None:
        """成片出来了 —— 每一批都点成"已完成 N 张"。"""
        batch = getattr(self, "batch", None)
        if batch is None or not self._planned_segments:
            return
        for index, (start, end) in enumerate(self._planned_segments):
            batch.set_state(index, DONE, f"已完成 {end - start + 1} 张")

    def _mark_batch_running(self, index: int, event: dict) -> None:
        """把分批进度里第 ``index`` 批标成"渲染中"，并写上渲了多少张。

        数字全从事件里拿：管线给的 ``frame_done`` / ``frame_total`` 是**整片**的，
        要减掉前面已完成批次的总长，才是**这一批**渲了多少 ——
        用户看的是"这一批"，不是"从开天辟地算起"。
        """
        batch = getattr(self, "batch", None)
        if batch is None or not self._planned_segments:
            return
        if not (0 <= index < len(self._planned_segments)):
            return
        # 优先用事件自带的段范围（比计划表更权威：计划表可能还没建好）
        if event.get("frame_start") is not None and event.get("segment_end") is not None:
            start = int(event["frame_start"])
            end = int(event["segment_end"])
        else:
            start, end = self._planned_segments[index]
        expected = end - start + 1
        done = event.get("frame_done")
        total = event.get("frame_total")
        if done is None or total is None:
            note = f"渲染中 0 / {expected} 张"
        else:
            # frame_done 是整片的已完成帧数，减去本批起点就是**本批**渲了多少
            local = max(0, min(expected, int(done) - start))
            note = f"渲染中 {local} / {expected} 张"
            # ── 用户要求：这里要告诉用户「多少秒一帧」和「还要多久」──
            #
            # ⚠️ 秒/帧 用的是**最近 10 帧的滑动平均**（渲染脚本算好的
            # ``seconds_per_frame_10``），不是总平均：总平均会被头几帧的
            # 着色器编译开销拉高、遇到复杂帧又反应迟钝，ETA 会一直骗人。
            per_frame = event.get("seconds_per_frame")
            eta = event.get("eta_seconds")
            if per_frame and per_frame > 0:
                note += f"　{per_frame:.2f} 秒/帧"
            if eta and eta > 0:
                note += f"　全片还剩 {self._duration(eta)}"
        batch.set_state(index, RUNNING, note)

    def _material_rows(self) -> list[tuple[str, str, bool]]:
        """素材清单卡的几行。**留空的项要写清"会走哪条路"**，不是空着。

        动作现在**可以多个**，所以这一行写"3 个文件"，第一行文件名跟在后面；
        其余几个走悬停提示看全（一屏放不下）。
        """
        model = self.vars["model"].get().strip()
        blend = self.vars["blend"].get().strip()
        motion_paths = self._motion_paths()
        camera = self.vars["camera"].get().strip()
        stage = self.vars["stage"].get().strip()

        if blend and not model:
            model_text, model_ok = os.path.basename(blend) + "（已配好的工程）", True
        else:
            model_text = os.path.basename(model) if model else "还没放角色模型"
            model_ok = bool(model)

        if not motion_paths:
            motion_text = "还没放动作"
        elif len(motion_paths) == 1:
            motion_text = os.path.basename(motion_paths[0])
        else:
            motion_text = f"{len(motion_paths)} 个文件 · {os.path.basename(motion_paths[0])}"

        camera_text = (os.path.basename(camera) if camera
                       else "留空 → 自动生成正面机位")
        if stage:
            stage_text = os.path.basename(stage)
        elif model:
            # 有角色没舞台 → 「渲染背景」默认关 = 出透明。这是最常用的用法。
            stage_text = "留空 → 不渲背景（输出带透明通道）"
        else:
            stage_text = "留空 → 照着默认环境渲"
        if blend and not stage:
            stage_text = "自带工程 · 背景沿用你文件里的设置"
        rows = [
            ("角色", _shorten(model_text), model_ok),
            ("动作", _shorten(motion_text), bool(motion_paths)),
        ]
        rows += [
            ("镜头", _shorten(camera_text), bool(camera)),
            ("舞台", _shorten(stage_text), bool(stage)),
        ]
        return rows

    def _set_card_step(self, index: int, state: str, badge: str = "") -> None:
        if index >= len(STEPS):
            return
        card = list(self.cards.values())[index]
        card.show_step(STEPS[index][1], state, badge)
        self._update_counts()

    def _reset_cards_to_steps(self) -> None:
        self.mode = "run"
        for index in range(len(STEPS)):
            self._set_card_step(index, PENDING)
        self._update_counts()

    def _update_counts(self) -> None:
        done = sum(1 for card in self.cards.values() if card.state == DONE)
        self.progress.set(done / len(STEPS))
        if done:
            self.vars["percent"].set(f"{done / len(STEPS) * 100:.0f}%")

    # --------------------------------------------------------------- 交互
    def _browse_into(self, key: str, kind: str | None) -> None:
        """开文件对话框选素材，选完走**跟拖放同一套**的归类逻辑。

        ⚠️ 这里原来是无脑 ``self.vars[key].set(path)``，于是**点卡片**选一个
        ``.blend`` 会直接塞进 ``vars["model"]`` —— 而 ``vars["blend"]`` 还是空的，
        ``route`` 就仍然是 ``assets``，build.py 拿 PMX 导入器去读 .blend，
        报「File signature is invalid.」。**拖放有分岔、点选没有**，
        同一个动作两条路结果不一样（用户就是这么踩到"不能渲染"的）。

        现在统一交给 :meth:`_assign` —— 它负责 ``.blend`` 那套"环境 / 整份工程"
        的分岔。**别再绕过它直接写 vars。**
        """
        path = filedialog.askopenfilename(
            title="选择文件", filetypes=FILE_TYPES.get(kind or "", [("所有文件", "*.*")]))
        if path:
            self._assign(key, path)

    def _browse_dir(self, key: str) -> None:
        path = filedialog.askdirectory(title="选择目录")
        if path:
            self.vars[key].set(path)

    def _on_orientation(self, _value=None) -> None:
        """换了横竖屏 —— 按**当前档位**重算宽高（而不是回到写死的 2560×1440）。"""
        self._apply_resolution()

    def _on_resolution(self, label: str) -> None:
        """点了清晰度档位（4K / 2K / 1080P / 360P）。

        档位给的是**长边**：横屏时是宽，竖屏时是高。
        这样"4K 竖屏"出来是 2160 × 3840，符合"竖屏 4K"的常识。
        短边按当前画幅比例算（16:9 就是长边 × 9/16）。
        """
        self.vars["resolution"].set(label)
        self._apply_resolution()

    def _apply_resolution(self) -> None:
        """把当前档位换算成宽 × 高填进输入框。"""
        long_edge = RESOLUTION_PRESETS.get(self.vars["resolution"].get())
        if not long_edge:
            return
        orientation = self.vars["orientation"].get()
        portrait = orientation.startswith("竖")
        square = orientation.startswith("方")
        if square:
            self.vars["width"].set(str(long_edge))
            self.vars["height"].set(str(long_edge))
        elif portrait:
            self.vars["width"].set(str(round(long_edge * 9 / 16)))
            self.vars["height"].set(str(long_edge))
        else:
            self.vars["width"].set(str(long_edge))
            self.vars["height"].set(str(round(long_edge * 9 / 16)))
        self._update_estimate_card()

    def _preset_for(self, width: int, height: int) -> str:
        """宽高对得上哪个档位就返回哪个，对不上返回空字符串。

        横竖屏都认：标称按**长边**算（3840×2160 和 2160×3840 都是 4K）。
        """
        long_edge = max(int(width), int(height))
        return next((name for name, size in RESOLUTION_PRESETS.items()
                     if size == long_edge), "")

    def _sync_resolution_face(self) -> None:
        """从宽高反推档位显示。

        用户手改了数字、对不上任何档位时，就**显示空白**（不硬套一个错的档位）
        —— 让他看得出"这是我自己填的"，而不是被悄悄归到某一档。
        """
        try:
            width = int(self.vars["width"].get())
            height = int(self.vars["height"].get())
        except (TypeError, ValueError):
            self.vars["resolution"].set("")
            return
        self.vars["resolution"].set(self._preset_for(width, height))

    def on_discover(self) -> None:
        """重新检测 Blender。

        ⚠️ 语义是"**给选项**"，不是"让用户自己去绑"。启动时会自动跑一次并自动选用；
        这里只是手动重来一遍（比如刚装完 Blender）。
        """
        self.vars["probe"].set("正在检测…")
        self._detect_blender_async()

    def _detect_blender_async(self) -> None:
        """在工作线程里探测所有候选 Blender（探测要起进程，别卡界面）。"""
        def job() -> None:
            probed = []
            # ⚠️ **必须把用户手动选过的那台带上**。
            # 原来这里 discover_blender() 不带 extra_paths，
            # 于是"自己选路径…"选完，下一次自动探测就把它丢了 ——
            # 表现正是"手动选择也不行"。
            manual = str(getattr(self.settings, "blender_exe", "") or "").strip()
            extra = [manual] if manual else []
            try:
                for candidate in discover_blender(extra_paths=extra):
                    try:
                        info = BASELINE.probe(candidate.exe)
                        probed.append({
                            "exe": str(candidate.exe),
                            "version": info.version_string,
                            "tools": info.mmd_tools_version,
                            "has_tools": info.mmd_tools_installed,
                        })
                    except KomaFlowError as exc:
                        # 探测失败**不丢弃** —— 有的机器上 Blender 能正常渲染，
                        # 只是 `--version` 那一趟超时（杀软/首次启动/Store 版）。
                        # 丢掉就等于"选不了"，所以留一条"未验证"让用户自己决定。
                        self._log(f"探测失败 {candidate.exe}：{exc.detail}", "warn")
                        probed.append({
                            "exe": str(candidate.exe),
                            "version": "",
                            "tools": "",
                            "has_tools": None,
                            "probe_error": str(exc.detail or exc),
                        })
                    except Exception as exc: # noqa: BLE001
                        self._log(f"探测异常 {candidate.exe}：{exc}", "error")
                        probed.append({
                            "exe": str(candidate.exe),
                            "version": "",
                            "tools": "",
                            "has_tools": None,
                            "probe_error": str(exc),
                        })
            except Exception as exc: # noqa: BLE001
                self._log(f"扫描 Blender 失败：{exc}", "error")
            self.queue.put({"type": "blender_found", "probed": probed})

        threading.Thread(target=job, daemon=True, name="KomaFlow-detect").start()

    def _apply_blender_list(self, probed: list[dict]) -> None:
        """探测结束：记住结果、**自动选一台**、把消息铺到主界面与设置窗口。

        ⚠️ 选择界面本身在**设置窗口**里（``EnvPicker``）。这里只负责
        决定"用哪台"，然后把结果告诉它 —— 主界面不再有下拉框了。
        """
        self._blender_probed = probed
        if not probed:
            try:
                from ..adapters.discovery import last_scan_summary
                for line in last_scan_summary().splitlines():
                    self._log(line, "warn")
            except Exception: # noqa: BLE001
                pass
            message = ("这台机器上没找到 Blender。本工具不带 Blender，得先自己装一个——\n"
                       "官网下载安装就行，装完回来点「重新找一遍」。")
            self.vars["probe"].set(message)
            self._show_env_note("没找到 Blender，去设置里装", ok=False)
            picker = getattr(self, "env_picker", None)
            if picker is not None:
                picker.set_list([])
                picker.show_choice(0)
                picker.set_hint(message)
            for panel in self._panels:
                self._update_panel_summary(panel)
            # 一个 Blender 都没有 —— **必须弹窗**。
            # 原来这里只有面板里一行小字，而"缺 mmd_tools"却会弹窗：
            # 缺插件只是功能受限，缺 Blender 是**完全没法用**，
            # 结果严重的那个反而更安静，说不过去。
            self._offer_blender_install()
            return

        # 用户以前选过哪台？选过就用他选的（设计取舍：工具不代选）。
        chosen = str(getattr(self.settings, "blender_exe", "") or "")
        for index, item in enumerate(probed):
            if chosen and str(item.get("exe")) == chosen:
                self._log(f"沿用上次选定的 Blender：{chosen}")
                self._choose_blender(index, auto=False)
                return

        # 首次启动 + 找到多台 → 把选择摆到面前（选完记住）
        if len(probed) > 1 and not getattr(self.settings, "first_run_done", False):
            self._offer_blender_pick(probed)
            return

        # 自动选：可用的优先，其次版本偏老的，最后才用缺插件的
        order = {"ok": 0, "old": 1, "notools": 2}
        best = min(range(len(probed)),
                   key=lambda i: order[env_picker.blender_status(probed[i])])
        self._choose_blender(best, auto=True)

    def _pick_blender_manual(self) -> None:
        """「自己选路径…」：选一个 blender.exe，**立刻记下并重新探测**。

        原来这条走的是素材那条 ``_browse_into`` → ``_assign``，
        只把路径塞进 ``vars["blender"]``：既不落设置，也不触发重新探测，
        所以选完界面上还是"没找到 Blender"（用户报的就是这个）。
        """
        path = filedialog.askopenfilename(
            title="选择 blender.exe",
            filetypes=[("Blender 可执行文件", "blender.exe"),
                       ("可执行文件", "*.exe"),
                       ("所有文件", "*.*")],
            parent=self.root)
        if not path:
            return
        chosen = Path(path)
        if not chosen.is_file():
            self.vars["status"].set(f"这个路径不是文件：{chosen}")
            return
        try:
            self.settings.blender_exe = str(chosen)
            self.settings.save()
        except Exception as exc: # noqa: BLE001 - 存不下也不该中断
            self._log(f"保存 Blender 路径失败：{exc}", "warn")
        self._log(f"手动指定 Blender：{chosen}（开始验证）")
        self.vars["status"].set(f"正在验证：{chosen.name} …")
        self._detect_blender_async()

    def choose_blender(self, probed: list[dict], index: int) -> None:
        """设置窗口里从下拉选中某一台时，回调到这里。"""
        self._blender_probed = list(probed)
        self._choose_blender(index)

    def _choose_blender(self, index: int, *, auto: bool = False) -> None:
        """选中某一台 Blender。``auto=True`` 表示这是程序自动挑的（措辞要说明）。"""
        probed = getattr(self, "_blender_probed", [])
        if not (0 <= index < len(probed)):
            return
        item = probed[index]
        self.vars["blender"].set(item["exe"])

        status = env_picker.blender_status(item)
        version = item.get("version") or "版本未知"
        chosen = f"Blender {version}"
        count = len(probed)
        # 先说"现在用哪台、行不行"，再说"还能换"
        if status == "notools":
            head = (f"现在用 {chosen}，但里面没装 mmd_tools 插件——"
                    f".pmx / .vmd 读不进去。换一台，或者装完插件再回来找。")
        elif status == "old":
            head = f"现在用 {chosen}（版本偏老）。渲得出来最好，不行就换一台。"
        else:
            head = (f"用 {chosen}，插件齐全，可以用。"
                    if auto else f"已切到 {chosen}，可以开工。")
        tail = f"\n机器上一共有 {count} 台，可以自己选。" if count > 1 else ""
        extra = env_picker.pick_reason(probed)
        message = head + (f"\n{extra}" if extra else "") + tail
        self.vars["probe"].set(message)
        self._log(f"选用 Blender：{item['exe']}（{chosen}，"
                  f"{env_picker.status_text(status)}）")

        # 选中一台**缺插件**的 Blender 时，弹那个柔和的说明窗。
        #
        # 为什么要主动弹：mmd_tools 是硬依赖 —— .pmx / .vmd 全靠它读，
        # 连"已配好的 .blend"路线也躲不掉（骨骼是 MMD 的就要调它挂动作）。
        # 刚装完 Blender 的用户一定没有这个插件，而原来只在面板里写一行
        # 小字，用户点开始渲染后才会撞上一个看不懂的错。
        # 这里**在还没开始渲的时候把话说清楚**，并给一条能走的路。
        # ⚠️ 先记下"这一轮到底算不算首次启动" —— 下面可能把它标记成走完了。
        # 首次启动的最后一步是**自检结果窗**（用户要的"选完版本然后自检"），
        # 它得按这个判断弹；不看这里的话，标记一改就永远弹不出来了。
        first_run_pending = not getattr(self.settings, "first_run_done", False)

        def _self_check_if_first_run() -> None:
            """首次启动的最后一步：把自检结果摆出来。"""
            if first_run_pending:
                self._offer_self_check()

        missing_now = status == "notools"
        if missing_now:
            self._was_missing_tools = True
            # 首次启动且缺插件：**先解决插件**，别拿自检结果挡着他。
            # 自检那一屏等插件装好、重新探测出"可用"之后再补。
            self._offer_tool_install(on_close=self._finish_first_run
                                     if first_run_pending else None)
        elif getattr(self, "_was_missing_tools", False):
            # 之前缺、现在有了 —— 这才值得庆祝（且只庆祝一次）。
            #
            # ⚠️ 庆祝之后还要接上自检那一屏（首次启动时）：用户刚点完
            # 「重新检测」，这时候直接静默进主界面，等于少了引导的最后一步。
            # 交给 _celebrate_ready 在窗关掉时接上，别同时开两个窗
            # （上一次的提示还开着时，第二个窗会抢焦点，看着像弹了两遍）。
            self._celebrate_ready(version=version,
                                  on_start=_self_check_if_first_run)
        else:
            _self_check_if_first_run()

        # 首次启动的引导到此结束 —— 记一笔，以后启动不再弹选择窗。
        # ⚠️ 放在**这里**（而不是 _apply_blender_list 里）的原因：
        # 用户选了版本 → 走到这儿才算"引导完成"。放在入口的话，
        # 用户还没选就被标记完成了，下次启动又会弹。
        #
        # ⚠️ 但**不能无条件记**：缺插件时这一屏还没完（自检还没给他看），
        # 早了会变成"下次启动不再弹选择窗，可自检这屏永远看不到"。
        # 所以缺插件那条路由提示窗关闭时回调 _finish_first_run 来记。
        if not missing_now:
            self._finish_first_run()

        # 铺到设置窗口（如果正开着）
        picker = getattr(self, "env_picker", None)
        if picker is not None:
            if not picker.probed:
                picker.set_list(probed)
            picker.show_choice(index)
            picker.set_hint(message)

        usable = status == "ok"
        self._show_env_note(
            f"{env_picker.MARK[status]} {chosen} · {env_picker.status_text(status)}"
            + ("" if usable else "\n要换一台，去设置里改。"),
            ok=usable)
        for panel in self._panels:
            self._update_panel_summary(panel)

    def _show_env_note(self, text: str, *, ok: bool) -> None:
        """在主界面「环境」面板里写那一行结论。"""
        label = getattr(self, "env_note", None)
        if label is None:
            return
        label.configure(text=text, fg=T.TEXT_SUB if ok else T.MAPLE)

    # ------------------------------------------------------- 缺插件时的引导
    def _offer_tool_install(self, *, on_close=None) -> None:
        """缺 mmd_tools 时弹那个柔和的说明窗。

        两个动作：
        * **下载并安装（需要联网）** —— 调 Blender 自己的扩展安装操作符。
          用户定的路线是**不随包分发**，所以这一步必须联网。
        * **我已自己装好，重新检测** —— 重跑一遍探测。

        :param on_close: 窗关掉时的回调。首次启动用它来收尾
                         （见 :meth:`_finish_first_run`）—— 这一屏代替了
                         原来"选完就直接记引导完成"的位置。

        ⚠️ 同一个窗不要弹两次：用户可能在设置窗口里来回切换选中的 Blender。
        """
        existing = getattr(self, "_dep_notice", None)
        if existing is not None and existing.window is not None:
            try:
                if existing.window.winfo_exists():
                    existing.window.lift()
                    return
            except tk.TclError:
                pass

        from .dependency_notice import DependencyNotice

        self._dep_notice = DependencyNotice(
            self.root,
            on_install=self._install_mmd_tools,
            on_recheck=self._rediscover_blender,
            on_close=on_close,
        )
        self._dep_notice.open()
        self._log("缺 mmd_tools —— 已弹出安装说明窗", "warn")

    def _celebrate_ready(self, version: str = "", *, on_start=None) -> None:
        """**确认能用之后**才弹「准备就绪」。

        ⚠️ 只有真的从"缺插件"变成"可用"时才弹 —— 否则就是骗人。
        所以这份状态记在 ``self._was_missing_tools`` 上，
        由 :meth:`_choose_blender` 在每次探测结果里维护。

        :param on_start: 点「好，开始用」之后接着做什么。首次启动用它接上
                         自检那一屏（见 :meth:`_offer_self_check`）——
                         不接的话，用户刚装好插件就直接进主界面，引导少一步。
        """
        if not getattr(self, "_was_missing_tools", False):
            return # 本来就没缺过，不用庆祝
        self._was_missing_tools = False

        existing = getattr(self, "_ready_notice", None)
        if existing is not None and existing.window is not None:
            try:
                if existing.window.winfo_exists():
                    return
            except tk.TclError:
                pass

        from .dependency_notice import ReadyNotice

        def _start() -> None:
            self._focus_main_window()
            if on_start is not None:
                on_start()

        self._ready_notice = ReadyNotice(
            self.root,
            version=version,
            on_start=_start,
            on_settings=self.on_settings,
        )
        self._ready_notice.open()
        self._log("mmd_tools 可用 —— 弹出「准备就绪」")

    def _focus_main_window(self) -> None:
        """把主窗口提到前面（「准备就绪」点了「好，开始用」之后）。"""
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    # ------------------------------------------------------- 首次启动的自检
    def self_check_facts(self) -> dict:
        """跑一遍自检，把结果整理成**可断言**的一份数据。

        用户要的流程是「第一次启动会引导到选择版本，**然后自检**」：
        选完版本得能看到"到底行不行"。这里就是那份结论 ——
        界面（:meth:`_offer_self_check`）与自检脚本都读它，
        **不在两处各算一套**。

        三个事实：Blender 版本、mmd_tools、成片要写的那块盘。

        ⚠️ 本方法必须**能在没有 Tk 主循环时也跑**（自检脚本要直接调它），
           所以除 ``self.vars`` 之外不碰任何 Tk 控件。
        """
        probed = list(getattr(self, "_blender_probed", []) or [])
        exe = ""
        try:
            exe = str(self.vars["blender"].get() or "")
        except (AttributeError, KeyError, tk.TclError):
            exe = ""
        item = next((row for row in probed if str(row.get("exe")) == exe), None)
        if item is None:
            item = next((row for row in probed if row.get("has_tools")), None)
        if item is None and probed:
            item = probed[0]

        blender = str((item or {}).get("version") or "版本未知")
        # ⚠️ 判定只看 ``has_tools``，**不能**要求版本号也读到了 ——
        # 有些装法探不到 manifest，那样会把"明明能用"报成缺插件。
        tools_ok = bool((item or {}).get("has_tools"))
        tools = str((item or {}).get("tools") or "")
        if tools_ok and not tools:
            tools = "已装好" # 探不到版本号也是"装好了"，别写"未知插件"
        if not tools:
            tools = "没找到"

        # 成片要写的那块盘。目录还没定就退回中间产物目录，
        # 再没有就问当前目录 —— 跟界面底部那行"所在盘剩余"同一套取值。
        output_dir = ""
        try:
            output_dir = (self.vars["output_dir"].get()
                          or self.vars["work_dir"].get())
        except (AttributeError, KeyError, tk.TclError):
            output_dir = ""
        output_dir = str(output_dir or Path.cwd())

        free = budget.free_space(output_dir)
        if free is None:
            disk, disk_ok = "", True # 查不到不拦人，也不冤枉它
        else:
            drive = ""
            try:
                drive = Path(output_dir).drive
            except (OSError, ValueError):
                drive = ""
            where = f"{drive} 盘" if drive else "成片那块盘"
            disk = f"{where}剩 {budget.format_size(free)}"
            disk_ok = free >= 3 * 1024 ** 3 # 3 GB 以下就该说一声

        return {
            "blender": blender,
            "tools": tools or "没找到",
            "tools_ok": tools_ok,
            "disk": disk,
            "disk_ok": disk_ok,
            "output_dir": output_dir,
            "ready": bool(tools_ok and disk_ok and disk),
        }

    def _offer_self_check(self) -> None:
        """首次启动的最后一屏：把自检结果摆出来再放人进主界面。"""
        existing = getattr(self, "_self_check_notice", None)
        if existing is not None and existing.window is not None:
            try:
                if existing.window.winfo_exists():
                    existing.window.lift()
                    return
            except tk.TclError:
                pass

        facts = self.self_check_facts()
        from .dependency_notice import SelfCheckNotice

        self._self_check_notice = SelfCheckNotice(
            self.root,
            blender=facts["blender"],
            tools=facts["tools"],
            tools_ok=facts["tools_ok"],
            disk=facts["disk"],
            disk_ok=facts["disk_ok"],
            output_dir=facts["output_dir"],
            # 写的是**本工具**的版本号（脚注那行）—— Blender 的版本号在正文里
            version=APP_VERSION,
            on_start=self._focus_main_window,
            on_settings=self.on_settings,
        )
        self._self_check_notice.open()
        self._log(f"自检：Blender {facts['blender']} · "
                  f"mmd_tools {facts['tools']} · "
                  f"{facts['disk'] or '盘剩余查不到'}"
                  + ("（全部通过）" if facts["ready"] else "（有要注意的地方）"))

    def _finish_first_run(self) -> None:
        """首次启动引导走完了，记一笔 —— 以后不再弹选择窗，也不再弹自检。

        ⚠️ 必须**幂等**：正常路径上 :meth:`_choose_blender` 与提示窗的
           ``on_close`` 都会调它，重复调用不能出问题（也不能重复写盘）。
        """
        if getattr(self.settings, "first_run_done", False):
            return
        try:
            self.settings.first_run_done = True
            self.settings.save()
            self._log("首次启动引导已完成，已记入设置")
        except Exception as exc: # noqa: BLE001
            self._log(f"记首次启动标记失败（不影响使用）：{exc}", "warn")

    def _install_mmd_tools(self) -> None:
        """从 Blender 官方插件库装 mmd_tools（需要联网）。

        走 Blender 自己的扩展安装操作符，不自己写下载逻辑 ——
        免得跟着官方仓库的改动跑。
        """
        exe = self.vars["blender"].get().strip()
        if not exe:
            messagebox.showwarning("先选一台 Blender",
                                   "要先在设置里指定用哪台 Blender，才能给它装插件。")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("先等一下", "正在跑别的任务，等它结束再装。")
            return
        if not messagebox.askyesno(
                "要联网安装",
                "将从 Blender 官方插件库下载 mmd_tools 并安装到：\n\n"
                f"{exe}\n\n"
                "这一步需要联网（下载完就不再需要网了）。现在开始吗？"):
            return

        self.vars["status"].set("正在从官方插件库安装 mmd_tools…")
        self._log("开始安装 mmd_tools（联网）")

        def job() -> None:
            try:
                from ..adapters import registry
                # ⚠️ ``VersionTuple`` 就是 ``tuple[int, ...]`` 的别名
                # （不是类，别当构造器用）。按已探测到的版本号选适配器；
                # 拿不到版本号时给个 5.x 的保守值 —— 装插件这件事
                # 在 5.x 上走的是同一套操作符。
                version = (5, 0, 0)
                for row in getattr(self, "_blender_probed", []):
                    if str(row.get("exe")) != exe:
                        continue
                    raw = str(row.get("version") or "").split(".")
                    try:
                        parts = tuple(int(x) for x in raw[:3] if x.strip())
                    except ValueError:
                        parts = ()
                    if len(parts) >= 2:
                        version = parts
                    break
                adapter = registry.select_adapter(version)
                outcome = adapter.install_mmd_tools(exe)
            except Exception as exc: # noqa: BLE001
                self.queue.put({"type": "mmd_install", "ok": False,
                                "message": f"{type(exc).__name__}: {exc}"})
            else:
                self.queue.put({"type": "mmd_install",
                                "ok": bool(outcome.get("ok")),
                                "message": str(outcome.get("message") or "")})

        self.worker = threading.Thread(target=job, daemon=True,
                                       name="KomaFlow-install")
        self.worker.start()

    def _rediscover_blender(self) -> None:
        """重新探测本机 Blender（用户可能刚自己装好插件）。"""
        self._log("重新探测 Blender…")
        self.vars["status"].set("正在重新探测 Blender…")
        self._detect_blender_async()

    # ------------------------------------------------- 没找到 Blender / 选哪台
    def _offer_blender_install(self) -> None:
        """一个 Blender 都没找到 —— 弹窗告诉他装哪个、去哪装。"""
        existing = getattr(self, "_missing_blender_notice", None)
        if existing is not None and existing.window is not None:
            try:
                if existing.window.winfo_exists():
                    existing.window.lift()
                    return
            except tk.TclError:
                pass

        from .dependency_notice import MissingBlenderNotice

        self._missing_blender_notice = MissingBlenderNotice(
            self.root,
            on_redetect=self._rediscover_blender,
            on_download=self._open_blender_download,
        )
        self._missing_blender_notice.open()
        self._log("没找到 Blender —— 已弹出引导窗", "warn")

    def _open_blender_download(self) -> None:
        """打开 Blender 官网下载页（用系统默认浏览器）。"""
        url = "https://www.blender.org/download/"
        try:
            import webbrowser
            webbrowser.open(url)
            self._log(f"已打开下载页：{url}")
        except Exception as exc: # noqa: BLE001
            self._log(f"打不开浏览器：{exc}", "warn")
            messagebox.showinfo("自己去下载", f"用浏览器打开这个地址：\n\n{url}")

    def _offer_blender_pick(self, probed: list[dict]) -> None:
        """找到多台 —— 让用户选一台，选完记进设置。"""
        existing = getattr(self, "_blender_pick_notice", None)
        if existing is not None and existing.window is not None:
            try:
                if existing.window.winfo_exists():
                    existing.window.lift()
                    return
            except tk.TclError:
                pass

        from .dependency_notice import BlenderPickNotice

        self._blender_pick_notice = BlenderPickNotice(
            self.root,
            candidates=probed,
            on_pick=lambda index: self._confirm_blender_pick(probed, index),
        )
        self._blender_pick_notice.open()
        self._log(f"找到 {len(probed)} 台 Blender —— 已弹出选择窗")

    def _confirm_blender_pick(self, probed: list[dict], index: int) -> None:
        """用户选定了某台：记进设置（下次不再问），再走正常的选择流程。"""
        if not (0 <= index < len(probed)):
            return
        exe = str(probed[index].get("exe") or "")
        if exe:
            try:
                self.settings.blender_exe = exe
                self.settings.save()
                self._log(f"已记住选定的 Blender：{exe}")
            except Exception as exc: # noqa: BLE001
                self._log(f"记住选择失败（不影响使用）：{exc}", "warn")
        self._choose_blender(index)

    def on_open_project(self) -> None:
        path = filedialog.askopenfilename(title="打开工程",
                                          filetypes=FILE_TYPES["project"])
        if not path:
            return
        project = Project.load(path)
        mapping = {
            "blender": project.blender_exe or "", "model": project.inputs.model or "",
            "camera": project.inputs.camera or "",
            "stage": project.inputs.stage or "", "blend": project.inputs.blend or "",
            "name": project.output.basename, "output_dir": project.output.dir,
            "preroll": str(project.params.preroll_frames),
            "fps": str(project.params.fps), "width": str(project.params.width),
            "height": str(project.params.height),
            "frame_start": str(project.params.frame_start),
            "frame_end": "" if project.params.frame_end is None
                         else str(project.params.frame_end),
            # 档位是从宽高反推的，不存盘 —— 工程文件里只有宽高（唯一真相）
            "resolution": self._preset_for(project.params.width, project.params.height),
            "framing": FRAMING_REVERSE.get(project.params.framing, "全身"),
        }
        for key, value in mapping.items():
            self.vars[key].set(value)
        # 动作：还原成有序列表（老工程档已经从 motion/expression 迁过来了，
        # 见 core.project._migrate_motion_fields）
        self._set_motion_paths([*project.inputs.motions,
                                *project.inputs.expressions])
        # 渲染背景：控件取值 + 编码器。
        #
        # ⚠️ **必须连"用户拨过没有"一起还原**（``transparent_touched``）：
        # 少了它，打开一个"用户明确选了照背景渲"的工程时，控件会被默认
        # 规则重算成别的值 —— 等于把他的选择悄悄改掉了。
        self._transparent_user_set = bool(project.params.transparent_touched)
        self.transparent.set("开" if project.params.transparent_background else "关")
        # 物理：已改为 mmd_tools 原生（无开关、无档位，不用回填）
        # 存的就是内部键，直接用；文件里万一有怪值就退回默认
        self.alpha_codec.set(project.params.alpha_codec
                             if project.params.alpha_codec in ALPHA_CODEC_CHOICES
                             else budget.DEFAULT_ALPHA_CODEC)   # A9：原来漏了 budget. 前缀，打开非法 alpha_codec 的工程必崩
        self.vars["limit"].set("" if project.params.segment_limit is None
                               else str(project.params.segment_limit))
        self._refresh_cards()
        self._log(f"已打开工程：{path}")

    def on_save_project(self) -> None:
        """保存工程。默认落在设置里的「工程文件」目录。"""
        initial_dir = self.settings.project_dir
        if initial_dir and not Path(initial_dir).is_dir():
            try:
                Path(initial_dir).mkdir(parents=True, exist_ok=True)
            except OSError:
                initial_dir = ""
        path = filedialog.asksaveasfilename(
            title="保存工程", defaultextension=".kfproj",
            initialdir=initial_dir or None,
            initialfile=f"{self.vars['name'].get() or 'KomaFlow'}.kfproj",
            filetypes=FILE_TYPES["project"])
        if not path:
            return
        try:
            self._collect_project().save(path)
        except ValueError:
            messagebox.showerror("参数不对", "分辨率 / 帧率 / 帧号这类字段必须是整数。")
            return
        self._log(f"已保存工程：{path}")

    def on_settings(self) -> None:
        """打开设置窗口。改完立刻生效（路径、缩放、渲染环境都现场应用）。"""
        window = settings_window.open_settings(
            self.root, self.settings, on_apply=self._apply_settings,
            on_rediscover=self.on_discover,
            on_blender_pick=self.choose_blender,
            on_blender_manual=self._pick_blender_manual,
            on_ffmpeg_download=self._download_ffmpeg_async)
        picker = getattr(window, "env_picker", None)
        if picker is None:
            return
        # 把当前探测结果喂给选择器，它才知道有哪些台可选
        probed = getattr(self, "_blender_probed", [])
        picker.set_list(probed)
        chosen = self.vars["blender"].get()
        index = next((i for i, item in enumerate(probed)
                      if item.get("exe") == chosen), 0)
        picker.show_choice(index)
        picker.set_hint(self.vars["probe"].get())

        # ⚠️ 主程序要一直记着这个选择器，之后重新探测/切台才能同步过去。
        # 但窗口一关它就销毁了 —— 不清掉的话，下次探测还会去 `configure`
        # 一个已经没了的控件，Tk 会抛 `invalid command name`。
        self.env_picker = picker

        def forget(_event=None) -> None:
            if getattr(self, "env_picker", None) is picker:
                self.env_picker = None

        window.window.bind("<Destroy>", forget, add="+")

    def _apply_settings(self, settings) -> None:
        """设置保存之后的联动：路径填回输入框、缩放换档。"""
        self.vars["output_dir"].set(settings.output_dir)
        self.vars["work_dir"].set(settings.work_dir)
        JOURNAL.bind(settings.work_dir)
        if abs(float(settings.ui_zoom) - T.UI_ZOOM) > 0.001:
            self.set_zoom(float(settings.ui_zoom))
        self.vars["status"].set("设置已保存")
        self._log(f"设置已保存：{settings.path()}")

    def _save_render_snapshot(self, project: Project) -> Path | None:
        """点「开始渲染」时，把**这一次装配好的东西**存一份到快照目录。

        存两种文件，各有各的用处：

        * ``.kfproj`` —— 这次的工程参数（素材路径、分辨率、透明开关…）。
          想复现或微调这次渲染，拿它重新打开就行。
        * ``work.blend`` —— 工具**真正喂给 Blender 的那份场景**（已经导入好
          模型、挂好动作、设好相机）。想自己进 Blender 手动调，拿这个。

        ⚠️ 有几个顺序上的讲究：

        * 在**开渲之前**存。装配是渲染的第一步，所以快照里的 ``work.blend``
          是**上一次**装配的结果 —— 如果这次参数变了，它可能还没更新。
          所以文件名带时间戳，并在文件里注明参数来源，别让人误以为它是本次产物。
        * 快照**失败不能拦住渲染**：盘满、目录没权限都可能发生，
          但用户的正文是"把片子渲出来"，不是"存快照"。
        """
        if not self.settings.snapshot_enabled:
            return None

        target_dir = Path(self.settings.snapshot_dir)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        stem = f"{project.output.basename}_{stamp}"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            # ① 工程参数
            params_out = target_dir / f"{stem}.kfproj"
            project.save(params_out)
            # ② 装配好的场景（有就一起带上）
            blend_src = Path(self.vars["work_dir"].get()) / "work.blend"
            blend_out = None
            if blend_src.is_file():
                blend_out = target_dir / f"{stem}_assembled.blend"
                shutil.copy2(blend_src, blend_out)
        except OSError as exc:
            note = f"渲染快照没存成（不影响渲染）：{exc}"
            self._log(note, "warn")
            self._set_notice(note, error=False)
            return None

        self.last_snapshot = params_out
        self._log(f"渲染快照已存：{params_out}"
                  + (f"；场景 {blend_out}" if blend_out else "（本次还没有装配好的场景）"))
        return params_out

    def on_open_snapshots(self) -> None:
        """打开快照目录 —— 让用户能找到那份"可调的东西"。"""
        target = Path(self.settings.snapshot_dir)
        if not target.is_dir():
            messagebox.showinfo("还没有快照",
                               f"快照目录还不存在：\n{target}\n\n"
                               "点一次「开始渲染」之后就会有。")
            return
        try:
            os.startfile(str(target)) # noqa: S606
        except (AttributeError, OSError):
            messagebox.showinfo("快照目录", str(target))

    def on_about(self) -> None:
        messagebox.showinfo(
            "关于",
            f"{APP_NAME} {APP_VERSION}\n\n"
            "把 MMD 模型渲成视频。\n"
            "导入素材 → 预热 → 分段渲染 → 合并 → 出片。\n\n"
            "· 不自带 Blender，调用你自己装的那个\n"
            "· 只覆盖分辨率与帧率，材质/灯光/色彩一律沿用你的设置\n"
            "· 全程离线，不收集任何数据\n\n"
            "LOGO 与角色均为原创。",
        )

    def on_open_logs(self) -> None:
        """打开后台日志目录。界面不显示日志，但排查时得让人找得到。"""
        target = JOURNAL.directory
        if target is None or not target.is_dir():
            messagebox.showinfo("还没有日志", "开始一次渲染之后才会有日志文件。")
            return
        try:
            os.startfile(str(target)) # noqa: S606
        except (AttributeError, OSError):
            messagebox.showinfo("日志目录", str(target))

    def _set_notice(self, text: str, *, error: bool = True) -> None:
        """底部那条"简易报错"。只在真出问题时出现，一句话说清 + 建议。"""
        self.notice.configure(text=text, fg=T.DANGER if error else T.WARN)
        # 助手也跟着表态：出错就皱眉，清掉了就回到待机
        if hasattr(self, "helper"):
            if text and error:
                self.helper.set_state("fail", text)
            elif not text:
                self.helper.set_state("idle", "素材放好啦，等你点头。")

    def on_open_output(self) -> None:
        target = (self.output_path.parent if self.output_path
                  else Path(self.vars["output_dir"].get()))
        if not target.is_dir():
            messagebox.showwarning("还没有输出", f"目录还不存在：{target}")
            return
        try:
            os.startfile(str(target)) # noqa: S606 - 桌面程序打开目录是预期行为
        except AttributeError:
            messagebox.showinfo("输出目录", str(target))
        except OSError as exc:
            messagebox.showwarning("打不开", f"{exc}\n\n目录：{target}")

    # ------------------------------------------------------------ 工程收集
    def _collect_project(self) -> Project:
        def as_int(key: str, default=None):
            raw = self.vars[key].get().strip()
            return default if not raw else int(raw)

        name = self.vars["name"].get().strip() or self._default_output_name()
        blend = self.vars["blend"].get().strip()
        model = self.vars["model"].get().strip()
        # ⚠️ **兜底**：``model`` 里是指向 .blend 的话，它其实是"整份已配好的工程"。
        # 正常路径不会走到这儿（``_assign`` 会把它放进 ``blend``），
        # 但历史状态、手改工程档、或以后新加的入口都可能漏。
        # 不兜这一下，build.py 会拿 PMX 导入器去读 .blend，
        # 报一句"File signature is invalid."，用户只看到"不能渲染"。
        if model and model.lower().endswith(".blend") and not blend:
            blend, model = model, ""
            self._log(f"「{os.path.basename(blend)}」是 .blend，按整份工程处理。", "warn")
            # ⚠️ 用户报过「场景导进去就消失了」「动作导进去就消失了」，根因就是这一跳：
            # 角色卡放的是 .blend 时整条路线变成「整份工程」——
            # build.py 那条路**不导入舞台、也不按动作重排相机**，
            # 于是用户明明放了场景和动作，出片里却两样都没有。
            # 不拦他（那是合法用法），但必须当场说清楚缺了什么。
            if self.vars["stage"].get().strip() or self._motion_paths():
                dropped = []
                if self.vars["stage"].get().strip():
                    dropped.append("场景（舞台）")
                if self._motion_paths():
                    dropped.append("动作")
                note = ("当前是「整份工程」模式：" + "、".join(dropped) +
                        " 不会参与渲染。想让它们生效，把角色卡里的 .blend 换成 .pmx"
                        "（.blend 只放「舞台」卡当场景）。")
                self._log(note, "warn")
                self.vars["status"].set(note)
        project = Project(
            name=name,
            inputs=Inputs(
                route="blend" if blend else "assets",
                model=model or None,
                # 动作是**一个有序列表**（数量不限），不再分"动作 / 表情"两个字段。
                # 顺序就是用户拖进来的顺序 —— 后导入的覆盖同名骨骼。
                motions=self._motion_paths(),
                camera=self.vars["camera"].get().strip() or None,
                stage=self.vars["stage"].get().strip() or None,
                blend=blend or None,
            ),
            output=Output(dir=self.vars["output_dir"].get().strip() or "output",
                          basename=name),
            blender_exe=self.vars["blender"].get().strip() or None,
        )
        params = project.params
        params.preroll_frames = as_int("preroll", 30)
        params.segment_limit = as_int("limit", 1000)
        params.fps = as_int("fps", 30)
        params.width = as_int("width", 2560)
        params.height = as_int("height", 1440)
        params.frame_start = as_int("frame_start", 0)
        params.frame_end = as_int("frame_end", None)
        # `quality` 不再由界面提供（画质档已删，那个参数在我们的流程里没用）。
        # 这里保留默认值，是因为**旧工程文件里可能存着它** —— 读到就留着，
        # 只是界面上不再有旋钮。见 project.py 的 Params.quality。
        params.framing = FRAMING_LABELS.get(self.vars["framing"].get(), "full")
        # 渲染背景：存的是"用户明确想要的"，能不能生效另算
        # （派生规则在 core/project.effective_transparent）
        params.transparent_background = self._current_transparent()
        # ⚠️ 这个记号必须一起存：没有它，下次打开工程就分不清
        # "用户明确选了渲背景"和"还没轮到默认规则"，会把他改掉。
        params.transparent_touched = bool(
            getattr(self, "_transparent_user_set", False))
        params.alpha_codec = self._current_codec()
        # 物理：恒开，走 mmd_tools 自带那一套（导入带 PHYSICS + build_rig + 烘焙）
        # 观感：固定用能力包那份（最近一次「渲染模型」存出的工程）
        params.look_from_blend = ""
        look = str(getattr(self.settings, "last_look_blend", "") or "")
        if look and Path(look).is_file():
            params.look_from_blend = look
        return project

    # ---------------------------------------------------------- 日志 / 状态
    def _log(self, message: str, level: str = "info") -> None:
        """**界面不显示日志** —— 全丢给后台文件，需要排查时再去看。

        用户口径：用户只需要看到"步骤走到哪"和"一句话报错"。
        """
        JOURNAL.write(message, level)

    def _tick(self) -> None:
        self.vars["clock"].set(time.strftime("%H:%M:%S"))
        if self.started_at and self.worker and self.worker.is_alive():
            elapsed = time.time() - self.started_at
            base = self.vars["status"].get().split("　已用")[0]
            self.vars["status"].set(f"{base}　已用 {self._duration(elapsed)}")
        self._schedule("_tick_job", 1000, self._tick)

    @staticmethod
    def _duration(seconds: float) -> str:
        seconds = max(0, int(seconds))
        hours, rest = divmod(seconds, 3600)
        minutes, secs = divmod(rest, 60)
        if hours:
            return f"{hours}小时{minutes}分"
        if minutes:
            return f"{minutes}分{secs}秒"
        return f"{secs}秒"

    def _set_busy(self, busy: bool) -> None:
        """进/出"正在跑"的状态。

        ⚠️ **收尾必须把界面解锁**（``mode`` 回 ``"config"``）。
        踩过的坑：``self.mode = "run"`` 在开始渲染时设上，但**全文件没有一处把它
        改回来** —— 于是点过一次「开始渲染」之后，拖放、点卡片选文件全被
        ``if self.mode == "run": return`` 挡掉，**改不了素材**。
        而渲染失败走的是 ``_fail()``，它连 ``_set_busy(False)`` 都不调，
        锁得更死。用户报的"失败时无法更改文件"就是这个。

        ⚠️ 这里**只复位 mode、不重画卡片**：重画会把刚标好的"失败"擦掉，
        用户就看不到是哪一步挂了。卡片什么时候回投放态，由
        :meth:`reset_cards_to_drop` 决定（用户下一次操作时调）。
        """
        # ⚠️ 「组装为工程」也要一起禁用 —— 它同样会起 Blender 子进程，
        # 跟渲染/预览抢机器，而且两种活并行会让日志与进度互相打岔。
        self.btn_start.set_enabled(not busy)
        self.btn_assemble.set_enabled(not busy)
        self.btn_pause.set_enabled(busy and self.control.supported)
        self.btn_stop.set_enabled(busy)
        if not busy:
            self.btn_pause.set_text("暂停")
            self.mode = "config"

    def reset_cards_to_drop(self) -> None:
        """把卡片从"步骤态"恢复成"素材投放态"。

        在**用户下一次动手**时调（拖文件、点卡片、点开始），而不是失败当下就调 ——
        失败当下就恢复的话，"失败"标记立刻被擦掉，用户不知道哪一步挂了。
        """
        if self.mode != "config":
            return
        if not any(card.state != EMPTY for card in self.cards.values()):
            return
        self._refresh_cards()

    # ----------------------------------------------------------------- 动作
    def _look_source_ready(self) -> bool:
        """三视图那份工程在不在？不在就提示用户先去渲，并返回 False。

        为什么必须拦：观感固定用能力包，而材质只能从「渲染模型」存出的工程里搬。
        没有它就只能按 MMD 原味渲 —— 那就不是用户要的画面了，不如提前说清楚。
        """
        # 角色卡里如果是「渲染模型」存出的 .blend，观感已经带在里面了 —— 不用再拦
        model = str(self.vars["model"].get() or "").strip()
        if model and model.lower().endswith(".blend") and Path(model).is_file():
            return True
        blend = str(self.vars["blend"].get() or "").strip()
        if blend and Path(blend).is_file():
            return True
        look = str(getattr(self.settings, "last_look_blend", "") or "").strip()
        if look and Path(look).is_file():
            return True
        messagebox.showinfo(
            "先渲三视图",
            "观感要用能力包的那套材质，而材质来自「渲染模型」渲出来的工程。\n\n"
            "请先点「渲染模型」——渲完看一眼三视图，满意就点「继续」导入角色模型，\n"
            "然后再回来预览 / 开始渲染。",
            parent=self.root)
        self.vars["status"].set("还没渲三视图：先点「渲染模型」")
        self._log("预览/渲染被拦下：还没有三视图工程（观感来源）", "warn")
        return False

    def on_still(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self._look_source_ready():
            return
        try:
            project = self._collect_project()
            frame = int(self.vars["still_frame"].get().strip() or 0)
        except ValueError:
            messagebox.showerror("参数不对", "帧号必须是整数。")
            return

        self._reset_cards_to_steps()
        self.cancel = threading.Event()
        work_dir = self.vars["work_dir"].get() # Tk 调用必须在主线程
        JOURNAL.bind(work_dir)
        self._set_notice("")
        self._set_busy(True)
        self.pill.set("预览中", T.NAVY_TINT, T.NAVY)
        self.vars["status"].set("正在渲单帧预览…")
        self._log("── 单帧预览 ──")

        def job() -> None:
            try:
                path = render_still(project, work_dir, frame,
                                    blender_exe=project.blender_exe,
                                    name=self.vars["still_name"].get().strip(),
                                    on_event=self.queue.put, cancel=self.cancel)
                self.queue.put({"type": "still_open", "path": str(path)})
            except Exception as exc: # noqa: BLE001
                self.queue.put({"type": "error",
                                "message": f"{type(exc).__name__}: {exc}"})
            finally:
                self.queue.put({"type": "finished"})

        self.started_at = time.time()
        self.worker = threading.Thread(target=job, daemon=True, name="KomaFlow-still")
        self.worker.start()

    def on_assemble(self) -> None:
        """「组装为工程」—— 不渲片，只把素材组装成一份 Blender 工程交给用户。

        用户原话：

        > 左上角生成视频旁边留出一个把所有东西组装在一起的 Blender 格式的选择
        > 给用户，也就是最终文件，以供用户自己微调更改。

        所以这里做的是：走**同一条装配链**（``assemble_project``，不是另写一套），
        然后把 ``work.blend`` 复制到用户挑的地方。那份工程里
        角色 + 动作 + 表情 + 镜头 + 舞台全在，打开就能自己调。

        ⚠️ 复制而不是"直接交出工作目录里那份"：工作目录里那份下次渲染会被覆盖，
           而且夹在一堆中间产物中间，用户根本找不到。
        """
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("先等一下", "正在跑别的任务，等它结束再说。")
            return
        try:
            project = self._collect_project()
        except ValueError:
            messagebox.showerror("参数不对", "分辨率 / 帧率 这类字段必须是整数。")
            return

        problems = project.validate()
        if problems:
            messagebox.showwarning(
                "还不能组装",
                "还差这些，补上就能组装了：\n\n"
                + "\n".join(f"· {p}" for p in problems))
            return

        # 默认文件名跟成片一致，用户一眼能对上
        default_name = f"{project.output.basename or 'KomaFlow'}.blend"
        target = filedialog.asksaveasfilename(
            title="把组装好的 Blender 工程存到哪",
            initialdir=project.output.dir or None,
            initialfile=default_name,
            defaultextension=".blend",
            filetypes=[("Blender 工程", "*.blend"), ("所有文件", "*.*")])
        if not target:
            return

        self.cancel = threading.Event()
        self.control = ProcessControl()
        work_dir = self.vars["work_dir"].get() # Tk 调用必须在主线程
        JOURNAL.bind(work_dir)
        self._set_notice("")
        self._set_busy(True)
        self.vars["status"].set("正在组装…（把角色、动作、镜头、舞台合到一个工程里）")
        self.pill.set("组装中", T.BEIGE_SOFT, "#8A6A2F")
        self._log(f"── 组装为工程 → {target} ──")

        def job() -> None:
            try:
                result = assemble_project(
                    project, work_dir, blender_exe=project.blender_exe,
                    on_event=self.queue.put, cancel=self.cancel,
                    control=self.control)
            except Exception as exc: # noqa: BLE001
                self.queue.put({"type": "assemble_finished", "ok": False,
                                "errors": [f"{type(exc).__name__}: {exc}"],
                                "target": target})
            else:
                self.queue.put({"type": "assemble_finished", "ok": result.ok,
                                "source": str(result.work_blend or ""),
                                "target": target, "errors": result.errors,
                                "frames": result.total_frames})

        self.worker = threading.Thread(target=job, daemon=True,
                                       name="KomaFlow-assemble")
        self.worker.start()


    def _ensure_ffmpeg(self, project) -> bool:
        """透明 MOV 需要外部 FFmpeg。没有就**问一句，然后自动下载**。

        返回 True 表示可以继续；False 表示用户取消了本次渲染。
        为什么自己做下载：目标用户往往只有 Blender，让他们去官网找 zip、
        解压、配 PATH，等于劝退。
        """
        needs = project.effective_transparent() and project.alpha_codec_name() != "png"
        if not needs:
            return True
        from komaflow.media.ffmpeg import find_ffmpeg
        try:
            find_ffmpeg()
            return True
        except Exception:      # noqa: BLE001  找不到才往下走
            pass

        answer = messagebox.askyesno(
            "透明 MOV 需要 FFmpeg",
            "输出透明 MOV（带 alpha 的视频）需要 FFmpeg，它不随本软件分发。\n\n"
            "要现在自动下载吗？约 90 MB，来自第三方构建站（BtbN / gyan.dev，不是 FFmpeg 项目自己的官方发布）。\n"
            "下载后放在本机用户目录，只用于编码，不会上传任何东西。\n\n"
            "不想下载的话：把输出格式改成「MP4」或「PNG 图片序列」，这两条都不需要它。",
        )
        if not answer:
            self._log("已取消：透明 MOV 需要 FFmpeg，用户选择不下载")
            return False

        # A10：这里原来是**在 Tk 主线程里同步下载 90 MB** ——
        # 用户一点"是"，整个界面就冻住（不能取消、看不到进度），慢网能冻几分钟。
        # 改成复用首启那套后台下载，并在状态栏告诉他再点一次。
        self._download_ffmpeg_async()
        self.vars["status"].set("正在后台下载 FFmpeg…下载完成后请再点「开始渲染」")
        return False

    def on_start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self._look_source_ready():
            return
        try:
            project = self._collect_project()
        except ValueError:
            messagebox.showerror("参数不对", "分辨率 / 帧率 / 帧号这类字段必须是整数。")
            return

        problems = project.validate()
        if problems:
            messagebox.showwarning(
                "还不能开始",
                "还差这些，补上就能跑了：\n\n" + "\n".join(f"· {p}" for p in problems))
            return

        # 盘够不够 —— **渲之前拦，不要渲到一半写满盘**。
        # 透明那条路（PNG 序列 + MOV）2K 三分钟就是几十 GB，这一道必须有。
        if not self._disk_ok(project):
            return

        # 透明 MOV 需要外部 FFmpeg：缺了就当场问、当场下载
        if not self._ensure_ffmpeg(project):
            return

        # 存一份"这次要渲的东西"快照，用户事后可以自己进 Blender 调
        self._save_render_snapshot(project)

        self._reset_cards_to_steps()
        self.started_at = time.time()
        self.last_percent = 0.0
        self.cancel = threading.Event()
        self.control = ProcessControl()
        work_dir = self.vars["work_dir"].get() # Tk 调用必须在主线程
        JOURNAL.bind(work_dir)
        self._set_notice("")
        self._set_busy(True)
        self._reset_batches()
        self.pill.set("运行中", T.BEIGE_SOFT, "#8A6A2F")
        if hasattr(self, "helper"):
            self.helper.set_state("work", "我盯着进度，摇铃报数。")
        self.vars["status"].set("正在执行…")
        self._log("── 开始执行 ──")

        def job() -> None:
            try:
                result: RunResult = run_project(
                    project, work_dir, blender_exe=project.blender_exe,
                    on_event=self.queue.put, cancel=self.cancel, control=self.control,
                )
                self.queue.put({"type": "run_finished", "result": result})
            except Exception as exc: # noqa: BLE001
                self.queue.put({"type": "error",
                                "message": f"{type(exc).__name__}: {exc}"})
            finally:
                self.queue.put({"type": "finished"})

        self.worker = threading.Thread(target=job, daemon=True, name="KomaFlow-run")
        self.worker.start()

    def on_pause(self) -> None:
        if not (self.worker and self.worker.is_alive()):
            return
        if not self.control.supported:
            messagebox.showinfo("不支持暂停", "当前系统不支持挂起渲染进程。")
            return
        paused = self.control.toggle()
        self.btn_pause.set_text("继续" if paused else "暂停")
        self.vars["status"].set("已暂停（进程挂起，随时可继续）" if paused else "继续执行…")
        self.pill.set("已暂停" if paused else "运行中",
                      T.BEIGE_SOFT if paused else T.BEIGE_SOFT,
                      "#8A6A2F" if paused else "#8A6A2F")
        self._log("已暂停渲染进程" if paused else "已继续渲染")

    def on_cancel(self) -> None:
        if self.cancel is not None and self.worker and self.worker.is_alive():
            if self.control.paused.is_set():
                self.control.paused.clear()
                self.btn_pause.set_text("暂停")
            self.cancel.set()
            self.vars["status"].set("已请求终止 —— 已渲的帧留在中间产物目录里")
            self._log("已请求终止。已完成的段不会白渲，重新开始会自动跳过。")
            _wd = self.vars["work_dir"].get()
            self._log(f"中间产物（已渲的帧）在：{Path(_wd) / 'segments'}")

    def on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("还在渲染",
                                       "渲染还没结束。确定要退出吗？\n"
                                       "（正在渲的那一段会作废，已完成的段会保留）"):
                return
            if self.cancel is not None:
                self.cancel.set()
        self._shutdown()
        self.root.destroy()

    def _shutdown(self) -> None:
        """关窗前把循环定时器与提示窗都收掉。

        ⚠️ 不收的话，``destroy()`` 之后那几条 ``after`` 回调还会再跑一次，
        控制台上刷一屏 ``invalid command name "..._poll_queue"`` ——
        功能上无害，但看着像崩了，而且会掩盖真的报错。

        这里清单要**跟所有 ``after`` 挂上的长期任务对齐**。
        漏掉一个就会在关窗时冒一条 Tcl 报错 —— 比如 ``_polish_title_bar``
        那条 140ms 的延迟任务，曾经漏在里面。
        """
        jobs = ("_poll_job", "_tick_job", "_preview_resize_job",
                "_titlebar_job", "_titlebar_map_job", "_icon_job",
                # 拖放轮询与"映射后补装拖放"也是定时器，不取消会在销毁后跑一次
                "_drop_job", "_dnd_map_job")
        for job in jobs:
            handle = getattr(self, job, None)
            if handle is None:
                continue
            try:
                self.root.after_cancel(handle)
            except (tk.TclError, ValueError):
                pass
            setattr(self, job, None)
        tip.close_all()

    # --------------------------------------------------------------- 事件泵
    def _poll_queue(self) -> None:
        try:
            while True:
                self._handle_event(self.queue.get_nowait())
        except queue.Empty:
            pass
        # 记住 job id：窗口关掉时要能取消，否则销毁之后这条回调还会跑一次，
        # 在控制台上刷一整屏 `invalid command name "..._poll_queue"`。
        self._schedule("_poll_job", 120, self._poll_queue)

    def _handle_event(self, event: dict) -> None:
        kind = event.get("type")

        if kind == "blender_found":
            self._apply_blender_list(event.get("probed") or [])
            return

        if kind == "mmd_install":
            # 装 mmd_tools 的结果。成功就重新探测一遍（让界面状态跟着变），
            # 失败就把**原因**原样告诉用户 —— 最常见的就是断网。
            ok = bool(event.get("ok"))
            message = str(event.get("message") or "")
            if ok:
                self._log(f"mmd_tools 安装成功：{message}")
                messagebox.showinfo("装好了", message or "mmd_tools 已装好并启用。")
                self._rediscover_blender()
            else:
                self._log(f"mmd_tools 安装失败：{message}", "error")
                messagebox.showerror(
                    "没装上",
                    (message or "安装失败。") +
                    "\n\n这一步要从 Blender 官方插件库下载，需要联网。"
                    "如果网络没问题，也可以自己在 Blender 里装：\n"
                    "编辑 → 偏好设置 → 获取扩展，搜索 MMD Tools。")
            return

        if kind == "stage":
            stage = event.get("stage", "")
            index = STAGE_TO_INDEX.get(stage)
            if index is not None:
                for previous in range(index):
                    if list(self.cards.values())[previous].state != DONE:
                        self._set_card_step(previous, DONE)
                self._set_card_step(index, RUNNING)
                self._update_counts()
            self.vars["status"].set(event.get("message", ""))
            self._log(f"{event.get('stage')}：{event.get('message', '')}")

        elif kind == "log":
            level = event.get("level", "info")
            text = str(event.get("message", ""))
            self._log(text, level)
            # 警告只挑一条摆到底部，不刷屏
            if level == "warn":
                self._set_notice(text, error=False)

        elif kind == "progress":
            percent = float(event.get("percent") or 0.0)
            # ⚠️ **编码阶段**的进度（帧已经渲完了、正在编 MOV / 拼接）。
            # 这一段原来**完全没有反馈**：进度条卡在 100%、界面一动不动，
            # 用户以为卡死，其实 ffmpeg 在干活。所以要单独显示。
            if event.get("encoding"):
                self.vars["percent"].set(f"{percent:.0f}%")
                self.progress.set(percent / 100.0)
                self.vars["status"].set(str(event.get("message") or "正在编码成片"))
                self._set_card_step(2, RUNNING, "编码中")
                self._log(str(event.get("message") or ""))
                return
            self.last_percent = percent
            self.vars["percent"].set(f"{percent:.2f}%")
            self.progress.set(percent / 100.0)
            eta = event.get("eta_seconds") or 0
            segment = event.get("segment") or 0
            per_frame = event.get("seconds_per_frame") or 0
            self.vars["status"].set(
                f"帧 {event.get('frame_done')}/{event.get('frame_total')}"
                f"　{per_frame:.2f} 秒/帧"
                f"　全片还剩 {self._duration(eta)}")
            self._set_card_step(1, RUNNING,
                                f"第 {segment + 1}/{event.get('segment_total')} 段")
            self._mark_batch_running(segment, event)

        elif kind == "segment":
            # 真实段范围到了 —— 这才是这批要渲多少张的唯一依据
            index = int(event["index"])
            start = int(event["frame_start"])
            end = int(event["frame_end"])
            total_segments = int(event["total"])
            first_plan = not self._planned_segments
            while len(self._planned_segments) < total_segments:
                self._planned_segments.append((0, 0))
            self._planned_segments[index] = (start, end)
            batch = getattr(self, "batch", None)
            if batch is not None and first_plan:
                # 只在第一次拿到段表时重建行（重建会清掉已完成状态）
                batch.set_plan(self._describe_batches())
            self._set_card_step(1, RUNNING, f"第 {index + 1}/{total_segments} 段")
            self._mark_batch_running(index, event)
            self._log(f"第 {event['index'] + 1}/{event['total']} 段："
                      f"帧 {event['frame_start']} ~ {event['frame_end']}")

        elif kind == "still_done":
            self._log(f"单帧预览已生成：{event.get('path')}")

        elif kind == "assemble_finished":
            self._on_assemble_finished(event)
        elif kind == "still_open":
            self._show_preview(Path(event["path"]))

        elif kind == "done":
            for index in range(len(STEPS)):
                self._set_card_step(index, DONE)
            self._update_counts()
            self._mark_all_batches_done()
            self.progress.set(1.0)
            self.vars["percent"].set("100.00%")
            self.pill.set("已完成", "#E3F5EC", T.OK)
            if hasattr(self, "helper"):
                self.helper.set_state("done", "出片啦，收工！")
            self.output_path = Path(event["output"])
            # ⚠️ 状态栏**必须把成片文件名说出来**。
            # 原来这里只写"739 帧 / 24.6 秒"，接着 ``run_finished`` 又把它
            # 改成"用时…渲了 N 段"—— 全程没出现过文件名，用户就以为
            # "没有输出结果"（其实文件在，只是没告诉他叫什么、在哪）。
            out = Path(event["output"])
            self.vars["status"].set(
                f"已完成：{out.name}　{event.get('frames')} 帧 / "
                f"{event.get('duration')} 秒")
            self._set_notice("")
            self._log(f"完成：{event['output']}")
            messagebox.showinfo(
                "渲染完成",
                f"成片已输出：\n{out}\n\n所在文件夹：\n{out.parent}")

        elif kind == "run_finished":
            result: RunResult = event["result"]
            if result.ok:
                self.output_path = result.output
                # 保留 ``done`` 里写好的文件名，只在后面补上耗时/段数
                name = result.output.name if result.output else "（未命名）"
                self.vars["status"].set(
                    f"已完成：{name}　用时 {self._duration(result.elapsed_seconds)}，"
                    f"渲了 {result.segments_rendered} 段，跳过 {result.segments_skipped} 段")
            else:
                self._fail()
                for item in result.errors:
                    self._log(item, "error")
                text = "\n\n".join(result.errors) or "未知错误"
                # 已经因为 error 事件弹过一次的，就别再弹第二个框（"要关两次"）
                if text.strip() != str(getattr(self, "_last_error", "")).strip():
                    messagebox.showerror("渲染失败", text)
            for warning in result.warnings:
                self._log(warning, "warn")

        elif kind == "error":
            message = str(event.get("message"))
            self._log(message, "error")
            self._fail()
            self._set_notice(message)
            # 同一个错误只弹一次：run_finished 里还会因为 result.errors 弹"渲染失败"。
            self._last_error = message
            messagebox.showerror("出错了", message)

        elif kind == "finished":
            self._set_busy(False)
            self.started_at = None

    def _on_assemble_finished(self, event: dict) -> None:
        """装配完成（或失败）：把工程复制到用户挑的位置，然后告诉他。

        ⚠️ 复制是**在装配成功之后才做** —— 装配失败时工作目录里那份是半成品，
           复制出去反而害人（打开一看缺东西，还以为我们给错了）。
        """
        self._set_busy(False)
        self.started_at = None
        target = str(event.get("target") or "")
        if not event.get("ok"):
            errors = [str(e) for e in (event.get("errors") or [])]
            message = errors[0] if errors else "装配没成功，看日志有详情"
            self._fail()
            self._set_notice(message)
            messagebox.showerror("组装没成功", message)
            return

        source = str(event.get("source") or "")
        frames = int(event.get("frames") or 0)
        if not source or not Path(source).is_file():
            self._fail()
            self._set_notice("装配完成但工程文件不见了")
            messagebox.showerror("找不到工程文件", f"装配完成，但没找到：\n{source}")
            return

        try:
            target_path = Path(target)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target_path)
        except OSError as exc:
            self._fail()
            self._set_notice(f"复制工程失败：{exc}")
            messagebox.showerror("复制失败", f"存不到这里：\n{target}\n\n{exc}")
            return

        size_mb = target_path.stat().st_size / 1024 / 1024
        self.pill.set("组装完成", T.OK_SOFT, T.OK)
        self.vars["status"].set(
            f"组装好了：{target_path.name}（{size_mb:.1f} MB）—— "
            f"可以直接在 Blender 里打开调")
        self._log(f"组装完成：{target_path}（{size_mb:.1f} MB，{frames} 帧）"
                  f"；源工程 {source}")
        messagebox.showinfo(
            "组装好了",
            f"工程存到这里了：\n{target_path}\n\n"
            f"里面是全套：角色、动作、表情、镜头、舞台（共 {frames} 帧）。\n"
            "用 Blender 打开就能自己调材质 / 灯光 / 相机。\n\n"
            "调完存盘，把这份 .blend 拖回「角色模型」卡，就能按你改的样子渲。")

    def _fail(self) -> None:
        """这一轮没通：**先解锁界面，再标失败**。

        ⚠️ 顺序不能反。``_set_busy(False)`` 会把 ``mode`` 复位，而它不再重画卡片；
        但万一以后有人给它加回重画，先标失败就会被擦掉。
        先解锁、后标记，这样"失败"一定看得见。

        ⚠️ 原来这里**压根没调 ``_set_busy(False)``**，而它是唯一复位 ``mode`` 的
        地方 —— 于是渲染一失败，素材就再也改不了（拖放、点卡片都被
        ``mode == "run"`` 挡住），只能重启程序。用户报过。
        """
        self._set_busy(False)
        self.started_at = None
        for index, card in enumerate(self.cards.values()):
            if card.state == RUNNING:
                self._set_card_step(index, FAILED)
        self.pill.set("失败", T.DANGER_SOFT, T.DANGER)
        self.vars["status"].set("失败 —— 改完素材可以重试，看日志有详情")
        if hasattr(self, "helper"):
            self.helper.set_state("fail", "这轮没通，底下有提示。")

    def _on_preview_resize(self, _event=None) -> None:
        """窗口拖动时别每一帧都重画渐变（1 像素一条，条数不少），拖动停 120ms 再画。"""
        if self._preview_resize_job is not None:
            try:
                self.root.after_cancel(self._preview_resize_job)
            except tk.TclError:
                pass
        self._schedule("_preview_resize_job", 120, self._draw_preview_background)

    def _draw_preview_background(self) -> None:
        """画预览区背景：1 像素一条的纵向渐变 + 抗锯齿的尾巴/音符水印。

        三条讲究，都是为了"别看着劣质"：

        1. 渐变**一条一像素**，之前 40 条铺满会看到 10px 一道的横条纹
        2. 飘带与音符用**预渲染的抗锯齿 PNG**（Tk 画曲线没有抗锯齿）
        3. 水印颜色贴近底色，让边缘的微弱台阶看不出来

        空态下这里还要画三样（全在 ``tags="hint"`` 里，一起删一起建）：

        * 正中那张 **「开始做一条片子」引导卡** —— 大片留白最忌讳只放一句话，
          那读起来就是"没做完"；有结构的卡才读成"设计留白"
        * 一张**极淡的 LOGO 水印**，把留白压实，又不抢将来的渲染图
        * 预览底边的**配置摘要条**（素材 2/4 · 分辨率 · 输出 · 预计体积）
        """
        self._preview_resize_job = None
        canvas = self.preview
        canvas.delete("bg")
        width = canvas.winfo_width() or 600
        height = canvas.winfo_height() or 300
        if width < 24 or height < 24:
            return

        T.draw_v_gradient(canvas, 0, 0, width, height,
                          T.PREVIEW_TOP, T.PREVIEW_BOTTOM, tags="bg")

        # 两侧水印：**狐狸尾巴**的弧线。
        # 取这个元素是因为设定里那句「尾巴划出一道弧线」—— 它天生就是一条 S 形大弧，
        # 比原来那条抽象飘带有指向性。
        if ui_assets.blit(canvas, "deco_tail_l.png", width * 0.06, height * 0.36,
                          tags="bg") is None:
            T.draw_ribbon(canvas, width * 0.10, height * 0.36, height * 0.72,
                          T.TAIL_SOFT, sign=-1, width=7, tags="bg")
        ui_assets.blit(canvas, "deco_tail_r.png", width * 0.86, height * 0.36,
                       tags="bg")

        # 音符水印（她是舞者，音符是"节奏"的意象；银铃在设定里就是节拍器）
        if ui_assets.blit(canvas, "deco_notes_soft.png", width * 0.28, height * 0.22,
                          tags="bg") is None:
            T.draw_note_cluster(canvas, width * 0.28, height * 0.22, 26,
                                T.PREVIEW_LINE, tags="bg")
        ui_assets.blit(canvas, "deco_notes_soft.png", width * 0.58, height * 0.72,
                       tags="bg")

        canvas.delete("hint")
        self._guide_button = None

        # ⚠️ **纵向几何必须自上而下算一遍**，不能各写各的常量：
        # 摘要条压住引导卡底部（"打开已有工程"被盖掉一截）、
        # 立绘的腿被摘要条啃掉 —— 都是"每块各自居中"造成的。
        # 这里只认两个常量：摘要条高度 + 它离下沿多远，其余全部推出来。
        #
        # ⚠️ 几何**空态用、有预览时也要用**（摘要条两种状态都在），
        # 所以放到分支外面算。
        band_h = T.S(34)
        band_y2 = height - T.S(8)
        band_y1 = band_y2 - band_h

        # 右下角的**正常比例**动作立绘（左上角助手卡才是 Q 版）。
        # 底边停在摘要条上方（内缩 10），腿脚不跟字打架。
        #
        # ⚠️ **吉祥物不能只画在空态里。** 它原来被套在
        # ``if self._preview_source is None:`` 里面，于是**一渲出单帧预览，
        # 右下角的立绘就没了**（用户报的"右面的吉祥物怎么丢了"）。
        # 现在挪到分支外面：空态和有预览时都画。
        mascot = ui_assets.mascot_normal() or ui_assets.mascot()
        self._mascot_column = 0
        if mascot is not None:
            self._mascot_column = self._draw_mascot(canvas, width, height,
                                                    band_y1, mascot)

        if self._preview_source is None:
            # ⚠️ **水印和引导卡不同时出现。** 水印画在 x≈30% 宽处、引导卡居中且很宽，
            # 两个一起画就会叠在一起 —— 引导卡左边露出水印那个白圆，
            # 看着像界面坏了（用户截图里就是这样）。
            # 空态真正要传达的信息都在引导卡上，水印在这儿只是噪音，所以空态不画。
            card_top = T.S(12)
            card_h = max(T.S(196), band_y1 - T.S(12) - card_top)
            self._draw_guide_card(canvas, width, top=card_top, card_h=card_h)
        else:
            # 有预览图时卡片不在，水印才画（极淡，压在预览图后面当底纹）
            self._draw_logo_watermark(canvas, width, height)

        # ⚠️ **摘要条最后画** —— 它要盖在立绘上面（顺序反了就是立绘压字）
        self._draw_summary_band(canvas, width, height, band_y1, band_y2)
        canvas.tag_raise("hint")
        canvas.tag_raise("photo")

    def _draw_mascot(self, canvas: tk.Canvas, width: int, height: int,
                     band_top: int, mascot: tk.PhotoImage) -> int:
        """右下角那尊**正常比例**的动作立绘。返回它用了多宽（画预览图时要避让）。

        高度取预览高的 3/4（设计图定死 252 对应 336），底边停在摘要条上方。
        ⚠️ 缩放只能用 ``subsample``（整数倍、最近邻）—— 480→40 会把圆边啃成锯齿，
        所以缩放档位要少动；这里只在明显超标时才缩，且**必须留引用**防 GC。

        ⚠️ **必须用独立的 tag ``"mascot"``，不能塞进 ``"hint"``。**
        ``_show_preview`` 里有一句 ``canvas.delete("hint")`` ——
        立绘如果挂在 ``"hint"`` 上，**一渲出单帧预览就被删掉了**
        （用户报的"右面的吉祥物怎么丢了"）。独立 tag 之后，
        预览的增删与立绘互不影响。
        """
        target = min(T.S(252), round((height - T.S(24)) * 0.75))
        shown = mascot
        factor = 1
        while factor < 8 and mascot.height() / (factor + 1) >= target:
            factor += 1
        if factor > 1:
            shown = mascot.subsample(factor, factor)
        self._preview_mascot = shown # 留引用，否则被 GC 掉变空白
        x = round(width - shown.width() / 2 - T.S(24))
        y = round(min(height - T.S(10), band_top - T.S(10)))
        canvas.delete("mascot")
        canvas.create_image(x, y, image=shown, anchor="s", tags="mascot")
        return shown.width() + T.S(36) # 含两侧留白，给预览图避让用

    def _draw_logo_watermark(self, canvas: tk.Canvas,
                             width: int, height: int) -> None:
        """正中一枚**极淡的 LOGO 水印**。

        ⚠️ Tk **做不了真半透明** —— 它没有 alpha 合成，``alpha`` 只对顶层窗口有效。
        所以"淡化"只能靠**预渲染一张已经调好透明度的图**。
        没有这张图时就**不画**：与其糊一个生硬的实心 LOGO 上去（比留白更丑），
        不如就留白。

        ⚠️ **生成器把这张图写成"跟 logo.png 一样的不透明图"过**，后果是
        预览区正中冒出一个扎眼的大白圆、还跟引导卡叠在一起。
        所以这里加一道闸：**看起来太实就不画**（``WATERMARK_MAX_ALPHA``）。
        判定用图片中部的采样 —— 我们那张极淡水印那里只有十几个 alpha。
        """
        logo = ui_assets.image("logo_watermark.png")
        if logo is None:
            return
        try:
            sample = logo.get(logo.width() // 2, logo.height() // 2)
            alpha = int(sample[3]) if len(sample) > 3 else 255
        except Exception: # noqa: BLE001 - 拿不到就别拦，照旧画
            alpha = 0
        if alpha > WATERMARK_MAX_ALPHA:
            self._log(f"水印图不透明度 {alpha} 偏高（应 ≤ "
                      f"{WATERMARK_MAX_ALPHA}），这次不画水印", "warn")
            return
        canvas.create_image(round(width * 0.30), round(height * 0.48),
                            image=logo, tags="hint")

    def _draw_summary_band(self, canvas: tk.Canvas, width: int, height: int,
                           y1: int, y2: int) -> None:
        """预览底边的**配置摘要条**：把"这次要出什么"一行说完。

        ``y1`` / ``y2`` 由调用方算好（它得先知道摘要条在哪，才能摆引导卡和立绘）。
        这一条**不是装饰** —— 它是用户每次抬头都要确认的东西。
        """
        x1, x2 = T.S(10), max(T.S(20), width - T.S(10))
        T.round_rect(canvas, x1, y1, x2, y2, T.S(8),
                     fill="#FFFFFF", outline=T.BORDER, width=1, tags="hint")

        items = self._summary_items()
        x = x1 + T.S(16)
        for text, strong in items:
            canvas.create_text(x, (y1 + y2) / 2 + T.S(1), text=text, anchor="w",
                               fill=T.MAPLE if strong else T.TEXT_SUB,
                               font=T.pick_font(9, bold=strong), tags="hint")
            x += T.measure_text(canvas, text, 9, strong) + T.S(24)
        canvas.create_text(x2 - T.S(16), (y1 + y2) / 2 + T.S(1),
                           text=f"预计 {self._compact_size()}", anchor="e",
                           fill=T.TEXT_SUB, font=T.pick_font(9), tags="hint")

    def _draw_guide_card(self, canvas: tk.Canvas, width: int, *,
                         top: int, card_h: int) -> None:
        """正中那张 **「开始做一条片子」引导卡**。

        ``top`` / ``card_h`` 都是**实际像素**（外面已经过 ``T.S()``）。
        卡里是**三步清单**：给"空白"一个结构，比一句提示有用得多。
        最后那个「打开已有工程…」是画布内按钮，矩形范围记在
        ``self._guide_button`` 里，点击判定在 :meth:`_on_preview_click`。
        """
        card_w = min(T.S(540), max(T.S(360), width - T.S(80)))
        left = round((width - card_w) / 2)
        top = round(top)
        bottom = top + card_h
        T.round_rect(canvas, left, top, left + card_w, bottom, T.S(10),
                     fill="#FFFFFF", outline=T.BORDER, width=1, tags="hint")

        x = left + T.S(30)
        # 卡片标题**说这工具是干什么的**，不是只喊一句"开始"。
        # 用户第一次打开，最先看到的应该是"这东西能帮我做什么"；
        # 具体怎么做交给下面那三步。
        #
        # ⚠️ 也别写"片子"。"做一条片子"是片场行话，用户直接问"片子…吗？" ——
        # 普通人说的是"视频"。用户可见的文案里一律用「视频」。
        canvas.create_text(x, top + T.S(30), text="把 MMD 模型渲成视频", anchor="w",
                           fill=T.TEXT, font=T.pick_font(13, bold=True),
                           tags="hint")
        canvas.create_text(x, top + T.S(50),
                           text="三步就行：素材拖到上面四个框里，再点「开始渲染」",
                           anchor="w", fill=T.TEXT_SUB, font=T.pick_font(9),
                           tags="hint")

        step_y = top + T.S(74)
        hint_x = left + card_w - T.S(30)
        # 右边那列只在**真能补充信息**时才写。原来第 2、3 步的右边也塞了字，
        # 结果第 2 步写"（镜头可以留空）"——那是另一个卡的事，硬凑上去的；
        # 第 3 步直接把标题重复了一遍。空着比说废话强。
        for num, title, hint in (
                ("1", "拖入角色模型", ".pmx / .pmd / .blend"),
                ("2", "拖入动作", ""),
                ("3", "点「开始渲染」", "先单帧预览确认构图")):
            badge_r = T.S(12)
            T.round_rect(canvas, x, step_y, x + badge_r * 2, step_y + badge_r * 2,
                         badge_r, fill=T.NAVY, outline="", tags="hint")
            canvas.create_text(x + badge_r, step_y + badge_r + T.S(1), text=num,
                               fill="#FFFFFF", font=T.pick_font(10, bold=True),
                               tags="hint")
            canvas.create_text(x + T.S(36), step_y + badge_r, text=title, anchor="w",
                               fill=T.TEXT, font=T.pick_font(10), tags="hint")
            if hint:
                canvas.create_text(hint_x, step_y + badge_r, text=hint, anchor="e",
                                   fill=T.TEXT_FAINT, font=T.pick_font(9),
                                   tags="hint")
            step_y += T.S(36)

        # 次要动作：打开已有工程。**画布内按钮**，范围留给点击判定。
        # ⚠️ 它**贴着卡片下沿**排（不是固定偏移）—— 卡片高度是算出来的，
        # 写死偏移就会在卡片变矮时把按钮顶出去。
        btn_w, btn_h = T.S(126), T.S(30)
        btn_x = left + T.S(30)
        btn_y = bottom - T.S(12) - btn_h
        self._guide_button = (btn_x, btn_y, btn_x + btn_w, btn_y + btn_h)
        T.round_rect(canvas, btn_x, btn_y, btn_x + btn_w, btn_y + btn_h, T.S(8),
                     fill="#FFFFFF", outline=T.BORDER, width=1, tags="hint")
        canvas.create_text((btn_x + btn_x + btn_w) / 2, (btn_y + btn_y + btn_h) / 2,
                           text="打开已有工程…", fill=T.NAVY, font=T.pick_font(9),
                           tags="hint")

    def _on_preview_click(self, event) -> None:
        """预览区点击：**先看是不是打在引导卡那个"打开已有工程"按钮上**。

        打进按钮 → 走打开工程；打在别处且已经显示了预览图 → 用系统看图程序
        打开原图（放大看细节的老行为，保留）。
        """
        button = self._guide_button
        if button is not None:
            x1, y1, x2, y2 = button
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self.on_open_project()
                return
        self._open_preview_original()

    # ------------------------------------------------------- 模型渲染那块
    def on_model_render(self) -> None:
        """打开「渲染模型」窗 —— 独立的一块区域（主界面只留这个入口）。

        渲染本身跑在**子进程**里（见 ``adapters/mmd_render.py``）：
        走子进程不传染 GPL，Blender 崩了也影响不到主程序。
        """
        if self.mode == "run":
            messagebox.showinfo("先等一下", "正在渲染整片，等它结束再渲模型。")
            return
        model_render_window.open_model_render(self)

    def show_preview(self, path, *, note: str = "") -> None:
        """让外部（模型渲染窗）也能把一张图摆进预览区。

        ``_show_preview`` 是单帧预览那条路用的内部方法；模型渲染窗
        **不该**去调那种带下划线的内部名，所以这里给一个明确的对外接口。
        """
        self._show_preview(Path(path))
        if note:
            self.vars["status"].set(note)

    def accept_model_render(self, path, outputs=None, source_pmx: str = "") -> None:
        """模型渲染用「继续」确认之后 —— **把结果放进「角色模型」卡**。

        需求原文：

        > 制作完毕后自动导入到流程内的模型区域里面

        放进卡里的是**那个可编辑的 .blend**（模型渲染时顺便存出来的），
        不是三视图 —— 角色卡接 ``.blend`` 走的是"已配好的工程"那条路，
        打开就能渲、也能自己在 Blender 里改。三视图只当预览留在出图目录。

        ⚠️ ``.blend`` 放进**角色卡**（不是舞台卡）：``_assign`` 那条规则是
           "拖到舞台 → 当环境；拖到别处 → 当整份已配好的工程"，
           而这里就是后者。所以直接写 ``vars["model"]``，
           **不能走 ``_assign``**（它会把它挪到 ``blend`` 字段，
           界面上第 1 张卡会显示成"已配好的工程"，跟用户的预期不一致）。

        ⚠️ 只在这里改界面：渲染过程中主界面一直保持原样，
           用户点「取消」就真的一点都没变。
        """
        path = Path(path)
        if not path.is_file():
            self._log(f"模型渲染的产物不见了：{path}", "warn")
            messagebox.showwarning(
                "文件不见了",
                f"这个文件现在找不到了：\n{path}\n\n可能被移走或删掉了。")
            return
        # ★ 让用户自己选：PMX（有物理）还是 .blend（保留观感）
        #
        # 现在两条路**都有物理**（渲染器保留原生刚体，装配层一律 build_rig + 烘焙），
        # 差别只剩材质观感，而观感有独立下拉。所以不再问"二选一"：
        # 优先用 PMX（物理最稳），同时把刚渲出来的 .blend 记为观感来源。
        # 「继续」放**渲好的那份 .blend**：
        # * 能力包 v2.0.1 保留 PMX 原生刚体 → 装配时 build_rig + 烘焙就有物理
        # * 材质已经是能力包观感 → 不用再迁移
        # * 下次打开能直接恢复它，用户不用重新渲一遍三视图
        # 源 PMX 只在两种情况下留着用：没有 .blend，或者用户直接拖了 PMX。
        pmx_candidate = Path(source_pmx) if source_pmx else None
        blend_candidate = Path(path) if Path(path).suffix.lower() == ".blend" else None
        # 🚨 **优先用源 PMX，不是那份 .blend。**
        #
        # 用户报的原话：「动作导进去出来动作消失了，场景导进去出来场景消失了」。
        # 查日志根因：这里原来把「渲染模型」存出的 .blend 塞进角色卡，
        # 而角色卡一旦是 .blend，整条流程就切到「整份工程」路线 ——
        # 那条路线不导入舞台、相机也不跟着动作走，于是场景和动作双双"消失"。
        # .blend 只作为**观感来源**（材质已经调好的那套），角色本体仍用 PMX。
        if pmx_candidate is not None and pmx_candidate.is_file():
            path = pmx_candidate
            look_blend = (blend_candidate
                          if blend_candidate is not None and blend_candidate.is_file()
                          else None)
        elif blend_candidate is not None and blend_candidate.is_file():
            # 拿不到源 PMX（比如用户直接拖了一个 .blend 来渲）才退回整份工程
            path = blend_candidate
            look_blend = blend_candidate
        else:
            look_blend = None
            path = Path(path)

        # ⚠️ 先复位卡片状态：上一步可能把卡片画成了"步骤卡"，
        # 不复位的话回填不上去（卡片还停在运行态）。
        self.reset_cards_to_drop()
        self.vars["model"].set(str(path))
        # 刚渲出来的工程里带着调好的材质观感 —— 记成观感来源，
        # 并把「观感」直接切到卡渲迁移（用户刚渲三视图，显然想要那套观感）。
        if look_blend is not None and look_blend.is_file():
            try:
                self.settings.last_look_blend = str(look_blend)
                self.settings.save()
            except Exception: # noqa: BLE001 - 记不住不影响本次
                pass
        # 这也是"用户确定要用这个角色"—— 记住它，下次打开默认还是它。
        self._remember_model(str(path))
        self._refresh_cards()
        count = len(outputs or [])
        if path.suffix.lower() == ".blend":
            extra = "（渲好的三视图在出图目录里，当预览看）" if count else ""
            self.vars["status"].set(
                f"已把装配好的工程放进「角色模型」：{path.name}{extra}")
        elif path.suffix.lower() == ".pmx" and look_blend is not None:
            self.vars["status"].set(
                f"角色模型用 PMX：{path.name}；观感从「{look_blend.name}」迁移")
            self._log(f"模型渲染结果（可编辑工程）已导入「角色模型」：{path}"
                      + (f"；同批预览图 {count} 张" if count else ""))
        else:
            extra = f"（另 {count - 1} 张单视图在同目录）" if count > 1 else ""
            self.vars["status"].set(
                f"已把渲染结果放进「角色模型」：{path.name}{extra}")
            self._log(f"模型渲染结果已导入「角色模型」：{path}"
                      + (f"；同批出图 {count} 张" if count else ""))
        # 顺手确认一下"素材齐了几样"那行跟着变
        self._refresh_panels()

    def _show_preview(self, path: Path) -> None:
        """把单帧预览显示在窗口里。

        ``tk.PhotoImage`` 直接支持 PNG（Tk 8.6），**不需要 Pillow**（NFR-16 禁 pip）。
        它只有整数倍缩小（``subsample``），所以算一个刚好放得下的档位。
        """
        if not path.is_file():
            self._log(f"预览图不存在：{path}", "warn")
            return
        try:
            source = tk.PhotoImage(file=str(path))
        except tk.TclError as exc:
            self._log(f"预览图加载失败：{exc}", "warn")
            return

        canvas = self.preview
        # ⚠️ **给右下角的立绘让出地方**，否则预览图会跟它重叠、
        # 而 ``tag_raise("photo")`` 又把预览图抬到立绘上面 ——
        # 表现就是"吉祥物不见了"（其实是被预览图盖住了）。
        # 立绘宽度由 ``_draw_preview_background`` 记在 ``_mascot_column``。
        reserve = int(getattr(self, "_mascot_column", 0) or 0)
        avail_w = max(T.S(200), canvas.winfo_width() - T.S(12) - reserve)
        box_w = avail_w
        box_h = max(T.S(160), canvas.winfo_height() - T.S(12))
        factor = max(1, -(-source.width() // box_w), -(-source.height() // box_h))
        shown = source.subsample(factor, factor) if factor > 1 else source

        self._preview_source_image = source # 留引用，否则图会被 GC 掉变空白
        self._preview_shown = shown
        self._preview_source = path
        canvas.delete("photo")
        # 水平位置：在"扣掉立绘之后的可用区"里居中（而不是整块画布居中）
        center_x = T.S(6) + avail_w / 2
        canvas.create_image(center_x, canvas.winfo_height() / 2,
                            image=shown, tags="photo")
        # ⚠️ **不要删 "mascot"** —— 立绘是独立 tag，跟预览的增删无关。
        # （原来这里 ``delete("hint")``，而立绘挂在 hint 上，于是预览一出
        # 吉祥物就没了。现在只清"hint"里跟空态有关的部分。）
        canvas.delete("hint")
        canvas.tag_raise("photo")
        self._log(f"单帧预览已显示（原图 {source.width()}×{source.height()}，"
                  f"缩小 {factor} 倍）：{path}")

    def _open_preview_original(self) -> None:
        """点预览图 = 用系统看图程序打开原图，方便放大看细节。"""
        if self._preview_source is None or not self._preview_source.is_file():
            return
        try:
            os.startfile(str(self._preview_source)) # noqa: S606
        except (AttributeError, OSError) as exc:
            self._log(f"打不开原图：{exc}", "warn")


def main() -> int:
    # 可选英文语言包：装了 locales/en.json 就翻译界面，没装保持中文
    try:
        from komaflow import i18n
        i18n.install()
    except Exception:      # noqa: BLE001  语言包坏了也不能挡启动
        pass
    root = tk.Tk()
    app = KomaFlowApp(root)
    # 首次运行：检查外部依赖（缺 FFmpeg 会问一次并自动下载）
    root.after(600, app._first_run_setup)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
