"""设置窗口：三类保存位置 + 渲染快照开关 + 界面缩放。

做成**独立窗口**而不是主界面里的一页：设置项只会越加越多，塞进侧栏会把
主界面撑爆；而且设置改完即走，不需要常驻在界面上。

窗口用**系统标准窗口**（有边框、可拖大小），跟前几天把主窗口改成原生窗口
是同一个理由 —— 该交给系统的就不要自己写。
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog

from . import env_picker
from . import theme as T
from .widgets import Select

#: 每条路径设置的说明。写成表，加一条设置只要加一行。
PATH_ROWS: tuple[tuple[str, str, str], ...] = (
    ("output_dir", "成片保存到",
     "渲染出来的 MOV / MP4 放这里。\n体积最大的是它，建议指到大一点的盘。"),
    ("project_dir", "工程文件保存到",
     "「保存工程」时的默认位置。工程文件很小（几十 KB），\n"
     "存哪都行，主要是方便下次找回来。"),
    ("snapshot_dir", "渲染快照保存到",
     "每次点「开始渲染」，都会把**这一次装配好的东西**存一份到这里，\n"
     "带时间戳。想事后自己进 Blender 改，就从这里拿。"),
    ("work_dir", "中间产物保存到",
     "分段渲染的中间文件（透明模式下是 PNG 序列、还有 work.blend）。\n"
     "**这个最占地方**：2K 三分钟可能几十 GB，建议指到空闲的盘。"),
)


class SettingsWindow:
    """设置窗口。同一时间只开一个（已经开着就抬到前面）。"""

    _instance: "SettingsWindow | None" = None

    def __init__(self, parent: tk.Misc, settings, on_apply=None, *,
                 on_rediscover=None, on_blender_pick=None,
                 on_blender_manual=None, on_ffmpeg_download=None) -> None:
        self.parent = parent
        self.settings = settings
        self.on_apply = on_apply
        # 这三个回调都指向主程序：探测结果归它管，环境选择的结果也由它应用
        self.on_rediscover = on_rediscover or (lambda: None)
        self.on_blender_pick = on_blender_pick or (lambda probed, i: None)
        self.on_blender_manual = on_blender_manual or (lambda: None)
        # FFmpeg 下载回调：主程序里有现成的后台下载流程（带日志与就绪提示），
        # 设置窗口只负责提供入口，不自己实现下载。
        self.on_ffmpeg_download = on_ffmpeg_download or (lambda: None)

        self.window = tk.Toplevel(parent)
        self.window.title("设置")
        self.window.configure(bg=T.CREAM)
        self.window.transient(parent) # 跟着主窗口最小化
        self.window.resizable(True, True)
        self.window.minsize(T.S(560), T.S(430))

        self.vars: dict[str, tk.StringVar] = {
            key: tk.StringVar(value=str(getattr(settings, key) or ""))
            for key, _label, _tip in PATH_ROWS
        }
        self.snapshot_on = tk.BooleanVar(value=bool(settings.snapshot_enabled))
        self.zoom_var = tk.StringVar(value=f"{int(float(settings.ui_zoom) * 100)}%")

        self._build()
        self._center()
        # 关窗时清掉单例标记，不然关掉之后再点齿轮就打不开了
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Destroy>", self._on_destroy)

    # ------------------------------------------------------------- 构建
    def _build(self) -> None:
        from . import tip as tip_module

        body = tk.Frame(self.window, bg=T.CREAM)
        body.pack(fill="both", expand=True, padx=T.S(16), pady=T.S(14))

        card = tk.Frame(body, bg=T.CARD, highlightthickness=1,
                        highlightbackground=T.BORDER)
        card.pack(fill="x")

        head = tk.Frame(card, bg=T.CARD)
        head.pack(fill="x", padx=T.S(12), pady=(T.S(10), T.S(6)))
        bar = tk.Canvas(head, width=T.S(3), height=T.S(13), bg=T.CARD,
                        highlightthickness=0)
        bar.create_rectangle(0, 0, T.S(3), T.S(13), fill=T.TEAL, outline="")
        bar.pack(side="left")
        tk.Label(head, text="保存位置", bg=T.CARD, fg=T.TEXT,
                 font=T.pick_font(11, bold=True)).pack(side="left", padx=T.S(7))

        box = tk.Frame(card, bg=T.CARD)
        box.pack(fill="x", padx=T.S(12), pady=(0, T.S(12)))

        for key, label, text in PATH_ROWS:
            row = tk.Frame(box, bg=T.CARD)
            row.pack(fill="x", pady=T.S(3))
            caption = tk.Label(row, text=label, bg=T.CARD, fg=T.TEXT_SUB,
                               font=T.pick_font(9), width=12, anchor="w")
            caption.pack(side="left")
            entry = tk.Entry(row, textvariable=self.vars[key], relief="flat",
                             bg="#FFFFFF", fg=T.TEXT, font=T.pick_font(9),
                             highlightthickness=1, highlightbackground=T.BORDER,
                             highlightcolor=T.NAVY, insertbackground=T.NAVY)
            entry.pack(side="left", fill="x", expand=True, ipady=T.S(3))
            T.FlatButton(row, "浏览…", lambda k=key: self._browse(k),
                         width=T.S(58), height=T.S(26), bg=T.CARD, fill="#FFFFFF",
                         hover=T.BEIGE_SOFT, fg=T.NAVY, outline=T.BORDER,
                         font=T.pick_font(9)).pack(side="left", padx=(T.S(6), 0))
            tip_module.attach(caption, text)
            tip_module.attach(entry, text)

        # ── 其它 ──
        other = tk.Frame(body, bg=T.CARD, highlightthickness=1,
                         highlightbackground=T.BORDER)
        other.pack(fill="x", pady=(T.S(12), 0))
        head2 = tk.Frame(other, bg=T.CARD)
        head2.pack(fill="x", padx=T.S(12), pady=(T.S(10), T.S(6)))
        bar2 = tk.Canvas(head2, width=T.S(3), height=T.S(13), bg=T.CARD,
                         highlightthickness=0)
        bar2.create_rectangle(0, 0, T.S(3), T.S(13), fill=T.MAPLE, outline="")
        bar2.pack(side="left")
        tk.Label(head2, text="渲染快照", bg=T.CARD, fg=T.TEXT,
                 font=T.pick_font(11, bold=True)).pack(side="left", padx=T.S(7))

        box2 = tk.Frame(other, bg=T.CARD)
        box2.pack(fill="x", padx=T.S(12), pady=(0, T.S(12)))

        # 用 tk.Checkbutton 是**故意的**：这一处要的是"对/错"这种系统语义，
        # 自绘开关还得自己管键盘与焦点，得不偿失。
        snap = tk.Checkbutton(
            box2, text="每次开始渲染前，存一份装配好的快照", variable=self.snapshot_on,
            bg=T.CARD, fg=T.TEXT, activebackground=T.CARD,
            selectcolor="#FFFFFF", font=T.pick_font(9), anchor="w",
            highlightthickness=0, bd=0)
        snap.pack(fill="x", anchor="w")
        tip_module.attach(
            snap, "存的是**这一次真正喂给 Blender 的那份东西**：\n"
                  "装配好的场景（.blend）＋ 这次用的工程参数（.kfproj）。\n"
                  "想事后自己进 Blender 调，就从快照目录拿；\n"
                  "关掉它，盘里就不会多出这些文件。")

        row3 = tk.Frame(box2, bg=T.CARD)
        row3.pack(fill="x", pady=(T.S(8), 0))
        zoom_label = tk.Label(row3, text="界面缩放", bg=T.CARD, fg=T.TEXT_SUB,
                              font=T.pick_font(9), anchor="w")
        zoom_label.pack(side="left")
        Select(row3, self.zoom_var,
               [f"{int(z * 100)}%" for z in T.ZOOM_LEVELS], width=T.S(80),
               height=T.S(28), bg=T.CARD, font_size=9).pack(side="left", padx=T.S(10))
        tip_module.attach(zoom_label,
                          "整个界面放大缩小。跟系统的显示缩放是**叠加**的，\n"
                          "所以两个都调就会过大。")

        # ── 渲染环境（从主界面搬过来的）──
        # ⚠️ 这块原来长在主界面侧栏里。搬走的理由："用哪台 Blender"是设置项，
        # 跟"成片存哪儿"一类，不是每次渲片都要看的东西。
        # 而且设置窗口更宽，下拉里能显示完整路径，比挤在 412px 的侧栏里强。
        env_card = tk.Frame(body, bg=T.CARD, highlightthickness=1,
                            highlightbackground=T.BORDER)
        env_card.pack(fill="x", pady=(T.S(12), 0))
        env_head = tk.Frame(env_card, bg=T.CARD)
        env_head.pack(fill="x", padx=T.S(12), pady=(T.S(10), T.S(6)))
        env_bar = tk.Canvas(env_head, width=T.S(3), height=T.S(13), bg=T.CARD,
                            highlightthickness=0)
        env_bar.create_rectangle(0, 0, T.S(3), T.S(13), fill=T.TEAL, outline="")
        env_bar.pack(side="left")
        tk.Label(env_head, text="渲染环境", bg=T.CARD, fg=T.TEXT,
                 font=T.pick_font(11, bold=True)).pack(side="left", padx=T.S(7))
        tk.Label(env_head, text="用哪台 Blender 来渲", bg=T.CARD,
                 fg=T.TEXT_FAINT, font=T.pick_font(9)).pack(side="left")

        env_box = tk.Frame(env_card, bg=T.CARD)
        env_box.pack(fill="x", padx=T.S(12), pady=(0, T.S(12)))
        self.env_picker = env_picker.EnvPicker(
            env_box, on_rediscover=self.on_rediscover,
            on_pick=self.on_blender_pick, on_manual=self.on_blender_manual,
            wraplength=520)
        self.env_picker.frame.pack(fill="x")

        # ── FFmpeg（渲染环境卡片的下半段）──
        # 首启弹窗承诺过"去设置里手动下载"，这里就是那个入口：
        # 缺 FFmpeg 时给按钮，点了走主程序的后台下载（不卡界面）。
        from ..media.ffmpeg import find_ffmpeg

        ff_row = tk.Frame(env_card, bg=T.CARD)
        ff_row.pack(fill="x", padx=T.S(12), pady=(0, T.S(12)))
        try:
            find_ffmpeg()
            ff_state, ff_ready = "FFmpeg：已就绪", True
        except Exception:      # noqa: BLE001 - 找不到才需要入口
            ff_state, ff_ready = "FFmpeg：未找到 —— 编码透明 MOV / 拼接分段需要它", False
        ff_label = tk.Label(ff_row, text=ff_state, bg=T.CARD,
                            fg=T.TEXT_SUB if ff_ready else T.TEXT,
                            font=T.pick_font(9), anchor="w")
        ff_label.pack(side="left")
        tip_module.attach(
            ff_label,
            "FFmpeg 不随本软件分发。\n"
            "渲染 MP4/MOV 的编码与分段拼接都要用它；\n"
            "只输出 PNG 图片序列则不需要。")
        if not ff_ready:
            T.FlatButton(ff_row, "下载 FFmpeg", self.on_ffmpeg_download,
                         width=T.S(96), height=T.S(28), bg=T.CARD, fill="#FFFFFF",
                         hover=T.BEIGE_SOFT, fg=T.NAVY, outline=T.BORDER,
                         font=T.pick_font(9)).pack(side="left", padx=(T.S(10), 0))

        # ── 底部按钮 ──
        foot = tk.Frame(body, bg=T.CREAM)
        foot.pack(fill="x", pady=(T.S(14), 0))
        T.FlatButton(foot, "恢复默认位置", self._reset, width=T.S(110),
                     height=T.S(30), bg=T.CREAM, fill="#FFFFFF", hover=T.BEIGE_SOFT,
                     fg=T.NAVY, outline=T.BORDER,
                     font=T.pick_font(9)).pack(side="left")
        T.FlatButton(foot, "取消", self.close, width=T.S(74), height=T.S(30),
                     bg=T.CREAM, fill="#FFFFFF", hover=T.BEIGE_SOFT, fg=T.NAVY,
                     outline=T.BORDER, font=T.pick_font(9)).pack(side="right")
        T.FlatButton(foot, "保存", self.apply, width=T.S(84), height=T.S(30),
                     bg=T.CREAM, fill=T.NAVY, hover=T.NAVY_SOFT,
                     font=T.pick_font(9, bold=True)).pack(side="right", padx=T.S(8))

        self.hint = tk.Label(body, text="", bg=T.CREAM, fg=T.TEXT_FAINT,
                             font=T.pick_font(9), anchor="w", justify="left")
        self.hint.pack(fill="x", pady=(T.S(10), 0))
        self._refresh_hint()

    def _refresh_hint(self) -> None:
        """底部那句提示：把设置文件位置告诉用户，方便他自己去备份/手改。"""
        self.hint.configure(text=f"设置存在：{self.settings.path()}")

    def _center(self) -> None:
        self.window.update_idletasks()
        parent = self.parent.winfo_toplevel()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.window.winfo_width()) // 2
        y = parent.winfo_rooty() + T.S(80)
        self.window.geometry(f"+{max(0, x)}+{max(0, y)}")

    # ------------------------------------------------------------- 交互
    def _browse(self, key: str) -> None:
        current = self.vars[key].get().strip()
        initial = current if current and Path(current).is_dir() else None
        chosen = filedialog.askdirectory(title="选择目录", initialdir=initial,
                                         parent=self.window)
        if chosen:
            self.vars[key].set(chosen)

    def _reset(self) -> None:
        from .settings import default_paths

        for key, value in default_paths().items():
            if key in self.vars:
                self.vars[key].set(value)

    def apply(self) -> None:
        """把界面上的值写回设置并落盘，然后通知调用方。"""
        for key, _label, _tip in PATH_ROWS:
            setattr(self.settings, key, self.vars[key].get().strip())
        self.settings.snapshot_enabled = bool(self.snapshot_on.get())
        try:
            self.settings.ui_zoom = int(str(self.zoom_var.get()).rstrip("%")) / 100.0
        except ValueError:
            self.settings.ui_zoom = 1.0
        self.settings.save()
        if self.on_apply is not None:
            self.on_apply(self.settings)
        self.close()

    def close(self) -> None:
        SettingsWindow._instance = None
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def _on_destroy(self, event) -> None:
        if event.widget is self.window:
            SettingsWindow._instance = None


def open_settings(parent: tk.Misc, settings, on_apply=None, *,
                  on_rediscover=None, on_blender_pick=None,
                  on_blender_manual=None, on_ffmpeg_download=None) -> SettingsWindow:
    """打开设置窗口；已经开着就把它抬到前面，不重复开。

    返回值**总是那个窗口对象**（不管是新开的还是抬起来的），
    调用方需要拿它里面的选择器去同步状态。
    """
    existing = SettingsWindow._instance
    if existing is not None:
        try:
            if existing.window.winfo_exists():
                existing.window.deiconify()
                existing.window.lift()
                existing.window.focus_force()
                return existing
        except tk.TclError:
            pass
        SettingsWindow._instance = None

    window = SettingsWindow(parent, settings, on_apply=on_apply,
                            on_rediscover=on_rediscover,
                            on_blender_pick=on_blender_pick,
                            on_blender_manual=on_blender_manual,
                            on_ffmpeg_download=on_ffmpeg_download)
    SettingsWindow._instance = window
    return window
