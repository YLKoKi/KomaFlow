"""提示窗的**共用外壳** —— 柔和卡片风 + 探头吉祥物。

为什么单独抽出来
----------------
两个窗（缺插件 / 准备就绪）除了文案和吉祥物**长得一模一样**：
暖米底、圆角、柔影、竖排胶囊按钮、右侧探头立绘。

主程序里踩过"同一件事各写一套"的坑（``_browse_into`` 与拖放两套归类
导致 .blend 被当模型文件），所以这里**只写一份**，两个窗都用它。

子类只要提供：
* ``title_text`` 标题
* ``blocks`` 若干段 (小标题, 正文)
* ``mascot`` 吉祥物资产名
* ``buttons`` 按钮 [(文字, 回调, 是否主按钮)]
"""

from __future__ import annotations

import tkinter as tk

from . import theme as T
from . import tip as tip_module

#: 窗宽（**实际像素**，不跟缩放走）
#: ⚠️ 立绘 PNG 是定死的实际像素宽，会盖住窗口右侧那一片。
#: 若窗宽跟着缩放变，1.5× 时就成了"文字胀大、立绘不变"，
#: 两者必然重叠（实测文字右缘 910 撞上立绘左缘 680）。
#: 所以这里按固定实际像素排，S() 只用在按钮内边距这种小地方。
#:
#: 980 → 1090 的原因：那张探头立绘（``mascot_peek``）**可见内容宽 559px**，
#: 980 时留给文字列只剩 358px，标题「缺一个插件：mmd_tools」被折成
#: 「mmd_too / ls」—— 断在词中间很难看。加宽到 1090 就一行放得下。
WIDTH_PX = 1090
#: 文字列左侧内边距（实际像素）
PAD_PX = 30
#: 立绘距右边 / 底边留多少（实际像素）
MASCOT_MARGIN = 16


def _bbox_height(canvas: tk.Canvas, item: int) -> int:
    """一个 canvas item 实际占的高度。"""
    box = canvas.bbox(item)
    return (box[3] - box[1]) if box else 0


def mascot_width(name: str) -> int:
    """某个吉祥物资产的**可见内容**左边界与宽度 → ``(left, width)``。

    为什么要读像素
    --------------
    立绘 PNG 的**图幅**框不出它实际画到哪 —— 探头那张左边有一片透明，
    可抬手和头发会伸到图幅左缘附近。按图幅倒推文字列宽，
    视觉上就会"手压到字上"（实测立绘 bbox 左缘 392、文字排到 400）。

    "留多少余量"这种事不能靠猜 —— 为它来回超了三次。所以直接**读 alpha
    通道**找出可见内容的左右边界，按可见边界排版。

    只用标准库（``zlib`` 解压 + PNG 的 filter 反算），不引 Pillow：
    运行时不带任何第三方依赖（NFR-16）。
    """
    from . import assets as ui_assets

    if name not in _BOUNDS_CACHE:
        img = ui_assets.image(name)
        _BOUNDS_CACHE[name] = (
            _content_bounds(str(ui_assets.ASSETS / name)) if img is not None
            else None
        )
    bounds = _BOUNDS_CACHE[name]
    if bounds and bounds[2] > bounds[0]:
        return bounds[0], bounds[2] - bounds[0]
    # 拿不到就退回图幅宽（保守：把整幅都算成内容）
    img = ui_assets.image(name)
    return 0, int(img.width()) if img is not None else 300


