# KomaFlow 架构大框

> **这份文档是什么**：本项目的架构说明：分层、接口与数据流。
> **当前状态**：全链路**已跑通并实测**（装配 → 预热 → 分段渲染 → 出 MP4），
> 界面可用。K-1.8 已定论，见第 11 节。

---

## 0. 概览

| 层 | 模块 | 状态 |
| --- | --- | --- |
| 核心逻辑层 | `core/segment.py` 分段 · 帧号 · 预热 · 抽帧 | ✅ **已实现，有测试** |
| 核心逻辑层 | `core/errors.py` 错误分类与人话文案 | ✅ **已实现** |
| 核心逻辑层 | `core/project.py` 工程档 schema + 校验 | ✅ **已实现，有测试** |
| 核心逻辑层 | `core/verify.py` 产物校验 · 原子写入 | ✅ **已实现** |
| 核心逻辑层 | `core/pipeline.py` 编排（装配 / 分段 / 续渲 / 重试 / 拼接 / 校验） | ✅ **已实现并实测跑通** |
| 外层 | `adapters/executor.py` Blender 子进程执行器（进度文件 · 心跳 · 取消） | ✅ **已实现** |
| 外层 | `adapters/discovery.py` Blender 自动探测 | ✅ **已实现** |
| 媒体 | `media/ffmpeg.py` ffprobe 探测 + 流拷贝拼接 | ✅ **已实现** |
| Blender 适配层 | `adapters/blender5.py` 协议 + 5.x 适配器 + `probe` | ✅ **已实现** |
| Blender 侧脚本 | `scripts/probe.py` · `build.py` · `render.py` | ✅ **已实现** |
| 界面层 | `ui/app.py` 单窗口表单式 | ✅ **已实现（tkinter）** |
| 命令行 | `cli.py` `discover` / `adapters` / `plan` / `samples` / `stages` / `run` | ✅ **可跑** |

**实测过的一件事**（比任何设计都重要）：用真实素材
（示例：某 PMX 人物 + 某 VMD 动作）从命令行与界面各跑通一次，
产出可播放的 MP4；复跑时命中"装配复用 + 段级续渲"，1 秒内重新出片。

---

## 1. 技术选型（已定）

| # | 议题 | 建议 | 理由 | 备选与代价 |
| --- | --- | --- | --- | --- |
| 1 | 语言 | **Python 3.12（仅标准库）** | Blender 侧脚本本来就是 Python，同语言可共享逻辑；NFR-16 禁止 pip 依赖 → 只能 stdlib | C#/.NET 亦可做原生 EXE，但与 Blender 脚本无法共享代码 |
| 2 | 界面 | **tkinter**（已实测 Tk 8.6 可用） | **唯一不违反 NFR-16"禁止 pip 依赖"的 GUI 方案**，随 Python 自带 | PySide6 更好看，但需 pip → 违反 NFR-16，除非改为"随包 vendored"并登记台账 |
| 3 | 打包 | **PyInstaller 6.22.3**（已装） | NFR-2 要求独立 EXE 自带运行时 | 注意：PyInstaller 是**构建工具**，不是运行时依赖，不违反 NFR-16 |

> 已按上表建议搭好骨架；若要改选型，**改动面局限在 `ui/` 与打包脚本**，核心层不受影响。

---

## 2. 三层架构

