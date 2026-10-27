"""从角色立绘制作界面吉祥物。

⚠️ **开发期工具，不参与运行时。** 需要 Pillow。

用法::

    python tools/prepare_mascot.py [源图]

默认源图是「本项目吉祥物」Q 版主立绘（长发版）。做三件事：

1. **抠背景** —— 立绘是平底灰 + 白色贴纸描边。从四角 flood fill 把背景标成
   哨兵色，再据此生成 alpha。**不能直接按"白色"键控**，那会把角色的白色描边
   一起吃穿。
2. **裁到内容边界**，去掉多余留白。
3. **LANCZOS 降采样**到界面需要的高度 —— 比直接缩放干净得多。

产出 ``src/komaflow/ui/assets/mascot.png``（RGBA）。
"""

from __future__ import annotations

import sys
import os
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

#: 哨兵色。选一个立绘里绝不会出现的颜色（洋红）。
SENTINEL = (255, 0, 255)

#: flood fill 容差。平底灰很干净，40 足够；调大容易吃到浅色发梢。
THRESHOLD = 40

#: 输出高度（像素）。它是预览区右下角的助手，不是主角，所以做小。
OUTPUT_HEIGHT = 168

#: 表情集是标准 **3×3 九宫格**，这里挑四个用（行, 列）。
#: 让侧栏的助手头像能跟着状态变，卡片上的小图标也用它 ——
#: 比一个静态几何图标有"人味"得多。
EXPRESSIONS: dict[str, tuple[int, int]] = {
    "face_idle.png": (0, 0), # 温和微笑 —— 待机
    "face_work.png": (2, 0), # 认真专注 —— 渲染中
    "face_done.png": (0, 1), # 张嘴大笑 —— 完成
    "face_fail.png": (1, 0), # 皱眉 —— 失败
}

#: 表情头像的输出高度
FACE_HEIGHT = 76

#: 正常比例立绘的输出高度。
#: **按实际显示尺寸出图** —— Tk 只能整数倍 subsample，不能任意缩放，
#: 所以界面要显示多大就出多大，省得中间再做一次重采样。
NORMAL_HEIGHT = 252

#: 角色设定集根目录（环境变量指定，默认放仓库外）
_ART_ROOT = Path(os.environ.get("KOMEFLOW_ART_DIR", "_assets_source")) / "mascot_source"

NORMAL_ART = Path(
    str(_ART_ROOT / "透明立绘" / "透明_活泼跳跃.png")
)

EXPRESSION_SHEET = Path(
    str(_ART_ROOT / "Q版" / "Q4v2_表情集_长发版.png")
)

#: 动态跳跃版 —— 渲染中用它，比站姿有"在干活"的感觉
JUMP_SOURCE = Path(
    str(_ART_ROOT / "Q版" / "Q3v2_动态跳跃_长发版.png")
)

#: **透明底**的动作立绘（跳跃）。用户自备，已是 RGBA。
#: 有它就不用再找"平底图"来抠了 —— 直接裁紧就能用。
#: 探头立绘：高度，以及裁切窗（相对裁紧后的内容框）。
#: 用的是 **活泼舞姿** 那张 —— 它上部 15% 处内容宽只有 673px
#: （跳跃那张 859px），头部区更窄，裁出来是紧凑人形而不是横铺的一截。
#: 52% 保住"脸 + 耳朵 + 抬到耳边的手"；横向 18%~78% 收掉两侧袖子，
#: 宽高比落在 0.88，放进说明窗不会把文字挤掉。
PEEK_HEIGHT = 420
PEEK_SRC = NORMAL_ART.parent / "透明_活泼舞姿.png"
PEEK_TOP, PEEK_BOTTOM = 0.00, 0.52
PEEK_LEFT, PEEK_RIGHT = 0.18, 0.78
#: 探头立绘保留内容框上部多少（与 PEEK_BOTTOM 同源；抽成具名常量供调用处引用）
PEEK_KEEP = PEEK_BOTTOM

#: 半身头像的输出边长（原来 84，偏小；放到 96 细节才站得住）
AVATAR_SIZE = 96

#: 卡片角标用的表情小号边长（卡片只有 146px 高，用 76px 的会溢出）
FACE_SMALL = 30

DEFAULT_SOURCE = Path(
    str(_ART_ROOT / "Q版" / "Q1v2_主立绘_长发版.png")
)
OUTPUT = (Path(__file__).resolve().parent.parent
          / "src" / "komaflow" / "ui" / "assets" / "mascot.png")


