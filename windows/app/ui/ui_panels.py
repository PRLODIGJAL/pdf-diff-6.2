# -*- coding: utf-8 -*-
"""
ui_panels.py —— **面板**：窗口里你看到、并用手点的那些东西。
==================================================================
管三件事，都只在「搭界面」和「把状态刷到界面上」这两个时刻发生：

  ① `_build_ui()` 一次性把工具栏、拖放区、四类胶囊、设置区、状态栏摆出来；
  ② 控件工厂（`_mk_check` / `_swatch` / `_attach_tip`）—— 只在这儿用，别处不要；
  ③ 把「后台刚算出来的结论」翻译成界面状态：胶囊亮灭（`_set_class`）、
     文案长短（`_fit_chips`）、两侧计数（`_update_counts`）、状态栏一句话
     （`_on_rep`）。

判据不在这里：什么算差异、能不能比，问 `judge/`；画面怎么画，问 `ui_viewport`。
这里只负责「有了结论之后，界面长什么样」。

2026-11-02 目录重构时从 `diff_app5.py` 原样切出来，逻辑一个字节没改。
"""

from __future__ import annotations

import os
import sys

import tkinter as tk
from tkinter import ttk

# 本文件是 ui 层的一个模块，单独 import 也要能用：先把 `app/` 塞进 sys.path，
# 再让 `paths.install()` 把 ui/judge/engine/shell 四层一起铺开。
_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP not in sys.path:
    sys.path.insert(0, _APP)
import paths as _paths                                          # noqa: E402

_paths.install()

import pdf_annotate as ann                                      # noqa: E402
import pdfdiff_preflight as pre                                 # noqa: E402
import pdfdiff_gate as gate                                     # noqa: E402
# 拖放能力探没探到，只有拖放模块知道（它要 `tkinterdnd2`），这里按需取。
from ui_dropzone import DND_FILES, _HAS_DND                     # noqa: E402
from ui_theme import (                                          # noqa: E402
    APP_TITLE, BG, BLINK_DEFAULT, BLINK_ITEMS, BORDER, CARD,
    CHIP_ALERT_BG, CHIP_OFF_BG, CHIP_OFF_FG, CHIP_ON_BG, CHIP_ON_FG,
    DROP_HINT, DZ_BG, FONT, LEGEND_ORDER, MANUAL_BOX_H, MANUAL_BOX_W,
    PRIMARY, PRIMARY_D, SLOT_EMPTY, STOP_FG, STOP_IDLE,
    SUPPORT_HINT, TEXT, TEXT2, VIEW_BLINK, VIEW_MODES, VIEW_SIDE,
    VIEW_SWIPE, VIEW_SWIPE_POS, _KIND_ROWS,
)


def _bind_opt(widget, sequence, func):
    """绑一个「有就用、没有就算了」的事件 —— 绑不上绝不能把窗口卡在启动阶段。

    Tk 8.6 及更早版本在**解析事件串**时就把鼠标键位限死在 1~5，写 `<Button-6>` /
    `<Button-7>` 会当场抛 `TclError: bad button number "6"`；Tk 9.0 才放开（9 以内
    都认）。Windows 官方 Python 自带 Tk 8.6，macOS 包里的 runtime 是 Tk 9.0 ——
    同一份代码在 Mac 上跑得好好的，到 Windows 上却在 `_build_ui()` 里直接炸掉、
    界面一个都出不来（2026-11-09 用户在 Windows 10 实测，见 `bad button number "6"`）。

    左右滚轮本来就只有部分鼠标有（Windows 上 Tk 也根本不投递 6/7 号键位），
    接不上不影响任何其它操作，所以这里静默跳过，不弹窗、不记日志。
    """
    try:
        widget.bind(sequence, func)
    except tk.TclError:
        pass


def _mk_check(parent, text, var, command, bg=BG):
    """统一的勾选框：**画的是对勾，不是叉**。

    （2026-10-21 用户反馈）`ttk.Checkbutton` 在本机 clam 主题下，选中态画出来的是
    一个「✗」，和「关闭 / 不通过」的语义撞车，很容易让人以为没勾上。
    改用经典 `tk.Checkbutton`：macOS 由系统画原生蓝底白勾，
    三处勾选（删减/新增/位移、底图灰度、开跑前提示）外观就一致了。
    """
    return tk.Checkbutton(parent, text=text, variable=var, command=command,
                          bg=bg, fg=TEXT, activebackground=bg,
                          activeforeground=TEXT, selectcolor=CARD,
                          font=(FONT, 10), bd=0, highlightthickness=0,
                          anchor="w")