```
┌──────────────────────────────────────────────────────────────┐
│ ② 界面层 ui/ │
│ 单窗口表单式（§8）。只做展示与输入。 │
│ ❌ 不得直接调用 Blender │
└───────────────────────────┬──────────────────────────────────┘
                            │ 调用核心层接口
┌───────────────────────────▼──────────────────────────────────┐
│ ① 核心逻辑层 core/ │
│ 装配编排 · 分段 · 预热偏移 · 进度 · 产物校验 · 错误分类 │
│ ✅ 零 GUI 依赖，可在命令行单独跑 │
│ ❌ 零 Blender 依赖，不得出现版本判断 │
└───────────────────────────┬──────────────────────────────────┘
                            │ 经适配层接口
┌───────────────────────────▼──────────────────────────────────┐
│ ③ Blender 适配层 adapters/ │
│ 所有版本相关的调用、枚举名、API 差异集中于此 │
│ 将来支持 4.2 LTS 只需新增一个适配器，**不动核心层** │
└───────────────────────────┬──────────────────────────────────┘
                            │ subprocess（命令行 + 文件交互）
                    ┌───────▼────────┐
                    │ 用户本地的 │
                    │ Blender 5.x │ ← 不自带、不打包（设计取舍）
                    │ + mmd_tools │
                    └────────────────┘
```

**为什么必须这样切**：设计取舍/32 把"只支持 5.x、4.2 预留"定成硬约束。若版本判断散落在业务代码里，将来加 4.2 就是全局手术；收进适配层后，只加一个类。

---

## 3. 目录结构

```
KomaFlow/ ← 仓库根
├── docs/
│ └── architecture.md ← 本文
├── src/
│ └── KomaFlow/
│ ├── branding.py 产品名唯一出处
│ ├── cli.py 命令行入口（L1 阶段的主要界面）
│ ├── core/ ① 核心逻辑层
│ │ ├── segment.py 分段 / 帧号 / 预热 / 抽帧（纯函数）
│ │ ├── errors.py 错误分类 + 人话文案
│ │ ├── project.py 工程档 schema + 校验
│ │ ├── verify.py 产物校验 + 原子写入
│ │ └── pipeline.py 编排流程（骨架）
│ ├── adapters/ ③ Blender 适配层
│ │ ├── base.py 适配器协议
│ │ ├── blender5.py Blender 5.x 适配器
│ │ ├── registry.py 适配器注册与选择
│ │ └── scripts/ 注入 Blender 执行的脚本（待写）
│ └── ui/ ② 界面层（待写）
├── tests/
│ └── unit/ 纯逻辑单元测试
├── .gitignore
└── 
```

**两点说明**

1. ** 要求文件名不得用中文**。仓库根现有 3 份中文名文档是历史产物；正式开源前需统一为英文名。**本次未改动**，等你确认后再动。
2. ** 要求的 README / LICENSE / CHANGELOG / CONTRIBUTING 尚未创建**。LICENSE 采用 MIT。

---

## 4. 模块职责（单一职责，）

| 模块 | 职责 | 明确不做 |
| --- | --- | --- |
| `core/segment.py` | 分段计算、帧号换算、预热偏移、抽帧规则 | 不碰文件、不碰 Blender |
| `core/errors.py` | 错误归口、人话文案、修复建议 | 不决定要不要重试（调用方决定） |
| `core/project.py` | 工程档 schema、序列化、参数体检 | 不执行渲染 |
| `core/verify.py` | 产物是否存在/完整、原子写入 | 不解析视频（那需要 Blender，属适配层） |
| `core/pipeline.py` | 把各阶段串起来，决定跳过哪些段 | 不实现阶段本身 |
| `adapters/*` | 探测 Blender、生成并执行脚本、解析回传 | 不做业务决策 |
| `ui/*` | 展示与输入 | 不直接调 Blender |

---

## 5. 接口契约

### 5.1 工程档（`.kfproj`，JSON，schema=1）

保存"素材引用 + 全部参数"，可反复打开修改（FR-1.5）。骨架字段：

```json
{
  "schema": 1,
  "name": "untitled",
  "inputs": {
    "route": "assets",
    "model": "<素材目录>\\人物.pmx",
    "motion": "<素材目录>\\动作.vmd",
    "camera": null,
    "stage": null,
    "pose": null,
    "audio": null,
    "blend": null
  },
  "params": {
    "preroll_frames": 30,
    "segment_limit": 1000,
    "frame_start": 0, "frame_end": null,
    "fps": 30, "width": 2560, "height": 1440,
    "orientation": "landscape",
    "camera_mode": "vmd", "framing": "full",
    "quality": "standard", "crf": null, "bitrate": null,
    "strip_preroll": false
  },
  "output": { "dir": "output", "basename": "KomaFlow" },
  "blender_exe": null
}
```