def cut_background(source: Image.Image) -> Image.Image:
    """把平底背景抠成透明，返回 RGBA。"""
    rgb = source.convert("RGB")
    width, height = rgb.size

    work = rgb.copy()
    for corner in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        ImageDraw.floodfill(work, corner, SENTINEL, thresh=THRESHOLD)

    # 与哨兵色逐个像素比，差为 0 的就是背景
    sentinel_layer = Image.new("RGB", work.size, SENTINEL)
    distance = ImageChops.difference(work, sentinel_layer).convert("L")
    is_background = distance.point(lambda value: 255 if value < 8 else 0)
    alpha = is_background.point(lambda value: 0 if value else 255)

    out = source.convert("RGBA")
    out.putalpha(alpha)
    return out


def trim(image: Image.Image) -> Image.Image:
    """裁到不透明内容的边界。"""
    box = image.getchannel("A").getbbox()
    return image.crop(box) if box else image


def cut_flat_background(source: Image.Image, *, low_edge: int = 196,
                        hard: int = 208, high_edge: int = 240,
                        chroma: int = 18) -> Image.Image:
    """**键控**抠底：低饱和 + 中灰亮度 → 透明。

    表情集的灰底带一点点渐变，四角 flood fill 只吃到一小块（实测 11%），
    所以这里改逐像素键控。

    ⚠️ **alpha 的过渡带必须窄、且必须能到 0**：第一版把过渡带设成 182→246，
    背景灰（亮度 209）只算到 58% 透明，贴到白卡片上就显出一块**灰方块**
    —— 在米色联系表上看不出来，一放进界面就露馅。

    区分依据：背景是**中灰**（196~240 且几乎无彩度），
    而角色的**白色贴纸描边**是纯白（>240，保留）、发色是亚麻（彩度高，保留）。
    """
    out = source.convert("RGBA")
    pixels = out.load()
    span = max(1, hard - low_edge)
    for y in range(out.height):
        for x in range(out.width):
            r, g, b, _ = pixels[x, y]
            high, low = max(r, g, b), min(r, g, b)
            if high - low > chroma or not (low_edge <= low <= high_edge):
                continue
            alpha = 0 if low >= hard else int(255 * (hard - low) / span)
            pixels[x, y] = (r, g, b, alpha)
    return out


def build_faces() -> None:
    """从 3×3 表情集里裁出四个小头像。"""
    if not EXPRESSION_SHEET.is_file():
        print(f"跳过表情：找不到 {EXPRESSION_SHEET}")
        return
    with Image.open(EXPRESSION_SHEET) as sheet:
        sheet.load()
        rgb = sheet.convert("RGB")
        cell_w, cell_h = rgb.width // 3, rgb.height // 3
        print(f"表情集 {rgb.size[0]}×{rgb.size[1]} → 每格 {cell_w}×{cell_h}")
        for name, (row, col) in EXPRESSIONS.items():
            cell = rgb.crop((col * cell_w, row * cell_h,
                             (col + 1) * cell_w, (row + 1) * cell_h))
            cut = trim(cut_flat_background(cell))
            face = circle_portrait(cut)
            face.save(OUTPUT.parent / name, optimize=True)
            # 卡片角标要用的**小号**：大号 76px 塞进 146px 高的卡片会溢出边缘
            small = face.resize((FACE_SMALL, FACE_SMALL), Image.LANCZOS)
            small.save(OUTPUT.parent / name.replace("face_", "face_small_"),
                       optimize=True)
            print(f" {name} {face.size[0]}×{face.size[1]} (+{FACE_SMALL}px 小号)")


