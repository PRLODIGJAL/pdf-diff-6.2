# -*- coding: utf-8 -*-
"""
ui_swipe.py —— 并排之外的**另外两种看法**：滑动分割、叠加闪烁。
==================================================================
两种都只用现成的两张渲染图，不重算、不重新比：

  `swipe`  一块 canvas 里放两张图，旧版切成竖条，拖拽只切条带的显隐。
           为什么不用「改左侧 canvas 宽度」：macOS/Tk 8.6 上任何 widget 几何
           变化都会重画整个 toplevel，成本只跟**窗口面积**有关（实测挪 2px
           花 72ms ＝ 14fps）；而同一块 canvas 内部切图形显隐只要 0.11ms。
  `blink`  整页在两版之间来回闪。改过的地方每帧都在跳，没改的地方两帧完全
           重合 —— 跳的那一小块自己会跳出来（天文上找新星就是这么干的）。

           2026-11-02 下午加了一档「关闭」：不自动翻，交给用户自己切。下午
           那版「手动」画的是一条横跨画布的长滑轨，用户一眼看成「滑动分割」，
           语义就废了；当晚改成**开关**（左＝整页旧、右＝整页新，越过中线
           瞬间切换，没有中间态）—— 叠加的手动操作只有两态，中间的每一个像素
           都是不该存在的状态。开关先是画在视口底部，用户又说「这不是挡挡字了
           吗」，于是搬进底栏、紧挨频率下拉的左边（见 `_blink_manual_build`）。

⚠️ 两个都不改 `marks`、不改文档、不写配置：切回并排就完全回到原样。
   这条有断言守着（`dev/_regress/ui_view_test.py`）。手动开关同理：它只动
   底栏那块小画布，切视图就清空，不留任何状态。


2026-11-02 目录重构时从 `diff_app5.py` 原样切出来，逻辑一个字节没改。
"""

from __future__ import annotations

import os
import sys
import time

import tkinter as tk

# 本文件是 ui 层的一个模块，单独 import 也要能用：先把 `app/` 塞进 sys.path，
# 再让 `paths.install()` 把 ui/judge/engine/shell 四层一起铺开。
_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP not in sys.path:
    sys.path.insert(0, _APP)
import paths as _paths                                          # noqa: E402

_paths.install()

from ui_theme import (                                          # noqa: E402
    BLINK_DEFAULT, BLINK_FRAME_MS, BLINK_OFF_IDX,
    BLINK_PERIODS, BLINK_TAG, BORDER, CARD, FONT, MANUAL_BOX_H, MANUAL_BOX_W,
    MANUAL_EDGE, MANUAL_H, MANUAL_INSET, MANUAL_KNOB, MANUAL_KNOB_EDGE,
    MANUAL_KNOB_OFF, MANUAL_KNOB_R, MANUAL_LBL_GAP, MANUAL_OFF_EDGE,
    MANUAL_TRACK, MANUAL_TRACK_OFF, MANUAL_TRACK_ON, MANUAL_W,
    SWIPE_GRAB, SWIPE_LINE, SWIPE_STRIP, TEXT, TEXT2,
    VIEW_BLINK, VIEW_SWIPE, VIEW_SWIPE_MAX, VIEW_SWIPE_MIN, VIEW_SWIPE_POS,
)

#: 手动开关所有画布图形的公共 tag —— 重建/删除一次调用搞定，也不用逐个记 id。
MANUAL_TAG = "blinkmanual"