`route` 只有两种：`assets`（PMX/VMD 路线 A）与 `blend`（路线 B）。

### 5.2 分段与帧号（已实现，纯函数）

| 函数 | 规则来源 |
| --- | --- |
| `plan_segments(total, limit)` | K-2.1~K-2.3：自第 0 帧起按上限切，末段不补齐；`limit=None` → 不分段（K-2.4） |
| `project_total_frames(motion_len, preroll)` | K-2 示例：动作 1500 + 预热 30 = 工程 1530 帧 |
| `vmd_to_scene_frame(vmd_frame, preroll)` | K-1.7：场景帧 = VMD帧 + N |
| `motion_start_frame(preroll)` | K-9.1：音频起点 = 动作生效帧 = 第 N 帧 |
| `sample_policy(total)` / `pick_sample_frames(total, n)` | 设计取舍 + K-3.1：分层均匀、覆盖首尾、确定性 |

**全部分段用例已写成测试**（800 / 1000 / 1001 / 1530 / 3000 / 5000 / 7000），见 `tests/unit/test_segment.py`。

### 5.3 适配器协议

```python
class BlenderAdapter(ABC):
    key: str # "blender5"
    supported_min: VersionTuple # (5, 0)
    supported_max: VersionTuple # (5, 99) ← 6.0 会被拒绝（R11）
    is_baseline: bool # 统一基准版本标记

    # —— 版本差异常量（附录 F 的全部内容都收在这里）——
    engine_eevee: str # "BLENDER_EEVEE"（4.2 才是 _NEXT）
    needs_media_type_first: bool # 出 MP4：先 media_type 再 file_format
    material_transparency_attr: str # "surface_render_method"
    compositor_attr: str # "compositing_node_group"

    def supports(version) -> bool
    def probe(exe) -> BlenderInfo # 探测版本 + mmd_tools
    def script(name) -> Path # 取注入 Blender 的脚本
```

核心层**永远**通过 `registry.select_adapter(version)` 拿适配器，绝不自己判断版本。

### 5.4 进度回传协议（K-10.3：以文件为准，不解析 Blender 输出）

Blender 侧脚本在 `bpy.app.handlers.render_post` 里每 25~30 帧覆写一次 `progress.json`：

```json
{ "segment": 1, "segment_total": 3,
  "frame_done": 1430, "frame_total": 3000,
  "seconds_per_frame": 3.8, "eta_seconds": 5966,
  "blender_pid": 12345, "written_at": 1792000000.0 }
```

工具侧监视**两件事**：进度文件的新鲜度（心跳，）+ 子进程存活（K-10.4）。
超时无更新 ⇒ 判定无响应 ⇒ 触发"重试 1 次"（FR-8.1）。

### 5.5 段产物与"完成"判定（K-2.6 ~ K-2.8）

- 路径：`<work_dir>/segments/seg_0001_001000-001999.mp4`
- 写入**原子化**：先写 `.part`，完成后改名 ⇒ 半成品不会被误判为已完成
- **判定只看产物**，绝不看 Blender 退出码（设计取舍 / FR-8.10）：
  1. 文件存在且体积 > 阈值
  2. 帧数与时长与预期一致
  3. 可播放（需 Blender 侧 `bpy.data.movieclips.load()`，属适配层）

### 5.6 错误分类（FR-8.5，已实现）

13 类归口，每类带**人话标题 + 修复建议**，且**只给建议、绝不自动改用户设置**（K-6.4）：

`blender_missing` · `blender_version_unsupported` · `mmd_tools_missing` · `blend_version_too_new`(FR-1.9) · `asset_corrupt` · `texture_missing` · `out_of_vram` · `out_of_memory` · `out_of_disk` · `physics_failed` · `frame_failed` · `user_cancelled` · `render_process_died`

