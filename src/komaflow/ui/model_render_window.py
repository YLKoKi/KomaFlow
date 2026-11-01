# -*- coding: utf-8 -*-
"""「渲染模型」—— 独立的一块区域：把 ``.pmx`` 渲成三视图，再接进流程。

需求原文
--------
> 把工作区里的模型渲染能力装进去，让用户不再需要自己打开 Blender 再导入，
> 记得做独立区域，然后制作完毕后自动导入到流程内的模型区域里面

所以这里是一个 **独立窗口**（不挤主界面那四张卡），流程是：

::

    选模型 → 选视角（2K 三视图） → 开始渲染
        → 出图，**先让用户在预览里看** → 继续 / 取消
            继续 → 写进「角色模型」卡，回主界面就能直接渲

三个设计上的取舍，别改回去
--------------------------
1. **不做成主界面里的面板。** 主界面侧栏只剩 400 多像素、已经在滚动了；
   而这块要放模型路径、四个视角、取景、分辨率、两个关键字……
   做成独立窗口用系统标准窗（跟设置窗同一套路），想看的才点开。
2. **渲完不直接动主界面。** 用户可能只是"渲出来看看"，也可能渲错了模型。
   所以先在预览区让他看，再给「继续 / 取消」——
   这两步是需求里点名的（"先在预览里面让用户看下，然后给个继续或者取消选项"）。
3. **产物落在输出目录的 ``模型预览`` 子目录**，不散在工作目录里。

⚠️ 渲染是在**子进程**里跑 Blender 的（``adapters/mmd_render.py``）：
   走子进程不传染 GPL，而且 Blender 崩了也影响不到主程序。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

from ..adapters import mmd_render
from . import theme as T
from . import tip as tip_module
from .widgets import ThinProgress

#: 2K 三视图 —— 需求里点名的默认档，打开就能直接渲
DEFAULT_RES = "2048x2048"
DEFAULT_VIEWS = ("front", "side", "back")

#: 分辨率档位。**三视图要正方形的** —— 三张竖条拼起来才不至于又高又瘦。
RES_CHOICES = ("2048x2048", "1536x1536", "1200x1920", "1024x1024")
FRAME_CHOICES = (("全身", "full"), ("半身", "half"), ("脸部", "face"))

#: 薄片不描边、取景忽略的默认关键字。
#: 踩坑总表 C6：零厚度薄片做反向壳描边会画成**实心黑块**；
#: 巨大翅膀又会把人物在取景里压成一个小点。默认填"翼"是用户模型的常见情况，
#: 用户自己的模型关键字不一样就自己改。
DEFAULT_THIN = "翼"

WINDOW_W = 780
WINDOW_H = 620
#: 进度条的实际像素宽（卡里扣掉左右内边距之后的可用宽）
PROGRESS_W = 716

CHECK_BOX = "☑"
CHECK_EMPTY = "☐"


class ModelRenderWindow:
    """模型渲染窗（同一时间只开一个）。"""

    _instance: "ModelRenderWindow | None" = None

    def __init__(self, app) -> None:
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title("渲染模型")
        self.window.configure(bg=T.CREAM)
        self.window.transient(app.root)
        self.window.resizable(True, True)
        # ⚠️ 允许调整大小是**安全阀**：屏幕小的时候就算把高度夹小了，
        # 用户也能自己拉大。原来写死 False，夹了就没退路。
        self.window.minsize(T.S(560), T.S(420))

        #: 「模型文件」和「出图目录」都**记得上次的**（用户要求）。
        #:
        #: 用户原话：
        # > 这个模型文件位置也保留成最近一次用户的位置
        #:
        #: 图目录一并记：这两个是同一个对话框里的"位置"，
        #: 只记一半会让用户每次都得重选出图目录。
        last_pmx, last_out = self._remembered_paths()
        self.pmx = tk.StringVar(value=last_pmx)
        self.outdir = tk.StringVar(value=last_out or self._default_outdir())
        self.res = tk.StringVar(value=DEFAULT_RES)
        self.ortho = tk.BooleanVar(value=True)
        self.frame = tk.StringVar(value="full")
        self.thin = tk.StringVar(value=DEFAULT_THIN)
        self.views: dict[str, tk.BooleanVar] = {
            view: tk.BooleanVar(value=view in DEFAULT_VIEWS) for view in mmd_render.VIEWS}

        #: 渲染线程 → 主线程的事件队列（Tk 只能在主线程碰）
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel = threading.Event()
        #: 窗口是否已被用户关掉（worker 可能还在收尾，此时不释放单例）
        self._closed = False
        self.outputs: list[str] = []
        self.sheet = ""
        #: 这次顺便存出来的**可编辑角色 .blend**（存到了才是非空）
        self.blend = ""
        self._blend_target = ""
        self.done = False
        self._job: str | None = None

        self._build()
        self._center()
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda _e: self.close())
        self._poll()

    # ------------------------------------------------------------ 位置 / 默认值
    def _remembered_paths(self) -> tuple[str, str]:
        """读上次用过的 (模型文件, 出图目录)。

        ⚠️ 模型文件**可能已经不在了**（被移走/删掉）。这时不能填一个不存在的
           路径 —— 用户一按「开始渲染」就报错，还看不出为什么。返回空串，
           让他重新选（出图目录不存在倒是无所谓，渲染时会自动建，
           所以目录只判空不判存在）。
        """
        settings = getattr(self.app, "settings", None)
        if settings is None:
            return "", ""
        pmx = str(getattr(settings, "last_render_model", "") or "").strip()
        outdir = str(getattr(settings, "last_render_outdir", "") or "").strip()
        if pmx and not Path(pmx).is_file():
            pmx = ""
        return pmx, outdir

    def _remember_paths(self) -> None:
        """把当前的模型文件 / 出图目录记进设置（下次打开自动填回来）。

        什么时候记：**真的把这一轮渲出去**（``start()``）或用户重新选了文件。
        只是打开窗、看一眼又关掉，不该改记忆。
        """
        settings = getattr(self.app, "settings", None)
        if settings is None:
            return
        try:
            pmx = self.pmx.get().strip()
            outdir = self.outdir.get().strip()
            if pmx:
                settings.last_render_model = pmx
            if outdir:
                settings.last_render_outdir = outdir
            settings.save()
        except Exception as exc: # noqa: BLE001 - 记不住也不该挡住渲染
            log = getattr(self.app, "_log", None)
            if log:
                log(f"记住渲染模型的位置失败（不影响使用）：{exc}", "warn")

    def _default_outdir(self) -> str:
        """默认出图目录：**输出目录**下的 ``模型预览`` 子目录。

        用输出目录而不是工作目录：这是用户要看的图，
        跟成片放一起他才找得到（工作目录是中间产物，我们自己也写了"很占地方"）。
        """
        base = ""
        try:
            base = (self.app.vars["output_dir"].get()
                    or self.app.vars["work_dir"].get())
        except (AttributeError, KeyError, tk.TclError):
            base = ""
        base = base or str(Path.cwd())
        return str(Path(base) / "模型预览")

    def _center(self) -> None:
        """摆窗口，并**确保它整个落在屏幕里**。

        ⚠️ 这里必须夹一次高度。踩过的坑：进度区详情原来是自由换行的 Label，
           渲完写三行就把窗口撑到 1113 高，而用户屏幕只有 1026 ——
           **底部整排按钮（含「继续」）被切到屏幕外**，
           用户看到的就是"渲好了，但继续的选项呢"。
           窗口高度现在虽然已经锁住（详情只显两行），但这道夹子要留着：
           用户换成 1280×720 的小屏时，光靠"锁两行"也未必装得下。
        """
        self.window.update_idletasks()
        root = self.app.root
        screen_w = self.window.winfo_screenwidth()
        screen_h = self.window.winfo_screenheight()
        want_w = self.window.winfo_reqwidth()
        want_h = self.window.winfo_reqheight()
        # 留一点边距，免得贴着任务栏
        height = min(want_h, screen_h - T.S(80))
        width = min(want_w, screen_w - T.S(40))
        self.window.geometry(f"{width}x{height}")
        self.window.update_idletasks()

        x = root.winfo_rootx() + (root.winfo_width() - width) // 2
        y = root.winfo_rooty() + T.S(60)
        # 夹回屏幕内（想停在上三分之一，但不许越界）
        x = max(0, min(x, screen_w - width))
        y = max(0, min(y, screen_h - height - T.S(40)))
        self.window.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------ 构建界面
    def _build(self) -> None:
        body = tk.Frame(self.window, bg=T.CREAM)
        body.pack(fill="both", expand=True, padx=T.S(16), pady=T.S(14))

        self._build_header(body)
        self._build_source(body)
        self._build_options(body)
        self._build_advanced(body)
        self._build_progress(body)
        self._build_footer(body)

    def _card(self, parent: tk.Frame, title: str, accent: str) -> tk.Frame:
        """一张卡（小标题 + 内容框），跟设置窗口一个长相。"""
        card = tk.Frame(parent, bg=T.CARD, highlightthickness=1,
                        highlightbackground=T.BORDER)
        card.pack(fill="x", pady=(0, T.S(10)))
        head = tk.Frame(card, bg=T.CARD)
        head.pack(fill="x", padx=T.S(12), pady=(T.S(9), T.S(5)))
        bar = tk.Canvas(head, width=T.S(3), height=T.S(13), bg=T.CARD,
                        highlightthickness=0)
        bar.create_rectangle(0, 0, T.S(3), T.S(13), fill=accent, outline="")
        bar.pack(side="left")
        tk.Label(head, text=title, bg=T.CARD, fg=T.TEXT,
                 font=T.pick_font(11, bold=True)).pack(side="left", padx=T.S(7))
        box = tk.Frame(card, bg=T.CARD)
        box.pack(fill="x", padx=T.S(12), pady=(0, T.S(11)))
        return box

    def _entry_row(self, parent: tk.Frame, label: str, var: tk.StringVar,
                   command, tip_text: str, *, width: int = 12) -> tk.Entry:
        row = tk.Frame(parent, bg=T.CARD)
        row.pack(fill="x", pady=T.S(3))
        caption = tk.Label(row, text=label, bg=T.CARD, fg=T.TEXT_SUB,
                           font=T.pick_font(9), width=width, anchor="w")
        caption.pack(side="left")
        entry = tk.Entry(row, textvariable=var, relief="flat", bg="#FFFFFF",
                         fg=T.TEXT, font=T.pick_font(9), highlightthickness=1,
                         highlightbackground=T.BORDER, highlightcolor=T.NAVY,
                         insertbackground=T.NAVY)
        entry.pack(side="left", fill="x", expand=True, ipady=T.S(3))
        T.FlatButton(row, "浏览…", command, width=T.S(58), height=T.S(26),
                     bg=T.CARD, fill="#FFFFFF", hover=T.BEIGE_SOFT, fg=T.NAVY,
                     outline=T.BORDER, font=T.pick_font(9)).pack(
            side="left", padx=(T.S(6), 0))
        tip_module.attach(caption, tip_text)
        tip_module.attach(entry, tip_text)
        return entry

    def _build_header(self, body: tk.Frame) -> None:
        head = tk.Frame(body, bg=T.CREAM)
        head.pack(fill="x", pady=(0, T.S(10)))
        tk.Label(head, text="喂它一个模型，先渲出三视图看看",
                 bg=T.CREAM, fg=T.TEXT,
                 font=T.pick_font(12, bold=True)).pack(side="left")
        tk.Label(head, text="渲完你可以先看，满意了再放进「角色模型」",
                 bg=T.CREAM, fg=T.TEXT_SUB,
                 font=T.pick_font(9)).pack(side="left", padx=T.S(10))

    def _build_source(self, body: tk.Frame) -> None:
        box = self._card(body, "模型与出图位置", T.NAVY)
        self._entry_row(box, "模型文件", self.pmx, self._browse_pmx,
                        "要渲的 MMD 模型（.pmx / .pmd）。\n"
                        "模型只读，不会被拷进本工具，也不会改动你的原文件。",
                        width=10)
        self._entry_row(box, "出图目录", self.outdir, self._browse_outdir,
                        "渲出来的图放哪儿。默认放在输出目录下的「模型预览」里，\n"
                        "跟成片挨着，找起来方便。", width=10)

    def _build_options(self, body: tk.Frame) -> None:
        box = self._card(body, "渲哪些 · 怎么渲", T.TEAL)

        # ── 视角：四个格子 ──
        views_row = tk.Frame(box, bg=T.CARD)
        views_row.pack(fill="x", pady=(T.S(2), T.S(7)))
        tk.Label(views_row, text="视角", bg=T.CARD, fg=T.TEXT_SUB,
                 font=T.pick_font(9), width=10, anchor="w").pack(side="left")
        self.view_buttons: dict[str, tk.Checkbutton] = {}
        for view in mmd_render.VIEWS:
            button = tk.Checkbutton(
                views_row, text=mmd_render.VIEW_LABELS[view],
                variable=self.views[view], bg=T.CARD, fg=T.TEXT,
                activebackground=T.CARD, selectcolor="#FFFFFF",
                font=T.pick_font(9), highlightthickness=0, bd=0,
                command=self._refresh)
            button.pack(side="left", padx=(0, T.S(12)))
            self.view_buttons[view] = button
        tip_module.attach(views_row, "要哪几个视角。三个（正面 / 侧面 / 背面）就是常见的三视图；\n"
                                    "只想要一张就只勾一个。")

        # ── 投影：正交 / 透视 ──
        proj_row = tk.Frame(box, bg=T.CARD)
        proj_row.pack(fill="x", pady=T.S(3))
        tk.Label(proj_row, text="投影", bg=T.CARD, fg=T.TEXT_SUB,
                 font=T.pick_font(9), width=10, anchor="w").pack(side="left")
        proj = tk.Checkbutton(
            proj_row, text="正交（设定图那种平视感）", variable=self.ortho,
            bg=T.CARD, fg=T.TEXT, activebackground=T.CARD, selectcolor="#FFFFFF",
            font=T.pick_font(9), highlightthickness=0, bd=0)
        proj.pack(side="left")
        tip_module.attach(proj, "勾上 = 正交投影，比例真实、适合当设定图看；\n"
                                "不勾 = 透视，近大远小，接近照片的感觉。")

        # ── 分辨率 ──
        res_row = tk.Frame(box, bg=T.CARD)
        res_row.pack(fill="x", pady=(T.S(6), T.S(3)))
        tk.Label(res_row, text="画幅", bg=T.CARD, fg=T.TEXT_SUB,
                 font=T.pick_font(9), width=10, anchor="w").pack(side="left")
        tk.Label(res_row, text="出图尺寸", bg=T.CARD, fg=T.TEXT_FAINT,
                 font=T.pick_font(9)).pack(side="left", padx=(0, T.S(6)))
        self.res_box = tk.Entry(res_row, textvariable=self.res, width=12,
                                relief="flat", bg="#FFFFFF", fg=T.TEXT,
                                font=T.pick_font(9), highlightthickness=1,
                                highlightbackground=T.BORDER,
                                highlightcolor=T.NAVY, insertbackground=T.NAVY)
        self.res_box.pack(side="left", ipady=T.S(3))
        for value in RES_CHOICES:
            T.FlatButton(res_row, value, lambda v=value: self.res.set(v),
                         width=T.S(84), height=T.S(24), bg=T.CARD, fill="#FFFFFF",
                         hover=T.BEIGE_SOFT, fg=T.NAVY, outline=T.BORDER,
                         font=T.pick_font(8)).pack(side="left", padx=(T.S(5), 0))
        tip_module.attach(self.res_box,
                          "宽x高，例如 2048x2048。\n"
                          "三视图建议用正方形：三张竖条拼起来才不会又高又瘦。")

        # ── 取景 ──
        frame_row = tk.Frame(box, bg=T.CARD)
        frame_row.pack(fill="x", pady=(T.S(6), 0))
        tk.Label(frame_row, text="取景", bg=T.CARD, fg=T.TEXT_SUB,
                 font=T.pick_font(9), width=10, anchor="w").pack(side="left")
        for label, value in FRAME_CHOICES:
            radio = tk.Radiobutton(
                frame_row, text=label, value=value, variable=self.frame,
                bg=T.CARD, fg=T.TEXT, activebackground=T.CARD,
                selectcolor="#FFFFFF", font=T.pick_font(9),
                highlightthickness=0, bd=0)
            radio.pack(side="left", padx=(0, T.S(12)))
        tip_module.attach(frame_row, "全身 = 从脚拍到头顶；半身、脸部是特写。\n"
                                     "特写时正面那张文件名会变（按取景命名）。")

    def _build_advanced(self, body: tk.Frame) -> None:
        box = self._card(body, "薄片与巨大配件", T.MAPLE)
        # ⚠️ 这里**不给「浏览…」按钮**。第一版顺手挂了一个（复用了那套行布局），
        # 可它点开是选 .pmx 文件 —— 而这一格要填的是"材质名的一部分"，
        # 两个字根本不是一个东西。按钮在那儿只会让人以为要选文件。
        row = tk.Frame(box, bg=T.CARD)
        row.pack(fill="x", pady=T.S(3))
        caption = tk.Label(row, text="薄片关键字", bg=T.CARD, fg=T.TEXT_SUB,
                           font=T.pick_font(9), width=10, anchor="w")
        caption.pack(side="left")
        entry = tk.Entry(row, textvariable=self.thin, relief="flat",
                         bg="#FFFFFF", fg=T.TEXT, font=T.pick_font(9),
                         highlightthickness=1, highlightbackground=T.BORDER,
                         highlightcolor=T.NAVY, insertbackground=T.NAVY)
        entry.pack(side="left", fill="x", expand=True, ipady=T.S(3))
        thin_tip = ("填**材质名**里的一段字，命中就不给这些部件做描边。\n"
                    "零厚度的薄片（翅膀、飘带）做反向壳描边会变成实心黑块。\n"
                    "多个关键字用英文逗号隔开；留空 = 全都做描边。")
        tip_module.attach(caption, thin_tip)
        tip_module.attach(entry, thin_tip)
        tk.Label(box, text="同一个关键字也会让取景忽略这些部件 —— "
                           "巨大翅膀会把人物压成一个小点。",
                 bg=T.CARD, fg=T.TEXT_FAINT, font=T.pick_font(9),
                 anchor="w", justify="left").pack(fill="x")

    def _build_progress(self, body: tk.Frame) -> None:
        box = self._card(body, "进度", T.ORANGE)
        self.stage = tk.Label(box, text="还没开始", bg=T.CARD, fg=T.TEXT_SUB,
                              font=T.pick_font(9), anchor="w")
        self.stage.pack(fill="x")
        self.bar = ThinProgress(box, bg=T.CARD, width=T.S(PROGRESS_W), height=T.S(10))
        self.bar.pack(fill="x", pady=(T.S(7), T.S(7)))
        # ⚠️ **详情锁死两行**（``height=2``）。
        #
        # 它原来是个会自动换行的 Label：渲完一次写三行（预览清单 / 可编辑工程 /
        # 三视图），窗口就跟着长高 —— 长到超过用户屏幕（他那边只有 1026 高），
        # 结果**底部整排按钮（含「继续」）被切到屏幕外**，
        # 用户看到的是"渲好了但没有继续的选项"。
        # 这是实测抓到的：窗口底边 1205 > 屏幕 1026。
        #
        # 锁两行之后窗口高度是**常量**，底部按钮永远在。
        # 超出的部分是"文件名清单"这种次要信息，鼠标停上去看全文。
        self.detail = tk.Label(box, text="", bg=T.CARD, fg=T.TEXT_FAINT,
                               font=T.pick_font(9), anchor="w", justify="left",
                               height=2, wraplength=T.S(PROGRESS_W))
        self.detail.pack(fill="x")

    def _build_footer(self, body: tk.Frame) -> None:
        foot = tk.Frame(body, bg=T.CREAM)
        foot.pack(fill="x", pady=(T.S(2), 0))
        self.btn_render = T.FlatButton(foot, "开始渲染", self.start,
                                       width=T.S(104), height=T.S(32),
                                       bg=T.CREAM, fill=T.NAVY,
                                       hover=T.NAVY_SOFT,
                                       font=T.pick_font(9, bold=True))
        self.btn_render.pack(side="left")
        # 「继续 / 取消」一开始**不出现** —— 没东西可确认时摆两个灰按钮是噪音
        self.btn_ok = T.FlatButton(foot, "继续（放进角色模型）", self._accept,
                                   width=T.S(180), height=T.S(32), bg=T.CREAM,
                                   fill=T.NAVY, hover=T.NAVY_SOFT,
                                   font=T.pick_font(9, bold=True))
        self.btn_cancel = T.FlatButton(foot, "取消", self._reject,
                                       width=T.S(74), height=T.S(32),
                                       bg=T.CREAM, fill="#FFFFFF",
                                       hover=T.BEIGE_SOFT, fg=T.NAVY,
                                       outline=T.BORDER, font=T.pick_font(9))
        T.FlatButton(foot, "关闭", self.close, width=T.S(74), height=T.S(32),
                     bg=T.CREAM, fill="#FFFFFF", hover=T.BEIGE_SOFT, fg=T.NAVY,
                     outline=T.BORDER, font=T.pick_font(9)).pack(side="right")
        self._refresh()

    # ------------------------------------------------------------ 选择
    def _browse_pmx(self) -> None:
        # 「浏览」的起点用上次的目录，省得每次从头翻
        current = self.pmx.get().strip()
        initial = (str(Path(current).parent)
                   if current and Path(current).parent.is_dir() else None)
        chosen = filedialog.askopenfilename(
            title="选一个 MMD 模型", parent=self.window, initialdir=initial,
            filetypes=[("MMD 模型", "*.pmx *.pmd"), ("所有文件", "*.*")])
        if chosen:
            self.pmx.set(chosen)
            # 选完就记住 —— 用户明确指定了"要用这个"
            self._remember_paths()
            self._refresh()

    def _browse_outdir(self) -> None:
        current = self.outdir.get().strip()
        initial = current if current and Path(current).is_dir() else None
        chosen = filedialog.askdirectory(title="出图放哪儿", initialdir=initial,
                                         parent=self.window)
        if chosen:
            self.outdir.set(chosen)
            self._remember_paths()

    # ------------------------------------------------------------ 状态刷新
    def _picked_views(self) -> list[str]:
        return [v for v in mmd_render.VIEWS if self.views[v].get()]

    def _refresh(self) -> None:
        """按当前选择更新按钮可用性与那行"将要渲成什么"。"""
        if self.worker is not None and self.worker.is_alive():
            self.btn_render.set_enabled(False)
            return
        self.btn_render.set_enabled(bool(self.pmx.get().strip())
                                    and bool(self._picked_views()))
        if self.done:
            return
        if not self.pmx.get().strip():
            self.stage.configure(text="先选一个模型文件", fg=T.TEXT_SUB)
            return
        views = "、".join(mmd_render.VIEW_LABELS[v] for v in self._picked_views())
        frame = dict((v, k) for k, v in FRAME_CHOICES).get(self.frame.get(), "全身")
        shape = "正交" if self.ortho.get() else "透视"
        self.stage.configure(
            text=f"将渲出：{views or '（一个视角都没勾）'} · {frame} · {shape} · "
                 f"{self.res.get()}", fg=T.TEXT_SUB)

    # ------------------------------------------------------------ 渲染
    def start(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        pmx = self.pmx.get().strip()
        if not pmx or not Path(pmx).is_file():
            self._fail("模型文件不存在。用「浏览…」重新选一个。")
            return
        views = self._picked_views()
        if not views:
            self._fail("一个视角都没勾，渲不出东西。至少勾一个。")
            return
        exe = ""
        try:
            exe = self.app.vars["blender"].get().strip()
        except (AttributeError, KeyError, tk.TclError):
            exe = ""
        ok, why = mmd_render.available()
        if not ok:
            self._fail(why)
            return
        if not exe or not Path(exe).is_file():
            self._fail("还没选定用哪台 Blender。先去【设置】里选一台，再回来渲。")
            return

        # 这一轮真的要渲了 —— 把模型文件 + 出图目录记下来（下次打开自动填回）
        self._remember_paths()

        self.cancel.clear()
        self.done = False
        self.outputs = []
        self.sheet = ""
        self._hide_confirm()
        self.bar.set(0.0)
        self._show_detail("")
        self.btn_render.set_text("正在渲染…")
        self.btn_render.set_enabled(False)
        self.stage.configure(text="正在起 Blender…", fg=T.TEXT)

        outdir = self.outdir.get().strip() or self._default_outdir()
        #: 顺便存一份**可编辑的角色 .blend**（渲染器会把模型单独归一个集合、
        #: 相机/灯放进「_预览用（可删）」，材质带驱动器，能直接 append 进别的场景）。
        #: 用户点了「继续」之后进角色卡的就是**它**，不是那张三视图。
        blend_target = str(Path(outdir) / f"{Path(pmx).stem}.blend")
        self._blend_target = blend_target
        options = {
            "shots": tuple(views),
            "res": self.res.get().strip() or DEFAULT_RES,
            "ortho": bool(self.ortho.get()),
            "frame": self.frame.get(),
            "edge_skip": self.thin.get().strip() or None,
            "frame_skip": self.thin.get().strip() or None,
            "save_blend": blend_target,
            "blend_keep_camera": False, # 纯素材：不要我们的预览相机与世界
            "blender": exe,
        }
        name = Path(pmx).stem
        self.app._log(f"── 渲染模型：{pmx} → {outdir}（{len(views)} 个视角）")

        def on_progress(data: dict) -> None:
            self.events.put(("progress", data))

        def job() -> None:
            """**渲染 + 拼图都在这条工作线程里做完**，主线程只负责更新界面。

            🚨 这条是血泪教训：拼图原来是在主线程的 ``_on_finished`` 里调的，
               而 ``stitch_views`` 会**再起一个 Blender 子进程**（十几秒到一分钟）。
               主线程一冻，Windows 过几秒就判 **Application Hang** 并关掉整个进程 ——
               实测事件日志里连着三条 ``Application Hang python.exe``，
               时间点跟每次"渲完就该出预览"完全对上。
               **凡是会起子进程 / 读大文件 / sleep 的活，一律不许在主线程干。**
            """
            try:
                result = mmd_render.render_model(
                    pmx, outdir, name, on_progress=on_progress,
                    cancel=self.cancel, **options)
            except mmd_render.MMDError as exc:
                self.events.put(("error", f"{exc}||{exc.log}"))
                self._release_instance()
                return
            except Exception as exc: # noqa: BLE001
                self.events.put(("error", f"{type(exc).__name__}: {exc}||"))
                self._release_instance()
                return

            # 关窗后渲染线程可能还在跑（取消要一点时间）：不要再拼图/回填。
            if self.cancel.is_set():
                self._release_instance()
                return

            # 拼三视图（同样起 Blender 子进程，所以必须留在本线程里）
            sheet_path = str(Path(outdir) / f"{result.get('name') or '模型'}_三视图.png")
            try:
                exe_now = self.app.vars["blender"].get().strip()
            except (AttributeError, KeyError, tk.TclError, RuntimeError):
                exe_now = ""
            stitched = {"ok": False, "path": "", "message": "（没拼）"}
            if len(result.get("outputs") or []) > 1:
                try:
                    stitched = mmd_render.stitch_views(
                        result["outputs"], sheet_path,
                        prefix=str(result.get("name") or ""), blender=exe_now,
                        cancel=self.cancel)
                except Exception as exc: # noqa: BLE001
                    stitched = {"ok": False, "path": "",
                                "message": f"{type(exc).__name__}: {exc}"}
            result["sheet"] = stitched.get("path") if stitched.get("ok") else ""
            result["stitch_message"] = str(stitched.get("message") or "")
            self.events.put(("finished", result))
            self._release_instance()

        self.worker = threading.Thread(target=job, daemon=True,
                                       name="KomaFlow-model-render")
        self.worker.start()
        self._refresh()

    def _poll(self) -> None:
        """把渲染线程的事件搬回主线程（Tk 只能在主线程碰）。"""
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    self._on_progress(payload)
                elif kind == "error":
                    message, log = payload.split("||", 1)
                    self._fail(message, log=log)
                elif kind == "finished":
                    self._on_finished(payload)
        except queue.Empty:
            pass
        try:
            self._job = self.window.after(120, self._poll)
        except tk.TclError:
            self._job = None

    def _on_progress(self, data: dict) -> None:
        stage = str(data.get("stage") or "")
        label = mmd_render.STAGE_LABELS.get(stage, stage or "处理中")
        self.stage.configure(
            text=f"{label} —— {data.get('message') or ''}".strip(" ——"),
            fg=T.TEXT)
        try:
            self.bar.set(float(data.get("progress") or 0.0))
        except (TypeError, ValueError):
            pass

    def _show_detail(self, text: str, *, fg: str | None = None) -> None:
        """更新进度区那两行详情；**全文进悬停提示**（Label 只显两行）。

        ⚠️ 为什么要小心：这块文字会随渲染结果变长（三行清单里全是长文件名），
           而窗口高度**不能跟着长** —— 长了就会把底部按钮顶出屏幕
           （实测：用户的屏 1026 高，窗口底边跑到 1205，「继续」整个看不见）。
           所以 Label 锁两行、超出的走提示。
        """
        self.detail.configure(text=text, fg=fg or T.TEXT_FAINT)
        try:
            self._detail_tip.text = text
        except AttributeError:
            # 第一次调用时才挂提示（tip.attach 返回一个 .text 可改的对象）
            self._detail_tip = tip_module.attach(self.detail, text)

    def _on_finished(self, result: dict) -> None:
        """渲染线程已经把图和三视图都备好了 —— 这里**只更新界面**。

        ⚠️ 这个函数在主线程跑，**不许起子进程、不许读大文件**。
           拼图原本在这里做，直接把窗口冻到被 Windows 判为无响应（见 ``job`` 的说明）。
        """
        self.outputs = list(result.get("outputs") or [])
        self.worker = None
        self.btn_render.set_text("重新渲一遍")
        self.btn_render.set_enabled(True)
        if not self.outputs:
            self._fail("渲完了但没找到出图文件。看看日志里有没有报错。")
            return

        # 三视图是**工作线程**拼好的，这里只取结果
        self.sheet = str(result.get("sheet") or "")
        if not self.sheet and result.get("stitch_message"):
            self.app._log(f"拼三视图没成功（不影响单张）："
                          f"{result.get('stitch_message')}", "warn")

        self.done = True
        names = "、".join(Path(p).name for p in self.outputs)
        stats = result.get("stats") or {}
        # ⚠️ 这次渲染的**主交付是那个 .blend**（可编辑的模型资产），
        # 三视图只是"先给你看一眼"的预览。渲染器存盘失败时不会让整轮失败，
        # 所以这里据实说：存到了就说存到了，没存到就只交付图。
        self.blend = str(result.get("blend") or "")
        detail = f"出了 {len(self.outputs)} 张预览：{names}"
        if stats:
            detail += "　材质 " + str(stats.get("材质", "?")) + " 个"
        if self.blend:
            detail += f"\n可编辑工程：{Path(self.blend).name}"
            detail += "（点「继续」放进角色卡的就是它）"
        else:
            detail += "\n（这次没存出 .blend —— 点「继续」只会放进三视图）"
        if self.sheet:
            detail += f"\n三视图：{Path(self.sheet).name}"
        self._show_detail(detail, fg=T.TEXT_SUB)
        self.stage.configure(text="渲好了 —— 在预览里看看，满意就点「继续」",
                             fg=T.OK)
        self.bar.set(1.0)
        self.app._log(
            f"模型渲染完成，{len(self.outputs)} 张：{', '.join(self.outputs)}"
            + (f"；可编辑工程：{self.blend}" if self.blend else "；没存出 .blend"))

        # ── 先给用户看（需求点名了这一步）──
        preferred = Path(self.sheet) if self.sheet else Path(self.outputs[0])
        self.app.show_preview(preferred, note=f"模型渲染：{preferred.name}")
        self._show_confirm()

    def _show_confirm(self) -> None:
        self.btn_ok.pack(side="left", padx=(T.S(8), 0))
        self.btn_cancel.pack(side="left", padx=(T.S(6), 0))
        self.btn_ok.set_enabled(True)

    def _hide_confirm(self) -> None:
        self.btn_ok.pack_forget()
        self.btn_cancel.pack_forget()

    def _fail(self, message: str, log: str = "") -> None:
        self.worker = None
        self.done = False
        self.btn_render.set_text("开始渲染")
        self.btn_render.set_enabled(bool(self.pmx.get().strip()))
        self.bar.set(0.0)
        self.stage.configure(text="没渲成：" + message, fg=T.MAPLE)
        if log:
            self._show_detail(log.strip()[-400:])
            self.app._log("模型渲染失败：" + message + "\n" + log.strip()[-1500:],
                          "error")
        else:
            self.app._log("模型渲染失败：" + message, "error")

    # ------------------------------------------------------------ 继续 / 取消
    def _accept(self) -> None:
        """用户点了「继续」：**这一步才动主界面** —— 把结果放进角色模型卡。

        ⚠️ 放进卡里的是**那个可编辑的 .blend**（如果这次存出来了），
        不是三视图 —— 角色卡接 .blend 就是"已配好的工程"那条路，
        直接就能渲；三视图只当"先给你看一眼"的预览留在出图目录里。
        """
        if not self.outputs and not self.blend:
            self._reject()
            return
        target = self.blend or self.sheet or self.outputs[0]
        # ⚠️ 把**源 PMX** 也交出去。
        #
        # 用户指出：用户提交的是 PMX，但「继续」之后角色卡里放的是 .blend，
        # 整片于是走成"自带工程"路线 —— 那条路**没有人物物理**。
        # 现在有源 PMX 就以 PMX 为准（走组装路线，物理才有效），
        # .blend 只当"可编辑工程"另存，给他自己拿去 Blender 里改。
        self.app.accept_model_render(
            target, self.outputs, source_pmx=self.pmx.get().strip())
        self.done = False
        self._hide_confirm()
        self.close()

    def _reject(self) -> None:
        """用户点了「取消」：什么都不改，只是不进流程。"""
        self.app._log("模型渲染结果未采用（用户点了取消），主界面没有改动")
        self.app.vars["status"].set("这次模型渲染没有采用，主界面没变")
        self.done = False
        self._hide_confirm()

    # ------------------------------------------------------------ 关
    def _release_instance(self) -> None:
        """worker 真正结束时才释放单例（关窗时它可能还在终止子进程）。"""
        if ModelRenderWindow._instance is self:
            ModelRenderWindow._instance = None

    def close(self) -> None:
        self._closed = True
        if self.worker is not None and self.worker.is_alive():
            # 只发取消信号；**不要**在这里释放单例，也不要置空 worker ——
            # worker 还要 terminate/wait 子进程（R17）。立刻放行会让用户
            # 再开一个窗口，两个进程同时写同名 PNG / .blend。
            self.cancel.set()
        else:
            ModelRenderWindow._instance = None
        if self._job is not None:
            try:
                self.window.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        try:
            self.window.destroy()
        except tk.TclError:
            pass


def open_model_render(app) -> ModelRenderWindow | None:
    """打开「渲染模型」窗；已经开着就抬到前面（不重复开）。"""
    existing = ModelRenderWindow._instance
    if existing is not None:
        try:
            if existing.window.winfo_exists():
                existing.window.deiconify()
                existing.window.lift()
                existing.window.focus_force()
                return existing
        except tk.TclError:
            pass
        # 上一个窗口已关，但渲染线程还在终止子进程/收尾：先别开新的，
        # 否则两个进程会同时写同名 PNG / .blend（审查 R17）。
        worker = getattr(existing, "worker", None)
        if worker is not None and worker.is_alive():
            try:
                app._log("上一次「渲染模型」还在收尾，等它结束再开新窗口。", "warn")
            except Exception: # noqa: BLE001
                pass
            return None
        ModelRenderWindow._instance = None

    window = ModelRenderWindow(app)
    ModelRenderWindow._instance = window
    return window
