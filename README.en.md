# KomaFlow

> **Download the app (Windows)** → <https://github.com/YLKoKi/KomaFlow/releases/latest>
>
> ⚠️ This is a **beta** build (pre-release): interfaces and defaults may change.
>
> Do not use the green `Code → Download ZIP` button — that is the **source code**
> and contains no executable. The app is the asset on the **Releases** page
> (`KomaFlow_V1.2.0-beta.zip`, **~12.3 MB**).


Turn MMD models (`.pmx`) and motions (`.vmd`) into a finished video — by driving
the **Blender you already have installed**. The app assembles the scene, builds
and bakes physics, renders in segments, then muxes the result with FFmpeg.

Windows desktop app (not a Blender add-on). **V1.2.0-beta** (beta).

> 中文说明见 [README.md](README.md)。界面默认中文，可单独下载英文语言包
> 切换为英文（见 [English-UI-Guide](#english-ui)）。

## What it does

| Step | Detail |
|---|---|
| Assemble | Import PMX / append a `.blend` stage / attach VMD motion and camera |
| Physics | `mmd_tools` `build_rig` (bone-follow constraints) + point-cache bake |
| Preroll | 30 still frames so hair and skirt settle before the motion starts |
| Render | Segmented (~1000 frames per segment), resumable |
| Encode | External FFmpeg; transparent output goes PNG → alpha MOV |

## Requirements

| Need | Notes |
|---|---|
| **Windows 10 / 11 (64-bit)** | Windows only |
| **Blender 5.0 – 5.99** | Tested on 5.2.3 LTS. **Not bundled** — use your own install |
| **mmd_tools add-on** | `bl_ext.blender_org.mmd_tools`; one-click install from the UI |
| **FFmpeg** | Needed for muxing/encoding. **Not bundled** — the app can fetch a **pinned build** (SHA256-verified) after you agree, or you can install your own.<br>Not required for single-pass **MP4** or **PNG sequence**; **required for transparent MOV** |
| Python 3.10+ | Only for running from source. **No pip dependencies** |

## Quick start

**Release build**

1. Download `KomaFlow_V1.2.0-beta.zip` from Releases and extract it
2. Run `KomaFlow.exe`
3. Drop a `.pmx` into Character, a `.vmd` into Motion, click Start Render

**From source**

```bat
python run.py
```

Read `User-Guide-EN.txt` (inside the release package) for the full walkthrough.

## Design notes

- **Blender and FFmpeg are not bundled** — both are called as separate processes,
  so no GPL code is distributed here.
- **No assets are bundled** — no models, motions, textures or footage.
- **Zero runtime dependencies** — standard library + Tk only.
- **Stable look** — the character renders in flat material colour (no cast shadows, no darkening as it moves)
- **The project is reusable** — assembly saves `<name>.blend` next to your output,
  with physics already baked. Re-render, change the range, or edit it in Blender
  and render again without re-baking.
- **Low bone match aborts early**: if the motion's bones match less than **35%** of the
  model's bones, assembly stops before rendering (no wasted full clip). Set
  `KOMEFLOW_ALLOW_LOW_MATCH=1` to force it; 35%–60% only warns.
- **Only two network actions, both ask first**:
  1. "Install mmd_tools" → Blender fetches the add-on from the official repository
  2. FFmpeg: asked once on first launch; also via Settings → Render environment →
     Download FFmpeg; and asked again if you start a transparent MOV without FFmpeg
     (all three share one downloader)
- The download is a **pinned build 9.0.2** (Windows 64-bit essentials), SHA256-verified;
  a mismatch is discarded and the next source is tried. https allow-list only.
- Order: **pinned build → GitHub direct → gyan.dev → mirrors (last resort)**
  (`ghfast.top` / `ghproxy.net` / `gh-proxy.com`). If you install FFmpeg yourself,
  the app will not download it. Install dir: `%LOCALAPPDATA%\KomaFlow\ffmpeg` (~90 MB).
- **No telemetry**, no accounts, no registry writes, no services.

## Layout

```
src/komaflow/            application
  core/                  pure logic (no bpy / tk)
  adapters/              Blender and FFmpeg drivers
  adapters/vendor/       MMD still renderer (MIT, see LICENSE / NOTICE)
  ui/                    Tk interface
tests/                   359 unit tests (actually run; 3 skipped by design)
tools/                   PyInstaller spec, packaging, asset generators
docs/                    architecture and usage notes (Chinese)
```

```bat
set PYTHONPATH=src
python -m unittest discover -s tests -t .
```

## English UI

The interface is Chinese by default. The English language pack is **not bundled
with the main package** — download `KomaFlow_语言包_英文_v1.1.zip` from Releases:

1. Extract the language pack zip (you get a `locales` folder with `en.json`)
2. Copy that `locales` folder next to `KomaFlow.exe`
3. Restart — the UI is English

Remove the folder to return to Chinese. Details: `English-UI-Guide.txt`.

## Known limitations

- The executable is **unsigned** (SmartScreen will warn on first run)
- No CI yet; tests require Tk and have only been run on Windows
- Physics is an approximation: `build_rig` uses distance constraints instead of
  MMD's soft constraints, so some models may clip or jitter
- Verified only on Blender 5.x with mmd_tools 4.5.x
- The FFmpeg auto-download has **not** been verified end-to-end on a real network;
  it may fail behind proxies/restricted networks. PNG sequences still work without it.

## Assets and licensing

This project contains **no** models, motions, textures or rendered footage, and
grants **no** rights to any third-party asset. MMD models and motions belong to
their authors; common terms are "no redistribution" and "no commercial use".

## How the code was produced

Written with **AI assistance**, reviewed and tested by a human. Apart from the
MIT renderer under `adapters/vendor/` (attributed), no third-party code was
copied.

## License

- This project: **MIT** (see [LICENSE](LICENSE))
- `adapters/vendor/MMD渲染.py`: **MIT**, copyright its author
- Blender / mmd_tools: **GPL**, not bundled — invoked as subprocesses only

Full component list: [THIRD-PARTY-NOTICES-EN.md](THIRD-PARTY-NOTICES-EN.md)
(Chinese: 第三方来源与许可_发布版.md).

## Contact

| Channel | Use for |
|---|---|
| **GitHub Issues** | Questions, bug reports, feature requests |
| **Issues (label 【侵权申诉】)** | Copyright / privacy / business — include the exact file and reason |

Please do **not** open a public issue for copyright or privacy matters.