---

## 6. 一次渲染的数据流

```
① prepare 体检：Blender 版本/适配器、mmd_tools、素材存在性、
             磁盘空间、分辨率与显存（K-6.1~K-6.4）→ 不合格就阻断并说人话
② assemble 导入素材 → 挂动作/镜头 → 物理装配 → 预热偏移
             （K-1；**是否需主动施加 +N 偏移（K-1.8 已定论：不施加，见 §11）**）
③ plan 算工程总帧数 → plan_segments() → 段清单
④ 段循环 对每段：
             ├─ 已完成？→ 跳过（K-2.7 段级续渲）
             ├─ 起一个 Blender 进程，连续渲完整段（K-10.9，绝不每帧启停）
             ├─ 读 progress.json 更新进度与 ETA（K-5.1/K-5.2）
             ├─ 失败 → 自动重试 1 次（FR-8.1）
             └─ 校验产物 → 原子改名落定
⑤ concat 各段同参数 → 流拷贝拼接，不重编码（K-2.10）
⑥ verify 成片总帧数/时长/可播放性校验（FR-8.4）
⑦ notify 完成/失败系统通知（K-5.5）
```

**崩溃时的最坏损失 = 正在渲的那一段**（默认 1000 帧 ≈ 1.2 小时），已完成的段全部保留。

---

## 7. 阶段目标

> 目标：命令行跑通「**素材 → 装配 → 30 帧预热 → 渲 60 帧 → 出 MP4**」。

| # | 验收项 | 判定方式 |
| --- | --- | --- |
| 1 | **K-1.8 已验（见 §11）** | 用 `--factory-startup` + 只启用 mmd_tools，导入一个 VMD，打印 action `frame_range`；确认 +30 是插件行为还是 mmd_tools 行为 |
| 2 | 适配器 `probe` 可用 | 能列出本机 Blender 并标注基准版本（设计取舍：多版本时用户自己选） |
| 3 | 装配脚本可跑 | 空 Blender → `open_mainfile`/导入 → 挂动作 |
| 4 | 渲 60 帧出 MP4 | 产物校验通过（不看退出码） |
| 5 | 进度文件可读 | 每 25~30 帧更新一次，ETA 收敛 |

---

## 8. 边界与未完成项

| 没做 | 为什么 / 去向 |
| --- | --- |
| 抽帧取样预览的**批量抽帧与网格查看器**（K-3 / FR-4.1~4.2） | 已有"指定帧单渲"（FR-4.3）；批量抽帧与缩略图网格待做 |
| 暂停 / 继续（K-5.4） | 现在支持**中断**（段级安全，已完成的段保留）；暂停需与 Blender 进程协商，待做 |
| 系统托盘与"关窗口继续跑"（K-10.5） | 待做 |
| 渲染完成/失败的**系统通知与提示音**（K-5.5 / K-10.8） | 界面内弹窗已有；系统级通知待做 |
| 显存/内存/磁盘的**渲前预检**（K-6.1~6.2） | 现在只做不需要 Blender 的静态体检；显存探测待做 |
| 音频合流（K-9） | 已由「V1.0 冻结范围」推到 1.5 |
| PNG 序列 / MOV 导出 | 同上，1.5 |
| 角色模板存档（K-4）、骨骼手动映射（FR-2.2） | 待做；当前会对失配骨骼给出清单，但不提供手动补齐 |
| LICENSE / CHANGELOG / CONTRIBUTING | LICENSE：MIT |
| 打包成 EXE | 装好 PyInstaller 后一步之遥，待做 |

---

## 9. 需求条目 → 代码落点对照