class PanelsMixin:
    """窗口「长什么样、有没有点亮」的那一半。

    `App` 继承它。名字以 Mixin 结尾是因为它不单独存在 —— 所有方法都是
    `App` 的一部分，靠 `self` 拿窗口、变量和状态。
    """

    def _sync_disp(self):
        """文件框只显示**文件名**（完整路径太长看不清哪个是哪个版）。

        路径仍完整存在 old_var/new_var 里，显示与存储分离。
        拖放区两行槽位用的是同一份文件名；空着时给一句灰字（SLOT_EMPTY），
        否则整块拖放区一片空白，用户看不出「到底选没选上」。
        """
        for var, disp, slot in ((self.old_var, self.old_disp, self.old_slot),
                                (self.new_var, self.new_disp, self.new_slot)):
            p = var.get().strip()
            name = os.path.basename(p) if p else ""
            disp.set(name)
            slot.set(name or SLOT_EMPTY)
        # 换了一对文件，工具条那格「类型」就先清空 —— 留着上一对的结论会误导
        # （它要到点「开始对比」时用体检结果才写得出来，见 `_run`）。
        # ⚠️ `_sync_disp` 在 `_build_ui` 之前（`_load_cfg`）也会被调一次，那时
        #    控件还没建，必须判空。
        if getattr(self, "cls_chips", None):
            self._set_class(())

    # ---------------- 界面 ----------------
    def _build_ui(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=TEXT, font=(FONT, 11))
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=TEXT, font=(FONT, 11))
        st.configure("TButton", font=(FONT, 11), padding=(8, 4),
                     background=CARD, bordercolor=BORDER,
                     lightcolor=CARD, darkcolor=CARD)
        st.map("TButton", background=[("active", "#f0f2f5"), ("pressed", "#e6e9ee")])
        st.configure("Small.TButton", font=(FONT, 10), padding=(6, 2),
                     background=CARD, bordercolor=BORDER,
                     lightcolor=CARD, darkcolor=CARD)
        st.map("Small.TButton", background=[("active", "#f0f2f5")])
        # 视图切换的选中/未选中两态。⚠️ 两个样式的 padding/字号**必须逐项对齐**
        # （只许换底色和字重）：选中态一变宽，整行会跟着左右抖一下。
        st.configure("ViewOn.TButton", font=(FONT, 10, "bold"), padding=(8, 3),
                     background=PRIMARY, foreground="#ffffff",
                     bordercolor=PRIMARY, lightcolor=PRIMARY, darkcolor=PRIMARY)
        st.map("ViewOn.TButton",
               background=[("active", PRIMARY_D), ("pressed", PRIMARY_D)])
        st.configure("ViewOff.TButton", font=(FONT, 10), padding=(8, 3),
                     background=CARD, foreground=TEXT2,
                     bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD)
        st.map("ViewOff.TButton", background=[("active", "#f0f2f5")])
        st.configure("Primary.TButton", font=(FONT, 11, "bold"), padding=(12, 4),
                     background=PRIMARY, foreground="#ffffff",
                     bordercolor=PRIMARY, lightcolor=PRIMARY, darkcolor=PRIMARY)
        st.map("Primary.TButton", background=[("active", PRIMARY_D), ("pressed", PRIMARY_D)])
        # 「停止对比」：白底 + 红字细框。不用整块红底 —— 那是「危险操作」的
        # 视觉重量，而这里只是「止损」，且平时是灰掉的。
        st.configure("Stop.TButton", font=(FONT, 10), padding=(7, 3),
                     background=CARD, foreground=STOP_FG,
                     bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD)
        st.map("Stop.TButton",
               background=[("active", "#fdf1ef"), ("pressed", "#f7e0dc"),
                           ("disabled", CARD)],
               foreground=[("disabled", "#c3c8ce")])
        # 进度条已删（2026-10-21），对应的 TProgressbar 样式一并去掉，
        # 免得以后有人看到样式就以为还有个进度条没接上。

        # ===== 顶部：选文件 + 操作 =====
        top = ttk.Frame(self)
        top.pack(fill=tk.X)

        bar = ttk.Frame(top, padding=(12, 6, 12, 0))
        bar.pack(fill=tk.X)
        self.bar = bar                      # 供回归断言定位（四格胶囊挂在这一行）
        tk.Label(bar, text=APP_TITLE, bg=BG, fg=TEXT,
                 font=(FONT, 12, "bold")).pack(side=tk.LEFT)

        # ===== 四类总览（2026-10-29 用户要求，2026-10-30 挪回标题行）=====
        # 原话：「先把预设好的这个名字给放到空白处，然后如果是哪一类识别的时候，
        # 就把那一类给高亮出来，证明现在是这一类。就是让人有个总体的观念，知道
        # 一共有哪些类别，而这个是哪一类。」
        # 所以四格**一直全在**，只亮命中的那格（两侧不同类时两格都亮）；
        # 还没比对过 / 读不出 → 四格全灰，不瞎猜。
        # 位置沿革：标题行 → 「开始对比」行右端（2026-10-29 第二轮）→ 又回标题行
        # 右端（2026-10-30 用户：「最上面那一排 PDF 差异对比工具的最右侧那个空白
        # 区域其实也是够的」）。挪回来是因为「开始对比」那一行要腾出整片右侧
        # 放视图切换（并排 / 滑动），四格留在那儿会和它抢位置。
        tk.Label(bar, text=SUPPORT_HINT, bg=BG, fg=TEXT2,
                 font=(FONT, 10)).pack(side=tk.LEFT, padx=(10, 0))
        self.cls_chips = {}
        for _c in reversed(pre.CLASS_ORDER):
            _lb = tk.Label(bar, text=pre.chip_label(_c),
                           bg=CHIP_OFF_BG, fg=CHIP_OFF_FG, font=(FONT, 9),
                           padx=3, pady=1)
            _lb.pack(side=tk.RIGHT, padx=(2, 0), pady=2)
            self.cls_chips[_c] = _lb
            self._attach_tip(_lb, pre.CLASS_TIP[_c])

        # ===== 唯一一块拖放区（2026-10-20 用户二次要求：三行合成一块）=====
        # 原来有「合并框 + ① 选择文件（旧版行 / 新版行）」三层入口，实际用起来
        # 永远是「找到文件 → 拖进来」；两行手工指定既占地方，又让整块往上缩。
        # 现在只剩这一块：一次拖两个、或一份一份拖，都走**同一套**自动排位；
        # 右边三个按钮 = 浏览（可一次挑一到两个）/ 清空 / ⇅ 交换（排序反了的兜底）。
        # 拖动范围 = 整块（含图标、提示、两行文件名），全部注册成投放目标。
        dz = tk.Frame(top, bg=DZ_BG, highlightbackground=BORDER,
                      highlightthickness=1)
        dz.pack(fill=tk.X, padx=(12, 12), pady=(6, 0))
        self.drop_zone = dz
        dz.columnconfigure(2, weight=1)          # 文件名一列吃掉全部富余宽度

        self.lbl_icon = tk.Label(dz, text="⬇", bg=DZ_BG, fg=PRIMARY,
                                 font=(FONT, 15))
        self.lbl_icon.grid(row=0, column=0, sticky="w", padx=(12, 4),
                           pady=(6, 0))

        self.lbl_drop = tk.Label(dz, text=DROP_HINT, bg=DZ_BG, fg=TEXT2,
                                 font=(FONT, 10), anchor="w", justify="left")
        self.lbl_drop.grid(row=0, column=1, columnspan=2, sticky="w",
                           pady=(8, 2))

        # 两行槽位：左「旧版 / 新版」灰标签 + 右文件名。空着时是灰字引导。
        self.lbl_tag_old = tk.Label(dz, text="旧版", bg=DZ_BG, fg=TEXT2,
                                    font=(FONT, 10, "bold"), width=4, anchor="w")
        self.lbl_tag_old.grid(row=1, column=1, sticky="w", padx=(0, 2))
        self.lbl_file_old = tk.Label(dz, textvariable=self.old_slot, bg=DZ_BG,
                                     fg=TEXT, font=(FONT, 10), anchor="w")
        self.lbl_file_old.grid(row=1, column=2, sticky="ew", padx=(0, 8))
        self.lbl_tag_new = tk.Label(dz, text="新版", bg=DZ_BG, fg=TEXT2,
                                    font=(FONT, 10, "bold"), width=4, anchor="w")
        self.lbl_tag_new.grid(row=2, column=1, sticky="w", padx=(0, 2))
        self.lbl_file_new = tk.Label(dz, textvariable=self.new_slot, bg=DZ_BG,
                                     fg=TEXT, font=(FONT, 10), anchor="w")
        self.lbl_file_new.grid(row=2, column=2, sticky="ew", padx=(0, 8),
                               pady=(0, 8))

        btns = tk.Frame(dz, bg=DZ_BG)
        btns.grid(row=0, column=3, rowspan=3, sticky="e", padx=(4, 10), pady=6)
        ttk.Button(btns, text="浏览…", style="Small.TButton",
                   command=self._pick).pack(side=tk.LEFT)
        ttk.Button(btns, text="清空", style="Small.TButton",
                   command=self._clear_pair).pack(side=tk.LEFT, padx=(5, 0))
        ttk.Button(btns, text="⇅ 交换", style="Small.TButton",
                   command=self._swap).pack(side=tk.LEFT, padx=(5, 0))
        self._btns_dz = btns

        # 拖放目标：整块 + 块内每一个可见件（ttk.Button 在部分 Tk 版本上不收，
        # 逐个 try 掉即可）。用户从哪条缝拖进来都算数。
        self._dz_targets = [dz, self.lbl_icon, self.lbl_drop,
                            self.lbl_tag_old, self.lbl_file_old,
                            self.lbl_tag_new, self.lbl_file_new, btns]
        if _HAS_DND:
            for _w in self._dz_targets:
                try:
                    _w.drop_target_register(DND_FILES)
                    _w.dnd_bind("<<Drop>>", self._on_drop_many)
                    _w.dnd_bind("<<DropEnter>>", self._drop_enter)
                    _w.dnd_bind("<<DropLeave>>", self._drop_leave)
                except tk.TclError:
                    pass

        f3 = ttk.Frame(top, padding=(12, 8, 12, 2))
        f3.pack(fill=tk.X)
        self.f3 = f3
        self.btn_run = ttk.Button(f3, text="开始对比", style="Primary.TButton",
                                  command=self._run)
        self.btn_run.pack(side=tk.LEFT)
        # 「停止对比」（2026-10-20 用户要求）：大面积偏移/多页文件一跑就是几分钟，
        # 选错文件时得能当场停下，不必退出软件。紧贴在「开始对比」右边。
        self.btn_stop = ttk.Button(f3, text=STOP_IDLE, style="Stop.TButton",
                                   command=self._stop, state="disabled")
        self.btn_stop.pack(side=tk.LEFT, padx=(6, 0))

        # ===== 视图切换（2026-10-30 用户要求）=====
        # 用户原话：「最右边那一大片位置，你可以设计两个按钮，一个是现在目前的
        # 这个界面保持左右对比，另外一个就是只有一份视图，左边是旧的右边是新的，
        # 中间通过一根线来左右拖拽。」
        # 放这一行最右：页码和缩放沉到底部、四格胶囊回到标题行之后，这块整片空着。
        # ⚠️ 两个按钮的 padding 必须一样（ViewOn/ViewOff 只换底色和字重），否则
        #    选中态一变宽，整行会跟着左右抖一下。
        # ⚠️ pack 顺序＝从右往左：[并排] 先 pack 反而在左，因为 side=RIGHT 是
        #    依次贴右边界。所以这里先 pack「滑动」，再 pack「并排」。
        self.view_mode = tk.StringVar(value=VIEW_SIDE)
        self._swipe_pos = VIEW_SWIPE_POS      # 分割线位置（0~1，相对画面宽度）
        self._view_btns = {}
        # ⚠️ 三颗都不带符号：带 `⇄ ▥ ◐` 时每颗约 74px，三颗就把设置区挤下 1011
        #    那条线（窗宽 1080 只剩 46px 余量）。纯文字每颗 ~54px，三颗比原来
        #    两颗只宽 16px，装得下；符号的作用由 tip 和选中态底色承担。
        for _key, _txt, _tip in (
                (VIEW_BLINK, "叠加", "叠加视图：整页在旧版/新版之间来回闪。\n"
                                     "没改的地方两帧完全重合，改过的那一小块每帧"
                                     "都在跳 —— 用来**定位**最快。"),
                (VIEW_SWIPE, "滑动", "单页视图：中间一根线左右拖，"
                                     "同一位置来回扫，动没动一眼看出来"),
                (VIEW_SIDE, "并排", "左右两份并排显示（默认）")):
            _b = ttk.Button(f3, text=_txt, style="ViewOff.TButton",
                            command=lambda k=_key: self._set_view(k))
            _b.pack(side=tk.RIGHT)
            self._view_btns[_key] = _b
            self._attach_tip(_b, _tip)

        # ===== 设置区（2026-10-30 用户要求聚成一块）=====
        # 用户原话：「像那个底图灰度，还有选择的那几个颜色的勾选，以及那个 OCR
        # 兜底，我觉得应该放在一块位置，而不应该像这样虽然聚集在一起了，但是
        # 视觉上还是比较零零散散的。」
        # 做法：一个 1px 细边框的容器把它们圈起来，内部再用竖线分三组
        # （灰度 ｜ 三个颜色 ｜ OCR 兜底）。底层控件一个都没合并 —— 灰度、每个
        # 颜色桶、OCR 档位仍然各自独立，只是视觉上归拢。
        self.opts = tk.Frame(f3, bg=CARD, highlightbackground=BORDER,
                             highlightthickness=1)
        self.opts.pack(side=tk.LEFT, padx=(12, 0))
        # 再往右原来是进度条（2026-10-21 删：prog 的 total 是 opcodes 块数，
        # 不是页数也不是区域数，跳得极不均匀）。位置现在是「底图灰度」——
        # 默认打开，只作用于**渲染**，不碰判定：彩色原件配上红/绿/浅蓝标注会
        # 很花，底图一灰，标注就跳出来了。
        self.gray_on = tk.BooleanVar(value=True)
        self.chk_gray = _mk_check(self.opts, "底图灰度", self.gray_on,
                                  self._on_gray_toggle, bg=CARD)
        self.chk_gray.pack(side=tk.LEFT, padx=(7, 2), pady=3)

        # ===== 分类开关（2026-10-29 合并成三颗）=====
        # 用户原话：「把现在的红绿新增删减，它不是两个分别的按钮吗？给合并为一个
        # 按钮，就是一键判断、一键打开这个红绿显示的开关；然后位移算是一个按钮；
        # 然后紫色的那个待判定算是一个按钮。」
        # 所以界面只剩三颗：**增删**（红+绿一起）／**位移**／**待判定**。
        # ⚠️ 合并的是**操作**，不是语义：底层仍然是两个桶（`kind_on[KIND_DEL]`
        #    与 `kind_on[KIND_ADD]`），判定/统计/图例/`filter_kinds` 全按桶走，
        #    一颗勾只是同时驱动两个 —— 这样「只看红」「只看绿」将来想放开，
        #    也随时能把两颗拆回去，不用动底层。
        # ⚠️ 增删那颗前面画**两个**圆点（红+绿）：一颗勾管两种颜色，必须让用户
        #    一眼看到管的是哪两色，否则「增删」这个词跟画面上的红绿对不上号。
        tk.Frame(self.opts, bg=BORDER, width=1).pack(
            side=tk.LEFT, fill=tk.Y, padx=4, pady=4)
        self.chk_kind = {}
        for _ks, _txt, _dots in _KIND_ROWS:
            for _dot in _dots:
                self._swatch(self.opts, *_dot).pack(side=tk.LEFT, padx=(3, 0))
            _chk = _mk_check(
                self.opts, _txt, self.kind_on[_ks[0]],
                (self._on_pair_toggle if len(_ks) > 1 else self._on_kind_toggle),
                bg=CARD)
            _chk.pack(side=tk.LEFT)
            for _k in _ks:          # 每个桶都指到这颗勾上（供 _refresh_pending_check 用）
                self.chk_kind[_k] = _chk
        # 紫那颗勾有个前提：这一批结果里**确实有**紫框才点得动 —— 纯原生 PDF
        # 勾着也没东西可画，索性置灰（顶部那格类型会写「① 纯原生」，一看就懂）。
        self.chk_kind[ann.KIND_PENDING].config(disabledforeground="#c3c8ce",
                                               state="disabled")

        # 2026-11-02：这个位置原来放「对比精度（快 / 准）」下拉，用户要求整个删掉
        # （只保留原来的默认档 120 dpi，见 `ui_theme.PIXEL_DPI`）。原话：
        # 「准也准不到哪里去，然后再加上这个叠加的功能，它其实已经也很准了，
        #   就是能够让人自己去识别」。删掉后设置区就停在「待判定」那颗勾上，
        # 那个竖分隔线也一起拿走 —— 后面没东西了，留着是个悬空的竖杠。
        # 「开跑前提示」的勾选框 2026-10-26 已删（用户：一直默认打开）——
        # 纯图片/扫描件/转曲/混合 开跑前都会弹窗问一句，判据在
        # core/pdfdiff_preflight（`warn_kinds`），拦不拦只看 `_gate_image_only()`。
        # 「快速定位（紫框）」这颗勾 2026-10-26 也删了（用户：「不应该有这个
        # 按钮存在的」）：紫框什么时候出现由文件自己决定，见 __init__ 里的判据。
        # 红/绿/浅蓝/紫的对应关系直接由图例色点给出（那排从 COLOR_OF 自动生成，
        # 永远和数据同源），这里不再重复。


        # ===== 底部：预览工具条（页码 / 缩放，2026-10-30 用户要求沉底）=====
        # 原话：「关于页码不应该放在上面，页码和那个缩放大小应该放在最底下。」
        # ⚠️ 这里**只建控件、不 pack 容器**：`self.toolbar` 要等状态栏 pack 完再
        #    pack(side=BOTTOM)，才会落在状态栏**上方**。pack 顺序决定谁更贴底
        #    （先 pack 的先贴边），所以在下面状态栏那段之后才调用。
        self.toolbar = ttk.Frame(self, padding=(12, 2, 12, 2))
        self.btn_prev = ttk.Button(self.toolbar, text="◀", width=3,
                                   style="Small.TButton", command=self._prev_page,
                                   state="disabled")
        self.btn_prev.pack(side=tk.LEFT)
        # ⚠️ 宽度交给文字自适应（原先是 width=7 固定字符宽）。两侧页数不同时
        # 这里要追加「（旧 1 页 · 新 2 页）」，固定 7 字符会把后半句**切掉**，
        # 只剩「1 / 2（旧 1 页 · 新」这种半截话（2026-10-20 截图核对时发现）。
        self.lbl_page = tk.Label(self.toolbar, text="– / –", bg=BG, fg=TEXT,
                                 font=(FONT, 10))
        self.lbl_page.pack(side=tk.LEFT)
        self.btn_next = ttk.Button(self.toolbar, text="▶", width=3,
                                   style="Small.TButton", command=self._next_page,
                                   state="disabled")
        self.btn_next.pack(side=tk.LEFT)
        self._zoom_out = ttk.Button(self.toolbar, text="−", width=2,
                                    style="Small.TButton",
                                    command=lambda: self._zoom(-1),
                                    state="disabled")
        self._zoom_out.pack(side=tk.LEFT, padx=(14, 0))
        self.lbl_dpi = tk.Label(self.toolbar, text="110 dpi", bg=BG, fg=TEXT2,
                                font=(FONT, 10), width=7)
        self.lbl_dpi.pack(side=tk.LEFT)
        self._zoom_in = ttk.Button(self.toolbar, text="+", width=2,
                                   style="Small.TButton",
                                   command=lambda: self._zoom(1), state="disabled")
        self._zoom_in.pack(side=tk.LEFT)
        # 「适应宽度」按钮 2026-10-26 已删（用户：「好像没有必要保留了，鼠标挺
        # 方便的」）—— 滚轮缩放本身够用，打开文档/翻页时也仍会自动适应一次
        # （`self._auto_fit`），所以删掉的只是那颗常驻按钮，不是这个能力。
        # 底栏右端那句「这份文件是怎么比的」说明：并排/滑动两种视图下都成立，
        # 放这里不占上面那一行的宽度。
        #
        # ===== 叠加闪烁（2026-10-30 晚，替代「自动摆动」）=====
        # 「快速摆动相当于手来回左右动，那有没有可能就是快速来回叠加：这一刻是旧
        #  版本，下一刻是新版本，再下一刻又是旧版。」—— 要的就是整页 A/B 轮换。
        # 放在底栏而不是视图按钮那一行：① 上面那行已经排满（开始/停止 + 设置区
        # + 两个视图按钮，需求 1004px，窗宽 1080）；② 它是**滑动视图的操作**，
        # 和同一行左边的页码/缩放同属「看的时候用的东西」。
        # ⚠️ pack 顺序＝从右往左（side=RIGHT 依次贴右边界），所以先 pack 最右的
        #    频率下拉，再 pack 勾选框，最后才是那句说明文字。
        # 只有**频率**下拉留在底栏 —— 「闪不闪」已经由视图本身决定（进了叠加
        # 视图就在闪），再放一颗勾选框就是同一件事两个开关。
        # ⚠️ 2026-11-02 下午：最后一档是「关闭」—— 关掉自动化后，画面交给**底栏
        #    右侧那颗小开关**（左＝整页旧 / 右＝整页新），按一下就翻，不连续滑。
        self.cbo_blink = ttk.Combobox(self.toolbar, width=6, state="readonly",
                                      values=list(BLINK_ITEMS),
                                      font=(FONT, 9))
        self.cbo_blink.current(BLINK_DEFAULT)
        self.cbo_blink.bind("<<ComboboxSelected>>", self._on_blink_speed)
        self._attach_tip(
            self.cbo_blink,
            "一版停多久再翻成另一版（只在叠加视图里用得上）。\n"
            "更慢 0.6 秒：逐块细看；慢 0.3 秒：慢慢扫；中 0.15 秒：默认；\n"
            "快 0.08 秒：整页扫一眼（再快就糊成灰了）。\n"
            "选「关闭」→ 自动停止，改用左边那颗「旧 / 新」开关，\n"
            "点一下或拖过去就换版，想停多久停多久。")
        self.lbl_view_hint = tk.Label(
            self.toolbar, text="", bg=BG, fg=TEXT2, font=(FONT, 9), anchor="e")
        self.lbl_view_hint.pack(side=tk.RIGHT, padx=(0, 6))

        # ---- 手动「旧 / 新」开关（2026-11-02 晚，「关闭」挡位）----
        # 位置＝频率下拉的**左边**：它俩是同一件事的两半（自动闪 / 自己按）。
        # ⚠️ 2026-11-02 晚二改：这颗开关原来画在页面画布底部居中，用户原话
        #    「位置有点尴尬，这不是挡挡字了吗」—— 它压的正是正文最后几行。
        #    搬进底栏后永远压不着纸。搬过来的代价只有一处：得自己建一小块
        #    画布来画（胶囊 / 圆钮 / 两端字都不可能在 ttk 控件里现成拼出来），
        #    好处是**尺寸固定**、拖动只改画布内图形的 `coords`（0.24ms），
        #    既不会触发窗口重排，也彻底不用再跟视口滚动同步位置。
        self.canv_manual = tk.Canvas(
            self.toolbar, width=MANUAL_BOX_W, height=MANUAL_BOX_H,
            bg=BG, highlightthickness=0, bd=0, takefocus=0)
        self.canv_manual.bind("<Button-1>", self._blink_manual_press)
        self.canv_manual.bind("<B1-Motion>", self._blink_manual_drag)
        self.canv_manual.bind("<ButtonRelease-1>",
                              lambda _e: self._blink_manual_release())
        self._attach_tip(
            self.canv_manual,
            "手动换版：点一下 → 另一版；按住拖过中线也换版。\n"
            "只有频率选「关闭」时才可用（自动闪烁期间它是灰的）。")

        # ⚠️ 分类开关（三颗勾 + 色点）已挪到顶部 `self.opts` 设置区（2026-10-30）：
        #    用户要求「底图灰度、颜色勾选、OCR 兜底放在一块」。这里不再重复建，
        #    否则 `self.chk_kind` 会被后建的那份覆盖，界面上还会多出第二排色点。

        # ⚠️ 2026-10-19 栏标题（旧版/新版 · 分桶计数）**不再放工具栏**：
        #    顶部和底部状态栏各显一份完全相同的统计，属于重复；且底部那句
        #    「旧版标红 N 处」直接用的 `stats["marked"]`，把位移（浅蓝）也算进
        #    「标红」，用户看到「标红 41」而画面红框只有 3 个（41 = 删减 3 + 位移 38）。
        #    现改为：统计只在**底部状态栏**出现一次，按当时勾选的桶分列，
        #    与画面严格同源（`_update_counts`），顶部腾出空间。

        # ===== 下部：画布区（主体）=====
        # 中间加一层 `stage`：两种视图用的**布局管理器不同** —— 并排是 grid
        # （两列等分），滑动是 place（两块叠放，上层那块按比例裁宽）。同一个父
        # 容器里不同时用两种管理器，切换时整批撤掉、再按另一种摆一遍。
        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True)
        stage = tk.Frame(body, bg=BG)
        stage.pack(fill=tk.BOTH, expand=True)
        self.stage = stage
        stage.columnconfigure(0, weight=1)
        stage.columnconfigure(1, weight=1)
        stage.rowconfigure(0, weight=1)

        self.canv = {}
        self.vsb = {}
        self.hsb = {}
        self.holder = {}
        self._syncing = False          # 联动滚动防回环
        self._drag = None              # 鼠标拖拽平移状态
        self._last_zoom = 0.0          # 滚轮缩放节流时间戳（触控板会连发事件）
        for side in ("old", "new"):
            holder = tk.Frame(stage, bg=CARD,
                              highlightbackground=BORDER, highlightthickness=1)
            self.holder[side] = holder
            cv = tk.Canvas(holder, bg="#eef0f3", highlightthickness=0, takefocus=1)
            vsb = ttk.Scrollbar(holder, orient=tk.VERTICAL,
                                command=lambda *a, s=side: self._scroll(s, *a))
            hsb = ttk.Scrollbar(holder, orient=tk.HORIZONTAL,
                                command=lambda *a, s=side: self._hscroll(s, *a))
            cv.configure(yscrollcommand=lambda *a, s=side: self._from_bar(s, *a),
                         xscrollcommand=lambda *a, s=side: self._from_hbar(s, *a))
            self.canv[side] = cv
            self.vsb[side] = vsb
            self.hsb[side] = hsb
            # 滚轮 = 缩放，锚定鼠标位置（滚到哪就放大到哪）。
            # 「鼠标中键上下翻页」已取消：纵向滚动改由 Shift+滚轮 / 触控板双指 承担，
            # 平移直接用左键拖动（见下面 B1-Motion）。
            cv.bind("<MouseWheel>", lambda e, s=side: self._wheel_zoom(s, e))
            cv.bind("<Button-4>", lambda e, s=side: self._wheel_zoom(s, e))
            cv.bind("<Button-5>", lambda e, s=side: self._wheel_zoom(s, e))
            # Shift+滚轮：纵向滚动（两栏同步）
            cv.bind("<Shift-MouseWheel>", lambda e, s=side: self._wheel(s, e))
            cv.bind("<Shift-Button-4>", lambda e, s=side: self._wheel(s, e))
            cv.bind("<Shift-Button-5>", lambda e, s=side: self._wheel(s, e))
            # Alt+滚轮 / 鼠标左右滚轮：横向滚动
            cv.bind("<Alt-MouseWheel>", lambda e, s=side: self._wheel_h(s, e))
            # 左右滚轮：只有 Tk ≥ 9 认得 6/7 号键位，8.6 上绑就会抛 TclError，
            # 所以走 `_bind_opt` 容错（Windows 上这一条等于不生效，横向滚动走 Alt+滚轮）
            _bind_opt(cv, "<Button-6>", lambda e, s=side: self._wheel_h(s, e))
            _bind_opt(cv, "<Button-7>", lambda e, s=side: self._wheel_h(s, e))
            # 鼠标按住左键拖动 = 平移（放大后比滚动条快）；滑动视图下如果按在
            # 分割线附近，则改为拖分割线（分流在 `_drag_start` 里，见那里的注释）。
            cv.bind("<ButtonPress-1>", self._drag_start)
            cv.bind("<B1-Motion>", self._drag_move)
            cv.bind("<ButtonRelease-1>", self._release_drag)
            cv.bind("<Motion>", lambda e, s=side: self._swipe_motion(s, e))
            cv.bind("<Double-Button-1>", self._swipe_reset)
            cv.bind("<Configure>", lambda e, s=side: self._on_canvas_resize(s, e))

        # ===== 滑动视图的分割线 / 滑钮 =====
        # 2026-10-30 晚改：**不再是 place 上去的控件，而是画在画布里的图形**。
        # 原因见文件头 `SWIPE_STRIP` 那段 —— 控件几何一变，macOS 上的 Tk 会重画
        # 整个窗口（实测 72ms／次 = 14fps）；画布内部移动图形只要 0.24ms。
        # 现在这一层什么都不建，`_render_swipe` 里用 create_line / create_rectangle
        # 画出来（重建画布时一起重建）。
        self._swipe_items = []          # 旧版竖条的 item id（按 x 排序）
        self._swipe_strips = []         # 竖条的 PhotoImage（不持有引用 = 画布空白）
        self._swipe_src = None          # 旧版整图，边界条从这里现裁
        self._swipe_line_item = None    # 分割线 item
        self._swipe_knob_items = []     # 滑钮的两个 item
        self._swipe_img_w = 0           # 拼合图的像素宽（不是画布宽）
        self._swipe_img_h = 0
        self._swipe_shown = 0           # 当前完整显示的竖条根数（diff 用）
        self._swipe_edge_rem = None     # 边界条当前露出的像素宽度（None＝整条藏）
        self._swipe_edge_item = None    # 哪一根的 image 被换成了裁剪版
        self._swipe_edge_img = None     # 那张裁剪版（必须一直持引用，见 _swipe_apply）
        self._swipe_dragging = False    # 这次按下是不是在拖分割线
        self._swipe_hover = False
        self._blink_id = None          # 叠加闪烁的 after id（None＝没在闪）
        self._blink_t0 = 0.0           # 相位基准（time.time()）
        self._blink_old = None         # 当前画面停在哪一版：True 旧 / False 新 / None 未定位
        self._blink_idx = BLINK_DEFAULT  # 频率档位（和底栏下拉同步）
        # 手动「旧 / 新」开关（2026-11-02 晚，「关闭」挡位）：它住在**底栏**那块
        # 独立的小画布 `self.canv_manual` 里（紧挨频率下拉的左边），不是页面画布
        # 上的图形 —— 所以重建页面画布不影响它，也不用跟着滚动挪位置。
        # `_blink_side` 用 None 当「还没定过」的哨兵，第一次关掉自动化时按当时
        # 停的那一版定初始状态。
        self._blink_manual_ids = {}     # {"track"/"knob"/"lbl_old"/"lbl_new"}
        self._blink_side = None         # 0＝整页旧版、1＝整页新版、None＝未定位
        self._blink_manual_dragfrac = None   # 拖动中的钮位置（松手归 None，吸附）
        self._blink_manual_on = False   # 开关现在能不能按（自动开着时就灰）
        self._blink_manual_dragging = False   # 这次按下是不是在按开关
        self._blink_manual_moved = False      # 按下之后挪动过没有（决定松手翻不翻）
        self._blink_manual_press_x = 0.0      # 按下时的 x（控件坐标），算 3px 死区

        # 状态栏：**左**＝进度 / 耗时 / 提示，**右**＝两侧分桶统计。
        # 统计只在这里出现一次（顶部栏标题已撤），且与画面同源：`_update_counts`
        # 按当前勾选的桶统计，勾掉「位移」后数字立刻跟着画面变。
        sb = tk.Frame(self, bg="#eceef1", highlightbackground=BORDER,
                      highlightthickness=0)
        sb.pack(fill=tk.X, side=tk.BOTTOM)
        sb.columnconfigure(0, weight=1)          # 左侧提示可伸缩，右侧统计贴右
        tk.Label(sb, textvariable=self.status_var, bg="#eceef1", fg=TEXT2,
                 font=(FONT, 10), anchor="w").grid(
            row=0, column=0, sticky="ew", padx=(12, 8), pady=4)
        self.lbl_old = tk.Label(sb, text="旧版 PDF", bg="#eceef1", fg=TEXT,
                                font=(FONT, 10), anchor="e")
        self.lbl_old.grid(row=0, column=1, sticky="e", padx=(8, 20), pady=4)
        self.lbl_new = tk.Label(sb, text="新版 PDF", bg="#eceef1", fg=TEXT,
                                font=(FONT, 10), anchor="e")
        self.lbl_new.grid(row=0, column=2, sticky="e", padx=(0, 16), pady=4)

        # ===== 页码 / 缩放沉到底（2026-10-30）=====
        # ⚠️ 顺序决定谁更贴底：状态栏先 pack（最底），工具条后 pack（落在它上方）。
        #    反过来写，页码那一行会被挤到状态栏下面、或者干脆看不见。
        self.toolbar.pack(side=tk.BOTTOM, fill=tk.X)

        # 视图按钮的选中态 + 画布布局，建完就摆一次（此时还没有文档，
        # `render()` 会走「没有页面可看」那一支，安全）。
        self._apply_view_mode()

        # 界面建完才量得准宽度（这时 f3 还没布局，`_fit_chips` 会自己跳过），
        # 等第一轮布局落定再摆一次四格文案。
        self.after(60, self._fit_chips)

    # ---------------- 视图模式（并排 / 滑动） ----------------
    def _set_view(self, mode):
        """切换视图：并排 ↔ 滑动。只换布局，不重算、不碰任何判定。"""
        if mode not in VIEW_MODES or mode == self.view_mode.get():
            return
        self.view_mode.set(mode)
        self._apply_view_mode()

    def _apply_view_mode(self):
        """按当前模式摆一次画布，并刷新三颗按钮的选中态。

        · 并排：两块 grid 到两列（5.0 以来的样子）；
        · 滑动 / 叠加：**只留新版那一块铺满整窗**，旧版整块收起 —— 它的内容由
          `_render_swipe` 画进新版那块画布的同一个坐标系里（切成竖条）。
          两者用的是**同一套渲染**（同一块画布、同一批竖条），差别只在：
          滑动按 `_swipe_pos` 露出左半旧、右半新；叠加把整片竖条同亮同灭，
          并藏起分割线和滑钮。所以叠加不是另写一条渲染路径，只是裁剪参数的
          两种走法 —— 换页/缩放/滚动的行为天然一致。

        ⚠️ 滑动视图为什么不再用「两块叠起来 + 旧版 place 改宽度」：
           那条路每次拖动都要改旧版控件的宽度，而 macOS 上的 Tk 只要**任何控件
           几何变化**就重画整个窗口 —— 实测 1240×740 窗口下挪一下 2px 的线要
           72ms（＝14fps），窗口缩到 1080×560 时 46.7ms，正好是面积比。而画布
           **内部**移动图形只要 0.24ms、切换显隐 0.11ms（差 300 倍）。
           见文件头 `SWIPE_STRIP` 的对照实验。
        ⚠️ 顺带解决的旧问题：以前两块画布各有一个 yview，要靠 `_sync_scrollbars`
           把它们对到同一个比例，差一像素两侧就错位。现在只有一块画布、两张图
           在同一个坐标系里，滚动天然同步，不可能错位。
        """
        mode = self.view_mode.get()
        swipe = mode in (VIEW_SWIPE, VIEW_BLINK)
        for side in ("old", "new"):
            self.holder[side].grid_forget()
            self.holder[side].place_forget()
        if swipe:
            self._pack_holder("new", bars=True)
            self.holder["new"].place(x=0, y=0, relwidth=1.0, relheight=1.0)
        else:
            for side in ("old", "new"):
                self._pack_holder(side, bars=True)
            for side, col in (("old", 0), ("new", 1)):
                self.holder[side].grid(
                    row=0, column=col, sticky="nsew",
                    padx=(6 if col else 2, 6))
        for key, btn in self._view_btns.items():
            btn.configure(style="ViewOn.TButton" if key == mode
                          else "ViewOff.TButton")
        # 频率下拉 + 手动开关只在叠加视图里出现：它们是叠加的两个参数，
        # 摆在滑动/并排下会让人以为那两个视图也有「闪不闪」这回事。
        self._pack_tool_right(blink=(mode == VIEW_BLINK),
                              manual=(mode == VIEW_BLINK))
        if mode != VIEW_BLINK:
            # 切走了就停：定时器不停，它会继续对着新画布的条带翻，把并排两侧
            # 搅成一片；而且 `_blink_exit` 负责把线和钮放回来。
            self._blink_stop()
        if mode == VIEW_BLINK:
            # 叠加时线和钮都藏着，再写「拖中间那根线」就是骗人。
            # 2026-11-02 下午多了「关闭自动化」：那时该说的是底部那颗开关。
            if self._blink_auto():
                self.lbl_view_hint.config(text="旧 ↔ 新 · 整页交替闪现 · 切走即停")
            else:
                self.lbl_view_hint.config(text="旧 / 新 · 点右侧小开关换版")
        elif mode == VIEW_SWIPE:
            self.lbl_view_hint.config(text="左＝旧版 · 右＝新版 · 拖中间那根线")
        else:
            self.lbl_view_hint.config(text="")
        # ⚠️ 切完视图必须**重新适应一次宽度**。并排时一页只占半窗（约 588px），
        #    滑动时一页要铺满整窗（约 1240px）；不重算 dpi 的话，滑动视图里
        #    图还是半宽，右半边全是画布底色 —— 看着像「新版没渲染出来」，
        #    其实是图本来就没那么大，`create_image(0,0)` 又贴着左边放。
        #    代价：手动放大过的档位会被重置回「适应宽度」。切换视图是低频操作，
        #    而且「换个看法」本来就该重新铺满，这个取舍是划算的。
        # ⚠️ 必须先把几何算完再量宽度：place 只是登记，`winfo_width()` 要等
        #    一次 idle 才会变成新值，否则量到的还是切换前那半窗宽（实测从 71
        #    只涨到 73 dpi，等于没适应）。
        self.update_idletasks()
        self._auto_fit = True
        self.render()

    def _pack_tool_right(self, blink, manual=False):
        """底栏右端三件（频率下拉 / 手动开关 / 那句说明）按固定顺序重排。

        ⚠️ `pack_forget()` 会把 packing 顺序一起丢掉：频率下拉收起来再放回去时，
           如果直接在最后 pack，它会跑到说明文字的**右边**（side=RIGHT 后 pack 的
           更贴右边界），窗口一换视图那段文字就左右横跳。所以整组撤、按固定顺序
           重新 pack（和 `_pack_holder` 同一个道理）。

        从右往左的最终次序：`[说明文字] [旧/新 开关] [频率下拉]`。
        开关紧跟在下拉左边 —— 用户原话「你应该放在那个开关的左边一点」。
        """
        for w in (self.cbo_blink, self.canv_manual, self.lbl_view_hint):
            w.pack_forget()
        if blink:
            self.cbo_blink.pack(side=tk.RIGHT, padx=(8, 0))
        if manual:
            self.canv_manual.pack(side=tk.RIGHT, padx=(10, 0))
        self.lbl_view_hint.pack(side=tk.RIGHT, padx=(0, 6))

    def _pack_holder(self, side, bars=True):
        """holder 里三件（画布 / 竖条 / 横条）按固定顺序重排一次。

        `pack_forget()` 会把 packing 顺序也丢掉，所以必须整组撤、再按固定顺序
        pack 回来；只藏其中一根的话，它再 pack 时会排到画布右边去。
        """
        cv, vsb, hsb = self.canv[side], self.vsb[side], self.hsb[side]
        for w in (cv, vsb, hsb):
            w.pack_forget()
        if bars:
            vsb.pack(side=tk.RIGHT, fill=tk.Y)
            hsb.pack(side=tk.BOTTOM, fill=tk.X)
        cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    def _on_rep(self, a, b, rep):
        """后台体检回来了（主线程）：**只点亮四类、只写状态栏，不弹窗也不置灰**。

        ⚠️ 2026-10-30 晚用户改口径（原话见 `_prejudge`）：丢文件可能只是「丢进来
           当查看器看看」，被模态框拦住最烦。所以这里连 `block`（不能比）也只写
           一句状态栏，按钮照旧可点 —— 真正的拦截与提醒都在 `_gate_image_only`
           （点「开始对比」那一刻）。「看文件」和「比文件」彻底分两步。
        """
        if (a, b) != self._rep_key:
            return                          # 用户已经又换了文件，这是过期结论
        self._rep_busy = False
        self._rep = rep
        if not rep:
            self._set_class((), None)
            self.status_var.set("文件类型没判出来（不影响对比，开跑前会再查一次）")
            return
        self._set_class(pre.class_of_pair(rep), pre.class_text(rep))
        # 预判与「打开原件预览」是两条异步线，谁后到谁都能把状态栏覆盖掉 ——
        # 这里在已有那句后面接，用户看到的永远是「原件已显示 + 类型结论」一句。
        _st = self.status_var.get()
        _pre = (_st + "　·　") if _st.startswith("已显示原件") else ""
        level = rep.get("level")
        if level == "block":
            # 不弹框、不置灰：只把「为什么不能比」摆在状态栏（翻看原件完全不受影响）
            _why = gate.block_reason(self._rep) or "这份不能对比"
            self.status_var.set(
                _pre + "不能对比：%s（仍可翻看原件；点「开始对比」会说清原因）" % _why)
            return
        if level != "warn":
            self.status_var.set(_pre + "类型已确认，可以直接比")
            return
        self.status_var.set(_pre + "这份读不出文字，点「开始对比」时会问你一句")

    # ---------------- 标注文档 ----------------
    def _rgb_hex(self, rgb) -> str:
        """把 (0..1, 0..1, 0..1) 的浮点颜色转成 Tk 用的 #RRGGBB。"""
        r, g, b = (int(round(max(0.0, min(1.0, c)) * 255)) for c in rgb)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _swatch(self, parent, outer, inner=None):
        """工具条上的色点：单色画「●」，双色画「外圈 A + 内芯 B」的 13px 小圆。

        双色点是 2026-10-30 加的，当时一颗勾会管**两种看起来不一样**的框
        （位移勾管「纯浅蓝」和「紫底蓝边」两种）。2026-11-02 紫框统一纯紫后，
        现在**没有一勾两长相**了，所以 `_KIND_ROWS` 里全是单色点 ——
        但双色这条路保留着：真再出现那种情况，填个 (外圈, 内芯) 就能用，
        不用回头改 UI 代码。

        方向规则：**外圈 = 边框色，内芯 = 填充底色** —— 和画面上框的长相同构。
        ⚠️ 这条规则用户纠正过一次：他先说「里面是蓝色外面一圈是紫色」，
           紧接着又明确「应该是蓝色一圈包着紫色」。按后者为准（外圈蓝、内芯
           紫）。所以别照抄第一句，也别再写反。
        """
        if inner is None:
            return tk.Label(parent, text="●", bg=BG,
                            fg=self._rgb_hex(outer), font=(FONT, 10))
        size = 13
        cv = tk.Canvas(parent, width=size, height=size, bg=BG,
                       highlightthickness=0, bd=0)
        cv.create_oval(0.5, 0.5, size - 0.5, size - 0.5,
                       fill=self._rgb_hex(outer), outline="")
        cx = cy = size / 2.0
        r = 3.2
        cv.create_oval(cx - r, cy - r, cx + r, cy + r,
                       fill=self._rgb_hex(inner), outline="")
        return cv

    def _pending_has_other(self) -> bool:
        """这批结果里有没有紫框 —— 决定「待判定」那颗勾能不能点。

        ⚠️ 2026-11-02：紫框统一纯紫之后，紫桶不再分「位移 / 其余」两档，
           所以「有紫框」就等于「这颗勾有东西可画」，判据回到最简单的一句。
           （原来的名字没改：调用点只有一处，改名收益为零。）
        """
        mk = self.marks or {}
        for side in ("old", "new"):
            bk = ((mk.get(side) or {}).get("by_kind") or {}).get(
                ann.KIND_PENDING) or {}
            for rs in bk.values():
                if rs:
                    return True
        return False

    def _enabled_kinds(self) -> tuple:
        """当前该画哪些颜色桶（按 `KIND_ALL` 钉序，紫在最前 = 垫底画）。

        两道闸都要过：
          ① 用户勾了没有（`kind_on`，紫那颗默认开、可以关）；
          ② 这批结果**有没有**紫框 —— `locate_marks` 返回的 `fast_mode` 为假
             就说明整批没有 `KIND_PENDING` 条目（纯原生文字文件），勾着也画不出
             东西，这里直接剔掉，免得统计栏列出「待判定 0」这种噪声。
        换一批结果时显隐自动跟着那批变，不会出现「上次的紫还留在画面上」。
        """
        on = {k for k in ann.KIND_ALL if self.kind_on[k].get()}
        if not (self.marks or {}).get("fast_mode"):
            on.discard(ann.KIND_PENDING)
        return tuple(k for k in ann.KIND_ALL if k in on)

    def _refresh_pending_check(self):
        """紫那颗勾能不能点：这批结果里有紫框才点得动，没有就置灰。

        ⚠️ 纯原生 PDF 根本不产紫框，勾着也没东西可画 —— 置灰比"画不出来"
            更早地把这件事告诉用户（见 `_pending_has_other`）。
        ⚠️ 只**置灰**，不改勾选状态 —— 用户关过一次，下一批有紫框时该保持关着，
            否则他每次都得重新关一遍。
        """
        self.chk_kind[ann.KIND_PENDING].config(
            state="normal" if self._pending_has_other() else "disabled")

    # ---------------- 「开始对比」那行右端那排四类胶囊 ----------------
    def _set_class(self, classes, texts=None):
        """点亮命中的类别格，其余保持灰。`classes` 为空 → 四格全灰。

        `texts` = `pre.class_text(rep)`，命中的格子换成「这一大类里的哪一种」
        （② 混合型·原生+图片 / ③ 纯转曲）。不给就四格都写预设短名 ——
        换文件、清空、还没比对时都是这种状态。

        ⚠️ 文案与底色分开管：`_fit_chips()` 只改字，不改色。窄窗退回短名时
           底色仍然是命中色，用户不会误以为「什么都没判出来」。
        """
        self._cls_on = tuple(sorted(int(c) for c in (classes or ())))
        self._cls_texts = {int(k): v for k, v in (texts or {}).items()}
        for c, lb in self.cls_chips.items():
            if c not in self._cls_on:
                lb.config(bg=CHIP_OFF_BG, fg=CHIP_OFF_FG)
            elif c == pre.CLASS_SCAN:
                lb.config(bg=CHIP_ALERT_BG, fg=CHIP_ON_FG)   # 扫描件 = 不让比
            else:
                lb.config(bg=CHIP_ON_BG, fg=CHIP_ON_FG)
        self._fit_chips()

    def _fit_chips(self):
        """四格文案：先试「命中 → 具体哪一种」，这一行放不下就退回预设短名。

        为什么要试两次：命中后 ②③ 会变长（「② 混合型·原生+图片+转曲」比
        「② 混合型」宽一倍），900px 极限窗下这一行放不下。Tk 的 pack 空间
        不够时**既不换行也不省略**，而是把最后 pack 的几格压到比文字还窄，
        汉字被切成半个（2026-10-29 顶部行实测 141px / 需 277px）。宁可少说
        一句细分，也不能出现半个字 —— 细分在悬停提示里还有一份（`CLASS_TIP`）。

        判据用 `row.winfo_reqwidth() > row.winfo_width()`：Frame 的 reqwidth
        就是子控件要求的总和（含 padx），比逐个加 padx 可靠。
        """
        if getattr(self, "_fitting", False) or not getattr(self, "cls_chips", None):
            return
        try:
            if not self.winfo_exists():
                return      # 窗口已关，`after` 排的回调还会醒一次
            row = self.cls_chips[pre.CLASS_ORDER[0]].master
            if row.winfo_width() <= 1:
                return          # 还没布局完（启动阶段），等 <Configure> 再来
        except tk.TclError:
            return
        self._fitting = True
        try:
            for c, lb in self.cls_chips.items():
                lb.config(text=self._cls_texts.get(c) or pre.chip_label(c))
            self.update_idletasks()
            # 留 8px 呼吸：正好贴死时左右两组看着像连在一起（900px 实测），
            # 也让 Windows 上更宽的字体自动落到「退回短名」这一支。
            if row.winfo_reqwidth() <= row.winfo_width() - 8:
                self._show_long = True
                return
            for c, lb in self.cls_chips.items():
                lb.config(text=pre.chip_label(c))
            self.update_idletasks()
            self._show_long = row.winfo_reqwidth() <= row.winfo_width()
        finally:
            self._fitting = False

    def _attach_tip(self, w, text):
        """鼠标停一会儿弹出说明（Tk 没有内建 tooltip，自己搭一个）。

        为什么要有：四格只写四个短名，而用户 2026-10-29 追问「混合型是哪几种
        混合？图片类是纯图片还是纯转曲？」—— 枚举太长，一行放不下，只能挂在
        悬停里。好处是**没比对之前**就能读到「这一类底下还有哪几种」，
        不必等跑完才明白自己这份属于哪种。

        `_tip_refs` 存住闭包，否则 `st` 被回收后 tooltip 再也弹不出来。
        """
        st = {"win": None, "job": None}

        def _open():
            st["job"] = None
            if st["win"] is not None or not w.winfo_exists():
                return
            tw = tk.Toplevel(w)
            tw.wm_overrideredirect(True)
            try:
                tw.attributes("-topmost", True)
            except tk.TclError:
                pass
            tk.Label(tw, text=text, bg="#fffdf2", fg=TEXT, font=(FONT, 9),
                     justify="left", anchor="w", padx=9, pady=7,
                     wraplength=430,          # 别让它横着长出一条带子
                     highlightbackground=BORDER,
                     highlightthickness=1).pack()
            tw.update_idletasks()
            h, wd = tw.winfo_reqheight(), tw.winfo_reqwidth()
            # 默认左对齐胶囊；比胶囊宽时改成右对齐 —— 这一排本来就贴窗口右端，
            # 右对齐才不会让气泡伸到窗口外面去（看着像跑了）。
            x = min(w.winfo_rootx(), w.winfo_rootx() + w.winfo_width() - wd)
            x = max(0, min(x, w.winfo_screenwidth() - wd - 4))
            y = w.winfo_rooty() + w.winfo_height() + 6
            # 别顶出屏幕下沿：贴着下边界时翻到胶囊上方
            if y + h > w.winfo_screenheight() - 4:
                y = max(0, w.winfo_rooty() - h - 6)
            tw.wm_geometry("+%d+%d" % (x, y))
            st["win"] = tw

        def _enter(_e=None):
            if st["win"] is None and st["job"] is None:
                st["job"] = w.after(350, _open)

        def _leave(_e=None):
            if st["job"] is not None:
                try:
                    w.after_cancel(st["job"])
                except Exception:
                    pass
                st["job"] = None
            if st["win"] is not None:
                try:
                    st["win"].destroy()
                except Exception:
                    pass
                st["win"] = None

        w.bind("<Enter>", _enter, add="+")
        w.bind("<Leave>", _leave, add="+")
        w.bind("<ButtonPress>", _leave, add="+")
        self._tip_refs.append(st)

    def _update_counts(self):
        """状态栏右侧两侧统计，按**当前勾选的桶**统计（与画面严格同源）。

        ⚠️ 两条历史教训，都别再犯：
        1. 不能用 `stats["marked"]`：它含**所有桶**（删减+新增+位移）和像素框，
           勾掉位移后画面只有 3 个红框、数字却写 41（= 删减 3 + 位移 38）。
        2. 状态行文案不能再写成「标红 N 处」：那等于把浅蓝的位移算成红框，
           用户一眼就发现数字和画面对不上（2026-10-19 DOC-1009 实测）。
        现在每桶分列（「删减 3　位移 38」），谁是谁一目了然。
        """
        if not self.marks:
            # 新一轮开始时 `_run` 会把 marks 清掉（画面已切回无标记原件）——
            # 右上角那两个计数必须跟着归零，否则会出现「画面干干净净，
            # 标签还写着上一轮的『删减 3』」（2026-10-29 接入预览后新增的分支）。
            for side, lbl in (("old", self.lbl_old), ("new", self.lbl_new)):
                name = "旧版 PDF" if side == "old" else "新版 PDF"
                lbl.config(text=name + "　·　无标注")
            return
        en = self._enabled_kinds()      # 含紫（勾着、且这批有紫框时），顺序即绘制顺序
        for side, lbl in (("old", self.lbl_old), ("new", self.lbl_new)):
            bk = (self.marks.get(side) or {}).get("by_kind") or {}
            # 一桶一列，按 `LEGEND_ORDER` 输出，跟工具条色点同序。
            # ⚠️ 2026-11-02：紫框统一纯紫后，紫桶不再有「判出位移的那几条」
            #    要并进位移列 —— 数字与画面同源的规矩没变，只是不用再拆了。
            cnt: dict = {}
            for k in en:
                for rs in (bk.get(k) or {}).values():
                    cnt[k] = cnt.get(k, 0) + len(rs)
            bits = [f"{ann.KIND_TEXT[k]} {cnt[k]}"
                    for k in LEGEND_ORDER if cnt.get(k)]
            name = "旧版 PDF" if side == "old" else "新版 PDF"
            lbl.config(text=name + ("　·　" + "　".join(bits) if bits else "　·　无标注"))

    def _on_pair_toggle(self):
        """「增删」那颗勾：一次改**两个**桶（删减红 + 新增绿）。

        2026-10-29 用户要求合并（原话：「红绿新增删减……给合并为一个按钮，就是
        一键判断，一键打开这个红绿显示的开关」）。合并的是操作，语义仍然是两个
        桶 —— 用户看的是「这次开不开红绿」，不该被逼着分两次点。

        ⚠️ 只把 ADD 同步成 DEL 的值，不反向回写：DEL 是这颗勾的 variable，
           它才是「勾没勾」的唯一真值来源。
        """
        v = bool(self.kind_on[ann.KIND_DEL].get())
        if bool(self.kind_on[ann.KIND_ADD].get()) != v:
            self.kind_on[ann.KIND_ADD].set(v)
        self._on_kind_toggle()

    def _on_kind_toggle(self):
        """分类开关切换：注释要**重画**（内容流里的标记摘不掉，整个重开重画）。

        2026-10-30：这条路已经压到 0.4s 以内 —— 重开文档本来就是毫秒级
        （`fitz.open` 懒加载），真正的耗时是画标记，见 `_paint_layers` 的说明。
        """
        if not self.marks:
            return
        self._reset_view = False
        self._add_annots(keep_cache=True)
        self._update_counts()
        self.render()
