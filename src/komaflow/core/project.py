"""工程档模型与读写。

对应需求：FR-1.5（保存全部素材引用与参数，可反复打开修改）·
FR-1.8（明确不支持 DirectX ``.x`` 舞台）· K-6（渲前预检能查的部分）。

工程档是**纯数据**，序列化为 JSON（后缀 ``.kfproj``），
所以可以在没有 Blender 的机器上打开、检查、比对与写测试。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .segment import DEFAULT_PREROLL_FRAMES, DEFAULT_SEGMENT_LIMIT, MAX_PREROLL_FRAMES

#: 工程档格式版本。字段有破坏性变更时递增，并在 ``from_dict`` 里做迁移。
SCHEMA_VERSION: int = 1

#: 允许的输入路线（设计取舍：两种都要）。
ROUTES = ("assets", "blend")

#: 允许的画面方向（设计取舍：由用户自选）。
ORIENTATIONS = ("landscape", "portrait")

#: 自动机位的景别档位（v3.5 用户已选）。
FRAMINGS = ("full", "seven_tenths", "half")

#: 画质档（冻结范围：质量三档 + 进阶码率/CRF）。
QUALITIES = ("preview", "standard", "high")

#: 透明成片用哪种形式存（决策记录：主交付 MOV + MP4 同时保留）。
#: * ``prores`` —— ProRes 4444 带 alpha，体积小得多，后期软件最认
#: * ``qtrle`` —— QuickTime 无损 RLE，体积最大（2K 三分钟 20GB 起）
#: * ``png`` —— **就留原始 PNG 序列**，不再编 MOV。给要自己拿去后期合成的人
ALPHA_CODECS = ("prores", "qtrle", "png")

#: 透明那条轨的默认编码器。选 ProRes 是因为它只要 QTRLE 的 1/3 体积，
#: 肉眼看不出差别，而 QTRLE 在 2K 三分钟就能吃掉几十 GB。
DEFAULT_ALPHA_CODEC = "prores"

#: MP4 垫的底色 —— 深褐，取自角色设定的辅色。
#: 不用她的主色枫红：她穿的就是枫红，同色背景会把人吞掉。
@dataclass
class Inputs:
    """素材引用。所有路径都是字符串，便于原样写回 JSON。"""

    route: str = "assets"
    model: str | None = None # .pmx / .pmd 人物

    #: 动作 .vmd（骨骼动画）。**可以挂多个**。
    #:
    #: 为什么是列表：MMD 圈子很常见的发法是"上半身一个文件、下半身一个文件"、
    #: "基础动作 + 补帧"、"动作 + 表情分开" —— 固定只能放两个根本不够。
    #: 每个文件都走同一个 ``import_vmd``，mmd_tools 每次会**新建一个 action**，
    #: 所以后导入的会覆盖同名骨骼、没碰到的骨骼保持前一个文件的值
    #: （这正是"分层"想要的行为）。
    #:
    #: ⚠️ 兼容：老工程档里是 ``motion`` / ``expression`` 两个字符串字段，
    #: :func:`from_dict` 会把它们搬进这两个列表（见那里的说明）。
    motions: list[str] = field(default_factory=list)
    #: 表情 .vmd（morph 动画）。也是列表，理由同上。
    expressions: list[str] = field(default_factory=list)

    camera: str | None = None # 镜头 .vmd；None = 无镜头文件，走自动机位
    stage: str | None = None # 舞台：.pmx / .pmd 模型，或 .blend 工程
    pose: str | None = None # 姿势 .vpd
    audio: str | None = None # 1.0 冻结范围不含音频，保留字段供 1.5 用
    blend: str | None = None # 路线 B：已配好的 .blend 工程

    def __init__(self, *, route: str = "assets", model: str | None = None,
                 motions: list[str] | None = None,
                 expressions: list[str] | None = None,
                 camera: str | None = None, stage: str | None = None,
                 pose: str | None = None, audio: str | None = None,
                 blend: str | None = None,
                 # 老式单数参数：**只为准入**。老代码 / 老测试还在这么写，
                 # 直接删掉它们会让一大片调用点报 TypeError（不好排查，
                 # 因为报错在"构造参数"层，看不出是这里改了）。
                 motion: str | None = None,
                 expression: str | None = None) -> None:
        self.route = route
        self.model = model
        # 单数参数当"第一个元素"，列表参数优先（同时给时以列表为准）
        self.motions = list(motions or ())
        if not self.motions and motion:
            self.motions = [motion]
        self.expressions = list(expressions or ())
        if not self.expressions and expression:
            self.expressions = [expression]
        self.camera = camera
        self.stage = stage
        self.pose = pose
        self.audio = audio
        self.blend = blend

    # -------------------------------------------------------- 兼容属性
    @property
    def motion(self) -> str | None:
        """第一个动作文件（老代码 / 老工程档用）。"""
        return self.motions[0] if self.motions else None

    @motion.setter
    def motion(self, value: str | None) -> None:
        rest = self.motions[1:]
        self.motions = ([value] if value else []) + rest

    @property
    def expression(self) -> str | None:
        """第一个表情文件（老代码 / 老工程档用）。"""
        return self.expressions[0] if self.expressions else None

    @expression.setter
    def expression(self, value: str | None) -> None:
        rest = self.expressions[1:]
        self.expressions = ([value] if value else []) + rest

    def all_motions(self) -> list[str]:
        """动作 + 表情，按导入顺序排好（去重，保持先后）。"""
        seen: set[str] = set()
        out: list[str] = []
        for path in [*self.motions, *self.expressions]:
            if not path:
                continue
            key = str(path).replace("/", "\\").lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(path)
        return out


@dataclass
class Params:
    """渲染参数。默认值即 V1.0 冻结范围的默认值。"""

    preroll_frames: int = DEFAULT_PREROLL_FRAMES
    segment_limit: int | None = DEFAULT_SEGMENT_LIMIT
    frame_start: int = 0
    frame_end: int | None = None

    #: 物理**算不算**（FR-2.6 的"一键开关"）。
    #:
    #: 默认开 —— 关掉的话头发/裙摆/胸/饰品全部静止，那就"不像 MMD 了"。
    #: 关掉的唯一正当用途是：模型没物理数据却想省时间，或者物理算崩了要绕。
    #: ⚠️ 只对 PMX 那条路有效；用户自带的 ``.blend`` 不碰（设计取舍）。
    #: 观感来源：空 = MMD 原味；非空 = 从这个 .blend 迁移材质观感过来
    #: （通常就是「渲染模型」存出来的那份工程）。
    look_from_blend: str = ""

    # 冻结范围：分辨率固定 2K 2560×1440 @ 30fps（设计取舍）
    fps: int = 30
    width: int = 2560
    height: int = 1440
    orientation: str = "landscape"

    # 相机：只用 VMD 镜头原样不改；没有镜头文件时自动生成正面机位
    camera_mode: str = "vmd" # "vmd" | "auto"
    framing: str = "full"

    quality: str = "standard"
    crf: int | None = None
    bitrate: str | None = None

    strip_preroll: bool = False # K-1.5，默认不裁（暂按"保留 + 开关可选"）

    # ── 透明背景（决策记录：透明背景，用户逐条确认过）──
    #: 用户手动打开的开关。**能不能生效要现算** —— 放了舞台就失效（见 effective_*）。
    transparent_background: bool = False
    #: 用户**有没有亲手拨过**「渲染背景」。
    #:
    #: ⚠️ 少了这个记号就分不清"用户明确关掉了"和"还没轮到他选"：
    #: 默认值是 False，而"有角色没背景"时该按 True 算 ——
    #: 两种情况在 ``transparent_background`` 上长得一模一样。
    transparent_touched: bool = False
    #: 透明那条轨用什么编码器（``ALPHA_CODECS``）。
    alpha_codec: str = DEFAULT_ALPHA_CODEC


@dataclass
class Output:
    """输出位置与命名。"""

    dir: str = "output"
    basename: str = "KomaFlow"


@dataclass
class Project:
    """一份工程。"""

    name: str = "untitled"
    inputs: Inputs = field(default_factory=Inputs)
    params: Params = field(default_factory=Params)
    output: Output = field(default_factory=Output)
    blender_exe: str | None = None # 设计取舍：多版本时由用户自己选，工具绝不代选
    schema: int = SCHEMA_VERSION

    # ------------------------------------------------------------ 透明背景

    # ------------------------------------------------------------ 透明背景

    def has_character(self) -> bool:
        """这一路有没有"角色"。

        ⚠️ ``.blend`` 那条路**不算**：用户自带的工程我们连胶片设置都不碰
           （设计取舍），它自己怎么配就怎么渲。
        """
        if self.inputs.route == "blend" or self.inputs.blend:
            return False
        return bool(self.inputs.model)

    def has_background(self) -> bool:
        """这一路有没有"背景"（舞台）。"""
        if self.inputs.route == "blend" or self.inputs.blend:
            return False
        return bool(self.inputs.stage)

    def transparent_allowed(self) -> bool:
        """透明背景这一项**能不能选**。

        规则（用户钉死的四种状态）：

        ============ ============ ================================
        角色 背景 「渲染背景」这一项
        ============ ============ ================================
        有 无 默认关（= 出透明），**可选**（不变灰）
        无 有 默认开（照着背景渲），**可选**（不变灰）
        无 无 默认开，**变灰**（没东西可渲）
        有 有 默认开，**可选**（不变灰）
        ============ ============ ================================

        所以这里**只在"两样都没有"时才不可选** —— 这跟旧版（有舞台就灰掉）
        是反的，旧版等于逼着用户"想看透明就只能清空舞台"。
        ``.blend`` 那条路仍然一律不可选（不碰用户的工程）。
        """
        if self.inputs.route == "blend" or self.inputs.blend:
            return False
        return self.has_character() or self.has_background()

    def default_transparent(self) -> bool:
        """「渲染背景」的**默认值**（取反 = 出透明）。用户没手动拨过时按它算。

        * 有角色、没背景 → ``True``（自动出透明，这是最常用的用法）
        * 其余三种情况 → ``False``（照着背景渲；没背景时等于渲默认环境）
        """
        if self.inputs.route == "blend" or self.inputs.blend:
            return False
        return self.has_character() and not self.has_background()

    def effective_transparent(self) -> bool:
        """这次到底渲不渲透明背景。

        规则（设计取舍/4 + 用户 2026-10-17 的四种状态）：

        * **用户手动拨过** → 听用户的（只要这一项可选）
        * 没拨过 → 按 :meth:`default_transparent`

        ⚠️ 一条重要例外：**用户自带的 ``.blend`` 不替他改背景**（设计取舍）。
        那条路线走的是用户自己配好的工程，我们连胶片设置都不该碰。
        """
        if self.inputs.route == "blend" or self.inputs.blend:
            return False
        if not self.transparent_allowed():
            return False
        # ⚠️ 字段名是历史包袱：``transparent_background`` 存的是
        # **「渲染背景」这个开关**，不是"透明" —— 所以这里要**取反**读它。
        # （界面叫「渲染背景」：开 = 照背景渲 = 不出透明。）
        if self.params.transparent_background:
            return False
        if self.params.transparent_touched:
            return True
        return self.default_transparent()

    def alpha_codec_name(self) -> str:
        """取一个合法的编码器名（工程档里写了乱七八糟的值就退回默认）。"""
        if self.params.alpha_codec in ALPHA_CODECS:
            return self.params.alpha_codec
        return DEFAULT_ALPHA_CODEC

    # ---------------------------------------------------------------- 校验

    def validate(self) -> list[str]:
        """渲前体检能查出的**阻塞级**问题（K-6.1 / K-6.2）。

        返回人话问题清单；空列表 = 可以放行。调用方决定是阻断还是仅警告。

        注意：这里只做**不需要 Blender** 的静态检查（素材是否存在、
        参数是否自相矛盾、格式是否被明确排除）。显存/内存/版本等
        必须真机探测的项，由 ``core.pipeline`` 的 prepare 阶段补。
        """
        problems: list[str] = []

        if self.inputs.route not in ROUTES:
            problems.append(f"输入路线只能是 {'/'.join(ROUTES)}，当前是 {self.inputs.route!r}。")

        # FR-1.8：明确不支持 DirectX .x 舞台
        if self.inputs.stage and self.inputs.stage.lower().endswith(".x"):
            problems.append(
                "本工具不支持 DirectX .x 格式的舞台，请先把它转换成 .pmx 或 .pmd。"
            )

        if self.inputs.route == "blend":
            if not self.inputs.blend:
                problems.append("选了「已配好的 .blend」这条路线，但还没放工程文件。")
        else:
            if not self.inputs.model:
                problems.append("还没放角色模型 —— 把 .pmx / .pmd 拖到第 1 张卡上。")
            if not self.inputs.motions:
                problems.append("还没放动作 —— 把 .vmd 拖到第 2 张卡上。")

        # 素材存在性：只有路径写成了绝对路径才检查，相对路径留给上层按基目录解析。
        # 动作/表情是列表，逐个查（漏一个就会在 Blender 里报看不懂的错）。
        existence: list[tuple[str, str]] = [
            ("模型", self.inputs.model or ""),
            ("镜头", self.inputs.camera),
            ("舞台", self.inputs.stage),
            ("姿势", self.inputs.pose),
            ("音频", self.inputs.audio),
            (".blend 工程", self.inputs.blend),
        ]
        for index, path in enumerate(self.inputs.motions, 1):
            existence.append((f"动作 {index}", path))
        for index, path in enumerate(self.inputs.expressions, 1):
            existence.append((f"表情 {index}", path))

        for label, raw in existence:
            if raw and Path(raw).is_absolute() and not Path(raw).is_file():
                problems.append(f"{label}文件不存在：{raw}")

        p = self.params
        if not 0 <= p.preroll_frames <= MAX_PREROLL_FRAMES:
            problems.append(f"预热帧数需在 0 ~ {MAX_PREROLL_FRAMES} 之间，当前是 {p.preroll_frames}。")
        if p.segment_limit is not None and p.segment_limit <= 0:
            problems.append(f"单段上限必须是正整数或留空（不分段），当前是 {p.segment_limit}。")
        if p.frame_start < 0:
            problems.append(f"起始帧不能为负，当前是 {p.frame_start}。")
        if p.frame_end is not None and p.frame_end < p.frame_start:
            problems.append(f"结束帧（{p.frame_end}）小于起始帧（{p.frame_start}）。")
        if p.fps <= 0: problems.append(f"帧率必须为正，当前是 {p.fps}。")
        if p.width <= 0 or p.height <= 0:
            problems.append(f"分辨率必须为正，当前是 {p.width}×{p.height}。")
        if p.orientation not in ORIENTATIONS:
            problems.append(f"画面方向只能是 {'/'.join(ORIENTATIONS)}，当前是 {p.orientation!r}。")
        if p.camera_mode not in ("vmd", "auto"):
            problems.append(f"相机模式只能是 vmd/auto，当前是 {p.camera_mode!r}。")
        if p.framing not in FRAMINGS:
            problems.append(f"景别只能是 {'/'.join(FRAMINGS)}，当前是 {p.framing!r}。")
        if p.quality not in QUALITIES:
            problems.append(f"画质档只能是 {'/'.join(QUALITIES)}，当前是 {p.quality!r}。")
        if p.crf is not None and not 0 <= p.crf <= 51:
            problems.append(f"CRF 需在 0 ~ 51 之间，当前是 {p.crf}。")
        # ⚠️ 编码器只在**这一路真的会出透明成片时**才校验。
        #
        # 旧版无条件校验，于是"格式选 MP4（h264）+ 不放舞台"直接挡住渲染，
        # 报的还是"透明成片的格式只能是 prores/qtrle/png" ——
        # 用户截图里的就是这条。而 MP4 明明是格式下拉里**第一个**选项、
        # 也是 ``DEFAULT_ALPHA_CODEC``，等于默认值自己把自己拦住了。
        #
        # 丢进"透明成片"这个框里的校验，只对透明成片成立。
        if p.alpha_codec not in ALPHA_CODECS and self.effective_transparent():
            problems.append(
                f"透明成片的格式只能是 {'/'.join(ALPHA_CODECS)}，当前是 {p.alpha_codec!r}。"
                "（不想出透明就选 MP4，或者把「渲染背景」打开）")

        # 「渲染背景」关掉（= 出透明）与舞台互斥的旧限制**已取消**：
        # 现在两样都有时你可以自己选（默认照着背景渲）。见 transparent_allowed。

        return problems

    def scope_warnings(self) -> list[str]:
        """**超出 V1.0 冻结范围**的提醒（不阻断，只是别让用户误会）。

        依据需求书「★ V1.0 冻结范围」：只做中文、只出 MP4、
        2K 2560×1440@30 固定、不调渲染、不含音频。
        """
        warnings: list[str] = []
        p = self.params

        if (p.width, p.height) != (2560, 1440) and (p.width, p.height) != (1440, 2560):
            warnings.append(
                f"1.0 的分辨率固定为 2K 2560×1440（竖屏 1440×2560），当前是 {p.width}×{p.height}；"
                "自定义分辨率属后续版本。"
            )
        if p.fps != 30:
            warnings.append(f"1.0 的帧率固定为 30fps，当前是 {p.fps}；其他帧率属后续版本。")
        if self.inputs.audio:
            warnings.append("1.0 不含音频合流（已推到 1.5）；该字段会被忽略。")
        if not self.inputs.camera:
            warnings.append(
                f"没有镜头文件 —— 将使用自动正面机位（景别：{p.framing}）。"
                "这是冻结范围内的备用方案，不是错误。"
            )
        # route=blend 时 validate 不强制动作（有的用户就是只渲静止场景/镜头动画），
        # 但"以为挂了动作、结果渲出来不动"是最常见的误用反馈 —— 不拦，必须说透。
        is_blend_route = (self.inputs.route == "blend" or bool(self.inputs.blend))
        if is_blend_route and not self.inputs.all_motions():
            warnings.append(
                "没有放置动作文件（.vmd）—— 本次渲染的角色将保持打开时的姿势，"
                "不会动。若这是本意可忽略；想要角色动起来，请把动作 .vmd 拖进动作卡。"
            )
        return warnings

    # ------------------------------------------------------------ 序列化

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        """从字典构造。**缺字段用默认值**，便于旧工程档向前兼容。"""
        return cls(
            name=data.get("name", "untitled"),
            schema=int(data.get("schema", SCHEMA_VERSION)),
            blender_exe=data.get("blender_exe"),
            inputs=_build(Inputs, data.get("inputs")),
            params=_build(Params, data.get("params")),
            output=_build(Output, data.get("output")),
        )

    def save(self, path: str | Path) -> Path:
        """写入工程档。父目录不存在会自动创建。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        # C11：先写 .tmp 再替换，避免"写一半崩掉"留下坏工程档
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, target)
        return target

    @classmethod
    def load(cls, path: str | Path) -> "Project":
        raw = Path(path).read_text(encoding="utf-8")
        return cls.from_dict(json.loads(raw))