| 需求 | 落点 |
| --- | --- |
| K-1 预热 | `core/segment.py` + `core/pipeline.py`（装配侧见 K-1.8（已定论）） |
| K-2 分段与续渲 | `core/segment.py` + `core/verify.py` + `core/pipeline.py` |
| K-3 抽帧 | `core/segment.py` |
| K-5 进度 | `core/pipeline.py` + 进度文件协议（5.4） |
| K-6 预检与人话报错 | `core/errors.py` + `core/project.py` 的 `validate()` |
| K-7 Blender 探测与版本 | `adapters/base.py` + `adapters/registry.py` |
| K-10 进程调用 | `core/pipeline.py` + `adapters/scripts/` |
| FR-1.8 不支持 `.x` | `core/project.py` `validate()` |
| FR-1.9 工程版本检测 | `core/errors.py` 的 `blend_version_too_new`（实现待 L1） |
| FR-8.4 / FR-8.10 产物校验 | `core/verify.py` |
| FR-8.5 错误分类 | `core/errors.py` |
| E-1.x 三层 | 目录结构 + 各模块导入边界 |
| E-2.x 基准版本与适配层 | `adapters/*` |
| 单元测试 | `tests/unit/` |
| 原子写入 | `core/verify.py` |

---

## 10. 已知待办

| # | 事项 | 状态 |
| --- | --- | --- |
| 1 | UI 选 tkinter 还是破例引 PySide6 | **待定** |
| 2 | 仓库根就用 `KomaFlow/`、现有中文名文档是否改名 | **待定** |
| 3 | 产品定名→ 改 `branding.py` 一行 | 待用户 |
| 4 | 开源协议→ 才能建 LICENSE | 待用户 |
| 5 | **K-1.8 定论** → 决定预热要不要自己写偏移逻辑 | **开工第一件事** |
| 6 | H.264/H.265 编解码专利 | 发布前必做 |

---

## 11. ★ 实测结论（踩出来的，别再踩）

### 11.1 K-1.8 定论 —— 预热偏移由谁施加

mmd_tools 4.5.7 的帧号公式是

```python
frame = vmd_frame + scene.frame_current + frame_margin
# frame_margin 只在 scene.frame_current ∈ {0, 1} 时才被采用
```

* **默认 `margin = 0` ⇒ mmd_tools 不加任何偏移。** 原 `前30帧检查.txt` 里那个 `+30`
  **不是**插件默认行为（测试机上还装着第三方插件，当时无法区分）。
* ✅ **采用**：`scene.frame_current = N`、`margin = 0` → 关键帧整段落在 N 之后，
  第 0 ~ N-1 帧处于首个关键帧之前，由 Blender 常量外推 ⇒ **真正冻住**（K-1.2），
  物理仍正常运算（K-1.4）。
* ❌ **不要用** `margin = N` + `frame_current = 0`：mmd_tools 会在第 0 帧补一个
  **静止姿势**关键帧并带 BEZIER 插值，预热段变成"从静止姿势缓慢过渡到第一个动作姿势"，
  违反 K-1.2（已实测确认）。
* 骨骼 / 相机 / 光照 / 表情 / 显示走**同一行偏移代码**，所以动作与镜头的相对关系
  天然不变（K-1.3），相机文件用同一种方式导入即可。

**实测对照**：帧 0 / 10 / 20 / 29 / 30 位移全部 `0.00000`，帧 31 起才动
—— 与原始证据 `前30帧检查.txt` 逐帧一致。

### 11.2 插件隔离（K-10.10）实测可行

`--factory-startup` + 脚本内 `addon_utils.enable("bl_ext.blender_org.mmd_tools")`
完全可用，`bpy.ops.mmd_tools` 算子齐全。

对照：不加 `--factory-startup` 时，这台机器会加载 `AnimCharTools`、`MBTs-*`（7 个）、
`blender_mcp_addon`、`MikuMikuImport_Pro`、`bl_ext.blender_org.f2`
—— 这正是"退出码变成 1"以及"导入行为可能被改写"的来源。**执行器一律带这个开关。**

### 11.3 启动开销要修正

纯后台启动 **约 1.5 秒**；需求书里的 17.3 秒是"启动 **+ 建显卡上下文**"。
分段代价表可以更乐观，但"一个进程渲完一整段"（K-10.9）仍然是刚需。