class SwipeMixin:
    """滑动分割 + 叠加闪烁。`App` 继承它。"""

    # ---------------- 滑动视图：一块画布装两版 ----------------
    def _render_swipe(self):
        """把新旧两版画进**同一块画布**：新版整张垫底，旧版切成竖条盖在上面。

        层叠顺序（后建的在上）：新版图 → 旧版竖条 → 分割线 → 滑钮。

        ⚠️ 为什么非要同一块画布：两块画布叠放时，上面那块（旧版）的**底色是
           不透明的**，竖条之间和右侧隐藏区域的空白会直接把下面的新版盖掉 ——
           画面变成「左边旧版、右边一片灰」。同一块画布里就没有这种底色问题。
        ⚠️ 竖条只切旧版：新版要整张露出，不需要裁。
        """
        cv = self.canv["new"]
        cv.delete("all")
        self._swipe_items = []
        self._swipe_strips = []
        self._swipe_line_item = None
        self._swipe_knob_items = []
        self._swipe_img_w = 0
        self._swipe_img_h = 0
        self._swipe_shown = 0
        self._swipe_edge_rem = None
        self._swipe_edge_item = None
        self._swipe_edge_img = None
        self._swipe_src = None

        doc_n, doc_o = self.docs.get("new"), self.docs.get("old")
        img_n = img_o = None
        if doc_n is not None and self.page < doc_n.page_count:
            img_n = self._cached_pixmap("new", doc_n[self.page])
        if doc_o is not None and self.page < doc_o.page_count:
            img_o = self._cached_pixmap("old", doc_o[self.page])

        if img_n is None and img_o is None:
            cv.configure(scrollregion=(0, 0, 10, 10))
            if doc_o is not None or doc_n is not None:
                _tip = "（两侧都没有这一页）"
            elif self._has_view():
                _tip = "（这两个文件打不开）"
            else:
                _tip = "把两个版本拖进上方，这里先显示原件"
            cv.create_text(20, 20, anchor="nw", fill="#6b7280",
                           font=(FONT, 11), text=_tip)
            return

        # 只有一侧有这一页时，就把它铺满整幅（没有可对比的另一侧）
        base = img_n if img_n is not None else img_o
        self._imgs["new"] = base
        self._imgs["old"] = img_o if img_o is not None else base
        cv.create_image(0, 0, anchor="nw", image=base)
        self._swipe_img_w = base.width()
        self._swipe_img_h = base.height()

        if img_n is not None and img_o is not None:
            self._build_swipe_strips(img_o)
            self._swipe_build_overlay()
            self._swipe_apply(self._swipe_pos, force=True)

        cv.configure(scrollregion=(0, 0, self._swipe_img_w, self._swipe_img_h))
        if self._reset_view:
            cv.yview_moveto(0)
            cv.xview_moveto(0)
        # 画布刚重建（换页 / 缩放 / 勾选 / 刚切到滑动）—— 闪烁要按新的 item 重新
        # 起一次，否则定时器还指着已经 delete 掉的条带，等于不闪。
        # ⚠️ 手动开关**不用**跟着重建：它住在底栏那块独立画布里，页面画布的
        #    `delete("all")` 碰不到它（这是它从画布搬到控件后白赚的一条 ——
        #    上一版这里必须清 id 再重建，漏了就是「开关凭空消失」）。
        self._blink_kick()


    def _build_swipe_strips(self, img):
        """旧版整图 → 一排等宽竖条（每根一个 image item）。

        切一次 38 根在实测里约 80ms，只在重建画布时发生（换页 / 缩放 / 切视图 /
        开跑前后）。拖动时只切换这些条带的 `state`，不再重建。
        """
        cv = self.canv["new"]
        W, H = img.width(), img.height()
        strips, items = [], []
        x = 0
        while x < W:
            w = min(SWIPE_STRIP, W - x)
            p = tk.PhotoImage(master=self, width=w, height=H)
            p.tk.call(p, "copy", img, "-from", x, 0, x + w, H, "-to", 0, 0)
            strips.append(p)
            # ⚠️ 必须打 tag：整页 A/B 切换靠 `itemconfigure(BLINK_TAG, ...)`
            #    一次调用翻 38 根，逐根循环要 4ms（一帧预算的 1/4）。
            items.append(cv.create_image(x, 0, anchor="nw", image=p,
                                         tags=(BLINK_TAG,)))
            x += w
        self._swipe_strips = strips
        self._swipe_items = items
        self._swipe_src = img

    def _swipe_build_overlay(self):
        """分割线 + 滑钮，都画成画布里的图形（不是控件）。"""
        cv = self.canv["new"]
        h = self._swipe_img_h
        self._swipe_line_item = cv.create_line(
            0, 0, 0, h, fill=SWIPE_LINE, width=2)
        cy = cv.canvasy(0) + max(30, cv.winfo_height()) // 2
        self._swipe_knob_items = [
            cv.create_rectangle(0, 0, 0, 0, fill=CARD, outline=BORDER),
            cv.create_text(0, 0, text="◀▶", fill=TEXT2, font=(FONT, 7)),
        ]
        self._swipe_place_knob(0, cy)

    def _swipe_place_knob(self, cx, cy):
        """滑钮跟着分割线横move；纵向永远钉在**视口**中心。

        ⚠️ 不能画在图像中心：一页图 1719px 高、视口只有 476px，画在图像中心
           就跑到视口外面去了（用户看不到能拖的东西）。
        """
        if not self._swipe_knob_items:
            return
        cv = self.canv["new"]
        rect, txt = self._swipe_knob_items
        cv.coords(rect, cx - 13, cy - 18, cx + 13, cy + 18)
        cv.coords(txt, cx, cy)

    def _swipe_sync_knob(self):
        """滚动后把滑钮钉回视口中心（它纵向跟着视口走，不跟图像走）。

        ⚠️ 必须单独处理：滑钮是画布里的图形，画布一滚它就跟着滚 —— 一页图
           1719px 高、视口才 476px，滚两下就跑到看不见的地方，用户会以为
           「拖不动了」。分割线不用管，它本来就横跨整幅图。
        """
        if self._swipe_line_item is None or not self._swipe_knob_items:
            return
        cv = self.canv["new"]
        self._swipe_place_knob(self._swipe_px(),
                               cv.canvasy(0) + max(30, cv.winfo_height()) // 2)
        # 叠加视图那颗「旧 / 新」开关从这儿起不再需要重新定位：它住在底栏，
        # 不跟视口滚动（上一版钉在视口底部，每次滚动/缩放都得挪一次）。


    def _swipe_px(self):
        """分割线在画布**内容坐标**里的 x（像素）。"""
        return int(round(self._swipe_pos * max(1, self._swipe_img_w)))

    def _edge_image(self, k, rem):
        """第 k 根竖条只取左边 rem 像素 —— 让分割线精确到 1px。

        没有这一步，分割线只能落在 32px 的倍数上（条宽），停手时会看出线右边
        多露出最多 31px 的旧版内容 / 遮住 31px 的新版内容。
        成本：裁一条 32×1719 约 1ms，只在分割线跨过像素时发生（同一位置不重裁）。
        """
        src = self._swipe_src
        H = src.height()
        x0 = k * SWIPE_STRIP
        img = tk.PhotoImage(master=self, width=rem, height=H)
        img.tk.call(img, "copy", src, "-from", x0, 0, x0 + rem, H, "-to", 0, 0)
        return img

    def _swipe_uncrop_edge(self):
        """把「被裁到 rem 像素」的那根边界条还原成整根原竖条。

        ⚠️ 这是叠加视图的一处**必须**做的复位。滑动视图为了 1px 精度，会把分割线
           所在那根条带换成 `rem` 像素宽的裁剪版（`_edge_image`）；叠加视图里整页
           翻旧版时，这根条只有 `rem` 像素宽，右边那 `32-rem` 像素是**透明**的 ——
           底下露出的正是新版。于是整页在 A/B 之间跳，唯独这一竖列永远是新版，
           看着就是「有一列不跟着动」。用户实测截图里那根竖条就是这个。

        `rem` 落在 (0, 32) 的任意值上，缺口就有 1~31px 宽，位置又正好在分割线处
        （通常就是页面中间），所以在快速闪烁时最扎眼。
        """
        j, cnt = self._swipe_edge_item, len(self._swipe_items)
        if j is None:
            return
        if 0 <= j < cnt:
            # ⚠️ 顺序：先把图换回原竖条，再放手引用 —— 裁剪图一旦没人指，Tk 就删。
            self.canv["new"].itemconfigure(self._swipe_items[j],
                                           image=self._swipe_strips[j])
        self._swipe_edge_item = None
        self._swipe_edge_img = None
        self._swipe_edge_rem = None

    def _swipe_apply(self, frac=None, force=False):
        """按分割位置显隐竖条 + 挪线挪钮。**只改画布内部图形，不动任何控件几何。**

        diff 是关键：每次只处理「显示的根数」变化的那一段，典型 0~1 根。
        全量刷 38 根约 4ms（实测 152 根要 16ms），一帧就吃掉 1/4 预算。

        ⚠️ 2026-11-02 晚：`lo`/`hi` 两个参数删掉了。它们是给上一版「长滑轨」
           留的（那条要能压到 0/1 才算纯一版）；现在手动模式改走
           `_blink_paint`（整页切一版），走不到这里了，留着就是两个永远拿默认
           值的死参数 —— 看着像"这里支持 0~1"，实际没有任何调用点会传。

        ⚠️ 边界条换图时的**引用顺序**不能乱：PhotoImage 一旦在 Python 侧没人
           引用，Tk 里那张图就被删掉；画布上的图形还指着它的话，下一次
           `itemconfigure` 会抛 `image "pyimageN" does not exist`（实测踩过）。
           所以这里守住两条：① 旧边界条换回原图**之前**不释放任何东西；
           ② `_swipe_edge_img` 一律在 `itemconfigure` **之后**才改，
           保证整个替换过程中旧图始终有引用。
        """
        if frac is not None:
            self._swipe_pos = max(VIEW_SWIPE_MIN,
                                  min(VIEW_SWIPE_MAX, float(frac)))
        cv = self.canv["new"]
        if self._swipe_line_item is None:
            return
        x = self._swipe_px()
        cv.coords(self._swipe_line_item, x, 0, x, self._swipe_img_h)
        cy = cv.canvasy(0) + max(30, cv.winfo_height()) // 2
        self._swipe_place_knob(x, cy)

        n = len(self._swipe_items)
        k, rem = divmod(x, SWIPE_STRIP)
        if force:
            # ⚠️ 新建的 item 默认就是 normal（可见）。不在这里统一压成 hidden，
            #    右侧那几十根竖条会一直盖着新版 —— 画面看着「左边旧版、右边还是
            #    旧版」，而且因为两版这一段文字常常一样，肉眼极难发现。
            #    先全压成 hidden、`_swipe_shown` 归 0，让下面的 diff 去点亮左侧，
            #    只有一条路径负责「哪些该亮」。
            for i in range(n):
                cv.itemconfigure(self._swipe_items[i], state="hidden")
            self._swipe_shown = 0

        # ① 边界跑到了别的条上：先把旧的那根换回原竖条（此时旧图还有引用）
        if self._swipe_edge_item is not None and self._swipe_edge_item != k:
            j = self._swipe_edge_item
            if j < n:
                cv.itemconfigure(self._swipe_items[j],
                                 image=self._swipe_strips[j])
            self._swipe_edge_item = None
            self._swipe_edge_img = None
            self._swipe_edge_rem = None

        # ② 完整的条：把 [旧值, 新值) 这一段翻成对应状态，其余一律不动
        # ⚠️ 上界要 +1：**旧的那个边界条**（index == 旧 k）也属于可见范围（它是
        #    被裁到 rem 像素的那一根），只刷到 hi 的话它会留在 normal，变成分割线
        #    右侧多出来的 32px 旧版内容 —— 实测就是这么漏的：拖到 25% 时
        #    `normal=[0..8, 16]`，第 16 根没被关掉。
        if k != self._swipe_shown:
            lo, hi = min(self._swipe_shown, k), max(self._swipe_shown, k)
            st = "normal" if k > self._swipe_shown else "hidden"
            for i in range(max(0, lo), min(hi + 1, n)):
                cv.itemconfigure(self._swipe_items[i], state=st)
            self._swipe_shown = k

        # ③ 边界那一根：按 1px 精度裁
        if k < n:
            want = None if rem == 0 else rem
            if force or want != self._swipe_edge_rem:
                if rem == 0:
                    cv.itemconfigure(self._swipe_items[k], state="hidden",
                                     image=self._swipe_strips[k])
                    self._swipe_edge_img = None
                    self._swipe_edge_item = None
                else:
                    img = self._edge_image(k, rem)
                    cv.itemconfigure(self._swipe_items[k], state="normal",
                                     image=img)
                    self._swipe_edge_img = img      # 换完才换引用
                    self._swipe_edge_item = k
                self._swipe_edge_rem = want

    def _swipe_hit(self, side, x, y=None):
        """鼠标是否算「抓住了分割线」。

        分割线横跨整幅图，所以只要 x 够近就算（不需要看 y）；滑钮只在**视口
        中心**那一小块，那 26×44 的片比线宽 3 倍，按在片边缘也该抓住 —— 所以
        在滑钮的纵向范围内把横向容差放宽到 15px。
        """
        if self.view_mode.get() != VIEW_SWIPE or self._swipe_line_item is None:
            return False
        cv = self.canv[side]
        dx = abs(x - (self._swipe_px() - cv.canvasx(0)))
        if dx <= SWIPE_GRAB:
            return True
        if y is None:
            return False
        cy = cv.canvasy(0) + cv.winfo_height() // 2
        return dx <= 15 and abs(y - cy) <= 24

    def _swipe_motion(self, side, event):
        """悬停到分割线附近 → 换成左右拖拽光标。只在状态变化时改。"""
        if self.view_mode.get() != VIEW_SWIPE:
            if self._swipe_hover:
                self._swipe_hover = False
                self.canv[side].configure(cursor="")
            return
        near = self._swipe_hit(side, event.x, event.y)
        if near != self._swipe_hover:
            self._swipe_hover = near
            try:
                self.canv[side].configure(
                    cursor="sb_h_double_arrow" if near else "")
            except tk.TclError:
                pass

    def _on_canvas_resize(self, side, _e=None):
        """画布尺寸变了 → 滑钮要重新钉回视口中心（它纵向跟着视口走）。"""
        if side != "new":
            return
        mode = self.view_mode.get()
        if mode == VIEW_BLINK:
            # 叠加视图里分割线和滑钮都藏着，没有要重排的东西 —— 那颗「旧 / 新」
            # 开关住在底栏、尺寸固定，窗口怎么变都跟它无关（上一版贴在视口
            # 底部，每次 resize 都要重新贴一次，忘了就是「拉大窗口后浮在页面
            # 中间」）。
            return

        if mode != VIEW_SWIPE or self._swipe_line_item is None:
            return
        cv = self.canv["new"]
        cv.coords(self._swipe_line_item, self._swipe_px(), 0,
                  self._swipe_px(), self._swipe_img_h)
        # ⚠️ 走 `_swipe_sync_knob` 而不是自己算 `winfo_height()//2`：滑钮的
        #    纵向位置是**内容坐标**，必须加 `canvasy(0)`（当前滚到哪了）。
        #    早先这里少加那一项，窗口一缩放/一滚动滑钮就往下偏一截。
        self._swipe_sync_knob()

    def _swipe_set(self, frac):
        """设分割位置。**不动控件几何**，只改画布内部图形（0.2~1ms）。"""
        if self.view_mode.get() != VIEW_SWIPE:
            self._swipe_pos = max(VIEW_SWIPE_MIN,
                                  min(VIEW_SWIPE_MAX, float(frac)))
            return
        self._swipe_apply(frac)

    def _swipe_reset(self, _event=None):
        """双击分割线（或画布）→ 回到正中。叠加闪烁时不该理它。"""
        if (self.view_mode.get() == VIEW_SWIPE
                and self._swipe_line_item is not None):
            self._swipe_apply(VIEW_SWIPE_POS)
            return "break"

    # ---------------- 叠加闪烁（2026-10-30 晚，替掉「自动摆动」）----------------
    # 一句话：按时间算「这一刻该显示哪一版」，整段停旧版、整段停新版，硬切不渐变。
    # 为什么用**时间**算而不是「每帧翻一次」：`after(16)` 不保证 16ms 就到，Tk 忙的
    # 时候（刚翻完页在重画）能拖到 40~60ms；按帧翻会让「0.15 秒一版」变成 0.3 秒
    # 甚至更慢，而且抖。按 `time.time()` 算相位，掉帧只是少翻几次，节奏永远是对的。
    #
    # ⚠️ 2026-11-02 下午：多了「关闭自动化」这一档。关掉之后画面交给**底部那颗
    #    「旧 / 新」开关**（左＝整页旧、右＝整页新），用户自己点、自己拖。两条路径
    #    共用同一个画面动作（`_blink_paint` 按 tag 批量改竖条 state），区别只在
    #    「谁来定现在哪一版」：自动 → 定时器按周期跳；手动 → 鼠标事件。
    #    互斥靠 `_blink_auto()`，而且**自动开着时开关要灰掉**（用户明确要求），
    #    所以它常驻可见、只换颜色，避免显隐引起布局跳动。
    def _on_blink_speed(self, _event=None):
        """下拉框变了：换档位，或者切到「关闭」把画面交给手动开关。"""
        try:
            self._blink_idx = max(0, min(BLINK_OFF_IDX,
                                         int(self.cbo_blink.current())))
        except Exception:
            self._blink_idx = BLINK_DEFAULT
        if self.view_mode.get() == VIEW_BLINK:
            # 那句说明也得跟着改：自动时写「整页交替闪现」，关闭后写「点开关换版」，
            # 否则关掉自动化后底栏还在说「切走即停」，用户以为没生效。
            try:
                self.lbl_view_hint.config(
                    text=("旧 ↔ 新 · 整页交替闪现 · 切走即停" if self._blink_auto()
                          else "旧 / 新 · 点右侧小开关换版"))
            except Exception:
                pass
            self._blink_start()          # 重设相位基准，立刻按新节奏走

    def _blink_auto(self) -> bool:
        """现在是自动闪，还是「关闭自动化」（手动滑条）。

        ⚠️ 判据只认 `_blink_idx`，**不读下拉控件的当前值**：控件是"用户看到
           什么"，`_blink_idx` 是"程序按什么跑"。程序自己改档位时控件还没跟上，
           读控件会拿到过时答案。
        """
        return getattr(self, "_blink_idx", BLINK_DEFAULT) != BLINK_OFF_IDX

    def _blink_period(self) -> float:
        i = max(0, min(len(BLINK_PERIODS) - 1, getattr(self, "_blink_idx",
                                                       BLINK_DEFAULT)))
        return BLINK_PERIODS[i][1]

    def _blink_cancel(self):
        if self._blink_id is not None:
            try:
                self.after_cancel(self._blink_id)
            except Exception:
                pass
            self._blink_id = None

    def _blink_active(self) -> bool:
        """现在是不是**叠加视图**。

        ⚠️ 它只回答"该不该在叠加里画"，不回答"该不该自动翻"—— 后者是
           `_blink_running()`。这两件事 2026-11-02 之前是同一个（进了叠加就闪），
           加了「关闭」之后分开了，混用会让定时器在手动模式里继续翻。
        """
        return self.view_mode.get() == VIEW_BLINK

    def _blink_running(self) -> bool:
        """定时器该不该在跑：叠加视图 **且** 自动化开着。"""
        return self._blink_active() and self._blink_auto()

    def _blink_start(self):
        """起闪（或，关掉自动化时，把画面交给底部开关）。"""
        self._blink_cancel()
        if not self._blink_active() or self._swipe_line_item is None:
            # 还没有可比的两页（只拖进来一份 / 还在预览）—— 档位保留，
            # 等 `_render_swipe` 建好画布后由 `_blink_kick()` 接手。
            return
        if not getattr(self, "_blink_manual_ids", None):
            self._blink_manual_build()
        if not self._blink_auto():
            self._blink_manual_sync()
            return
        self._blink_manual_enable(False)   # 自动模式：开关灰掉、按不动
        self._blink_t0 = time.time()
        self._blink_old = None
        self._blink_tick()

    def _blink_stop(self):
        """停闪，并且把画面还原成「分割线」的样子。

        ⚠️ 只 cancel 定时器是不够的：叠加期间画面停在某一版上、线和钮都藏着，
           直接停会让用户看到「一张图、没有分割线、也拖不动」，像坏了。
        """
        self._blink_cancel()
        self._blink_exit()

    def _blink_kick(self):
        """画布刚重画完（换页 / 缩放 / 勾选）时把闪烁接回来。

        重画会把所有竖条连同 tag 一起重建，而且 `_render_swipe` 末尾刚按
        `_swipe_pos` 重排过一遍（＝画成了「分割线」的样子）；此刻旧的定时器其实
        还活着（id 不为 None），只按「没在跑才起」判断会整个跳过 —— 画面就停在
        「一半旧版一半新版」上，直到下一次翻转到点才恢复。所以这里**无条件重起**，
        相位也归零（换页/缩放后从旧版重新开始最自然）。

        ⚠️ 手动开关也要在这里重建：`_render_swipe` 的 `delete("all")` 已经把它
           连同旧 id 一起清了（那边已经把 `_blink_manual_ids` 置空），这里不重建
           的话，关掉自动化换页之后就只剩那张图、开关凭空消失。
        """
        if self._blink_active() and self._swipe_line_item is not None:
            # ⚠️ 条件是 `_blink_active()` 而不是 `_blink_running()`：手动模式下
            #    定时器本来就不该跑，但开关**必须重建**（画布 delete("all") 把它
            #    一起清了）。用 `_blink_running()` 会让「关掉自动化 + 滚一下滚轮」
            #    之后开关凭空消失，而画面还是停在某一版上 —— 没有任何报错。
            self._blink_start()

    def _blink_paint(self, old: bool):
        """整页切到某一版。old=True → 旧版（竖条全亮，盖住底层的新版）。"""
        if self._swipe_line_item is None:
            return
        # 一次 tag 批量搞定 38 根条带（逐根循环要 4ms，按 tag 约 1ms）。
        self.canv["new"].itemconfigure(
            BLINK_TAG, state=("normal" if old else "hidden"))
        self._blink_old = old

    def _blink_enter(self):
        """进叠加：藏掉分割线和滑钮 + 把边界条还原成整根。

        叠加里「左旧右新」这个前提不成立了（整页都是同一版），线杵在中间只是
        一道噪声；滑钮更糟 —— 看着像还能拖，其实 `_swipe_hit` 已经拒绝。
        边界条必须一起复位（`_swipe_uncrop_edge`）：它留着 `rem` 像素的裁剪版时，
        右边那截是透明的，翻到旧版会露出下面新版 —— 就是那根「不跟着动的竖列」。
        """
        if self._swipe_line_item is None:
            return
        cv = self.canv["new"]
        self._swipe_uncrop_edge()
        for it in [self._swipe_line_item] + list(self._swipe_knob_items):
            cv.itemconfigure(it, state="hidden")

    def _blink_exit(self):
        """出叠加：放回线和钮，按记着的位置把竖条重排一次。"""
        ids = getattr(self, "_blink_manual_ids", None)
        if ids:
            # 开关只属于叠加视图 —— 切走就清空（控件本身由 `_pack_tool_right`
            # 收起）。清内容而不是留着，是为了下次进来先画成当前状态，
            # 不会闪一帧旧颜色。
            try:
                self.canv_manual.delete("all")
            except (tk.TclError, AttributeError):
                pass
            self._blink_manual_ids = {}

        if self._swipe_line_item is None:
            self._blink_old = None
            return
        cv = self.canv["new"]
        for it in [self._swipe_line_item] + list(self._swipe_knob_items):
            cv.itemconfigure(it, state="normal")
        # force=True：叠加期间条带是「整片 normal / 整片 hidden」，跟 `_swipe_shown`
        # 记的账早就对不上了 —— 必须全量重排一次，否则线右边会残留整片旧版。
        self._swipe_apply(self._swipe_pos, force=True)
        self._blink_old = None

    def _blink_tick(self):
        self._blink_id = None
        if not self._blink_running() or self._swipe_line_item is None:
            return
        period = self._blink_period()
        # 方波：前半段旧版、后半段新版。**不做渐变** —— 渐变那几帧两版叠在一起
        # 都是糊的，而叠加的全部价值就在「这一帧干净地是 A，下一帧干净地是 B」。
        old = (int((time.time() - self._blink_t0) / period) % 2 == 0)
        if self._blink_old is None:
            self._blink_enter()
        if old != self._blink_old:
            self._blink_paint(old)
        self._blink_id = self.after(BLINK_FRAME_MS, self._blink_tick)

    # ---------------- 叠加视图的「旧 / 新」开关（2026-11-02 晚）----------------
    # 三条不变量，改这里之前先读一遍：
    #   ① 它是**底栏里的一小块画布**（`self.canv_manual`，紧挨频率下拉的左边），
    #      不是画在页面上的图形。上一版浮在视口底部居中，用户原话：「位置有点
    #      尴尬，这不是挡挡字了吗」—— 它压的正是正文最后几行。进了底栏就再也
    #      压不着纸，也省掉「每次滚动/缩放都要把它钉回视口底部」那堆代码。
    #   ② **只有两态**：side=0 整页旧、side=1 整页新。中间那半个像素不存在的 ——
    #      拖着越过中线就立刻翻，松手吸附到那一端。这条是这一版存在的理由：
    #      最早那版画了条长滑轨，用户一眼看成「滑动分割」，语义就废了。
    #   ③ 尺寸**固定**（MANUAL_W 是常数），不跟视口宽拉伸 —— 一旦变成 w-60，
    #      又退化成滑条。
    def _blink_manual_usable(self) -> bool:
        """开关此刻能不能按：叠加视图 + 关掉自动化 + 两版都在。"""
        return (self.view_mode.get() == VIEW_BLINK
                and not self._blink_auto()
                and self._swipe_line_item is not None)

    def _blink_manual_geom(self):
        """开关在**控件坐标**里的几何（控件本身尺寸固定，不随窗口/视口变）。

        返回 dict：
          `cy`     胶囊中线 y
          `left`   胶囊左端 x（圆头最左的点）
          `right`  胶囊右端 x
          `k0`     圆钮圆心能到的最左位置
          `k1`     圆钮圆心能到的最右位置
        """
        cy = MANUAL_BOX_H / 2.0
        left = (MANUAL_BOX_W - MANUAL_W) / 2.0
        right = left + MANUAL_W
        return {"cy": cy, "left": left, "right": right,
                "k0": left + MANUAL_INSET, "k1": right - MANUAL_INSET}

    def _blink_manual_build(self):
        """在底栏那块小画布上画出开关：胶囊槽 + 圆钮 + 两端「旧 / 新」。

        幂等：先 `delete("all")` 再建 —— 那块画布只装开关，没有别的东西，
        整块清掉最省事（旧版要小心绕开页面图形，现在不用了）。

        胶囊用 `create_line(width=..., capstyle="round")` 画：Tk 的矩形不支持
        圆角，而一条够粗的圆头线，两端自然就是半圆 —— 和真开关一个形状，
        还只用一个图形（改色、摆钮都是 O(1)）。
        """
        self._blink_manual_ids = {}
        cv = getattr(self, "canv_manual", None)
        if cv is None or self.view_mode.get() != VIEW_BLINK:
            return
        if self._swipe_line_item is None:
            return
        cv.delete("all")
        g = self._blink_manual_geom()
        cy, left, right = g["cy"], g["left"], g["right"]
        half = MANUAL_H / 2.0
        ids = {}
        # 槽：中线两端各内缩 `half`，再靠圆头补回来 → 外沿正好落在 left/right。
        ids["track"] = cv.create_line(
            left + half, cy, right - half, cy, fill=MANUAL_TRACK,
            width=MANUAL_H, capstyle="round", tags=(MANUAL_TAG,))
        ids["lbl_old"] = cv.create_text(
            left - MANUAL_LBL_GAP, cy, text="旧", anchor="e", fill=MANUAL_EDGE,
            font=(FONT, 9), tags=(MANUAL_TAG,))
        ids["lbl_new"] = cv.create_text(
            right + MANUAL_LBL_GAP, cy, text="新", anchor="w", fill=MANUAL_EDGE,
            font=(FONT, 9), tags=(MANUAL_TAG,))
        # 钮最后建：压在最上面，不然会被槽线穿过去半格。
        ids["knob"] = cv.create_oval(
            0, 0, 0, 0, fill=MANUAL_KNOB, outline=MANUAL_KNOB_EDGE,
            tags=(MANUAL_TAG,))
        self._blink_manual_ids = ids
        self._blink_manual_colors()
        self._blink_manual_knob()

    def _blink_manual_knob(self, frac=None):
        """把圆钮摆到某一侧。`frac` 只给拖动中用（0＝贴左、1＝贴右）。"""
        ids = getattr(self, "_blink_manual_ids", None)
        if not ids:
            return
        try:
            g = self._blink_manual_geom()
            if frac is None:
                # 拖动中没有实时位置时，直接贴到当前那一端（吸附后的静止态）。
                frac = getattr(self, "_blink_manual_dragfrac", None)
                if frac is None:
                    frac = 1.0 if getattr(self, "_blink_side", 0) else 0.0
            frac = max(0.0, min(1.0, float(frac)))
            cx = g["k0"] + (g["k1"] - g["k0"]) * frac
            r = MANUAL_KNOB_R
            self.canv_manual.coords(ids["knob"], cx - r, g["cy"] - r,
                                    cx + r, g["cy"] + r)
        except (tk.TclError, KeyError, AttributeError):
            self._blink_manual_ids = {}

    def _blink_manual_colors(self):
        """按「能不能按 + 现在哪一版」给槽 / 钮 / 两端字上色。

        灰掉只是换色，**不隐藏** —— 隐藏会引起布局跳动，而且用户看不到
        「这里本来有个东西、只是自动开着时用不上」。
        """
        ids = getattr(self, "_blink_manual_ids", None)
        if not ids:
            return
        on = bool(getattr(self, "_blink_manual_on", False))
        new = bool(getattr(self, "_blink_side", 0))
        if not on:
            track, knob = MANUAL_TRACK_OFF, MANUAL_KNOB_OFF
            old_fg = new_fg = MANUAL_OFF_EDGE
        else:
            track = MANUAL_TRACK_ON if new else MANUAL_TRACK
            knob = MANUAL_KNOB
            # 选中的那一端加深，另一端浅 —— 只靠钮的位置判断"现在哪一版"，
            # 眼睛要在两个几乎一样的位置之间比对，太费劲。
            old_fg, new_fg = (MANUAL_EDGE, TEXT) if new else (TEXT, MANUAL_EDGE)
        cv = self.canv_manual
        try:
            cv.itemconfigure(ids["track"], fill=track)
            cv.itemconfigure(ids["knob"], fill=knob)
            cv.itemconfigure(ids["lbl_old"], fill=old_fg)
            cv.itemconfigure(ids["lbl_new"], fill=new_fg)
        except tk.TclError:
            self._blink_manual_ids = {}

    def _blink_manual_enable(self, on: bool):
        """亮/灰开关（自动开着时灰），并把光标切成「能按」的手形。"""
        self._blink_manual_on = bool(on)
        self._blink_manual_colors()
        try:
            self.canv_manual.configure(cursor="hand2" if on else "")
        except tk.TclError:
            pass

    def _blink_manual_sync(self):
        """进入手动模式：藏掉分割线、建开关、点亮它、按记着的那一版摆好。"""
        if self._swipe_line_item is None:
            return
        self._blink_enter()          # 藏线和滑钮 + 边界条复位
        if not getattr(self, "_blink_manual_ids", None):
            self._blink_manual_build()
        self._blink_manual_enable(True)
        self._blink_manual_dragfrac = None
        side = getattr(self, "_blink_side", None)
        if side is None:
            # 第一次关掉自动化：接着刚才闪到的那一版，别跳一下。
            side = 0 if self._blink_old in (True, None) else 1
        self._blink_set_side(side, force=True)

    def _blink_set_side(self, side, force=False):
        """**整页**切到某一版。这是手动模式唯一的画面动作。

        `side=0` 旧版 / `side=1` 新版，没有第三种取值 —— 参数是布尔就够，不必
        传一个 0~1 的连续量。拖动越过中线才调这里，所以画面永远"一整版"，
        绝不会出现左旧右新的混合（那正是上一版长滑轨被否掉的原因）。

        底层就是 `_blink_paint`（自动闪用的同一个函数）：它按 tag 一次性改所有
        竖条的 state，约 1ms。手动模式复用它是**故意的** —— 两条路的像素结果
        必须完全一致，否则"关掉自动化"那一瞬画面会抖一下。
        """
        side = 1 if side else 0
        changed = (side != getattr(self, "_blink_side", None))
        self._blink_side = side
        if not (changed or force):
            self._blink_manual_knob()
            return
        self._blink_paint(side == 0)            # side=0(旧) → old=True
        self._blink_manual_colors()
        self._blink_manual_knob()

    def _blink_manual_press(self, event):
        """按下开关：先**不动画面**，等松开时看是「点一下」还是「拖过去」。

        ⚠️ 不能按下就翻：用户想「拖到右边」时，按下的那一瞬钮还在左边，
           立刻翻会先闪一下再翻回来，看着像抖。
        ⚠️ 事件来自 `self.canv_manual`（底栏那块小画布），`event.x` 已经是
           控件坐标，**不用**再 `canvasx(0)` 换算 —— 上一版画在页面画布上，
           才需要把屏幕坐标换回内容坐标。
        """
        if not self._blink_manual_usable():
            return "break"
        self._blink_manual_dragging = True
        self._blink_manual_moved = False
        self._blink_manual_press_x = event.x
        return "break"

    def _blink_manual_drag(self, event):
        """按住开关横向拖：钮跟手，越过中线立刻翻版。

        ⚠️ 这里是「一瞬间切换」的落点：判断只有 `frac > 0.5` 一句，没有渐变、
           没有插值。钮可以停在半路（视觉跟手），但**画面**永远整页一整版。
        ⚠️ 3px 死区：按下时手抖一下不该算拖动 —— 否则「点一下翻版」这个动作
           会有一半概率变成"没翻"。
        """
        if not getattr(self, "_blink_manual_dragging", False):
            return "break"
        g = self._blink_manual_geom()
        x = event.x
        if not getattr(self, "_blink_manual_moved", False):
            if abs(x - getattr(self, "_blink_manual_press_x", x)) <= 3:
                return "break"
            self._blink_manual_moved = True
        span = max(1.0, float(g["k1"] - g["k0"]))
        frac = max(0.0, min(1.0, (x - g["k0"]) / span))
        self._blink_manual_dragfrac = frac
        self._blink_set_side(1 if frac > 0.5 else 0)
        self._blink_manual_knob(frac)
        return "break"


    def _blink_manual_release(self):
        """松手：没挪动过就是「点一下」→ 翻版；拖过就吸附到当前那一端。"""
        if not getattr(self, "_blink_manual_dragging", False):
            return
        self._blink_manual_dragging = False
        if not getattr(self, "_blink_manual_moved", False):
            self._blink_manual_toggle()
        self._blink_manual_dragfrac = None
        self._blink_manual_knob()

    def _blink_manual_toggle(self):
        """点一下 → 翻到另一版。"""
        self._blink_set_side(0 if getattr(self, "_blink_side", 0) else 1)
