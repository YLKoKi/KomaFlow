# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""通用 MMD 渲染器 —— 严格按 MMD 材质规格还原，不自己发明着色。

规格出处见 `../20_渲染规格.md`：
  ① 基础贴图 × 材质拡散色
  ② × Toon 色阶（竖直 1 维，由 N·L 决定采哪一行，**乘算**）
  ③ + 球面スフィア（黑底白点=**加算** / 景物=**乘算**）—— 模型没有就跳过
  ④ 描边エッジ（反向壳）

用法：
  blender --background --python MMD渲染.py -- <模型.pmx> <输出目录> [名称]

环境变量：LIGHT_DIR / EMIT / EDGE_T / RES / FRAME(full|half|face)

观感层（★ 默认开，解决「MMD 原样 = 平涂惨白」）：
  LOOK=anime|mmd anime=加明暗/边缘光/高光/AO/泛光/调色；mmd=严格还原（逐像素等于旧版）
  LOOK_STYLE=cel|soft ★ cel=赛璐璐分色（默认，二游"画出来的"味）/ soft=平滑压光（写实向）
  LOOK_STEPS=2 cel 色阶数：2=二分 3=三分
  BG=grad|gray 背景：竖直渐变 / 纯灰（默认随 LOOK 走）
  逐项：LOOK_SHADE LOOK_SHADOW_RGB LOOK_SHADOW_SAT LOOK_LIGHT_DIR LOOK_TERM
        LOOK_RIM LOOK_RIM_RGB LOOK_RIM_POW LOOK_SPEC LOOK_SPEC_T LOOK_GLOSS
        LOOK_AO LOOK_AO_DIST LOOK_SAT LOOK_CONTRAST LOOK_EXPOSURE
        LOOK_KNEE LOOK_KNEE_T LOOK_FACE LOOK_BLOOM LOOK_BLOOM_T
        LOOK_BLOOM_SIZE EDGE_FROM_PMX EDGE_GAIN
  （完整含义与实测数据见 ../20_渲染规格.md §九）