### 11.4 `import_vmd` 的三个坑

1. **必须同时传 `directory` 与 `files`**：算子 `execute()` 循环的是 `self.files`，
   程序化调用只给 `filepath` 会**静默什么都不导入**，却照样返回 `FINISHED`。
2. **`bone_mapper` 必须用 `"PMX"`**：它按 `mmd_bone.name_j`（日文名）映射。
   实测同一模型同一动作：`PMX` 命中 84 根骨骼，`BLENDER` / `RENAMED_BONES` 只命中 10 根。
3. `import_model` 的 `types` 合法值只有 `MESH / ARMATURE / PHYSICS / DISPLAY / MORPHS`；
   **不含 `PHYSICS`** 可避免生成几百个刚体球与关节辅助物（附录 C 的老坑）。

### 11.5 骨骼命名长尾是真实存在的（风险 R1）

实测该模型的 238 个动作骨骼只匹配上 84 个，但 **`critical_missing` 为空** ——
頭 / 首 / 腰 / 足ＩＫ / ひざ / 足首 / 腕 / ひじ / 手首 / 肩 **全部命中**。
缺的是该模型本身没有的内衬骨（`インナー_*`）、`HALO`、部分饰品骨。

所以工具现在的做法是**列出缺失清单 + 单独点名关键骨骼**（FR-1.4），而不是静默。
手动补齐映射（FR-2.2）与角色模板（K-4）尚未做。

### 11.6 相对路径会把整段渲染成果丢掉

管线必须用**绝对路径**。踩的坑：用相对路径时 Blender 子进程写出的产物在 Python 侧
被判成"不存在"，于是自动重试 → 白白渲了两遍才失败。
已修为 `Path.resolve()`，并加了 8 秒的"等产物落盘"重试（Windows 上刷盘/杀毒扫描会滞后）。

### 11.7 Blender 侧出片子

* 出 MP4：**先** `image_settings.media_type = "VIDEO"`、**再** `file_format = "FFMPEG"`
  （顺序反了报 enum 不存在）
* `use_file_extension = False` 才能拿到确定的段文件名
* 单帧预览：`media_type = "IMAGE"` + `file_format = "PNG"`
* 进度只写文件、只读文件，**从不解析 Blender 屏幕输出**（K-10.3）

---

## 决策记录：透明背景（用户逐条确认）

**需求**：用户不导入环境 / 舞台时，启用「不渲染背景」，输出带透明通道。

| # | 项目 | 结论 |
| --- | --- | --- |
| 1 | 语义 | **透明背景**（不是纯色、不是保留默认灰） |
| 2 | 输出 | 主交付 **MOV（带 alpha）**；**MP4 同时保留**（透明帧垫底色后编码） |
| 3 | 触发 | 舞台卡空着 → **自动开启**；放了舞台 → **变灰、不可选、且失效**（复用 `_sync_framing_state` 那套逻辑） |
| 4 | 范围 | **只在「没放舞台」这一条路径上允许动世界 / 胶片设置**；其余路径继续受设计取舍 约束（只覆盖分辨率与帧率） |
| 5 | 用户自带 `.blend` | **不替用户改**他文件里的背景设置 |
| 6 | MP4 垫底色 | **深褐 `#4A3728`** —— 取自角色设定的辅色 |

> **对第 2、5 条的落地修正**（实现时定的，用户口径未变）：
>
> * 编码器从 QTRLE 改成 **ProRes 4444 作为默认**，QTRLE 保留在下拉里可选。
> 理由：ProRes 只要前者三分之一的体积，肉眼无差，后期软件更认；
> QTRLE 在 2K 三分钟就能吃掉几十 GB。
> * 第 5 条在实现中收紧成**"自带 `.blend` 一律不开透明"**，而不是"默认关、可手动开"。
> `effective_transparent()` 对 `.blend` 路线直接返回假 —— 那条路线我们连胶片
> 设置都不该碰，"可手动开"等于把设计取舍 的边界交回给用户去突破。

