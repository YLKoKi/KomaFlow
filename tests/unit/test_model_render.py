# -*- coding: utf-8 -*-
"""模型渲染（独立区域）的契约测试。

需求原文：

> 把工作区里的模型渲染能力装进去，让用户不再需要自己打开 Blender 再导入，
> 记得做独立区域，然后制作完毕后自动导入到流程内的模型区域里面

这一轮加的东西分三层，各测什么：

* **适配层**（``adapters/mmd_render.py``）：**不起 Blender**。
  只测"参数校验 / 排序 / 超时"这些纯逻辑，以及"渲染脚本与许可文件在不在"。
  真起 Blender 的验证在 ``work/selfcheck5.py`` 里（那要几十秒，不进单元测试）。
* **界面层**（``ui/model_render_window.py``）：建真窗口，测"还没渲完不摆确认按钮"、
  "渲完要先给用户看"，以及**确认/取消对主界面有没有副作用**。
* **接线**：主界面工具行有没有那个入口、模型卡接不接受渲好的图。

⚠️ 全部替换掉真实渲染 —— 单元测试里绝不起 Blender 子进程。
"""

from __future__ import annotations

import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from komaflow.adapters import mmd_render # noqa: E402
from komaflow.settings import Settings # noqa: E402


class TestRenderScriptInPlace(unittest.TestCase):
    """随包分发的渲染脚本与它的许可必须都在。"""

    def test_script_and_license_exist(self):
        self.assertTrue(mmd_render.RENDER_SCRIPT.is_file(),
                        f"缺渲染脚本：{mmd_render.RENDER_SCRIPT}")
        license_file = mmd_render.VENDOR_DIR / "LICENSE_MMD渲染.txt"
        self.assertTrue(license_file.is_file(), "MIT 许可要求保留许可文件")
        text = license_file.read_text(encoding="utf-8")
        self.assertIn("MIT", text)

    def test_stitch_script_exists(self):
        """拼三视图跑在 Blender 里（它自带 PIL，我们不准引 pip 依赖）。"""
        self.assertTrue(mmd_render.STITCH_SCRIPT.is_file(),
                        f"缺拼图脚本：{mmd_render.STITCH_SCRIPT}")

    def test_available(self):
        ok, why = mmd_render.available()
        self.assertTrue(ok, why)


class TestPureHelpers(unittest.TestCase):
    """不碰 Blender 的那几个纯函数。"""

    def test_timeout_is_never_none(self):
        """超时是硬约束：不给也要算一个出来，绝不能是"不限时"。"""
        self.assertGreaterEqual(mmd_render.default_timeout(1),
                                mmd_render.MIN_TIMEOUT)
        self.assertGreater(mmd_render.default_timeout(4),
                           mmd_render.default_timeout(1))

    def test_view_order_puts_front_first(self):
        """正面那张按取景命名（full/half/face），得排在最前面。

        ⚠️ 拼图与回填都按"第一张是正面"这个约定走 —— 排错了三视图就先侧面后正面。
        """
        order = mmd_render._view_order
        views = ["front", "side", "back"]
        rows = [(order("角色_side", views, "角色"), "side"),
                (order("角色_full", views, "角色"), "full"),
                (order("角色_back", views, "角色"), "back")]
        rows.sort(key=lambda r: r[0])
        self.assertEqual([name for _key, name in rows],
                         ["full", "side", "back"])


class TestRenderGuards(unittest.TestCase):
    """参数不对要在**起进程之前**就报，别让用户等一轮。"""

    def test_missing_model(self):
        with self.assertRaises(mmd_render.MMDError):
            mmd_render.render_model(r"Z:\不存在\角色.pmx", r"Z:\out",
                                    blender=sys.executable)

    def test_no_blender_chosen(self):
        with tempfile.TemporaryDirectory() as tmp:
            pmx = Path(tmp) / "角色.pmx"
            pmx.write_bytes(b"pmx")
            with self.assertRaises(mmd_render.MMDError) as ctx:
                mmd_render.render_model(pmx, tmp, blender="")
            self.assertIn("Blender", str(ctx.exception))

    def test_unknown_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            pmx = Path(tmp) / "角色.pmx"
            pmx.write_bytes(b"pmx")
            with self.assertRaises(mmd_render.MMDError) as ctx:
                mmd_render.render_model(pmx, tmp, shots=("front", "斜上方"),
                                        blender=sys.executable)
            self.assertIn("斜上方", str(ctx.exception))


