# KomaFlow

[![Release](https://img.shields.io/github/v/release/YLKoKi/KomaFlow?label=download)](https://github.com/YLKoKi/KomaFlow/releases/latest)


> **直接下载（Windows 程序包）** → <https://github.com/YLKoKi/KomaFlow/releases/latest>
>
> ⚠️ 当前是**测试版（beta）**：接口与默认值可能调整，GitHub Release 标为 pre-release。
>
> ⚠️ 请不要用绿色的 `Code → Download ZIP`：那是**源码**，里面没有 exe。
> 程序包在 **Releases** 页面的 Assets 里（`KomaFlow_V1.2.0-beta.zip`，**约 12.3 MB**）。


> English: [README.en.md](README.en.md)

把 MMD 的**模型（.pmx）**和**动作（.vmd）**拖进来，调用你电脑上已装的 Blender
完成「装配 → 物理预热 → 分段渲染 → 合并出片」，输出 MP4 或带透明通道的 MOV。

Windows 独立程序（不是 Blender 插件）。**V1.2.0-beta**（测试版）。

---

## 它做什么

| 步骤 | 说明 |
|---|---|
| 装配 | 导入 PMX / 追加 `.blend` 舞台 / 挂 VMD 动作与镜头 |
| 物理 | 用 `mmd_tools` 的 `build_rig` 建骨骼跟随约束，再烘焙点缓存 |
| 预热 | 默认 30 帧静置，让头发裙摆先收敛；第 30 帧起动作才生效 |
| 渲染 | 按段渲染（每段一个 Blender 进程），断点续渲 |
| 出片 | 有 FFmpeg 时流拷贝合并；没有则单段直接落 MP4；透明走 PNG 序列 → 带 alpha 的 MOV |

## 环境要求

| 需要 | 说明 |
|---|---|
| **Windows 10 / 11 64 位** | 只支持 Windows |
| **Blender 5.0 – 5.99** | 实测 5.2.3 LTS；**不随包分发**，用你自己装的 |
| **mmd_tools 扩展** | `bl_ext.blender_org.mmd_tools`；界面里可一键触发安装 |
| **FFmpeg** | 不随包分发；可在你同意后按**固定版本下载**（校验 SHA256），也可自行安装（见下方联网说明）|
| Python 3.10+ | 仅源码运行时需要；**零 pip 依赖** |

## 使用

**方式一：发行包**

1. 下载 Release 里的 `KomaFlow_V1.2.0-beta.zip` 并解压
2. 双击 `KomaFlow.exe`
3. 拖入 `.pmx` → 拖入 `.vmd` → 点「开始渲染」

**方式二：源码运行**

```bat
python run.py
```

## 关键设计（几个常见疑问）

- **不打包 Blender、不打包 FFmpeg**：两者都是各自的许可，本项目只通过**子进程**调用，
  不分发、不链接，因此仓库里不含 GPL 代码。
- **不内置任何素材**：仓库里没有模型、动作、贴图、成片。
- **零运行时依赖**：只用 Python 标准库 + Tk。
- **观感稳定**：人物按材质本色渲染（默认不出阴影、不随动作变暗），不会因为低头/转身出现明暗漂移
- **工程可交付**：装配完成会在出图目录留一份 `.blend`（含烘焙好的物理；
  **贴图仍是指向模型目录的外链** —— 换机器打开时请一并带上模型与贴图目录），
  可以直接用 Blender 打开改材质 / 灯光 / 镜头，再回来渲。
- **骨骼对不上就不渲**：动作与模型的骨骼匹配率低于 **35%** 时，装配阶段直接中止，
  不白渲整片；报错里会写怎么改（角色卡换回原始 `.pmx`）。
  环境变量 `KOMEFLOW_ALLOW_LOW_MATCH=1` 可强制继续；35%~60% 只警告。
- **联网只有两处，都会先问你；你不同意就不联网**：
  1. 界面里的「安装 mmd_tools」→ 让 Blender 去官方扩展库下载
  2. FFmpeg：首启会问一次；之后可在「设置 → 渲染环境 → 下载 FFmpeg」手动下；
     选透明 MOV 但缺 FFmpeg 时，点「开始渲染」还会再问一次（三处共用一个下载器）
- FFmpeg 下载的是**固定版本 9.0.2**（Windows 64 位 essentials 构建），
  下完先算 SHA256，对不上就丢掉、换下一个来源；只允许 https 白名单域名。
- 下载顺序：**固定版本 → GitHub 直连 → gyan.dev → 第三方加速镜像（最后兜底）**
  （`ghfast.top` / `ghproxy.net` / `gh-proxy.com`）。
  不想让流量经过第三方，就先自己装好 FFmpeg；程序检测到可用就不再下载。
- 下载目录：`%LOCALAPPDATA%\KomaFlow\ffmpeg`（约 90 MB）。
- **不改系统**：不写注册表、不装服务、不采集遥测。

## 目录

```
src/komaflow/                      程序本体
  core/                            纯逻辑（不依赖 bpy / tk）
  adapters/                        调 Blender、调 FFmpeg
  adapters/vendor/                 MMD 静帧渲染器（MIT，见同目录 LICENSE / NOTICE）
  ui/                              Tk 界面
tests/                             356 项单元测试（实跑通过，3 项按设计跳过）
tools/                             打包（PyInstaller spec）、LOGO 生成、资源导出
docs/                              架构说明、使用细节
```

跑测试：

```bat
set PYTHONPATH=src
python -m unittest discover -s tests -t .
```

## 语言包（可选）

界面默认中文。英文语言包**不随主包附带**，单独下载安装即可：

1. 下载 `KomaFlow_语言包_英文_v1.1.zip`（Releases 页面单独附件）
2. 解压后把 `locales` 文件夹放到 `KomaFlow.exe` 同级目录
3. 重新打开程序 —— 界面变英文

卸载：删掉该文件夹即可恢复中文。也可以放到
`%APPDATA%\KomaFlow\locales\en.json`。

想自己翻译或补漏，直接改 `locales/en.json`：它就是一个
`{"中文原文": "English"}` 的扁平字典。

## 已知限制

- **未做代码签名**：exe 首次运行会被 SmartScreen 拦，需点「更多信息 → 仍要运行」。
- **未配置 CI**：测试依赖 Tk，只在 Windows 上跑过。
- **物理是近似解算**：`build_rig` 用非碰撞距离约束近似 MMD 软约束，个别模型可能穿插或偏摆。
- **只在 Blender 5.x + mmd_tools 4.5.x 上验证**。
- **FFmpeg 自动下载还没在真实网络上完整验证**：网络受限或被代理拦截时会失败，
  失败不影响只出 PNG 序列；也可以先自己装好 FFmpeg。

## 合规与素材

本项目**不含**任何模型 / 动作 / 贴图 / 成片，也**不授予**任何素材的使用权。
MMD 模型与动作的版权属于各自作者，常见规约为「禁止二次配布 / 禁止商用」——
使用前请自行阅读原作者说明。

## 关于代码生成方式

本项目的代码由 **AI 辅助生成**、经人工审阅与测试。除 `adapters/vendor/` 下的
第三方渲染器（MIT，已标注来源）外，未复制他人代码。

## 许可证

- 本项目代码：**MIT**（见 [LICENSE](LICENSE)）
- `adapters/vendor/MMD渲染.py`：**MIT**，版权归其作者，随附 `LICENSE_MMD渲染.txt`
  与 `NOTICE_MMD渲染.md`
- Blender、mmd_tools：**GPL**，本项目不分发、不修改，仅子进程调用

第三方组件清单见 [第三方来源与许可_发布版.md](第三方来源与许可_发布版.md)。

## 联系

| 渠道 | 用来做什么 |
|---|---|
| **GitHub Issue** | 日常问题、改进建议、使用报障 |
| **Issue（标注【侵权申诉】）** | 侵权申诉 / 隐私问题 / 商务合作，请附具体文件与理由 |

⚠️ 侵权与隐私问题请在 Issue 中标注【侵权申诉】，或使用仓库 **Security → Report a vulnerability** 私密报告；请勿在公开 Issue 里张贴证据。
