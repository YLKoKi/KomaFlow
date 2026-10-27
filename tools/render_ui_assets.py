"""生成界面用的抗锯齿装饰图（音符 / 飘带 / 狐耳 / 银铃 / 徽标）。

⚠️ **开发期工具，不参与运行时。** 需要 Pillow（`pip install pillow`）；
生成出来的 PNG 才是随包分发的东西，程序本身**依然零第三方依赖**（NFR-16）。

为什么需要它
------------
Tk 的 Canvas **不做抗锯齿**：圆、斜线、曲线的边缘必然带台阶。想干净，
就把这些装饰用 Pillow **4 倍超采样**画好、再 LANCZOS 降采样存成 PNG，
程序里 1:1 贴上去 —— 像素里已经带着抗锯齿了。

🚨 **本脚本不再生成 LOGO**（`logo*.png` / `logo.ico` 都不写）。
   生成器是 ``tools/build_logo_assets.py`` —— 它按尺寸分档
   （16~40 用无字简化版、48 起用完整标记）并打包多档 `.ico`。
   本文件里那套自绘 LOGO（``build_logo_flat`` 等）**已经没人调用**，留着只作参考；
   千万别再把它的输出写进 assets —— 它没有分档，会把顶栏那个 40px 徽标糊掉，
   而且跟外部品牌资产不是同一个标记。

用法::

    python tools/render_ui_assets.py # 装饰图 + 徽标
    python tools/build_logo_assets.py # LOGO / 图标（另一支）
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

#: 超采样倍数。4 倍足够，8 倍收益很小但慢很多。
SS = 4

OUT = Path(__file__).resolve().parent.parent / "src" / "komaflow" / "ui" / "assets"

TEAL = (62, 201, 192)
TEAL_SOFT = (180, 224, 219)
TEAL_WATERMARK = (205, 232, 228)
BEIGE = (234, 208, 176)


def _canvas(width: int, height: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGBA", (width * SS, height * SS), (0, 0, 0, 0))
    return image, ImageDraw.Draw(image)


def _save(image: Image.Image, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    final = image.resize((image.width // SS, image.height // SS), Image.LANCZOS)
    final.save(OUT / name, optimize=True)
    print(f" {name} {final.width}×{final.height}")


def _note(draw: ImageDraw.ImageDraw, cx: float, cy: float, size: float,
          color: tuple) -> None:
    """一个音符：符头 + 符干 + 符尾。坐标是"最终像素"，内部会乘 SS。"""
    def s(v: float) -> float:
        return v * SS

    head_r = size * 0.26
    draw.ellipse([s(cx - head_r), s(cy + size * 0.30 - head_r),
                  s(cx + head_r), s(cy + size * 0.30 + head_r)], fill=color)
    stem = max(2, int(size * 0.11 * SS))
    draw.line([s(cx + head_r * 0.85), s(cy + size * 0.30),
               s(cx + head_r * 0.85), s(cy - size * 0.42)],
              fill=color, width=stem)
    draw.line([s(cx + head_r * 0.85), s(cy - size * 0.42),
               s(cx + head_r * 0.85 + size * 0.36), s(cy - size * 0.24)],
              fill=color, width=stem)


def _ribbon(draw: ImageDraw.ImageDraw, cx: float, cy: float, height: float,
            color: tuple, sign: int, width: float, spread: float = 0.30,
            sway: float = 1.7, samples: int = 60) -> None:
    """抽象的 S 形飘带（"双马尾"意象）。纯几何曲线，不指向任何角色造型。"""
    points = []
    for index in range(samples + 1):
        t = index / samples
        x = cx + sign * height * (spread * 0.5 + spread * t) \
            + sign * height * 0.07 * math.sin(t * math.pi * sway)
        y = cy - height * 0.5 + height * t
        points.append((x * SS, y * SS))
    draw.line(points, fill=color, width=max(1, int(width * SS)), joint="curve")
    # 端点补圆，免得线头是方的
    radius = width * SS / 2
    for x, y in (points[0], points[-1]):
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=color)


# --------------------------------------------------------------------------
# 本项目的角色元素
# --------------------------------------------------------------------------
#
# 角色是她自己设定的原创 OC，配色与标志物都写在设定文档里：
# 主色 枫红 #B33A2B / 辅色 深褐 #4A3728、暖橘 #E88B3C / 发色 亚麻 #D9C09A
# 瞳 琥珀 #D89A2B（竖瞳） / 点睛 银 #C9CDD4、红绳 #C0392B
# 标志物：狐耳（耳内白绒、耳尖白毛）、尾巴（向后延伸、蓬松）、左脚踝银铃 + 红绳
#
# 下面把它们做成可贴的图元。

FOX_RED = (179, 58, 43) # #B33A2B 主色
FOX_ORANGE = (232, 139, 60) # #E88B3C
FOX_LINEN = (217, 192, 154) # #D9C09A 发色 / 耳外
FOX_CREAM = (247, 241, 232) # 耳内绒毛（偏白）
FOX_PINK = (238, 176, 186) # 耳内粉
FOX_AMBER = (216, 154, 43) # #D89A2B 瞳色
FOX_SILVER = (201, 205, 212) # #C9CDD4 银铃
FOX_CORD = (192, 57, 43) # #C0392B 红绳
FOX_BROWN = (74, 55, 40) # #4A3728
FOX_TAIL_SOFT = (243, 226, 206) # 尾巴水印（极浅暖）
FOX_TAIL_TIP = (250, 245, 238)


def _fox_ear(draw: ImageDraw.ImageDraw, cx: float, base_y: float, width: float,
             height: float, *, tilt: float = 0.0, outline: tuple = FOX_BROWN,
             outer: tuple = FOX_LINEN, inner: tuple = FOX_PINK) -> None:
    """一只狐耳。

    **窄而高**才像狐狸，宽扁了会变成蝴蝶结（第一版就是那么翻车的）。
    三层叠加，与设定文档一致：外耳（亚麻+描边）→ 耳内绒毛（粉）→ 耳尖白毛。
    """
    def s(v: float) -> float:
        return v * SS

    def poly(points, **kw) -> None:
        draw.polygon([(s(x), s(y)) for x, y in points], **kw)

    tip_x, tip_y = cx + tilt * width, base_y - height
    stroke = max(1, int(1.8 * SS))

    poly([(cx - width / 2, base_y), (tip_x, tip_y), (cx + width / 2, base_y)],
         fill=outer, outline=outline, width=stroke)
    poly([(cx - width * 0.27, base_y - height * 0.09),
          (tip_x, tip_y + height * 0.30),
          (cx + width * 0.27, base_y - height * 0.09)], fill=inner)
    poly([(tip_x - width * 0.26, tip_y + height * 0.30),
          (tip_x, tip_y),
          (tip_x + width * 0.26, tip_y + height * 0.30)], fill=(255, 255, 255))


def _fox_tail(draw: ImageDraw.ImageDraw, cx: float, cy: float, height: float,
              color: tuple, tip_color: tuple, sign: int,
              samples: int = 54, base_r: float = 15.0, tip_r: float = 3.0) -> None:
    """狐狸尾巴。

    做法是**沿 S 形曲线摆一串半径递减的圆** —— 自然收成尖、还带蓬松感，
    比"把一条线描粗"像样得多。这也贴合设定里那句「尾巴划出一道弧线」。
    """
    for index in range(samples + 1):
        t = index / samples
        x = cx + sign * height * (0.10 + 0.26 * t) \
            + sign * height * 0.055 * math.sin(t * math.pi * 1.55)
        y = cy - height * 0.5 + height * t
        radius = base_r * (1 - t) + tip_r * t
        fill = tip_color if t > 0.82 else color
        draw.ellipse([(x - radius) * SS, (y - radius) * SS,
                      (x + radius) * SS, (y + radius) * SS], fill=fill)


def _bell(draw: ImageDraw.ImageDraw, cx: float, cy: float, size: float,
          body: tuple = FOX_SILVER, dark: tuple = (139, 146, 158),
          cord: tuple = FOX_CORD) -> None:
    """小银铃 + 红绳。设定里系在左脚踝，是「跳舞时的节拍器」。

    形状要点：**上半圆顶 + 下半外撇口沿**。少了外撇就成了帐篷（第一版那样）。
    """
    def s(v: float) -> float:
        return v * SS

    def pts(pairs):
        return [(s(x), s(y)) for x, y in pairs]

    radius = size * 0.40
    stroke = max(1, int(size * 0.055 * SS))

    # 红绳 + 绳结
    draw.line([s(cx), s(cy - size * 1.05), s(cx), s(cy - size * 0.70)],
              fill=cord, width=max(1, int(size * 0.12 * SS)))
    knot = size * 0.13
    draw.ellipse([s(cx - knot), s(cy - size * 1.16),
                  s(cx + knot), s(cy - size * 0.90)], fill=cord)

    # 顶环
    draw.ellipse([s(cx - radius * 0.24), s(cy - size * 0.76),
                  s(cx + radius * 0.24), s(cy - size * 0.40)],
                 outline=dark, width=stroke)

    # 铃身
    draw.pieslice([s(cx - radius), s(cy - radius * 0.98),
                   s(cx + radius), s(cy + radius * 1.02)],
                  start=180, end=360, fill=body, outline=dark, width=stroke)
    draw.polygon(pts([(cx - radius, cy - radius * 0.02),
                      (cx - radius * 1.22, cy + radius * 0.46),
                      (cx + radius * 1.22, cy + radius * 0.46),
                      (cx + radius, cy - radius * 0.02)]),
                 fill=body, outline=dark, width=stroke)

    # 铃舌
    draw.ellipse([s(cx - radius * 0.22), s(cy + radius * 0.48),
                  s(cx + radius * 0.22), s(cy + radius * 0.92)], fill=dark)


def _slit_eye(draw: ImageDraw.ImageDraw, cx: float, cy: float, size: float,
              iris: tuple = FOX_AMBER, pupil: tuple = FOX_BROWN) -> None:
    """琥珀竖瞳。设定里的标志性识别点之一（狐/猫类裂隙瞳）。"""
    def s(v: float) -> float:
        return v * SS

    r = size * 0.5
    draw.ellipse([s(cx - r), s(cy - r), s(cx + r), s(cy + r)], fill=iris)
    # 竖直裂隙
    draw.polygon([(s(cx), s(cy - r * 0.92)), (s(cx + r * 0.30), s(cy - r * 0.2)),
                  (s(cx + r * 0.18), s(cy + r * 0.72)), (s(cx), s(cy + r * 0.95)),
                  (s(cx - r * 0.18), s(cy + r * 0.72)), (s(cx - r * 0.30), s(cy - r * 0.2))],
                 fill=pupil)
    draw.ellipse([s(cx - r * 0.52), s(cy - r * 0.62), s(cx - r * 0.10), s(cy - r * 0.20)],
                 fill=(255, 255, 255, 215))


LOGO_NAVY = (12, 57, 103)
LOGO_NAVY_DEEP = (7, 34, 62)
LOGO_CREAM = (247, 243, 235)
NEON_CYAN = (92, 240, 228)
NEON_MAGENTA = (246, 140, 232)
NEON_MINT = (176, 250, 240)


def _lerp(a, b, t):
    """两个 RGB 颜色按 ``t`` 线性插值，返回同长度的整数元组。"""
    return tuple(int(round(ba + (bb - ba) * t)) for ba, bb in zip(a, b))

#: 狐狸头的轮廓（比例坐标，``x``/``y`` 都在 0~1）。
#:
#: ⚠️ 这段造型返工过好几轮，教训值得留着：
#:
#: 1. **别写成一串直线点**：9 个点直接连出来棱角分明，像机器人头或猫
#: （``draw.polygon`` 不会给你圆角）。
#: 2. **别用"参数化推导"去拼耳朵**：试过按耳宽/耳根间距推贝塞尔控制点，
#: 控制点会把曲线拉出端点范围，耳朵最后变成横贯整个圆的平板。
#: 曲线的手感和参数不是线性关系，凭公式推就是碰运气。
#: 3. **两手都要用**：耳尖要尖 → 直线段；脸颊下巴要圆 → 贝塞尔采样。
#: 4. **点序要顺着轮廓走一圈**（这条最容易翻车）：。耳朵必须从
#: **耳根外侧**起笔、绕到耳尖、再回到耳根内侧。曾经把耳尖排在第一位，
#: 结果左耳尖直接连到右耳尖，两只耳朵被凭空切掉。
#:
#: 交付前用尺子核过（``work/logo_diff.py``）：单耳底宽 0.224、耳高 0.232，
#: 整体 x 0.248~0.752。
LOGO_HEAD_EARS: list[tuple[float, float]] = [
    (0.278, 0.392), # 左耳根外侧（起笔点，脸颊曲线最后回到这里）
    (0.296, 0.230), # 左耳外缘中段
    (0.318, 0.116), # 左耳尖
    (0.390, 0.122), # 左耳内上（耳尖留一点宽度，纯尖点缩到 16px 会消失）
    (0.424, 0.356), # 左耳内根
    (0.500, 0.344), # 额头中点（那道浅凹，别太深否则像两只角）
    (0.576, 0.356), # 右耳内根
    (0.610, 0.122), # 右耳内上
    (0.682, 0.116), # 右耳尖
    (0.704, 0.230), # 右耳外缘中段
    (0.722, 0.392), # 右耳根外侧 —— 与 LOGO_HEAD_CHEEKS 首段起点必须一致
]

#: 脸颊 + 下巴的曲线段，从右耳根外侧接回左耳根外侧。每段 ``(P0, C1, C2, P1)``。
#:
#: ⚠️ 首段起点要**正好等于右耳根外侧**、末段终点要**正好等于左耳根外侧**，
#: 否则轮廓会断开一截（实测断开后渲染出来耳朵是"飘"在头上的两块）。
LOGO_HEAD_CHEEKS: list[tuple[tuple[float, float], ...]] = [
    # 右脸颊：轻轻鼓一下，不要鼓成球
    ((0.722, 0.392), (0.782, 0.486), (0.786, 0.628), (0.720, 0.704)),
    # 收下巴（右半）
    ((0.720, 0.704), (0.664, 0.796), (0.556, 0.868), (0.500, 0.874)),
    # 收下巴（左半）
    ((0.500, 0.874), (0.444, 0.868), (0.336, 0.796), (0.280, 0.704)),
    # 左脸颊：回到左耳根外侧，收口
    ((0.280, 0.704), (0.214, 0.628), (0.218, 0.486), (0.278, 0.392)),
]

#: 头部整体缩放。脸已经按比例画好了，这里只是留出圆里的呼吸空边。
LOGO_HEAD_SCALE = 0.99


def logo_head_points() -> list[tuple[float, float]]:
    """整只狐狸头的轮廓点：**直线耳朵 + 曲线脸**。

    耳尖那几段走直线 —— 狐狸耳就是要尖，用曲线反而磨圆；
    脸颊和下巴走贝塞尔采样，否则下颌会是折线（看着像纸片剪的）。
    """
    return LOGO_HEAD_EARS + _sample_cubic(LOGO_HEAD_CHEEKS)


#: 播放三角（比例坐标）。中心略偏右一点点，避免视觉重心偏左。
#: 半高 0.128 —— 再大就把脸颊占了，再小在小尺寸下会糊成一块。
LOGO_PLAY = [(0.417, 0.472), (0.417, 0.728), (0.673, 0.600)]

#: 那道"动势弧"的参数。
#: ⚠️ 弧必须**整段落在圆内**：第一版弧的外沿压到了描边上、还被裁掉一截，
#: 读起来像两条分开的元素、不像"狐耳后面甩出来的动势"。
#: ⚠️ 半径也别太小：贴着脑袋画的话整条弧会被耳朵和脸挡住，等于白画
#: （实测 0.90 时只有零星几个像素露出来）。0.94 才能露成一道清楚的弧。
LOGO_ARC_BOX = 0.94
LOGO_ARC_START = 210.0
LOGO_ARC_END = 330.0
LOGO_ARC_WIDTH = 0.056


def _sample_cubic(curves: list[tuple[tuple[float, float], ...]],
                  per_curve: int = 26) -> list[tuple[float, float]]:
    """把一串三次贝塞尔转成密集点。

    Tk 和 Pillow 的 ``polygon`` 都只吃直线段，所以"曲线"得自己采样出来。
    每段 26 个点，480px 的画布上肉眼已经看不出折线。
    """
    points: list[tuple[float, float]] = []
    for p0, c1, c2, p1 in curves:
        for step in range(per_curve + 1):
            t = step / per_curve
            if t == 0 and points:
                continue # 接头处不要重复点
            u = 1.0 - t
            x = (u ** 3 * p0[0] + 3 * u * u * t * c1[0]
                 + 3 * u * t * t * c2[0] + t ** 3 * p1[0])
            y = (u ** 3 * p0[1] + 3 * u * u * t * c1[1]
                 + 3 * u * t * t * c2[1] + t ** 3 * p1[1])
            points.append((x, y))
    return points



def build_logo_flat(size: int = 480) -> Image.Image:
    """产品 LOGO：**奶白圆底 + 深蓝狐狸头 + 挖空的播放三角 + 青色动势弧**。

    三层含义：

    * **狐狸头** —— 角色（用户自设 OC本项目的狐耳）
    * **青色动势弧** —— 在动 / 在跳舞（她就是街头舞者，产品也是做动作渲染的）
    * **播放三角** —— 渲染出片

    ⚠️ 这里**不含任何字母**。旧版 LOGO 上写的是 `MMD` —— 需求书 明确把
    `MMD` 列为需避开的他人商标、 记着原代号正因此废弃、14.7 又列为
    发布前一票否决，所以图标这一处必须去掉。

    **为什么是反白而不是渐变深蓝**：LOGO 会被缩到 **16×16** 用在任务栏上，
    实测渐变版在那个尺寸会糊成一团浅色球（对比图见 `tools/logo_variants.py`）。
    奶白底 + 深蓝剪影对比度最高，16px 下三层含义都还认得出；
    而且任务栏是深色的，浅底图标不会糊进背景。

    造型上的几处讲究（都是对着渲染结果一版版调出来的）：

    * 耳尖走直线、脸颊走曲线（见 :data:`LOGO_HEAD_EARS` 的说明）
    * 耳朵不能太宽：≳0.4 会摊平成蝙蝠/老鼠耳
    * 额头那道浅凹不能太深，太深就把两个耳朵切成"两只角"
    * 动势弧**整段在圆内**，不压描边
    """
    big = size * SS
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # 奶白圆底 + 一圈深蓝描边（描边让它在浅色背景上也有边界）
    draw.ellipse([0, 0, big - 1, big - 1], fill=LOGO_CREAM)
    rim = max(2, int(size * 0.022 * SS))
    draw.ellipse([rim, rim, big - 1 - rim, big - 1 - rim],
                 outline=LOGO_NAVY, width=rim)

    def place(pairs, scale: float = 1.0) -> list[tuple]:
        """比例坐标 → 画布坐标。``scale`` 以画布中心为基准放大。"""
        return [((x - 0.5) * scale * big + big / 2,
                 (y - 0.5) * scale * big + big / 2) for x, y in pairs]

    # **动势弧**：从脑后甩出来的一道月牙。
    # 这只角色是"街头舞者"，而产品又是做动作/跳舞渲染的 —— 弧线同时说出了
    # "在动"和"跳舞"这两层意思，补上"只有狐狸头、没体现动作"缺的那一半。
    # 用粗弧而不是细速度线：细线在 16px 会被圆边切断，看着像瑕疵。
    margin = big * (1.0 - LOGO_ARC_BOX) / 2.0
    arc_width = max(2, int(size * LOGO_ARC_WIDTH * SS))
    # 先把弧画在一张独立层上，再把它的 alpha 与"圆面"取交集 ——
    # 这样弧绝不会越出圆边，也就不会去压那圈描边
    arc_layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(arc_layer).arc(
        [margin, margin, big - margin, big - margin],
        start=LOGO_ARC_START, end=LOGO_ARC_END,
        fill=(96, 238, 226), width=arc_width)
    disc = Image.new("L", (big, big), 0)
    ImageDraw.Draw(disc).ellipse([rim, rim, big - 1 - rim, big - 1 - rim], fill=255)
    from PIL import ImageChops
    arc_layer.putalpha(ImageChops.multiply(arc_layer.getchannel("A"), disc))
    canvas.alpha_composite(arc_layer)

    head = place(logo_head_points(), LOGO_HEAD_SCALE)
    draw.polygon(head, fill=LOGO_NAVY)

    # 播放三角：用底色挖空，比再画一个图形干净
    draw.polygon(place(LOGO_PLAY), fill=LOGO_CREAM)

    return canvas.resize((size, size), Image.LANCZOS)


def build_logo(size: int = 480) -> Image.Image:
    """生成产品 LOGO：深蓝圆底 + **渐变狐狸头剪影 + 挖空的播放三角**。

    ⚠️ 这里**刻意不含任何字母**。上一版 LOGO 上写的是 `MMD`，而需求书
     明确把 `MMD` 列为需避开的他人商标、 记着原代号正因此废弃、
    14.7 又把它列成发布前一票否决 —— 所以图标这一处必须去掉。

    记号取自**本项目吉祥物「本项目吉祥物」**的狐耳（耳间那道上凸就是额头），
    中间挖一个"播放/渲染"三角。**填色而不是描边** —— 描边版在小尺寸下会
    变成一堆点和线，像电路图（第一版就是那么翻车的）。
    """
    big = size * SS
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # 深蓝圆底 + 一圈稍亮的边（现代扁平，不做假体积）
    draw.ellipse([0, 0, big - 1, big - 1], fill=LOGO_NAVY)
    rim = max(2, int(size * 0.013 * SS))
    draw.ellipse([rim, rim, big - 1 - rim, big - 1 - rim],
                 outline=(26, 84, 132), width=rim)

    def place(pairs) -> list[tuple]:
        return [(x * size * SS, y * size * SS) for x, y in pairs]

    # 狐狸头：两耳 + 额头凹口 + 脸颊 + 下巴，一笔闭合成型
    head = place([
        (0.300, 0.115), # 左耳尖
        (0.420, 0.375), # 左耳内根（额头左）
        (0.580, 0.375), # 右耳内根（额头右）
        (0.700, 0.115), # 右耳尖
        (0.795, 0.495), # 右外缘
        (0.740, 0.720), # 右颊
        (0.500, 0.885), # 下巴
        (0.260, 0.720), # 左颊
        (0.205, 0.495), # 左外缘
    ])
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).polygon(head, fill=255)

    # 上青下粉的渐变
    gradient = Image.new("RGBA", (big, big))
    painter = ImageDraw.Draw(gradient)
    for y in range(big):
        painter.line([(0, y), (big, y)],
                     fill=_lerp(NEON_CYAN, NEON_MAGENTA, y / (big - 1)))

    # 外发光（先合成模糊层，再压上清晰层）
    glow = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    glow.paste(gradient, (0, 0), mask)
    glow = glow.filter(ImageFilter.GaussianBlur(radius=size * 0.032 * SS))
    glow.putalpha(glow.getchannel("A").point(lambda value: int(value * 0.80)))
    canvas.alpha_composite(glow)
    canvas.paste(gradient, (0, 0), mask)

    # 播放三角：用底色挖空，比再画一个图形干净
    ImageDraw.Draw(canvas).polygon(
        place([(0.432, 0.478), (0.432, 0.722), (0.638, 0.600)]), fill=LOGO_NAVY)

    return canvas.resize((size, size), Image.LANCZOS)


def build() -> None:
    print("生成界面装饰图（超采样 ×%d）…" % SS)

    # 1) 顶栏音符：深蓝底上用，颜色亮一点
    image, draw = _canvas(58, 32)
    _note(draw, 12, 21, 16, TEAL)
    _note(draw, 36, 11, 11, BEIGE)
    _save(image, "deco_notes_header.png")

    # 2) 卡片角落的小音符（浅底上用，柔和的青）
    image, draw = _canvas(30, 34)
    _note(draw, 14, 21, 19, TEAL_SOFT)
    _save(image, "deco_note_card.png")

    # 3) 预览区水印：左右两条飘带
    for name, sign in (("deco_ribbon_l.png", -1), ("deco_ribbon_r.png", 1)):
        image, draw = _canvas(150, 470)
        _ribbon(draw, 75, 235, 430, TEAL_WATERMARK, sign, 9)
        _save(image, name)

    # 4) 预览区水印：音符组
    image, draw = _canvas(170, 120)
    _note(draw, 34, 76, 54, TEAL_WATERMARK)
    _note(draw, 96, 40, 40, TEAL_WATERMARK)
    _note(draw, 132, 84, 30, (218, 226, 216))
    _save(image, "deco_notes_soft.png")

    # ── 以下是本项目的元素 ──

    # 5) 狐耳一对：顶栏里从 LOGO 圆后面冒出来（窄而高，别做成蝴蝶结）
    image, draw = _canvas(66, 56)
    _fox_ear(draw, 24, 52, 22, 44, tilt=-0.22)
    _fox_ear(draw, 42, 52, 22, 44, tilt=0.22)
    _save(image, "deco_fox_ears.png")

    # 6) 狐狸尾巴：预览区两侧水印（替换抽象飘带）
    for name, sign in (("deco_tail_l.png", -1), ("deco_tail_r.png", 1)):
        image, draw = _canvas(160, 470)
        _fox_tail(draw, 80, 235, 420, FOX_TAIL_SOFT, FOX_TAIL_TIP, sign,
                  base_r=17, tip_r=3)
        _save(image, name)


    # 7) 银铃 + 红绳：完成态图标
    image, draw = _canvas(34, 40)
    _bell(draw, 17, 22, 26)
    _save(image, "deco_bell.png")

    # 8) LOGO —— **不在这里生成**。
    #
    # ⚠️ 这里以前自己画一套 LOGO（深蓝圆底 + 渐变狐头 + 播放三角）并**覆盖**
    # ``logo.png`` / ``logo.ico`` / ``logo_{26,40,64,96}.png``。问题是那套
    # 完全没有尺寸分档：40px 顶栏徽标塞着圆里那堆细节，缩下来就糊成一团
    # （用户报的"LOGO 坏了"就是这个）。而且它跟外部品牌资产
    # **不是同一个标记** —— 谁跑一次这个脚本，就会把品牌标记悄悄换掉。
    #
    # 现在 LOGO 一律由 ``tools/build_logo_assets.py`` 产（自带尺寸分档：
    # 16~40 用无字简化版、48 起用完整标记，并打包多档 .ico）。
    print(" LOGO 不在这里生成 —— 用 tools/build_logo_assets.py")

    # 9) 序号徽标 + 完成勾：预渲染的抗锯齿小圆。
    # ⚠️ Tk 的 create_oval **不做抗锯齿**，圆边必然一圈台阶（放大看很毛）。
    # 这类"固定尺寸 + 少数颜色"的元素就该预渲染 —— 尺寸不定的大圆角矩形没法这么干。
    BADGES = (
        ("badge_navy.png", (12, 57, 103)),
        ("badge_maple.png", (179, 58, 43)),
        ("badge_ok.png", (46, 158, 107)),
        ("badge_danger.png", (196, 69, 63)),
        ("badge_beige.png", (220, 195, 163)),
        ("badge_grey.png", (201, 210, 220)),
    )
    for name, fill in BADGES:
        image, draw = _canvas(24, 24)
        draw.ellipse([0, 0, 24 * SS - 1, 24 * SS - 1], fill=fill)
        _save(image, name)

    image, draw = _canvas(22, 22)
    draw.ellipse([0, 0, 22 * SS - 1, 22 * SS - 1], fill=(46, 158, 107))
    draw.line([(5 * SS, 11 * SS), (9 * SS, 15 * SS), (17 * SS, 6 * SS)],
              fill=(255, 255, 255), width=int(2.4 * SS), joint="curve")
    _save(image, "badge_check.png")

    print("完成 ->", OUT)


if __name__ == "__main__":
    build()