"""
import math
import os
import sys

import addon_utils
import bmesh
import bpy
import numpy as np
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
PMX = argv[0]
OUTDIR = argv[1]
NAME = argv[2] if len(argv) > 2 else os.path.splitext(os.path.basename(PMX))[0]
MODELDIR = os.path.dirname(PMX)

LIGHT_DIR = tuple(float(x) for x in os.environ.get("LIGHT_DIR", "-0.3,-0.9,0.3").split(","))
EMIT = float(os.environ.get("EMIT", "1.0"))
# 描边厚度：严格还原模式保持旧值 0.002（保证逐像素一致），观感层模式用更明显的 0.0032。
EDGE_T = float(os.environ.get(
    "EDGE_T", "0.002" if os.environ.get("LOOK", "anime").lower() == "mmd" else "0.0022"))
RES = os.environ.get("RES", "1080x1920")
FRAME = os.environ.get("FRAME", "full")
# 贴图加载失败的补救：按文件名去模型目录里找（PMX 里存的是作者机器的绝对路径）
RESCUE_TEX = os.environ.get("RESCUE_TEX", "1") == "1"
# 诊断开关：NO_EDGE=1 不建描边壳；PIPE=base 只上基础贴图（跳色阶/球面/贴图alpha）
NO_EDGE = os.environ.get("NO_EDGE", "0") == "1"
PIPE = os.environ.get("PIPE", "full")

def _f(key, dv):
    try:
        return float(os.environ.get(key, dv))
    except Exception:
        return float(dv)

def _rgb(key, dv, n=3):
    """取 RGB(A) 环境变量；dv 允许写 3 或 4 个分量，内部统一成 n+1 个。"""
    base = [float(x) for x in dv][:n]
    while len(base) < n:
        base.append(base[-1] if base else 1.0)
    raw = os.environ.get(key, "")
    if not raw:
        return tuple(base) + (1.0,)
    try:
        v = [float(x) for x in raw.split(",")][:n]
        while len(v) < n:
            v.append(v[-1] if v else 1.0)
        return tuple(v) + (1.0,)
    except Exception:
        return tuple(base) + (1.0,)

# ---------------------------------------------------------------- 观感层 LOOK
# 在 MMD 三层贴图之后叠加；LOOK=mmd 时整层归零，输出与旧版逐像素一致。
LOOK = os.environ.get("LOOK", "anime").lower()
IS_MMD = LOOK == "mmd"
# 风格：cel = 赛璐璐分色（二游/游戏立绘那种"画出来的"色块），soft = 平滑压光（写实向）
LOOK_STYLE = os.environ.get("LOOK_STYLE", "cel").lower()
LOOK_CEL = (LOOK_STYLE == "cel") and not IS_MMD
LOOK_STEPS = _f("LOOK_STEPS", "2") # 2=经典二分；3=三分（多一层中间调）
# 明暗来源：emission=自发光解析 N·L（无投影）；lit=EEVEE 真打灯（有投射阴影）。
LIGHT_MODE = os.environ.get("LIGHT_MODE", "emission").lower()
LIT_KEY = _f("LIT_KEY", "2.6") # 主光（太阳，投影来源）
LIT_FILL = _f("LIT_FILL", "1.7") # 补光（不投影）
LIT_RIM = _f("LIT_RIM", "1.1") # 轮廓光（不投影）
LIT_AMBIENT = _f("LIT_AMBIENT", "0.50") # 环境光强度（抬暗部）
# 要出形体，明暗光必须**擦边**（斜上方 45° 左右），一侧受光一侧落影。
LOOK_LIGHT = _rgb("LOOK_LIGHT_DIR", (-0.52, -0.50, 0.69, 1.0))
# 明暗：0 = 完全平（MMD 原样），1 = 全强度。阴影色是**线性空间**的乘数
# （Standard 视图变换会把线性→sRGB 提亮：线性 0.36 ≈ 显示 0.63）
LOOK_SHADE = 0.0 if IS_MMD else _f("LOOK_SHADE", "1.0") # 全局乘数（保持 1.0），真正的强度逐材质给
LOOK_SHADOW_RGB = _rgb("LOOK_SHADOW_RGB", (0.80, 0.78, 0.86, 1.0)) # 兜底
# 参考里兜帽/紧身衣的背光面近乎炭色（暗部≈0.25），而皮肤暗部只比亮部暗 ~14%。
LOOK_SHADOW_CLOTH = _rgb("LOOK_SHADOW_CLOTH", (0.27, 0.29, 0.53, 1.0))
LOOK_SHADOW_SKIN = _rgb("LOOK_SHADOW_SKIN", (0.84, 0.82, 0.90, 1.0))
LOOK_SHADOW_SAT = _f("LOOK_SHADOW_SAT", "1.12") # 暗部彩度补偿（乘法会掉彩度）
LOOK_RIM = 0.0 if IS_MMD else _f("LOOK_RIM", "0.10") # 边缘光：二游质感的第一权重
LOOK_RIM_RGB = _rgb("LOOK_RIM_RGB", (1.0, 0.95, 0.85, 1.0))
LOOK_RIM_DIR = _rgb("LOOK_RIM_DIR", (0.62, -0.30, 0.72, 1.0)) # 必须朝相机一侧，否则边缘光全打在背面被剔除
LOOK_RIM_POW = _f("LOOK_RIM_POW", "1.6") # 越小边缘光越宽（2.5 只剩 1px 细线）
LOOK_SPEC = 0.0 if IS_MMD else _f("LOOK_SPEC", "0.04") # 高光
LOOK_GLOSS = _f("LOOK_GLOSS", "26") # 高光锐度
LOOK_SPEC_T = _f("LOOK_SPEC_T", "0.45") # 高光切块阈值（cel 硬高光）
LOOK_AO = 0.0 if IS_MMD else _f("LOOK_AO", "0.12") # 接触阴影（cel 风格要克制，否则会变"3D 灰"）
LOOK_AO_DIST = _f("LOOK_AO_DIST", "0.20")
LOOK_SAT = 1.0 if IS_MMD else _f("LOOK_SAT", "0.90") # 饱和度
LOOK_WB = (1.0, 1.0, 1.0, 1.0) if IS_MMD else _rgb("LOOK_WB", (1.035, 1.005, 0.972, 1.0)) # 白平衡偏移（暖亮冷暗里的"暖"）
LOOK_SUB = 0.0 if IS_MMD else _f("LOOK_SUB", "0.10") # 次表面（皮肤透光的"活人感"）
LOOK_SUB_RGB = _rgb("LOOK_SUB_RGB", (1.0, 0.42, 0.34, 1.0)) # 透光颜色（暖红）
LOOK_SKIN_RGB = (1.0, 1.0, 1.0, 1.0) if IS_MMD else _rgb(
    "LOOK_SKIN_RGB", (1.13, 1.02, 0.975, 1.0))
# 眼窝/虹膜属中暗调，会被任何"压暗部"的曲线吃掉，所以单独提权最直接。
LOOK_EYE_RGB = (1.0, 1.0, 1.0, 1.0) if IS_MMD else _rgb("LOOK_EYE_RGB", (0.99, 1.03, 1.10, 1.0))

# ---- 替换色阶贴图（手绘 LUT）------------------------------------------------
# 用竖向 ramp 替换材质里的 toon：模型自带色阶近乎纯白，暗部压不下去。
LOOK_LUT = 0.0 if IS_MMD else _f("LOOK_LUT", "0") # 0=用模型自带色阶；1=用手绘 LUT
LOOK_LUT_LIGHT = _rgb("LOOK_LUT_LIGHT", (1.00, 1.00, 1.00, 1.0))
LOOK_LUT_SHADOW = _rgb("LOOK_LUT_SHADOW", (0.52, 0.52, 0.68, 1.0))
LOOK_LUT_STEP = _f("LOOK_LUT_STEP", "0.46") # 明暗分界位置（0~1，越小亮面越大）
LOOK_LUT_SOFT = _f("LOOK_LUT_SOFT", "0.06") # 分界宽度（越小越硬，cel 味越重）
# ★ 姿态无关光照（默认开，不做界面开关）
#   把 toon 色阶的采样位置 v 从"N·L"改成常数 ⇒ 人物明暗不再随骨骼姿态/朝向变化。
#   · LOOK=anime（默认）：启用
#   · LOOK=mmd（严格还原）：**强制关闭**，保持逐像素等于旧版
#   · LOOK_POSE_LOCK=0：内部回退到"跟着姿态变"（不写进用户文档）
LOOK_POSE_LOCK = (os.environ.get("LOOK_POSE_LOCK", "1") != "0") and not IS_MMD
LOOK_FLAT_NL = _f("LOOK_FLAT_NL", "0.55")      # 固定的 N·L 值（0~1）
_LUT_IMG = None

def _lut_image():
    """生成/复用竖向色阶 LUT。V=0 是暗部，V=1 是受光端。"""
    global _LUT_IMG
    if _LUT_IMG is not None:
        return _LUT_IMG
    h = 128
    img = bpy.data.images.new("_look_lut", width=8, height=h, alpha=False, float_buffer=True)
    try:
        img.colorspace_settings.name = "Non-Color" # 数值直通，避免再被 sRGB 拐一次
    except Exception:
        pass
    px = np.zeros((h, 8, 4), dtype=np.float32)
    lo = np.array(LOOK_LUT_SHADOW[:3], dtype=np.float32)
    hi = np.array(LOOK_LUT_LIGHT[:3], dtype=np.float32)
    for y in range(h):
        v = y / (h - 1.0)
        t = (v - LOOK_LUT_STEP) / max(1e-4, LOOK_LUT_SOFT)
        t = 1.0 / (1.0 + np.exp(-float(np.clip(t, -20, 20)))) # sigmoid 平滑阶跃
        px[y, :, :3] = lo + (hi - lo) * t
        px[y, :, 3] = 1.0
    img.pixels.foreach_set(px.reshape(-1))
    _LUT_IMG = img
    return img
LOOK_CONTRAST = 0.0 if IS_MMD else _f("LOOK_CONTRAST", "0.04") # 对比（绕线性中灰 0.18 收紧）
LOOK_GAMMA = 1.0 if IS_MMD else _f("LOOK_GAMMA", "1.12") # 中等力度；过大会把虹膜这类中暗调压死（眼睛发凶）
LOOK_KNEE = 0.0 if IS_MMD else _f("LOOK_KNEE", "0.12") # 高光软肩（防糊成死白）
LOOK_KNEE_T = _f("LOOK_KNEE_T", "0.95") # 软肩起点：模型皮肤多在 0.9+，
                                                                      # 卡在 1.0 就永远是死白，压到 0.72 才露出皮肤层次
LOOK_EXPOSURE = 1.0 if IS_MMD else _f("LOOK_EXPOSURE", "0.98") # 材质整体曝光（只压角色，不压背景）
LOOK_TERM = _f("LOOK_TERM", "0.50") # 明暗交界宽度（越小越硬=越二次元）
LOOK_RING = 0.0 if IS_MMD else _f("LOOK_RING", "0.55") # 头发天使环高光带
LOOK_FACE = 0.0 if IS_MMD else _f("LOOK_FACE", "1.0") # 脸部微调力度（0=全部按默认）
# 后期泛光：只该吃金属/宝石/眼睛这类小面积高光；阈值低了会把皮肤整片"糊成雾"
LOOK_BLOOM = 0.0 if IS_MMD else _f("LOOK_BLOOM", "0.10") # 泛光=柔化，是"糊"的主要来源，宁少勿多
LOOK_BLOOM_T = _f("LOOK_BLOOM_T", "0.95") # 泛光阈值（线性空间）
LOOK_BLOOM_SIZE = _f("LOOK_BLOOM_SIZE", "5")
BG = os.environ.get("BG", "gray" if IS_MMD else "none")
BG_TOP = _rgb("BG_TOP", (0.22, 0.22, 0.24, 1.0))
BG_BOT = _rgb("BG_BOT", (0.07, 0.07, 0.09, 1.0))
EDGE_FROM_PMX = os.environ.get("EDGE_FROM_PMX", "1" if not IS_MMD else "0") == "1"
# 直接乘增益仍是一条"红划痕"。正确做法是把它当**色相提示**，主体混向深墨色。
EDGE_GAIN = 0.22 if IS_MMD else _f("EDGE_GAIN", "0.30")
EDGE_INK = 0.0 if IS_MMD else _f("EDGE_INK", "0.62")
EDGE_INK_RGB = _rgb("EDGE_INK_RGB", (0.06, 0.055, 0.075, 1.0)) # 逐材质 edge_color 的增益：黑描边乘任何数仍是黑，
                                     # 只把 (0.502,0,0) 这类彩线压成近黑的暗酒红。

os.makedirs(OUTDIR, exist_ok=True)

def log(*a):
    print("[MMD渲染]", *a, flush=True)

# ---------------------------------------------------------------- 机器可读进度 / 结果
# 这是给「把这个工具嵌进别的项目」用的稳定接口：外部只需读这个文件，不用解析终端输出。
_PROG_PATH = os.environ.get("PROGRESS_JSON", "")
_PROG = {"script": "MMD渲染", "pmx": PMX, "outdir": OUTDIR, "name": NAME,
         "stage": "init", "progress": 0.0, "message": "", "outputs": [],
         "stats": {}, "error": None}

def prog(stage=None, progress=None, message=None, **kw):
    if stage:
        _PROG["stage"] = stage
    if progress is not None:
        _PROG["progress"] = round(float(progress), 4)
    if message:
        _PROG["message"] = message
        log(message)
    if kw:
        _PROG.update(kw)
    if _PROG_PATH:
        try:
            with open(_PROG_PATH, "w", encoding="utf-8") as _fh:
                import json as _json
                _json.dump(_PROG, _fh, ensure_ascii=False, indent=2)
        except Exception:
            pass

import atexit as _atexit

def _on_exit():
    if _PROG_PATH and _PROG.get("stage") not in ("done", "error"):
        _PROG["stage"] = "error"
        _PROG["error"] = _PROG.get("error") or "进程提前退出（未走到 done）"
        prog()

_atexit.register(_on_exit)

def _excepthook(t, v, tb):
    import traceback as _tb
    _PROG["stage"] = "error"
    _PROG["error"] = "%s: %s" % (t.__name__, v)
    _PROG["traceback"] = "".join(_tb.format_exception(t, v, tb))[-4000:]
    prog()
    sys.__excepthook__(t, v, tb)

sys.excepthook = _excepthook
prog("init", 0.0, "开始：%s" % os.path.basename(PMX))

# ---------------------------------------------------------------- 清场 + 导入
addon_utils.enable("mmd_tools", default_set=True, persistent=True)
for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)

# ★ 物理默认【导入并原封不动保留】：刚体 + 关节 + 绑定关系全部留在场景里。
# ⚠ 只导入，不做 build_rig、不启用物理世界、不烘焙 —— 一切保持 MMD Tools 导入后的原样。
# 要关掉物理（产物里不留刚体）请设 NO_PHYSICS=1。
_TYPES = {"MESH", "ARMATURE", "DISPLAY", "MORPHS"}
if os.environ.get("NO_PHYSICS") != "1":
    _TYPES.add("PHYSICS")
bpy.ops.mmd_tools.import_model(filepath=PMX, scale=0.08,
                               types=_TYPES,
                               rename_bones=True, use_mipmap=False, clean_model=True)

# ---- 补救加载失败的贴图 -------------------------------------------------
if RESCUE_TEX:
    # 递归建「文件名 -> 路径」索引：模型常把贴图放在 tex/ textures/ spa/ toon/ 等子目录里
    _pool = {}
    _root = os.path.dirname(PMX)
    for _r, _d, _fs in os.walk(_root):
        for _fname in _fs:
            _pool.setdefault(_fname, os.path.join(_r, _fname))
    fixed = 0
    for img in bpy.data.images:
        try:
            bad = (img.size[0] == 0) or (not img.filepath) or (not os.path.exists(bpy.path.abspath(img.filepath)))
        except Exception:
            bad = True
        if not bad:
            continue
        cand = _pool.get(img.name) or os.path.join(MODELDIR, img.name)
        if os.path.exists(cand):
            img.filepath = cand
            img.reload()
            fixed += 1
            log(" 贴图补回:", img.name, "->", "OK" if img.size[0] else "仍失败")
    log("贴图补救完成，补回", fixed, "张")
prog("import", 0.15, "导入完成，贴图补救 %d 张" % fixed)

_SHRINK = int(os.environ.get("SHRINK", "0"))
if _SHRINK:
    for _img in bpy.data.images:
        try:
            _mx = max(_img.size)
            if _mx > _SHRINK:
                _k = _SHRINK / float(_mx)
                _img.scale(int(_img.size[0] * _k), int(_img.size[1] * _k))
                log(" 降采样:", _img.name, _img.size)
        except Exception as _e:
            log(" 降采样失败:", _img.name, _e)

def _pick_mesh(objects):
    """挑出代表模型：**任何网格都算合法**（材质槽数量与模型合法性无关）。

    ⚠️ 以前过滤 ``len(o.material_slots) > 4``（审查 R15）：单材质角色 / 低模
    是有效 PMX，却被过滤成空列表 → ``max()`` 直接 ValueError。现在只要求
    至少有一个 MESH；多个网格仍按顶点数取最大（单角色约定，见说明文档）。
    """
    meshes = [o for o in objects if getattr(o, "type", "") == "MESH"]
    if not meshes:
        raise RuntimeError("场景里没有任何网格（MESH）模型 —— 无法做静帧渲染。")
    return max(meshes, key=lambda o: len(getattr(o.data, "vertices", [])))


mesh = _pick_mesh(bpy.data.objects)
log("模型:", mesh.name, "顶点", len(mesh.data.vertices), "材质槽", len(mesh.material_slots))

def tex_of(mat, key):
    if not mat or not mat.node_tree:
        return None
    for n in mat.node_tree.nodes:
        if n.bl_idname == "ShaderNodeTexImage" and key in n.name and getattr(n, "image", None):
            return n.image
    return None

# ---------------------------------------------------------------- 建共享节点组
GN = "MMD材质"
if GN in bpy.data.node_groups:
    bpy.data.node_groups.remove(bpy.data.node_groups[GN])
ng = bpy.data.node_groups.new(GN, "ShaderNodeTree")
it = ng.interface
it.new_socket("着色器", in_out="OUTPUT", socket_type="NodeSocketShader")
it.new_socket("基础色", in_out="INPUT", socket_type="NodeSocketColor")
it.new_socket("色阶", in_out="INPUT", socket_type="NodeSocketColor")
it.new_socket("拡散色", in_out="INPUT", socket_type="NodeSocketColor")
it.new_socket("Alpha", in_out="INPUT", socket_type="NodeSocketFloat")
it.new_socket("基础Alpha", in_out="INPUT", socket_type="NodeSocketFloat")
it.new_socket("球面", in_out="INPUT", socket_type="NodeSocketColor")
it.new_socket("球面模式", in_out="INPUT", socket_type="NodeSocketFloat") # 0=关 1=乘算 2=加算
it.new_socket("自发光", in_out="INPUT", socket_type="NodeSocketFloat")

def _in(name, kind, default):
    s = it.new_socket(name, in_out="INPUT", socket_type=kind)
    try:
        s.default_value = default
    except Exception:
        pass
    return s

# 观感层（LOOK）的可调输入。全部中性值时 = 纯 MMD 原样。
_in("明暗强度", "NodeSocketFloat", LOOK_SHADE)
_in("外部明暗", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0))
_in("用外部明暗", "NodeSocketFloat", 0.0)
_in("明暗量化", "NodeSocketFloat", 1.0) # 0=平滑过渡 1=硬切色阶（cel）。脸用 0.25 保留柔和的形体
_in("天使环", "NodeSocketFloat", 0.0) # 头发高光带（髪 材质开）
_in("次表面", "NodeSocketFloat", 0.0) # 皮肤透光强度（活人感）
_in("次表面色", "NodeSocketColor", LOOK_SUB_RGB)
_in("白平衡", "NodeSocketColor", LOOK_WB)
_in("阴影彩度", "NodeSocketFloat", LOOK_SHADOW_SAT)
_in("明暗光向", "NodeSocketVector", tuple(LOOK_LIGHT[:3]))
_in("交界硬度", "NodeSocketFloat", LOOK_TERM)
_in("阴影色", "NodeSocketColor", LOOK_SHADOW_RGB)
_in("边缘光", "NodeSocketFloat", LOOK_RIM)
_in("边缘光色", "NodeSocketColor", LOOK_RIM_RGB)
_in("高光", "NodeSocketFloat", LOOK_SPEC)
_in("高光锐度", "NodeSocketFloat", LOOK_GLOSS)
_in("AO强度", "NodeSocketFloat", LOOK_AO)
_in("AO距离", "NodeSocketFloat", LOOK_AO_DIST)
_in("饱和度", "NodeSocketFloat", LOOK_SAT)
_in("对比", "NodeSocketFloat", LOOK_CONTRAST)
_in("软肩", "NodeSocketFloat", LOOK_KNEE)

N, L = ng.nodes, ng.links.new
gi, go = N.new("NodeGroupInput"), N.new("NodeGroupOutput")

mul_d = N.new("ShaderNodeMix"); mul_d.data_type = "RGBA"; mul_d.blend_type = "MULTIPLY"
mul_d.name = "基础×拡散"; mul_d.inputs[0].default_value = 1.0
L(gi.outputs["基础色"], mul_d.inputs[6]); L(gi.outputs["拡散色"], mul_d.inputs[7])

mul_t = N.new("ShaderNodeMix"); mul_t.data_type = "RGBA"; mul_t.blend_type = "MULTIPLY"
mul_t.name = "×色阶"; mul_t.inputs[0].default_value = 1.0
L(mul_d.outputs[2], mul_t.inputs[6]); L(gi.outputs["色阶"], mul_t.inputs[7])

smul = N.new("ShaderNodeMix"); smul.data_type = "RGBA"; smul.blend_type = "MULTIPLY"
smul.name = "球面乘算"; smul.inputs[0].default_value = 1.0
L(mul_t.outputs[2], smul.inputs[6]); L(gi.outputs["球面"], smul.inputs[7])
sadd = N.new("ShaderNodeMix"); sadd.data_type = "RGBA"; sadd.blend_type = "ADD"
sadd.name = "球面加算"; sadd.inputs[0].default_value = 1.0
L(sadd.inputs[6], mul_t.outputs[2]); L(gi.outputs["球面"], sadd.inputs[7])
t1 = N.new("ShaderNodeMath"); t1.operation = "GREATER_THAN"; t1.inputs[1].default_value = 1.5
L(gi.outputs["球面模式"], t1.inputs[0]) # >1.5 ⇒ 模式2 加算
t2 = N.new("ShaderNodeMath"); t2.operation = "GREATER_THAN"; t2.inputs[1].default_value = 0.5
L(gi.outputs["球面模式"], t2.inputs[0]) # >0.5 ⇒ 有球面（1 或 2）
m1 = N.new("ShaderNodeMix"); m1.data_type = "RGBA"; m1.blend_type = "MIX"; m1.name = "球面乘算择"
L(t2.outputs[0], m1.inputs[0])
L(mul_t.outputs[2], m1.inputs[6]); L(smul.outputs[2], m1.inputs[7])
m2 = N.new("ShaderNodeMix"); m2.data_type = "RGBA"; m2.blend_type = "MIX"; m2.name = "球面加算择"
L(t1.outputs[0], m2.inputs[0])
L(m1.outputs[2], m2.inputs[6]); L(sadd.outputs[2], m2.inputs[7])

em = N.new("ShaderNodeEmission"); em.name = "自发光"
# ============================================================ 观感层 LOOK
# 观感层：接在「基础×拡散×色阶×球面」之后，不改变 MMD 的材料构成，
# 只补明暗 / 边缘光 / 高光 / 接触阴影 / 饱和度；强度全为 0 时逐像素等于旧结果。
def _vm(op, a=None, b=None, scale=None, name=""):
    v = N.new("ShaderNodeVectorMath"); v.operation = op; v.name = name or op
    if a is not None:
        _src(v.inputs[0], a)
    if b is not None:
        _src(v.inputs[1], b)
    if scale is not None:
        _src(v.inputs["Scale"], scale)
    return v

def _math(op, a=None, b=None, name=""):
    m = N.new("ShaderNodeMath"); m.operation = op; m.name = name or op
    if a is not None:
        _src(m.inputs[0], a)
    if b is not None:
        _src(m.inputs[1], b)
    return m

def _src(sock, val):
    """给输入插槽接线：val 是 socket 就 link，是数值/元组就写 default。"""
    if hasattr(val, "is_output") or hasattr(val, "links"):
        try:
            L(val, sock)
            return
        except Exception:
            pass
    try:
        sock.default_value = val
    except Exception:
        pass

def _mixrgb(fac, a, b, blend="MIX", name=""):
    m = N.new("ShaderNodeMix"); m.data_type = "RGBA"; m.blend_type = blend
    m.name = name or "mixrgb"
    _src(m.inputs[0], fac)
    _src(m.inputs[6], a)
    _src(m.inputs[7], b)
    return m

WHITE = N.new("ShaderNodeCombineXYZ")
for _i in range(3):
    WHITE.inputs[_i].default_value = 1.0
WHITE.name = "白"

_g2 = N.new("ShaderNodeNewGeometry"); _g2.name = "几何2"
_L = N.new("ShaderNodeCombineXYZ"); _L.name = "光向"
for _i in range(3):
    _L.inputs[_i].default_value = LIGHT_DIR[_i]

# ---- ① 明暗：赛璐璐分色（cel）还是平滑渐变（soft）----
_dNL = _vm("DOT_PRODUCT", _g2.outputs["Normal"], gi.outputs["明暗光向"], name="N点L")

def _ramp(name, interp):
    """同一窗口下的一条明暗斜坡（STEPPED / SMOOTHSTEP 各建一条，再按 量化 混合）。"""
    m = N.new("ShaderNodeMapRange"); m.name = name
    m.data_type = "FLOAT"; m.clamp = True
    m.interpolation_type = interp
    L(_dNL.outputs["Value"], m.inputs["Value"])
    m.inputs["To Min"].default_value = 0.0
    m.inputs["To Max"].default_value = 1.0
    if interp == "STEPPED":
        try:
            m.inputs["Steps"].default_value = max(2, int(LOOK_STEPS)) # 2=经典二分 3=三分
        except Exception:
            pass
    return m

_t = _ramp("明暗t", "STEPPED")
_ts = _ramp("明暗t软", "SMOOTHSTEP")
# 交界位置固定 0.05，宽度 = 交界硬度
_half_w = _math("MULTIPLY", gi.outputs["交界硬度"], 0.5, name="半宽")
_fmin = _math("SUBTRACT", 0.05, _half_w.outputs[0], name="FromMin")
_fmax = _math("ADD", 0.05, _half_w.outputs[0], name="FromMax")
for _r in (_t, _ts):
    L(_fmin.outputs[0], _r.inputs["From Min"])
    L(_fmax.outputs[0], _r.inputs["From Max"])
_tq = _mixrgb(gi.outputs["明暗量化"], _ts.outputs["Result"], _t.outputs["Result"], name="量化混合")

# 曝光：只作用于材质（世界背景是独立的世界着色器），给高光留出压缩余量
_lit = _vm("SCALE", m2.outputs[2], scale=LOOK_EXPOSURE, name="材质曝光")
# 暗部乘数 = 阴影色（带色相偏移），再把彩度提回来（纯乘法会掉彩度）
_tint_luma = _vm("DOT_PRODUCT", gi.outputs["阴影色"], (0.299, 0.587, 0.114), name="阴影色亮度")
_tint_lumac = _vm("SCALE", WHITE.outputs[0], scale=_tint_luma.outputs["Value"], name="阴影色灰")
_tint = _mixrgb(gi.outputs["阴影彩度"], _tint_lumac.outputs["Vector"], gi.outputs["阴影色"], name="阴影提彩")
_mul_ana = _mixrgb(_tq.outputs[2], _tint.outputs[2], WHITE.outputs[0], name="解析明暗乘数")
# lit 模式：明暗乘数来自真实光照的色阶（含投射阴影）
_mul = _mixrgb(gi.outputs["用外部明暗"], _mul_ana.outputs[2], gi.outputs["外部明暗"], name="明暗来源")
_shaded = _mixrgb(1.0, _lit.outputs["Vector"], _mul.outputs[2], blend="MULTIPLY", name="上明暗")
_c1 = _mixrgb(gi.outputs["明暗强度"], _lit.outputs["Vector"], _shaded.outputs[2], name="明暗力度")

# ---- ② 边缘光：fresnel × (N·RimDir) ----
_dNV = _vm("DOT_PRODUCT", _g2.outputs["Normal"], _g2.outputs["Incoming"], name="N点V")
_aNV = _math("ABSOLUTE", _dNV.outputs["Value"], name="|N点V|")
_one = _math("SUBTRACT", 1.0, _aNV.outputs[0], name="1-|N点V|")
_fres = _math("POWER", _one.outputs[0], LOOK_RIM_POW, name="fresnel")
_R = N.new("ShaderNodeCombineXYZ"); _R.name = "边缘光向"
for _i in range(3):
    _R.inputs[_i].default_value = [x / (sum(y * y for y in LOOK_RIM_DIR[:3]) ** 0.5 or 1.0)
                                   for x in LOOK_RIM_DIR[:3]][_i]
_dNR = _vm("DOT_PRODUCT", _g2.outputs["Normal"], _R.outputs[0], name="N点R")
_rmask = N.new("ShaderNodeMapRange"); _rmask.name = "边缘光范围"
_rmask.data_type = "FLOAT"; _rmask.interpolation_type = "SMOOTHSTEP"; _rmask.clamp = True
_src(_rmask.inputs["Value"], _dNR.outputs["Value"])
_rmask.inputs["From Min"].default_value = -0.15
_rmask.inputs["From Max"].default_value = 0.55
_rv = _math("MULTIPLY", _fres.outputs[0], _rmask.outputs["Result"], name="边缘光x范围")
# 赛璐璐：把边缘光也切成一条**硬带**（0/1），而不是柔和的菲涅尔衰减
if LOOK_CEL:
    _rvq = N.new("ShaderNodeMapRange"); _rvq.name = "边缘光硬化"
    _rvq.data_type = "FLOAT"; _rvq.interpolation_type = "STEPPED"; _rvq.clamp = True
    _rvq.inputs["Steps"].default_value = 2
    _rvq.inputs["From Min"].default_value = 0.0
    _rvq.inputs["From Max"].default_value = 1.0
    _rvq.inputs["To Min"].default_value = 0.0
    _rvq.inputs["To Max"].default_value = 1.0
    L(_rv.outputs[0], _rvq.inputs["Value"])
    _rvsrc = _rvq.outputs["Result"]
else:
    _rvsrc = _rv.outputs[0]
_rv2 = _math("MULTIPLY", _rvsrc, gi.outputs["边缘光"], name="边缘光x强度")
_rimcol = _vm("SCALE", gi.outputs["边缘光色"], scale=_rv2.outputs[0], name="边缘光色x")
_c2 = _vm("ADD", _c1.outputs[2], _rimcol.outputs["Vector"], name="加边缘光")

# ---- ②.5 次表面透光：「活人感」的核心。皮肤在薄处/掠射角会透出暖红。
_nvpos0 = _math("MULTIPLY", _dNV.outputs["Value"], -1.0, name="透光N点V取正")
_nvc0 = _math("MAXIMUM", _nvpos0.outputs[0], 0.0, name="透光maxN点V")
_thin = _math("POWER", _nvc0.outputs[0], 2.0, name="薄处")
_litmask = N.new("ShaderNodeMapRange"); _litmask.name = "受光范围"
_litmask.data_type = "FLOAT"; _litmask.interpolation_type = "SMOOTHSTEP"; _litmask.clamp = True
L(_dNL.outputs["Value"], _litmask.inputs["Value"])
_litmask.inputs["From Min"].default_value = -0.20
_litmask.inputs["From Max"].default_value = 0.60
_subv = _math("MULTIPLY", _thin.outputs[0], _litmask.outputs["Result"], name="透光x受光")
_subv2 = _math("MULTIPLY", _subv.outputs[0], gi.outputs["次表面"], name="透光x强度")
_subcol = _vm("SCALE", gi.outputs["次表面色"], scale=_subv2.outputs[0], name="透光色")
_c2b = _vm("ADD", _c2.outputs["Vector"], _subcol.outputs["Vector"], name="加透光")

# ---- ③ 高光：blinn-phong（PMX 的 specular 普遍为 0，实测示例模型 33 材质全 0 ⇒ 只能程序化补）----
_invV = _vm("SCALE", _g2.outputs["Incoming"], scale=-1.0, name="视向")
_hv = _vm("ADD", _L.outputs[0], _invV.outputs["Vector"], name="L+V")
_hvn = _vm("NORMALIZE", _hv.outputs["Vector"], name="归一")
_dNH = _vm("DOT_PRODUCT", _g2.outputs["Normal"], _hvn.outputs["Vector"], name="N点H")
_nh = _math("MAXIMUM", _dNH.outputs["Value"], 0.0, name="maxN点H")
_sp = _math("POWER", _nh.outputs[0], gi.outputs["高光锐度"], name="高光pow")
# 赛璐璐：高光要的是**一块硬高光**（阈值切），不是一圈柔光
if LOOK_CEL:
    _spq = _math("GREATER_THAN", _sp.outputs[0], LOOK_SPEC_T, name="高光切块")
    _spsrc = _spq.outputs[0]
else:
    _spsrc = _sp.outputs[0]
_sp2 = _math("MULTIPLY", _spsrc, gi.outputs["高光"], name="高光x强度")
_spc = _vm("SCALE", WHITE.outputs[0], scale=_sp2.outputs[0], name="高光色")
_c3 = _vm("ADD", _c2b.outputs["Vector"], _spc.outputs["Vector"], name="加高光")

# ---- ③.5 头发「天使环」：朝相机 + 朝上的表面亮一条，这是二游头发的标志性高光
# （PMX 没有各向异性参数，用「视角朝向 × 朝上程度」近似出那条带）
_nvpos = _math("MULTIPLY", _dNV.outputs["Value"], -1.0, name="N点V取正")
_nvc = _math("MAXIMUM", _nvpos.outputs[0], 0.0, name="maxN点V")
_sepN = N.new("ShaderNodeSeparateXYZ"); _sepN.name = "法线分量"
L(_g2.outputs["Normal"], _sepN.inputs[0])
_nup = _math("MAXIMUM", _sepN.outputs["Z"], 0.0, name="maxNz")
_r1 = _math("POWER", _nvc.outputs[0], 2.0, name="环1")
_r2 = _math("POWER", _nup.outputs[0], 2.0, name="环2")
_r12 = _math("MULTIPLY", _r1.outputs[0], _r2.outputs[0], name="环x")
_rst = _math("MULTIPLY", _r12.outputs[0], gi.outputs["天使环"], name="环强度")
_ringcol = _vm("SCALE", gi.outputs["边缘光色"], scale=_rst.outputs[0], name="环色")
_c3b = _vm("ADD", _c3.outputs["Vector"], _ringcol.outputs["Vector"], name="加天使环")

# ---- ④ 接触阴影：AO 节点（EEVEE 的 GTAO）----
_ao = N.new("ShaderNodeAmbientOcclusion"); _ao.name = "AO"
_src(_ao.inputs["Distance"], gi.outputs["AO距离"])
_aom = _mixrgb(gi.outputs["AO强度"], WHITE.outputs[0], _ao.outputs["Color"], name="AO力度")
_c4 = _mixrgb(1.0, _c3b.outputs["Vector"], _aom.outputs[2], blend="MULTIPLY", name="上AO")

# ---- ⑤ 饱和度 ----
_luma = _vm("DOT_PRODUCT", _c4.outputs[2],
            (0.299, 0.587, 0.114), name="亮度")
_lumac = _vm("SCALE", WHITE.outputs[0], scale=_luma.outputs["Value"], name="亮度灰")
_sat = _mixrgb(gi.outputs["饱和度"], _lumac.outputs["Vector"], _c4.outputs[2], name="饱和度")

# ---- ⑤.5 对比：绕线性中灰 0.18 收紧，拉开亮暗层次（平涂发"糊"的主因之一）----
_piv = _vm("SUBTRACT", _sat.outputs[2], (0.18, 0.18, 0.18), name="减中灰")
_pivk = _math("ADD", gi.outputs["对比"], 1.0, name="对比系数")
_piv2 = _vm("SCALE", _piv.outputs["Vector"], scale=_pivk.outputs[0], name="对比缩放")
_ctr = _vm("ADD", _piv2.outputs["Vector"], (0.18, 0.18, 0.18), name="加回中灰")
# ---- ⑤.7 白平衡：整体往暖挪一点（冷灰 → 暖艳，是"二游"和"工业渲染"的观感差）
_wb0 = _mixrgb(1.0, _ctr.outputs["Vector"], gi.outputs["白平衡"], blend="MULTIPLY", name="白平衡")
# ---- ⑤.8 黑位伽马：>1 把暗部压得更黑、亮部几乎不动（“自然明亮但黑得下去”靠这一条）
_gam = _vm("POWER", _wb0.outputs[2], (LOOK_GAMMA, LOOK_GAMMA, LOOK_GAMMA), name="黑位伽马")
_wb = _gam

# ---- ⑥ 高光软肩：超过「软肩起点」的部分按比例压回来。
_kstart = _vm("SUBTRACT", _wb.outputs[0],
              (LOOK_KNEE_T, LOOK_KNEE_T, LOOK_KNEE_T), name="超起点部")
_kpos = _vm("MAXIMUM", _kstart.outputs["Vector"], (0.0, 0.0, 0.0), name="正部")
_knee2 = _math("MULTIPLY", gi.outputs["软肩"], 0.9, name="肩力度")
_ksoft = _vm("SCALE", _kpos.outputs["Vector"], scale=_knee2.outputs[0], name="软肩量")
_c5 = _vm("SUBTRACT", _wb.outputs[0], _ksoft.outputs["Vector"], name="软肩")
L(_c5.outputs["Vector"], em.inputs["Color"])
L(gi.outputs["自发光"], em.inputs["Strength"])
log("观感层 LOOK=%s 明暗%.2f 边缘光%.2f 高光%.2f AO%.2f 饱和%.2f"
    % (LOOK, LOOK_SHADE, LOOK_RIM, LOOK_SPEC, LOOK_AO, LOOK_SAT))

tr1 = N.new("ShaderNodeBsdfTransparent")
mx1 = N.new("ShaderNodeMixShader"); mx1.name = "透明级1"
L(gi.outputs["Alpha"], mx1.inputs[0])
L(tr1.outputs[0], mx1.inputs[1]); L(em.outputs[0], mx1.inputs[2])
tr2 = N.new("ShaderNodeBsdfTransparent")
mx2 = N.new("ShaderNodeMixShader"); mx2.name = "透明级2"
L(gi.outputs["基础Alpha"], mx2.inputs[0])
L(tr2.outputs[0], mx2.inputs[1]); L(mx1.outputs[0], mx2.inputs[2])
L(mx2.outputs[0], go.inputs[0])
log("共享节点组", GN, "建好，节点数", len(ng.nodes))

# ---------------------------------------------------------------- 逐材质：**新建**材质，不碰插件的树
stat = {"材质": 0, "基础贴图": 0, "色阶": 0, "球面": 0, "描边": 0}
_matid = []
keep_edge = set()
edge_cols = {} # 材质槽索引 → PMX edge_color（描边壳逐面上色用）
_re_face = __import__("re").compile(r"顏|顔|脸|颜|face|Face|FACE")
_re_eye = __import__("re").compile(r"目|瞳|eye|Eye|pupil|白目|目光|眉睫|睫毛|lash|Lash")
_re_skin = __import__("re").compile(r"肌|skin|Skin|腕|腿|首")
_re_hair = __import__("re").compile(r"髪|髮|发|hair|Hair|HAIR|前髪|後髪")
_re_metal = __import__("re").compile(
    r"金屬|金属|金飾|金饰|銀|银|钢|鋼|刃|剑|劍|刀|鎖|锁|扣|甲|"
    r"metal|Metal|gold|Gold|silver|Silver|armor|Armor")
_re_lip = __import__("re").compile(r"^口$|嘴|唇|舌|lip|Lip")

def _mat_class(name):
    """材质分类：脸 > 眼 > 皮肤 > 头发 > 金属 > 布料。
    LOOK_FACE=0 时关掉脸/眼/皮肤的细分（全部按基础类走）。"""
    if LOOK_FACE > 0:
        if _re_face.search(name):
            return "脸"
        if _re_eye.search(name):
            return "眼"
        if _re_skin.search(name):
            return "皮肤"
    if _re_hair.search(name):
        return "头发"
    if _re_metal.search(name):
        return "金属"
    return "布料"
# 材质的 UV 表，用于「按该材质 UV 覆盖区域」统计贴图 alpha
_uvall = np.array([[d.uv[0], d.uv[1]] for d in mesh.data.uv_layers.active.data], np.float64)
for si, slot in enumerate(mesh.material_slots):
    src = slot.material
    mm = getattr(src, "mmd_material", None) if src else None
    if src is None or mm is None or src.name.startswith("mmd_edge."):
        continue
    if float(getattr(mm, "alpha", 1.0)) < 0.02:
        stat["跳过隐形层"] = stat.get("跳过隐形层", 0) + 1
        continue
    base_img = tex_of(src, "mmd_base_tex")
    toon_img = tex_of(src, "mmd_toon_tex")
    sph_img = tex_of(src, "mmd_sphere_tex")
    if base_img is None and toon_img is None:
        continue

    nm = bpy.data.materials.new(src.name + "_mmd")
    nm.use_nodes = True
    nt = nm.node_tree
    for x in list(nt.nodes):
        nt.nodes.remove(x)
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    g = nt.nodes.new("ShaderNodeGroup"); g.node_tree = ng
    g.inputs["拡散色"].default_value = (*mm.diffuse_color, 1.0)
    g.inputs["Alpha"].default_value = float(mm.alpha)
    g.inputs["基础Alpha"].default_value = 1.0
    g.inputs["自发光"].default_value = EMIT
    # ---- 观感层逐材质参数 ------------------------------------------------
    # （历史上散着写 + 用字符串替换维护，出过两次"参数写在注释里、从未生效"的事故）
    # shk 明暗强度（绝对值；LOOK_SHADE 只是总开关）
    # quant 明暗量化（None = 跟随 LOOK_CEL）
    # spk/gloss 高光强度 / 锐度 rimk 边缘光倍率
    # aok/aod AO 强度 / 距离（None = 用全局默认）
    # sub 次表面透光 ring 头发天使环
    # shcol 暗部乘数 satsh 暗部彩度倍率（None = 全局）
    _P = {
        # 布料：平涂为主，极弱高光；几乎不加明暗（加了就"像被灯打了一下"）
        "布料": dict(shk=0.34, quant=None, spk=0.06, gloss=14.0, rimk=0.75,
                     aok=1.00, aod=None, sub=0.0, ring=0.0,
                     shcol=LOOK_SHADOW_CLOTH, satsh=None, term=None),
        "头发": dict(shk=0.35, quant=None, spk=0.35, gloss=40.0, rimk=0.50,
                     aok=0.45, aod=0.020, sub=0.0, ring=LOOK_RING,
                     shcol=LOOK_SHADOW_CLOTH, satsh=None, term=None),
        # 金属：高光又紧又亮，明暗中等
        "金属": dict(shk=0.50, quant=None, spk=0.40, gloss=70.0, rimk=1.25,
                     aok=1.00, aod=None, sub=0.0, ring=0.0,
                     shcol=LOOK_SHADOW_CLOTH, satsh=None, term=None),
        # 皮肤：透光为主，明暗克制，暗部偏粉紫
        "皮肤": dict(shk=0.45, quant=0.30, spk=0.10, gloss=20.0, rimk=0.75,
                     aok=0.55, aod=0.070, sub=LOOK_SUB, ring=0.0,
                     shcol=LOOK_SHADOW_SKIN, satsh=1.10, term=LOOK_TERM * 1.4),
        # 脸：要体块但不能硬切（量化 0.2）；AO 必须克制，眼窝是凹的，AO 一强就黑眼圈显凶
        "脸": dict(shk=0.30, quant=0.20, spk=0.00, gloss=26.0, rimk=0.60,
                   aok=0.40, aod=0.045, sub=LOOK_SUB * 0.55, ring=0.0,
                   shcol=(0.86, 0.80, 0.86, 1.0), satsh=1.0, term=LOOK_TERM * 2.2),
        # 眼（含睫毛）：画好的平涂，压平 + 一点高光点即可
        "眼": dict(shk=0.00, quant=0.0, spk=0.30, gloss=60.0, rimk=0.0,
                   aok=0.00, aod=None, sub=0.0, ring=0.0,
                   shcol=LOOK_SHADOW_RGB, satsh=None, term=None),
    }
    _cls = _mat_class(src.name)
    P = dict(_P[_cls])
    if P["quant"] is None:
        P["quant"] = 1.0 if LOOK_CEL else 0.0
    P["quant"] = min(1.0, P["quant"] * _f("LOOK_QUANT_MUL", "1.0"))
    if P["aod"] is None:
        P["aod"] = LOOK_AO_DIST
    if P["satsh"] is None:
        P["satsh"] = LOOK_SHADOW_SAT
    if P["term"] is None:
        P["term"] = LOOK_TERM
    g.inputs["明暗强度"].default_value = LOOK_SHADE * P["shk"]
    g.inputs["明暗量化"].default_value = P["quant"]
    g.inputs["天使环"].default_value = P["ring"]
    g.inputs["阴影彩度"].default_value = P["satsh"]
    g.inputs["阴影色"].default_value = P["shcol"]
    g.inputs["明暗光向"].default_value = tuple(LOOK_LIGHT[:3])
    g.inputs["交界硬度"].default_value = P["term"]
    g.inputs["对比"].default_value = LOOK_CONTRAST
    g.inputs["AO距离"].default_value = P["aod"]
    g.inputs["AO强度"].default_value = LOOK_AO * P["aok"]
    # 零高光的材质（脸/眼）边缘光也压下去，免得脸上一圈亮边
    g.inputs["边缘光"].default_value = LOOK_RIM * P["rimk"] * (P["shk"] if P["spk"] == 0.0 else 1.0)
    g.inputs["高光"].default_value = LOOK_SPEC * P["spk"]
    g.inputs["次表面"].default_value = P["sub"]
    # 下面这些是全局量，逐材质再写一遍只是为了确保不被节点组接口默认值干扰
    g.inputs["边缘光色"].default_value = LOOK_RIM_RGB
    g.inputs["高光锐度"].default_value = P["gloss"]
    g.inputs["次表面色"].default_value = LOOK_SUB_RGB
    g.inputs["饱和度"].default_value = LOOK_SAT
    g.inputs["白平衡"].default_value = LOOK_WB
    g.inputs["软肩"].default_value = LOOK_KNEE
    stat["分类:" + _cls] = stat.get("分类:" + _cls, 0) + 1
    edge_cols[si] = tuple(float(x) for x in mm.edge_color[:3])
    if getattr(mm, "edge_size", 0.0):
        stat["edge_size"] = round(float(mm.edge_size), 4)
    # HIDE=材质名1,材质名2 : 把这些材质设为全透明（诊断/排除用）
    _only = [x for x in os.environ.get("ONLY", "").split(",") if x]
    if _only and src.name not in _only:
        g.inputs["Alpha"].default_value = 0.0
    if _only and src.name in _only:
        log(" 只显示材质:", src.name)
    _hide = [x for x in os.environ.get("HIDE", "").split(",") if x]
    if src.name in _hide:
        g.inputs["Alpha"].default_value = 0.0
        log(" 隐藏材质:", src.name)
    g.inputs["球面模式"].default_value = (float(mm.sphere_texture_type)
                                          if sph_img is not None else 0.0)

    _uvname = mesh.data.uv_layers[0].name if len(mesh.data.uv_layers) else "UVMap"
    _uvn = nt.nodes.new("ShaderNodeUVMap"); _uvn.uv_map = _uvname; _uvn.name = "UV指定"
    if PIPE == "id":
        import colorsys
        _N = max(1, len(mesh.material_slots))
        _r, _g, _b = colorsys.hsv_to_rgb((si % _N) / _N, 1.0, 1.0)
        g.inputs["基础色"].default_value = (_r, _g, _b, 1.0)
        _matid.append((si, src.name, round(_r, 3), round(_g, 3), round(_b, 3)))
    elif base_img is not None and PIPE != "flat":
        t = nt.nodes.new("ShaderNodeTexImage"); t.image = base_img
        nt.links.new(_uvn.outputs["UV"], t.inputs["Vector"])
        if os.environ.get("CLOSEST") == "1": t.interpolation = "Closest"
        _bt = None
        if not IS_MMD:
            if _re_lip.search(src.name):
                _bt = (1.05, 0.62, 0.66, 1.0)
            elif _re_eye.search(src.name):
                _bt = LOOK_EYE_RGB
            elif _re_face.search(src.name) or _re_skin.search(src.name):
                _bt = LOOK_SKIN_RGB
        if _bt is not None:
            _lt = nt.nodes.new("ShaderNodeMix"); _lt.data_type = "RGBA"; _lt.blend_type = "MULTIPLY"
            _lt.inputs[0].default_value = 1.0
            nt.links.new(t.outputs["Color"], _lt.inputs[6])
            _lt.inputs[7].default_value = _bt
            nt.links.new(_lt.outputs[2], g.inputs["基础色"])
            stat["底色校正"] = stat.get("底色校正", 0) + 1
        else:
            nt.links.new(t.outputs["Color"], g.inputs["基础色"])
        if PIPE != "base":
            nt.links.new(t.outputs["Alpha"], g.inputs["基础Alpha"])
        stat["基础贴图"] += 1
    else:
        g.inputs["基础色"].default_value = (1, 1, 1, 1)

    # ---- lit 模式：真光照色阶（含 EEVEE 投射阴影）乘数链，接到节点组的「外部明暗」
    g.inputs["用外部明暗"].default_value = 0.0
    if LIGHT_MODE == "lit" and _spk > 0.0 and PIPE not in ("flat", "id", "base"):
        # 用该材质自己的底色做 Diffuse，让 N·L 的亮度按真实反照率走
        _lb = nt.nodes.new("ShaderNodeMix"); _lb.data_type = "RGBA"; _lb.blend_type = "MULTIPLY"
        _lb.inputs[0].default_value = 1.0
        if base_img is not None and "t" in dir():
            nt.links.new(t.outputs["Color"], _lb.inputs[6])
        else:
            _lb.inputs[6].default_value = (1, 1, 1, 1)
        _lb.inputs[7].default_value = (*mm.diffuse_color, 1.0)
        _ldiff = nt.nodes.new("ShaderNodeBsdfDiffuse")
        nt.links.new(_lb.outputs[2], _ldiff.inputs["Color"])
        _ls2 = nt.nodes.new("ShaderNodeShaderToRGB")
        nt.links.new(_ldiff.outputs[0], _ls2.inputs[0])
        _lbw = nt.nodes.new("ShaderNodeRGBToBW")
        nt.links.new(_ls2.outputs["Color"], _lbw.inputs[0])
        _lr = nt.nodes.new("ShaderNodeValToRGB"); _lr.name = "光色阶"
        _el = _lr.color_ramp.elements
        _sc = tuple(_shcol[:3])
        _el[0].position = 0.0
        _el[0].color = (_sc[0], _sc[1], _sc[2], 1.0)
        _el[1].position = 0.40
        _el[1].color = (min(1, _sc[0] * 0.62 + 0.38), min(1, _sc[1] * 0.62 + 0.38),
                        min(1, _sc[2] * 0.62 + 0.38), 1.0)
        if int(LOOK_STEPS) >= 3:
            _e1 = _el.new(0.30)
            _e1.color = (min(1, _sc[0] * 0.80 + 0.20), min(1, _sc[1] * 0.80 + 0.20),
                         min(1, _sc[2] * 0.80 + 0.20), 1.0)
        _e2 = _el.new(0.50); _e2.color = (1.04, 1.00, 0.96, 1.0)
        _e3 = _el.new(0.78); _e3.color = (1.10, 1.02, 0.92, 1.0) # 亮部微暖（二游都偏暖，别做冷白）
        _lr.color_ramp.interpolation = "CONSTANT" if LOOK_CEL and _quant >= 0.9 else "EASE"
        nt.links.new(_lbw.outputs[0], _lr.inputs[0])
        _lout = _lr.outputs["Color"]
        if _quant < 0.9: # 脸这类"要柔和"的材质：把色阶往白里混
            _lf = nt.nodes.new("ShaderNodeMix"); _lf.data_type = "RGBA"; _lf.blend_type = "MIX"
            _lf.inputs[0].default_value = 1.0 - _quant
            _lf.inputs[6].default_value = (1, 1, 1, 1)
            nt.links.new(_lr.outputs["Color"], _lf.inputs[7])
            _lout = _lf.outputs[2]
        nt.links.new(_lout, g.inputs["外部明暗"])
        g.inputs["用外部明暗"].default_value = 1.0
        stat["真光照"] = stat.get("真光照", 0) + 1

    if toon_img is not None and PIPE not in ("base", "flat"):
        _use_lut = (LOOK_LUT > 0) and (_mat_class(src.name) != "眼")
        tn = nt.nodes.new("ShaderNodeTexImage")
        tn.image = _lut_image() if _use_lut else toon_img
        nt.links.new(_uvn.outputs["UV"], tn.inputs["Vector"])
        if os.environ.get("CLOSEST") == "1": tn.interpolation = "Closest"
        # LUT 是 1 维 ramp：横向固定取中点，纵向按 N·L 走
        tn.name = "toon"
        tn.interpolation = "Linear"
        tn.extension = "EXTEND"
        nt.links.new(tn.outputs["Color"], g.inputs["色阶"])
        stat["色阶"] += 1
        if _use_lut:
            stat["手绘LUT"] = stat.get("手绘LUT", 0) + 1
    else:
        g.inputs["色阶"].default_value = (1, 1, 1, 1)

    if sph_img is not None:
        sn = nt.nodes.new("ShaderNodeTexImage"); sn.image = sph_img
        nt.links.new(_uvn.outputs["UV"], sn.inputs["Vector"])
        if os.environ.get("CLOSEST") == "1": sn.interpolation = "Closest"
        sn.name = "sphere"; sn.interpolation = "Linear"; sn.extension = "EXTEND"
        nt.links.new(sn.outputs["Color"], g.inputs["球面"])
        stat["球面"] += 1
    else:
        g.inputs["球面"].default_value = (0, 0, 0, 1)

    # ---- 色阶 UV：u=0.5, v = <N·L 或常数>*0.5+0.5 ----
    #   ★ 默认（LOOK=anime）v 取常数 LOOK_FLAT_NL ⇒ **姿态无关**：
    #     低头/转身只改轮廓与位置，不改明暗分布（MMD 的观感口径）。
    #   ★ LOOK=mmd 或 LOOK_POSE_LOCK=0 ⇒ 仍走 N·L（原行为，逐像素不变）。
    #   ⚠ geo 节点下面「球面 UV」还要用，必须始终创建。
    geo = nt.nodes.new("ShaderNodeNewGeometry")
    half = nt.nodes.new("ShaderNodeMath"); half.operation = "MULTIPLY_ADD"
    half.inputs[1].default_value = 0.5
    half.inputs[2].default_value = 0.5
    if LOOK_POSE_LOCK:
        half.inputs[0].default_value = LOOK_FLAT_NL
    else:
        ld = nt.nodes.new("ShaderNodeCombineXYZ")
        for i in range(3):
            ld.inputs[i].default_value = LIGHT_DIR[i]
        dot = nt.nodes.new("ShaderNodeVectorMath"); dot.operation = "DOT_PRODUCT"
        nt.links.new(geo.outputs["Normal"], dot.inputs[0])
        nt.links.new(ld.outputs[0], dot.inputs[1])
        nt.links.new(dot.outputs["Value"], half.inputs[0])
    cuv = nt.nodes.new("ShaderNodeCombineXYZ"); cuv.name = "色阶UV"
    cuv.inputs[0].default_value = 0.5
    nt.links.new(half.outputs[0], cuv.inputs[1])
    tnode = nt.nodes.get("toon") if PIPE != "base" else None
    if tnode:
        nt.links.new(cuv.outputs[0], tnode.inputs["Vector"])

    # ---- 球面 UV：相机空间法线 xy*0.5+0.5 ----
    snode = nt.nodes.get("sphere")
    if snode:
        vt = nt.nodes.new("ShaderNodeVectorTransform")
        vt.vector_type = "NORMAL"; vt.convert_from = "WORLD"; vt.convert_to = "CAMERA"
        nt.links.new(geo.outputs["Normal"], vt.inputs[0])
        mad = nt.nodes.new("ShaderNodeVectorMath"); mad.operation = "MULTIPLY_ADD"
        mad.inputs[1].default_value = (0.5, 0.5, 0.5)
        mad.inputs[2].default_value = (0.5, 0.5, 0.5)
        nt.links.new(vt.outputs["Vector"], mad.inputs[0])
        nt.links.new(mad.outputs["Vector"], snode.inputs["Vector"])

    nt.links.new(g.outputs[0], out.inputs["Surface"])
    # NO_CULL=<子串1,子串2>：命中这些材质名**关闭背面剔除**（双面渲染）。
    # 用途：单面薄片（翅膀/飘带等）背面朝相机时会被剔掉、整片消失；
    # MMD 里这种件若标了「両面描画」就该双面画。默认仍按 MMD 的 is_double_sided 走。
    _nocull = [x for x in os.environ.get("NO_CULL", "").split(",") if x]
    if _nocull and any(k in src.name for k in _nocull):
        nm.use_backface_culling = False
    else:
        nm.use_backface_culling = not getattr(mm, "is_double_sided", False)
# 贴图 alpha 优先于材质 alpha（例：肌+ 的贴图 alpha=0.26，材质 alpha=1）。
    _ta = 1.0
    if base_img is not None:
        try:
            _w, _h = base_img.size
            if _w and _h:
                _buf = np.empty(len(base_img.pixels), dtype=np.float32)
                base_img.pixels.foreach_get(_buf)
                _al = _buf.reshape(_h, _w, 4)[:, :, 3]
                # 图集往往有大片透明留白，用整图均值会把一堆材质误判成半透明。
                _polys = [p for p in mesh.data.polygons if p.material_index == si]
                if _polys:
                    _li = np.concatenate([np.asarray(p.loop_indices) for p in _polys])
                    _u = _uvall[_li]
                    _su = np.clip((_u[:, 0] % 1.0) * (_w - 1), 0, _w - 1).astype(int)
                    _sv = np.clip((_u[:, 1] % 1.0) * (_h - 1), 0, _h - 1).astype(int)
                    _ta = float(_al[_sv, _su].mean())
                else:
                    _ta = float(_al.mean())
        except Exception:
            _ta = 1.0
    try:
        nm.surface_render_method = "BLENDED" if (float(mm.alpha) < 0.999 or _ta < 0.95) else "DITHERED"
    except Exception:
        pass
    if _ta < 0.95:
        stat["贴图带alpha"] = stat.get("贴图带alpha", 0) + 1
    slot.material = nm
    stat["材质"] += 1
    # EDGE_SKIP=<子串> 这些材质不做描边（薄片/翅膀等做反向壳会变成实心黑片）
    _eskip = [x for x in os.environ.get("EDGE_SKIP", "").split(",") if x]
    if mm.enabled_toon_edge and not any(k in src.name for k in _eskip):
        stat["描边"] += 1
        keep_edge.add(si)
log("材质逐项:", stat)
prog("materials", 0.40, "材质重建完成 %d 个" % stat["材质"], stats=dict(stat))
if PIPE == "id":
    with open(os.path.join(OUTDIR, "_matid.txt"), "w", encoding="utf-8") as _fh:
        for _row in _matid:
            _fh.write("%d\\t%s\\t%s\
" % (_row[0], _row[1], _row[2:]))
    log("材质ID映射写入 _matid.txt，共", len(_matid))

# ---------------------------------------------------------------- 描边（反向壳）
# 反向壳（沿法线外扩 + 翻面 + 背面剔除）**只对闭合体积成立**。
# EDGE_KEEP_ALL=1 关掉自动判定；EDGE_SKIP=<子串> 手动追加排除。
if keep_edge and not NO_EDGE and os.environ.get("EDGE_SHEET_RULE") == "1":
    _bm2 = bmesh.new(); _bm2.from_mesh(mesh.data)
    _bm2.faces.ensure_lookup_table(); _bm2.edges.ensure_lookup_table()
    _eo = {}
    for _ffc in _bm2.faces:
        if _ffc.material_index not in keep_edge:
            continue
        for _e in _ffc.edges:
            _eo.setdefault(_ffc.material_index, set()).add(_e)
    _drop_sheet = set()
    for _si, _es in _eo.items():
        if not _es:
            continue
        _bnd = sum(1 for _e in _es if len(_e.link_faces) == 1)
        if _bnd / len(_es) >= 0.5:
            _mm2 = mesh.material_slots[_si].material
            _drop_sheet.add(_si)
            log(" 跳过薄片描边: %s (开放边 %.0f%%)"
                % (_mm2.name if _mm2 else _si, _bnd / len(_es) * 100))
    _bm2.free()
    keep_edge -= _drop_sheet
    if not keep_edge:
        log("描边：参与材质全被判为薄片，跳过建壳")

if keep_edge and not NO_EDGE:
    arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
    shell = mesh.copy()
    shell.data = mesh.data.copy()
    shell.name = NAME + "_描边"
    bpy.context.scene.collection.objects.link(shell)
    bm = bmesh.new(); bm.from_mesh(shell.data)
    bm.faces.ensure_lookup_table()
    dead = [f for f in bm.faces if f.material_index not in keep_edge]
    if dead:
        bmesh.ops.delete(bm, geom=dead, context="FACES")
    bm.normal_update()
    # 先按「面心相对包围盒中心」的方向和判断整体朝向，朝内就先翻一次。
    _c = Vector((0.0, 0.0, 0.0))
    for _v in bm.verts:
        _c += _v.co
    if len(bm.verts):
        _c /= len(bm.verts)
    _s = 0.0
    for _ffc in bm.faces:
        _s += _ffc.normal.dot(_ffc.calc_center_median() - _c)
    log(" 壳法线朝向和 %.5f → %s" % (_s, "朝外(正常)" if _s >= 0 else "朝内，先翻转"))
    if _s < 0:
        bmesh.ops.reverse_faces(bm, faces=bm.faces[:])
        bm.normal_update()

    # 反向壳只对闭合体积成立。薄片有里外两层，**里层法线朝向物体内部**；
    # 判据用「面法线是否背离模型中心」，比按开放边占比更直接、不用调阈值。
    # EDGE_KEEP_ALL=1 可关掉。
    if os.environ.get("EDGE_KEEP_ALL") != "1":
        _n0 = len(bm.faces)
        _dead_in = [f for f in bm.faces if f.normal.dot(f.calc_center_median() - _c) < 0.0]
        if _dead_in:
            bmesh.ops.delete(bm, geom=_dead_in, context="FACES")
            log(" 剔除朝内（薄片里层）壳面 %d / %d" % (len(_dead_in), _n0))
        bm.normal_update()
    # 这里按「该面沿顶点法线实际能被推动多少」判断，推动量太小的面直接剔除。
    _dead2 = []
    for f in bm.faces:
        push = 0.0
        for v in f.verts:
            push += v.normal.dot(f.normal)
        push /= max(len(f.verts), 1)
        if push < 0.35:
            _dead2.append(f)
    if _dead2:
        bmesh.ops.delete(bm, geom=_dead2, context="FACES")
        log(" 剔除退化（零厚度薄片）壳面 %d 个" % len(_dead2))
    # 描边就连续了（这就是"描边法线"那条）。
    if not IS_MMD:
        _avg = {}
        for _ffc in bm.faces:
            _w = _ffc.calc_area()
            for _v in _ffc.verts:
                _acc = _avg.get(_v)
                if _acc is None:
                    _avg[_v] = _ffc.normal * _w
                else:
                    _acc += _ffc.normal * _w
        _pushed = 0
        for _v in bm.verts:
            _n = _avg.get(_v)
            if _n is not None and _n.length > 1e-9:
                _n = _n.normalized()
            else:
                _n = _v.normal
            _v.co += _n * EDGE_T
            _pushed += 1
        log(" 壳外扩：面积加权平均法线 %d 顶点" % _pushed)
    else:
        for _v in bm.verts:
            _v.co += _v.normal * EDGE_T
    bmesh.ops.reverse_faces(bm, faces=bm.faces[:])
# 逐材质描边色：PMX 的 edge_color 写入循环属性 edgecol，供壳材质读取。
    _lay = None
    try:
        _lay = bm.loops.layers.float_color.get("edgecol")
    except Exception:
        _lay = None
    if _lay is None:
        _lay = bm.loops.layers.float_color.new("edgecol")
    _ncol = 0
    for f in bm.faces:
        if EDGE_FROM_PMX:
            _c0 = edge_cols.get(f.material_index, (0.0, 0.0, 0.0))
        else:
            _c0 = (0.0, 0.0, 0.0)
        _k = EDGE_INK
        _v4 = (_c0[0] * EDGE_GAIN * (1 - _k) + EDGE_INK_RGB[0] * _k,
               _c0[1] * EDGE_GAIN * (1 - _k) + EDGE_INK_RGB[1] * _k,
               _c0[2] * EDGE_GAIN * (1 - _k) + EDGE_INK_RGB[2] * _k, 1.0)
        if any(x > 0.0 for x in _c0):
            _ncol += 1
        for lp in f.loops:
            lp[_lay] = _v4
    for f in bm.faces:
        f.material_index = 0
    bm.to_mesh(shell.data); bm.free(); shell.data.update()
    emm = bpy.data.materials.new("描边黑")
    emm.use_nodes = True
    ntt = emm.node_tree
    for x in list(ntt.nodes):
        ntt.nodes.remove(x)
    o2 = ntt.nodes.new("ShaderNodeOutputMaterial")
    e2 = ntt.nodes.new("ShaderNodeEmission")
    # EDGE_RGB="r,g,b" 可把描边壳染成醒目颜色，用来定位「壳盖到哪了」（诊断用）
    _ergb = os.environ.get("EDGE_RGB", "")
    if _ergb:
        e2.inputs["Color"].default_value = tuple(float(x) for x in _ergb.split(",")) + (1.0,)
    else:
        _attr = ntt.nodes.new("ShaderNodeAttribute")
        _attr.attribute_type = "GEOMETRY"; _attr.attribute_name = "edgecol"
        _attr.name = "描边色"
        ntt.links.new(_attr.outputs["Color"], e2.inputs["Color"])
    ntt.links.new(e2.outputs[0], o2.inputs["Surface"])
    emm.use_backface_culling = True
    shell.data.materials.clear(); shell.data.materials.append(emm)
    shell.visible_shadow = False
    for _mo in list(shell.modifiers):
        shell.modifiers.remove(_mo)
    if arm:
        md = shell.modifiers.new("Armature", "ARMATURE"); md.object = arm
        shell.parent = mesh.parent
        shell.matrix_parent_inverse = mesh.matrix_parent_inverse.copy()
    log("描边壳: 面", len(shell.data.polygons), "参与材质", len(keep_edge),
        "厚度", EDGE_T, "非黑描边面", _ncol, "来源", "PMX edge_color" if EDGE_FROM_PMX else "纯黑")
    prog("outline", 0.55, "描边壳 %d 面 / %d 个材质" % (len(shell.data.polygons), len(keep_edge)))
else:
    log("跳过描边壳（NO_EDGE=%s，可描边材质 %d 个）" % (NO_EDGE, len(keep_edge)))
    prog("outline", 0.55, "跳过描边")

# ---------------------------------------------------------------- 严格模式护栏
# 这里在渲染前把"严格模式下必须归零/归位"的参数逐项体检，漏了就大声喊出来。
if IS_MMD:
    _IDENTITY = {
        "LOOK_BLOOM": (LOOK_BLOOM, 0.0), "LOOK_AO": (LOOK_AO, 0.0),
        "LOOK_GAMMA": (LOOK_GAMMA, 1.0), "LOOK_EXPOSURE": (LOOK_EXPOSURE, 1.0),
        "LOOK_CONTRAST": (LOOK_CONTRAST, 0.0), "LOOK_SUB": (LOOK_SUB, 0.0),
        "LOOK_RIM": (LOOK_RIM, 0.0), "LOOK_SPEC": (LOOK_SPEC, 0.0),
        "LOOK_KNEE": (LOOK_KNEE, 0.0),
        "LOOK_SHADE": (LOOK_SHADE, 0.0), "EDGE_INK": (EDGE_INK, 0.0),
        "EDGE_GAIN": (EDGE_GAIN, 0.22), "LOOK_SAT": (LOOK_SAT, 1.0),
    }
    _bad = ["%s=%s(应 %s)" % (k, v[0], v[1]) for k, v in _IDENTITY.items()
            if abs(float(v[0]) - float(v[1])) > 1e-6]
    if _bad:
        log("⚠ 严格还原模式有参数没归位，逐像素一致性可能已被破坏：")
        for _b in _bad:
            log(" " + _b)
    else:
        log("严格还原模式参数体检：全部归位 ✓")

# ---------------------------------------------------------------- 相机 / 世界 / 渲染
sc = bpy.context.scene
sc.render.engine = "BLENDER_EEVEE"
# 采样数只对观感层提高；严格还原模式保持 Blender 默认，否则逐像素一致性被破坏。
if not IS_MMD:
    try:
        sc.eevee.taa_render_samples = int(os.environ.get("TAA", "96"))
    except Exception:
        pass
sc.render.film_transparent = (BG == "none")
sc.view_settings.view_transform = "Raw" if os.environ.get("PIPE", "") in ("id", "idx") else "Standard"
sc.view_settings.exposure = 0.0
w = sc.world or bpy.data.worlds.new("W")
sc.world = w
if BG == "grad":
    # 竖直渐变背景：人物在深色底上轮廓更清晰，「惨白」的观感有一半是被浅灰底衬出来的
    w.use_nodes = True
    _wn, _wl = w.node_tree.nodes, w.node_tree.links
    for _x in list(_wn):
        _wn.remove(_x)
    _wo = _wn.new("ShaderNodeOutputWorld")
    _wbg = _wn.new("ShaderNodeBackground"); _wbg.name = "背景"
    _tc = _wn.new("ShaderNodeTexCoord")
    _sep = _wn.new("ShaderNodeSeparateXYZ")
    _wl.new(_tc.outputs["Window"], _sep.inputs[0])
    _ramp = _wn.new("ShaderNodeValToRGB"); _ramp.name = "渐变色"
    _ramp.color_ramp.elements[0].position = 0.0
    _ramp.color_ramp.elements[0].color = BG_BOT
    _ramp.color_ramp.elements[1].position = 0.85
    _ramp.color_ramp.elements[1].color = BG_TOP
    _wl.new(_sep.outputs["Y"], _ramp.inputs[0])
    # ---- 暗角（vignette）+ 中心背光：让角色从背景里"浮"出来（解决"平贴"）
    _dx = _wn.new("ShaderNodeMath"); _dx.operation = "SUBTRACT"
    _dx.inputs[1].default_value = 0.5
    _wl.new(_sep.outputs["X"], _dx.inputs[0])
    _dy = _wn.new("ShaderNodeMath"); _dy.operation = "SUBTRACT"
    _dy.inputs[1].default_value = 0.5
    _wl.new(_sep.outputs["Y"], _dy.inputs[0])
    _dc = _wn.new("ShaderNodeCombineXYZ")
    _wl.new(_dx.outputs[0], _dc.inputs[0])
    _wl.new(_dy.outputs[0], _dc.inputs[1])
    _dl = _wn.new("ShaderNodeVectorMath"); _dl.operation = "LENGTH"
    _wl.new(_dc.outputs[0], _dl.inputs[0])
    _vg = _wn.new("ShaderNodeMapRange"); _vg.name = "暗角"
    _vg.data_type = "FLOAT"; _vg.interpolation_type = "SMOOTHSTEP"; _vg.clamp = True
    _wl.new(_dl.outputs["Value"], _vg.inputs["Value"])
    _vg.inputs["From Min"].default_value = 0.22
    _vg.inputs["From Max"].default_value = 0.72
    _vg.inputs["To Min"].default_value = 1.0
    _vg.inputs["To Max"].default_value = float(os.environ.get("BG_VIGNETTE", "0.42"))
    _vgc = _wn.new("ShaderNodeCombineXYZ")
    _wl.new(_vg.outputs["Result"], _vgc.inputs[0])
    _wl.new(_vg.outputs["Result"], _vgc.inputs[1])
    _wl.new(_vg.outputs["Result"], _vgc.inputs[2])
    _vmix = _wn.new("ShaderNodeMix"); _vmix.data_type = "RGBA"; _vmix.blend_type = "MULTIPLY"
    _vmix.inputs[0].default_value = 1.0
    _wl.new(_ramp.outputs["Color"], _vmix.inputs[6])
    _wl.new(_vgc.outputs[0], _vmix.inputs[7])
    _gw = _wn.new("ShaderNodeMapRange"); _gw.name = "背光"
    _gw.data_type = "FLOAT"; _gw.interpolation_type = "SMOOTHSTEP"; _gw.clamp = True
    _wl.new(_dl.outputs["Value"], _gw.inputs["Value"])
    _gw.inputs["From Min"].default_value = 0.0
    _gw.inputs["From Max"].default_value = 0.55
    _gw.inputs["To Min"].default_value = float(os.environ.get("BG_GLOW", "0.10"))
    _gw.inputs["To Max"].default_value = 0.0
    _gwc = _wn.new("ShaderNodeCombineXYZ")
    for _i in range(3):
        _wl.new(_gw.outputs["Result"], _gwc.inputs[_i])
    _gadd = _wn.new("ShaderNodeMix"); _gadd.data_type = "RGBA"; _gadd.blend_type = "ADD"
    _gadd.inputs[0].default_value = 1.0
    _wl.new(_vmix.outputs[2], _gadd.inputs[6])
    _wl.new(_gwc.outputs[0], _gadd.inputs[7])
    _wl.new(_gadd.outputs[2], _wbg.inputs["Color"])
    # EEVEE 里世界着色器**同时**决定「相机看到什么」和「物体收到什么环境光」。
    # 用 Light Path 的 Is Camera Ray 把两条路分开：相机射线看渐变背景，其他射线吃中性暖环境光。
    _lp = _wn.new("ShaderNodeLightPath")
    _amb = _wn.new("ShaderNodeBackground"); _amb.name = "环境光"
    _ambc = _rgb("LIT_AMBIENT_RGB", (0.92, 0.90, 0.86, 1.0))
    _amb.inputs[0].default_value = (_ambc[0], _ambc[1], _ambc[2], 1.0)
    _amb.inputs[1].default_value = 1.0
    _wsep = _wn.new("ShaderNodeMixShader")
    _wl.new(_lp.outputs["Is Camera Ray"], _wsep.inputs[0])
    _wl.new(_amb.outputs[0], _wsep.inputs[1])
    _wl.new(_wbg.outputs[0], _wsep.inputs[2])
    _wl.new(_wsep.outputs[0], _wo.inputs["Surface"])
    log("背景: 渐变 + 暗角%.2f + 背光%.2f" % (float(os.environ.get("BG_VIGNETTE", "0.42")),
                                            float(os.environ.get("BG_GLOW", "0.10"))))
elif BG == "none":
    w.use_nodes = True
    _bg0 = w.node_tree.nodes.get("Background")
    if _bg0:
        _bg0.inputs[0].default_value = (0.0, 0.0, 0.0, 1.0)
        _bg0.inputs[1].default_value = 0.0
    log("背景: 透明（无背景，可直接叠底）")
elif BG == "white":
    w.use_nodes = True
    _wn, _wl = w.node_tree.nodes, w.node_tree.links
    for _x in list(_wn):
        _wn.remove(_x)
    _wo = _wn.new("ShaderNodeOutputWorld")
    _wbg = _wn.new("ShaderNodeBackground"); _wbg.name = "Background"
    _wc = _rgb("BG_WHITE_RGB", (0.96, 0.96, 0.97, 1.0))
    _wbg.inputs[0].default_value = (_wc[0], _wc[1], _wc[2], 1.0)
    _wbg.inputs[1].default_value = 1.0
    # 相机看到的仍是白底，物体收到的环境光只有 LIT_AMBIENT 那么强。
    _lp = _wn.new("ShaderNodeLightPath")
    _amb = _wn.new("ShaderNodeBackground"); _amb.name = "环境光"
    _ambc = _rgb("LIT_AMBIENT_RGB", (0.92, 0.90, 0.86, 1.0))
    _amb.inputs[0].default_value = (_ambc[0], _ambc[1], _ambc[2], 1.0)
    _amb.inputs[1].default_value = 1.0
    _wsep = _wn.new("ShaderNodeMixShader")
    _wl.new(_lp.outputs["Is Camera Ray"], _wsep.inputs[0])
    _wl.new(_amb.outputs[0], _wsep.inputs[1])
    _wl.new(_wbg.outputs[0], _wsep.inputs[2])
    _wl.new(_wsep.outputs[0], _wo.inputs["Surface"])
    log("背景: 白底 %.2f（同时作为环境光）" % _wc[0])
else:
    if not w.use_nodes:
        w.use_nodes = True
    bg = w.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = tuple(BG_TOP)
        bg.inputs[1].default_value = 1.0
# AO 节点在 EEVEE 里依赖射线追踪 / 快速 GI，没开的话 AO 恒为 1（= 没效果），静默失效
for _k, _v in (("use_raytracing", True), ("use_shadows", True)):
    try:
        setattr(sc.eevee, _k, _v)
    except Exception:
        pass
try:
    sc.eevee.ray_tracing_options.use_denoise = True
except Exception:
    pass

# ---------------------------------------------------------------- 内线（法线边缘检测）
# 做法：用 material_override 换成法线 / 深度 / 材质ID 可视化材质各渲一张，
# 在合成阶段取边缘并压暗原图。代价是渲染时间约 ×2~×4（视开启通道数）。
LOOK_INNER = 0.0 if IS_MMD else _f("LOOK_INNER", "0.9") # 0=关；开内线会让渲染时间约 ×2
LOOK_INNER_T = _f("LOOK_INNER_T", "0.62") # 边缘阈值（越小线越多；太低会在甲片上画出"划痕"）
LOOK_INNER_K = _f("LOOK_INNER_K", "1.6") # 线强
LOOK_INNER_W = _f("LOOK_INNER_W", "0") # 线宽（膨胀次数；0=1px 细线，2 会变粗）
LOOK_INNER_D = _f("LOOK_INNER_D", "0") # 深度线强度（0=关；12:00 那版就是关的）
LOOK_INNER_DT = _f("LOOK_INNER_DT", "0.012") # 深度梯度阈值（归一化深度，越小线越多）
LOOK_INNER_ID = _f("LOOK_INNER_ID", "0") # ID 线强度（0=关；开了会在所有材质边界描线）
LOOK_INNER_IT = _f("LOOK_INNER_IT", "0.15") # ID 跳变阈值（ID 是纯色相，0.15 足够区分）
LOOK_INNER_IW = _f("LOOK_INNER_IW", "0") # ID 线宽（0=1px；一旦膨胀就会像马克笔黑框）

def _img_load_raw(path):
    img = bpy.data.images.load(path, check_existing=False)
    try:
        img.colorspace_settings.name = "Non-Color"
    except Exception:
        pass
    w, h = img.size
    buf = np.empty(w * h * 4, dtype=np.float32)
    img.pixels.foreach_get(buf)
    a = buf.reshape(h, w, 4)
    bpy.data.images.remove(img)
    return a

def _img_save_raw(path, arr):
    h, w, _ = arr.shape
    img = bpy.data.images.new("_inner_out", width=w, height=h, alpha=True, float_buffer=False)
    try:
        img.colorspace_settings.name = "Non-Color"
    except Exception:
        pass
    img.pixels.foreach_set(arr.reshape(-1))
    img.filepath_raw = path
    img.file_format = "PNG"
    img.save()
    bpy.data.images.remove(img)

def _make_id_mats(groups):
    """按「ID 分组」造唯一纯色材质。groups = [key, key, ...]，同 key 得到同色（不描线）。

    ★ 为什么要分组而不是一材质一色：脸、眼、睫毛、以及 alpha=0 的隐藏层(`顏+`)都是
      **盖在脸上的独立材质块**，它们的边界是 UV 矩形，不是造型边缘。
      一材质一色 ⇒ 脸上会被描出方框和横杠（实测：眼睛被套了个方框、脸上多一条横线）。
      所以这几类并成同一组，脸的线交给贴图里的线稿去画。
    """
    import colorsys
    keys = []
    for k in groups:
        if k not in keys:
            keys.append(k)
    n = max(1, len(keys))
    out = []
    for k in groups:
        j = keys.index(k)
        m = bpy.data.materials.new("_idmat%d" % j)
        m.use_nodes = True
        nt = m.node_tree
        for nd in list(nt.nodes):
            nt.nodes.remove(nd)
        o = nt.nodes.new("ShaderNodeOutputMaterial")
        e = nt.nodes.new("ShaderNodeEmission")
        r, g, b = colorsys.hsv_to_rgb((j % n) / n, 1.0, 1.0)
        e.inputs["Color"].default_value = (r, g, b, 1.0)
        nt.links.new(e.outputs[0], o.inputs["Surface"])
        out.append(m)
    return out

def _id_groups(mesh):
    """把材质槽映射成 ID 分组键。脸/眼/隐藏层并为一组，其余各自一组。"""
    grp = []
    for i, s in enumerate(mesh.material_slots):
        src = s.material
        nm = src.name if src else ""
        mm = getattr(src, "mmd_material", None) if src else None
        alpha = float(mm.alpha) if mm is not None else 1.0
        cls = _mat_class(nm) if nm else "布料"
        if alpha < 0.02 or cls in ("脸", "眼"):
            grp.append("__face__")
        else:
            grp.append("m%d" % i)
    return grp

def _id_edge(idimg, thr):
    """材质 ID 图的邻域跳变 → 边缘（单像素）。"""
    a = idimg[:, :, :3]
    dx = np.abs(np.roll(a, -1, axis=1) - a).max(axis=2)
    dy = np.abs(np.roll(a, -1, axis=0) - a).max(axis=2)
    return (np.maximum(dx, dy) > thr).astype(np.float32)

def _make_normal_mat():
    m = bpy.data.materials.new("_法线可视化")
    m.use_nodes = True
    nt = m.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    o = nt.nodes.new("ShaderNodeOutputMaterial")
    g = nt.nodes.new("ShaderNodeNewGeometry")
    mad = nt.nodes.new("ShaderNodeVectorMath"); mad.operation = "MULTIPLY_ADD"
    mad.inputs[1].default_value = (0.5, 0.5, 0.5)
    mad.inputs[2].default_value = (0.5, 0.5, 0.5)
    nt.links.new(g.outputs["Normal"], mad.inputs[0])
    e = nt.nodes.new("ShaderNodeEmission")
    nt.links.new(mad.outputs["Vector"], e.inputs["Color"])
    nt.links.new(e.outputs[0], o.inputs["Surface"])
    return m

def _make_depth_mat(scale):
    """视深可视化：近=黑、远=白。用来画「深度不连续」的线。

    ★ 为什么不能只靠几何描边 / 法线：描边壳靠沿法线外扩，绑带离大腿只有 ~2mm，
      比壳厚度还小 ⇒ 两片壳互相穿插，线被啃成一段一段（实测腰带/绑带处最明显）。
      深度不连续是屏幕空间的，不受片间距离影响 ⇒ 线天然连续。
    """
    m = bpy.data.materials.new("_深度可视化")
    m.use_nodes = True
    nt = m.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    o = nt.nodes.new("ShaderNodeOutputMaterial")
    cd = nt.nodes.new("ShaderNodeCameraData")
    mul = nt.nodes.new("ShaderNodeMath"); mul.operation = "MULTIPLY"
    mul.inputs[1].default_value = scale
    nt.links.new(cd.outputs["View Z Depth"], mul.inputs[0])
    e = nt.nodes.new("ShaderNodeEmission")
    nt.links.new(mul.outputs[0], e.inputs["Color"])
    nt.links.new(e.outputs[0], o.inputs["Surface"])
    return m

def _sobel_mag(p):
    """单通道 3x3 梯度幅值。"""
    gx = np.roll(p, -1, 0) - np.roll(p, 1, 0)
    gy = np.roll(p, -1, 1) - np.roll(p, 1, 1)
    return np.sqrt(gx ** 2 + gy ** 2)

def _dilate(mask, times):
    for _ in range(int(max(0, times))):
        mask = np.maximum.reduce([np.roll(mask, s, ax) for ax in (0, 1)
                                  for s in (-1, 0, 1)])
    return mask

def _inner_line(color_path, normal_path, depth_path=None, id_path=None):
    """法线 + 深度 + 材质ID 三通道边缘 → 压暗叠回彩色图。原地覆盖 color_path。"""
    col = _img_load_raw(color_path)
    nrm = _img_load_raw(normal_path) if normal_path else None
    if nrm is None or col.shape != nrm.shape:
        log(" 内线跳过（法线图尺寸不一致）")
        return False
    n = np.clip(nrm[:, :, :3], 0.0, 1.0)
    mag = np.sqrt(sum(_sobel_mag(n[:, :, c]) ** 2 for c in range(3)))
    line = np.clip((mag - LOOK_INNER_T) * LOOK_INNER_K, 0.0, 1.0)
    # 深度通道：物体边界 / 前后遮挡（描边壳画不出来的地方，这里靠深度差补上）
    if depth_path and LOOK_INNER_D > 0:
        try:
            dep = _img_load_raw(depth_path)
            if dep.shape == col.shape:
                dm = _sobel_mag(dep[:, :, 0])
                dline = np.clip((dm - LOOK_INNER_DT) * LOOK_INNER_D, 0.0, 1.0)
                line = np.maximum(line, dline * 0.92)
        except Exception as _e:
            log(" 深度线跳过:", _e)
    iline = None
    if id_path and LOOK_INNER_ID > 0:
        try:
            idm = _img_load_raw(id_path)
            if idm.shape == col.shape:
                iline = _id_edge(idm, LOOK_INNER_IT)
        except Exception as _e:
            log(" ID 线跳过:", _e)
    # 线宽：Sobel 只有 1px，法线/深度线要膨胀才有"画上去"的存在感；
    line = _dilate(line, LOOK_INNER_W)
    if iline is not None:
        line = np.maximum(line, _dilate(iline, LOOK_INNER_IW) * min(1.0, LOOK_INNER_ID))
    # 只在角色内部生效（alpha 有值处），并保留 alpha
    keep = col[:, :, 3] > 0.02
    line = line * keep
    out = col.copy()
    f = (1.0 - line)[:, :, None]
    out[:, :, :3] = col[:, :, :3] * f
    _img_save_raw(color_path, out)
    return True

_inner_nmat = None

# ---------------------------------------------------------------- 真光照灯组（LIGHT_MODE=lit）
# 头发压额头、兜帽压脖子、手臂压身体这些遮挡，是"像渲染"和"像贴纸"的分界。
if LIGHT_MODE == "lit":
    def _add_sun(_nm, _dir, _energy, _shadow):
        _d = bpy.data.lights.new(_nm, "SUN")
        _d.energy = _energy
        _d.angle = math.radians(4.0)
        _d.use_shadow = _shadow
        _o = bpy.data.objects.new(_nm, _d)
        sc.collection.objects.link(_o)
        _v = Vector(_dir).normalized()
        _o.rotation_euler = _v.to_track_quat("Z", "Y").to_euler()
        return _o

    _add_sun("主光", tuple(LOOK_LIGHT[:3]), LIT_KEY, True)
    _add_sun("补光", (0.50, -0.75, 0.20), LIT_FILL, False)
    _add_sun("轮廓光", (0.70, 0.55, 0.35), LIT_RIM, False)
    try:
        sc.eevee.shadow_resolution_scale = 2.0
        sc.eevee.use_shadows = True
    except Exception:
        pass
    # 环境光：把暗部抬起来，别让色阶的暗部变成死黑
    for _nm in ("环境光", "Background"):
        _bg = w.node_tree.nodes.get(_nm)
        if _bg:
            _bg.inputs[1].default_value = max(LIT_AMBIENT, 0.05)
    log("真光照灯组: 主光 %.1f(投影) 补光 %.1f 轮廓光 %.1f 环境 %.2f / 环境色 %s"
        % (LIT_KEY, LIT_FILL, LIT_RIM, LIT_AMBIENT,
           tuple(round(x, 2) for x in _rgb("LIT_AMBIENT_RGB", (0.92, 0.90, 0.86, 1.0))[:3])))

# ---------------------------------------------------------------- 后期泛光（compositor）
# Blender 5.0 起合成器改成 scene.compositing_node_group（旧 scene.node_tree 已移除；
def _build_bloom():
    if hasattr(sc, "compositing_node_group"):
        tree = bpy.data.node_groups.new("泛光合成", "CompositorNodeTree")
        sc.compositing_node_group = tree
        tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
        _out = tree.nodes.new("NodeGroupOutput")
        _out_sock = _out.inputs[0]
    else:
        sc.use_nodes = True
        tree = sc.node_tree
        for _n in list(tree.nodes):
            tree.nodes.remove(_n)
        _out = tree.nodes.new("CompositorNodeComposite")
        _out_sock = _out.inputs["Image"]
    rl = tree.nodes.new("CompositorNodeRLayers")
    gl = tree.nodes.new("CompositorNodeGlare")
    gl.name = "泛光"

    def _gset(sock_name, attr_name, val):
        s = gl.inputs.get(sock_name)
        if s is not None:
            try:
                s.default_value = val
                return True
            except Exception:
                pass
        try:
            setattr(gl, attr_name, val)
            return True
        except Exception:
            return False

    for _t in ("Bloom", "BLOOM", "Fog Glow", "FOG_GLOW"):
        if _gset("Type", "glare_type", _t):
            break
    _gset("Threshold", "threshold", LOOK_BLOOM_T)
    _gset("Size", "size", int(LOOK_BLOOM_SIZE))
    _gset("Smoothness", "smoothness", 0.2)
    _gset("Quality", "quality", "HIGH")
    tree.links.new(rl.outputs["Image"], gl.inputs["Image"])
    # 用 Mix 节点自己控制泛光强度：各版本 Glare 的 mix/strength 语义不一致，
    # 但「原图 × (1-k) + 泛光图 × k」这个语义是确定的。
    _mix_ok = False
    for _t in ("CompositorNodeMixRGB", "ShaderNodeMix"):
        try:
            if _t == "CompositorNodeMixRGB":
                _mx = tree.nodes.new(_t)
                _mx.blend_type = "MIX"
                _mx.inputs[0].default_value = LOOK_BLOOM
                tree.links.new(rl.outputs["Image"], _mx.inputs[1])
                tree.links.new(gl.outputs["Image"], _mx.inputs[2])
                tree.links.new(_mx.outputs[0], _out_sock)
            else:
                _mx = tree.nodes.new(_t)
                _mx.data_type = "RGBA"
                _mx.blend_type = "MIX"
                _mx.inputs[0].default_value = LOOK_BLOOM
                tree.links.new(rl.outputs["Image"], _mx.inputs[6])
                tree.links.new(gl.outputs["Image"], _mx.inputs[7])
                tree.links.new(_mx.outputs[2], _out_sock)
            _mix_ok = True
            break
        except Exception:
            continue
    if not _mix_ok:
        tree.links.new(gl.outputs["Image"], _out_sock)
    return _mix_ok

if LOOK_BLOOM > 0:
    try:
        _ok = _build_bloom()
        log("后期泛光: 阈值 %.2f 尺寸 %d 强度 %.2f%s"
            % (LOOK_BLOOM_T, int(LOOK_BLOOM_SIZE), LOOK_BLOOM,
               "" if _ok else "（无 Mix 节点，按 Glare 默认强度）"))
    except Exception as _e:
        log("后期泛光不可用（跳过）:", _e)

mn = Vector((1e9,) * 3); mx = Vector((-1e9,) * 3)
# FRAME_SKIP=<子串,...>：取景时忽略这些材质占的面（巨大翅膀 / 披风 / 特效）。
_FS = [s for s in os.environ.get("FRAME_SKIP", "").split(",") if s]
_skip_v = set()
if _FS:
    _skip_slots = {i for i, s in enumerate(mesh.material_slots)
                   if s.material and any(k in s.material.name for k in _FS)}
    for _p in mesh.data.polygons:
        if _p.material_index in _skip_slots:
            _skip_v.update(_p.vertices)
    log("取景忽略材质 %s → 跳过 %d 个顶点" % (_FS, len(_skip_v)))
for v in mesh.data.vertices:
    if v.index in _skip_v:
        continue
    p = mesh.matrix_world @ v.co
    for i in range(3):
        mn[i] = min(mn[i], p[i]); mx[i] = max(mx[i], p[i])
if mn.x > mx.x: # 全被跳过时的兜底
    mn = Vector((1e9,) * 3); mx = Vector((-1e9,) * 3)
    for v in mesh.data.vertices:
        p = mesh.matrix_world @ v.co
        for i in range(3):
            mn[i] = min(mn[i], p[i]); mx[i] = max(mx[i], p[i])
H = mx.z - mn.z
cz = (mn.z + mx.z) / 2; cx = (mn.x + mx.x) / 2
log("包围盒 高 %.3f m z %.3f..%.3f x %.3f..%.3f" % (H, mn.z, mx.z, mn.x, mx.x))
_PROG["bbox"] = {"h": round(H, 4), "z0": round(mn.z, 4), "z1": round(mx.z, 4),
                 "x0": round(mn.x, 4), "x1": round(mx.x, 4)}
prog("camera", 0.60)

Wp, Hp = (int(x) for x in RES.split("x"))
sc.render.resolution_x, sc.render.resolution_y = Wp, Hp
sc.render.resolution_percentage = 100
cam_d = bpy.data.cameras.new("cam"); cam_d.lens = 50.0; cam_d.sensor_width = 36.0
cam = bpy.data.objects.new("cam", cam_d)
sc.collection.objects.link(cam); sc.camera = cam
cam_d.clip_start = 0.01
cam_d.clip_end = 200.0

ORTHO = os.environ.get("ORTHO", "0") == "1"
_M = float(os.environ.get("ORTHO_MARGIN", "1.06"))
ORTHO_S = max(H * _M, (mx.x - mn.x) * _M * (Hp / Wp))
AZ = {"front": 0.0, "back": 180.0, "side": -90.0, "side2": 90.0}
SHOTS = [s.strip() for s in os.environ.get("SHOTS", "front").split(",") if s.strip()]
PREFIX = os.environ.get("PREFIX", NAME)

# 取景：face/half 是特写，其余是全身
if FRAME == "face":
    cam_d.lens = 70.0; dist = H * 0.60; aim = mn.z + H * 0.90
    ORTHO_S *= 0.34
elif FRAME == "half":
    dist = H * 1.15; aim = mn.z + H * 0.72
    ORTHO_S *= 0.62
else:
    dist = H * 1.75; aim = cz

# 边缘锐度：Blender 默认 filter_size=1.5px 会把 AA 糊宽；1.0 更接近原画线稿。
try:
    sc.render.filter_size = 1.5 if IS_MMD else _f("FILTER", "1.0")
except Exception:
    pass
sc.render.image_settings.file_format = "PNG"
for view in SHOTS:
    th = math.radians(AZ.get(view, 0.0))
    cam.rotation_euler = (math.radians(90), 0.0, th)
    d = dist
    if ORTHO:
        cam_d.type = "ORTHO"
        cam_d.ortho_scale = ORTHO_S
        d = max(dist, H * 3.0)
    else:
        cam_d.type = "PERSP"
    cam.location = (cx + d * math.sin(th), -d * math.cos(th), aim)
    sc.render.filepath = os.path.join(OUTDIR, "%s_%s%s.png" % (
        PREFIX, FRAME if view == "front" else view,
        "" if view == "front" else ""))
    log("渲染 视角=%s 正交=%s 距离 %.2f 对准 z=%.3f → %s" % (
        view, ORTHO, d, aim, os.path.basename(sc.render.filepath)))
    _i = SHOTS.index(view)
    prog("render", 0.60 + 0.38 * (_i / max(len(SHOTS), 1)),
         "渲染 %s (%d/%d)" % (view, _i + 1, len(SHOTS)))
    bpy.ops.render.render(write_still=True)
    # ---- 内线：法线 / 深度 / 材质ID 三通道，Sobel 出线压回原图（LOOK_INNER>0 才做）
    if LOOK_INNER > 0 and not IS_MMD:
        _colp = sc.render.filepath
        _np = _colp[:-4] + "_n.png"
        _dp = _colp[:-4] + "_d.png"
        _ip = _colp[:-4] + "_i.png"
        _orig_mats = None
        _shell_hidden = None
        try:
            _vl = sc.view_layers[0]
            # ① 法线图（表面朝向突变 → 发丝/褶线）
            if _inner_nmat is None:
                _inner_nmat = _make_normal_mat()
            _vl.material_override = _inner_nmat
            sc.render.filepath = _np
            bpy.ops.render.render(write_still=True)
            # ② 深度图（前后遮挡 → 轮廓；薄件相压处也不会断）
            if LOOK_INNER_D > 0:
                _dmat = _make_depth_mat(1.0 / max(0.5, (d + 2.0 * H)))
                _vl.material_override = _dmat
                sc.render.filepath = _dp
                bpy.ops.render.render(write_still=True)
            _vl.material_override = None
            # ③ 材质ID图（语义边界 → 最连续的线）。要临时换掉每个槽的材质
            if LOOK_INNER_ID > 0:
                _orig_mats = [s.material for s in mesh.material_slots]
                _grp = _id_groups(mesh)
                _idm = _make_id_mats(_grp)
                for _k in range(len(_orig_mats)):
                    mesh.data.materials[_k] = _idm[_k]
                try:
                    if "shell" in globals() and shell is not None:
                        _shell_hidden = shell.hide_render
                        shell.hide_render = True
                except Exception:
                    pass
                sc.render.filepath = _ip
                bpy.ops.render.render(write_still=True)
                for _k, _m in enumerate(_orig_mats):
                    mesh.data.materials[_k] = _m
                _orig_mats = None
                if _shell_hidden is not None:
                    try:
                        shell.hide_render = _shell_hidden
                    except Exception:
                        pass
                    _shell_hidden = None
            sc.render.filepath = _colp
            if _inner_line(_colp, _np,
                           _dp if LOOK_INNER_D > 0 else None,
                           _ip if LOOK_INNER_ID > 0 else None):
                log(" 内线已叠加 → %s" % os.path.basename(_colp))
        except Exception as _e:
            log(" 内线失败（跳过）:", _e)
        finally:
            # 无论成功失败都要收尾：还原材质、恢复描边壳、清材质覆盖、还原路径、删中间产物
            try:
                if _orig_mats is not None:
                    for _k, _m in enumerate(_orig_mats):
                        mesh.data.materials[_k] = _m
                if _shell_hidden is not None and "shell" in globals() and shell is not None:
                    shell.hide_render = _shell_hidden
            except Exception:
                pass
            try:
                sc.view_layers[0].material_override = None
            except Exception:
                pass
            sc.render.filepath = _colp
            for _t in (_np, _dp, _ip):
                try:
                    if os.path.exists(_t):
                        os.remove(_t)
                except Exception:
                    pass
    _PROG.setdefault("outputs", []).append(sc.render.filepath)
log("全部出图完成，共 %d 张" % len(SHOTS))
prog("done", 1.0, "完成，共 %d 张" % len(SHOTS))

# ---------------------------------------------------------------- 可选：把工程存成 .blend
# SAVE_BLEND=<路径> 存盘目标
# BLEND_KEEP_CAM=0 存盘时删掉相机与世界（纯素材，供 append 进别人场景）
# BLEND_PACK=0 不打包贴图（默认打包，便于整体搬走）
BLEND = os.environ.get("SAVE_BLEND", "")
if BLEND:
    KEEP_CAM = os.environ.get("BLEND_KEEP_CAM", "1") == "1"

    # ---- 归集：模型放一个集合，预览用物（相机等）放另一个，便于整体 append / 一键排除
    # ★★ 物理对象绝不能搬（实测）：MMD Tools 把刚体挂在 RigidBodyWorld、关节挂在
    # RigidBodyConstraints，而 scene.rigidbody_world.collection 指向前者。
    # 一旦把刚体搬进别的集合，物理世界就空了 ⇒ 物理静默失效（刚体都在，但一动不动）。
    _PHYS_COLS = {"RigidBodyWorld", "RigidBodyConstraints"}

    def _is_physics(ob):
        if getattr(ob, "rigid_body", None):
            return True
        if any(c.type.startswith("RIGID_BODY") for c in ob.constraints):
            return True
        return any(c.name in _PHYS_COLS for c in ob.users_collection)

    def _move_to(ob, col):
        for c in list(ob.users_collection):
            c.objects.unlink(ob)
        col.objects.link(ob)

    col_model = bpy.data.collections.new(NAME)
    sc.collection.children.link(col_model)
    col_prev = bpy.data.collections.new("_预览用（可删）")
    sc.collection.children.link(col_prev)
    for ob in list(bpy.data.objects):
        if _is_physics(ob):
            continue # ★ 物理对象留在它自己的集合里
        _move_to(ob, col_prev if ob.type in ("CAMERA", "LIGHT") else col_model)
    for c in list(sc.collection.children):
        if c.name in _PHYS_COLS:
            continue # ★ 物理集合永不删
        if c.name not in (col_model.name, col_prev.name) and not c.objects and not c.children:
            sc.collection.children.unlink(c)
            bpy.data.collections.remove(c)
    log("集合归集完成：%s / %s（物理对象保持原集合）" % (col_model.name, col_prev.name))

    # ★★ 物理必须在导入后立刻 build_rig
    # （= MMD Tools 侧栏 Model Setup > Assembly 里的 **Physics** 按钮）
    # 不做的后果（实测）：刚体和关节都在、但**骨骼完全不跟刚体走**（物理贡献 0.0000）；
    # 若绕过它直接烘焙，会得到爆掉的模拟（最大骨骼位移 1.09 m）。
    # build_rig 会额外加入「非碰撞距离约束」来近似 MMD 的软约束：
    # 做之前 1.0889 → 做之后 0.4175（distance_scale=1.5），画面恢复正常。
    # ⚠ 必须在**导入动作之前**做（官方教程明确要求）。
    if os.environ.get("PHYSICS") == "1" and os.environ.get("PHYS_BUILD", "0") == "1":
        _root = next((_o for _o in bpy.data.objects
                      if hasattr(_o, "mmd_root") and getattr(_o.mmd_root, "name", "")), None)
        if _root is not None and not getattr(_root.mmd_root, "is_built", False):
            try:
                for _o in bpy.data.objects:
                    _o.select_set(False)
                _root.select_set(True)
                bpy.context.view_layer.objects.active = _root
                _ds = float(os.environ.get("PHYS_BUILD_SCALE", "1.5"))
                bpy.ops.mmd_tools.build_rig(non_collision_distance_scale=_ds,
                                            collision_margin=1e-06)
                log("物理已 build_rig（non_collision_distance_scale=%.2f）" % _ds)
            except Exception as _e:
                log("build_rig 失败:", _e)

    # ★ 物理世界要显式打开：MMD 导入后 rigidbody_world.enabled 是 False，
    # 存进 .blend 就是"刚体都在、但一动不动"。
    if (os.environ.get("PHYSICS") == "1"
            and os.environ.get("PHYS_ENABLE", "0") == "1"
            and sc.rigidbody_world):
        _rbw = sc.rigidbody_world
        _rbw.enabled = True
        log("物理世界已启用（刚体 %d / 关节 %d）" % (
            len(_rbw.collection.objects) if _rbw.collection else 0,
            len(bpy.data.collections["RigidBodyConstraints"].objects)
            if "RigidBodyConstraints" in bpy.data.collections else 0))

    # ★★ 物理稳定化（legacy，⚠ 默认【关】PHYS_FIX=0）
    # 做法：把所有关节的 disable_collisions 打开。
    # ⚠ 实测结论：在示例模型上让 97 根骨骼的位移【变大】，方向是错的。
    # 保留它只是为了"什么都没删"，默认不要开；正解是 build_rig（见 G8）。
    if os.environ.get("PHYSICS") == "1" and os.environ.get("PHYS_FIX", "0") == "1":
        _nj = 0
        for _o in bpy.data.objects:
            if getattr(_o, "mmd_type", "") == "JOINT":
                _rbc = getattr(_o, "rigid_body_constraint", None)
                if _rbc is not None:
                    _rbc.disable_collisions = True
                    _nj += 1
        log("物理稳定化（legacy）：%d 个关节已关闭两端自碰撞（实测更糟，仅留档）" % _nj)


    # ---- 清掉零引用的材质与贴图（插件原始材质、默认材质），免得文件里两套材质分不清
    _used = set()
    for ob in bpy.data.objects:
        if ob.type == "MESH":
            _used.update(s.material for s in ob.material_slots if s.material)
    _drop = [m for m in bpy.data.materials if m not in _used and m.users == 0]
    for m in _drop:
        bpy.data.materials.remove(m)
    _left = [m.name for m in bpy.data.materials if m not in _used]
    log("清理零引用材质 %d 个，剩余 %d 个%s" % (
        len(_drop), len(bpy.data.materials),
        ("；仍被引用的旧材质: %s" % _left) if _left else ""))
    _iu = [i for i in bpy.data.images
           if not i.packed_file and i.size[0] and i.users == 0]
    for i in _iu:
        bpy.data.images.remove(i)

    # ---- 把「材质形态」指向重建后的新材质，并给 Alpha 加驱动器
    # 用驱动器把 mmd_material.alpha 接到组输入上，改了立刻生效（也才能被形态/动画驱动）
    _renamed = {}
    for _m in bpy.data.materials:
        if _m.name.endswith("_mmd") and bpy.data.materials.get(_m.name[:-4]):
            _renamed[_m.name[:-4]] = _m.name
    _fix_morph = 0
    for _ob in bpy.data.objects:
        _mr = getattr(_ob, "mmd_root", None)
        if not _mr:
            continue
        for _mo in _mr.material_morphs:
            for _d in _mo.data:
                _new = _renamed.get(_d.material)
                if _new:
                    _d.material = _new
                    _fix_morph += 1
    log("材质形态重新指向新材质 %d 处" % _fix_morph)

    _drv = 0
    # 「材质节点树 ←→ 材质属性」的循环依赖，被驱动的 Alpha 会求值成 0
    if os.environ.get("BLEND_DRIVER", "0") == "1":
        for _m in bpy.data.materials:
            _mm = getattr(_m, "mmd_material", None)
            if not _mm or not _m.node_tree:
                continue
            if _m.node_tree.animation_data and _m.node_tree.animation_data.drivers:
                continue
            _grp = next((n for n in _m.node_tree.nodes
                         if n.bl_idname == "ShaderNodeGroup"), None)
            if not _grp:
                continue
            _sock = _grp.inputs.get("Alpha")
            if not _sock:
                continue
            try:
                _fc = _sock.driver_add("default_value")
                _dv = _fc.driver
                _dv.type = "SCRIPTED"
                _v = _dv.variables.new(); _v.name = "a"; _v.type = "SINGLE_PROP"
                _t = _v.targets[0]
                _t.id_type = "MATERIAL"
                _t.id = _m
                _t.data_path = "mmd_material.alpha"
                _dv.expression = "a"
                _drv += 1
            except Exception as _e:
                log(" 驱动器失败 %s: %s" % (_m.name, _e))
    log("Alpha 驱动器已加 %d 个（BLEND_DRIVER=1 才开）" % _drv)

    if os.environ.get("BLEND_PACK", "1") == "1":
        try:
            bpy.ops.file.pack_all()
            log("贴图已打包")
        except Exception as e:
            log("打包贴图失败:", e)
    if not KEEP_CAM:
        for ob in [o for o in list(bpy.data.objects) if o.type in ("CAMERA", "LIGHT")]:
            bpy.data.objects.remove(ob, do_unlink=True)
        sc.camera = None
        sc.world = None
        log("已移除相机 / 灯光 / 世界（纯素材模式）")
    # ★ 物理工程不能把帧范围压成 1 帧，否则下游没法做物理预热 / 动画。
    # BLEND_FRAMES="start,end" 可显式指定；不给时按是否带物理自动选。
    _bf = os.environ.get("BLEND_FRAMES", "")
    if _bf:
        sc.frame_start, sc.frame_end = (int(x) for x in _bf.split(","))
        log("帧范围（BLEND_FRAMES）→ %d..%d" % (sc.frame_start, sc.frame_end))
    elif os.environ.get("PHYSICS") == "1" and os.environ.get("PHYS_ENABLE", "0") == "1":
        sc.frame_start = 1
        sc.frame_end = max(int(sc.frame_end), 250)
        log("帧范围 → 1..%d（含物理，留给下游做预热/动画）" % sc.frame_end)
    else:
        sc.frame_start = 1
        sc.frame_end = 1
        log("帧范围 → 1..1（无物理，纯静帧素材）")
    if (os.environ.get("PHYSICS") == "1"
            and os.environ.get("PHYS_ENABLE", "0") == "1"
            and sc.rigidbody_world):
        try:
            sc.rigidbody_world.point_cache.frame_start = sc.frame_start
            sc.rigidbody_world.point_cache.frame_end = sc.frame_end
        except Exception:
            pass
    # 存盘分辨率给个正常默认（不继承临时渲染用的低分辨率）
    _br = os.environ.get("BLEND_RES", "")
    if _br:
        sc.render.resolution_x, sc.render.resolution_y = (int(x) for x in _br.split("x"))
    os.makedirs(os.path.dirname(os.path.abspath(BLEND)) or ".", exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(BLEND), compress=True)
    log("工程已存 →", os.path.abspath(BLEND))