def _content_bounds(path: str) -> tuple[int, int, int, int] | None:
    """读 PNG 的 alpha，返回非透明内容的 ``(left, top, right, bottom)``。

    PNG 是逐行 filter 后再 zlib 压的，所以得按行反算 filter 才能拿到
    真实像素值。只做「有 alpha 的 8 位图」这一种情况（我们的立绘都是）。
    """
    import struct
    import zlib

    try:
        with open(path, "rb") as handle:
            blob = handle.read()
        if blob[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        pos = 8
        width = height = bit_depth = color_type = 0
        idat = bytearray()
        while pos + 8 <= len(blob):
            length, kind = struct.unpack(">I4s", blob[pos:pos + 8])
            body = blob[pos + 8:pos + 8 + length]
            if kind == b"IHDR":
                width, height, bit_depth, color_type = struct.unpack(
                    ">IIBB", body[:10])
            elif kind == b"IDAT":
                idat += body
            elif kind == b"IEND":
                break
            pos += 12 + length
        if not width or bit_depth != 8 or color_type not in (4, 6):
            return None

        channels = 4 if color_type == 6 else 2
        stride = width * channels
        raw = zlib.decompress(bytes(idat))

        left, top, right, bottom = width, height, 0, 0
        previous = bytearray(stride)
        row = bytearray(stride)
        offset = 0
        for y in range(height):
            filter_type = raw[offset]
            offset += 1
            row[:] = raw[offset:offset + stride]
            offset += stride
            _unfilter(filter_type, row, previous, channels)
            alpha_index = 3 if color_type == 6 else 1
            for x in range(width):
                if row[x * channels + alpha_index] > 8:
                    if x < left:
                        left = x
                    if x > right:
                        right = x
            if any(row[x * channels + alpha_index] > 8 for x in range(0, width, 4)):
                if y < top:
                    top = y
                bottom = y
            previous[:] = row
        if right < left or bottom < top:
            return None
        return left, top, right, bottom
    except Exception: # noqa: BLE001 - 排版用的辅助信息，读不到就退回图幅
        return None


def _unfilter(kind: int, row: bytearray, previous: bytearray,
              bpp: int) -> None:
    """把 PNG 的行 filter 反算回原始像素（就地改 ``row``）。"""
    if kind == 0:
        return
    if kind == 1:
        for i in range(bpp, len(row)):
            row[i] = (row[i] + row[i - bpp]) & 0xFF
    elif kind == 2:
        for i in range(len(row)):
            row[i] = (row[i] + previous[i]) & 0xFF
    elif kind == 3:
        for i in range(len(row)):
            base = (row[i - bpp] >> 1) if i >= bpp else 0
            row[i] = (row[i] + base + (previous[i] >> 1)) & 0xFF
    elif kind == 4:
        for i in range(len(row)):
            a = row[i - bpp] if i >= bpp else 0
            b = previous[i]
            c = previous[i - bpp] if i >= bpp else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            predictor = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            row[i] = (row[i] + predictor) & 0xFF


#: 每个资产的可见内容边界缓存（读一次就够）
_BOUNDS_CACHE: dict[str, tuple[int, int, int, int] | None] = {}


class NoticeWindow:
    """柔和提示窗的基类。子类覆写 :meth:`title_text` / :meth:`blocks` 等。"""

    #: 吉祥物资产名（子类覆写）
    mascot: str = "mascot_peek.png"

    def __init__(self, parent: tk.Misc, *, on_close=None) -> None:
        self.parent = parent
        self.on_close = on_close
        self.window: tk.Toplevel | None = None
        self._mascot_shown: tk.PhotoImage | None = None # 留引用防 GC

    # ------------------------------------------------------------ 子类接口
    @property
    def title_text(self) -> str:
        raise NotImplementedError

    @property
    def blocks(self) -> list[tuple[str, str]]:
        raise NotImplementedError

    @property
    def buttons(self) -> list[tuple[str, object, bool]]:
        raise NotImplementedError

    @property
    def footnote(self) -> str:
        return ""

    # ------------------------------------------------------------ 开 / 关
    def open(self) -> None:
        if self.window is not None and self.window.winfo_exists():
            self.window.lift()
            self.window.focus_force()
            return

        win = tk.Toplevel(self.parent)
        self.window = win
        win.title("提示")
        win.configure(bg=T.CREAM)
        win.resizable(False, False)
        win.transient(self.parent.winfo_toplevel())
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Escape>", lambda _e: self.close())

        body = tk.Canvas(win, bg=T.CREAM, highlightthickness=0, bd=0,
                         width=WIDTH_PX, height=1)
        body.pack(fill="both", expand=True)

        height = self._draw(body)
        body.configure(height=height)
        win.geometry(f"{WIDTH_PX}x{height}")
        self._center(win)
        win.lift()
        win.focus_force()

    def close(self) -> None:
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
            self.window = None
        if self.on_close is not None:
            self.on_close()

    # ------------------------------------------------------------ 画
    def _draw(self, canvas: tk.Canvas) -> int:
        pad = PAD_PX
        # 文字列宽度**从立绘可见内容的左缘倒推**（见 :func:`mascot_width`）。
        # 不是从图幅倒推 —— 图幅左边有透明区，按它算会让手压到字上。
        content_left, content_w = mascot_width(self.mascot)
        frame_left = WIDTH_PX - T.S(MASCOT_MARGIN) - content_w
        mascot_left = frame_left + content_left
        text_w = max(T.S(200), mascot_left - pad - T.S(22))

        y = T.S(26)

        # ⚠️ **标题也要折行。** 它不折的话会超出文字列、压到立绘上
        # （实测标题右缘 400，而立绘可见左缘 396 —— 重叠 4px；
        # 正文都在 368 以内，就它冒头）。
        title_item = canvas.create_text(
            pad, y, text=tip_module.wrap_text(self.title_text, text_w, size=14),
            anchor="nw", fill=T.NAVY, font=T.pick_font(14, bold=True))
        y += _bbox_height(canvas, title_item) + T.S(12)

        for head, text in self.blocks:
            y = self._paragraph(canvas, pad, y, text_w, head, text)
            y += T.S(14)

        btn_y = y + T.S(6)
        for label, command, primary in self.buttons:
            self._button(canvas, pad, btn_y, label, command, primary=primary)
            btn_y += T.S(46)
        y = btn_y

        if self.footnote:
            # ⚠️ 小字**也要折行** —— 漏了会因为不折而一路铺到立绘上
            item = canvas.create_text(
                pad, y, text=tip_module.wrap_text(self.footnote, text_w, size=9),
                anchor="nw", fill=T.TEXT_FAINT, font=T.pick_font(9))
            y += _bbox_height(canvas, item) + T.S(18)
        else:
            y += T.S(10)

        canvas.create_line(T.S(20), y - T.S(10), WIDTH_PX - T.S(20), y - T.S(10),
                           fill=T.BEIGE_LINE, width=1)

        height = max(y, T.S(300))
        # 边框条**先画**（在立绘下面）—— 立绘会盖住它一部分，
        # 读起来就是"她扒在弹窗这条边上、身体在边外面"。
        # 顺序反了（立绘先画）就变成"她站在边前面"，透视就错了。
        self._draw_edge_bar(canvas, height)
        self._draw_mascot(canvas, height)
        return height

    def _draw_edge_bar(self, canvas: tk.Canvas, height: int) -> None:
        """弹窗最右缘的一条竖边 —— 给"扒着"当抓手。

        ⚠️ **默认不画**。探头那张立绘里**已经画好了她抓的那条边**，
        再补一条会变成两条边、反而看不懂。
        这个方法留着，是给"立绘里没有边"的素材用的（``edge_bar = True`` 才画）。
        """
        if not getattr(self, "edge_bar", False):
            return
        from . import assets as ui_assets

        # 边条高度：跟立绘的实际高度对齐（她扒到哪，边就画到哪）
        art = ui_assets.image(self.mascot)
        art_h = art.height() if art is not None else T.S(300)
        top = max(T.S(10), height - art_h - T.S(20))
        canvas.create_rectangle(
            WIDTH_PX - T.S(6), top, WIDTH_PX, height,
            fill=T.BEIGE_LINE, outline=T.BEIGE, width=0)

    def _paragraph(self, canvas: tk.Canvas, x: int, y: int, width: int,
                   head: str, text: str) -> int:
        canvas.create_text(x, y, text=head, anchor="nw",
                           fill=T.MAPLE, font=T.pick_font(9, bold=True))
        y += T.S(20)
        # ⚠️ 中文折行只能自己来（Tk 的 wraplength 按空格断词，对中文无效），
        # 而且**折完之后不要再给 create_text 传 width** —— 那会把每个
        # 逻辑行又当成长单词，反而撑得比给定宽度更宽。
        wrapped = tip_module.wrap_text(text, width, size=10)
        item = canvas.create_text(x, y, text=wrapped, anchor="nw",
                                  fill=T.TEXT, font=T.pick_font(10))
        return y + _bbox_height(canvas, item)

    def _button(self, canvas: tk.Canvas, x: int, y: int, text: str,
                command, *, primary: bool = False) -> None:
        font = T.pick_font(10, bold=primary)
        w = T.measure_text(canvas, text, 10, bold=primary) + T.S(34)
        h = T.S(38)
        fill = T.NAVY if primary else T.CARD
        outline = T.NAVY if primary else T.BEIGE_LINE
        fg = "#FFFFFF" if primary else T.NAVY
        radius = T.round_rect_radius(h)

        # ⚠️ **悬停绝对不能重建图形、也不能 tag_raise。**
        #
        # 踩了两次：
        # 第一版在悬停里 canvas.delete(tag) + 重画 + ag_raise(tag)——
        # ag_raise 改变了"画布上谁在鼠标底下"，Tk 因此**再合成一对
        # Enter/Leave**：Enter → 重画 → raise → Leave → 重画 → raise → …
        # 死循环，界面卡住 + 疯狂重绘。
        # 第二版加了"状态没变就不重画"的守卫 —— **没用**，因为 Enter/Leave
        # 是**交替**来的，状态每次都真的在变，守卫拦不住。
        # 现象是**光标在 hand2 与箭头之间频闪**（用户一眼看出来了）。
        #
        # 正解：**只改已有图形的填充色**（itemconfigure）。
        # 它不动层叠、不产生新事件，从根本上没有自激的可能。
        rect = T.round_rect(canvas, x, y, x + w, y + h, radius,
                            fill=fill, outline=outline, width=1)
        label = canvas.create_text(x + w / 2, y + h / 2, text=text, fill=fg,
                                   font=font)

        hot = T.NAVY_SOFT if primary else T.BEIGE_SOFT
        state = {"hovered": False}

        def set_hover(hovered: bool) -> None:
            if state["hovered"] == hovered:
                return
            state["hovered"] = hovered
            canvas.itemconfigure(rect, fill=hot if hovered else fill)
            canvas.configure(cursor="hand2" if hovered else "")

        # R18：文字压在矩形上面 —— Canvas 命中拿到的是**文字**，只绑矩形的话
        # 用户点按钮中心（文字）毫无反应。两个 item 都绑同一组事件；
        # 仍然只改填充色（不重建/不 tag_raise），避免悬停自激。
        for item in (rect, label):
            canvas.tag_bind(item, "<Enter>", lambda _e: set_hover(True))
            canvas.tag_bind(item, "<Leave>", lambda _e: set_hover(False))
            canvas.tag_bind(item, "<Button-1>", lambda _e: command())

    def _draw_mascot(self, canvas: tk.Canvas, height: int) -> None:
        """右下角一只**探头**的立绘：下沿贴窗沿，上半身探出来。

        素材按 1× 出图（见 ``tools/prepare_mascot.py``），Tk 的 PhotoImage
        不随系统 DPI 自动放大，所以这里按总缩放重采样一次。
        """
        from . import assets as ui_assets

        source = ui_assets.image(self.mascot)
        if source is None:
            return
        scale = T.total_scale()
        shown = source
        if scale >= 1.75:
            z = max(1, int(round(scale)))
            shown = source.zoom(z, z)
        elif scale <= 0.7:
            s = max(1, int(round(1.0 / scale)))
            shown = source.subsample(s, s)
        self._mascot_shown = shown

        x = WIDTH_PX - T.S(MASCOT_MARGIN) - shown.width() / 2
        bottom = height - T.S(4)

        # 脚下柔影（两层：外圈更淡）—— "落在"窗沿上，不是飘着
        for radius_x, radius_y, color in (
                (shown.width() * 0.40, T.S(9), T.SHADOW_SOFT),
                (shown.width() * 0.28, T.S(6), T.SHADOW)):
            canvas.create_oval(x - radius_x, bottom - T.S(2) - radius_y,
                               x + radius_x, bottom - T.S(2) + radius_y,
                               fill=color, outline="")

        canvas.create_image(x, bottom, image=shown, anchor="s")

    # ------------------------------------------------------------ 位置
    def _center(self, window: tk.Toplevel) -> None:
        window.update_idletasks()
        root = self.parent.winfo_toplevel()
        x = root.winfo_rootx() + (root.winfo_width() - window.winfo_width()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - window.winfo_height()) // 3
        window.geometry(f"+{max(0, x)}+{max(0, y)}")