def _shown(widget) -> bool:
    """控件现在有没有摆在布局里。

    ⚠️ 不用 ``winfo_ismapped()``：那要看窗口有没有真被系统映射出来，
       测试里的窗常常还没映射，会把"已经 pack 上了"误报成 False。
    """
    try:
        return bool(widget.winfo_manager())
    except tk.TclError:
        return False


def _make_png(path: Path, size: int = 8) -> None:
    """造一张真 PNG（Tk 自己就能写，不用装 Pillow）。"""
    root = tk.Tk()
    root.withdraw()
    image = tk.PhotoImage(width=size, height=size)
    image.put("#3366AA", to=(0, 0, size, size))
    image.write(str(path), format="png")
    root.destroy()


class _WindowCase(unittest.TestCase):
    """建真 Tk 窗口 + 真 ``KomaFlowApp`` 的用例基类（同 test_first_run 的约定）。"""

    def setUp(self) -> None:
        try:
            from komaflow.ui import app as app_module
            from komaflow.ui import model_render_window as mrw
        except Exception as exc: # noqa: BLE001
            self.skipTest(f"界面模块导入失败：{exc}")
        self.app_module = app_module
        self.mrw = mrw
        self._tmpdir = tempfile.TemporaryDirectory(prefix="komaflow_mrw_")
        self.tmp = Path(self._tmpdir.name)
        self._settings_path = self.tmp / "settings.json"
        self._real_path = Settings.path
        Settings.path = classmethod(lambda cls: self._settings_path)
        # 不起真探测（那是 Blender 子进程）
        app_module.KomaFlowApp._detect_blender_async = lambda self: None
        from komaflow.ui import assets as ui_assets
        ui_assets.reset()
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"建不了 Tk 窗口：{exc}")
        self.root.geometry("1100x700+30+30")
        self.app = app_module.KomaFlowApp(self.root)
        self.root.update()

    def tearDown(self) -> None:
        if self.mrw.ModelRenderWindow._instance is not None:
            try:
                self.mrw.ModelRenderWindow._instance.close()
            except Exception: # noqa: BLE001
                pass
        try:
            self.app._shutdown()
        except Exception: # noqa: BLE001
            pass
        try:
            self.root.destroy()
        except (tk.TclError, AttributeError):
            pass
        Settings.path = self._real_path
        self._tmpdir.cleanup()

    def open_window(self, *, pmx: str = ""):
        window = self.mrw.open_model_render(self.app)
        self.root.update()
        if pmx:
            window.pmx.set(pmx)
            window._refresh()
        return window