### 垫底色为什么不用她的主色

主色是**枫红 `#B33A2B`**，但角色自己穿的正是枫红外套 —— 同色背景会把角色吞掉。
改用她配色里负责「压住下盘」的**深褐 `#4A3728`**：暖、暗、不抢主体。

> 换色只改一处常量。备选：枫红 `#B33A2B` / 亚麻 `#D9C09A` / 暖橘 `#E88B3C` / 纯黑。

### 实现落点（**已做完**）

| # | 落在哪 | 说明 |
| --- | --- | --- |
| 1 | `core/project.py` | `Params.transparent_background` / `alpha_codec`；派生规则 `transparent_allowed()` / `effective_transparent()` / `alpha_codec_name()`；互斥校验 |
| 2 | `adapters/scripts/build.py` | `_set_transparent_background()`：设 `film_transparent` **并把世界背景压到全黑**（只设胶片不压世界，透明区会挂一层灰） |
| 3 | `core/pipeline.py` + `core/verify.py` | 分段产物改 **PNG 序列（RGBA）**；续渲判据改**数 PNG 张数 + 查首尾两张结构完整**；`_encode_png_tracks()` 出 MOV + MP4 |
| 4 | `media/ffmpeg.py` | `encode_mov_alpha()`（prores_ks 4444 / qtrle）、`encode_mp4_on_color()`（垫底色）、`concat_streams()` |
| 5 | `ui/app.py` | 「不渲染背景」开关 + 「透明格式」下拉；放了舞台自动灰掉失效；`on_start` 里接 `budget.preflight()`，**盘不够会拦住** |

验收：`work/dryrun_transparent.py`（不需要 Blender 的端到端干跑，造假的 PNG 分段 →
判续渲 → 出两条轨 → ffprobe 核对）。三条轨的实测结果：

```
MOV prores / profile=4444 / yuva444p12le / 12 帧 ← alpha 在
MP4 h264 / yuv420p / 12 帧 ← 透明处是深褐
```

### 实现时踩到的四个坑（都别重踩）

1. **`ShowWindow` 那类同步派发不能出现在窗口过程里** —— 见下文的实测结论
   的 ⛔ 一节（`_restore_taskbar_button`，会让进程栈溢出崩掉）。
2. **垫底色合成 MP4 会掉帧**：``color=...:d=1`` + ``overlay=shortest=1`` 是按**最短**
   输入收尾，那个纯色源只有 1 帧，于是最后一张被砍掉（6 张输入出 5 帧）。
   改成**同一份序列喂两路**（一路 `colorchannelmixer=aa=0` 当底）就对了，
   顺带尺寸也自动对齐（早先用 `scale2ref` 出片只有 16×16）。
3. **`-filter_complex_script` 在 ffmpeg 9 已被删掉**。用 `-filter_complex` 传参数列表，
   shell 不参与，没有转义问题。
4. **判"PNG 写完没写完"不要用体积阈值**：小分辨率下会误判
   （实测 160×120 的合法帧只有 400 字节，被 1024 的阈值打成残片）。
   改看**结构**：开头 8 字节签名 + 结尾 `IEND` 块。

### ⚠️ 已知代价

* **QTRLE 体积**：2K 3 分钟（5400 帧）约 **20~50GB**；ProRes 4444 约 6~15GB。
  默认值已从 QTRLE 改成 **ProRes**（`DEFAULT_ALPHA_CODEC = "prores"`），
  QTRLE 仍在下拉里可选。
* **磁盘预检已经接上**（`ui/app.py` 的 `_disk_ok()`）：按**上限**算，不够就弹窗拦住，
  用户可以选"照样开始"。这是在渲之前，不会渲到一半才发现。
* 透明走 PNG 序列后，分段产物的「成功」判据已经改成
  **数 PNG 张数 + 校验首尾两张完整**（`core/verify.verify_png_sequence`）。