def _build(cls: type, data: dict[str, Any] | None):
    """只挑出 ``cls`` 声明过的字段来构造，忽略多余键。"""
    if not data:
        return cls()
    known = {f.name for f in fields(cls)}
    clean = dict(data)
    if cls is Inputs:
        clean = _migrate_motion_fields(clean)
    return cls(**{k: v for k, v in clean.items() if k in known})


def _migrate_motion_fields(data: dict[str, Any]) -> dict[str, Any]:
    """把老工程档的 ``motion`` / ``expression``（字符串）搬进新列表。

    ⚠️ 这一步**不能省**。``motion`` / ``expression`` 现在是 ``Inputs`` 上的
       **属性**（指向列表第一个元素），不再是 dataclass 字段 ——
       于是 ``_build`` 里那句"只挑声明过的字段"会把它们**直接丢掉**，
       打开旧工程就成了"素材全空"。这不报错，只是静默丢数据，
       比报错危险得多（用户会以为工程坏了）。

    规则：列表里没有值时才搬；老档同时有 ``motions`` 就用新的（新档优先）。
    """
    out = dict(data)
    for legacy, modern in (("motion", "motions"), ("expression", "expressions")):
        old = out.pop(legacy, None)
        if not old:
            continue
        existing = out.get(modern)
        if existing:
            continue
        out[modern] = [old] if isinstance(old, str) else list(old)
    return out