def build_avatars() -> None:
    """从**主立绘**裁半身头像（要有躯干）。

    ⚠️ 表情集是纯头部素材，拿它当头像是错的 —— 会变成"一颗悬空的头加头发"。
    这里改从主立绘裁：头顶往下取 46% 高度，正好是头 + 肩 + 手臂 + 尾巴根。

    两张：
    * ``avatar.png`` —— 主立绘（待机 / 完成 / 出错）
    * ``avatar_work.png`` —— 动态跳跃（渲染中，动起来的那张）
    """
    jobs = (("avatar.png", DEFAULT_SOURCE), ("avatar_work.png", JUMP_SOURCE))
    for name, source in jobs:
        if not source.is_file():
            print(f" 跳过 {name}：找不到 {source}")
            continue
        with Image.open(source) as raw:
            raw.load()
            figure = trim(cut_background(raw))
        # Q 版角色的头占比很大，裁多少差别极明显：
        # 0.68 → 头+肩+手臂+尾巴，信息量全塞进 96px，脸只剩一小块 → "高糊"
        # 0.46 → 只到胸口，看着又像"只有一颗头"
        # 0.55 → 头 + 肩 + 一点外套，脸能占到大半，躯干也还在 ← 采用
        bust_h = int(figure.height * 0.55)
        bust = figure.crop((0, 0, figure.width, bust_h))
        side = bust_h
        left = max(0, (bust.width - side) // 2)
        bust = bust.crop((left, 0, min(bust.width, left + side), bust_h))
        bust = circle_portrait(bust, height=AVATAR_SIZE, ring=(12, 57, 103))
        bust.save(OUTPUT.parent / name, optimize=True)
        print(f" {name} {bust.size[0]}×{bust.size[1]} ← {source.name}")


def circle_portrait(cut: Image.Image, *, height: int = FACE_HEIGHT,
                    ring: tuple = (179, 58, 43)) -> Image.Image:
    """把立绘裁成**圆形头像**。

    为什么要裁圆：直接贴一块裁到脖子的图，看着就是"一颗悬空的头"，很怪。
    圆形 + 一圈细描边是"用户头像"的通用视觉语言，一眼就懂。
    """
    side = max(cut.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(cut, ((side - cut.width) // 2, (side - cut.height) // 2))

    mask = Image.new("L", (side, side), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, side - 1, side - 1], fill=255)
    square.putalpha(ImageChops.multiply(square.getchannel("A"), mask))

    face = square.resize((height, height), Image.LANCZOS)

    # ⚠️ **预锐化**。界面是按 96DPI 画的，而屏幕是 150% 缩放 ——
    # Windows 会把整张位图**拉伸 1.5 倍**，画面一定发虚。
    # 在降采样之后补一道 UnsharpMask，能把被拉伸糊掉的边缘找回来一部分。
    # （根治要等 DPI 感知那一版，但这一步几乎零成本、肉眼立见。）
    face = face.filter(ImageFilter.UnsharpMask(radius=1.1, percent=115,
                                               threshold=2))

    ImageDraw.Draw(face).ellipse([1, 1, height - 2, height - 2],
                                 outline=ring + (255,), width=3)
    return face


#: 高光回收的膝点：亮度到这里算"到顶"，以上的压缩到 HIGH_CEILING
HIGH_KNEE = 250
#: 压缩之后最高落在哪（给高光留一点余地，不再被削平成一块白）
HIGH_CEILING = 253


def recover_highlights(image: Image.Image) -> Image.Image:
    """把最顶上那一点高光压回来，中间调一点不动。

    为什么要这一步
    --------------
    量过（不透明像素里"某通道打满 255"的比例）：

    ==================== ==========
    用户自备的原图 13.40%
    直接预锐化之后 19.95%
    ==================== ==========

    原图本身就偏亮，而 ``UnsharpMask`` 会把亮部边缘继续推高 ——
    等于我们**又给过曝加了一截**。加一道高光回收之后纯过曝降到 **0%**。

    只影响 ``HIGH_KNEE`` 以上的像素，肤色和中间调完全不动，
    所以看起来几乎没变化，只是那些"白成一片"的地方重新有了层次。
    """
    rgb = image.convert("RGB")
    lut = []
    for value in range(256):
        if value <= HIGH_KNEE:
            lut.append(value)
        else:
            ratio = (value - HIGH_KNEE) / (255.0 - HIGH_KNEE)
            lut.append(int(round(HIGH_KNEE + ratio * (HIGH_CEILING - HIGH_KNEE))))
    fixed = rgb.point(lut * 3)
    out = fixed.convert("RGBA")
    out.putalpha(image.getchannel("A"))
    return out


def build_normal_figure() -> None:
    """**正常比例**立绘（透明底 · 跳跃动作）—— 预览区右下角那个伴侣用。

    用户提供了 ``透明立绘/透明_活泼跳跃.png``，是带 alpha 的成品，
    所以这里**不再抠底** —— ``cut_background`` 会先 ``convert("RGB")``，
    把 alpha 直接抹平，透明底反而被毁掉。

    左上角的头像仍然用 Q 版（五官大，小尺寸才看得清）。
    """
    if not NORMAL_ART.is_file():
        print(f" 跳过正常立绘：找不到 {NORMAL_ART}")
        return
    with Image.open(NORMAL_ART) as raw:
        raw.load()
        art = raw.convert("RGBA")
    # 已是透明底：按 alpha 裁到内容边界即可
    figure = trim(art)
    scale = NORMAL_HEIGHT / figure.height
    out = figure.resize((max(1, round(figure.width * scale)), NORMAL_HEIGHT),
                        Image.LANCZOS)
    # ⚠️ 锐化**要轻**。原来 percent=90，实测把亮部边缘推爆：
    # 纯过曝从原图的 13.40% 涨到 19.95%；降到 45 只剩 15.61%。
    # （DPI 感知那一版已经在 app.py 里开了，系统不再拉伸位图，
    # 这道"抵消拉伸"的锐化本来也不需要那么狠。）
    out = out.filter(ImageFilter.UnsharpMask(radius=1.0, percent=45, threshold=2))
    # 再走一道高光回收：纯过曝 → 0%，肤色与中间调不变
    out = recover_highlights(out)
    out.save(OUTPUT.parent / "mascot_normal.png", optimize=True)
    print(f" mascot_normal.png {out.size[0]}×{out.size[1]} ← {NORMAL_ART.name}"
          "（已降锐化 + 压高光）")


def build_peeking_figure() -> None:
    """生成**探头用**的立绘 ``mascot_peek.png``（只取上半身，从原图裁）。

    为什么要单独一张
    ----------------
    "缺插件"说明窗里要一只**扒着窗沿往里看**的吉祥物 —— 只露上半身、
    下半身被窗沿裁掉。运行时要做到这点只能靠 Tk 裁图，而 Tk 的
    ``zoom`` 是**最近邻插值**：拿已缩小的 ``mascot_normal.png``（162×252）
    放大，边缘会明显发糊。

    所以从**原图**（1493×2304）直接裁上半身、按目标高度重采样一次 ——
    只重采样一次，画质最好。运行时直接铺这张，不再缩放。

    ⚠️ 裁切留了"要探出来"的意思：取上部约 62%，并且**不裁到头顶贴边**，
       留一点呼吸空间（贴死了像"被切掉"而不是"探头"）。
    """
    if not NORMAL_ART.is_file():
        print(f" 跳过探头立绘：找不到 {NORMAL_ART}")
        return
    with Image.open(NORMAL_ART) as raw:
        raw.load()
        art = raw.convert("RGBA")
    figure = trim(art)

    # 只保留上部 —— 下半身留着也会被窗沿盖住，不如直接裁掉省体积
    keep_h = max(1, round(figure.height * PEEK_KEEP))
    upper = figure.crop((0, 0, figure.width, keep_h))

    scale = PEEK_HEIGHT / upper.height
    out = upper.resize((max(1, round(upper.width * scale)), PEEK_HEIGHT),
                       Image.LANCZOS)
    # 锐化跟 mascot_normal 用同一档（percent=90 会把亮部推爆，实测过）
    out = out.filter(ImageFilter.UnsharpMask(radius=1.0, percent=45, threshold=2))
    out = recover_highlights(out)
    out.save(OUTPUT.parent / "mascot_peek.png", optimize=True)
    print(f" mascot_peek.png {out.size[0]}×{out.size[1]} ← {NORMAL_ART.name}"
          f"（取上部 {PEEK_KEEP:.0%}，供缺插件说明窗的探头用）")


def main() -> int:
    source_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE
    if not source_path.is_file():
        print(f"找不到源图：{source_path}")
        return 1

    print(f"源图：{source_path}")
    with Image.open(source_path) as raw:
        raw.load()
        print(f" 尺寸 {raw.size[0]}×{raw.size[1]} 模式 {raw.mode}")

        cut = cut_background(raw)
        trimmed = trim(cut)
        print(f" 抠底+裁紧后 {trimmed.size[0]}×{trimmed.size[1]}")

        scale = OUTPUT_HEIGHT / trimmed.height
        target = (max(1, round(trimmed.width * scale)), OUTPUT_HEIGHT)
        final = trimmed.resize(target, Image.LANCZOS)

        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        final.save(OUTPUT, optimize=True)

    print(f"已写出：{OUTPUT} {final.size[0]}×{final.size[1]}")
    print("四角 alpha（应为 0）：",
          [final.getpixel(p)[3] for p in
           ((0, 0), (final.width - 1, 0), (0, final.height - 1),
            (final.width - 1, final.height - 1))])

    build_faces()
    build_avatars()
    build_normal_figure()
    build_peeking_figure()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