class TestModelRenderWindow(_WindowCase):
    """那个独立窗的行为。"""

    def test_entry_button_exists_on_main_ui(self):
        """主界面工具行得有这个入口（独立区域＝单独一个窗）。"""
        self.assertTrue(hasattr(self.app, "btn_model"), "主界面没有「渲染模型」入口")
        self.assertTrue(self.app.btn_model.winfo_exists())

    def test_defaults_are_2k_three_views(self):
        window = self.open_window()
        self.assertEqual(window.res.get(), "2048x2048")
        picked = [v for v in mmd_render.VIEWS if window.views[v].get()]
        self.assertEqual(picked, ["front", "side", "back"])

    def test_no_confirm_buttons_before_rendering(self):
        """还没渲完就摆「继续 / 取消」是噪音 —— 而且会让人以为已经好了。"""
        window = self.open_window()
        self.assertFalse(_shown(window.btn_ok))
        self.assertFalse(_shown(window.btn_cancel))

    def test_start_refuses_without_model(self):
        window = self.open_window()
        window.start()
        self.assertIn("模型文件不存在", window.stage.cget("text"))
        self.assertIsNone(window.worker, "参数不对就不该起渲染线程")

    def test_start_refuses_without_views(self):
        pmx = self.tmp / "角色.pmx"
        pmx.write_bytes(b"pmx")
        window = self.open_window(pmx=str(pmx))
        for var in window.views.values():
            var.set(False)
        window.start()
        self.assertIn("一个视角都没勾", window.stage.cget("text"))
        self.assertIsNone(window.worker)

    def test_finished_shows_preview_then_confirm(self):
        """渲完**先给用户看**，再摆「继续 / 取消」—— 需求点名了这一步。"""
        pmx = self.tmp / "角色.pmx"
        pmx.write_bytes(b"pmx")
        image = self.tmp / "角色_full.png"
        _make_png(image)

        window = self.open_window(pmx=str(pmx))
        with mock.patch.object(mmd_render, "stitch_views",
                               return_value={"ok": False, "path": "",
                                             "message": "测试不拼图"}):
            window._on_finished({"outputs": [str(image)], "stats": {"材质": 24},
                                 "name": "角色", "outdir": str(self.tmp)})
        self.root.update()

        # ① 预览区拿到了这张图
        self.assertEqual(str(self.app._preview_source), str(image))
        # ② 确认按钮这才出现
        self.assertTrue(_shown(window.btn_ok))
        self.assertTrue(_shown(window.btn_cancel))
        # ③ 但**主界面的模型卡还没被改**（用户还没点继续）
        self.assertEqual(self.app.vars["model"].get(), "")

    def test_accept_fills_model_card(self):
        image = self.tmp / "角色_三视图.png"
        _make_png(image)
        before = self.app.vars["model"].get()

        self.app.accept_model_render(image, [str(image), "另一张.png"])
        self.root.update()

        self.assertNotEqual(before, self.app.vars["model"].get())
        self.assertEqual(self.app.vars["model"].get(), str(image))
        # 卡片要跟着变成"已放好"（空态点不动，用户就以为没生效）
        from komaflow.ui.widgets import FILLED

        self.assertEqual(self.app.cards["model"].state, FILLED)
        self.assertEqual(self.app.cards["model"]._filename, str(image))

    def test_accept_missing_file_does_not_change_anything(self):
        before = self.app.vars["model"].get()
        with mock.patch("komaflow.ui.app.messagebox.showwarning") as warn:
            self.app.accept_model_render(self.tmp / "根本没有.png", [])
        self.assertTrue(warn.called, "产物不见了要告诉用户")
        self.assertEqual(self.app.vars["model"].get(), before,
                         "产物不见了还把路径写进卡里，等于留了个坏值")

    def test_reject_leaves_main_ui_untouched(self):
        image = self.tmp / "角色_full.png"
        _make_png(image)
        window = self.open_window(pmx=str(self.tmp / "角色.pmx"))
        window.outputs = [str(image)]
        before = self.app.vars["model"].get()
        window._reject()
        self.assertEqual(self.app.vars["model"].get(), before,
                         "点了取消主界面不能变")

    def test_windows_are_singletons(self):
        first = self.open_window()
        second = self.mrw.open_model_render(self.app)
        self.assertIs(first, second, "重复点入口不该开出第二个窗")


class TestModelCardAcceptsPng(_WindowCase):
    """模型卡接 PNG（渲好的三视图）—— 但要说清它不能当渲染用的模型。"""

    def test_png_lands_in_model_slot_with_a_warning(self):
        image = self.tmp / "角色_三视图.png"
        _make_png(image)
        self.app._assign("model", str(image))
        self.assertEqual(self.app.vars["model"].get(), str(image))
        self.assertIn("参考图", self.app.vars["status"].get())

    def test_png_is_offered_in_the_browse_filter(self):
        patterns = self.app_module.FILE_TYPES["model"][0][1]
        self.assertIn("*.png", patterns)


class TestRenderWindowRemembersPaths(_WindowCase):
    """「渲染模型」窗要记住上次的**模型文件**与**出图目录**。

    用户要求：
      > 这个模型文件位置也保留成最近一次用户的位置

    ⚠️ 这里记的跟主流程角色卡的记忆（``last_model``）**是两套** ——
       静帧三视图的输入不该把"整片要用的角色"悄悄换掉。
    """

    def _pmx(self, name: str = "模型.pmx") -> str:
        path = self.tmp / name
        path.write_bytes(b"pmx")
        return str(path)

    def test_browse_remembers_model_path(self):
        window = self.open_window()
        model = self._pmx()
        with mock.patch.object(self.mrw.filedialog, "askopenfilename",
                               return_value=model):
            window._browse_pmx()
        self.assertEqual(Settings.load().last_render_model, model)

    def test_browse_remembers_outdir(self):
        window = self.open_window()
        out = self.tmp / "出图"
        out.mkdir()
        with mock.patch.object(self.mrw.filedialog, "askdirectory",
                               return_value=str(out)):
            window._browse_outdir()
        self.assertEqual(Settings.load().last_render_outdir, str(out))

    def test_paths_are_restored_next_time(self):
        """重开窗口时要把上次的模型文件与出图目录填回来。"""
        model = self._pmx()
        out = self.tmp / "出图2"
        out.mkdir()
        settings = Settings.load()
        settings.last_render_model = model
        settings.last_render_outdir = str(out)
        settings.save()
        # 模拟"下次打开程序"：应用重启时设置是从磁盘**重新读**的。
        # 不重新读的话内存里还是旧值，测的就不是"记住"而是同一份对象。
        self.app.settings = Settings.load()

        window = self.open_window()
        self.assertEqual(window.pmx.get(), model,
                         "模型文件要填回上次那个")
        self.assertEqual(window.outdir.get(), str(out),
                         "出图目录要填回上次那个")

    def test_missing_model_is_not_restored(self):
        """记住的模型被删了 → 不填，让用户重新选（填了会一渲就报错）。"""
        settings = Settings.load()
        settings.last_render_model = str(self.tmp / "已经没了.pmx")
        settings.save()
        self.app.settings = Settings.load() # 模拟重启后重读
        window = self.open_window()
        self.assertEqual(window.pmx.get(), "",
                         "文件不在了就不该填一个不存在的路径")

    def test_memory_is_separate_from_character_card(self):
        """渲三视图记的模型，不能改掉主流程角色卡的记忆。"""
        model = self._pmx("三视图用.pmx")
        window = self.open_window()
        with mock.patch.object(self.mrw.filedialog, "askopenfilename",
                               return_value=model):
            window._browse_pmx()
        saved = Settings.load()
        self.assertEqual(saved.last_render_model, model)
        self.assertEqual(saved.last_model, "",
                         "渲三视图不该动「整片要用的角色」那条记忆")


class TestModelRenderKeepsPmx(_WindowCase):
    """渲完三视图后，角色卡要留 **PMX**，而不是那份三视图 .blend。

    用户报的原话：

    > 动作导进去出来动作消失了，场景导进去出来场景消失了

    查日志根因：``accept_model_render`` 把「渲染模型」存出的 .blend 塞进角色卡，
    角色卡一旦是 .blend，整条流程就切成「整份工程」路线 —— 那条路不导入舞台、
    骨骼命名也对不上动作（实测匹配只有 22%），于是场景和动作双双"消失"。

    .blend 只该当**观感来源**，角色本体仍用 PMX。
    """

    def _files(self):
        pmx = self.tmp / "角色.pmx"
        pmx.write_bytes(b"pmx")
        blend = self.tmp / "角色.blend"
        blend.write_bytes(b"BLENDER")
        return pmx, blend

    def test_pmx_stays_in_card_and_blend_becomes_look_source(self):
        pmx, blend = self._files()
        self.app.accept_model_render(blend, [], source_pmx=str(pmx))
        self.root.update()

        self.assertEqual(self.app.vars["model"].get(), str(pmx),
                         "角色卡必须是 PMX，否则 route 会切成整份工程")
        self.assertEqual(self.app.vars["blend"].get(), "",
                         "不该顺手把 blend 也填上（那会直接走整份工程）")
        self.assertEqual(Settings.load().last_look_blend, str(blend),
                         "那份 .blend 要留下当观感来源")

    def test_without_pmx_falls_back_to_blend(self):
        _pmx, blend = self._files()
        self.app.accept_model_render(blend, [], source_pmx="")
        self.root.update()
        # R06 第三入口：退回整份工程时必须进 blend 字段（并清掉 model），
        # 否则 _collect_project 会拿 PMX 导入器去读 .blend。
        self.assertEqual(self.app.vars["blend"].get(), str(blend),
                         "拿不到源 PMX 时只能退回整份工程")
        self.assertEqual(self.app.vars["model"].get(), "",
                         "整份工程不能塞进 model —— route 会算错")


class TestWholeProjectModeSaysWhatItDrops(_WindowCase):
    """角色卡是 .blend 时，必须**当场说清**场景/动作不参与。"""

    def _blend(self):
        path = self.tmp / "工程.blend"
        path.write_bytes(b"BLENDER")
        return path

    def test_status_mentions_dropped_assets(self):
        blend = self._blend()
        motion = self.tmp / "动作.vmd"
        motion.write_bytes(b"Vocaloid Motion Data")
        self.app._set_motion_paths([str(motion)])
        self.app.vars["stage"].set(str(self.tmp / "场景.blend"))
        self.app.vars["model"].set(str(blend))

        project = self.app._collect_project()

        self.assertEqual(project.inputs.route, "blend")
        status = self.app.vars["status"].get()
        self.assertIn("整份工程", status)
        self.assertIn("不会参与渲染", status)


if __name__ == "__main__":
    unittest.main()
